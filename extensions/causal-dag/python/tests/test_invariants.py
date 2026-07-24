"""Table-driven invariant tests over a small hand-written v5 fixture.

``build_valid`` returns a 6-node forest that passes every invariant. Each fail
case mutates a fresh copy so exactly one invariant is expected to fire; the
mutation runner recomputes diagnostics so the target rule is the only finding
(the diagnostics case sets its own stale counter).
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from causal_dag import (  # noqa: E402
    Artifact, Basis, Edge, Node, SourceRef, Turn, check, dumps, loads,
    recompute_diagnostics,
)
from causal_dag.schema import Construction, EventRange, Projection, Session  # noqa: E402


def _node(nid, root_id, kind, status, step, event, meta=None):
    ref = SourceRef(f"{nid}-r", "event", event, "tool.result", None)
    return Node(nid, root_id, kind, status, nid.title(), "",
                [Turn(step, [event])], [ref], Basis("operator", "asserted", [ref.id]),
                meta or {})


def _edge(eid, frm, to, cls, kind, backbone, event):
    ref = SourceRef(f"{eid}-r", "event", event, "tool.result", None)
    return Edge(eid, frm, to, cls, kind, backbone, [ref], Basis("operator", "asserted", [ref.id]))


def build_valid() -> Artifact:
    a, b, c, d, e, f = ("node-a-root", "node-b", "node-c", "node-d",
                        "node-e", "node-f-root")
    nodes = [
        _node(a, a, "question", "open", 0, "ev-0"),
        _node(b, a, "investigation", "succeeded", 1, "ev-1"),
        _node(c, a, "investigation", "dead_end", 2, "ev-2"),  # terminal
        _node(d, a, "investigation", "succeeded", 3, "ev-3"),
        _node(e, a, "synthesis", "stated", 4, "ev-4", {"consolidation": False}),
        _node(f, f, "synthesis", "verified", 5, "ev-5", {"consolidation": False}),
    ]
    edges = [
        _edge("edge-1", a, b, "structural", "fork", True, "ev-0"),
        _edge("edge-2", a, c, "structural", "fork", True, "ev-0"),
        _edge("edge-3", c, d, "structural", "repair", True, "ev-2"),  # shares c's evidence
        _edge("edge-4", b, e, "structural", "integration", True, "ev-1"),
        _edge("edge-5", c, b, "annotation", "pivot", False, "ev-2"),  # generative, cites failure
        _edge("edge-6", f, a, "annotation", "related", False, "ev-5"),  # cross-root
    ]
    art = Artifact(
        generated_at="2026-07-24T00:00:00Z",
        session=Session("session-synthetic", EventRange("ev-0", "ev-5", True)),
        projection=Projection("causal-dag", "ev-5", "bounded_provenance_query", False),
        construction=Construction("snapshot", "manual", "command"),
        nodes=nodes, edges=edges, active_root=a,
    )
    art.diagnostics = recompute_diagnostics(art)
    return art


def _names(findings):
    return {finding.invariant for finding in findings}


class ValidFixtureTest(unittest.TestCase):
    def test_passes_all_invariants(self):
        self.assertEqual(check(build_valid()), [])

    def test_serialization_is_deterministic(self):
        art = build_valid()
        once = dumps(art)
        twice = dumps(loads(once))
        self.assertEqual(once, twice)

    def test_roundtrip_preserves_findings_free(self):
        self.assertEqual(check(loads(dumps(build_valid()))), [])


# (invariant name, mutation) — each mutation should make that invariant fire.
FAIL_CASES = [
    ("id_uniqueness", lambda a: setattr(a.nodes[1], "id", a.nodes[0].id)),
    ("canonical_ordering", lambda a: a.nodes.reverse()),
    ("vocabulary", lambda a: setattr(a.nodes[1], "status", "proven")),
    ("source_ref_shape", lambda a: setattr(a.nodes[1].source_refs[0], "artifact", {"x": 1})),
    ("edge_endpoints", lambda a: setattr(a.edges[0], "to_node", "ghost")),
    ("backbone_parent_count",
     lambda a: a.edges.append(_edge("edge-7", "node-a-root", "node-b", "structural", "fork", True, "ev-0"))),
    ("root_membership", lambda a: setattr(a.nodes[3], "root_id", "node-f-root")),
    ("cross_root", lambda a: (setattr(a.edges[5], "edge_class", "structural"),
                              setattr(a.edges[5], "kind", "continuation"))),
    ("acyclicity",
     lambda a: a.edges.append(_edge("edge-7", "node-e", "node-a-root", "structural", "continuation", False, "ev-4"))),
    ("terminal_children", lambda a: setattr(a.edges[2], "kind", "continuation")),
    ("generative_content_backed",
     lambda a: (setattr(a.edges[4], "source_refs", []),
                setattr(a.edges[4].basis, "kind", "inferred"),
                setattr(a.edges[4].basis, "source_ref_ids", []))),
    ("one_node_per_turn", lambda a: a.nodes[1].turns.append(Turn(2, ["ev-2"]))),
    ("diagnostics", lambda a: setattr(a.diagnostics, "node_count", 999)),
    ("schema_identity", lambda a: setattr(a, "schema", "euler.causal_dag.v3")),
    ("construction", lambda a: setattr(a.construction, "operation", "rewrite")),
    ("construction",
     lambda a: setattr(a.construction, "predecessor_artifact_event_id", "ev-9")),
    ("backbone_class", lambda a: setattr(a.edges[4], "canonical_backbone", True)),
    ("degraded_marking",
     lambda a: (setattr(a.edges[5], "edge_class", "chronology"),
                setattr(a.edges[5], "kind", "sequence"))),
    ("basis_required", lambda a: setattr(a.nodes[1], "basis", None)),
    ("metadata_shadow", lambda a: a.nodes[1].metadata.update(status="open")),
    ("turns_nonempty", lambda a: setattr(a.nodes[1], "turns", [])),
]


class FailCaseTest(unittest.TestCase):
    def test_each_invariant_fires(self):
        for name, mutate in FAIL_CASES:
            with self.subTest(invariant=name):
                art = build_valid()
                mutate(art)
                if name != "diagnostics":
                    art.diagnostics = recompute_diagnostics(art)
                self.assertIn(name, _names(check(art)),
                              f"expected {name} to fire; got {_names(check(art))}")


class RobustnessTest(unittest.TestCase):
    def test_backbone_cycle_terminates_with_findings(self):
        # A backbone cycle must produce findings, never hang the validator
        # (the depth computation skips cycle edges instead of chasing them).
        art = build_valid()
        art.edges.append(
            _edge("edge-7", "node-d", "node-a-root", "structural", "continuation",
                  True, "ev-3"))
        art.diagnostics = recompute_diagnostics(art)
        self.assertIn("acyclicity", _names(check(art)))

    def test_nan_diagnostics_refused_by_dumps(self):
        art = build_valid()
        art.diagnostics.branching_ratio = float("nan")
        with self.assertRaises(ValueError):
            dumps(art)

    def test_metadata_insertion_order_does_not_change_bytes(self):
        one, two = build_valid(), build_valid()
        one.nodes[1].metadata = {"alpha": 1, "beta": {"y": 2, "x": 1}}
        two.nodes[1].metadata = {"beta": {"x": 1, "y": 2}, "alpha": 1}
        one.diagnostics = recompute_diagnostics(one)
        two.diagnostics = recompute_diagnostics(two)
        self.assertEqual(dumps(one), dumps(two))

    def test_long_valid_chain_validates_without_recursion_error(self):
        # A 1,400-node backbone chain is a legal graph; the validator must
        # handle it iteratively (report-only, terminates on any input).
        nodes = [_node("node-0000", "node-0000", "question", "open", 0, "ev-0000")]
        edges = []
        for i in range(1, 1400):
            nid, prev = f"node-{i:04d}", f"node-{i - 1:04d}"
            nodes.append(_node(nid, "node-0000", "investigation", "succeeded",
                               i, f"ev-{i:04d}"))
            edges.append(_edge(f"edge-{i:04d}", prev, nid, "structural",
                               "continuation", True, f"ev-{i - 1:04d}"))
        art = Artifact(
            generated_at="2026-07-24T00:00:00Z",
            session=Session("session-chain", EventRange("ev-0000", "ev-1399", True)),
            projection=Projection("causal-dag", "ev-1399", "bounded_provenance_query", False),
            construction=Construction("snapshot", "manual", "command"),
            nodes=nodes, edges=edges, active_root="node-0000",
        )
        art.diagnostics = recompute_diagnostics(art)
        self.assertEqual(check(art), [])
        self.assertEqual(art.diagnostics.maximum_depth, 1399)


class StrictLoadsTest(unittest.TestCase):
    def test_nan_text_is_rejected(self):
        import re
        text = re.sub(r'"branching_ratio": [0-9.]+', '"branching_ratio": NaN',
                      dumps(build_valid()))
        self.assertIn("NaN", text)
        with self.assertRaises(ValueError):
            loads(text)

    def test_unknown_top_level_key_is_rejected(self):
        import json
        d = json.loads(dumps(build_valid()))
        d["extra"] = True
        with self.assertRaises(ValueError):
            loads(json.dumps(d))

    def test_tampered_roots_are_rejected(self):
        import json
        d = json.loads(dumps(build_valid()))
        d["forest"]["roots"] = ["node-a-root"]  # drops the second derived root
        with self.assertRaises(ValueError):
            loads(json.dumps(d))


if __name__ == "__main__":
    unittest.main()
