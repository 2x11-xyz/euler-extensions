"""The three command handlers over the managed-process ``Host``.

``update`` is one checkpointed projection tick; ``catch-up`` runs bounded ticks
under a strict host-request budget and hands the cursor back when it cannot
finish (the resumable design that fixes the old 81 > 64 over-budget failure);
``export`` renders the active artifact. Handlers return plain dicts and raise
``ValueError`` with plain-language messages — the SDK maps any exception to a
generic ``-32000`` for the wire, but the message still serves the logs.

The active projection state lives in the session-scoped extension private state
dir (``state_dir()``, capability ``fs-write``): a small ``active.json`` pointer
carrying the last artifact's event id, sha256, path, watermark, and a cached
copy of the artifact JSON so an incremental tick can extend it without
re-reading all of provenance. The file is replaced atomically (temp + rename).
Reading and writing that file is the process's own I/O, not a host API.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Mapping, Optional

from .exports import to_dot, to_markdown, to_summary
from .invariants import check
from .projection import QUERY_KINDS, is_self_error, project_tick, split_closed
from .schema import Artifact, EPOCH, MEDIA_TYPE, SCHEMA, dumps, loads
from .viewer import VIEWS, render_html

_CHECKPOINT = "main"
_SLOT = "graph"
_STATE_FILE = "active.json"
_DEFAULT_LIMIT = 64
_MAX_LIMIT = 128
_DEFAULT_MAX_TICKS = 8
_MAX_TICKS = 10
# Strict budget: the host caps a single invocation at 64 requests. A writing
# tick spends ~6 (state-dir, load-checkpoint, query, write, slot, store); we
# stop before 58 so the worst-case next tick still lands under 64 with slack.
_REQUEST_BUDGET = 58
_TICK_COST = 6
# Byte-budget model (§ degraded-spine size ceiling). The host frames artifact
# bytes as base64 (×4/3) inside a JSON-RPC message; 1KB covers the envelope.
# A single message is capped near 1 MiB and total output near 4 MiB — we halt
# below those with margin rather than wedge on a host ProtocolError. When the
# spine hits the ceiling the observer lane (milestone 4) supersedes it.
_MESSAGE_BYTE_LIMIT = 900_000
_OUTPUT_BYTE_BUDGET = 3_000_000
_GROWTH_ESTIMATE = 65_536  # per-tick artifact growth headroom for catch-up


def _framed_bytes(n: int) -> int:
    """Estimated on-the-wire size of an ``n``-byte artifact payload."""
    return (n * 4) // 3 + 1024

_EXPORT_MEDIA = {
    "json": MEDIA_TYPE,
    "dot": "text/vnd.graphviz",
    "markdown": "text/markdown",
    "summary": "text/plain; charset=utf-8",
    "html": "text/html; charset=utf-8",
}


class _Ledger:
    """A thin ``Host`` wrapper that counts host requests.

    catch-up reads ``requests`` to hard-stop before the 64-request invocation
    cap. Every counted method is a real JSON-RPC round trip; local file I/O on
    the state pointer is not counted because it is not a host request.
    """

    def __init__(self, host: Any) -> None:
        self._host = host
        self.requests = 0
        self.bytes_written = 0
        self.last_artifact_bytes = 0

    def state_dir(self) -> str:
        self.requests += 1
        return self._host.state_dir()

    def query_provenance(self, **kwargs: Any) -> Dict[str, Any]:
        self.requests += 1
        return self._host.query_provenance(**kwargs)

    def load_checkpoint(self, name: str) -> Optional[Dict[str, Any]]:
        self.requests += 1
        return self._host.load_checkpoint(name)

    def store_checkpoint(self, name: str, checkpoint: Mapping[str, Any]) -> None:
        self.requests += 1
        self._host.store_checkpoint(name, checkpoint)

    def write_artifact(self, **kwargs: Any) -> Dict[str, Any]:
        self.requests += 1
        framed = _framed_bytes(len(kwargs.get("data") or b""))
        self.bytes_written += framed
        self.last_artifact_bytes = framed
        return self._host.write_artifact(**kwargs)

    def update_context_slot(self, slot: str, content: str) -> None:
        self.requests += 1
        self._host.update_context_slot(slot, content)


def _load_state(path: str) -> "tuple[Optional[Dict[str, Any]], Optional[str]]":
    """Read ``active.json``, distinguishing missing from corrupt (FIX 5).

    Returns ``(state, was)`` where ``was`` is ``None`` on a clean read,
    ``"missing"`` when the file is absent, or ``"corrupt"`` when it exists but
    is unreadable/malformed (invalid JSON, or not a dict). A corrupt pointer is
    treated as absent so the caller can self-heal.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        return None, "missing"
    except (json.JSONDecodeError, ValueError, OSError):
        return None, "corrupt"
    if not isinstance(state, dict):
        return None, "corrupt"
    return state, None


def _state_path(ledger: "_Ledger") -> str:
    # The host derives state_dir from the session path as the USER typed it, so
    # an offline `euler extension run rel/events.jsonl` hands us a relative
    # path — unresolvable from our cwd (the package dir). Diagnose it instead
    # of failing later with a bare FileNotFoundError deep in a state write.
    state_dir = ledger.state_dir()
    if not os.path.isdir(state_dir):
        raise ValueError(
            f"state_dir {state_dir!r} is not reachable from the extension "
            "(relative session path?); re-run with an absolute session path")
    return os.path.join(state_dir, _STATE_FILE)


def _write_state_atomic(path: str, state: Dict[str, Any]) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False)
    os.replace(tmp, path)


def _positive_int(value: Any, name: str, default: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return min(value, maximum)


def _tick(ledger: _Ledger, limit: int) -> Dict[str, Any]:
    """One projection tick: extend the artifact from the next provenance page.

    Segmentation is pagination-invariant (``split_closed``): the open turn is
    carried across ticks in ``active.json``'s ``pending``, so node structure is
    identical for any ``limit``. A crash between the state write and the
    checkpoint advance replays nothing: the state's own cursor is authoritative
    when present, so the next tick resumes exactly where segmentation stopped.
    Empty/self-only/no-close pages short-circuit but still advance the
    checkpoint and persist ``pending``, so catch-up drains the feed (including
    the extension.artifact events this command appends) and terminates.
    """
    state_path = _state_path(ledger)
    prior_state, state_was = _load_state(state_path)
    checkpoint = ledger.load_checkpoint(_CHECKPOINT)
    cursor = checkpoint.get("after_event_id") if checkpoint else None

    # FIX 5: pointer lost mid-stream — ignore the stale cursor and rebuild the
    # whole graph from the beginning as an honest fresh snapshot.
    recovered = None
    if prior_state is None and cursor is not None:
        recovered = state_was  # "corrupt" | "missing"
        cursor = None

    # The state records the cursor it is synced to, and every tick writes the
    # state BEFORE advancing the checkpoint — so on a crash between the two,
    # state.cursor is ahead and authoritative. Querying from it means no
    # already-segmented event is ever replayed into the projector (replaying
    # would re-cut the open pending turn at stale boundaries). The host
    # checkpoint is the durable backup that FIX 5 falls back to when the state
    # file itself is lost.
    if prior_state and prior_state.get("cursor"):
        cursor = prior_state["cursor"]

    prior_art = (Artifact.from_dict(prior_state["artifact"])
                 if prior_state and prior_state.get("artifact") else None)
    pending: List[Any] = [list(p) for p in (prior_state.get("pending") or [])] \
        if prior_state else []
    prior_count = len(prior_art.nodes) if prior_art else 0

    page = ledger.query_provenance(after_event_id=cursor, kinds=QUERY_KINDS, limit=limit)
    events = [e for e in (page.get("events") or []) if not is_self_error(e)]
    new_cursor = page.get("next_after_event_id") or page.get("watermark_event_id") or cursor
    has_more = bool(page.get("truncated"))

    def advance_checkpoint() -> None:
        if new_cursor and new_cursor != cursor:
            ledger.store_checkpoint(
                _CHECKPOINT, {"schema_version": 1, "after_event_id": new_cursor})

    def skip(**extra: Any) -> Dict[str, Any]:
        out = {"projected": False, "turns_added": 0, "node_count": prior_count,
               "watermark_event_id": new_cursor, "has_more": has_more}
        if recovered:
            out["recovered"] = "state-rebuilt"
            out["state_was"] = recovered
        out.update(extra)
        return out

    # Belt-and-braces: a pending entry the artifact already owns (state
    # tampering or an unforeseen replay hole) would double-own an event and
    # wedge every subsequent tick on the invariant checker. Drop it here.
    prior_owned = {ev for n in (prior_art.nodes if prior_art else [])
                   for t in n.turns for ev in t.event_ids}
    pending = [p for p in pending if p[0] not in prior_owned]

    closed_turns, new_pending = split_closed(pending, events, has_more)

    if not closed_turns:
        # Nothing closed: persist the (possibly grown) open tail and move on.
        _write_state_atomic(
            state_path, _carry_state(prior_state, prior_art, new_pending, new_cursor))
        advance_checkpoint()
        return skip()

    closed_ids = {eid for turn in closed_turns for eid, _kind in turn}
    timestamps = [event["ts"] for event in events
                  if event.get("ts") and event["id"] in closed_ids]
    generated_at = max(timestamps) if timestamps else (
        prior_art.generated_at if prior_art else EPOCH)

    artifact = project_tick(
        prior_art, closed_turns,
        session_id=(events[0].get("session") if events else None)
        or (prior_art.session.id if prior_art else "session"),
        watermark=new_cursor or closed_turns[-1][-1][0],
        generated_at=generated_at,
        predecessor_artifact_event_id=prior_state.get("artifact_event_id") if prior_state else None,
        predecessor_watermark_event_id=prior_state.get("watermark") if prior_state else None,
    )

    findings = check(artifact)
    if findings:
        raise ValueError(
            "projected artifact failed the invariant checker: "
            + "; ".join(f"{f.invariant}: {f.message}" for f in findings[:5]))

    turns_added = len(artifact.nodes) - prior_count
    data = dumps(artifact).encode("utf-8")

    # FIX 4a: single-message guard — an artifact past the wire cap is a diagnosed
    # halt, not a host ProtocolError. Do not write; do not advance the checkpoint.
    framed = _framed_bytes(len(data))
    if framed > _MESSAGE_BYTE_LIMIT:
        # node_count is the DURABLE artifact's — the oversized one was never
        # written; would_be_node_count is what the halt refused to write.
        return {"projected": False, "halted": "artifact-size-limit",
                "artifact_bytes": framed, "turns_added": 0,
                "node_count": prior_count,
                "would_be_node_count": len(artifact.nodes),
                "watermark_event_id": new_cursor, "has_more": has_more}

    source_event_ids = sorted(closed_ids)
    record = ledger.write_artifact(
        display_name="Causal DAG",
        media_type=MEDIA_TYPE,
        data=data,
        source_event_ids=source_event_ids,
        metadata={
            "schema": SCHEMA,
            "degraded": True,
            "operation": artifact.construction.operation,
            "node_count": len(artifact.nodes),
            "edge_count": len(artifact.edges),
            "watermark_event_id": artifact.projection.watermark_event_id,
        },
    )

    _write_state_atomic(state_path, {
        "artifact_event_id": record.get("persisted_event_id"),
        "sha256": record.get("sha256"),
        "path": record.get("relative_path"),
        "watermark": artifact.projection.watermark_event_id,
        "artifact": artifact.to_dict(),
        "pending": new_pending,
        "cursor": new_cursor,
    })
    ledger.update_context_slot(_SLOT, to_summary(artifact, 4096))
    advance_checkpoint()

    result = {
        "projected": True,
        "turns_added": turns_added,
        "node_count": len(artifact.nodes),
        "edge_count": len(artifact.edges),
        "operation": artifact.construction.operation,
        "artifact_event_id": record.get("persisted_event_id"),
        "sha256": record.get("sha256"),
        "path": record.get("relative_path"),
        "watermark_event_id": artifact.projection.watermark_event_id,
        "has_more": has_more,
    }
    if recovered:
        result["recovered"] = "state-rebuilt"
        result["state_was"] = recovered
    return result


def _carry_state(prior_state: Optional[Dict[str, Any]], prior_art: Optional[Artifact],
                 pending: List[Any], cursor: Optional[str]) -> Dict[str, Any]:
    """State pointer for a no-close tick: keep the prior artifact, update pending."""
    return {
        "artifact_event_id": prior_state.get("artifact_event_id") if prior_state else None,
        "sha256": prior_state.get("sha256") if prior_state else None,
        "path": prior_state.get("path") if prior_state else None,
        "watermark": prior_state.get("watermark") if prior_state else None,
        "artifact": prior_art.to_dict() if prior_art else None,
        "pending": pending,
        "cursor": cursor,
    }


def _input_object(context: Any) -> Dict[str, Any]:
    value = context.input
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("input must be a JSON object")
    return value


def update(context: Any) -> Dict[str, Any]:
    """One checkpointed projection tick."""
    fields = _input_object(context)
    _reject_unknown(fields, {"limit"})
    limit = _positive_int(fields.get("limit"), "limit", _DEFAULT_LIMIT, _MAX_LIMIT)
    ledger = _Ledger(context.host)
    result = _tick(ledger, limit)
    result["requests_used"] = ledger.requests
    return result


def catch_up(context: Any) -> Dict[str, Any]:
    """Bounded ticks until caught up or the request budget is nearly spent.

    Caught up means a tick both projected nothing new and reported no more
    pages. When the budget or ``max_ticks`` runs out first, the checkpoint is
    already durable, so the returned ``continue: true`` tells the caller to
    re-invoke and resume exactly where this run stopped.
    """
    fields = _input_object(context)
    _reject_unknown(fields, {"max_ticks", "limit"})
    max_ticks = _positive_int(fields.get("max_ticks"), "max_ticks", _DEFAULT_MAX_TICKS, _MAX_TICKS)
    limit = _positive_int(fields.get("limit"), "limit", _DEFAULT_LIMIT, _MAX_LIMIT)

    ledger = _Ledger(context.host)
    ticks: List[Dict[str, Any]] = []
    caught_up = False
    stopped: Optional[str] = None
    for _ in range(max_ticks):
        if ledger.requests + _TICK_COST > _REQUEST_BUDGET:
            break
        # FIX 4b: stop before a tick whose write could push cumulative output
        # past the budget (estimate: current artifact size + growth headroom).
        if (ledger.bytes_written + ledger.last_artifact_bytes + _GROWTH_ESTIMATE
                > _OUTPUT_BYTE_BUDGET):
            stopped = "byte-budget"
            break
        result = _tick(ledger, limit)
        ticks.append(result)
        if result.get("halted"):
            break
        if not result["has_more"] and not result["projected"]:
            caught_up = True
            break

    projected = [tick for tick in ticks if tick["projected"]]
    last = ticks[-1] if ticks else {}
    # FIX 6: node_count is the latest PROJECTING tick's count (or the prior
    # artifact's, surfaced by any tick when none projected).
    node_count = (projected[-1]["node_count"] if projected
                  else last.get("node_count", 0))
    # A halt is a diagnosis, not a pause: re-invoking replays the same
    # oversized projection, so continue must be false — the caller escalates
    # instead of spinning.
    halted = next((tick["halted"] for tick in ticks if tick.get("halted")), None)
    out = {
        "caught_up": caught_up,
        "continue": not caught_up and halted is None,
        "ticks_run": len(ticks),
        "ticks_projected": len(projected),
        "turns_added": sum(tick.get("turns_added", 0) for tick in ticks),
        "node_count": node_count,
        "watermark_event_id": last.get("watermark_event_id"),
        "artifact_event_id": (projected[-1].get("artifact_event_id") if projected else None),
        "requests_used": ledger.requests,
    }
    if stopped:
        out["stopped"] = stopped
    if halted:
        out["halted"] = halted
    return out


def export(context: Any) -> Dict[str, Any]:
    """Render the active artifact to ``format`` and write it as an artifact."""
    fields = _input_object(context)
    _reject_unknown(fields, {"format", "view"})
    fmt = fields.get("format")
    if fmt not in _EXPORT_MEDIA:
        raise ValueError(
            "format must be one of json, dot, markdown, summary, html")
    view = fields.get("view")
    if view is not None and view not in VIEWS:
        raise ValueError(f"view must be one of {sorted(VIEWS)}")

    ledger = _Ledger(context.host)
    state_path = _state_path(ledger)
    state, _was = _load_state(state_path)
    if state is None or not state.get("artifact"):
        raise ValueError("no active causal-dag artifact; run update first")
    artifact = Artifact.from_dict(state["artifact"])

    if fmt == "json":
        text = dumps(artifact)
    elif fmt == "dot":
        text = to_dot(artifact)
    elif fmt == "markdown":
        text = to_markdown(artifact)
    elif fmt == "summary":
        text = to_summary(artifact, 4096)
    else:  # html
        text = render_html(artifact, view or "top-down")

    record = ledger.write_artifact(
        display_name=f"Causal DAG ({fmt})",
        media_type=_EXPORT_MEDIA[fmt],
        data=text.encode("utf-8"),
        source_event_ids=[],
        metadata={"schema": SCHEMA, "format": fmt,
                  "view": (view or "top-down") if fmt == "html" else None},
    )
    return {
        "format": fmt,
        "view": (view or "top-down") if fmt == "html" else None,
        "persisted_event_id": record.get("persisted_event_id"),
        "relative_path": record.get("relative_path"),
        "sha256": record.get("sha256"),
        "byte_len": record.get("byte_len"),
        "node_count": len(artifact.nodes),
        "edge_count": len(artifact.edges),
        "requests_used": ledger.requests,
    }


def _reject_unknown(fields: Mapping[str, Any], allowed: set) -> None:
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise ValueError(f"unknown input field(s): {', '.join(unknown)}")


HANDLERS = {"update": update, "catch-up": catch_up, "export": export}
