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

from causal_dag import (  # noqa: E402
    check, dumps, import_walk, loads, structural_projection,
)

MANIFEST = pathlib.Path(__file__).resolve().parent / "gold_structure.json"
DIGEST = pathlib.Path(__file__).resolve().parent / "gold_digest.txt"

TOOL = pathlib.Path("/home/exedev/code/2x11-xyz/causal-dag-annotation-tool")
EXPORT = TOOL / "exports" / "gold-current.json"
WALK_DB = TOOL / "walk.db"
SESSION = "01KXBZY130DSAMPT8C72558Z4J"
EVENTS = pathlib.Path.home() / ".euler" / "sessions" / SESSION / "events.jsonl"


def _load_steps():
    con = sqlite3.connect(f"file:{WALK_DB}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT steps_json FROM sessions WHERE session_id = ?", (SESSION,)
        ).fetchone()
    finally:
        con.close()
    return json.loads(row[0])["steps"]


def _load_events():
    meta = {}
    with EVENTS.open() as fh:
        for line in fh:
            event = json.loads(line)
            meta[event["id"]] = {"kind": event["kind"], "ts": event["ts"]}
    return meta


@unittest.skipUnless(EXPORT.exists() and WALK_DB.exists() and EVENTS.exists(),
                     "gold export / walk.db / session log not present "
                     "(kept out of this public repo)")
class GoldRoundTripTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.export = json.loads(EXPORT.read_text())
        cls.steps = _load_steps()
        cls.events = _load_events()
        cls.artifact = import_walk(cls.export, cls.steps, cls.events)

    def test_conversion_passes_all_invariants(self):
        self.assertEqual(check(self.artifact), [])

    def test_structure_matches_the_committed_manifest(self):
        # SCHEMA-v5 §6: the gate verifies segmentation and backbone SHAPE, not
        # counts. Any change to turn ownership, edge endpoints, kinds, statuses,
        # or backbone flags diverges from tests/gold_structure.json.
        manifest = json.loads(MANIFEST.read_text())
        self.assertEqual(structural_projection(self.artifact), manifest)

    def test_pristine_projection_state(self):
        # The gold artifact is semantic and complete: degraded must stay off
        # (it is the flag that relaxes citation rules), the range complete,
        # and no warnings present. Pins the importer against regressions in
        # its own escape hatches.
        self.assertFalse(self.artifact.projection.degraded)
        self.assertTrue(self.artifact.session.event_range.complete)
        self.assertEqual(self.artifact.diagnostics.warnings, [])

    def test_full_content_digest(self):
        # The manifest pins shape; this pins everything else (titles, notes,
        # bases) without publishing it: a sha256 over the canonical bytes.
        import hashlib
        digest = hashlib.sha256(dumps(self.artifact).encode()).hexdigest()
        self.assertEqual(digest, DIGEST.read_text().strip())

    def test_counts_match_export(self):
        self.assertEqual(len(self.artifact.nodes), len(self.export["nodes"]))
        self.assertEqual(len(self.artifact.edges), len(self.export["edges"]))
        turns = sum(len(n.turns) for n in self.artifact.nodes)
        self.assertEqual(turns, len(self.export["node_steps"]))

    def test_single_question_root(self):
        # Ruling 13 removed the clerical apply_patch node; the one root is the
        # goal question, and step 15 is an unowned turn.
        roots = self.artifact.roots()
        self.assertEqual(len(roots), 1)
        root = next(n for n in self.artifact.nodes if n.id == roots[0])
        self.assertEqual(root.kind, "question")
        owned_steps = {t.step_id for n in self.artifact.nodes for t in n.turns}
        self.assertNotIn(15, owned_steps)

    def test_serialization_is_deterministic(self):
        once = dumps(self.artifact)
        twice = dumps(loads(once))
        self.assertEqual(once, twice)

    def test_citations_carry_real_event_kinds(self):
        for node in self.artifact.nodes:
            for ref in node.source_refs:
                self.assertEqual(ref.event_kind, self.events[ref.event_id]["kind"])

    def test_generated_at_is_the_range_end_timestamp(self):
        end = self.artifact.session.event_range.end
        self.assertEqual(self.artifact.generated_at, self.events[end]["ts"])


if __name__ == "__main__":
    unittest.main()
