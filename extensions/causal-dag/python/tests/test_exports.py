"""Deterministic DOT / Markdown / slot-summary renderers over a v5 artifact.

Structural (non-brittle) assertions on the public mini fixture, a first-class
check that ``refuted`` is reported as knowledge (its own heading and hue, never
the dead-end pile), and a budget-fitting test on a large synthetic graph that
pins the documented trim order — dead ends survive longest.
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from test_walk_import import EXPORT, STEPS, EVENTS  # noqa: E402

from causal_dag import (  # noqa: E402
    Artifact, Basis, Edge, Node, Turn, import_walk, to_dot, to_markdown,
    to_summary,
)
from causal_dag.schema import (  # noqa: E402
    Construction, EventRange, Projection, Session,
)


def _claim_artifact() -> Artifact:
    """A goal with a refuted claim and a dead-end investigation under it."""
    def basis():
        return Basis("operator", "op")

    nodes = [
        Node("n-goal", "n-goal", "question", "open", "The goal", "why we are here",
             [Turn(0, ["e0"])], [], basis(), {}),
        Node("n-claim", "n-goal", "claim", "refuted", "Closed form exists",
             "counterexample at k=5 falsifies it.", [Turn(1, ["e1"])], [], basis(), {}),
        Node("n-dead", "n-goal", "investigation", "dead_end", "Brute force",
             "too slow; abandoned after timeout.", [Turn(2, ["e2"])], [], basis(), {}),
    ]
    edges = [
        Edge("e-1", "n-goal", "n-claim", "structural", "decomposition", True,
             [], basis(), {}),
        Edge("e-2", "n-goal", "n-dead", "structural", "fork", True, [], basis(), {}),
        Edge("e-3", "n-claim", "n-dead", "annotation", "related", False, [], basis(), {}),
    ]
    return Artifact(
        generated_at="2026-07-25T00:00:00Z",
        session=Session("s-claim", EventRange("e0", "e2", True)),
        projection=Projection("causal-dag", "e2", "bounded_provenance_query", False),
        construction=Construction("snapshot", "manual", "command"),
        nodes=nodes, edges=edges, active_root="n-goal",
    )


def _big_artifact(chain=30, dead=30, opens=20) -> Artifact:
    """A graph far larger than a 4096-byte slot: a long active spine, many
    long-summary dead ends, and many open frontier nodes."""
    def basis():
        return Basis("operator", "op")

    nodes = [Node("root", "root", "question", "open", "Root goal of the session",
                  "", [Turn(0, ["e0"])], [], basis(), {})]
    edges = []
    prev = "root"
    for i in range(chain):
        nid = f"a{i:03d}"
        nodes.append(Node(nid, "root", "investigation", "succeeded",
                          f"Active step number {i} on the winning path", "",
                          [Turn(i + 1, [f"c{i}"])], [], basis(), {}))
        edges.append(Edge(f"be{i:03d}", prev, nid, "structural", "continuation",
                          True, [], basis(), {}))
        prev = nid
    reason = ("this approach failed because the invariant broke under load and "
              "the fixture could not be reconciled with the observed output; " * 3)
    for i in range(dead):
        nid = f"d{i:03d}"
        nodes.append(Node(nid, "root", "investigation", "dead_end",
                          f"Dead end attempt {i}", reason,
                          [Turn(1000 + i, [f"d{i}"])], [], basis(), {}))
        edges.append(Edge(f"de{i:03d}", "root", nid, "structural", "fork",
                          True, [], basis(), {}))
    for i in range(opens):
        nid = f"o{i:03d}"
        nodes.append(Node(nid, "root", "question", "open",
                          f"Open question number {i} still unresolved", "",
                          [Turn(2000 + i, [f"o{i}"])], [], basis(), {}))
        edges.append(Edge(f"oe{i:03d}", "root", nid, "structural", "decomposition",
                          True, [], basis(), {}))
    nodes.sort(key=lambda n: n.id)
    edges.sort(key=lambda e: e.id)
    return Artifact(
        generated_at="2026-07-25T00:00:00Z",
        session=Session("s-big", EventRange("e0", "z", True)),
        projection=Projection("causal-dag", "z", "bounded_provenance_query", False),
        construction=Construction("snapshot", "manual", "command"),
        nodes=nodes, edges=edges, active_root="root",
    )


class DotTest(unittest.TestCase):
    def setUp(self):
        self.art = import_walk(EXPORT, STEPS, EVENTS)

    def test_deterministic(self):
        self.assertEqual(to_dot(self.art), to_dot(self.art))

    def test_backbone_solid_annotations_dashed(self):
        dot = to_dot(self.art)
        self.assertTrue(dot.startswith("digraph causal_dag {"))
        self.assertIn('"n-goal" -> "n-try1"', dot)
        # mini has no cross-arcs, so no dashed edges
        self.assertNotIn("style=dashed", dot)

    def test_refuted_uses_its_own_hue_not_dead_end(self):
        dot = to_dot(_claim_artifact())
        # refuted crimson, decisive glyph ⊥ — never the dead_end vermillion/✗
        self.assertIn("#D7263D", dot)
        self.assertIn("⊥", dot)
        # the dashed annotation edge shows the arc kind
        self.assertIn("style=dashed", dot)


class MarkdownTest(unittest.TestCase):
    def test_sections_and_outline(self):
        md = to_markdown(import_walk(EXPORT, STEPS, EVENTS))
        for heading in ("## Backbone", "## Dead ends", "## Decided (refuted)",
                        "## Open frontier", "## Cross-arcs"):
            self.assertIn(heading, md)
        self.assertIn("**Mini goal**", md)

    def test_refuted_listed_under_decided_not_dead_ends(self):
        md = to_markdown(_claim_artifact())
        decided = md.index("## Decided (refuted)")
        dead = md.index("## Dead ends")
        self.assertIn("Closed form exists", md[decided:])
        # the refuted claim must not appear in the dead-ends block
        self.assertNotIn("Closed form exists", md[dead:decided])


class SummaryTest(unittest.TestCase):
    def test_header_and_sections(self):
        summary = to_summary(import_walk(EXPORT, STEPS, EVENTS))
        self.assertTrue(summary.startswith("GRAPH: Mini goal (4 nodes, 3 edges)"))
        self.assertIn("DEAD ENDS:", summary)
        self.assertIn("ACTIVE PATH:", summary)

    def test_budget_is_never_exceeded_and_dead_ends_survive(self):
        art = _big_artifact()
        full = to_summary(art, budget=100000)
        self.assertGreater(len(full.encode()), 4096)
        fit = to_summary(art, budget=4096)
        self.assertLessEqual(len(fit.encode()), 4096)
        # OPEN is dropped first; dead ends survive longest.
        self.assertNotIn("Open question number", fit)
        self.assertIn("DEAD ENDS:", fit)
        self.assertIn("Dead end attempt", fit)

    def test_trim_order_active_path_goes_before_dead_ends(self):
        # A budget tight enough to force ACTIVE PATH trimming still keeps a
        # dead-end line — the discipline the summary exists to enforce.
        art = _big_artifact(chain=12, dead=12, opens=12)
        fit = to_summary(art, budget=1200)
        self.assertLessEqual(len(fit.encode()), 1200)
        self.assertIn("Dead end attempt", fit)


if __name__ == "__main__":
    unittest.main()
