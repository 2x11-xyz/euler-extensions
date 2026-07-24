"""Convert a ``causal-dag.walk-annotations.v2`` export into a v5 artifact.

The walk was annotated in the *old* vocabulary (root/attempt/claim/checkpoint/
synthesis, the eight flat statuses). This converter maps it mechanically to v5.
It never re-interprets the human's grading — ``dead_end`` stays ``dead_end``; a
walk that graded a refuted claim as ``dead_end`` (the Q2 miscoding) is carried
across faithfully, not silently promoted to ``refuted``.

Kind mapping (old -> v5):

    root        -> question        rootness is topology (§2.1); the human's
                                   goal node becomes a question.
    attempt     -> investigation   cross-domain rename (Q3).
    claim       -> claim           unchanged.
    checkpoint  -> synthesis       kind dropped (Q3); metadata.consolidation=True.
    synthesis   -> synthesis       metadata.consolidation=False.

Status mapping (old -> v5), per target kind (§2.2 per-kind axes). Cells the gold
walk never exercises are best-effort and marked lossy:

    question:   open->open  blocked->blocked  success/verified->answered
                dead_end/abandoned->abandoned  superseded->superseded
                inconclusive->open (lossy)
    investigation: open->open  blocked->blocked  dead_end->dead_end
                success->succeeded  verified->verified  superseded->superseded
                abandoned->abandoned  inconclusive->dead_end (lossy: no
                investigation verdict for "unsettled")
    claim:      open->open  success->supported  verified->supported (lossy:
                never *strengthen* — proven is reserved for deductive evidence
                the old grading cannot attest)  dead_end->abandoned (lossy: the
                Q2 miscoding is carried, not promoted to refuted; regrade in a
                new walk to claim refuted)  inconclusive->inconclusive
                superseded->superseded  abandoned->abandoned  blocked->open (lossy)
    synthesis:  open->open  success->stated  verified->verified
                superseded->superseded  dead_end/abandoned->abandoned
                inconclusive/blocked->open (lossy)

Rootness / the deliberately-unplaced node: rootness is pure topology (§2.1) — a
root is any node with no backbone parent. The converter changes no kind or status
on that basis. The gold walk's ``apply_patch`` node (kind synthesis) was left
without a backbone parent on purpose; it therefore surfaces as a second root with
its kind and status unchanged, exactly as SCHEMA-v5's rootness-is-topology
demands. Only the human's explicit ``root``-kind node is renamed (to ``question``).

Provenance: a node owns whole turns (R1); its ``turns[]`` come from ``node_steps``
joined to the session's ``steps[].event_ids``. Each turn yields one ``event``
source_ref anchored on the turn's first event, ``payload_pointer`` null. Every
node and edge carries basis kind ``operator`` — a human asserted the graph. Each
edge anchors on its ``from`` node's evidence, which is what makes repair/pivot/
refutation edges share evidence with the failure they spring from (§4.2, §4.6).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .invariants import recompute_diagnostics
from .schema import (
    Artifact, Basis, Construction, Edge, EventRange, Node, Projection,
    Session, SourceRef, Turn, Warning,
)

KIND_MAP = {
    "root": "question",
    "attempt": "investigation",
    "claim": "claim",
    "checkpoint": "synthesis",
    "synthesis": "synthesis",
}

STATUS_MAP: Dict[str, Dict[str, str]] = {
    "question": {"open": "open", "blocked": "blocked", "success": "answered",
                 "verified": "answered", "dead_end": "abandoned",
                 "abandoned": "abandoned", "superseded": "superseded",
                 "inconclusive": "open"},
    "investigation": {"open": "open", "blocked": "blocked", "dead_end": "dead_end",
                      "success": "succeeded", "verified": "verified",
                      "superseded": "superseded", "abandoned": "abandoned",
                      "inconclusive": "dead_end"},
    "claim": {"open": "open", "success": "supported", "verified": "supported",
              "dead_end": "abandoned", "inconclusive": "inconclusive",
              "superseded": "superseded", "abandoned": "abandoned", "blocked": "open"},
    "synthesis": {"open": "open", "success": "stated", "verified": "verified",
                  "superseded": "superseded", "dead_end": "abandoned",
                  "abandoned": "abandoned", "inconclusive": "open", "blocked": "open"},
}

# Status cells the gold walk never exercises map with information loss; the
# artifact must say so (honest degradation) — each use emits a warning.
LOSSY_CELLS = frozenset({
    ("question", "inconclusive"), ("investigation", "inconclusive"),
    ("claim", "blocked"), ("claim", "verified"), ("claim", "dead_end"),
    ("synthesis", "inconclusive"), ("synthesis", "blocked"),
    ("synthesis", "dead_end"), ("question", "dead_end"),
})

# Empty event stream sentinel, inherited from v3.
EPOCH = "1970-01-01T00:00:00Z"


def import_walk(export: Dict[str, Any], steps: List[Dict[str, Any]],
                events: Dict[str, Dict[str, str]]) -> Artifact:
    """Project a walk-annotations.v2 export + its session steps into a v5 artifact.

    ``events`` is an *ordered* mapping of event id -> ``{"kind": ..., "ts": ...}``
    whose iteration order is the provenance stream order (build it by reading the
    session log top to bottom). Stream order is authoritative: euler event ids
    are non-monotonic ULIDs, so ranges and anchors are derived from position,
    never from id sorting. Citations must be honest: every event a turn owns
    must be present in ``events`` — an unknown event is an error, not a default.
    ``generated_at`` equals the range-end event's timestamp (inherited v3 rule),
    not the export's wall-clock time.
    """
    order = {eid: i for i, eid in enumerate(events)}
    step_events = {s["step_id"]: list(s.get("event_ids", [])) for s in steps}
    for step_id, evs in step_events.items():
        for ev in evs:
            if ev not in order:
                raise ValueError(f"no event metadata known for event {ev} (step {step_id})")

    owned_steps: Dict[str, List[int]] = {}
    for entry in export["node_steps"]:
        owned_steps.setdefault(entry["node_id"], []).append(entry["step_id"])

    old_kind = {n["node_id"]: n["kind"] for n in export["nodes"]}
    backbone_parent = {
        e["to_node"]: e["from_node"] for e in export["edges"] if e.get("backbone")
    }

    def climb_root(nid: str) -> str:
        seen = set()
        while nid in backbone_parent and nid not in seen:
            seen.add(nid)
            nid = backbone_parent[nid]
        return nid

    # node id -> its evidence anchor (first event of its first owned turn).
    anchor: Dict[str, SourceRef] = {}
    nodes: List[Node] = []
    lossy: Dict[tuple, List[str]] = {}
    for raw in export["nodes"]:
        nid = raw["node_id"]
        kind = KIND_MAP[raw["kind"]]
        status = STATUS_MAP[kind][raw["status"]]
        if (kind, raw["status"]) in LOSSY_CELLS:
            lossy.setdefault((kind, raw["status"], status), []).append(nid)
        turns, refs = _turns_and_refs(nid, owned_steps.get(nid, []), step_events,
                                      events, order)
        metadata: Dict[str, Any] = {}
        if kind == "synthesis":
            metadata["consolidation"] = raw["kind"] == "checkpoint"
        nodes.append(Node(
            id=nid,
            root_id=climb_root(nid),
            kind=kind,
            status=status,
            title=raw.get("title", "").strip(),
            summary=raw.get("note", ""),
            turns=turns,
            source_refs=refs,
            basis=Basis("operator", _node_basis_summary(raw), sorted(r.id for r in refs)),
            metadata=metadata,
        ))
        if refs:
            anchor[nid] = min(refs, key=lambda r: order[r.event_id])

    nodes.sort(key=lambda n: n.id)
    edges = sorted((_edge(raw, anchor) for raw in export["edges"]), key=lambda e: e.id)

    cited = {ev for evs in step_events.values() for ev in evs}
    start = min(cited, key=order.__getitem__) if cited else None
    end = max(cited, key=order.__getitem__) if cited else None

    artifact = Artifact(
        generated_at=events[end]["ts"] if end is not None else EPOCH,
        session=Session(export["session_id"], EventRange(start, end, complete=True)),
        projection=Projection("causal-dag", end, "bounded_provenance_query", degraded=False),
        construction=Construction("snapshot", "manual", "command"),
        nodes=nodes,
        edges=edges,
        active_root=_active_root(nodes, old_kind),
    )
    artifact.diagnostics = recompute_diagnostics(artifact)
    warnings = [
        Warning(
            code="lossy_status_mapping",
            severity="warning",
            message=f"old status {old!r} mapped lossily to {new!r} for kind {kind!r}",
            node_ids=sorted(ids),
        )
        for (kind, old, new), ids in sorted(lossy.items())
    ]
    if not nodes:
        warnings.append(Warning("empty_forest", "info",
                                "the walk export contains no nodes"))
    artifact.diagnostics.warnings = warnings
    return artifact


def _turns_and_refs(node_id, step_ids, step_events, events, order):
    turns: List[Turn] = []
    refs: List[SourceRef] = []
    for step_id in sorted(step_ids):
        event_ids = step_events.get(step_id, [])
        if len(set(event_ids)) != len(event_ids):
            raise ValueError(f"turn {step_id} lists duplicate events")
        # v5 turns are ordered event-id spans: normalize to stream order.
        event_ids = sorted(event_ids, key=order.__getitem__)
        turns.append(Turn(step_id, event_ids))
        if event_ids:
            first = event_ids[0]
            refs.append(SourceRef(
                id=f"{node_id}-t{step_id}",
                kind="event",
                event_id=first,
                event_kind=events[first]["kind"],
                payload_pointer=None,
            ))
    refs.sort(key=lambda r: r.id)
    return turns, refs


def _edge(raw: Dict[str, Any], anchor: Dict[str, SourceRef]) -> Edge:
    eid = raw["edge_id"]
    parent_ref = anchor.get(raw["from_node"])
    source_refs: List[SourceRef] = []
    if parent_ref is not None:
        source_refs.append(SourceRef(
            id=f"{eid}-s0",
            kind="event",
            event_id=parent_ref.event_id,
            event_kind=parent_ref.event_kind,
            payload_pointer=None,
        ))
    return Edge(
        id=eid,
        from_node=raw["from_node"],
        to_node=raw["to_node"],
        edge_class=raw["class"],
        kind=raw["kind"],
        canonical_backbone=bool(raw.get("backbone")),
        source_refs=source_refs,
        basis=Basis("operator", raw.get("note", "") or f"operator-drawn {raw['kind']} edge",
                    [r.id for r in source_refs]),
        metadata={},
    )


def _node_basis_summary(raw: Dict[str, Any]) -> str:
    note = raw.get("note", "")
    return note if note else f"operator-classified {raw['kind']}"


def _active_root(nodes: List[Node], old_kind: Dict[str, str]) -> Optional[str]:
    roots = sorted(n.id for n in nodes if n.root_id == n.id)
    for rid in roots:
        if old_kind.get(rid) == "root":
            return rid
    return roots[0] if roots else None
