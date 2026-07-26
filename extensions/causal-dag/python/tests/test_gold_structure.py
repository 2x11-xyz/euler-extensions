"""CI-runnable checks over the committed gold structural manifest.

The full gold gate (comparing a fresh import against the manifest) needs the
private annotation data and runs where that data exists. This suite runs
everywhere: it verifies the committed manifest itself is a structurally sound
graph — so CI guards the manifest against corruption even though it cannot
rebuild the artifact.
"""

import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from causal_dag.schema import EDGE_KINDS_BY_CLASS, KINDS, STATUS_BY_KIND  # noqa: E402

MANIFEST = pathlib.Path(__file__).resolve().parent / "gold_structure.json"


class GoldManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = json.loads(MANIFEST.read_text())
        cls.nodes = {n["id"]: n for n in cls.m["nodes"]}

    def test_shape_counts(self):
        self.assertEqual(len(self.m["nodes"]), 31)
        self.assertEqual(len(self.m["edges"]), 46)
        self.assertEqual(sum(len(n["turns"]) for n in self.m["nodes"]), 60)

    def test_vocabulary(self):
        for n in self.m["nodes"]:
            self.assertIn(n["kind"], KINDS)
            self.assertIn(n["status"], STATUS_BY_KIND[n["kind"]])
        for e in self.m["edges"]:
            self.assertIn(e["kind"], EDGE_KINDS_BY_CLASS[e["class"]])

    def test_edges_name_real_nodes(self):
        for e in self.m["edges"]:
            self.assertIn(e["from"], self.nodes)
            self.assertIn(e["to"], self.nodes)

    def test_turn_and_event_ownership_is_unique(self):
        steps, events = set(), set()
        for n in self.m["nodes"]:
            for t in n["turns"]:
                self.assertNotIn(t["step_id"], steps)
                steps.add(t["step_id"])
                self.assertTrue(t["event_ids"])
                for ev in t["event_ids"]:
                    self.assertNotIn(ev, events)
                    events.add(ev)

    def test_backbone_is_a_forest(self):
        parents = {}
        for e in self.m["edges"]:
            if e["backbone"]:
                self.assertNotIn(e["to"], parents,
                                 f"{e['to']} has two backbone parents")
                parents[e["to"]] = e["from"]
        roots = [nid for nid in self.nodes if nid not in parents]
        self.assertEqual(len(roots), 2)  # the goal + the unplaced synthesis
        for nid in self.nodes:  # every node climbs to a root without cycling
            seen = set()
            while nid in parents:
                self.assertNotIn(nid, seen)
                seen.add(nid)
                nid = parents[nid]
            self.assertIn(nid, roots)


if __name__ == "__main__":
    unittest.main()
