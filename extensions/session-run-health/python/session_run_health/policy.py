"""Pure event fold and evidence-backed run-health policy.

The fold consumes canonical event metadata only. It never reads model reasoning
payloads, retains user/tool text, or treats model-call/transport liveness as
meaningful progress.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional


EXTENSION_ID = "session-run-health"
CONTEXT_SLOT = "session-run-health"
MAX_TRACKED_FAILURES = 32
MAX_TRACKED_FILES = 64
MAX_TRACKED_CALLS = 256
MAX_EVIDENCE_IDS = 8
MAX_CHECKPOINT_SIGNALS = 5

_SIGNAL_ORDER = {
    "repeated_failure": 0,
    "edit_thrashing": 1,
    "no_meaningful_progress": 2,
    "context_cache_churn": 3,
    "active_run_input": 4,
}

_PYTHON_EXECUTABLE = re.compile(r"^python(?:3(?:\.\d+)?)?$")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", re.DOTALL)
_OPERATION_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_.+-]*$")

_PROGRESS_KINDS = {
    "user.message",
    "assistant.message",
    "assistant.response.chunk",
    "tool.result",
    "permission.decision",
    "file.change",
    "workspace.restore",
    "check.result",
    "model.result",
    "plan.update",
    "run.terminal",
    "error",
}

_IGNORED_MODEL_KINDS = {"model.reasoning", "model.delta"}

# The bounded feed excludes model.reasoning/model.delta entirely. Content-bearing
# events needed for lifecycle/progress are admitted but never retained. The only
# text classification is a conservative in-memory validation-command signature;
# the command itself is discarded immediately.
OBSERVED_KINDS = sorted(
    {
        "user.message",
        "assistant.message",
        "assistant.response.chunk",
        "plan.update",
        "tool.call",
        "tool.result",
        "permission.prompt",
        "permission.decision",
        "file.change",
        "workspace.restore",
        "check.result",
        "model.call",
        "model.result",
        "model.switched",
        "context.limit",
        "canvas.snapshot",
        "canvas.swap",
        "canvas.candidate.discarded",
        "extension.artifact",
        "context.slot.updated",
        "session.start",
        "run.started",
        "run.terminal",
        "error",
    }
)


def fold_events(
    state: Dict[str, Any],
    events: Iterable[Dict[str, Any]],
    thresholds: Dict[str, int],
) -> None:
    for event in events:
        fold_event(state, event, thresholds)


def fold_event(
    state: Dict[str, Any], event: Dict[str, Any], thresholds: Dict[str, int]
) -> None:
    event_id, kind, timestamp, agent, run_id = _event_header(event)
    if kind in _IGNORED_MODEL_KINDS:
        return
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("provenance event payload must be an object")
    if _is_own_effect(kind, payload):
        _reconcile_own_effect(state, kind, event_id, payload)
        return

    if state["root_agent"] is None:
        if kind not in ("session.start", "run.started"):
            return
        _establish_root_owner(state, agent)
    elif kind == "session.start" or agent != state["root_agent"]:
        return

    sequence = state["observed_sequence"] + 1
    if sequence >= (1 << 63):
        raise ValueError("run-health observation sequence is exhausted")
    state["observed_sequence"] = sequence
    state["latest_observed_event_id"] = event_id
    state["latest_observed_ts"] = timestamp

    if kind == "run.started" and run_id is not None:
        state["open_runs"][run_id] = {
            "messages": 0,
            "alerted_count": 0,
            "event_ids": [],
            "last_input_sequence": 0,
        }
    elif kind == "run.terminal":
        if run_id is not None:
            state["open_runs"].pop(run_id, None)
        _reset_run_local_tracks(state)
    elif kind == "user.message":
        _fold_user_message(state, event_id, run_id)
    elif kind == "tool.call":
        _fold_tool_call(state, payload, sequence)
    elif kind == "tool.result":
        _fold_tool_result(state, event_id, payload, sequence)
    elif kind == "check.result":
        _fold_check_result(state, event_id, payload, sequence)
    elif kind == "file.change":
        _fold_file_change(state, event_id, payload, sequence)
    elif kind == "permission.prompt":
        state["waiting_permission"] = True
    elif kind == "permission.decision":
        state["waiting_permission"] = False
    elif kind == "model.call":
        if payload.get("purpose") is None:
            state["open_root_calls"][agent] = event_id
    elif kind == "model.result":
        if payload.get("purpose") is None:
            state["open_root_calls"].pop(agent, None)
            _fold_usage_sample(state, event_id, payload, thresholds, sequence)
    elif kind == "model.switched":
        _switch_usage_target(
            state,
            payload.get("to_provider"),
            payload.get("to_model"),
        )
    elif kind == "canvas.swap":
        state["usage_samples"] = []
        state["context_alerted"] = False
    elif kind == "error":
        if payload.get("purpose") is None:
            _fold_error(state, event_id, agent, payload, sequence)

    compaction_terminal = (
        kind in ("model.result", "error") and payload.get("purpose") == "compaction"
    )
    if kind in _PROGRESS_KINDS and not compaction_terminal:
        state["last_progress_event_id"] = event_id
        state["last_progress_ts"] = timestamp
        state["last_progress_sequence"] = sequence
        if state["no_progress_alerted_for"] != event_id:
            state["no_progress_alerted_for"] = None


def collect_signals(
    state: Dict[str, Any], thresholds: Dict[str, int]
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for key, track in state["failure_tracks"].items():
        if track["count"] >= thresholds["failure_recurrence"] and not track["alerted"]:
            candidates.append(
                {
                    "kind": "repeated_failure",
                    "count": track["count"],
                    "event_ids": list(track["event_ids"]),
                    "_entity": key,
                    "_sequence": track["first_seen_sequence"],
                }
            )

    for key, track in state["edit_tracks"].items():
        if track["count"] >= thresholds["edit_recurrence"] and not track["alerted"]:
            candidates.append(
                {
                    "kind": "edit_thrashing",
                    "count": track["count"],
                    "event_ids": list(track["event_ids"]),
                    "_entity": key,
                    "_sequence": track["first_seen_sequence"],
                }
            )

    no_progress = _no_progress_candidate(state, thresholds)
    if no_progress is not None:
        candidates.append(no_progress)

    context_churn = _context_churn_candidate(state, thresholds)
    if context_churn is not None:
        candidates.append(context_churn)

    for run_id, run in state["open_runs"].items():
        accepted_inputs = max(0, run["messages"] - 1)
        if (
            accepted_inputs - run["alerted_count"]
            >= thresholds["active_run_input_recurrence"]
        ):
            candidates.append(
                {
                    "kind": "active_run_input",
                    "count": accepted_inputs,
                    "event_ids": list(run["event_ids"]),
                    "_entity": _fingerprint("run", run_id),
                    "_sequence": run["last_input_sequence"],
                }
            )

    candidates.sort(
        key=lambda signal: (
            signal["_sequence"],
            _SIGNAL_ORDER[signal["kind"]],
            signal["_entity"],
        )
    )
    selected = candidates[:MAX_CHECKPOINT_SIGNALS]
    for signal in selected:
        _mark_signal_alerted(state, signal)
    return selected


def make_pending_checkpoint(
    state: Dict[str, Any],
    signals: List[Dict[str, Any]],
    cutoff_event_id: str,
    thresholds: Dict[str, int],
) -> Dict[str, Any]:
    if not 1 <= len(signals) <= MAX_CHECKPOINT_SIGNALS:
        raise ValueError("run-health checkpoint signal count is invalid")
    if any(state[name] is not None for name in (
        "pending_checkpoint",
        "active_checkpoint",
        "pending_retirement",
    )):
        raise ValueError("run-health already owns a checkpoint transition")
    revision = state["revision"] + 1
    if revision >= (1 << 63):
        raise ValueError("run-health checkpoint revision is exhausted")
    signature = checkpoint_signature(
        revision,
        cutoff_event_id,
        signals,
        thresholds,
    )
    pending = {
        "revision": revision,
        "signature": signature,
        "cutoff_event_id": cutoff_event_id,
        "signals": signals,
        "thresholds": dict(thresholds),
        "artifact_event_id": None,
        "plan_published": False,
        "context_published": False,
    }
    state["revision"] = revision
    state["pending_checkpoint"] = pending
    return pending


def artifact_document(pending: Dict[str, Any]) -> Dict[str, Any]:
    return {
        **public_checkpoint_fields(
            pending["revision"],
            pending["cutoff_event_id"],
            pending["signals"],
            pending["thresholds"],
        ),
        "signature": pending["signature"],
    }


def public_checkpoint_fields(
    revision: int,
    cutoff_event_id: str,
    signals: List[Dict[str, Any]],
    thresholds: Dict[str, int],
) -> Dict[str, Any]:
    public_signals = [_public_signal(signal) for signal in signals]
    return {
        "schema_version": 1,
        "revision": revision,
        "cutoff_event_id": cutoff_event_id,
        "signals": public_signals,
        "thresholds": dict(thresholds),
        "recovery_options": _recovery_options(public_signals),
    }


def checkpoint_signature(
    revision: int,
    cutoff_event_id: str,
    signals: List[Dict[str, Any]],
    thresholds: Dict[str, int],
) -> str:
    public_fields = public_checkpoint_fields(
        revision,
        cutoff_event_id,
        signals,
        thresholds,
    )
    return hashlib.sha256(
        json.dumps(public_fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def checkpoint_is_unresolved(state: Dict[str, Any], checkpoint: Dict[str, Any]) -> bool:
    return any(
        _signal_is_unresolved(state, signal, checkpoint["thresholds"])
        for signal in checkpoint["signals"]
    )


def activate_pending_checkpoint(state: Dict[str, Any]) -> None:
    pending = state["pending_checkpoint"]
    if pending is None:
        raise ValueError("run-health has no pending checkpoint to activate")
    if (
        pending["artifact_event_id"] is None
        or not pending["plan_published"]
        or not pending["context_published"]
    ):
        raise ValueError("run-health checkpoint effects are incomplete")
    state["active_checkpoint"] = {
        key: pending[key]
        for key in (
            "revision",
            "signature",
            "cutoff_event_id",
            "signals",
            "thresholds",
        )
    }
    state["active_checkpoint"]["context_published"] = True
    state["pending_checkpoint"] = None


def discard_or_retire_stale_checkpoint(state: Dict[str, Any]) -> bool:
    """Prepare retirement when stale surfaces may already be externally active."""

    pending = state["pending_checkpoint"]
    if pending is not None and not checkpoint_is_unresolved(state, pending):
        surfaces_visible = pending["plan_published"] or pending["context_published"]
        source_signature = pending["signature"]
        state["pending_checkpoint"] = None
        if surfaces_visible:
            _prepare_retirement(state, source_signature)
        return True

    active = state["active_checkpoint"]
    if active is not None and not checkpoint_is_unresolved(state, active):
        source_signature = active["signature"]
        state["active_checkpoint"] = None
        _prepare_retirement(state, source_signature)
        return True
    return False


def retirement_plan_presentation(retirement: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "revision": retirement["revision"],
        "status": "completed",
        "explanation": "Strategy checkpoint retired after observed recovery",
        "items": [
            {
                "step": "Run-health strategy checkpoint retired",
                "status": "completed",
            }
        ],
    }


def _prepare_retirement(state: Dict[str, Any], source_signature: str) -> None:
    revision = state["revision"] + 1
    if revision >= (1 << 63):
        raise ValueError("run-health retirement revision is exhausted")
    state["revision"] = revision
    state["pending_retirement"] = {
        "revision": revision,
        "source_signature": source_signature,
        "plan_completed": False,
        "context_cleared": False,
    }


def render_context_slot(pending: Dict[str, Any]) -> str:
    lines = [
        f"Strategy checkpoint r{pending['revision']} · {pending['signature'][:12]}",
        "Evidence (content-free):",
    ]
    for signal in pending["signals"]:
        lines.append(f"- {_signal_summary(signal)}")
    lines.append("Recovery options:")
    for option in _recovery_options(pending["signals"]):
        lines.append(f"- {option}")
    lines.append("This advisory does not impose a round or duration limit.")
    rendered = "\n".join(lines)
    if len(rendered.encode("utf-8")) > 4096:
        raise RuntimeError("run-health context renderer exceeded its fixed byte budget")
    return rendered


def plan_presentation(pending: Dict[str, Any]) -> Dict[str, Any]:
    summaries = "; ".join(_signal_summary(signal) for signal in pending["signals"])
    explanation = f"Strategy checkpoint: {summaries}"
    items = [
        {"step": "Review the bounded run-health evidence", "status": "in_progress"},
        {"step": "Choose a different or narrower next strategy", "status": "pending"},
        {"step": "Obtain fresh validation evidence before repeating work", "status": "pending"},
    ]
    return {
        "revision": pending["revision"],
        "status": "active",
        "explanation": explanation,
        "items": items,
    }


def evidence_event_ids(pending: Dict[str, Any]) -> List[str]:
    ordered: List[str] = []
    for signal in pending["signals"]:
        for event_id in signal["event_ids"]:
            if event_id not in ordered:
                ordered.append(event_id)
    return ordered[:16]


def _event_header(event: Any) -> tuple[str, str, str, str, Optional[str]]:
    if not isinstance(event, dict):
        raise ValueError("provenance event must be an object")
    event_id = event.get("id")
    kind = event.get("kind")
    timestamp = event.get("ts")
    agent = event.get("agent")
    run_id = event.get("run")
    if not all(isinstance(value, str) and value for value in (event_id, kind, timestamp, agent)):
        raise ValueError("provenance event header is invalid")
    if run_id is not None and not isinstance(run_id, str):
        raise ValueError("provenance event run identity is invalid")
    _parse_timestamp(timestamp)
    return event_id, kind, timestamp, agent, run_id


def _is_own_effect(kind: str, payload: Dict[str, Any]) -> bool:
    if kind in ("extension.artifact", "plan.update", "context.slot.updated"):
        return payload.get("extension_id") == EXTENSION_ID
    return (
        kind == "error"
        and payload.get("source") == "extension"
        and payload.get("extension_id") == EXTENSION_ID
    )


def _reconcile_own_effect(
    state: Dict[str, Any], kind: str, event_id: str, payload: Dict[str, Any]
) -> None:
    pending = state["pending_checkpoint"]
    if pending is not None:
        metadata = payload.get("metadata")
        if (
            kind == "extension.artifact"
            and isinstance(metadata, dict)
            and metadata.get("kind") == "run-health-checkpoint"
            and metadata.get("signature") == pending["signature"]
        ):
            pending["artifact_event_id"] = event_id
        elif kind == "plan.update" and _plan_payload_matches(
            payload, plan_presentation(pending)
        ):
            pending["plan_published"] = True
        elif kind == "context.slot.updated" and payload.get("slot") == CONTEXT_SLOT:
            content = payload.get("content")
            if content == render_context_slot(pending):
                pending["context_published"] = True
            elif content == "":
                pending["context_published"] = False

    active = state["active_checkpoint"]
    if (
        active is not None
        and kind == "context.slot.updated"
        and payload.get("slot") == CONTEXT_SLOT
    ):
        content = payload.get("content")
        if content == render_context_slot(active):
            active["context_published"] = True
        elif content == "":
            active["context_published"] = False

    retirement = state["pending_retirement"]
    if retirement is None:
        return
    if (
        kind == "context.slot.updated"
        and payload.get("slot") == CONTEXT_SLOT
        and payload.get("content") == ""
    ):
        retirement["context_cleared"] = True
    elif (
        kind == "plan.update"
        and retirement["context_cleared"]
        and _plan_payload_matches(payload, retirement_plan_presentation(retirement))
    ):
        retirement["plan_completed"] = True


def _plan_payload_matches(
    payload: Dict[str, Any], presentation: Dict[str, Any]
) -> bool:
    return all(payload.get(key) == value for key, value in presentation.items())


def _fold_user_message(
    state: Dict[str, Any], event_id: str, run_id: Optional[str]
) -> None:
    if run_id is None:
        return
    run = state["open_runs"].get(run_id)
    if run is None:
        return
    run["messages"] += 1
    if run["messages"] > 1:
        run["last_input_sequence"] = state["observed_sequence"]
        _append_evidence(run["event_ids"], event_id)


def _fold_tool_call(
    state: Dict[str, Any], payload: Dict[str, Any], sequence: int
) -> None:
    call_id = payload.get("id")
    name = payload.get("name")
    if not isinstance(call_id, str) or not isinstance(name, str):
        return
    call_key = _fingerprint("tool-call-id", call_id)
    validation, requires_exit_code = _validation_classification(
        name,
        payload.get("input"),
    )
    state["call_classes"][call_key] = {
        "family": _operation_fingerprint(name, payload.get("input"), call_key),
        "validation": validation,
        "validation_requires_exit_code": requires_exit_code,
        "last_seen_sequence": sequence,
    }
    _bound_mapping(state["call_classes"], MAX_TRACKED_CALLS)


def _fold_tool_result(
    state: Dict[str, Any],
    event_id: str,
    payload: Dict[str, Any],
    sequence: int,
) -> None:
    call_id = payload.get("id")
    call_key = _fingerprint("tool-call-id", call_id) if isinstance(call_id, str) else None
    call_class = state["call_classes"].pop(call_key, None) if call_key is not None else None
    paired = isinstance(call_class, dict)
    family = call_class["family"] if paired else _fingerprint(
        "unpaired-tool-result",
        call_key or event_id,
    )
    exit_code = payload.get("exit_code")
    nonzero_exit = (
        not isinstance(exit_code, bool)
        and isinstance(exit_code, int)
        and exit_code != 0
    )
    neutral = payload.get("cancelled") is True or payload.get("recovery_closure") is True
    if neutral:
        return
    failed = payload.get("ok") is not True or nonzero_exit
    if failed:
        variant = f"exit:{exit_code}" if nonzero_exit else "failed"
        _record_failure(
            state,
            _fingerprint("tool-failure", family, variant),
            family,
            event_id,
            sequence,
        )
    elif paired:
        _clear_failure_family(state, family)
    completed_validation = paired and call_class["validation"]
    if completed_validation and call_class["validation_requires_exit_code"]:
        completed_validation = (
            not isinstance(exit_code, bool) and isinstance(exit_code, int)
        )
    if completed_validation:
        state["edit_tracks"] = {}


def _fold_check_result(
    state: Dict[str, Any],
    event_id: str,
    payload: Dict[str, Any],
    sequence: int,
) -> None:
    if payload.get("cancelled") is True or payload.get("recovery_closure") is True:
        return
    name = payload.get("name")
    family = _fingerprint("check-family", name if isinstance(name, str) else "unknown")
    exit_code = payload.get("exit_code")
    failed = payload.get("ok") is not True or (
        not isinstance(exit_code, bool)
        and isinstance(exit_code, int)
        and exit_code != 0
    )
    if failed:
        _record_failure(
            state,
            _fingerprint("check-failure", family, str(exit_code)),
            family,
            event_id,
            sequence,
        )
    else:
        _clear_failure_family(state, family)
    state["edit_tracks"] = {}


def _fold_file_change(
    state: Dict[str, Any],
    event_id: str,
    payload: Dict[str, Any],
    sequence: int,
) -> None:
    path = payload.get("path")
    if not isinstance(path, str) or not path:
        return
    key = _fingerprint("file", path)
    track = state["edit_tracks"].setdefault(
        key,
        {
            "count": 0,
            "event_ids": [],
            "alerted": False,
            "first_seen_sequence": sequence,
            "last_seen_sequence": sequence,
        },
    )
    track["count"] += 1
    track["last_seen_sequence"] = sequence
    _append_evidence(track["event_ids"], event_id)
    _bound_mapping(state["edit_tracks"], MAX_TRACKED_FILES)


def _fold_error(
    state: Dict[str, Any],
    event_id: str,
    agent: str,
    payload: Dict[str, Any],
    sequence: int,
) -> None:
    source = payload.get("source")
    purpose = payload.get("purpose")
    if purpose is None and source in ("provider", "session"):
        state["open_root_calls"].pop(agent, None)
    if payload.get("cancelled") is True or payload.get("recovery_closure") is True:
        return
    if source not in ("provider", "guardian", "session"):
        return
    category = payload.get("category")
    if not isinstance(category, str) or not category:
        return
    timeout_stage = payload.get("timeout_stage")
    safe_timeout = (
        timeout_stage
        if isinstance(timeout_stage, str) and timeout_stage
        else "none"
    )
    family = _fingerprint("error-family", str(source), category, safe_timeout)
    _record_failure(state, family, family, event_id, sequence)


def _fold_usage_sample(
    state: Dict[str, Any],
    event_id: str,
    payload: Dict[str, Any],
    thresholds: Dict[str, int],
    sequence: int,
) -> None:
    target = _model_target_fingerprint(payload.get("provider"), payload.get("model"))
    if target is None:
        _switch_usage_target(state, None, None)
        return
    if target != state["usage_target_fingerprint"]:
        state["usage_samples"] = []
        state["context_alerted"] = False
        state["usage_target_fingerprint"] = target
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return
    input_tokens = usage.get("input_tokens")
    cached_tokens = usage.get("cached_tokens")
    if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
        return
    if cached_tokens is not None and (
        isinstance(cached_tokens, bool)
        or not isinstance(cached_tokens, int)
        or not 0 <= cached_tokens <= input_tokens
    ):
        cached_tokens = None
    state["usage_samples"].append(
        {
            "event_id": event_id,
            "input_tokens": input_tokens,
            "cached_tokens": cached_tokens,
            "sequence": sequence,
        }
    )
    window = thresholds["context_window"]
    state["usage_samples"] = state["usage_samples"][-window:]


def _no_progress_candidate(
    state: Dict[str, Any],
    thresholds: Dict[str, int],
    *,
    include_alerted: bool = False,
) -> Optional[Dict[str, Any]]:
    progress_id = state["last_progress_event_id"]
    if (
        progress_id is None
        or state["waiting_permission"]
        or state["open_root_calls"]
        or (
            not include_alerted
            and state["no_progress_alerted_for"] == progress_id
        )
    ):
        return None
    progress_ts = state["last_progress_ts"]
    latest_ts = state["latest_observed_ts"]
    latest_id = state["latest_observed_event_id"]
    if progress_ts is None or latest_ts is None or latest_id is None:
        return None
    elapsed = int((_parse_timestamp(latest_ts) - _parse_timestamp(progress_ts)).total_seconds())
    if elapsed < thresholds["no_progress_seconds"]:
        return None
    return {
        "kind": "no_meaningful_progress",
        "count": elapsed,
        "event_ids": [progress_id, latest_id] if progress_id != latest_id else [progress_id],
        "_entity": _fingerprint("progress", progress_id),
        "_sequence": state["last_progress_sequence"],
    }


def _context_churn_candidate(
    state: Dict[str, Any], thresholds: Dict[str, int]
) -> Optional[Dict[str, Any]]:
    samples = state["usage_samples"]
    evidence = _context_churn_evidence(samples, thresholds)
    if evidence is None:
        complete = len(samples) >= thresholds["context_window"] and all(
            sample["cached_tokens"] is not None for sample in samples
        )
        if complete:
            state["context_alerted"] = False
        return None
    if state["context_alerted"]:
        return None
    absolute_growth, maximum_reuse = evidence
    return {
        "kind": "context_cache_churn",
        "count": len(samples),
        "input_growth_tokens": absolute_growth,
        "maximum_observed_cache_reuse_percent": maximum_reuse,
        "event_ids": [sample["event_id"] for sample in samples],
        "_entity": _fingerprint("context", samples[0]["event_id"]),
        "_sequence": samples[0]["sequence"],
    }


def _context_churn_evidence(
    samples: List[Dict[str, Any]], thresholds: Dict[str, int]
) -> Optional[tuple[int, int]]:
    if len(samples) < thresholds["context_window"]:
        return None
    inputs = [sample["input_tokens"] for sample in samples]
    cached = [sample["cached_tokens"] for sample in samples]
    if not all(value is not None for value in cached):
        return None
    growing = all(right > left for left, right in zip(inputs, inputs[1:]))
    absolute_growth = inputs[-1] - inputs[0]
    relative_growth = absolute_growth * 100 >= max(1, inputs[0]) * thresholds[
        "context_growth_percent"
    ]
    cache_poor = all(
        value * 100 <= max(1, total) * thresholds["maximum_cache_reuse_percent"]
        for total, value in zip(inputs, cached)
    )
    if not (
        growing
        and inputs[-1] >= thresholds["minimum_context_tokens"]
        and absolute_growth >= thresholds["context_growth_min_tokens"]
        and relative_growth
        and cache_poor
    ):
        return None
    maximum_reuse = max(
        (value * 100) // max(1, total) for total, value in zip(inputs, cached)
    )
    return absolute_growth, maximum_reuse


def _mark_signal_alerted(state: Dict[str, Any], signal: Dict[str, Any]) -> None:
    kind = signal["kind"]
    entity = signal["_entity"]
    if kind == "repeated_failure":
        state["failure_tracks"][entity]["alerted"] = True
    elif kind == "edit_thrashing":
        state["edit_tracks"][entity]["alerted"] = True
    elif kind == "no_meaningful_progress":
        state["no_progress_alerted_for"] = signal["event_ids"][0]
    elif kind == "context_cache_churn":
        state["context_alerted"] = True
    elif kind == "active_run_input":
        for run_id, run in state["open_runs"].items():
            if _fingerprint("run", run_id) == entity:
                run["alerted_count"] = signal["count"]
                return
        raise ValueError("run-health active input epoch disappeared")
    else:
        raise ValueError("unknown run-health signal")


def _signal_is_unresolved(
    state: Dict[str, Any], signal: Dict[str, Any], thresholds: Dict[str, int]
) -> bool:
    kind = signal["kind"]
    entity = signal["_entity"]
    if kind == "repeated_failure":
        track = state["failure_tracks"].get(entity)
        return isinstance(track, dict) and track["count"] >= signal["count"]
    if kind == "edit_thrashing":
        track = state["edit_tracks"].get(entity)
        return isinstance(track, dict) and track["count"] >= signal["count"]
    if kind == "no_meaningful_progress":
        candidate = _no_progress_candidate(state, thresholds, include_alerted=True)
        return candidate is not None and candidate["_entity"] == entity
    if kind == "context_cache_churn":
        return _context_churn_evidence(state["usage_samples"], thresholds) is not None
    if kind == "active_run_input":
        progress_sequence = state["last_progress_sequence"] or 0
        return progress_sequence <= signal["_sequence"] and any(
            _fingerprint("run", run_id) == entity for run_id in state["open_runs"]
        )
    raise ValueError("unknown run-health signal")


def _record_failure(
    state: Dict[str, Any],
    key: str,
    family: str,
    event_id: str,
    sequence: int,
) -> None:
    track = state["failure_tracks"].setdefault(
        key,
        {
            "family": family,
            "count": 0,
            "event_ids": [],
            "alerted": False,
            "first_seen_sequence": sequence,
            "last_seen_sequence": sequence,
        },
    )
    track["count"] += 1
    track["last_seen_sequence"] = sequence
    _append_evidence(track["event_ids"], event_id)
    _bound_mapping(state["failure_tracks"], MAX_TRACKED_FAILURES)


def _clear_failure_family(state: Dict[str, Any], family: str) -> None:
    state["failure_tracks"] = {
        key: track
        for key, track in state["failure_tracks"].items()
        if track["family"] != family
    }


def _reset_run_local_tracks(state: Dict[str, Any]) -> None:
    state["failure_tracks"] = {}
    state["edit_tracks"] = {}
    state["call_classes"] = {}
    state["usage_samples"] = []
    state["usage_target_fingerprint"] = None
    state["context_alerted"] = False
    state["waiting_permission"] = False


def _establish_root_owner(state: Dict[str, Any], agent: str) -> None:
    """Start observation only once durable root authority is identifiable."""

    state["observed_sequence"] = 0
    state["failure_tracks"] = {}
    state["edit_tracks"] = {}
    state["call_classes"] = {}
    state["open_runs"] = {}
    state["open_root_calls"] = {}
    state["waiting_permission"] = False
    state["last_progress_event_id"] = None
    state["last_progress_ts"] = None
    state["last_progress_sequence"] = None
    state["latest_observed_event_id"] = None
    state["latest_observed_ts"] = None
    state["no_progress_alerted_for"] = None
    state["usage_samples"] = []
    state["usage_target_fingerprint"] = None
    state["context_alerted"] = False
    state["root_agent"] = agent


def _switch_usage_target(
    state: Dict[str, Any], provider: Any, model: Any
) -> None:
    state["usage_samples"] = []
    state["context_alerted"] = False
    state["usage_target_fingerprint"] = _model_target_fingerprint(provider, model)


def _model_target_fingerprint(provider: Any, model: Any) -> Optional[str]:
    if not all(isinstance(value, str) and value for value in (provider, model)):
        return None
    return _fingerprint("model-target", provider, model)


def _validation_classification(name: str, input_value: Any) -> tuple[bool, bool]:
    if name in ("check", "run_check", "cargo_check", "cargo_test"):
        return True, False
    if name != "run_shell" or not isinstance(input_value, dict):
        return False, False
    command = input_value.get("command")
    validation = isinstance(command, str) and _is_validation_command(command)
    return validation, validation


def _is_validation_command(command: str) -> bool:
    """Recognize one anchored validation invocation, never incidental words."""

    tokens = _parse_simple_shell_tokens(command)
    if not tokens:
        return False

    executable = tokens[0].rsplit("/", 1)[-1].lower()
    arguments = [token.lower() for token in tokens[1:]]
    if executable == "cargo":
        while arguments and arguments[0].startswith("+"):
            arguments.pop(0)
        if not arguments:
            return False
        if arguments[0] == "nextest":
            return len(arguments) >= 2 and arguments[1] == "run"
        if arguments[0] == "fmt":
            return "--check" in arguments[1:]
        return arguments[0] in {"test", "check", "clippy"}
    if executable == "nextest":
        return bool(arguments) and arguments[0] == "run"
    if executable in {"pytest", "jest", "vitest", "ctest"}:
        return True
    if _PYTHON_EXECUTABLE.fullmatch(executable):
        return len(arguments) >= 2 and arguments[:2] in (
            ["-m", "pytest"],
            ["-m", "unittest"],
        )
    if executable == "go":
        return bool(arguments) and arguments[0] == "test"
    if executable == "swift":
        return bool(arguments) and arguments[0] == "test"
    if executable in {"make", "just"}:
        return bool(arguments) and arguments[0] in {"test", "check", "lint"}
    if executable == "npx":
        return bool(arguments) and arguments[0] in {"jest", "vitest"}
    if executable in {"npm", "pnpm", "yarn", "bun"}:
        return _package_validation(arguments)
    return False


def _strip_environment_prefix(tokens: List[str]) -> List[str]:
    index = 0
    while index < len(tokens) and _ASSIGNMENT.fullmatch(tokens[index]):
        index += 1
    if index >= len(tokens) or tokens[index] != "env":
        return tokens[index:]

    index += 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return tokens[index + 1 :]
        if token in ("-i", "--ignore-environment"):
            index += 1
            continue
        if token in ("-u", "--unset"):
            if index + 1 >= len(tokens):
                return []
            index += 2
            continue
        if token.startswith("--unset=") or _ASSIGNMENT.fullmatch(token):
            index += 1
            continue
        if token.startswith("-"):
            return []
        break
    return tokens[index:]


def _package_validation(arguments: List[str]) -> bool:
    if not arguments:
        return False
    if arguments[0] in {"test", "lint", "jest", "vitest"}:
        return True
    return len(arguments) >= 2 and arguments[0] == "run" and arguments[1] in {
        "test",
        "lint",
    }


def _operation_fingerprint(name: str, input_value: Any, call_key: str) -> str:
    if name != "run_shell":
        return _fingerprint("tool-operation", name)
    if not isinstance(input_value, dict):
        return _fingerprint("shell-call", call_key)
    command = input_value.get("command")
    operation = _shell_operation_class(command) if isinstance(command, str) else None
    if operation is None:
        return _fingerprint("shell-call", call_key)
    return _fingerprint("shell-operation", *operation)


def _shell_operation_class(command: str) -> Optional[tuple[str, ...]]:
    tokens = _parse_simple_shell_tokens(command)
    if not tokens:
        return None
    executable = tokens[0].rsplit("/", 1)[-1].lower()
    arguments = [token.lower() for token in tokens[1:]]
    if executable == "cargo":
        while arguments and arguments[0].startswith("+"):
            arguments.pop(0)
        subcommand = _operation_subcommand(arguments)
        if subcommand == "nextest":
            nested = _operation_subcommand(arguments[1:])
            return (executable, subcommand, nested) if nested is not None else None
        return (executable, subcommand) if subcommand is not None else None
    if executable in {"git", "go", "swift", "make", "just", "npx"}:
        subcommand = _operation_subcommand(arguments)
        return (executable, subcommand) if subcommand is not None else None
    if executable in {"npm", "pnpm", "yarn", "bun"}:
        subcommand = _operation_subcommand(arguments)
        if subcommand is None:
            return None
        if subcommand == "run":
            script = _operation_subcommand(arguments[1:])
            return (executable, "run", script) if script is not None else None
        return (executable, subcommand)
    if _PYTHON_EXECUTABLE.fullmatch(executable):
        if (
            len(arguments) >= 2
            and arguments[0] == "-m"
            and _OPERATION_TOKEN.fullmatch(arguments[1])
        ):
            return ("python", "-m", arguments[1])
        return None
    if executable in {"pytest", "nextest", "ctest", "jest", "vitest"}:
        return (executable,)
    return None


def _operation_subcommand(arguments: List[str]) -> Optional[str]:
    if not arguments or not _OPERATION_TOKEN.fullmatch(arguments[0]):
        return None
    return arguments[0]


def _parse_simple_shell_tokens(command: str) -> List[str]:
    if any(marker in command for marker in ("\n", "\r", ";", "|", "&", "<", ">", "`", "$(")):
        return []
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return []
    return _strip_environment_prefix(tokens)


def _append_evidence(event_ids: List[str], event_id: str) -> None:
    if not event_ids or event_ids[-1] != event_id:
        event_ids.append(event_id)
    del event_ids[:-MAX_EVIDENCE_IDS]


def _bound_mapping(mapping: Dict[str, Any], maximum: int) -> None:
    while len(mapping) > maximum:
        oldest = min(
            mapping,
            key=lambda key: (mapping[key]["last_seen_sequence"], key),
        )
        mapping.pop(oldest)


def _fingerprint(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("provenance event timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise ValueError("provenance event timestamp must include an offset")
    return parsed


def _public_signal(signal: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in signal.items() if not key.startswith("_")}


def _signal_summary(signal: Dict[str, Any]) -> str:
    kind = signal["kind"]
    count = signal["count"]
    if kind == "repeated_failure":
        return f"the same content-free failure class recurred {count} times"
    if kind == "edit_thrashing":
        return f"one file identity changed {count} times without fresh validation"
    if kind == "no_meaningful_progress":
        return f"no meaningful-progress event for {count} seconds"
    if kind == "context_cache_churn":
        return (
            f"context grew by {signal['input_growth_tokens']} tokens across {count} samples "
            f"with at most {signal['maximum_observed_cache_reuse_percent']}% cache reuse"
        )
    if kind == "active_run_input":
        return f"{count} steering input(s) were accepted during the active run"
    raise ValueError("unknown run-health signal")


def _recovery_options(signals: List[Dict[str, Any]]) -> List[str]:
    options: List[str] = []
    kinds = {signal["kind"] for signal in signals}
    if "repeated_failure" in kinds:
        options.append("Inspect the failure boundary and change tool or strategy before retrying.")
    if "edit_thrashing" in kinds:
        options.append("Stop editing the same area and obtain a focused validation result.")
    if "no_meaningful_progress" in kinds:
        options.append(
            "Re-check the objective and choose one bounded next operation or ask for input."
        )
    if "context_cache_churn" in kinds:
        options.append(
            "Narrow the working set and reuse stable context instead of rediscovering it."
        )
    if "active_run_input" in kinds:
        options.append(
            "Reassess whether the accepted input changes scope or belongs in a follow-up run."
        )
    return options[:5]
