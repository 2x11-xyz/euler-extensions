"""The ``euler.causal_dag.v5`` artifact: dataclasses, vocabularies, canonical I/O.

SCHEMA-v5.md is the contract. The envelope (§5) is inherited wholesale from v3;
this module holds its Python shape plus the v5 node model (per-kind status axes,
first-class ``turns[]``) and edge vocabulary (§2-3).

Serialization is canonical and deterministic: closed key sets in a fixed order,
lists sorted by ``id``, ``source_ref_ids`` sorted and unique. ``dumps`` of a
value equals ``dumps`` of its ``loads`` round-trip, byte for byte.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SCHEMA = "euler.causal_dag.v5"
MEDIA_TYPE = "application/vnd.euler.causal-dag.v5+json"

# §2.1 kinds. Rootness is topology, not a kind — there is no `root` kind.
KINDS = ("question", "claim", "investigation", "synthesis")

# §2.2 per-kind status axes. The legal status set depends on the node kind.
STATUS_BY_KIND: Dict[str, tuple] = {
    "question": ("open", "blocked", "answered", "abandoned", "superseded"),
    "claim": ("open", "supported", "refuted", "proven", "inconclusive",
              "superseded", "abandoned"),
    "investigation": ("open", "blocked", "dead_end", "succeeded", "verified",
                      "superseded", "abandoned"),
    "synthesis": ("open", "stated", "verified", "superseded", "abandoned"),
}

# §2.2: terminal for the structural rule in §4.2.
TERMINAL_STATUSES = frozenset(
    {"dead_end", "refuted", "superseded", "abandoned", "blocked"}
)

# §3 edge vocabulary, unchanged from v3.
EDGE_KINDS_BY_CLASS: Dict[str, tuple] = {
    "structural": ("continuation", "refinement", "repair", "fork",
                   "decomposition", "integration", "verification"),
    "annotation": ("evidence", "refutation", "artifact_use", "pivot",
                   "related", "supersedes"),
    "chronology": ("sequence",),
}

# §3 genealogy lens (Q4): the ordered generative family.
GENEALOGY_KINDS = frozenset({"repair", "pivot", "refutation"})

BASIS_KINDS = ("direct", "cluster", "inferred", "chronology", "operator")

# §5 construction enums, inherited from v3.
CONSTRUCTION_OPERATIONS = ("snapshot", "incremental", "reframe", "final")
CONSTRUCTION_POLICIES = ("manual", "rolling_only", "rolling_and_final", "final_only")
CONSTRUCTION_TRIGGERS = ("command", "round_cadence", "explicit_reframe", "session_end")

# Metadata may not shadow structural fields (v3 rule).
METADATA_SHADOW_KEYS = frozenset(
    {"id", "root_id", "kind", "status", "source_refs", "basis",
     "class", "from", "to", "canonical_backbone"}
)


def _canon(value: Any) -> Any:
    """Recursively sort dict keys so semantically equal values serialize identically."""
    if isinstance(value, dict):
        return {k: _canon(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_canon(v) for v in value]
    return value


@dataclass
class SourceRef:
    """A provenance citation. One variant is populated by ``kind`` (§5, v3)."""

    id: str
    kind: str  # event | artifact | blob
    event_id: str
    event_kind: str
    payload_pointer: Optional[str] = None
    artifact: Optional[Dict[str, Any]] = None
    blob: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "event_id": self.event_id,
            "event_kind": self.event_kind,
            "payload_pointer": self.payload_pointer,
            "artifact": _canon(self.artifact),
            "blob": _canon(self.blob),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "SourceRef":
        return SourceRef(d["id"], d["kind"], d["event_id"], d["event_kind"],
                         d["payload_pointer"], d["artifact"], d["blob"])


@dataclass
class Basis:
    """Why the node/edge is asserted, and which of its source_refs ground it."""

    kind: str  # BASIS_KINDS
    summary: str
    source_ref_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "source_ref_ids": sorted(set(self.source_ref_ids)),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Basis":
        return Basis(d["kind"], d["summary"], list(d["source_ref_ids"]))


@dataclass
class Turn:
    """A whole turn the node owns (R1): its ordered event-id span (§5)."""

    step_id: int
    event_ids: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {"step_id": self.step_id, "event_ids": list(self.event_ids)}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Turn":
        return Turn(d["step_id"], list(d["event_ids"]))


@dataclass
class Node:
    id: str
    root_id: str  # derived (§5), retained for viewer compat
    kind: str
    status: str
    title: str
    summary: str
    turns: List[Turn] = field(default_factory=list)
    source_refs: List[SourceRef] = field(default_factory=list)
    basis: Optional[Basis] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def source_event_ids(self) -> frozenset:
        return frozenset(ref.event_id for ref in self.source_refs)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "root_id": self.root_id,
            "kind": self.kind,
            "status": self.status,
            "title": self.title,
            "summary": self.summary,
            "turns": [t.to_dict() for t in sorted(self.turns, key=lambda t: t.step_id)],
            "source_refs": [r.to_dict() for r in sorted(self.source_refs, key=lambda r: r.id)],
            "basis": self.basis.to_dict() if self.basis else None,
            "metadata": _canon(self.metadata),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Node":
        return Node(
            d["id"], d["root_id"], d["kind"], d["status"], d["title"], d["summary"],
            [Turn.from_dict(t) for t in d["turns"]],
            [SourceRef.from_dict(r) for r in d["source_refs"]],
            Basis.from_dict(d["basis"]) if d["basis"] else None,
            dict(d["metadata"]),
        )


@dataclass
class Edge:
    id: str
    from_node: str  # serialized as "from"
    to_node: str  # serialized as "to"
    edge_class: str  # serialized as "class"
    kind: str
    canonical_backbone: bool
    source_refs: List[SourceRef] = field(default_factory=list)
    basis: Optional[Basis] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def source_event_ids(self) -> frozenset:
        return frozenset(ref.event_id for ref in self.source_refs)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "from": self.from_node,
            "to": self.to_node,
            "class": self.edge_class,
            "kind": self.kind,
            "canonical_backbone": self.canonical_backbone,
            "source_refs": [r.to_dict() for r in sorted(self.source_refs, key=lambda r: r.id)],
            "basis": self.basis.to_dict() if self.basis else None,
            "metadata": _canon(self.metadata),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Edge":
        return Edge(
            d["id"], d["from"], d["to"], d["class"], d["kind"], d["canonical_backbone"],
            [SourceRef.from_dict(r) for r in d["source_refs"]],
            Basis.from_dict(d["basis"]) if d["basis"] else None,
            dict(d["metadata"]),
        )


@dataclass
class Warning:
    code: str
    severity: str  # error | warning | info
    message: str
    node_ids: List[str] = field(default_factory=list)
    edge_ids: List[str] = field(default_factory=list)
    source_ref_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "node_ids": sorted(self.node_ids),
            "edge_ids": sorted(self.edge_ids),
            "source_ref_ids": sorted(self.source_ref_ids),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Warning":
        return Warning(d["code"], d["severity"], d["message"],
                       list(d["node_ids"]), list(d["edge_ids"]), list(d["source_ref_ids"]))


# Diagnostics counters, in canonical order. v3's set carries over; §5 adds
# `genealogy_edge_count` and `refuted_claim_count` (the Q4 lens as a metric).
DIAGNOSTIC_COUNTERS = (
    "node_count", "edge_count", "root_count", "leaf_count", "fork_count",
    "maximum_depth", "branching_ratio", "backbone_edge_count",
    "structural_edge_count", "annotation_edge_count", "sequence_edge_count",
    "sequence_edge_ratio", "source_backed_edge_count", "inferred_edge_count",
    "missing_source_ref_count", "degraded_chronology", "projection_heavy_branching",
    "genealogy_edge_count", "refuted_claim_count",
)


@dataclass
class Diagnostics:
    node_count: int = 0
    edge_count: int = 0
    root_count: int = 0
    leaf_count: int = 0
    fork_count: int = 0
    maximum_depth: int = 0
    branching_ratio: float = 0.0
    backbone_edge_count: int = 0
    structural_edge_count: int = 0
    annotation_edge_count: int = 0
    sequence_edge_count: int = 0
    sequence_edge_ratio: float = 0.0
    source_backed_edge_count: int = 0
    inferred_edge_count: int = 0
    missing_source_ref_count: int = 0
    degraded_chronology: bool = False
    projection_heavy_branching: bool = False
    genealogy_edge_count: int = 0
    refuted_claim_count: int = 0
    warnings: List[Warning] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {c: getattr(self, c) for c in DIAGNOSTIC_COUNTERS}
        out["warnings"] = [w.to_dict() for w in sorted(self.warnings, key=_warning_key)]
        return out

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Diagnostics":
        diag = Diagnostics(**{c: d[c] for c in DIAGNOSTIC_COUNTERS})
        diag.warnings = [Warning.from_dict(w) for w in d["warnings"]]
        return diag


@dataclass
class EventRange:
    start: Optional[str]
    end: Optional[str]
    complete: bool


@dataclass
class Session:
    id: str
    event_range: EventRange


@dataclass
class Projection:
    extension_id: str
    watermark_event_id: Optional[str]
    basis: str
    degraded: bool


@dataclass
class Construction:
    operation: str
    policy: str
    trigger: str
    predecessor_artifact_event_id: Optional[str] = None
    predecessor_watermark_event_id: Optional[str] = None
    observer_result_event_id: Optional[str] = None


@dataclass
class Artifact:
    """The whole ``euler.causal_dag.v5`` artifact. ``forest.roots`` is derived."""

    generated_at: str
    session: Session
    projection: Projection
    construction: Construction
    nodes: List[Node] = field(default_factory=list)
    edges: List[Edge] = field(default_factory=list)
    active_root: Optional[str] = None
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    schema: str = SCHEMA
    media_type: str = MEDIA_TYPE

    def roots(self) -> List[str]:
        """Nodes with no incoming canonical-backbone edge (§5, derived)."""
        with_parent = {e.to_node for e in self.edges if e.canonical_backbone}
        return sorted(n.id for n in self.nodes if n.id not in with_parent)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "media_type": self.media_type,
            "generated_at": self.generated_at,
            "session": {
                "id": self.session.id,
                "event_range": {
                    "start": self.session.event_range.start,
                    "end": self.session.event_range.end,
                    "complete": self.session.event_range.complete,
                },
            },
            "projection": {
                "extension_id": self.projection.extension_id,
                "watermark_event_id": self.projection.watermark_event_id,
                "basis": self.projection.basis,
                "degraded": self.projection.degraded,
            },
            "construction": {
                "operation": self.construction.operation,
                "policy": self.construction.policy,
                "trigger": self.construction.trigger,
                "predecessor_artifact_event_id": self.construction.predecessor_artifact_event_id,
                "predecessor_watermark_event_id": self.construction.predecessor_watermark_event_id,
                "observer_result_event_id": self.construction.observer_result_event_id,
            },
            "forest": {
                "roots": self.roots(),
                "active_root": self.active_root,
                "nodes": [n.to_dict() for n in sorted(self.nodes, key=lambda n: n.id)],
                "edges": [e.to_dict() for e in sorted(self.edges, key=lambda e: e.id)],
            },
            "diagnostics": self.diagnostics.to_dict(),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Artifact":
        forest = d["forest"]
        session = d["session"]
        proj = d["projection"]
        con = d["construction"]
        rng = session["event_range"]
        return Artifact(
            generated_at=d["generated_at"],
            session=Session(session["id"], EventRange(rng["start"], rng["end"], rng["complete"])),
            projection=Projection(proj["extension_id"], proj["watermark_event_id"],
                                  proj["basis"], proj["degraded"]),
            construction=Construction(
                con["operation"], con["policy"], con["trigger"],
                con["predecessor_artifact_event_id"],
                con["predecessor_watermark_event_id"],
                con["observer_result_event_id"]),
            nodes=[Node.from_dict(n) for n in forest["nodes"]],
            edges=[Edge.from_dict(e) for e in forest["edges"]],
            active_root=forest["active_root"],
            diagnostics=Diagnostics.from_dict(d["diagnostics"]),
            schema=d["schema"],
            media_type=d["media_type"],
        )


def _warning_key(w: Warning) -> tuple:
    rank = {"error": 0, "warning": 1, "info": 2}.get(w.severity, 3)
    return (w.code, rank, w.message,
            tuple(sorted(w.node_ids)), tuple(sorted(w.edge_ids)),
            tuple(sorted(w.source_ref_ids)))


def dumps(artifact: Artifact) -> str:
    """Canonical, deterministic, strict JSON text (trailing newline included).

    ``allow_nan=False``: non-finite numbers have no JSON representation and
    would silently break canonical byte equality — refuse them.
    """
    return json.dumps(artifact.to_dict(), indent=2, ensure_ascii=False,
                      allow_nan=False) + "\n"


def loads(text: str) -> Artifact:
    return Artifact.from_dict(json.loads(text))
