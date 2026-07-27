"""Viewer payload invariants, palette-v5 completeness, and degraded rendering.

The payload is the contract the archived shells render against: every node's
folded backbone parent must resolve, ``sequence`` must place parents before
children, arcs must reference real nodes, and the schema id is v5. The palette
must cover every v5 status and kind (day+night+glyph) and every annotation-edge
arc colour, and a degraded artifact must still render to a self-contained page.
"""

import json
import os
import pathlib
import re
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


class DefinitionsInputTest(unittest.TestCase):
    """Definitions are an explicit input: no environment reaches the payload."""

    _CUSTOM = {"meta": {}, "node_kinds": {"question": "EXPLICIT DEF"},
               "statuses": {}, "edge_kinds": {"structural": {}, "annotation": {}}}

    def test_loader_returns_a_private_copy_of_the_embedded_codebook(self):
        a, b = load_definitions(), load_definitions()
        self.assertIsNot(a, b)  # mutating one render's copy can't taint another
        self.assertEqual(a, b)

    def test_explicit_path_is_read_by_the_loader(self):
        import json
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(self._CUSTOM, fh)
            path = fh.name
        try:
            self.assertEqual(load_definitions(path)["node_kinds"]["question"],
                             "EXPLICIT DEF")
        finally:
            os.unlink(path)

    def test_payload_ignores_the_environment(self):
        # A bogus env var must not change the payload: the old CAUSAL_DAG_DEFINITIONS
        # lookup is gone from the payload/render path entirely.
        art = _arc_artifact()
        baseline = viewer_payload(art)
        old = os.environ.get("CAUSAL_DAG_DEFINITIONS")
        os.environ["CAUSAL_DAG_DEFINITIONS"] = "/nonexistent/definitions.json"
        try:
            self.assertEqual(viewer_payload(art), baseline)
        finally:
            if old is None:
                del os.environ["CAUSAL_DAG_DEFINITIONS"]
            else:
                os.environ["CAUSAL_DAG_DEFINITIONS"] = old

    def test_same_artifact_and_args_give_a_byte_identical_payload(self):
        import json
        art = _arc_artifact()
        old = os.environ.get("CAUSAL_DAG_DEFINITIONS")
        os.environ["CAUSAL_DAG_DEFINITIONS"] = "/some/other/definitions.json"
        try:
            a = json.dumps(viewer_payload(art), sort_keys=True)
        finally:
            if old is None:
                del os.environ["CAUSAL_DAG_DEFINITIONS"]
            else:
                os.environ["CAUSAL_DAG_DEFINITIONS"] = old
        b = json.dumps(viewer_payload(art), sort_keys=True)
        self.assertEqual(a, b)

    def test_explicit_definitions_argument_is_honored(self):
        p = viewer_payload(_arc_artifact(), definitions=self._CUSTOM)
        self.assertEqual(p["definitions"]["node_kinds"]["question"], "EXPLICIT DEF")

    def test_none_definitions_embeds_the_packaged_mirror(self):
        p = viewer_payload(_arc_artifact())
        self.assertEqual(p["definitions"], load_definitions())


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

    def _nav(self, stem):
        suffix = {"top-down": "top-down", "indented": "indented",
                  "3d": "3d", "3-5d": "3-5d"}
        return {v: f"{stem}-{s}.html" for v, s in suffix.items()}

    def test_nav_map_renders_plain_sibling_hrefs_and_no_postmessage(self):
        art = _arc_artifact()
        nav = self._nav("mini")
        for view in VIEWS:
            html = render_html(art, view, nav=nav)
            # every sibling filename is linked as a plain href
            for filename in nav.values():
                self.assertIn(f'href="{filename}"', html)
            # the archived postMessage/dagNav bridge is gone
            self.assertNotIn("dagNav", html)
            # no unresolved href tokens or hidden-nav style leak through
            self.assertNotIn("__EULER_NAV", html)
            self.assertNotIn(".dag-nav{display:none", html)

    def test_nav_omitted_hides_the_nav_ui(self):
        art = _arc_artifact()
        for view in VIEWS:
            html = render_html(art, view)  # no nav map
            # the two view-switch dropdowns (class dag-nav) are hidden outright
            self.assertIn(".dag-nav{display:none !important;}", html)
            self.assertNotIn("dagNav", html)
            self.assertNotIn("__EULER_NAV", html)

    def test_detail_card_content_scrolls_and_is_not_clip_hidden(self):
        art = _arc_artifact()
        for view in VIEWS:
            html = render_html(art, view, nav=self._nav("mini"))
            # the card scrolls within its bounded height ...
            self.assertRegex(html, r"\.dag-card\s*\{[^}]*overflow-y:\s*auto")
            # ... and no dag-card rule clips its content with overflow:hidden
            for rule in re.findall(r"\.dag-card\s*\{[^}]*\}", html):
                self.assertNotIn("overflow:hidden", rule)
            # the card element's own inline style never hidden-clips either
            for style in re.findall(r'class="dag-card"[^>]*style="([^"]*)"', html):
                self.assertNotIn("overflow:hidden", style)


if __name__ == "__main__":
    unittest.main()
