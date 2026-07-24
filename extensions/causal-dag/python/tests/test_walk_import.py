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

    def test_missing_middle_turn_event_is_an_error(self):
        # Every event a turn owns must be known — not just the anchor.
        partial = {eid: meta for eid, meta in EVENTS.items() if eid != "ev-c"}
        with self.assertRaises(ValueError):
            import_walk(EXPORT, STEPS, partial)

    def test_range_follows_stream_order_not_id_order(self):
        # Euler event ids are non-monotonic ULIDs: id sort order can invert
        # stream order. The range must follow the stream.
        export = {
            "schema": "causal-dag.walk-annotations.v2",
            "session_id": "session-inverted",
            "exported_at": "2026-07-24T12:00:00+00:00",
            "nodes": [{"node_id": "n-only", "kind": "root", "status": "open",
                       "title": "Goal", "note": ""}],
            "node_steps": [{"node_id": "n-only", "step_id": 0}],
            "edges": [],
        }
        steps = [{"step_id": 0, "kind": "user", "event_ids": ["ev-z", "ev-a"]}]
        events = {  # stream order: ev-z first — the opposite of id order
            "ev-z": {"kind": "user.message", "ts": "2026-07-01T09:00:00Z"},
            "ev-a": {"kind": "tool.result", "ts": "2026-07-01T09:05:00Z"},
        }
        art = import_walk(export, steps, events)
        self.assertEqual(art.session.event_range.start, "ev-z")
        self.assertEqual(art.session.event_range.end, "ev-a")
        self.assertEqual(art.generated_at, "2026-07-01T09:05:00Z")
        self.assertEqual(art.nodes[0].source_refs[0].event_id, "ev-z")

        # A turn listing its events against stream order is normalized to it.
        steps_reversed = [{"step_id": 0, "kind": "user",
                           "event_ids": ["ev-a", "ev-z"]}]
        art2 = import_walk(export, steps_reversed, events)
        self.assertEqual(art2.nodes[0].turns[0].event_ids, ["ev-z", "ev-a"])

        # Duplicate events in a turn are corrupt input, not a span.
        steps_dup = [{"step_id": 0, "kind": "user",
                      "event_ids": ["ev-z", "ev-z", "ev-a"]}]
        with self.assertRaises(ValueError):
            import_walk(export, steps_dup, events)

    def test_empty_export_produces_valid_empty_artifact(self):
        empty = {"schema": "causal-dag.walk-annotations.v2",
                 "session_id": "session-empty",
                 "exported_at": "2026-07-24T12:00:00+00:00",
                 "nodes": [], "node_steps": [], "edges": []}
        art = import_walk(empty, [], {})
        self.assertEqual(check(art), [])
        self.assertEqual(art.generated_at, "1970-01-01T00:00:00Z")
        self.assertTrue(any(w.code == "empty_forest"
                            for w in art.diagnostics.warnings))

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
