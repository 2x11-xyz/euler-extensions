from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


EXTENSION_DIRECTORY = Path(__file__).resolve().parents[2]
REPOSITORY_DIRECTORY = Path(__file__).resolve().parents[4]
PYTHON_DIRECTORY = EXTENSION_DIRECTORY / "python"
SDK_SOURCE = (
    REPOSITORY_DIRECTORY
    / "sdks"
    / "python"
    / "euler-managed-process-sdk"
    / "src"
)
sys.path.insert(0, str(REPOSITORY_DIRECTORY))
sys.path.insert(0, str(SDK_SOURCE))
sys.path.insert(0, str(PYTHON_DIRECTORY))

from plan_todo.plan import load_plan, replace_plan
from tests.managed_process_harness import ManagedProcessPeer


class ManagedProcessProtocolTests(unittest.TestCase):
    def peer(self):
        manifest_argv = ["python3", "-B", "-u", "python/extension.py"]
        return ManagedProcessPeer(manifest_argv, cwd=EXTENSION_DIRECTORY)

    def test_update_plan_uses_canonical_sdk_and_host_methods(self):
        with TemporaryDirectory() as state_directory, self.peer() as peer:
            peer.initialize()
            peer.invoke(
                "update-plan",
                {
                    "plan_status": "active",
                    "plan": [
                        {"step": "Inspect", "status": "completed"},
                        {"step": "Implement", "status": "in_progress"},
                    ],
                },
            )

            self._provide_state_directory(peer, state_directory)
            plan = peer.read()
            self.assertEqual(
                plan["method"],
                "euler/host/update-plan-presentation",
            )
            self.assertEqual(
                plan["params"],
                {
                    "revision": 1,
                    "status": "active",
                    "explanation": None,
                    "items": [
                        {"step": "Inspect", "status": "completed"},
                        {"step": "Implement", "status": "in_progress"},
                    ],
                },
            )
            peer.respond(plan)

            slot = peer.read()
            self.assertEqual(slot["method"], "euler/host/update-context-slot")
            self.assertEqual(slot["params"]["slot"], "plan")
            self.assertIn("2. [>] Implement", slot["params"]["content"])
            peer.respond(slot)

            result = peer.read()["result"]
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
            self.assertEqual(load_plan(state_directory).revision, 1)
            peer.finish()

    def test_update_survives_best_effort_projection_failures(self):
        cases = ("presentation", "context")
        for rejected_projection in cases:
            with self.subTest(rejected_projection=rejected_projection):
                with TemporaryDirectory() as state_directory, self.peer() as peer:
                    peer.initialize()
                    peer.invoke(
                        "update-plan",
                        {
                            "plan_status": "active",
                            "plan": [
                                {"step": "Continue", "status": "in_progress"}
                            ],
                        },
                    )
                    self._provide_state_directory(peer, state_directory)

                    plan = peer.read()
                    self.assertEqual(
                        plan["method"],
                        "euler/host/update-plan-presentation",
                    )
                    if rejected_projection == "presentation":
                        peer.respond(plan, error="presentation unavailable")
                    else:
                        peer.respond(plan)

                    slot = peer.read()
                    self.assertEqual(
                        slot["method"],
                        "euler/host/update-context-slot",
                    )
                    if rejected_projection == "context":
                        peer.respond(slot, error="context unavailable")
                    else:
                        peer.respond(slot)

                    result = peer.read()["result"]
                    self.assertTrue(result["accepted"])
                    self.assertEqual(
                        result["presentation_published"],
                        rejected_projection != "presentation",
                    )
                    self.assertEqual(
                        result["context_published"],
                        rejected_projection != "context",
                    )
                    self.assertIsNotNone(load_plan(state_directory))
                    peer.finish()

    def test_update_plan_returns_model_visible_semantic_rejection(self):
        with TemporaryDirectory() as state_directory, self.peer() as peer:
            peer.initialize()
            peer.invoke(
                "update-plan",
                {
                    "plan_status": "active",
                    "plan": [
                        {"step": "One", "status": "in_progress"},
                        {"step": "Two", "status": "in_progress"},
                    ],
                },
            )
            self._provide_state_directory(peer, state_directory)

            result = peer.read()["result"]
            self.assertFalse(result["accepted"])
            self.assertIn("at most one in_progress", result["error"])
            self.assertIsNone(load_plan(state_directory))
            peer.finish()

    def test_cancellation_is_not_swallowed_by_best_effort_projection(self):
        with TemporaryDirectory() as state_directory, self.peer() as peer:
            peer.initialize()
            peer.invoke(
                "update-plan",
                {
                    "plan_status": "active",
                    "plan": [{"step": "Continue", "status": "in_progress"}],
                },
            )
            self._provide_state_directory(peer, state_directory)
            presentation = peer.read()
            self.assertEqual(
                presentation["method"],
                "euler/host/update-plan-presentation",
            )
            peer.write(
                {
                    "jsonrpc": "2.0",
                    "method": "$/cancelRequest",
                    "params": {"id": "command"},
                }
            )

            error = peer.read()["error"]
            self.assertEqual(error["code"], -32800)
            self.assertIsNotNone(load_plan(state_directory))
            peer.finish()

    def test_idle_returns_exact_continue_envelope_despite_projection_failure(self):
        with TemporaryDirectory() as state_directory:
            replace_plan(
                state_directory,
                {
                    "plan_status": "active",
                    "plan": [{"step": "Finish tests", "status": "pending"}],
                },
            )
            with self.peer() as peer:
                peer.initialize()
                peer.invoke("continue-if-needed", {})
                self._provide_state_directory(peer, state_directory)

                plan = peer.read()
                self.assertEqual(
                    plan["method"],
                    "euler/host/update-plan-presentation",
                )
                self.assertEqual(plan["params"]["revision"], 1)
                peer.respond(plan)

                slot = peer.read()
                self.assertEqual(slot["method"], "euler/host/update-context-slot")
                peer.respond(slot, error="context slot quota reached")
                result = peer.read()["result"]
                self.assertEqual(set(result), {"action", "input"})
                self.assertEqual(result["action"], "continue")
                self.assertIn("Finish tests", result["input"])
                peer.finish()

    def test_idle_clears_context_when_state_is_missing_or_complete(self):
        for complete in (False, True):
            with self.subTest(complete=complete):
                with TemporaryDirectory() as state_directory, self.peer() as peer:
                    if complete:
                        replace_plan(
                            state_directory,
                            {
                                "plan_status": "active",
                                "plan": [{"step": "Done", "status": "completed"}],
                            },
                        )
                    peer.initialize()
                    peer.invoke("continue-if-needed", {})
                    self._provide_state_directory(peer, state_directory)

                    if complete:
                        plan = peer.read()
                        self.assertEqual(
                            plan["method"],
                            "euler/host/update-plan-presentation",
                        )
                        self.assertEqual(plan["params"]["status"], "completed")
                        peer.respond(plan)

                    slot = peer.read()
                    self.assertEqual(
                        slot["method"],
                        "euler/host/update-context-slot",
                    )
                    self.assertEqual(slot["params"]["content"], "")
                    peer.respond(slot)
                    self.assertEqual(peer.read()["result"], {"action": "stop"})
                    peer.finish()

    def test_corrupt_state_clears_context_then_fails_closed(self):
        with TemporaryDirectory() as state_directory:
            (Path(state_directory) / "plan.json").write_text("{", encoding="utf-8")
            with self.peer() as peer:
                peer.initialize()
                peer.invoke("continue-if-needed", {})
                self._provide_state_directory(peer, state_directory)

                slot = peer.read()
                self.assertEqual(slot["method"], "euler/host/update-context-slot")
                self.assertEqual(slot["params"]["content"], "")
                peer.respond(slot)
                error = peer.read()["error"]
                self.assertEqual(error["code"], -32000)
                self.assertEqual(error["message"], "extension command failed")
                peer.finish()

    def test_update_over_corrupt_state_clears_context_and_rejects(self):
        with TemporaryDirectory() as state_directory:
            (Path(state_directory) / "plan.json").write_text("{", encoding="utf-8")
            with self.peer() as peer:
                peer.initialize()
                peer.invoke(
                    "update-plan",
                    {
                        "plan_status": "active",
                        "plan": [{"step": "Replacement", "status": "pending"}],
                    },
                )
                self._provide_state_directory(peer, state_directory)

                slot = peer.read()
                self.assertEqual(slot["method"], "euler/host/update-context-slot")
                self.assertEqual(slot["params"]["content"], "")
                peer.respond(slot)
                result = peer.read()["result"]
                self.assertFalse(result["accepted"])
                self.assertIn("durable plan", result["error"])
                peer.finish()

    def _provide_state_directory(self, peer, state_directory):
        request = peer.read()
        self.assertEqual(request["method"], "euler/host/state-dir")
        peer.respond(request, result={"path": state_directory})


if __name__ == "__main__":
    unittest.main()
