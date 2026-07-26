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
    VIEWER_SCHEMA, VIEWS, import_walk, load_palette, render_html, viewer_payload,
)
from causal_dag.schema import (  # noqa: E402
    EDGE_KINDS_BY_CLASS, KINDS, STATUS_BY_KIND,
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

    def test_unknown_view_is_an_error(self):
        with self.assertRaises(ValueError):
            render_html(build_degraded(), "isometric")


if __name__ == "__main__":
    unittest.main()
