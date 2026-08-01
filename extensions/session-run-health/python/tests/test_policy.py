import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


PYTHON_DIRECTORY = Path(__file__).resolve().parents[1]
REPOSITORY_DIRECTORY = Path(__file__).resolve().parents[4]
SDK_SOURCE = (
    REPOSITORY_DIRECTORY
    / "sdks"
    / "python"
    / "euler-managed-process-sdk"
    / "src"
)
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SDK_SOURCE))
sys.path.insert(0, str(PYTHON_DIRECTORY))

from session_run_health.config import DEFAULT_THRESHOLDS
from session_run_health.policy import (
    OBSERVED_KINDS,
    artifact_document,
    checkpoint_signature,
    collect_signals,
    fold_events,
    make_pending_checkpoint,
    render_context_slot,
)
from session_run_health.state import new_state


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def kinds(signals):
    return {signal["kind"] for signal in signals}


def rooted_state():
    state = new_state()
    state["root_agent"] = "root"
    return state


class ReducerPolicyTests(unittest.TestCase):
    def assess(self, name):
        state = new_state()
        fold_events(state, fixture(name), DEFAULT_THRESHOLDS)
        return state, collect_signals(state, DEFAULT_THRESHOLDS)

    def test_8kq_derived_reducer_does_not_misclassify_stall_as_deliberation(self):
        state, signals = self.assess("8kq-provider-stall.json")

        self.assertEqual(signals, [])
        self.assertEqual(state["last_progress_event_id"], "8kq-06")
        self.assertEqual(state["open_root_calls"], {})
        self.assertEqual(len(state["failure_tracks"]), 2)

    def test_4f1_derived_reducer_triggers_on_behavior_before_round_104(self):
        events = fixture("4f1-failure-churn.json")
        state = new_state()
        fold_events(state, events, DEFAULT_THRESHOLDS)
        signals = collect_signals(state, DEFAULT_THRESHOLDS)

        self.assertLess(len(events), 104)
        self.assertEqual(
            kinds(signals),
            {
                "repeated_failure",
                "edit_thrashing",
                "context_cache_churn",
                "active_run_input",
            },
        )

    def test_productive_long_run_remains_quiet(self):
        _state, signals = self.assess("productive-long-run.json")
        self.assertEqual(signals, [])

    def test_repeated_malformed_patches_use_a_content_free_failure_class(self):
        state, signals = self.assess("repeated-malformed-patches.json")

        self.assertEqual(kinds(signals), {"repeated_failure"})
        pending = make_pending_checkpoint(
            state,
            signals,
            "patch-04",
            DEFAULT_THRESHOLDS,
        )
        rendered = json.dumps(artifact_document(pending), sort_keys=True)
        slot = render_context_slot(pending)
        for private_text in (
            "first raw error",
            "different raw wording",
            "third raw error",
            "apply_patch",
        ):
            self.assertNotIn(private_text, rendered)
            self.assertNotIn(private_text, slot)

    def test_checkpoint_signature_contains_only_public_checkpoint_fields(self):
        public = {
            "kind": "repeated_failure",
            "count": 3,
            "event_ids": ["event-1", "event-2", "event-3"],
        }
        first = {**public, "_entity": "a" * 64, "_sequence": 1}
        private_guess = {**public, "_entity": "b" * 64, "_sequence": 99}

        first_signature = checkpoint_signature(
            1, "cutoff-1", [first], DEFAULT_THRESHOLDS
        )
        guessed_signature = checkpoint_signature(
            1, "cutoff-1", [private_guess], DEFAULT_THRESHOLDS
        )

        self.assertEqual(first_signature, guessed_signature)
        serialized = json.dumps(artifact_document({
            "revision": 1,
            "signature": first_signature,
            "cutoff_event_id": "cutoff-1",
            "signals": [first],
            "thresholds": DEFAULT_THRESHOLDS,
        }))
        self.assertNotIn("a" * 64, serialized)
        self.assertNotIn("_sequence", serialized)

    def test_signal_selection_caps_before_marking_and_leaves_dropped_tracks_eligible(self):
        state = rooted_state()
        events = []
        sequence = 0
        for family in range(6):
            for recurrence in range(3):
                sequence += 1
                events.append(
                    {
                        "id": f"failure-{sequence}",
                        "ts": f"2026-07-30T00:{sequence:02d}:00.000Z",
                        "agent": "root",
                        "kind": "error",
                        "payload": {
                            "source": "provider",
                            "category": f"class-{family}",
                            "timeout_stage": "semantic_idle",
                            "message": f"private-{recurrence}",
                        },
                    }
                )
        fold_events(state, events, DEFAULT_THRESHOLDS)

        first = collect_signals(state, DEFAULT_THRESHOLDS)
        second = collect_signals(state, DEFAULT_THRESHOLDS)

        self.assertEqual(len(first), 5)
        self.assertEqual(len(second), 1)
        self.assertEqual(
            {signal["_entity"] for signal in first + second},
            set(state["failure_tracks"]),
        )

    def test_track_eviction_keeps_the_most_recent_observations(self):
        state = rooted_state()
        events = [
            {
                "id": f"edit-{index}",
                "ts": f"2026-07-30T00:0{index}:00.000Z",
                "agent": "root",
                "kind": "file.change",
                "payload": {"path": f"private-{index}.rs"},
            }
            for index in range(4)
        ]

        with patch("session_run_health.policy.MAX_TRACKED_FILES", 2):
            fold_events(state, events, DEFAULT_THRESHOLDS)

        self.assertEqual(
            {track["event_ids"][-1] for track in state["edit_tracks"].values()},
            {"edit-2", "edit-3"},
        )

    def test_unrelated_shell_failures_remain_operation_specific(self):
        state = rooted_state()
        events = []
        commands = ("rg alpha src", "rg beta tests", "echo test")
        for index, command in enumerate(commands):
            events.extend(
                self._tool_attempt(
                    f"shell-{index}",
                    "run_shell",
                    {"command": command},
                    ok=False,
                    exit_code=1,
                )
            )

        fold_events(state, events, DEFAULT_THRESHOLDS)

        self.assertEqual(len(state["failure_tracks"]), 3)
        self.assertTrue(
            all(track["count"] == 1 for track in state["failure_tracks"].values())
        )
        self.assertEqual(collect_signals(state, DEFAULT_THRESHOLDS), [])

    def test_unstructured_provider_and_session_errors_do_not_aggregate(self):
        state = rooted_state()
        events = []
        for index, source in enumerate(("provider", "session", "provider")):
            events.append(
                {
                    "id": f"unstructured-{index}",
                    "ts": f"2026-07-30T00:0{index}:00.000Z",
                    "agent": "root",
                    "kind": "error",
                    "payload": {
                        "source": source,
                        "message": f"private-{index}",
                    },
                }
            )
        events.append(
            {
                "id": "structured-1",
                "ts": "2026-07-30T00:04:00.000Z",
                "agent": "root",
                "kind": "error",
                "payload": {
                    "source": "provider",
                    "category": "transport",
                    "message": "private-structured",
                },
            }
        )

        fold_events(state, events, DEFAULT_THRESHOLDS)

        self.assertEqual(len(state["failure_tracks"]), 1)
        self.assertEqual(next(iter(state["failure_tracks"].values()))["count"], 1)
        self.assertEqual(collect_signals(state, DEFAULT_THRESHOLDS), [])

    def test_cancellation_recovery_and_unrelated_success_are_neutral(self):
        state = rooted_state()
        events = []
        for index in range(2):
            events.extend(
                self._tool_attempt(
                    f"check-failure-{index}",
                    "run_shell",
                    {"command": "cargo check --workspace"},
                    ok=False,
                    exit_code=101,
                )
            )
        events.extend(
            self._tool_attempt(
                "check-cancelled",
                "run_shell",
                {"command": "cargo check -p focused"},
                ok=False,
                cancelled=True,
            )
        )
        events.extend(
            self._tool_attempt(
                "check-recovered",
                "run_shell",
                {"command": "cargo check -p focused"},
                ok=False,
                recovery_closure=True,
            )
        )
        events.extend(
            self._tool_attempt(
                "test-success",
                "run_shell",
                {"command": "cargo test -p focused"},
                ok=True,
                exit_code=0,
            )
        )
        fold_events(state, events, DEFAULT_THRESHOLDS)

        self.assertEqual(len(state["failure_tracks"]), 1)
        self.assertEqual(next(iter(state["failure_tracks"].values()))["count"], 2)

        fold_events(
            state,
            self._tool_attempt(
                "check-success",
                "run_shell",
                {"command": "cargo check -p focused"},
                ok=True,
                exit_code=0,
            ),
            DEFAULT_THRESHOLDS,
        )
        self.assertEqual(state["failure_tracks"], {})

    def test_incidental_test_words_and_compound_shell_are_not_validation(self):
        for command in (
            "rg test src",
            "echo test",
            "git status -- tests",
            "echo ready && cargo test",
            "cargo nextest list",
            "cargo fmt --all",
        ):
            with self.subTest(command=command):
                state = rooted_state()
                edits = [
                    {
                        "id": f"edit-{index}",
                        "ts": f"2026-07-30T00:0{index}:00.000Z",
                        "agent": "root",
                        "kind": "file.change",
                        "payload": {"path": "private/path.rs"},
                    }
                    for index in range(5)
                ]
                fold_events(state, edits, DEFAULT_THRESHOLDS)
                fold_events(
                    state,
                    self._tool_attempt(
                        "incidental",
                        "run_shell",
                        {"command": command},
                        ok=True,
                        exit_code=0,
                    ),
                    DEFAULT_THRESHOLDS,
                )
                self.assertIn(
                    "edit_thrashing",
                    kinds(collect_signals(state, DEFAULT_THRESHOLDS)),
                )

    def test_shell_validation_without_an_exit_status_does_not_clear_edits(self):
        state = rooted_state()
        edits = [
            {
                "id": f"edit-{index}",
                "ts": f"2026-07-30T00:0{index}:00.000Z",
                "agent": "root",
                "kind": "file.change",
                "payload": {"path": "private/path.rs"},
            }
            for index in range(5)
        ]
        fold_events(state, edits, DEFAULT_THRESHOLDS)
        fold_events(
            state,
            self._tool_attempt(
                "missing-exit",
                "run_shell",
                {"command": "cargo test -p focused"},
                ok=False,
            ),
            DEFAULT_THRESHOLDS,
        )

        self.assertIn(
            "edit_thrashing",
            kinds(collect_signals(state, DEFAULT_THRESHOLDS)),
        )

    def test_many_round_fixture_triggers_on_behavior_not_round_count(self):
        events = self._many_round_events(15)
        state = new_state()
        fold_events(state, events, DEFAULT_THRESHOLDS)
        signals = collect_signals(state, DEFAULT_THRESHOLDS)

        self.assertLess(len(events), 104)
        self.assertEqual(sum(event["kind"] == "model.result" for event in events), 15)
        self.assertTrue(all(len(event["id"]) == 26 for event in events))
        self.assertIn("repeated_failure", kinds(signals))
        self.assertIn("edit_thrashing", kinds(signals))
        self.assertIn("context_cache_churn", kinds(signals))
        self.assertIn("active_run_input", kinds(signals))

    @staticmethod
    def _tool_attempt(call_id, name, input_value, ok, **result_fields):
        suffix = call_id.replace("_", "-")
        return [
            {
                "id": f"{suffix}-call",
                "ts": "2026-07-30T00:00:00.000Z",
                "agent": "root",
                "kind": "tool.call",
                "payload": {"id": call_id, "name": name, "input": input_value},
            },
            {
                "id": f"{suffix}-result",
                "ts": "2026-07-30T00:00:01.000Z",
                "agent": "root",
                "kind": "tool.result",
                "payload": {"id": call_id, "name": name, "ok": ok, **result_fields},
            },
        ]

    @staticmethod
    def _many_round_events(rounds):
        next_identity = 0

        def identity():
            nonlocal next_identity
            next_identity += 1
            return f"01K{next_identity:023d}"

        run_id = f"01K{999:023d}"
        events = []

        def append(kind, payload, *, run=run_id):
            event_id = identity()
            events.append(
                {
                    "v": 1,
                    "id": event_id,
                    "ts": (
                        f"2026-07-30T{len(events) // 60:02d}:"
                        f"{len(events) % 60:02d}:00.000Z"
                    ),
                    "session": f"01K{998:023d}",
                    "agent": "root",
                    "run": run,
                    "parent": events[-1]["id"] if events else None,
                    "kind": kind,
                    "payload": payload,
                    "blobs": {},
                }
            )

        append("run.started", {"trigger": "direct"})
        append("user.message", {"content": "omitted"})
        for index in range(rounds):
            append("model.call", {"provider": "fixture", "model": "fixture"})
            patch_call_id = f"patch-call-{index}" if index < 3 else None
            edit_call_id = f"edit-call-{index}"
            tool_calls = []
            if patch_call_id is not None:
                tool_calls.append(
                    {
                        "id": patch_call_id,
                        "name": "apply_patch",
                        "input": {"patch": f"omitted-{index}"},
                    }
                )
            tool_calls.append(
                {
                    "id": edit_call_id,
                    "name": "edit_file",
                    "input": {"path": "src/session.rs", "content": "omitted"},
                }
            )
            append(
                "model.result",
                {
                    "provider": "fixture",
                    "model": "fixture",
                    "content": "omitted",
                    "tool_calls": tool_calls,
                    "stop_reason": "tool",
                    "usage": {
                        "input_tokens": 8000 * (2**index),
                        "output_tokens": 100,
                        "cached_tokens": 0,
                    },
                },
            )
            if patch_call_id is not None:
                append(
                    "tool.call",
                    {
                        "id": patch_call_id,
                        "name": "apply_patch",
                        "input": {"patch": f"omitted-{index}"},
                    },
                )
                append(
                    "tool.result",
                    {
                        "id": patch_call_id,
                        "name": "apply_patch",
                        "ok": False,
                        "error": "omitted",
                    },
                )
            append(
                "tool.call",
                {
                    "id": edit_call_id,
                    "name": "edit_file",
                    "input": {"path": "src/session.rs", "content": "omitted"},
                },
            )
            append(
                "tool.result",
                {
                    "id": edit_call_id,
                    "name": "edit_file",
                    "ok": True,
                    "output": "omitted",
                },
            )
            append(
                "file.change",
                {
                    "tool_call_id": edit_call_id,
                    "origin": "edit_file",
                    "action": "modify",
                    "path": "src/session.rs",
                    "old_path": None,
                },
            )
            if index == 4:
                append(
                    "queue.enqueued",
                    {
                        "queue_id": f"01K{997:023d}",
                        "mode": "steering",
                        "position": "back",
                        "content": "omitted",
                        "source_run_id": run_id,
                    },
                )
                append("queue.delivered", {"queue_id": f"01K{997:023d}"})
                append("user.message", {"content": "omitted"})
        return events

    def test_missing_optional_cache_telemetry_does_not_guess(self):
        _state, signals = self.assess("missing-optional-telemetry.json")
        self.assertNotIn("context_cache_churn", kinds(signals))

    def test_context_samples_reset_at_model_target_boundaries(self):
        state = new_state()

        def result(event_id, provider, model, input_tokens):
            return {
                "id": event_id,
                "ts": "2026-07-30T00:00:00.000Z",
                "agent": "root",
                "kind": "model.result",
                "payload": {
                    "provider": provider,
                    "model": model,
                    "usage": {
                        "input_tokens": input_tokens,
                        "cached_tokens": 0,
                    },
                },
            }

        fold_events(
            state,
            [
                {
                    "id": "run-start",
                    "ts": "2026-07-30T00:00:00.000Z",
                    "agent": "root",
                    "run": "run-root",
                    "kind": "run.started",
                    "payload": {"trigger": "direct"},
                },
                result("sample-1", "provider-a", "model-a", 8_000),
                result("sample-2", "provider-a", "model-a", 12_000),
                result("sample-3", "provider-a", "model-a", 18_000),
                {
                    "id": "switch-1",
                    "ts": "2026-07-30T00:00:00.000Z",
                    "agent": "root",
                    "kind": "model.switched",
                    "payload": {
                        "from_provider": "provider-a",
                        "from_model": "model-a",
                        "to_provider": "provider-b",
                        "to_model": "model-b",
                        "reason": "user",
                    },
                },
                result("sample-4", "provider-b", "model-b", 8_000),
            ],
            DEFAULT_THRESHOLDS,
        )

        self.assertIn("model.switched", OBSERVED_KINDS)
        self.assertEqual(len(state["usage_samples"]), 1)
        self.assertNotIn(
            "context_cache_churn",
            kinds(collect_signals(state, DEFAULT_THRESHOLDS)),
        )
        serialized = json.dumps(state, sort_keys=True)
        self.assertNotIn("provider-a", serialized)
        self.assertNotIn("model-b", serialized)

        fold_events(
            state,
            [
                result("sample-5", "provider-b", "model-b", 12_000),
                result("sample-6", "provider-b", "model-b", 18_000),
                result("sample-7", "provider-b", "model-b", 27_000),
            ],
            DEFAULT_THRESHOLDS,
        )
        self.assertIn(
            "context_cache_churn",
            kinds(collect_signals(state, DEFAULT_THRESHOLDS)),
        )

        fold_events(
            state,
            [result("sample-8", "provider-c", "model-c", 30_000)],
            DEFAULT_THRESHOLDS,
        )
        self.assertEqual(len(state["usage_samples"]), 1)
        self.assertFalse(state["context_alerted"])

    def test_model_reasoning_is_neither_queried_nor_interpreted(self):
        self.assertNotIn("model.reasoning", OBSERVED_KINDS)
        self.assertNotIn("model.delta", OBSERVED_KINDS)
        state = new_state()
        fold_events(
            state,
            [
                {
                    "id": "reasoning-1",
                    "ts": "2026-07-30T00:00:00.000Z",
                    "agent": "root",
                    "kind": "model.reasoning",
                    "payload": {"content": "must never be inspected"},
                }
            ],
            DEFAULT_THRESHOLDS,
        )
        self.assertEqual(state, new_state())

    def test_no_progress_uses_event_time_but_waiting_and_open_calls_suppress_it(self):
        progress = {
            "id": "progress-1",
            "ts": "2026-07-30T00:00:00.000Z",
            "agent": "root",
            "kind": "user.message",
            "payload": {"content": "omitted"},
        }
        later_control = {
            "id": "control-1",
            "ts": "2026-07-30T00:06:00.000Z",
            "agent": "root",
            "kind": "canvas.candidate.discarded",
            "payload": {"reason": "omitted"},
        }

        state = rooted_state()
        fold_events(state, [progress, later_control], DEFAULT_THRESHOLDS)
        self.assertEqual(
            kinds(collect_signals(state, DEFAULT_THRESHOLDS)),
            {"no_meaningful_progress"},
        )

        for blocker in ("permission", "model"):
            with self.subTest(blocker=blocker):
                state = rooted_state()
                intervening = (
                    {
                        "id": "permission-1",
                        "ts": "2026-07-30T00:01:00.000Z",
                        "agent": "root",
                        "kind": "permission.prompt",
                        "payload": {"capability": "fs-write"},
                    }
                    if blocker == "permission"
                    else {
                        "id": "call-1",
                        "ts": "2026-07-30T00:01:00.000Z",
                        "agent": "root",
                        "kind": "model.call",
                        "payload": {"provider": "fixture", "model": "fixture"},
                    }
                )
                fold_events(
                    state,
                    [progress, intervening, later_control],
                    DEFAULT_THRESHOLDS,
                )
                self.assertNotIn(
                    "no_meaningful_progress",
                    kinds(collect_signals(state, DEFAULT_THRESHOLDS)),
                )

    def test_compaction_terminals_do_not_reset_foreground_progress(self):
        for terminal_kind in ("model.result", "error"):
            with self.subTest(terminal_kind=terminal_kind):
                state = rooted_state()
                fold_events(
                    state,
                    [
                        {
                            "id": "foreground-progress",
                            "ts": "2026-07-30T00:00:00.000Z",
                            "agent": "root",
                            "kind": "user.message",
                            "payload": {"content": "omitted"},
                        },
                        {
                            "id": "compaction-call",
                            "ts": "2026-07-30T00:01:00.000Z",
                            "agent": "root",
                            "kind": "model.call",
                            "payload": {
                                "provider": "fixture",
                                "model": "fixture",
                                "purpose": "compaction",
                            },
                        },
                        {
                            "id": "compaction-terminal",
                            "ts": "2026-07-30T00:06:00.000Z",
                            "agent": "root",
                            "kind": terminal_kind,
                            "payload": {
                                "provider": "fixture",
                                "model": "fixture",
                                "purpose": "compaction",
                                "source": "provider",
                                "category": "transport",
                            },
                        },
                    ],
                    DEFAULT_THRESHOLDS,
                )
                signals = collect_signals(state, DEFAULT_THRESHOLDS)

                self.assertEqual(state["last_progress_event_id"], "foreground-progress")
                self.assertEqual(kinds(signals), {"no_meaningful_progress"})
                self.assertEqual(state["failure_tracks"], {})

    def test_completed_validation_resets_same_file_edit_recurrence(self):
        events = []
        for index in range(4):
            events.append(
                {
                    "id": f"edit-before-{index}",
                    "ts": f"2026-07-30T00:0{index}:00.000Z",
                    "agent": "root",
                    "kind": "file.change",
                    "payload": {"path": "private/path.rs"},
                }
            )
        events.extend(
            [
                {
                    "id": "validation-call",
                    "ts": "2026-07-30T00:05:00.000Z",
                    "agent": "root",
                    "kind": "tool.call",
                    "payload": {
                        "id": "validation",
                        "name": "run_shell",
                        "input": {"command": "cargo test -p focused"},
                    },
                },
                {
                    "id": "validation-result",
                    "ts": "2026-07-30T00:06:00.000Z",
                    "agent": "root",
                    "kind": "tool.result",
                    "payload": {
                        "id": "validation",
                        "name": "run_shell",
                        "ok": False,
                        "exit_code": 101,
                    },
                },
                {
                    "id": "edit-after",
                    "ts": "2026-07-30T00:07:00.000Z",
                    "agent": "root",
                    "kind": "file.change",
                    "payload": {"path": "private/path.rs"},
                },
            ]
        )
        state = new_state()
        fold_events(state, events, DEFAULT_THRESHOLDS)
        self.assertNotIn("edit_thrashing", kinds(collect_signals(state, DEFAULT_THRESHOLDS)))

    def test_env_prefixed_nextest_resets_same_file_edit_recurrence(self):
        state = rooted_state()
        events = [
            {
                "id": f"edit-before-{index}",
                "ts": f"2026-07-30T00:0{index}:00.000Z",
                "agent": "root",
                "kind": "file.change",
                "payload": {"path": "private/path.rs"},
            }
            for index in range(4)
        ]
        events.extend(
            [
                {
                    "id": "nextest-call",
                    "ts": "2026-07-30T00:05:00.000Z",
                    "agent": "root",
                    "kind": "tool.call",
                    "payload": {
                        "id": "nextest",
                        "name": "run_shell",
                        "input": {
                            "command": (
                                "env -u EULER_HOME cargo nextest run --workspace"
                            )
                        },
                    },
                },
                {
                    "id": "nextest-result",
                    "ts": "2026-07-30T00:06:00.000Z",
                    "agent": "root",
                    "kind": "tool.result",
                    "payload": {
                        "id": "nextest",
                        "name": "run_shell",
                        "ok": True,
                        "exit_code": 0,
                    },
                },
                {
                    "id": "edit-after",
                    "ts": "2026-07-30T00:07:00.000Z",
                    "agent": "root",
                    "kind": "file.change",
                    "payload": {"path": "private/path.rs"},
                },
            ]
        )

        fold_events(state, events, DEFAULT_THRESHOLDS)

        self.assertNotIn("edit_thrashing", kinds(collect_signals(state, DEFAULT_THRESHOLDS)))

    def test_child_agent_failures_do_not_poison_root_run_health(self):
        events = [
            {
                "id": "root-start",
                "ts": "2026-07-30T00:00:00.000Z",
                "agent": "root",
                "run": "run-root",
                "kind": "run.started",
                "payload": {"trigger": "direct"},
            }
        ]
        for index in range(3):
            events.append(
                {
                    "id": f"child-failure-{index}",
                    "ts": f"2026-07-30T00:0{index + 1}:00.000Z",
                    "agent": "child",
                    "run": "run-root",
                    "kind": "tool.result",
                    "payload": {
                        "id": f"child-call-{index}",
                        "name": "apply_patch",
                        "ok": False,
                        "error": "private child output",
                    },
                }
            )
        state = new_state()
        fold_events(state, events, DEFAULT_THRESHOLDS)

        self.assertEqual(state["root_agent"], "root")
        self.assertEqual(collect_signals(state, DEFAULT_THRESHOLDS), [])

    def test_history_before_legacy_root_ownership_is_discarded(self):
        state = new_state()
        events = []
        for index in range(3):
            events.extend(
                [
                    {
                        "id": f"prior-error-{index}",
                        "ts": f"2026-07-30T00:0{index}:00.000Z",
                        "agent": "prior-child",
                        "kind": "error",
                        "payload": {
                            "source": "provider",
                            "category": "transport",
                        },
                    },
                    {
                        "id": f"prior-edit-{index}",
                        "ts": f"2026-07-30T00:0{index}:01.000Z",
                        "agent": "unknown-prior-root",
                        "kind": "file.change",
                        "payload": {"path": "private/prior.rs"},
                    },
                ]
            )
        events.extend(
            [
                {
                    "id": "root-start",
                    "ts": "2026-07-30T00:04:00.000Z",
                    "agent": "root",
                    "run": "run-root",
                    "kind": "run.started",
                    "payload": {"trigger": "direct"},
                },
                {
                    "id": "root-user",
                    "ts": "2026-07-30T00:04:01.000Z",
                    "agent": "root",
                    "run": "run-root",
                    "kind": "user.message",
                    "payload": {"content": "omitted"},
                },
                {
                    "id": "duplicate-session-start",
                    "ts": "2026-07-30T00:04:01.500Z",
                    "agent": "prior-child",
                    "kind": "session.start",
                    "payload": {
                        "provider": "private-provider",
                        "model": "private-model",
                    },
                },
                {
                    "id": "root-error",
                    "ts": "2026-07-30T00:04:02.000Z",
                    "agent": "root",
                    "run": "run-root",
                    "kind": "error",
                    "payload": {
                        "source": "provider",
                        "category": "transport",
                    },
                },
            ]
        )

        fold_events(state, events, DEFAULT_THRESHOLDS)

        self.assertEqual(state["root_agent"], "root")
        self.assertEqual(state["observed_sequence"], 3)
        self.assertEqual(state["edit_tracks"], {})
        self.assertEqual(len(state["failure_tracks"]), 1)
        self.assertEqual(next(iter(state["failure_tracks"].values()))["count"], 1)
        self.assertEqual(collect_signals(state, DEFAULT_THRESHOLDS), [])

    def test_cancelled_steering_never_opens_a_scope_checkpoint(self):
        state = new_state()
        fold_events(
            state,
            [
                {
                    "id": "run-start",
                    "ts": "2026-07-30T00:00:00.000Z",
                    "agent": "root",
                    "run": "run-root",
                    "kind": "run.started",
                    "payload": {"trigger": "direct"},
                },
                {
                    "id": "user-start",
                    "ts": "2026-07-30T00:00:01.000Z",
                    "agent": "root",
                    "run": "run-root",
                    "kind": "user.message",
                    "payload": {"content": "omitted"},
                },
                {
                    "id": "steer-1",
                    "ts": "2026-07-30T00:01:00.000Z",
                    "agent": "root",
                    "run": "run-root",
                    "kind": "queue.enqueued",
                    "payload": {
                        "queue_id": "queue-1",
                        "mode": "steering",
                        "position": "back",
                        "content": "omitted",
                    },
                },
                {
                    "id": "steer-1-cancelled",
                    "ts": "2026-07-30T00:01:01.000Z",
                    "agent": "root",
                    "run": "run-root",
                    "kind": "queue.cancelled",
                    "payload": {"queue_id": "queue-1", "reason": "user"},
                },
            ],
            DEFAULT_THRESHOLDS,
        )

        self.assertNotIn("queue.enqueued", OBSERVED_KINDS)
        self.assertNotIn("queue.cancelled", OBSERVED_KINDS)
        self.assertEqual(collect_signals(state, DEFAULT_THRESHOLDS), [])

    def test_replaced_steering_counts_only_after_durable_user_message_delivery(self):
        state = new_state()
        fold_events(
            state,
            [
                {
                    "id": "run-start",
                    "ts": "2026-07-30T00:00:00.000Z",
                    "agent": "root",
                    "run": "run-root",
                    "kind": "run.started",
                    "payload": {"trigger": "direct"},
                },
                {
                    "id": "user-start",
                    "ts": "2026-07-30T00:00:01.000Z",
                    "agent": "root",
                    "run": "run-root",
                    "kind": "user.message",
                    "payload": {"content": "omitted"},
                },
                {
                    "id": "steer-original",
                    "ts": "2026-07-30T00:01:00.000Z",
                    "agent": "root",
                    "run": "run-root",
                    "kind": "queue.enqueued",
                    "payload": {
                        "queue_id": "queue-original",
                        "mode": "steering",
                        "position": "back",
                        "content": "omitted",
                    },
                },
                {
                    "id": "steer-replaced",
                    "ts": "2026-07-30T00:01:01.000Z",
                    "agent": "root",
                    "run": "run-root",
                    "kind": "queue.replaced",
                    "payload": {
                        "queue_id": "queue-original",
                        "replacement_queue_id": "queue-replacement",
                        "mode": "steering",
                        "content": "omitted-replacement",
                    },
                },
                {
                    "id": "steer-delivered",
                    "ts": "2026-07-30T00:01:02.000Z",
                    "agent": "root",
                    "run": "run-root",
                    "kind": "queue.delivered",
                    "payload": {"queue_id": "queue-replacement"},
                },
            ],
            DEFAULT_THRESHOLDS,
        )
        self.assertEqual(collect_signals(state, DEFAULT_THRESHOLDS), [])

        fold_events(
            state,
            [
                {
                    "id": "steer-user-message",
                    "ts": "2026-07-30T00:02:00.000Z",
                    "agent": "root",
                    "run": "run-root",
                    "kind": "user.message",
                    "payload": {"content": "omitted"},
                }
            ],
            DEFAULT_THRESHOLDS,
        )
        first = collect_signals(state, DEFAULT_THRESHOLDS)
        self.assertEqual(kinds(first), {"active_run_input"})
        self.assertEqual(first[0]["count"], 1)
        self.assertEqual(first[0]["event_ids"], ["steer-user-message"])

        fold_events(
            state,
            [
                {
                    "id": "progress-after-steer",
                    "ts": "2026-07-30T00:03:00.000Z",
                    "agent": "root",
                    "run": "run-root",
                    "kind": "model.result",
                    "payload": {
                        "provider": "fixture",
                        "model": "fixture",
                        "content": "omitted",
                        "tool_calls": [],
                        "stop_reason": "stop",
                    },
                },
                {
                    "id": "steer-user-message-2",
                    "ts": "2026-07-30T00:04:00.000Z",
                    "agent": "root",
                    "run": "run-root",
                    "kind": "user.message",
                    "payload": {"content": "omitted"},
                }
            ],
            DEFAULT_THRESHOLDS,
        )
        signals = collect_signals(state, DEFAULT_THRESHOLDS)
        self.assertEqual(kinds(signals), {"active_run_input"})
        self.assertEqual(signals[0]["count"], 2)

    def test_queue_bookkeeping_is_not_queried_or_meaningful_progress(self):
        state = new_state()
        fold_events(
            state,
            [
                {
                    "id": "run-start",
                    "ts": "2026-07-30T00:00:00.000Z",
                    "agent": "root",
                    "run": "run-root",
                    "kind": "run.started",
                    "payload": {"trigger": "direct"},
                },
                {
                    "id": "foreground-progress",
                    "ts": "2026-07-30T00:00:01.000Z",
                    "agent": "root",
                    "run": "run-root",
                    "kind": "user.message",
                    "payload": {"content": "omitted"},
                },
                {
                    "id": "steering-admitted",
                    "ts": "2026-07-30T00:01:00.000Z",
                    "agent": "root",
                    "run": "run-root",
                    "kind": "queue.enqueued",
                    "payload": {
                        "queue_id": "queue-1",
                        "mode": "steering",
                        "position": "back",
                        "content": "omitted",
                    },
                },
                {
                    "id": "steering-replaced",
                    "ts": "2026-07-30T00:06:01.000Z",
                    "agent": "root",
                    "run": "run-root",
                    "kind": "queue.replaced",
                    "payload": {
                        "queue_id": "queue-1",
                        "replacement_queue_id": "queue-2",
                        "mode": "steering",
                        "content": "omitted",
                    },
                },
                {
                    "id": "later-control",
                    "ts": "2026-07-30T00:06:02.000Z",
                    "agent": "root",
                    "run": "run-root",
                    "kind": "canvas.candidate.discarded",
                    "payload": {"reason": "omitted"},
                },
            ],
            DEFAULT_THRESHOLDS,
        )

        signals = collect_signals(state, DEFAULT_THRESHOLDS)

        self.assertEqual(state["last_progress_event_id"], "foreground-progress")
        self.assertEqual(kinds(signals), {"no_meaningful_progress"})
        for kind in ("queue.enqueued", "queue.replaced", "queue.delivered"):
            self.assertNotIn(kind, OBSERVED_KINDS)


if __name__ == "__main__":
    unittest.main()
