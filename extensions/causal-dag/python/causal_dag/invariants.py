"""The SCHEMA-v5 §4 structural invariants as a data-driven check table.

Each invariant is one small named function ``(Artifact) -> List[Finding]``.
``CHECKS`` lists them; ``check`` runs the table and concatenates findings.
An empty result is a pass.

The checks are internal-consistency only: they read the artifact, never a live
event stream (provenance-against-stream validation belongs to the projection
lane). Per R7/§4.9 the engine *reports* — it returns findings and never raises;
the caller decides whether an operator-input finding is advisory or, for
projection output, a defect.

``recompute_diagnostics`` is the single source of truth for §5's counters: the
converter fills diagnostics with it, and ``check_diagnostics`` re-derives and
compares against it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Set

from .schema import (
    Artifact, Diagnostics, EDGE_KINDS_BY_CLASS, GENEALOGY_KINDS, KINDS,
    STATUS_BY_KIND, TERMINAL_STATUSES, BASIS_KINDS, DIAGNOSTIC_COUNTERS,
)


@dataclass
class Finding:
    invariant: str
    message: str
    node_ids: List[str]
    edge_ids: List[str]


def _f(invariant: str, message: str, node_ids=(), edge_ids=()) -> Finding:
    return Finding(invariant, message, list(node_ids), list(edge_ids))


def _backbone_parents(art: Artifact) -> Dict[str, List[str]]:
    parents: Dict[str, List[str]] = {n.id: [] for n in art.nodes}
    for e in art.edges:
        if e.canonical_backbone and e.to_node in parents:
            parents[e.to_node].append(e.from_node)
    return parents


def check_id_uniqueness(art: Artifact) -> List[Finding]:
    """Node/edge ids are disjoint; source_ref ids are globally unique (v3)."""
    out: List[Finding] = []
    seen: Dict[str, str] = {}
    for n in art.nodes:
        if n.id in seen:
            out.append(_f("id_uniqueness", f"duplicate id {n.id}", node_ids=[n.id]))
        seen[n.id] = "node"
    for e in art.edges:
        if e.id in seen:
            out.append(_f("id_uniqueness", f"node/edge id collision {e.id}", edge_ids=[e.id]))
        seen[e.id] = "edge"
    refs: Set[str] = set()
    for owner in list(art.nodes) + list(art.edges):
        for ref in owner.source_refs:
            if ref.id in seen or ref.id in refs:
                out.append(_f("id_uniqueness", f"source_ref id not globally unique {ref.id}"))
            refs.add(ref.id)
    return out


def check_canonical_ordering(art: Artifact) -> List[Finding]:
    """Roots, nodes, edges, source_refs, basis ids are sorted and duplicate-free."""
    out: List[Finding] = []

    def sorted_unique(ids) -> bool:
        ids = list(ids)
        return all(ids[i] < ids[i + 1] for i in range(len(ids) - 1))

    if not sorted_unique(art.roots()):
        out.append(_f("canonical_ordering", "forest.roots not sorted/unique"))
    if not sorted_unique([n.id for n in art.nodes]):
        out.append(_f("canonical_ordering", "nodes not sorted by id"))
    if not sorted_unique([e.id for e in art.edges]):
        out.append(_f("canonical_ordering", "edges not sorted by id"))
    for owner in list(art.nodes) + list(art.edges):
        if not sorted_unique([r.id for r in owner.source_refs]):
            out.append(_f("canonical_ordering", "source_refs not sorted by id"))
        if owner.basis and not sorted_unique(owner.basis.source_ref_ids):
            out.append(_f("canonical_ordering", "basis.source_ref_ids not sorted/unique"))
    return out


def check_vocabulary(art: Artifact) -> List[Finding]:
    """Node kind/status obey the per-kind axes (§2); edge class/kind are legal (§3)."""
    out: List[Finding] = []
    for n in art.nodes:
        if n.kind not in KINDS:
            out.append(_f("vocabulary", f"unknown node kind {n.kind!r}", node_ids=[n.id]))
        elif n.status not in STATUS_BY_KIND[n.kind]:
            out.append(_f("vocabulary",
                          f"status {n.status!r} illegal for kind {n.kind!r}", node_ids=[n.id]))
    for e in art.edges:
        kinds = EDGE_KINDS_BY_CLASS.get(e.edge_class)
        if kinds is None:
            out.append(_f("vocabulary", f"unknown edge class {e.edge_class!r}", edge_ids=[e.id]))
        elif e.kind not in kinds:
            out.append(_f("vocabulary",
                          f"kind {e.kind!r} illegal for class {e.edge_class!r}", edge_ids=[e.id]))
    return out


def check_source_ref_shape(art: Artifact) -> List[Finding]:
    """Basis and source_ref shape (v3): variant fields, pointers, basis coverage."""
    out: List[Finding] = []
    for owner in list(art.nodes) + list(art.edges):
        oid = owner.id
        local = {r.id for r in owner.source_refs}
        if owner.basis:
            if owner.basis.kind not in BASIS_KINDS:
                out.append(_f("source_ref_shape", f"unknown basis kind {owner.basis.kind!r}"))
            if owner.basis.kind in ("direct", "cluster", "operator") and not owner.source_refs:
                out.append(_f("source_ref_shape", f"{oid} basis {owner.basis.kind} needs source_refs"))
            for rid in owner.basis.source_ref_ids:
                if rid not in local:
                    out.append(_f("source_ref_shape", f"{oid} basis cites missing source_ref {rid}"))
        for r in owner.source_refs:
            if r.kind == "event" and (r.artifact is not None or r.blob is not None):
                out.append(_f("source_ref_shape", f"event ref {r.id} must null artifact/blob"))
            elif r.kind == "artifact" and (r.artifact is None or r.blob is not None):
                out.append(_f("source_ref_shape", f"artifact ref {r.id} needs artifact object"))
            elif r.kind == "blob" and (r.blob is None or r.artifact is not None):
                out.append(_f("source_ref_shape", f"blob ref {r.id} needs blob object"))
            elif r.kind not in ("event", "artifact", "blob"):
                out.append(_f("source_ref_shape", f"unknown source_ref kind {r.kind!r}"))
            if r.payload_pointer is not None and r.payload_pointer and not r.payload_pointer.startswith("/"):
                out.append(_f("source_ref_shape", f"payload_pointer of {r.id} not a JSON pointer"))
    return out


def check_edge_endpoints(art: Artifact) -> List[Finding]:
    """Every edge names existing nodes (v3)."""
    ids = {n.id for n in art.nodes}
    out: List[Finding] = []
    for e in art.edges:
        if e.from_node not in ids or e.to_node not in ids:
            out.append(_f("edge_endpoints", f"edge {e.id} names a missing node", edge_ids=[e.id]))
    return out


def check_backbone_parent_count(art: Artifact) -> List[Finding]:
    """Roots have 0 backbone parents; every other node has exactly 1 (v3)."""
    parents = _backbone_parents(art)
    roots = set(art.roots())
    out: List[Finding] = []
    for n in art.nodes:
        count = len(parents.get(n.id, []))
        if n.id in roots:
            if count != 0:  # unreachable given roots() derives from parents
                out.append(_f("backbone_parent_count", f"root {n.id} has a backbone parent", node_ids=[n.id]))
        elif count != 1:
            out.append(_f("backbone_parent_count",
                          f"node {n.id} has {count} backbone parents, expected 1", node_ids=[n.id]))
    return out


def check_root_membership(art: Artifact) -> List[Finding]:
    """root_id names the root reached by climbing backbone; active_root is a root (v3)."""
    parents = _backbone_parents(art)
    out: List[Finding] = []

    def climb(nid: str) -> str:
        seen: Set[str] = set()
        while parents.get(nid):
            if nid in seen:
                return nid
            seen.add(nid)
            nid = parents[nid][0]
        return nid

    for n in art.nodes:
        reached = climb(n.id)
        if n.root_id != reached:
            out.append(_f("root_membership",
                          f"node {n.id} root_id {n.root_id} != backbone root {reached}", node_ids=[n.id]))
    if art.active_root is not None and art.active_root not in set(art.roots()):
        out.append(_f("root_membership", f"active_root {art.active_root} is not a root"))
    return out


def check_cross_root(art: Artifact) -> List[Finding]:
    """Backbone edges stay within a root; cross-root edges are annotations (v3, §4.3)."""
    root_of = {n.id: n.root_id for n in art.nodes}
    out: List[Finding] = []
    for e in art.edges:
        if root_of.get(e.from_node) != root_of.get(e.to_node):
            if e.canonical_backbone:
                out.append(_f("cross_root", f"edge {e.id} is a cross-root backbone edge", edge_ids=[e.id]))
            if e.edge_class != "annotation":
                out.append(_f("cross_root", f"cross-root edge {e.id} must be an annotation", edge_ids=[e.id]))
    return out


def check_acyclicity(art: Artifact) -> List[Finding]:
    """Structural, backbone, and chronology subgraphs are acyclic (v3)."""
    out: List[Finding] = []
    lenses = {
        "structural": lambda e: e.edge_class == "structural",
        "backbone": lambda e: e.canonical_backbone,
        "chronology": lambda e: e.edge_class == "chronology",
    }
    for name, keep in lenses.items():
        adj: Dict[str, List[str]] = {}
        for e in art.edges:
            if keep(e):
                adj.setdefault(e.from_node, []).append(e.to_node)
        if _has_cycle(adj):
            out.append(_f("acyclicity", f"{name} subgraph contains a cycle"))
    return out


def _has_cycle(adj: Dict[str, List[str]]) -> bool:
    visiting: Set[str] = set()
    visited: Set[str] = set()

    def walk(node: str) -> bool:
        if node in visited:
            return False
        if node in visiting:
            return True
        visiting.add(node)
        for nxt in adj.get(node, ()):
            if walk(nxt):
                return True
        visiting.discard(node)
        visited.add(node)
        return False

    return any(walk(node) for node in adj)


def check_terminal_children(art: Artifact) -> List[Finding]:
    """Terminal nodes take structural children only via repair, sharing evidence (§4.2)."""
    node_by_id = {n.id: n for n in art.nodes}
    out: List[Finding] = []
    for e in art.edges:
        if e.edge_class != "structural":
            continue
        parent = node_by_id.get(e.from_node)
        if parent is None or parent.status not in TERMINAL_STATUSES:
            continue
        if e.kind != "repair":
            out.append(_f("terminal_children",
                          f"terminal node {parent.id} has non-repair structural child via {e.id}",
                          node_ids=[parent.id], edge_ids=[e.id]))
        elif parent.source_event_ids().isdisjoint(e.source_event_ids()):
            out.append(_f("terminal_children",
                          f"repair {e.id} shares no evidence with the failure {parent.id}",
                          node_ids=[parent.id], edge_ids=[e.id]))
    return out


def check_generative_content_backed(art: Artifact) -> List[Finding]:
    """Generative edges cite the failure they spring from — never adjacency alone (§4.6)."""
    node_by_id = {n.id: n for n in art.nodes}
    out: List[Finding] = []
    for e in art.edges:
        if e.kind not in GENEALOGY_KINDS:
            continue
        parent = node_by_id.get(e.from_node)
        if not e.source_refs or (parent and parent.source_event_ids().isdisjoint(e.source_event_ids())):
            out.append(_f("generative_content_backed",
                          f"{e.kind} edge {e.id} does not cite the failure's evidence", edge_ids=[e.id]))
    return out


def check_one_node_per_turn(art: Artifact) -> List[Finding]:
    """A turn — and its events — belong to at most one node (R1, R2)."""
    step_owner: Dict[int, str] = {}
    event_owner: Dict[str, str] = {}
    out: List[Finding] = []
    for n in art.nodes:
        for turn in n.turns:
            if turn.step_id in step_owner and step_owner[turn.step_id] != n.id:
                out.append(_f("one_node_per_turn",
                              f"turn {turn.step_id} owned by {step_owner[turn.step_id]} and {n.id}",
                              node_ids=[n.id]))
            step_owner[turn.step_id] = n.id
            for ev in turn.event_ids:
                if ev in event_owner and event_owner[ev] != n.id:
                    out.append(_f("one_node_per_turn",
                                  f"event {ev} owned by {event_owner[ev]} and {n.id}", node_ids=[n.id]))
                event_owner[ev] = n.id
    return out


def check_diagnostics(art: Artifact) -> List[Finding]:
    """Diagnostics counters match a fresh recomputation; warnings resolve (§5)."""
    computed = recompute_diagnostics(art)
    out: List[Finding] = []
    for counter in DIAGNOSTIC_COUNTERS:
        actual, expected = getattr(art.diagnostics, counter), getattr(computed, counter)
        if isinstance(expected, float):
            if abs(actual - expected) > 1e-6:
                out.append(_f("diagnostics", f"{counter} is {actual}, expected {expected}"))
        elif actual != expected:
            out.append(_f("diagnostics", f"{counter} is {actual}, expected {expected}"))
    node_ids = {n.id for n in art.nodes}
    edge_ids = {e.id for e in art.edges}
    ref_ids = {r.id for owner in list(art.nodes) + list(art.edges) for r in owner.source_refs}
    for w in art.diagnostics.warnings:
        if (set(w.node_ids) - node_ids or set(w.edge_ids) - edge_ids
                or set(w.source_ref_ids) - ref_ids):
            out.append(_f("diagnostics", f"warning {w.code} references an unknown id"))
    if not art.nodes and not any(w.code == "empty_forest" for w in art.diagnostics.warnings):
        out.append(_f("diagnostics", "empty forest requires an empty_forest warning"))
    return out


# The invariant table. One entry per §4 rule (plus the envelope rules v5 inherits
# from v3). Order is reporting order only; checks are independent.
CHECKS: List[Callable[[Artifact], List[Finding]]] = [
    check_id_uniqueness,
    check_canonical_ordering,
    check_vocabulary,
    check_source_ref_shape,
    check_edge_endpoints,
    check_backbone_parent_count,
    check_root_membership,
    check_cross_root,
    check_acyclicity,
    check_terminal_children,
    check_generative_content_backed,
    check_one_node_per_turn,
    check_diagnostics,
]


def check(art: Artifact) -> List[Finding]:
    """Run every invariant; return all findings (empty means the artifact passes)."""
    return [finding for run in CHECKS for finding in run(art)]


def recompute_diagnostics(art: Artifact) -> Diagnostics:
    """Derive the §5 diagnostics counters from the graph. Single source of truth."""
    roots = art.roots()
    backbone = [e for e in art.edges if e.canonical_backbone]
    out_deg: Dict[str, int] = {}
    adj: Dict[str, List[str]] = {}
    for e in backbone:
        out_deg[e.from_node] = out_deg.get(e.from_node, 0) + 1
        adj.setdefault(e.from_node, []).append(e.to_node)

    node_count = len(art.nodes)
    edge_count = len(art.edges)
    leaf_count = sum(1 for n in art.nodes if out_deg.get(n.id, 0) == 0)
    fork_count = sum(1 for c in out_deg.values() if c > 1)
    sequence = sum(1 for e in art.edges if e.kind == "sequence")

    def source_backed(owner) -> bool:
        return bool(owner.source_refs) and owner.basis is not None \
            and owner.basis.kind in ("direct", "cluster", "operator") \
            and all(r.payload_pointer is None or r.payload_pointer.startswith("/")
                    for r in owner.source_refs)

    weak_backbone = sum(1 for e in backbone
                        if e.basis and e.basis.kind in ("inferred", "chronology"))
    source_backed_backbone = sum(1 for e in backbone if source_backed(e))

    return Diagnostics(
        node_count=node_count,
        edge_count=edge_count,
        root_count=len(roots),
        leaf_count=leaf_count,
        fork_count=fork_count,
        maximum_depth=_maximum_depth(roots, adj),
        branching_ratio=fork_count / max(1, node_count - len(roots)),
        backbone_edge_count=len(backbone),
        structural_edge_count=sum(1 for e in art.edges if e.edge_class == "structural"),
        annotation_edge_count=sum(1 for e in art.edges if e.edge_class == "annotation"),
        sequence_edge_count=sequence,
        sequence_edge_ratio=sequence / max(1, edge_count),
        source_backed_edge_count=sum(1 for e in art.edges if source_backed(e)),
        inferred_edge_count=sum(1 for e in art.edges
                                if e.basis and e.basis.kind in ("inferred", "chronology")),
        missing_source_ref_count=0,
        degraded_chronology=sequence > 0,
        projection_heavy_branching=weak_backbone > source_backed_backbone,
        genealogy_edge_count=sum(1 for e in art.edges if e.kind in GENEALOGY_KINDS),
        refuted_claim_count=sum(1 for n in art.nodes if n.kind == "claim" and n.status == "refuted"),
        warnings=list(art.diagnostics.warnings),
    )


def _maximum_depth(roots: List[str], adj: Dict[str, List[str]]) -> int:
    best = 0
    stack = [(r, 0) for r in roots]
    while stack:
        node, depth = stack.pop()
        best = max(best, depth)
        stack.extend((child, depth + 1) for child in adj.get(node, ()))
    return best
