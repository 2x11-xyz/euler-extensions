import json
import hashlib
from contextlib import ExitStack
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch


PYTHON_DIRECTORY = Path(__file__).resolve().parents[1]
REPOSITORY_DIRECTORY = Path(__file__).resolve().parents[4]
SDK_SOURCE = REPOSITORY_DIRECTORY / "sdks/python/euler-managed-process-sdk/src"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SDK_SOURCE))
sys.path.insert(0, str(PYTHON_DIRECTORY))

from euler_managed_process_sdk import Cancelled, HostError, ProtocolError

from session_run_health.commands import handle_request_tick
from session_run_health.config import (
    CONFIG_FILENAME,
    ConfigurationError,
    DEFAULT_THRESHOLDS,
)
from session_run_health.policy import make_pending_checkpoint
from session_run_health.state import (
    DurableStateError,
    STATE_FILENAME,
    load_state,
    new_state,
    store_state,
)


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def minimal_event(event_id, kind="canvas.snapshot", payload=None):
    return {
        "v": 1,
        "id": event_id,
        "ts": "2026-07-30T00:00:00.000Z",
        "session": "fixture",
        "agent": "root",
        "parent": None,
        "kind": kind,
        "payload": payload or {},
        "blobs": {},
    }


class FakeHost:
    def __init__(self, state_directory, events, *, page_size=None):
        self._state_directory = state_directory
        self.events = list(events)
        self.page_size = page_size
        self.queries = []
        self.query_response_sizes = []
        self.artifact_calls = 0
        self.artifact_appends = 0
        self.artifact_documents = []
        self.plan_calls = 0
        self.plan_appends = 0
        self.context_calls = 0
        self.context_appends = 0
        self._plans = set()
        self._slots = {}
        self._effect_sequence = 0

    def state_dir(self):
        return self._state_directory

    def query_provenance(self, **query):
        self.queries.append(query)
        after = query["after_event_id"]
        through = query["through_event_id"]
        ids = [event["id"] for event in self.events]
        start = 0 if after is None else ids.index(after) + 1
        stop = ids.index(through) + 1
        scanned = self.events[start:stop]
        page_size = min(
            self.page_size or len(scanned) or 1,
            query["limit"],
        )
        selected_prefix = scanned[:page_size]
        selected = [
            event for event in selected_prefix if event["kind"] in query["kinds"]
        ]
        truncated = len(scanned) > len(selected_prefix)
        watermark = selected_prefix[-1]["id"] if selected_prefix else (after or through)
        response = {
            "events": selected,
            "watermark_event_id": watermark,
            "next_after_event_id": watermark if truncated else None,
            "truncated": truncated,
        }
        self.query_response_sizes.append(
            len(json.dumps(response, separators=(",", ":")).encode("utf-8"))
        )
        return response

    def write_artifact(self, **request):
        self.artifact_calls += 1
        self.artifact_documents.append(json.loads(request["data"]))
        self._effect_sequence += 1
        event_id = f"effect-artifact-{self._effect_sequence}"
        self.events.append(
            minimal_event(
                event_id,
                "extension.artifact",
                {
                    "extension_id": "session-run-health",
                    "metadata": request["metadata"],
                },
            )
        )
        self.artifact_appends += 1
        return {"persisted_event_id": event_id}

    def update_plan_presentation(self, **presentation):
        self.plan_calls += 1
        identity = json.dumps(presentation, sort_keys=True)
        if identity not in self._plans:
            self._plans.add(identity)
            self.plan_appends += 1
            self._effect_sequence += 1
            self.events.append(
                minimal_event(
                    f"effect-plan-{self._effect_sequence}",
                    "plan.update",
                    {
                        "extension_id": "session-run-health",
                        **presentation,
                    },
                )
            )

    def update_context_slot(self, slot, content):
        self.context_calls += 1
        if self._slots.get(slot) != content:
            self._slots[slot] = content
            self.context_appends += 1
            self._effect_sequence += 1
            self.events.append(
                minimal_event(
                    f"effect-slot-{self._effect_sequence}",
                    "context.slot.updated",
                    {
                        "extension_id": "session-run-health",
                        "slot": slot,
                        "content": content,
                    },
                )
            )


class CommandTests(unittest.TestCase):
    def test_query_host_error_clears_context_before_escape(self):
        with TemporaryDirectory() as directory:
            host = FakeHost(directory, [minimal_event("cutoff-1")])
            host._slots["session-run-health"] = "stale advisory"
            original = HostError("query denied")
            with patch.object(host, "query_provenance", side_effect=original):
                with self.assertRaises(HostError) as raised:
                    handle_request_tick(
                        SimpleNamespace(
                            input={"through_event_id": "cutoff-1"},
                            host=host,
                        )
                    )

        self.assertIs(raised.exception, original)
        self.assertEqual(host._slots["session-run-health"], "")
        self.assertEqual(host.context_calls, 1)

    def test_malformed_page_and_fold_clear_context_before_escape(self):
        malformed = (
            (
                {
                    "events": [],
                    "watermark_event_id": "cutoff-1",
                    "next_after_event_id": "different-cursor",
                    "truncated": True,
                },
                "continuation",
            ),
            (
                {
                    "events": [
                        {
                            **minimal_event("cutoff-1", "run.started"),
                            "run": "run-root",
                            "payload": "not-an-object",
                        }
                    ],
                    "watermark_event_id": "cutoff-1",
                    "next_after_event_id": None,
                    "truncated": False,
                },
                "payload",
            ),
        )
        for response, message in malformed:
            with self.subTest(message=message), TemporaryDirectory() as directory:
                host = FakeHost(directory, [minimal_event("cutoff-1")])
                host._slots["session-run-health"] = "stale advisory"
                with patch.object(host, "query_provenance", return_value=response):
                    with self.assertRaisesRegex(ValueError, message):
                        handle_request_tick(
                            SimpleNamespace(
                                input={"through_event_id": "cutoff-1"},
                                host=host,
                            )
                        )

                self.assertEqual(host._slots["session-run-health"], "")
                self.assertEqual(host.context_calls, 1)

    def test_state_store_error_clears_context_before_escape(self):
        with TemporaryDirectory() as directory:
            host = FakeHost(directory, [minimal_event("cutoff-1")])
            host._slots["session-run-health"] = "stale advisory"
            original = OSError("state device failed")
            with patch(
                "session_run_health.commands.store_state",
                side_effect=original,
            ):
                with self.assertRaises(OSError) as raised:
                    handle_request_tick(
                        SimpleNamespace(
                            input={"through_event_id": "cutoff-1"},
                            host=host,
                        )
                    )

        self.assertIs(raised.exception, original)
        self.assertEqual(host._slots["session-run-health"], "")
        self.assertEqual(host.context_calls, 1)

    def test_publication_validation_error_clears_context_before_escape(self):
        with TemporaryDirectory() as directory:
            host = FakeHost(directory, fixture("repeated-malformed-patches.json"))
            host._slots["session-run-health"] = "stale advisory"
            with patch.object(host, "write_artifact", return_value={}):
                with self.assertRaisesRegex(ValueError, "artifact record"):
                    handle_request_tick(
                        SimpleNamespace(
                            input={"through_event_id": "patch-04"},
                            host=host,
                        )
                    )

        self.assertEqual(host._slots["session-run-health"], "")
        self.assertEqual(host.context_calls, 1)

    def test_cleanup_host_error_never_replaces_original_failure(self):
        cases = (
            ("query", HostError("query original")),
            ("page", ValueError("page original")),
            ("fold", ValueError("fold original")),
            ("store", OSError("store original")),
        )
        for failure_point, original in cases:
            with self.subTest(failure_point=failure_point):
                with TemporaryDirectory() as directory:
                    host = FakeHost(directory, [minimal_event("cutoff-1")])
                    with ExitStack() as stack:
                        if failure_point == "query":
                            stack.enter_context(
                                patch.object(
                                    host,
                                    "query_provenance",
                                    side_effect=original,
                                )
                            )
                        else:
                            target = {
                                "page": "session_run_health.commands._parse_page",
                                "fold": "session_run_health.commands.fold_events",
                                "store": "session_run_health.commands.store_state",
                            }[failure_point]
                            stack.enter_context(patch(target, side_effect=original))
                        cleanup = stack.enter_context(
                            patch.object(
                                host,
                                "update_context_slot",
                                side_effect=HostError("cleanup denied"),
                            )
                        )
                        with self.assertRaises(Exception) as raised:
                            handle_request_tick(
                                SimpleNamespace(
                                    input={"through_event_id": "cutoff-1"},
                                    host=host,
                                )
                            )

                    self.assertIs(raised.exception, original)
                    cleanup.assert_called_once_with("session-run-health", "")

    def test_cleanup_exception_classes_never_replace_original_failure(self):
        cleanup_errors = (
            HostError("cleanup host failure"),
            ProtocolError("cleanup protocol failure"),
            Cancelled("cleanup cancellation"),
            OSError("cleanup wire failure"),
            RuntimeError("cleanup runtime failure"),
        )
        for cleanup_error in cleanup_errors:
            with self.subTest(cleanup_type=type(cleanup_error).__name__):
                with TemporaryDirectory() as directory:
                    host = FakeHost(directory, [minimal_event("cutoff-1")])
                    original = ValueError("original page failure")
                    with patch.object(
                        host,
                        "query_provenance",
                        side_effect=original,
                    ), patch.object(
                        host,
                        "update_context_slot",
                        side_effect=cleanup_error,
                    ) as cleanup:
                        with self.assertRaises(ValueError) as raised:
                            handle_request_tick(
                                SimpleNamespace(
                                    input={"through_event_id": "cutoff-1"},
                                    host=host,
                                )
                            )

                self.assertIs(raised.exception, original)
                cleanup.assert_called_once_with("session-run-health", "")

    def test_cancellation_escapes_without_context_cleanup(self):
        with TemporaryDirectory() as directory:
            host = FakeHost(directory, [minimal_event("cutoff-1")])
            host._slots["session-run-health"] = "active advisory"
            original = Cancelled("host cancelled command")
            with patch.object(host, "query_provenance", side_effect=original):
                with self.assertRaises(Cancelled) as raised:
                    handle_request_tick(
                        SimpleNamespace(
                            input={"through_event_id": "cutoff-1"},
                            host=host,
                        )
                    )

        self.assertIs(raised.exception, original)
        self.assertEqual(host._slots["session-run-health"], "active advisory")
        self.assertEqual(host.context_calls, 0)

    def test_invalid_config_or_state_clears_fixed_context_before_propagating(self):
        cases = (
            (
                CONFIG_FILENAME,
                '{"schema_version":1,"unknown":true}',
                ConfigurationError,
            ),
            (STATE_FILENAME, "{}", DurableStateError),
        )
        for filename, content, error_type in cases:
            with self.subTest(filename=filename), TemporaryDirectory() as directory:
                Path(directory, filename).write_text(content, encoding="utf-8")
                host = FakeHost(directory, [minimal_event("cutoff-1")])
                host._slots["session-run-health"] = "stale advisory"

                with self.assertRaises(error_type):
                    handle_request_tick(
                        SimpleNamespace(
                            input={"through_event_id": "cutoff-1"},
                            host=host,
                        )
                    )

                self.assertEqual(host._slots["session-run-health"], "")
                self.assertEqual(host.context_calls, 1)
                self.assertEqual(host.queries, [])

    def test_load_error_survives_context_clear_host_error(self):
        cases = (
            (CONFIG_FILENAME, "{}", ConfigurationError, "schema_version"),
            (STATE_FILENAME, "{}", DurableStateError, "incompatible shape"),
        )
        for filename, content, error_type, message in cases:
            with self.subTest(filename=filename), TemporaryDirectory() as directory:
                Path(directory, filename).write_text(content, encoding="utf-8")
                host = FakeHost(directory, [minimal_event("cutoff-1")])
                with patch.object(
                    host,
                    "update_context_slot",
                    side_effect=HostError("context-slot denied"),
                ) as clear:
                    with self.assertRaisesRegex(error_type, message):
                        handle_request_tick(
                            SimpleNamespace(
                                input={"through_event_id": "cutoff-1"},
                                host=host,
                            )
                        )

                clear.assert_called_once_with("session-run-health", "")
                self.assertEqual(host.queries, [])

    def test_bounded_tick_publishes_once_and_persists_no_private_payload_text(self):
        private_text = (
            "PRIVATE USER REQUEST",
            "PRIVATE MODEL REASONING",
            "PRIVATE TOOL INPUT",
            "PRIVATE TOOL CALL ID",
            "first raw error",
            "different raw wording",
            "third raw error",
        )
        with TemporaryDirectory() as directory:
            host = FakeHost(directory, fixture("repeated-malformed-patches.json"))
            context = SimpleNamespace(
                input={"through_event_id": "patch-04"},
                host=host,
            )
            first = handle_request_tick(context)
            second = handle_request_tick(context)
            state_text = Path(directory, "run-health-state.json").read_text(
                encoding="utf-8"
            )

        self.assertTrue(first["checkpoint_published"])
        self.assertFalse(first["checkpoint_pending"])
        self.assertFalse(second["checkpoint_published"])
        self.assertEqual(host.artifact_appends, 1)
        self.assertEqual(host.plan_appends, 1)
        self.assertEqual(host.context_appends, 1)
        persisted_surfaces = state_text + json.dumps(host.artifact_documents)
        for raw in (*private_text, "apply_patch"):
            self.assertNotIn(raw, persisted_surfaces)
        self.assertTrue(
            all(query["through_event_id"] == "patch-04" for query in host.queries)
        )

    def test_active_context_rearms_after_failure_cleanup_and_resume(self):
        with TemporaryDirectory() as directory:
            host = self._active_checkpoint_host(directory)
            self._clear_active_context(host, "active-failure")
            host.events.append(minimal_event("active-resume"))

            result = handle_request_tick(
                SimpleNamespace(
                    input={"through_event_id": "active-resume"},
                    host=host,
                )
            )
            state = load_state(directory)

        self.assertTrue(result["checkpoint_active"])
        self.assertFalse(result["checkpoint_published"])
        self.assertTrue(state["active_checkpoint"]["context_published"])
        self.assertEqual(host.artifact_calls, 1)
        self.assertEqual(host.plan_calls, 1)
        self.assertEqual(host.context_calls, 3)
        self.assertEqual(host.context_appends, 3)
        self.assertNotEqual(host._slots["session-run-health"], "")

    def test_recovery_retires_cleared_active_context_without_rearming(self):
        with TemporaryDirectory() as directory:
            host = self._active_checkpoint_host(directory)
            self._clear_active_context(host, "active-recovery-failure")
            host.events.extend(self._recovery_attempt("active-recovery"))

            result = handle_request_tick(
                SimpleNamespace(
                    input={"through_event_id": "active-recovery-result"},
                    host=host,
                )
            )
            state = load_state(directory)

        self.assertTrue(result["checkpoint_retired"])
        self.assertFalse(result["checkpoint_active"])
        self.assertIsNone(state["active_checkpoint"])
        self.assertEqual(host.artifact_calls, 1)
        self.assertEqual(host.plan_appends, 2)
        self.assertEqual(host.context_appends, 2)
        self.assertEqual(host._slots["session-run-health"], "")

    def test_active_context_rearm_retries_after_crash_before_state_store(self):
        with TemporaryDirectory() as directory:
            host = self._active_checkpoint_host(directory)
            self._clear_active_context(host, "active-rearm-failure")
            host.events.append(minimal_event("active-rearm-cutoff"))
            store_calls = 0
            original = OSError("simulated crash after active context append")

            def crash_after_rearm(state_directory, state):
                nonlocal store_calls
                store_calls += 1
                if store_calls == 2:
                    raise original
                store_state(state_directory, state)

            with patch(
                "session_run_health.commands.store_state",
                side_effect=crash_after_rearm,
            ):
                with self.assertRaises(OSError) as raised:
                    handle_request_tick(
                        SimpleNamespace(
                            input={"through_event_id": "active-rearm-cutoff"},
                            host=host,
                        )
                    )

            self.assertIs(raised.exception, original)
            self.assertFalse(
                load_state(directory)["active_checkpoint"]["context_published"]
            )
            self.assertEqual(host._slots["session-run-health"], "")
            host.events.append(minimal_event("active-rearm-confirm"))

            result = handle_request_tick(
                SimpleNamespace(
                    input={"through_event_id": "active-rearm-confirm"},
                    host=host,
                )
            )
            state = load_state(directory)

        self.assertTrue(result["checkpoint_active"])
        self.assertTrue(state["active_checkpoint"]["context_published"])
        self.assertEqual(host.artifact_calls, 1)
        self.assertEqual(host.plan_calls, 1)
        self.assertEqual(host.context_appends, 5)
        self.assertNotEqual(host._slots["session-run-health"], "")

    def test_active_rearm_replay_confirms_effect_when_cleanup_fails(self):
        with TemporaryDirectory() as directory:
            host = self._active_checkpoint_host(directory)
            self._clear_active_context(host, "active-replay-failure")
            host.events.append(minimal_event("active-replay-cutoff"))
            store_calls = 0
            original = OSError("state store failed after active context append")
            cleanup_failure = ProtocolError("cleanup wire failed")
            real_update_context_slot = host.update_context_slot

            def crash_after_rearm(state_directory, state):
                nonlocal store_calls
                store_calls += 1
                if store_calls == 2:
                    raise original
                store_state(state_directory, state)

            def fail_only_cleanup(slot, content):
                if content == "":
                    raise cleanup_failure
                real_update_context_slot(slot, content)

            with patch(
                "session_run_health.commands.store_state",
                side_effect=crash_after_rearm,
            ), patch.object(
                host,
                "update_context_slot",
                side_effect=fail_only_cleanup,
            ) as slot_updates:
                with self.assertRaises(OSError) as raised:
                    handle_request_tick(
                        SimpleNamespace(
                            input={"through_event_id": "active-replay-cutoff"},
                            host=host,
                        )
                    )

            self.assertIs(raised.exception, original)
            self.assertFalse(
                load_state(directory)["active_checkpoint"]["context_published"]
            )
            self.assertNotEqual(host._slots["session-run-health"], "")
            self.assertEqual(slot_updates.call_count, 2)
            host.events.append(minimal_event("active-replay-confirm"))

            result = handle_request_tick(
                SimpleNamespace(
                    input={"through_event_id": "active-replay-confirm"},
                    host=host,
                )
            )
            state = load_state(directory)

        self.assertTrue(result["checkpoint_active"])
        self.assertTrue(state["active_checkpoint"]["context_published"])
        self.assertEqual(host.artifact_calls, 1)
        self.assertEqual(host.plan_calls, 1)
        self.assertEqual(host.context_appends, 3)

    def test_active_context_rearm_host_error_remains_retryable(self):
        with TemporaryDirectory() as directory:
            host = self._active_checkpoint_host(directory)
            self._clear_active_context(host, "active-host-error-failure")
            host.events.append(minimal_event("active-host-error-cutoff"))

            with patch.object(
                host,
                "update_context_slot",
                side_effect=HostError("context capability unavailable"),
            ) as update:
                first = handle_request_tick(
                    SimpleNamespace(
                        input={"through_event_id": "active-host-error-cutoff"},
                        host=host,
                    )
                )
            self.assertTrue(first["checkpoint_active"])
            self.assertFalse(
                load_state(directory)["active_checkpoint"]["context_published"]
            )
            update.assert_called_once()

            host.events.append(minimal_event("active-host-error-retry"))
            second = handle_request_tick(
                SimpleNamespace(
                    input={"through_event_id": "active-host-error-retry"},
                    host=host,
                )
            )
            state = load_state(directory)

        self.assertTrue(second["checkpoint_active"])
        self.assertTrue(state["active_checkpoint"]["context_published"])
        self.assertEqual(host.artifact_calls, 1)
        self.assertEqual(host.plan_calls, 1)
        self.assertEqual(host.context_appends, 3)

    def test_event_after_cutoff_cannot_affect_the_current_tick(self):
        events = fixture("repeated-malformed-patches.json")
        with TemporaryDirectory() as directory:
            host = FakeHost(directory, events)
            first = handle_request_tick(
                SimpleNamespace(
                    input={"through_event_id": "patch-03"},
                    host=host,
                )
            )
            second = handle_request_tick(
                SimpleNamespace(
                    input={"through_event_id": "patch-04"},
                    host=host,
                )
            )

        self.assertFalse(first["checkpoint_published"])
        self.assertEqual(first["assessed_through_event_id"], "patch-03")
        self.assertTrue(second["checkpoint_published"])
        self.assertEqual(host.queries[0]["through_event_id"], "patch-03")
        self.assertEqual(host.queries[1]["through_event_id"], "patch-04")

    def test_partial_page_budget_reports_the_reached_cursor_not_requested_cutoff(self):
        events = [minimal_event("page-1"), minimal_event("page-2")]
        with TemporaryDirectory() as directory:
            host = FakeHost(directory, events, page_size=1)
            with patch("session_run_health.commands.MAX_QUERY_PAGES", 1):
                result = handle_request_tick(
                    SimpleNamespace(
                        input={"through_event_id": "page-2"},
                        host=host,
                    )
                )

            persisted = load_state(directory)

        self.assertFalse(result["caught_up"])
        self.assertEqual(result["requested_through_event_id"], "page-2")
        self.assertEqual(result["assessed_through_event_id"], "page-1")
        self.assertEqual(persisted["after_event_id"], "page-1")

    def test_content_heavy_pages_stay_well_below_the_managed_frame_limit(self):
        events = [
            minimal_event(
                f"large-{index:02d}",
                "model.result",
                {
                    "provider": "fixture",
                    "model": "fixture",
                    "content": "x" * 8192,
                    "tool_calls": [],
                    "stop_reason": "tool",
                },
            )
            for index in range(32)
        ]
        with TemporaryDirectory() as directory:
            host = FakeHost(directory, events)
            result = handle_request_tick(
                SimpleNamespace(input={"through_event_id": "large-31"}, host=host)
            )

        self.assertTrue(result["caught_up"])
        self.assertEqual(result["pages"], 4)
        self.assertTrue(all(query["limit"] == 8 for query in host.queries))
        self.assertLess(max(host.query_response_sizes), 256 * 1024)

    def test_partial_failure_waits_for_recovery_later_in_the_same_cutoff(self):
        events = [
            {
                **minimal_event("recovery-run", "run.started", {"trigger": "direct"}),
                "run": "run-root",
            }
        ]
        for index in range(1, 4):
            events.extend(
                [
                    {
                        **minimal_event(
                            f"recovery-call-{index}",
                            "tool.call",
                            {
                                "id": f"failure-{index}",
                                "name": "apply_patch",
                                "input": {"patch": f"omitted-{index}"},
                            },
                        ),
                        "run": "run-root",
                    },
                    {
                        **minimal_event(
                            f"recovery-failure-{index}",
                            "tool.result",
                            {
                                "id": f"failure-{index}",
                                "name": "apply_patch",
                                "ok": False,
                                "error": "omitted",
                            },
                        ),
                        "run": "run-root",
                    },
                ]
            )
        events.extend(
            [
                {
                    **minimal_event(
                        "recovery-success-call",
                        "tool.call",
                        {
                            "id": "success",
                            "name": "apply_patch",
                            "input": {"patch": "omitted-success"},
                        },
                    ),
                    "run": "run-root",
                },
                {
                    **minimal_event(
                        "recovery-success",
                        "tool.result",
                        {"id": "success", "name": "apply_patch", "ok": True},
                    ),
                    "run": "run-root",
                },
            ]
        )

        with TemporaryDirectory() as directory:
            host = FakeHost(directory, events)
            before_threshold = handle_request_tick(
                SimpleNamespace(
                    input={"through_event_id": "recovery-failure-2"},
                    host=host,
                )
            )
            self.assertFalse(before_threshold["checkpoint_published"])

            host.page_size = 2
            with patch("session_run_health.commands.MAX_QUERY_PAGES", 1):
                partial = handle_request_tick(
                    SimpleNamespace(
                        input={"through_event_id": "recovery-success"},
                        host=host,
                    )
                )
            self.assertFalse(partial["caught_up"])
            self.assertEqual(
                partial["assessed_through_event_id"],
                "recovery-failure-3",
            )
            self.assertFalse(partial["checkpoint_published"])

            host.page_size = None
            recovered = handle_request_tick(
                SimpleNamespace(
                    input={"through_event_id": "recovery-success"},
                    host=host,
                )
            )

        self.assertTrue(recovered["caught_up"])
        self.assertFalse(recovered["checkpoint_published"])
        self.assertEqual(host.artifact_calls, 0)
        self.assertEqual(host.plan_calls, 0)
        self.assertEqual(host.context_calls, 0)

    def test_resume_reconciles_an_artifact_written_before_state_acknowledgement(self):
        signal = {
            "kind": "repeated_failure",
            "count": 3,
            "event_ids": ["evidence-1", "evidence-2", "evidence-3"],
            "_entity": "f" * 64,
            "_sequence": 1,
        }
        with TemporaryDirectory() as directory:
            state = new_state()
            state["root_agent"] = "root"
            state["after_event_id"] = "base-1"
            self._seed_failure_track(state)
            pending = make_pending_checkpoint(
                state,
                [signal],
                "base-1",
                DEFAULT_THRESHOLDS,
            )
            store_state(directory, state)
            already_written = minimal_event(
                "artifact-existing",
                "extension.artifact",
                {
                    "extension_id": "session-run-health",
                    "metadata": {
                        "kind": "run-health-checkpoint",
                        "signature": pending["signature"],
                    },
                },
            )
            host = FakeHost(
                directory,
                [minimal_event("base-1"), already_written, minimal_event("cutoff-2")],
            )
            result = handle_request_tick(
                SimpleNamespace(
                    input={"through_event_id": "cutoff-2"},
                    host=host,
                )
            )

        self.assertTrue(result["checkpoint_published"])
        self.assertEqual(host.artifact_calls, 0)
        self.assertEqual(host.plan_appends, 1)
        self.assertEqual(host.context_appends, 1)

    def test_artifact_effect_is_reconciled_after_process_death(self):
        with TemporaryDirectory() as directory:
            self._stored_pending(
                directory,
                artifact_published=False,
                plan_published=False,
                context_published=False,
            )
            host = FakeHost(directory, [minimal_event("base-1")])
            store_calls = 0

            def crash_after_artifact(state_directory, state):
                nonlocal store_calls
                store_calls += 1
                if store_calls == 2:
                    raise OSError("simulated process death after artifact append")
                store_state(state_directory, state)

            with patch(
                "session_run_health.commands.store_state",
                side_effect=crash_after_artifact,
            ):
                with self.assertRaises(OSError):
                    handle_request_tick(
                        SimpleNamespace(input={"through_event_id": "base-1"}, host=host)
                    )
            self.assertIsNone(
                load_state(directory)["pending_checkpoint"]["artifact_event_id"]
            )
            self.assertEqual(host._slots["session-run-health"], "")
            host.events.append(minimal_event("after-artifact"))

            # PR #219 skips more live-session ticks after the escaped error;
            # this call models the first request boundary after resume.
            result = handle_request_tick(
                SimpleNamespace(input={"through_event_id": "after-artifact"}, host=host)
            )

        self.assertTrue(result["checkpoint_published"])
        self.assertEqual(host.artifact_calls, 1)
        self.assertEqual(host.artifact_appends, 1)

    def test_plan_reconciliation_is_exact_after_crash_and_session_resume(self):
        with TemporaryDirectory() as directory:
            self._stored_pending(directory, plan_published=False, context_published=False)
            host = FakeHost(directory, [minimal_event("base-1")])
            store_calls = 0

            def crash_after_host_append(state_directory, state):
                nonlocal store_calls
                store_calls += 1
                if store_calls == 2:
                    raise OSError("simulated crash after plan append")
                store_state(state_directory, state)

            with patch(
                "session_run_health.commands.store_state",
                side_effect=crash_after_host_append,
            ):
                with self.assertRaises(OSError):
                    handle_request_tick(
                        SimpleNamespace(
                            input={"through_event_id": "base-1"},
                            host=host,
                        )
                    )
            self.assertFalse(load_state(directory)["pending_checkpoint"]["plan_published"])
            self.assertEqual(host._slots["session-run-health"], "")
            host.events.append(minimal_event("resume-after-plan"))

            # The new cutoff models a resumed process with a fresh tick latch.
            result = handle_request_tick(
                SimpleNamespace(
                    input={"through_event_id": "resume-after-plan"},
                    host=host,
                )
            )
            self.assertTrue(result["checkpoint_published"])

        self.assertEqual(host.plan_calls, 1)
        self.assertEqual(host.plan_appends, 1)
        self.assertEqual(host.context_appends, 2)

    def test_context_republication_is_exact_after_crash_and_session_resume(self):
        with TemporaryDirectory() as directory:
            self._stored_pending(directory, plan_published=True, context_published=False)
            host = FakeHost(directory, [minimal_event("base-1")])
            store_calls = 0

            def crash_after_host_append(state_directory, state):
                nonlocal store_calls
                store_calls += 1
                if store_calls == 2:
                    raise OSError("simulated crash after slot append")
                store_state(state_directory, state)

            with patch(
                "session_run_health.commands.store_state",
                side_effect=crash_after_host_append,
            ):
                with self.assertRaises(OSError):
                    handle_request_tick(
                        SimpleNamespace(
                            input={"through_event_id": "base-1"},
                            host=host,
                        )
                    )
            self.assertFalse(load_state(directory)["pending_checkpoint"]["context_published"])
            self.assertEqual(host._slots["session-run-health"], "")
            host.events.append(minimal_event("resume-after-context"))

            # The clear event is folded before the resumed publication retry.
            result = handle_request_tick(
                SimpleNamespace(
                    input={"through_event_id": "resume-after-context"},
                    host=host,
                )
            )
            self.assertTrue(result["checkpoint_published"])

        self.assertEqual(host.context_calls, 3)
        self.assertEqual(host.context_appends, 3)

    def test_crashed_plan_is_reconciled_and_retired_without_stale_context(self):
        with TemporaryDirectory() as directory:
            self._stored_pending(
                directory,
                plan_published=False,
                context_published=False,
            )
            host = FakeHost(directory, [minimal_event("base-1")])
            store_calls = 0

            def crash_after_plan(state_directory, state):
                nonlocal store_calls
                store_calls += 1
                if store_calls == 2:
                    raise OSError("simulated process death after plan append")
                store_state(state_directory, state)

            with patch(
                "session_run_health.commands.store_state",
                side_effect=crash_after_plan,
            ):
                with self.assertRaises(OSError):
                    handle_request_tick(
                        SimpleNamespace(input={"through_event_id": "base-1"}, host=host)
                    )
            host.events.extend(self._recovery_attempt("after-plan"))

            result = handle_request_tick(
                SimpleNamespace(
                    input={"through_event_id": "after-plan-result"},
                    host=host,
                )
            )

        self.assertTrue(result["checkpoint_retired"])
        self.assertEqual(host.plan_appends, 2)
        self.assertEqual(host.context_appends, 1)
        self.assertEqual(host._slots["session-run-health"], "")

    def test_crashed_context_is_reconciled_and_retired_without_republication(self):
        with TemporaryDirectory() as directory:
            self._stored_pending(
                directory,
                plan_published=True,
                context_published=False,
            )
            host = FakeHost(directory, [minimal_event("base-1")])
            store_calls = 0

            def crash_after_context(state_directory, state):
                nonlocal store_calls
                store_calls += 1
                if store_calls == 2:
                    raise OSError("simulated process death after context append")
                store_state(state_directory, state)

            with patch(
                "session_run_health.commands.store_state",
                side_effect=crash_after_context,
            ):
                with self.assertRaises(OSError):
                    handle_request_tick(
                        SimpleNamespace(input={"through_event_id": "base-1"}, host=host)
                    )
            cleared_content = host._slots["session-run-health"]
            host.events.extend(self._recovery_attempt("after-context"))

            result = handle_request_tick(
                SimpleNamespace(
                    input={"through_event_id": "after-context-result"},
                    host=host,
                )
            )

        self.assertEqual(cleared_content, "")
        self.assertTrue(result["checkpoint_retired"])
        self.assertEqual(host.context_calls, 3)
        self.assertEqual(host.context_appends, 2)
        self.assertEqual(host._slots["session-run-health"], "")

    def test_next_request_tick_retires_terminal_run_plan_and_context(self):
        with TemporaryDirectory() as directory:
            host = FakeHost(directory, fixture("repeated-malformed-patches.json"))
            first = handle_request_tick(
                SimpleNamespace(input={"through_event_id": "patch-04"}, host=host)
            )
            self.assertTrue(first["checkpoint_active"])
            host.events.append(
                {
                    **minimal_event(
                        "run-terminal",
                        "run.terminal",
                        {"status": "failed"},
                    ),
                    "run": "run-patch",
                }
            )

            result = handle_request_tick(
                SimpleNamespace(input={"through_event_id": "run-terminal"}, host=host)
            )
            state = load_state(directory)

        self.assertTrue(result["checkpoint_retired"])
        self.assertFalse(result["checkpoint_active"])
        self.assertIsNone(state["pending_retirement"])
        self.assertEqual(host._slots["session-run-health"], "")
        self.assertEqual(host.plan_appends, 2)

    def test_retirement_plan_effect_is_reconciled_after_process_death(self):
        self._assert_retirement_effect_recovery(crash_store_call=4)

    def test_retirement_context_effect_is_reconciled_after_process_death(self):
        self._assert_retirement_effect_recovery(crash_store_call=3)

    def _assert_retirement_effect_recovery(self, *, crash_store_call):
        with TemporaryDirectory() as directory:
            host = FakeHost(directory, fixture("repeated-malformed-patches.json"))
            handle_request_tick(
                SimpleNamespace(input={"through_event_id": "patch-04"}, host=host)
            )
            host.events.extend(self._recovery_attempt("retirement"))
            store_calls = 0

            def crash_after_effect(state_directory, state):
                nonlocal store_calls
                store_calls += 1
                if store_calls == crash_store_call:
                    raise OSError("simulated process death during retirement")
                store_state(state_directory, state)

            with patch(
                "session_run_health.commands.store_state",
                side_effect=crash_after_effect,
            ):
                with self.assertRaises(OSError):
                    handle_request_tick(
                        SimpleNamespace(
                            input={"through_event_id": "retirement-result"},
                            host=host,
                        )
                    )
            host.events.append(minimal_event("after-retirement-effect"))

            result = handle_request_tick(
                SimpleNamespace(
                    input={"through_event_id": "after-retirement-effect"},
                    host=host,
                )
            )
            state = load_state(directory)

        self.assertTrue(result["checkpoint_retired"])
        self.assertIsNone(state["pending_retirement"])
        self.assertEqual(host.plan_appends, 2)
        self.assertEqual(host.context_appends, 2)
        self.assertEqual(host._slots["session-run-health"], "")

    def _stored_pending(
        self,
        directory,
        *,
        plan_published,
        context_published,
        artifact_published=True,
    ):
        state = new_state()
        state["root_agent"] = "root"
        state["after_event_id"] = "base-1"
        self._seed_failure_track(state)
        pending = make_pending_checkpoint(
            state,
            [
                {
                    "kind": "repeated_failure",
                    "count": 3,
                    "event_ids": ["evidence-1"],
                    "_entity": "f" * 64,
                    "_sequence": 1,
                }
            ],
            "base-1",
            DEFAULT_THRESHOLDS,
        )
        pending["artifact_event_id"] = (
            "artifact-existing" if artifact_published else None
        )
        pending["plan_published"] = plan_published
        pending["context_published"] = context_published
        store_state(directory, state)
        return state

    def _active_checkpoint_host(self, directory):
        host = FakeHost(directory, fixture("repeated-malformed-patches.json"))
        result = handle_request_tick(
            SimpleNamespace(input={"through_event_id": "patch-04"}, host=host)
        )
        self.assertTrue(result["checkpoint_active"])
        self.assertTrue(
            load_state(directory)["active_checkpoint"]["context_published"]
        )
        return host

    def _clear_active_context(self, host, prefix):
        cutoff = f"{prefix}-cutoff"
        host.events.append(minimal_event(cutoff))
        original = ValueError(f"{prefix} ordinary failure")
        with patch.object(host, "query_provenance", side_effect=original):
            with self.assertRaises(ValueError) as raised:
                handle_request_tick(
                    SimpleNamespace(input={"through_event_id": cutoff}, host=host)
                )
        self.assertIs(raised.exception, original)
        self.assertEqual(host._slots["session-run-health"], "")

    @staticmethod
    def _seed_failure_track(state):
        family = hashlib.sha256(
            "tool-operation\x1fapply_patch".encode("utf-8")
        ).hexdigest()
        state["observed_sequence"] = 3
        state["latest_observed_event_id"] = "evidence-3"
        state["latest_observed_ts"] = "2026-07-30T00:00:03.000Z"
        state["failure_tracks"]["f" * 64] = {
            "family": family,
            "count": 3,
            "event_ids": ["evidence-1", "evidence-2", "evidence-3"],
            "alerted": True,
            "first_seen_sequence": 1,
            "last_seen_sequence": 3,
        }

    @staticmethod
    def _recovery_attempt(prefix):
        return [
            minimal_event(
                f"{prefix}-call",
                "tool.call",
                {
                    "id": f"{prefix}-tool",
                    "name": "apply_patch",
                    "input": {"patch": "omitted-recovery"},
                },
            ),
            minimal_event(
                f"{prefix}-result",
                "tool.result",
                {"id": f"{prefix}-tool", "name": "apply_patch", "ok": True},
            ),
        ]


if __name__ == "__main__":
    unittest.main()
