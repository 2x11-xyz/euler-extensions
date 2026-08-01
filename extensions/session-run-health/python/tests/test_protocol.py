import base64
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


EXTENSION_DIRECTORY = Path(__file__).resolve().parents[2]
REPOSITORY_DIRECTORY = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPOSITORY_DIRECTORY))

from tests.managed_process_harness import ManagedProcessPeer


class ManagedProcessProtocolTests(unittest.TestCase):
    def peer(self):
        return ManagedProcessPeer(
            ["python3", "-B", "-u", "python/extension.py"],
            cwd=EXTENSION_DIRECTORY,
        )

    def acknowledge_context_cleanup(self, peer):
        cleanup = peer.read()
        self.assertEqual(cleanup["method"], "euler/host/update-context-slot")
        self.assertEqual(
            cleanup["params"],
            {"slot": "session-run-health", "content": ""},
        )
        peer.respond(cleanup)

    def test_request_tick_uses_the_exact_host_cutoff_and_excludes_reasoning(self):
        with TemporaryDirectory() as state_directory, self.peer() as peer:
            peer.initialize()
            peer.invoke("assess-run-health", {"through_event_id": "cutoff-1"})

            state = peer.read()
            self.assertEqual(state["method"], "euler/host/state-dir")
            peer.respond(state, result={"path": state_directory})

            query = peer.read()
            self.assertEqual(query["method"], "euler/host/query-provenance")
            self.assertEqual(query["params"]["through_event_id"], "cutoff-1")
            self.assertEqual(query["params"]["limit"], 8)
            self.assertFalse(query["params"]["include_blob_fields"])
            self.assertNotIn("model.reasoning", query["params"]["kinds"])
            self.assertNotIn("model.delta", query["params"]["kinds"])
            peer.respond(
                query,
                result={
                    "events": [],
                    "watermark_event_id": "cutoff-1",
                    "next_after_event_id": None,
                    "truncated": False,
                },
            )

            result = peer.read()["result"]
            self.assertEqual(result["assessed_through_event_id"], "cutoff-1")
            self.assertTrue(result["caught_up"])
            self.assertFalse(result["checkpoint_published"])
            peer.finish()

    def test_content_heavy_page_has_large_managed_frame_headroom(self):
        events = []
        for index in range(8):
            event_id = "heavy-cutoff" if index == 7 else f"heavy-{index}"
            events.append(
                {
                    "v": 1,
                    "id": event_id,
                    "ts": f"2026-07-30T00:0{index}:00.000Z",
                    "session": "fixture",
                    "agent": "root",
                    "parent": None,
                    "kind": "model.result",
                    "payload": {
                        "provider": "fixture",
                        "model": "fixture",
                        "content": "x" * 8192,
                        "tool_calls": [],
                        "stop_reason": "tool",
                    },
                    "blobs": {},
                }
            )
        with TemporaryDirectory() as state_directory, self.peer() as peer:
            peer.initialize()
            peer.invoke("assess-run-health", {"through_event_id": "heavy-cutoff"})
            state = peer.read()
            peer.respond(state, result={"path": state_directory})
            query = peer.read()
            self.assertEqual(query["params"]["limit"], 8)
            page = {
                "events": events,
                "watermark_event_id": "heavy-cutoff",
                "next_after_event_id": None,
                "truncated": False,
            }
            response_frame = {
                "jsonrpc": "2.0",
                "id": query["id"],
                "result": page,
            }
            self.assertLess(
                len(json.dumps(response_frame, separators=(",", ":")).encode("utf-8")),
                128 * 1024,
            )
            peer.respond(query, result=page)

            result = peer.read()["result"]
            self.assertTrue(result["caught_up"])
            self.assertEqual(result["pages"], 1)
            peer.finish()

    def test_page_continuation_must_exactly_match_truncation_contract(self):
        malformed_pages = (
            {
                "events": [],
                "watermark_event_id": "page-1",
                "next_after_event_id": "page-skipped",
                "truncated": True,
            },
            {
                "events": [],
                "watermark_event_id": "cutoff-1",
                "next_after_event_id": "unexpected",
                "truncated": False,
            },
        )
        for page in malformed_pages:
            with self.subTest(page=page), TemporaryDirectory() as state_directory:
                with self.peer() as peer:
                    peer.initialize()
                    peer.invoke(
                        "assess-run-health",
                        {"through_event_id": "cutoff-1"},
                    )
                    state = peer.read()
                    peer.respond(state, result={"path": state_directory})
                    query = peer.read()
                    peer.respond(query, result=page)

                    self.acknowledge_context_cleanup(peer)
                    error = peer.read()["error"]
                    self.assertEqual(error["code"], -32000)
                    self.assertEqual(error["message"], "extension command failed")
                    peer.finish()

    def test_tick_input_is_closed(self):
        with self.peer() as peer:
            peer.initialize()
            peer.invoke(
                "assess-run-health",
                {"through_event_id": "cutoff-1", "unexpected": True},
            )
            self.acknowledge_context_cleanup(peer)
            error = peer.read()["error"]
            self.assertEqual(error["code"], -32000)
            self.assertEqual(error["message"], "extension command failed")
            peer.finish()

    def test_host_cancellation_does_not_issue_context_cleanup(self):
        with TemporaryDirectory() as state_directory, self.peer() as peer:
            peer.initialize()
            peer.invoke("assess-run-health", {"through_event_id": "cutoff-1"})
            state = peer.read()
            peer.respond(state, result={"path": state_directory})
            query = peer.read()
            self.assertEqual(query["method"], "euler/host/query-provenance")

            peer.write(
                {
                    "jsonrpc": "2.0",
                    "method": "$/cancelRequest",
                    "params": {"id": "command"},
                }
            )

            response = peer.read()
            self.assertEqual(
                response["error"],
                {
                    "code": -32800,
                    "message": "extension command cancelled",
                },
            )
            peer.finish()

    def test_strategy_checkpoint_uses_existing_bounded_host_surfaces(self):
        fixture_path = Path(__file__).resolve().parent / "fixtures/repeated-malformed-patches.json"
        events = json.loads(fixture_path.read_text(encoding="utf-8"))
        private_text = (
            "PRIVATE USER REQUEST",
            "PRIVATE MODEL REASONING",
            "PRIVATE TOOL INPUT",
            "PRIVATE TOOL CALL ID",
            "first raw error",
        )
        with TemporaryDirectory() as state_directory, self.peer() as peer:
            peer.initialize()
            peer.invoke("assess-run-health", {"through_event_id": "patch-04"})

            state = peer.read()
            peer.respond(state, result={"path": state_directory})
            query = peer.read()
            selected = [
                event for event in events if event["kind"] in query["params"]["kinds"]
            ]
            peer.respond(
                query,
                result={
                    "events": selected,
                    "watermark_event_id": "patch-04",
                    "next_after_event_id": None,
                    "truncated": False,
                },
            )

            artifact = peer.read()
            self.assertEqual(artifact["method"], "euler/host/write-artifact")
            artifact_text = base64.b64decode(
                artifact["params"]["bytes_base64"]
            ).decode("utf-8")
            self.assertEqual(
                artifact["params"]["metadata"]["kind"],
                "run-health-checkpoint",
            )
            for text in private_text:
                self.assertNotIn(text, artifact_text)
            peer.respond(artifact, result={"persisted_event_id": "artifact-1"})

            plan = peer.read()
            self.assertEqual(plan["method"], "euler/host/update-plan-presentation")
            self.assertEqual(plan["params"]["status"], "active")
            peer.respond(plan)

            slot = peer.read()
            self.assertEqual(slot["method"], "euler/host/update-context-slot")
            self.assertEqual(slot["params"]["slot"], "session-run-health")
            for text in private_text:
                self.assertNotIn(text, slot["params"]["content"])
            peer.respond(slot)

            result = peer.read()["result"]
            self.assertTrue(result["checkpoint_published"])
            self.assertFalse(result["checkpoint_pending"])
            peer.finish()


if __name__ == "__main__":
    unittest.main()
