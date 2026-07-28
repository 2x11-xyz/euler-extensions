"""The deterministic degraded projector: a bounded provenance page -> a v5 artifact.

This is honest scaffolding, not analysis. Without a live model reading the
session (the observer lane, milestone 4), the projector cannot judge which turns
are epistemic acts and which are clerical — ruling R13's distinction is exactly
what a degraded pass cannot make. So it does the one thing it *can* do
truthfully: segment the page into turns (R1 — a turn is the atomic unit, the
span from one ``model.call`` to the next; a ``user.message`` opens its own turn),
lay them on a single chronology spine, and mark the whole result
``projection.degraded``. The ``degraded_chronology`` warning over every sequence
edge is the disclaimer: these edges assert ordering, never causality. A later
observer tick replaces this spine with reasoned structure; until then the marker
is the honest limit of what page order alone can claim.

Everything here is pure: no host, no I/O. ``project_tick`` takes the prior
artifact (or ``None``) and the new page and returns the next immutable revision,
or ``None`` when the page carried no ownable source events.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .invariants import recompute_diagnostics
from .schema import (
    Artifact, Basis, Construction, Edge, EventRange, Node, Projection,
    Session, SourceRef, Turn, Warning,
)

# The declarative source filter (R1/R13). INCLUDE_KINDS are the event kinds a
# node may *own* — the epistemic surface of a turn. Everything else is dropped:
# ``model.call`` is a turn *boundary* but never owned (bookkeeping); permission,
# canvas, and our own extension.artifact / context.slot.updated self-events
# (the feedback loop) are excluded outright. Keeping this a frozenset + two
# predicates makes the policy one readable declaration, not scattered ``if``s.
INCLUDE_KINDS = frozenset({
    "user.message", "assistant.message", "model.result",
    "tool.call", "tool.result", "patch.proposed", "file.diff", "check.result",
    "error",  # failures are epistemic (the walk extractor owned error steps).
})
_MODEL_CALL = "model.call"
_USER_MESSAGE = "user.message"
_BOUNDARY_KINDS = frozenset({_MODEL_CALL, _USER_MESSAGE})
# Self-events that must never re-enter the projection as source (the loop the
# include-list already forecloses; named here so the intent is explicit).
SELF_EVENT_KINDS = frozenset({"extension.artifact", "context.slot.updated"})

EXTENSION_ID = "causal-dag"


def is_self_error(event: Dict[str, Any]) -> bool:
    """True for host ``error`` events recording THIS extension's own failures.

    The one self-event channel a kind filter cannot exclude: ``error`` is
    shared with genuinely epistemic session failures, but the host also
    appends one whenever an extension command fails — owning those would let
    a failing tick pollute its own graph on every retry.
    """
    if event.get("kind") != "error":
        return False
    payload = event.get("payload") or {}
    return (payload.get("source") == "extension"
            and payload.get("extension_id") == EXTENSION_ID)

# The kinds worth pulling from the host: the ownable surface plus the model.call
# boundary marker. The host drops the rest, so a page carries no self-events.
QUERY_KINDS = sorted(INCLUDE_KINDS | {_MODEL_CALL})

_ARTIFACT_BASIS = "bounded_provenance_query"
_DEGRADED_MESSAGE = (
    "chronology fallback: sequence edges are page ordering, not causality"
)


def is_owned(kind: str) -> bool:
    """True when an event of ``kind`` may be owned by a turn-node."""
    return kind in INCLUDE_KINDS


def group_pairs(pairs: List[Tuple[str, str]]) -> List[List[Tuple[str, str]]]:
    """Segment an ``(event_id, event_kind)`` stream into turns deterministically.

    A turn is the ownable span from one ``model.call`` to the next; a
    ``user.message`` opens its own turn. Each returned turn is the ordered list
    of the pairs it owns — boundary and excluded events never appear. Empty
    spans (a boundary that owns nothing, e.g. a ``model.call`` followed only by
    permission events) are dropped: clerical turns stay unowned (R13).
    """
    turns: List[List[Tuple[str, str]]] = []
    current: List[Tuple[str, str]] = []

    def flush() -> None:
        if current:
            turns.append(list(current))
        current.clear()

    for event_id, kind in pairs:
        if kind in _BOUNDARY_KINDS:
            flush()
        if is_owned(kind):
            current.append((event_id, kind))
    flush()
    return turns


def group_turns(events: List[Dict[str, Any]]) -> List[List[Tuple[str, str]]]:
    """``group_pairs`` over raw event dicts (R1)."""
    return group_pairs([(e["id"], e.get("kind")) for e in events])


def split_closed(pending: List[Tuple[str, str]], events: List[Dict[str, Any]],
                 has_more: bool
                 ) -> Tuple[List[List[Tuple[str, str]]], List[Tuple[str, str]]]:
    """Pagination-invariant segmentation: carry the open turn across ticks.

    ``pending`` is the owned tail of the turn left open by the prior page.
    Prepending it to ``events`` reconstitutes turns split by a page boundary, so
    the node structure is identical for any page ``limit``. When the page is
    truncated (``has_more``), the turn opened by the *last* boundary in the
    combined stream is still growing — it is held back as the new pending and
    only the turns before it are returned as closed. A page with no boundary
    closes nothing (the whole combined stream stays open). When the page is the
    end of the stream, everything is closed and pending clears.

    ``events`` must contain only events the artifact has never seen — the
    caller resumes the query from the cursor its state is synced to, because a
    replayed boundary re-applied here would cut the still-open pending turn at
    a stale position.
    """
    combined: List[Tuple[str, str]] = list(pending)
    combined += [(e["id"], e.get("kind")) for e in events]
    if not has_more:
        return group_pairs(combined), []
    last_boundary = -1
    for i, (_eid, kind) in enumerate(combined):
        if kind in _BOUNDARY_KINDS:
            last_boundary = i
    if last_boundary < 0:
        return [], [p for p in combined if is_owned(p[1])]
    closed = combined[:last_boundary]
    open_tail = [p for p in combined[last_boundary:] if is_owned(p[1])]
    return group_pairs(closed), open_tail


def _node_id(index: int) -> str:
    # Zero-padded so lexicographic order (the canonical sort) equals turn order.
    return f"node-{index:06d}"


def _edge_id(index: int) -> str:
    return f"edge-{index:06d}"


def _build_node(index: int, turn: List[Tuple[str, str]], root_id: str) -> Node:
    node_id = _node_id(index)
    # The very first turn of a session that opens with the user is the question
    # that starts it; every other turn is an investigation (§2.1). A degraded
    # pass keeps it that simple — one question, the rest open investigations.
    opens_with_user = turn and turn[0][1] == _USER_MESSAGE
    kind = "question" if index == 0 and opens_with_user else "investigation"
    refs = [
        SourceRef(f"{node_id}-r{position:03d}", "event", event_id, event_kind)
        for position, (event_id, event_kind) in enumerate(turn)
    ]
    return Node(
        id=node_id,
        root_id=root_id,
        kind=kind,
        status="open",
        title=f"turn {index}",
        summary="",
        turns=[Turn(index, [event_id for event_id, _ in turn])],
        source_refs=refs,
        basis=Basis("chronology", "page order"),
        metadata={},
    )


def project_tick(prior: Optional[Artifact],
                 turns: List[List[Tuple[str, str]]], *,
                 session_id: str, watermark: str, generated_at: str,
                 predecessor_artifact_event_id: Optional[str] = None,
                 predecessor_watermark_event_id: Optional[str] = None
                 ) -> Optional[Artifact]:
    """Extend ``prior`` (or seed a fresh spine) with a batch of *closed* turns.

    ``turns`` is the pre-segmented, pagination-invariant batch (``split_closed``)
    — each turn an ordered list of ``(event_id, event_kind)`` pairs. Returns the
    next immutable revision, or ``None`` when the batch is empty (the caller
    advances its cursor without minting an empty artifact). Lineage: a fresh
    graph is a ``snapshot``; an extension is ``incremental`` and carries the
    predecessor artifact/watermark pair the caller threads in.
    """
    if not turns:
        return None

    prior_nodes = list(prior.nodes) if prior else []
    prior_edges = list(prior.edges) if prior else []
    start = len(prior_nodes)
    root_id = _node_id(0)

    new_nodes: List[Node] = []
    new_edges: List[Edge] = []
    for offset, turn in enumerate(turns):
        index = start + offset
        new_nodes.append(_build_node(index, turn, root_id))
        if index > 0:
            new_edges.append(Edge(
                id=_edge_id(index),
                from_node=_node_id(index - 1),
                to_node=_node_id(index),
                edge_class="chronology",
                kind="sequence",
                canonical_backbone=True,
                source_refs=[],
                basis=Basis("chronology", "page order"),
                metadata={},
            ))

    nodes = prior_nodes + new_nodes
    edges = prior_edges + new_edges

    range_start = nodes[0].turns[0].event_ids[0]
    range_end = nodes[-1].turns[0].event_ids[-1]

    if prior is None:
        construction = Construction("snapshot", "manual", "command")
    else:
        construction = Construction(
            "incremental", "manual", "command",
            predecessor_artifact_event_id=predecessor_artifact_event_id,
            predecessor_watermark_event_id=predecessor_watermark_event_id,
        )

    artifact = Artifact(
        generated_at=generated_at,
        session=Session(session_id, EventRange(range_start, range_end, complete=False)),
        projection=Projection("causal-dag", watermark, _ARTIFACT_BASIS, degraded=True),
        construction=construction,
        nodes=nodes,
        edges=edges,
        active_root=root_id,
    )
    artifact.diagnostics = recompute_diagnostics(artifact)
    sequence_ids = sorted(e.id for e in edges if e.kind == "sequence")
    if sequence_ids:
        artifact.diagnostics.warnings = [
            Warning("degraded_chronology", "warning", _DEGRADED_MESSAGE,
                    edge_ids=sequence_ids),
        ]
    return artifact
