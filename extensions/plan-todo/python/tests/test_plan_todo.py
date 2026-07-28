import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
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
sys.path.insert(0, str(SDK_SOURCE))
sys.path.insert(0, str(PYTHON_DIRECTORY))

from euler_managed_process_sdk import HostError
from plan_todo.commands import (
    MAX_MODEL_ERROR_BYTES,
    _bounded_model_error,
    handle_idle,
    handle_update_plan,
)
from plan_todo.plan import (
    DurablePlanError,
    MAX_CONTEXT_SLOT_BYTES,
    MAX_ITEMS,
    PlanValidationError,
    load_plan,
    render_context_slot,
    replace_plan,
)


def replacement(
    statuses=("pending",),
    *,
    plan_status="active",
    explanation=None,
):
    value = {
        "plan_status": plan_status,
        "plan": [
            {"step": f"Step {index}", "status": status}
            for index, status in enumerate(statuses, start=1)
        ],
    }
    if explanation is not None:
        value["explanation"] = explanation
    return value


class FakeHost:
    def __init__(
        self,
        state_directory,
        *,
        reject_slots=False,
        reject_presentations=False,
    ):
        self._state_directory = state_directory
        self._reject_slots = reject_slots
        self._reject_presentations = reject_presentations
        self.slots = []
        self.presentations = []

    def state_dir(self):
        return self._state_directory

    def update_context_slot(self, slot, content):
        if self._reject_slots:
            raise HostError("context slot unavailable")
        self.slots.append((slot, content))

    def update_plan_presentation(self, **presentation):
        if self._reject_presentations:
            raise HostError("plan presentation unavailable")
        self.presentations.append(presentation)


class PlanStateTests(unittest.TestCase):
    def test_full_replacements_are_durable_and_revisioned(self):
        with TemporaryDirectory() as directory:
            first = replace_plan(directory, replacement(("in_progress", "pending")))
            second = replace_plan(directory, replacement(("completed", "in_progress")))
            loaded = load_plan(directory)

        self.assertEqual(first.revision, 1)
        self.assertEqual(second.revision, 2)
        self.assertEqual(loaded, second)

    def test_state_is_scoped_by_the_host_directory(self):
        with TemporaryDirectory() as first_directory, TemporaryDirectory() as second_directory:
            replace_plan(first_directory, replacement(("pending",)))

            self.assertIsNotNone(load_plan(first_directory))
            self.assertIsNone(load_plan(second_directory))

    def test_rejects_open_or_malformed_tool_input(self):
        invalid_values = [
            {"plan_status": "active", "plan": [], "extra": True},
            replacement(("pending", "in_progress", "in_progress")),
            replacement(("unknown",)),
            replacement(("pending",), plan_status="paused"),
            replacement(("pending",), plan_status="blocked"),
            replacement(("pending",), plan_status="waiting", explanation=" "),
            {
                "plan_status": "active",
                "plan": [{"step": "one", "status": "pending", "extra": 1}],
            },
            {
                "plan_status": "active",
                "plan": [{"step": "line\nbreak", "status": "pending"}],
            },
            {
                "plan_status": "active",
                "plan": [{"step": "bidi\u2067spoof", "status": "pending"}],
            },
            {
                "plan_status": "active",
                "plan": [{"step": "é" * 257, "status": "pending"}],
            },
            replacement(tuple("pending" for _ in range(MAX_ITEMS + 1))),
        ]
        with TemporaryDirectory() as directory:
            for value in invalid_values:
                with self.subTest(value=value):
                    with self.assertRaises(PlanValidationError):
                        replace_plan(directory, value)

    def test_text_validation_matches_host_control_and_format_policy(self):
        unsafe_characters = (
            "\n",
            "\u0085",
            "\u0600",
            "\u2067",
            "\u2028",
            "\u2029",
            "\U00013430",
            "\U000e0001",
        )
        with TemporaryDirectory() as directory:
            for character in unsafe_characters:
                for step in (f"before{character}after", f"trimmed{character}"):
                    with self.subTest(codepoint=hex(ord(character)), step=step):
                        with self.assertRaises(PlanValidationError):
                            replace_plan(
                                directory,
                                {
                                    "plan_status": "active",
                                    "plan": [{"step": step, "status": "pending"}],
                                },
                            )

    def test_rejects_non_utf8_unicode_scalar_input(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                PlanValidationError,
                "not valid Unicode text",
            ):
                replace_plan(
                    directory,
                    {
                        "plan_status": "active",
                        "plan": [{"step": "surrogate\ud800", "status": "pending"}],
                    },
                )

    def test_rejects_unknown_and_duplicate_durable_fields(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "revision": 1,
                        "plan_status": "active",
                        "explanation": "",
                        "plan": [{"step": "one", "status": "pending"}],
                        "unknown": True,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(PlanValidationError):
                load_plan(directory)

    def test_schema_version_requires_exact_non_boolean_integer(self):
        invalid_versions = (True, False, 1.0, "1", 2, None)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            for schema_version in invalid_versions:
                with self.subTest(schema_version=schema_version):
                    path.write_text(
                        json.dumps(
                            {
                                "schema_version": schema_version,
                                "revision": 1,
                                "plan_status": "active",
                                "explanation": "",
                                "plan": [{"step": "one", "status": "pending"}],
                            }
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        DurablePlanError,
                        "unsupported schema version",
                    ):
                        load_plan(directory)

    def test_rejects_invalid_utf8_and_non_object_state(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_bytes(b"\xff")
            with self.assertRaisesRegex(PlanValidationError, "valid UTF-8"):
                load_plan(directory)

            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(DurablePlanError, "must be an object"):
                load_plan(directory)

            path.write_text(
                '{"schema_version":1,"revision":1,"revision":2,'
                '"plan_status":"active","explanation":"",'
                '"plan":[{"step":"one","status":"pending"}]}',
                encoding="utf-8",
            )
            with self.assertRaises(PlanValidationError):
                load_plan(directory)

    def test_rejects_non_regular_durable_state(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.mkdir()
            with self.assertRaisesRegex(
                PlanValidationError,
                "regular file",
            ):
                load_plan(directory)

    def test_failed_atomic_replace_preserves_previous_state(self):
        with TemporaryDirectory() as directory:
            original = replace_plan(directory, replacement(("pending",)))
            with patch(
                "plan_todo.plan.os.replace",
                side_effect=OSError("simulated rename failure"),
            ):
                with self.assertRaises(OSError):
                    replace_plan(directory, replacement(("completed",)))

            self.assertEqual(load_plan(directory), original)
            self.assertEqual(list(Path(directory).glob(".plan-*.tmp")), [])

    def test_context_slot_is_bounded_for_maximal_utf8_input(self):
        value = {
            "plan_status": "active",
            "explanation": "reason",
            "plan": [
                {"step": "🧭" * 256, "status": "pending"}
                for _ in range(MAX_ITEMS)
            ],
        }
        with TemporaryDirectory() as directory:
            state = replace_plan(directory, value)
            rendered = render_context_slot(state)

        self.assertLessEqual(len(rendered.encode("utf-8")), MAX_CONTEXT_SLOT_BYTES)
        self.assertIn("1. [ ]", rendered)
        self.assertIn(f"{MAX_ITEMS}. [ ]", rendered)


class CommandTests(unittest.TestCase):
    def test_update_persists_then_publishes_context(self):
        with TemporaryDirectory() as directory:
            host = FakeHost(directory)
            result = handle_update_plan(
                SimpleNamespace(
                    input=replacement(("completed", "in_progress")),
                    host=host,
                )
            )
            loaded = load_plan(directory)

        self.assertEqual(
            result,
            {
                "accepted": True,
                "revision": 1,
                "plan_status": "active",
                "counts": {
                    "pending": 0,
                    "in_progress": 1,
                    "completed": 1,
                },
                "presentation_published": True,
                "context_published": True,
            },
        )
        self.assertEqual(host.slots[0][0], "plan")
        self.assertIn("2. [>] Step 2", host.slots[0][1])
        self.assertEqual(host.presentations[0]["revision"], 1)
        self.assertEqual(host.presentations[0]["status"], "active")
        self.assertEqual(
            host.presentations[0]["items"][1],
            {"step": "Step 2", "status": "in_progress"},
        )
        self.assertEqual(loaded.revision, 1)

    def test_idle_stops_without_state_or_when_complete(self):
        with TemporaryDirectory() as directory:
            host = FakeHost(directory)
            context = SimpleNamespace(input={}, host=host)
            self.assertEqual(handle_idle(context), {"action": "stop"})
            self.assertEqual(host.slots, [("plan", "")])

            replace_plan(directory, replacement(("completed", "completed")))
            self.assertEqual(handle_idle(context), {"action": "stop"})
            self.assertEqual(host.slots[-1], ("plan", ""))

    def test_complete_update_clears_context_projection(self):
        with TemporaryDirectory() as directory:
            host = FakeHost(directory)
            result = handle_update_plan(
                SimpleNamespace(
                    input=replacement(("completed", "completed")),
                    host=host,
                )
            )

        self.assertTrue(result["accepted"])
        self.assertEqual(host.slots, [("plan", "")])
        self.assertEqual(host.presentations[0]["status"], "completed")
        self.assertIsNone(host.presentations[0]["explanation"])

    def test_complete_projection_omits_stale_blocking_explanation(self):
        with TemporaryDirectory() as directory:
            host = FakeHost(directory)
            result = handle_update_plan(
                SimpleNamespace(
                    input=replacement(
                        ("completed",),
                        plan_status="blocked",
                        explanation="Was blocked before completion",
                    ),
                    host=host,
                )
            )

        self.assertTrue(result["accepted"])
        self.assertEqual(host.presentations[0]["status"], "completed")
        self.assertIsNone(host.presentations[0]["explanation"])

    def test_semantic_rejection_is_bounded_model_visible_output(self):
        with TemporaryDirectory() as directory:
            host = FakeHost(directory)
            result = handle_update_plan(
                SimpleNamespace(
                    input=replacement(("in_progress", "in_progress")),
                    host=host,
                )
            )

            self.assertEqual(
                result,
                {
                    "accepted": False,
                    "error": "update_plan input permits at most one in_progress item",
                },
            )
            self.assertIsNone(load_plan(directory))
            self.assertEqual(host.slots, [])

    def test_semantic_rejection_sanitizes_adversarial_field_names(self):
        with TemporaryDirectory() as directory:
            invalid = replacement()
            invalid["unknown-\ud800\u2067" + ("🧨" * 2048)] = True
            result = handle_update_plan(
                SimpleNamespace(input=invalid, host=FakeHost(directory))
            )

        self.assertFalse(result["accepted"])
        self.assertEqual(result["error"], "update_plan input has unknown fields")
        self.assertLessEqual(
            len(result["error"].encode("utf-8")),
            MAX_MODEL_ERROR_BYTES,
        )
        self.assertNotIn("\ud800", result["error"])
        self.assertNotIn("\u2067", result["error"])

    def test_model_error_output_is_format_safe_and_byte_bounded(self):
        message = ("detail\u2067" * 256) + "\ud800"
        output = _bounded_model_error(PlanValidationError(message))

        self.assertLessEqual(len(output.encode("utf-8")), MAX_MODEL_ERROR_BYTES)
        self.assertTrue(output.endswith("..."))
        self.assertNotIn("\ud800", output)
        self.assertNotIn("\u2067", output)

    def test_context_projection_failure_does_not_hide_durable_plan_or_stop_idle(self):
        with TemporaryDirectory() as directory:
            host = FakeHost(directory, reject_slots=True)
            updated = handle_update_plan(
                SimpleNamespace(input=replacement(("pending",)), host=host)
            )
            continued = handle_idle(SimpleNamespace(input={}, host=host))

            self.assertIsNotNone(load_plan(directory))

        self.assertTrue(updated["accepted"])
        self.assertFalse(updated["context_published"])
        self.assertTrue(updated["presentation_published"])
        self.assertEqual(continued["action"], "continue")

    def test_presentation_failure_does_not_hide_durable_plan(self):
        with TemporaryDirectory() as directory:
            host = FakeHost(directory, reject_presentations=True)
            updated = handle_update_plan(
                SimpleNamespace(input=replacement(("in_progress",)), host=host)
            )

            self.assertIsNotNone(load_plan(directory))

        self.assertTrue(updated["accepted"])
        self.assertFalse(updated["presentation_published"])
        self.assertTrue(updated["context_published"])

    def test_idle_repairs_active_presentation_after_update_failure(self):
        with TemporaryDirectory() as directory:
            failed_host = FakeHost(directory, reject_presentations=True)
            updated = handle_update_plan(
                SimpleNamespace(input=replacement(("in_progress",)), host=failed_host)
            )
            recovery_host = FakeHost(directory)
            continued = handle_idle(SimpleNamespace(input={}, host=recovery_host))

        self.assertFalse(updated["presentation_published"])
        self.assertEqual(continued["action"], "continue")
        self.assertEqual(
            recovery_host.presentations,
            [
                {
                    "revision": 1,
                    "status": "active",
                    "explanation": None,
                    "items": [{"step": "Step 1", "status": "in_progress"}],
                }
            ],
        )

    def test_idle_repairs_completed_presentation_before_stopping(self):
        with TemporaryDirectory() as directory:
            failed_host = FakeHost(directory, reject_presentations=True)
            updated = handle_update_plan(
                SimpleNamespace(input=replacement(("completed",)), host=failed_host)
            )
            recovery_host = FakeHost(directory)
            stopped = handle_idle(SimpleNamespace(input={}, host=recovery_host))

        self.assertFalse(updated["presentation_published"])
        self.assertEqual(stopped, {"action": "stop"})
        self.assertEqual(recovery_host.slots, [("plan", "")])
        self.assertEqual(
            recovery_host.presentations,
            [
                {
                    "revision": 1,
                    "status": "completed",
                    "explanation": None,
                    "items": [{"step": "Step 1", "status": "completed"}],
                }
            ],
        )

    def test_corrupt_durable_state_clears_projection_and_fails_closed(self):
        with TemporaryDirectory() as directory:
            (Path(directory) / "plan.json").write_text("{", encoding="utf-8")
            host = FakeHost(directory)

            with self.assertRaises(DurablePlanError):
                handle_idle(SimpleNamespace(input={}, host=host))

        self.assertEqual(host.slots, [("plan", "")])

    def test_update_clears_projection_only_for_corrupt_durable_state(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text("{", encoding="utf-8")
            corrupt_host = FakeHost(directory)
            corrupt_result = handle_update_plan(
                SimpleNamespace(input=replacement(), host=corrupt_host)
            )

            path.unlink()
            original = replace_plan(directory, replacement(("in_progress",)))
            invalid_host = FakeHost(directory)
            invalid_result = handle_update_plan(
                SimpleNamespace(
                    input=replacement(("in_progress", "in_progress")),
                    host=invalid_host,
                )
            )

            self.assertEqual(load_plan(directory), original)

        self.assertFalse(corrupt_result["accepted"])
        self.assertIn("durable plan", corrupt_result["error"])
        self.assertEqual(corrupt_host.slots, [("plan", "")])
        self.assertFalse(invalid_result["accepted"])
        self.assertEqual(invalid_host.slots, [])
        self.assertEqual(invalid_host.presentations, [])

    def test_idle_stops_for_explicit_blocked_and_waiting_plans(self):
        for plan_status in ("blocked", "waiting"):
            with self.subTest(plan_status=plan_status), TemporaryDirectory() as directory:
                replace_plan(
                    directory,
                    replacement(
                        ("in_progress", "pending"),
                        plan_status=plan_status,
                        explanation="Needs an external response",
                    ),
                )
                host = FakeHost(directory)

                self.assertEqual(
                    handle_idle(SimpleNamespace(input={}, host=host)),
                    {"action": "stop"},
                )
                self.assertIn("Needs an external response", host.slots[-1][1])

    def test_idle_continues_at_in_progress_then_pending_item(self):
        cases = [
            (("pending", "in_progress"), 2, "Step 2"),
            (("completed", "pending"), 2, "Step 2"),
        ]
        for statuses, expected_index, expected_step in cases:
            with self.subTest(statuses=statuses), TemporaryDirectory() as directory:
                replace_plan(directory, replacement(statuses))
                result = handle_idle(
                    SimpleNamespace(input={}, host=FakeHost(directory))
                )

                self.assertEqual(set(result), {"action", "input"})
                self.assertEqual(result["action"], "continue")
                self.assertIn(
                    f"item {expected_index}: {expected_step}",
                    result["input"],
                )

    def test_idle_requires_exact_empty_input(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                handle_idle(
                    SimpleNamespace(input={"extra": True}, host=FakeHost(directory))
                )


if __name__ == "__main__":
    unittest.main()
