"""A degraded chronology-spine artifact is legal when it says so honestly.

The degraded mode is the deterministic fallback when no semantic input
exists: a linear spine of sequence edges, chronology bases with no source
refs, an incomplete range — all legal exactly because projection.degraded
is true and the degraded_chronology warning covers every sequence edge.
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from causal_dag import (  # noqa: E402
    Artifact, Basis, Edge, Node, Turn, check, dumps, loads,
    recompute_diagnostics,
)
from causal_dag.schema import (  # noqa: E402
    Construction, EventRange, Projection, Session, Warning,
)


def build_degraded() -> Artifact:
    def node(nid, kind, step, event):
        return Node(nid, "node-1", kind, "open", nid, "",
                    [Turn(step, [event])], [],
                    Basis("chronology", "page order"), {})

    nodes = [
        node("node-1", "question", 0, "ev-0"),
        node("node-2", "investigation", 1, "ev-1"),
        node("node-3", "investigation", 2, "ev-2"),
    ]
    edges = [
        Edge("seq-1", "node-1", "node-2", "chronology", "sequence", True,
             [], Basis("chronology", "page order")),
        Edge("seq-2", "node-2", "node-3", "chronology", "sequence", True,
             [], Basis("chronology", "page order")),
    ]
    art = Artifact(
        generated_at="2026-07-25T00:00:00Z",
        session=Session("session-degraded", EventRange("ev-0", "ev-2", False)),
        projection=Projection("causal-dag", "ev-2", "bounded_provenance_query",
                              degraded=True),
        construction=Construction("snapshot", "manual", "command"),
        nodes=nodes, edges=edges, active_root="node-1",
    )
    art.diagnostics = recompute_diagnostics(art)
    art.diagnostics.warnings = [
        Warning("degraded_chronology", "warning",
                "chronology fallback: sequence edges are ordering, not causality",
                edge_ids=["seq-1", "seq-2"]),
    ]
    return art


class DegradedFixtureTest(unittest.TestCase):
    def test_honest_degraded_artifact_passes(self):
        self.assertEqual(check(build_degraded()), [])

    def test_round_trips(self):
        art = build_degraded()
        self.assertEqual(dumps(art), dumps(loads(dumps(art))))

    def test_unmarked_degradation_fails(self):
        art = build_degraded()
        art.projection.degraded = False
        names = {f.invariant for f in check(art)}
        self.assertIn("degraded_marking", names)

    def test_uncovered_sequence_edge_fails(self):
        art = build_degraded()
        art.diagnostics.warnings[0].edge_ids = ["seq-1"]  # seq-2 uncovered
        names = {f.invariant for f in check(art)}
        self.assertIn("degraded_marking", names)


if __name__ == "__main__":
    unittest.main()
