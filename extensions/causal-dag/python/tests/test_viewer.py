"""Viewer payload invariants, palette-v5 completeness, and degraded rendering.

The payload is the contract the archived shells render against: every node's
folded backbone parent must resolve, ``sequence`` must place parents before
children, arcs must reference real nodes, and the schema id is v5. The palette
must cover every v5 status and kind (day+night+glyph) and every annotation-edge
arc colour, and a degraded artifact must still render to a self-contained page.
"""

import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from test_walk_import import EXPORT, STEPS, EVENTS  # noqa: E402
from test_degraded import build_degraded  # noqa: E402

from causal_dag import (  # noqa: E402
    VIEWER_SCHEMA, VIEWS, import_walk, load_definitions, load_palette,
    render_html, viewer_payload,
)
from causal_dag.schema import (  # noqa: E402
    Artifact, Basis, Construction, Edge, EDGE_KINDS_BY_CLASS, EventRange, KINDS,
    Node, Projection, Session, STATUS_BY_KIND, Turn,
)


def _arc_artifact() -> Artifact:
    """A goal, a refuted claim under it, and a source-backed cross-arc with a note."""
    def basis(summary):
        return Basis("operator", summary)

    nodes = [
        Node("n-goal", "n-goal", "question", "open", "The goal",
             "Why we are here. A second sentence for the summary.",
             [Turn(0, ["e0"])], [], basis("op"), {}),
        Node("n-claim", "n-goal", "claim", "refuted", "Closed form exists",
             "Counterexample at k=5 falsifies it.", [Turn(1, ["e1"]), Turn(2, ["e2"])],
             [], basis("op"), {}),
    ]
    edges = [
        Edge("e-1", "n-goal", "n-claim", "structural", "decomposition", True,
             [], basis("op"), {}),
        Edge("e-2", "n-claim", "n-goal", "annotation", "refutation", False,
             [], basis("the k=5 counterexample refutes the goal's premise"), {}),
    ]
    return Artifact(
        generated_at="2026-07-26T00:00:00Z",
        session=Session("s-arc", EventRange("e0", "e2", True)),
        projection=Projection("causal-dag", "e2", "bounded_provenance_query", False),
        construction=Construction("snapshot", "manual", "command"),
        nodes=nodes, edges=edges, active_root="n-goal",
    )


def _all_statuses():
    return {s for axis in STATUS_BY_KIND.values() for s in axis}


class PayloadInvariantsTest(unittest.TestCase):
    def _check_payload(self, art):
        p = viewer_payload(art)
        self.assertEqual(p["schema"], VIEWER_SCHEMA)
        ids = {n["id"] for n in p["nodes"]}
        seq = {n["id"]: n["sequence"] for n in p["nodes"]}
        # sequence is a permutation 0..n-1
        self.assertEqual(sorted(seq.values()), list(range(len(p["nodes"]))))
        for n in p["nodes"]:
            if n["parent"] is not None:
                self.assertIn(n["parent"], ids)
                self.assertLess(seq[n["parent"]], n["sequence"],
                                f"{n['id']} precedes its parent")
            self.assertEqual(n["isRoot"], n["id"] in set(p["roots"]))
        for a in p["arcs"]:
            self.assertIn(a["from"], ids)
            self.assertIn(a["to"], ids)
        return p

    def test_mini_payload(self):
        self._check_payload(import_walk(EXPORT, STEPS, EVENTS))

    def test_degraded_payload(self):
        # sequence edges are backbone in the degraded spine, so they fold into
        # parents and no arcs remain.
        p = self._check_payload(build_degraded())
        self.assertEqual(p["arcs"], [])

    def test_occurrence_is_the_first_turn_step(self):
        p = viewer_payload(import_walk(EXPORT, STEPS, EVENTS))
        goal = next(n for n in p["nodes"] if n["id"] == "n-goal")
        self.assertEqual(goal["occurrence"], 0)

    def test_nodes_carry_summary_and_owned_turn_span(self):
        p = viewer_payload(_arc_artifact())
        claim = next(n for n in p["nodes"] if n["id"] == "n-claim")
        self.assertIn("Counterexample at k=5", claim["summary"])
        # two owned turns collapse to a compact span for the detail card
        self.assertEqual(claim["turns"], "1-2")
        goal = next(n for n in p["nodes"] if n["id"] == "n-goal")
        self.assertEqual(goal["turns"], "0")

    def test_arcs_carry_their_edge_note(self):
        p = viewer_payload(_arc_artifact())
        arc = next(a for a in p["arcs"] if a["id"] == "e-2")
        self.assertEqual(arc["kind"], "refutation")
        self.assertEqual(arc["class"], "annotation")
        self.assertIn("k=5 counterexample refutes", arc["note"])

    def test_payload_embeds_the_definitions_codebook(self):
        p = viewer_payload(_arc_artifact())
        defs = p["definitions"]
        for section in ("node_kinds", "statuses", "edge_kinds"):
            self.assertIn(section, defs)
        # the codebook mirror is complete enough to explain the cards' vocabulary
        self.assertIn("dead_end", defs["statuses"])
        self.assertIn("refutation", defs["edge_kinds"]["annotation"])


class DefinitionsLoaderTest(unittest.TestCase):
    def test_loader_returns_a_private_copy_of_the_embedded_codebook(self):
        a, b = load_definitions(), load_definitions()
        self.assertIsNot(a, b)  # mutating one render's copy can't taint another
        self.assertEqual(a, b)

    def test_env_override_is_used_when_readable(self):
        import json
        import os
        import tempfile
        custom = {"meta": {}, "node_kinds": {"question": "OVERRIDE DEF"},
                  "statuses": {}, "edge_kinds": {"structural": {}, "annotation": {}}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(custom, fh)
            path = fh.name
        old = os.environ.get("CAUSAL_DAG_DEFINITIONS")
        os.environ["CAUSAL_DAG_DEFINITIONS"] = path
        try:
            self.assertEqual(load_definitions()["node_kinds"]["question"],
                             "OVERRIDE DEF")
        finally:
            if old is None:
                del os.environ["CAUSAL_DAG_DEFINITIONS"]
            else:
                os.environ["CAUSAL_DAG_DEFINITIONS"] = old
            os.unlink(path)

    def test_unreadable_override_falls_back_to_the_embedded_mirror(self):
        import os
        old = os.environ.get("CAUSAL_DAG_DEFINITIONS")
        os.environ["CAUSAL_DAG_DEFINITIONS"] = "/nonexistent/definitions.json"
        try:
            self.assertIn("node_kinds", load_definitions())
        finally:
            if old is None:
                del os.environ["CAUSAL_DAG_DEFINITIONS"]
            else:
                os.environ["CAUSAL_DAG_DEFINITIONS"] = old


class PaletteCompletenessTest(unittest.TestCase):
    def setUp(self):
        self.pal = load_palette()

    def test_every_v5_status_has_day_night_glyph(self):
        statuses = self.pal["statuses"]
        for status in _all_statuses():
            self.assertIn(status, statuses, f"missing status {status}")
            for field in ("day", "night", "glyph", "label"):
                self.assertTrue(statuses[status].get(field), f"{status}.{field}")

    def test_status_order_matches_statuses(self):
        self.assertEqual(set(self.pal["status_order"]), set(self.pal["statuses"]))
        self.assertLessEqual(_all_statuses(), set(self.pal["status_order"]))

    def test_refuted_hue_is_not_the_dead_end_hue(self):
        s = self.pal["statuses"]
        self.assertNotEqual(s["refuted"]["day"], s["dead_end"]["day"])
        self.assertNotEqual(s["refuted"]["glyph"], s["dead_end"]["glyph"])

    def test_every_kind_has_day_night_glyph(self):
        kinds = self.pal["kinds"]
        for kind in KINDS:
            self.assertIn(kind, kinds, f"missing kind {kind}")
            for field in ("day", "night", "glyph"):
                self.assertTrue(kinds[kind].get(field), f"{kind}.{field}")
        # the consolidation flag gets its own 2D-surface glyph too
        self.assertTrue(kinds.get("consolidation", {}).get("glyph"))

    def test_every_annotation_kind_has_an_arc_colour(self):
        arc = self.pal["cross_arcs"]["kinds"]
        for kind in EDGE_KINDS_BY_CLASS["annotation"]:
            self.assertIn(kind, arc, f"annotation kind {kind} has no arc colour")


class RenderTest(unittest.TestCase):
    def test_every_view_is_self_contained_with_a_reparseable_payload(self):
        art = import_walk(EXPORT, STEPS, EVENTS)
        for view in VIEWS:
            html = render_html(art, view)
            for marker in ("__EULER_DAG__", "__EULER_PALETTE__", "__EULER_RUNTIME__"):
                self.assertNotIn(marker, html)
            self.assertNotIn("<script src=", html)
            start = html.index("const __DAG = ") + len("const __DAG = ")
            payload, _ = json.JSONDecoder().raw_decode(html, start)
            self.assertEqual(payload["schema"], VIEWER_SCHEMA)

    def test_degraded_artifact_renders(self):
        html = render_html(build_degraded(), "3-5d")
        self.assertIn("const __DAG = ", html)

    def test_every_view_carries_definitions_and_arc_notes_in_the_page(self):
        art = _arc_artifact()
        # a stable codebook line the node detail card looks up (dead-end status)
        dead_end_def = load_definitions()["statuses"]["dead_end"]
        for view in VIEWS:
            html = render_html(art, view)
            start = html.index("const __DAG = ") + len("const __DAG = ")
            payload, _ = json.JSONDecoder().raw_decode(html, start)
            self.assertIn("dead_end", payload["definitions"]["statuses"])
            self.assertTrue(any(a["note"] for a in payload["arcs"]))
            # the definition text and the influence-legend line reach the page
            self.assertIn(dead_end_def, html)
            self.assertIn("Arrows point in the direction of influence", html)
            self.assertIn("Counterexample at k=5", html)  # node summary plumbed

    def test_unknown_view_is_an_error(self):
        with self.assertRaises(ValueError):
            render_html(build_degraded(), "isometric")


if __name__ == "__main__":
    unittest.main()
