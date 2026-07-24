"""Public importer fixture: a hand-written mini walk export, runnable anywhere.

Exercises the walk-import path end to end without the private gold data:
real event kinds are required, lossy status cells surface as warnings, and
the result passes every invariant.
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from causal_dag import check, dumps, import_walk, loads  # noqa: E402

EXPORT = {
    "schema": "causal-dag.walk-annotations.v2",
    "session_id": "session-mini",
    "exported_at": "2026-07-24T12:00:00+00:00",
    "nodes": [
        {"node_id": "n-goal", "kind": "root", "status": "open",
         "title": "Mini goal", "note": ""},
        {"node_id": "n-try1", "kind": "attempt", "status": "dead_end",
         "title": "Attempt 1", "note": ""},
        {"node_id": "n-try2", "kind": "attempt", "status": "inconclusive",
         "title": "Attempt 2", "note": ""},  # lossy cell -> dead_end + warning
        {"node_id": "n-wrap", "kind": "checkpoint", "status": "verified",
         "title": "Wrap-up", "note": ""},
    ],
    "node_steps": [
        {"node_id": "n-goal", "step_id": 0},
        {"node_id": "n-try1", "step_id": 1},
        {"node_id": "n-try2", "step_id": 2},
        {"node_id": "n-wrap", "step_id": 3},
    ],
    "edges": [
        {"edge_id": "e-1", "from_node": "n-goal", "to_node": "n-try1",
         "class": "structural", "kind": "fork", "backbone": 1, "note": ""},
        {"edge_id": "e-2", "from_node": "n-try1", "to_node": "n-try2",
         "class": "structural", "kind": "repair", "backbone": 1, "note": ""},
        {"edge_id": "e-3", "from_node": "n-goal", "to_node": "n-wrap",
         "class": "structural", "kind": "integration", "backbone": 1, "note": ""},
    ],
}

STEPS = [
    {"step_id": 0, "kind": "user", "event_ids": ["ev-a"]},
    {"step_id": 1, "kind": "round", "event_ids": ["ev-b", "ev-c"]},
    {"step_id": 2, "kind": "round", "event_ids": ["ev-d"]},
    {"step_id": 3, "kind": "assistant", "event_ids": ["ev-e"]},
]

EVENTS = {
    "ev-a": {"kind": "user.message", "ts": "2026-07-01T10:00:00Z"},
    "ev-b": {"kind": "tool.call", "ts": "2026-07-01T10:01:00Z"},
    "ev-c": {"kind": "tool.result", "ts": "2026-07-01T10:01:05Z"},
    "ev-d": {"kind": "tool.call", "ts": "2026-07-01T10:02:00Z"},
    "ev-e": {"kind": "assistant.message", "ts": "2026-07-01T10:03:00Z"},
}


class WalkImportTest(unittest.TestCase):
    def setUp(self):
        self.artifact = import_walk(EXPORT, STEPS, EVENTS)

    def test_passes_all_invariants(self):
        self.assertEqual(check(self.artifact), [])

    def test_event_kinds_come_from_the_stream(self):
        goal = next(n for n in self.artifact.nodes if n.id == "n-goal")
        self.assertEqual(goal.source_refs[0].event_kind, "user.message")

    def test_generated_at_is_the_range_end_timestamp(self):
        self.assertEqual(self.artifact.session.event_range.end, "ev-e")
        self.assertEqual(self.artifact.generated_at, "2026-07-01T10:03:00Z")

    def test_unknown_event_is_an_error_not_a_guess(self):
        with self.assertRaises(ValueError):
            import_walk(EXPORT, STEPS, {"ev-a": EVENTS["ev-a"]})

    def test_lossy_cell_emits_warning(self):
        try2 = next(n for n in self.artifact.nodes if n.id == "n-try2")
        self.assertEqual(try2.status, "dead_end")
        warnings = [w for w in self.artifact.diagnostics.warnings
                    if w.code == "lossy_status_mapping"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("n-try2", warnings[0].node_ids)

    def test_checkpoint_becomes_consolidation_synthesis(self):
        wrap = next(n for n in self.artifact.nodes if n.id == "n-wrap")
        self.assertEqual(wrap.kind, "synthesis")
        self.assertTrue(wrap.metadata["consolidation"])

    def test_serialization_is_deterministic(self):
        self.assertEqual(dumps(self.artifact), dumps(loads(dumps(self.artifact))))


if __name__ == "__main__":
    unittest.main()
