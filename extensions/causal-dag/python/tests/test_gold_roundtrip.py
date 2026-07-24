"""Gold round-trip: project the walk's annotation export and check every invariant.

The gold data lives outside this public repo (it is the human's annotation walk
over a real session). These paths are the annotation tool's local checkout; the
test skips cleanly when they are absent so the public suite still runs.
"""

import json
import pathlib
import sqlite3
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from causal_dag import check, dumps, import_walk, loads  # noqa: E402

TOOL = pathlib.Path("/home/exedev/code/2x11-xyz/causal-dag-annotation-tool")
EXPORT = TOOL / "exports" / "01KXBZY130DSAMPT8C72558Z4J-gold-20260724.json"
WALK_DB = TOOL / "walk.db"
SESSION = "01KXBZY130DSAMPT8C72558Z4J"


def _load_steps():
    con = sqlite3.connect(f"file:{WALK_DB}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT steps_json FROM sessions WHERE session_id = ?", (SESSION,)
        ).fetchone()
    finally:
        con.close()
    return json.loads(row[0])["steps"]


@unittest.skipUnless(EXPORT.exists() and WALK_DB.exists(),
                     "gold export / walk.db not present (kept out of this public repo)")
class GoldRoundTripTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.export = json.loads(EXPORT.read_text())
        cls.steps = _load_steps()
        cls.artifact = import_walk(cls.export, cls.steps)

    def test_conversion_passes_all_invariants(self):
        self.assertEqual(check(self.artifact), [])

    def test_counts_match_export(self):
        self.assertEqual(len(self.artifact.nodes), len(self.export["nodes"]))
        self.assertEqual(len(self.artifact.edges), len(self.export["edges"]))
        turns = sum(len(n.turns) for n in self.artifact.nodes)
        self.assertEqual(turns, len(self.export["node_steps"]))

    def test_two_roots_including_unplaced_node(self):
        # apply_patch has no backbone parent by design: rootness is topology,
        # so it surfaces as a second root with kind/status unchanged (§2.1).
        roots = self.artifact.roots()
        self.assertEqual(len(roots), 2)
        unplaced = next(n for n in self.artifact.nodes if n.id == "n-sao1is14t7bys1lt5ehi")
        self.assertIn(unplaced.id, roots)
        self.assertEqual(unplaced.kind, "synthesis")
        self.assertEqual(unplaced.status, "verified")

    def test_serialization_is_deterministic(self):
        once = dumps(self.artifact)
        twice = dumps(loads(once))
        self.assertEqual(once, twice)


if __name__ == "__main__":
    unittest.main()
