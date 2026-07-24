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
    claim:      open->open  success->supported  verified->proven
                dead_end->refuted  inconclusive->inconclusive
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
    "claim": {"open": "open", "success": "supported", "verified": "proven",
              "dead_end": "refuted", "inconclusive": "inconclusive",
              "superseded": "superseded", "abandoned": "abandoned", "blocked": "open"},
    "synthesis": {"open": "open", "success": "stated", "verified": "verified",
                  "superseded": "superseded", "dead_end": "abandoned",
                  "abandoned": "abandoned", "inconclusive": "open", "blocked": "open"},
}

# Status cells the gold walk never exercises map with information loss; the
# artifact must say so (honest degradation) — each use emits a warning.
LOSSY_CELLS = frozenset({
    ("question", "inconclusive"), ("investigation", "inconclusive"),
    ("claim", "blocked"), ("synthesis", "inconclusive"), ("synthesis", "blocked"),
    ("synthesis", "dead_end"), ("question", "dead_end"),
})


def import_walk(export: Dict[str, Any], steps: List[Dict[str, Any]],
                event_kinds: Dict[str, str]) -> Artifact:
    """Project a walk-annotations.v2 export + its session steps into a v5 artifact.

    ``event_kinds`` maps event id -> the event's real kind from the session's
    provenance stream. Citations must be honest: a guessed kind is a wrong
    citation, so an event without a known kind is an error, not a default.
    """
    step_events = {s["step_id"]: list(s.get("event_ids", [])) for s in steps}

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
        turns, refs = _turns_and_refs(nid, owned_steps.get(nid, []), step_events, event_kinds)
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
            anchor[nid] = min(refs, key=lambda r: r.event_id)

    nodes.sort(key=lambda n: n.id)
    edges = sorted((_edge(raw, anchor) for raw in export["edges"]), key=lambda e: e.id)

    all_events = sorted({ev for evs in step_events.values() for ev in evs})
    start = all_events[0] if all_events else None
    end = all_events[-1] if all_events else None

    artifact = Artifact(
        generated_at=export.get("exported_at", "1970-01-01T00:00:00Z"),
        session=Session(export["session_id"], EventRange(start, end, complete=True)),
        projection=Projection("causal-dag", end, "bounded_provenance_query", degraded=False),
        construction=Construction("snapshot", "manual", "command"),
        nodes=nodes,
        edges=edges,
        active_root=_active_root(nodes, old_kind),
    )
    artifact.diagnostics = recompute_diagnostics(artifact)
    artifact.diagnostics.warnings = [
        Warning(
            code="lossy_status_mapping",
            severity="warning",
            message=f"old status {old!r} mapped lossily to {new!r} for kind {kind!r}",
            node_ids=sorted(ids),
        )
        for (kind, old, new), ids in sorted(lossy.items())
    ]
    return artifact


def _turns_and_refs(node_id, step_ids, step_events, event_kinds):
    turns: List[Turn] = []
    refs: List[SourceRef] = []
    for step_id in sorted(step_ids):
        events = step_events.get(step_id, [])
        turns.append(Turn(step_id, events))
        if events:
            first = events[0]
            if first not in event_kinds:
                raise ValueError(f"no event kind known for cited event {first}")
            refs.append(SourceRef(
                id=f"{node_id}-t{step_id}",
                kind="event",
                event_id=first,
                event_kind=event_kinds[first],
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
