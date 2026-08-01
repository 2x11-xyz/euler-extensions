import json
from copy import deepcopy
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


PYTHON_DIRECTORY = Path(__file__).resolve().parents[1]
REPOSITORY_DIRECTORY = Path(__file__).resolve().parents[4]
SDK_SOURCE = REPOSITORY_DIRECTORY / "sdks/python/euler-managed-process-sdk/src"
sys.path.insert(0, str(SDK_SOURCE))
sys.path.insert(0, str(PYTHON_DIRECTORY))

from session_run_health.config import (
    CONFIG_FILENAME,
    ConfigurationError,
    DEFAULT_THRESHOLDS,
    load_thresholds,
)
from session_run_health.policy import (
    activate_pending_checkpoint,
    make_pending_checkpoint,
)
from session_run_health.state import (
    DurableStateError,
    STATE_FILENAME,
    load_state,
    new_state,
    store_state,
)


class ConfigurationAndStateTests(unittest.TestCase):
    def test_missing_config_uses_explicit_defaults_and_partial_override_is_closed(self):
        with TemporaryDirectory() as directory:
            self.assertEqual(load_thresholds(directory), DEFAULT_THRESHOLDS)
            Path(directory, CONFIG_FILENAME).write_text(
                json.dumps({"schema_version": 1, "failure_recurrence": 4}),
                encoding="utf-8",
            )
            configured = load_thresholds(directory)
            self.assertEqual(configured["failure_recurrence"], 4)
            self.assertEqual(
                configured["no_progress_seconds"],
                DEFAULT_THRESHOLDS["no_progress_seconds"],
            )

            Path(directory, CONFIG_FILENAME).write_text(
                json.dumps({"schema_version": 1, "round_cap": 10}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "unknown fields"):
                load_thresholds(directory)

    def test_configuration_rejects_boolean_and_out_of_range_thresholds(self):
        with TemporaryDirectory() as directory:
            path = Path(directory, CONFIG_FILENAME)
            for value in (True, 1, 21):
                with self.subTest(value=value):
                    path.write_text(
                        json.dumps(
                            {"schema_version": 1, "failure_recurrence": value}
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaises(ConfigurationError):
                        load_thresholds(directory)

    def test_state_round_trips_and_corrupt_state_fails_closed(self):
        with TemporaryDirectory() as directory:
            state = new_state()
            state["after_event_id"] = "event-1"
            store_state(directory, state)
            self.assertEqual(load_state(directory), state)

            Path(directory, STATE_FILENAME).write_text("{}", encoding="utf-8")
            with self.assertRaises(DurableStateError):
                load_state(directory)

    def test_nested_state_cannot_smuggle_payload_text_or_unknown_fields(self):
        with TemporaryDirectory() as directory:
            state = new_state()
            state["failure_tracks"]["a" * 64] = {
                "family": "b" * 64,
                "count": 3,
                "event_ids": ["event-1"],
                "alerted": True,
                "raw_error": "must never become durable state",
            }
            Path(directory, STATE_FILENAME).write_text(
                json.dumps(state),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(DurableStateError, "failure track"):
                load_state(directory)

    def test_usage_target_state_is_closed_and_content_free(self):
        invalid_states = []

        missing_target_field = new_state()
        del missing_target_field["usage_target_fingerprint"]
        invalid_states.append((missing_target_field, "incompatible shape"))

        invalid_target = new_state()
        invalid_target["usage_target_fingerprint"] = "provider/model"
        invalid_states.append((invalid_target, "usage target fingerprint"))

        sample_without_target = new_state()
        sample_without_target["root_agent"] = "root"
        sample_without_target["observed_sequence"] = 1
        sample_without_target["latest_observed_event_id"] = "sample-1"
        sample_without_target["latest_observed_ts"] = "2026-07-30T00:00:00.000Z"
        sample_without_target["usage_samples"] = [
            {
                "event_id": "sample-1",
                "input_tokens": 8_000,
                "cached_tokens": 0,
                "sequence": 1,
            }
        ]
        invalid_states.append((sample_without_target, "no model target"))

        for index, (state, message) in enumerate(invalid_states):
            with self.subTest(case=index), TemporaryDirectory() as directory:
                self._write_raw(directory, state)
                with self.assertRaisesRegex(DurableStateError, message):
                    load_state(directory)

    def test_unowned_state_cannot_retain_root_observations(self):
        state = new_state()
        state["observed_sequence"] = 1
        state["latest_observed_event_id"] = "legacy-error"
        state["latest_observed_ts"] = "2026-07-30T00:00:00.000Z"

        with TemporaryDirectory() as directory:
            self._write_raw(directory, state)
            with self.assertRaisesRegex(DurableStateError, "unowned state"):
                load_state(directory)

    def test_checkpoint_signature_is_recomputed_from_durable_fields(self):
        with TemporaryDirectory() as directory:
            state = self._checkpoint_state()
            state["pending_checkpoint"]["signals"][0]["count"] = 4
            self._write_raw(directory, state)

            with self.assertRaisesRegex(DurableStateError, "signature"):
                load_state(directory)

    def test_checkpoint_revision_zero_and_overlapping_transitions_fail_closed(self):
        with TemporaryDirectory() as directory:
            revision_zero = self._checkpoint_state()
            revision_zero["revision"] = 0
            revision_zero["pending_checkpoint"]["revision"] = 0
            self._write_raw(directory, revision_zero)
            with self.assertRaisesRegex(DurableStateError, "revision"):
                load_state(directory)

            overlapping = self._checkpoint_state()
            pending = overlapping["pending_checkpoint"]
            overlapping["active_checkpoint"] = {
                key: deepcopy(pending[key])
                for key in (
                    "revision",
                    "signature",
                    "cutoff_event_id",
                    "signals",
                    "thresholds",
                )
            }
            self._write_raw(directory, overlapping)
            with self.assertRaisesRegex(DurableStateError, "overlap"):
                load_state(directory)

    def test_checkpoint_effect_ordering_and_retirement_order_fail_closed(self):
        invalid_states = []

        context_without_plan = self._checkpoint_state()
        context_without_plan["pending_checkpoint"]["artifact_event_id"] = "artifact-1"
        context_without_plan["pending_checkpoint"]["context_published"] = True
        invalid_states.append((context_without_plan, "context"))

        plan_without_artifact = self._checkpoint_state()
        plan_without_artifact["pending_checkpoint"]["plan_published"] = True
        invalid_states.append((plan_without_artifact, "artifact"))

        retirement_out_of_order = self._checkpoint_state()
        signature = retirement_out_of_order["pending_checkpoint"]["signature"]
        retirement_out_of_order["pending_checkpoint"] = None
        retirement_out_of_order["revision"] = 2
        retirement_out_of_order["pending_retirement"] = {
            "revision": 2,
            "source_signature": signature,
            "plan_completed": True,
            "context_cleared": False,
        }
        invalid_states.append((retirement_out_of_order, "out of order"))

        for index, (state, message) in enumerate(invalid_states):
            with self.subTest(case=index), TemporaryDirectory() as directory:
                self._write_raw(directory, state)
                with self.assertRaisesRegex(DurableStateError, message):
                    load_state(directory)

    def test_fully_published_checkpoint_can_become_valid_active_state(self):
        with TemporaryDirectory() as directory:
            state = self._checkpoint_state()
            pending = state["pending_checkpoint"]
            pending["artifact_event_id"] = "artifact-1"
            pending["plan_published"] = True
            pending["context_published"] = True
            activate_pending_checkpoint(state)

            store_state(directory, state)

            self.assertEqual(load_state(directory), state)
            self.assertTrue(state["active_checkpoint"]["context_published"])

    def test_active_checkpoint_context_publication_state_is_closed(self):
        cases = ((None, "active checkpoint"), ("yes", "context publication"))
        for replacement, message in cases:
            with self.subTest(replacement=replacement):
                with TemporaryDirectory() as directory:
                    state = self._checkpoint_state()
                    pending = state["pending_checkpoint"]
                    pending["artifact_event_id"] = "artifact-1"
                    pending["plan_published"] = True
                    pending["context_published"] = True
                    activate_pending_checkpoint(state)
                    if replacement is None:
                        del state["active_checkpoint"]["context_published"]
                    else:
                        state["active_checkpoint"]["context_published"] = replacement
                    self._write_raw(directory, state)

                    with self.assertRaisesRegex(DurableStateError, message):
                        load_state(directory)

    @staticmethod
    def _checkpoint_state():
        state = new_state()
        state["root_agent"] = "root"
        state["observed_sequence"] = 3
        state["latest_observed_event_id"] = "failure-3"
        state["latest_observed_ts"] = "2026-07-30T00:00:03.000Z"
        state["after_event_id"] = "cutoff-1"
        state["failure_tracks"]["a" * 64] = {
            "family": "b" * 64,
            "count": 3,
            "event_ids": ["failure-1", "failure-2", "failure-3"],
            "alerted": True,
            "first_seen_sequence": 1,
            "last_seen_sequence": 3,
        }
        make_pending_checkpoint(
            state,
            [
                {
                    "kind": "repeated_failure",
                    "count": 3,
                    "event_ids": ["failure-1", "failure-2", "failure-3"],
                    "_entity": "a" * 64,
                    "_sequence": 1,
                }
            ],
            "cutoff-1",
            DEFAULT_THRESHOLDS,
        )
        return state

    @staticmethod
    def _write_raw(directory, state):
        Path(directory, STATE_FILENAME).write_text(
            json.dumps(state),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
