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
    Construction, EventRange, Projection, Session, SourceRef,
)
from causal_dag.invariants import check, recompute_diagnostics  # noqa: E402


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


def _layered_artifact(chain=6, opens=8, inconc=6, blocked=4, dead=5,
                      refuted=3) -> Artifact:
    """A graph with a long active spine plus every off-path decisive class:
    dead ends, refuted claims, blocked and inconclusive nodes, open questions.
    The forked classes hang off the root, so none of them ride the longest
    active path — they exist only to be reported (or trimmed) in their own
    sections."""
    def basis():
        return Basis("operator", "op")

    nodes = [Node("n0000", "n0000", "question", "open",
                  "Root goal of the session", "", [Turn(0, ["e0"])], [], basis(), {})]
    edges = []
    prev = "n0000"
    for i in range(1, chain):
        nid = f"a{i:03d}"
        nodes.append(Node(nid, "n0000", "investigation", "succeeded",
                          f"Active spine step number {i}", "",
                          [Turn(i, [f"c{i}"])], [], basis(), {}))
        edges.append(Edge(f"be{i:03d}", prev, nid, "structural", "continuation",
                          True, [], basis(), {}))
        prev = nid
    reason = ("the invariant broke under load and could not be reconciled with "
              "observed output. ")
    spec = [
        ("o", opens, "question", "open", "Open question number {} still unresolved", ""),
        ("i", inconc, "claim", "inconclusive", "Inconclusive claim number {} pending",
         "evidence split."),
        ("b", blocked, "investigation", "blocked", "Blocked probe number {} waiting",
         "waiting on access."),
        ("d", dead, "investigation", "dead_end", "Dead end attempt number {}", reason),
        ("r", refuted, "claim", "refuted", "Refuted claim number {}",
         "counterexample at k=5 falsifies it."),
    ]
    turn = 100
    for prefix, count, kind, status, title, summary in spec:
        edge_kind = "fork" if status in ("blocked", "dead_end") else "decomposition"
        for i in range(count):
            nid = f"{prefix}{i:03d}"
            nodes.append(Node(nid, "n0000", kind, status, title.format(i), summary,
                              [Turn(turn, [f"t{turn}"])], [], basis(), {}))
            edges.append(Edge(f"{prefix}e{i:03d}", "n0000", nid, "structural",
                              edge_kind, True, [], basis(), {}))
            turn += 1
    nodes.sort(key=lambda n: n.id)
    edges.sort(key=lambda e: e.id)
    return Artifact(
        generated_at="2026-07-25T00:00:00Z",
        session=Session("s-layered", EventRange("e0", "z", True)),
        projection=Projection("causal-dag", "z", "bounded_provenance_query", False),
        construction=Construction("snapshot", "manual", "command"),
        nodes=nodes, edges=edges, active_root="n0000",
    )


def _chain_artifact(n=1400) -> Artifact:
    """A valid n-node backbone chain that passes ``check()``. Deep enough to
    blow Python's recursion limit through any recursive traversal."""
    def basis():
        return Basis("operator", "op")

    nodes, edges = [], []
    for i in range(n):
        nid = f"n{i:05d}"
        kind = "question" if i == 0 else "investigation"
        status = "open" if i == 0 else "succeeded"
        nref = SourceRef(f"sr-n{i:05d}", "event", f"ev-n{i:05d}", "extension.turn")
        nodes.append(Node(nid, "n00000", kind, status, f"Chain step {i}", "",
                          [Turn(i, [f"tv{i:05d}"])], [nref], basis(), {}))
        if i > 0:
            eref = SourceRef(f"sr-e{i:05d}", "event", f"ev-e{i:05d}", "extension.turn")
            edges.append(Edge(f"e{i:05d}", f"n{i - 1:05d}", nid, "structural",
                              "continuation", True, [eref], basis(), {}))
    art = Artifact(
        generated_at="2026-07-25T00:00:00Z",
        session=Session("s-chain", EventRange("ev-n00000", f"ev-n{n - 1:05d}", True)),
        projection=Projection("causal-dag", f"ev-n{n - 1:05d}",
                              "bounded_provenance_query", False),
        construction=Construction("snapshot", "manual", "command"),
        nodes=nodes, edges=edges, active_root="n00000",
    )
    art.diagnostics = recompute_diagnostics(art)
    return art


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

    def test_cross_arc_note_is_carried_into_markdown(self):
        art = _claim_artifact()
        art.edges[-1].basis = Basis("operator", "shares the k=5 counterexample")
        md = to_markdown(art)
        arcs = md[md.index("## Cross-arcs"):]
        self.assertIn("shares the k=5 counterexample", arcs)


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

    def test_dead_end_reason_is_the_first_sentence_of_the_summary(self):
        # The dead-end reason slot takes only the first sentence of the node
        # summary (≤360 bytes), not the whole thing.
        art = _claim_artifact()
        art.nodes[2].summary = ("Timed out at n=40. A long second sentence that "
                                "must not leak into the reason slot at all.")
        summary = to_summary(art, budget=100000)
        self.assertIn("Timed out at n=40.", summary)
        self.assertNotIn("must not leak", summary)

    def test_trim_order_active_path_goes_before_dead_ends(self):
        # A budget tight enough to force ACTIVE PATH trimming still keeps a
        # dead-end line — the discipline the summary exists to enforce.
        art = _big_artifact(chain=12, dead=12, opens=12)
        fit = to_summary(art, budget=1200)
        self.assertLessEqual(len(fit.encode()), 1200)
        self.assertIn("Dead end attempt", fit)

    def test_off_path_decisive_state_is_reported(self):
        # Finding 1: refuted, blocked, and inconclusive nodes that fall off the
        # longest active path must not vanish — each gets its own heading, and
        # refuted (like dead ends) carries a first-sentence reason.
        summary = to_summary(_layered_artifact(), budget=100000)
        for heading in ("DEAD ENDS:", "REFUTED:", "BLOCKED:", "INCONCLUSIVE:",
                        "ACTIVE PATH:", "OPEN:"):
            self.assertIn(heading, summary)
        self.assertIn("Refuted claim number 0", summary)
        self.assertIn("counterexample at k=5 falsifies it.", summary)  # reason line
        self.assertIn("Blocked probe number 0", summary)
        self.assertIn("Inconclusive claim number 0", summary)
        self.assertIn("Active spine step number 1", summary)  # spine survives
        # Report order: the decisive sections precede the frontier ones.
        self.assertLess(summary.index("REFUTED:"), summary.index("BLOCKED:"))
        self.assertLess(summary.index("INCONCLUSIVE:"), summary.index("ACTIVE PATH:"))
        self.assertLess(summary.index("ACTIVE PATH:"), summary.index("OPEN:"))

    def test_trim_order_open_then_inconclusive_then_active(self):
        # Finding 1 trim discipline, verified on one over-budget graph:
        # OPEN vanishes before INCONCLUSIVE, INCONCLUSIVE before ACTIVE is even
        # touched, and the decisive pile (dead ends + refuted) outlives them.
        art = _layered_artifact()
        self.assertGreater(len(to_summary(art, budget=100000).encode()), 1700)

        # OPEN gone, INCONCLUSIVE still present.
        fit = to_summary(art, budget=1450)
        self.assertLessEqual(len(fit.encode()), 1450)
        self.assertNotIn("Open question number", fit)
        self.assertIn("Inconclusive claim number", fit)

        # INCONCLUSIVE now gone, and the ACTIVE PATH is still intact (all five
        # spine steps) — inconclusive drains fully before active is trimmed.
        fit = to_summary(art, budget=1200)
        self.assertLessEqual(len(fit.encode()), 1200)
        self.assertNotIn("Inconclusive claim number", fit)
        self.assertEqual(fit.count("Active spine step number"), 5)

        # At the 512-byte floor the decisive negatives still survive.
        fit = to_summary(art, budget=512)
        self.assertLessEqual(len(fit.encode()), 512)
        self.assertIn("Dead end attempt", fit)
        self.assertIn("Refuted claim number", fit)

    def test_budget_below_minimum_raises(self):
        with self.assertRaises(ValueError):
            to_summary(_big_artifact(), budget=64)

    def test_tiny_budgets_are_byte_honoured(self):
        # Finding 6: at any accepted budget the output is <= budget, headings
        # and all. The old renderer overran because fixed headings survived.
        art = _layered_artifact()
        for budget in (512, 600):
            fit = to_summary(art, budget=budget)
            self.assertLessEqual(len(fit.encode()), budget)
            self.assertTrue(fit.startswith("GRAPH:"))


class DeepGraphTest(unittest.TestCase):
    """Finding 4: a 1,400-node backbone chain (a valid deep graph) must render
    through every exporter without a RecursionError."""

    def test_chain_is_valid_and_deep(self):
        art = _chain_artifact(1400)
        self.assertEqual(check(art), [])
        self.assertEqual(art.diagnostics.maximum_depth, 1399)

    def test_all_three_exporters_survive_a_deep_chain(self):
        art = _chain_artifact(1400)
        # Guard against a raised recursion limit masking the regression: the
        # default (~1000) is well below the chain depth.
        limit = sys.getrecursionlimit()
        sys.setrecursionlimit(1000)
        try:
            self.assertTrue(to_markdown(art).startswith("# Chain step 0"))
            self.assertTrue(to_summary(art).startswith("GRAPH:"))
            self.assertTrue(to_dot(art).startswith("digraph causal_dag {"))
        finally:
            sys.setrecursionlimit(limit)


if __name__ == "__main__":
    unittest.main()
