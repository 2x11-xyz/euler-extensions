import base64
from pathlib import Path
import sys
import unittest


TEST_DIRECTORY = Path(__file__).resolve().parent
REPOSITORY_DIRECTORY = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPOSITORY_DIRECTORY))

from tests.managed_process_harness import ManagedProcessPeer


class CanonicalSdkProtocolTests(unittest.TestCase):
    def peer(self, *, max_message_bytes=1024 * 1024):
        return ManagedProcessPeer(
            [sys.executable, "-B", "-u", str(TEST_DIRECTORY / "fixture_extension.py")],
            cwd=REPOSITORY_DIRECTORY,
            max_message_bytes=max_message_bytes,
        )

    def test_every_public_host_method_uses_the_versioned_wire_shape(self):
        with self.peer() as peer:
            peer.initialize()
            peer.invoke("exercise-host", {})

            self.assertEqual(
                peer.read(),
                {
                    "jsonrpc": "2.0",
                    "method": "euler/progress",
                    "params": {"message": "starting", "fraction": 0.25},
                },
            )

            query = peer.read()
            self.assertEqual(query["method"], "euler/host/query-provenance")
            self.assertEqual(
                query["params"],
                {
                    "after_event_id": "event-1",
                    "kinds": ["tool.result"],
                    "limit": 7,
                    "scan_limit": 19,
                    "include_blob_fields": True,
                    "blob_byte_limit": 1234,
                },
            )
            provenance = {"events": [{"id": "event-2"}], "truncated": False}
            peer.respond(query, result=provenance)

            diagnostics = peer.read()
            self.assertEqual(diagnostics["method"], "euler/host/read-diagnostics")
            self.assertEqual(
                diagnostics["params"],
                {"tail_lines": 5, "max_bytes": 600},
            )
            diagnostics_result = {"lines": ["one"], "truncated": False}
            peer.respond(diagnostics, result=diagnostics_result)

            state = peer.read()
            self.assertEqual(state["method"], "euler/host/state-dir")
            self.assertEqual(state["params"], {})
            peer.respond(state, result={"path": "/private/state"})

            artifact = peer.read()
            self.assertEqual(artifact["method"], "euler/host/write-artifact")
            self.assertEqual(
                artifact["params"],
                {
                    "display_name": "proof.json",
                    "media_type": "application/json",
                    "bytes_base64": base64.b64encode(b'{"ok":true}').decode("ascii"),
                    "source_event_ids": ["event-2"],
                    "metadata": {"kind": "proof"},
                },
            )
            artifact_result = {"persisted_event_id": "artifact-1"}
            peer.respond(artifact, result=artifact_result)

            load = peer.read()
            self.assertEqual(
                (load["method"], load["params"]),
                ("euler/host/load-checkpoint", {"name": "cursor"}),
            )
            peer.respond(load, result=None)

            store = peer.read()
            self.assertEqual(
                (store["method"], store["params"]),
                (
                    "euler/host/store-checkpoint",
                    {
                        "name": "cursor",
                        "checkpoint": {
                            "schema_version": 1,
                            "after_event_id": "event-2",
                        },
                    },
                ),
            )
            peer.respond(store)

            record = peer.read()
            self.assertEqual(
                (record["method"], record["params"]),
                (
                    "euler/host/record-agent-task-result",
                    {
                        "task": {
                            "task": "inspect",
                            "persona": "observer",
                            "provider": "",
                            "model": "",
                            "capabilities": [],
                            "budget": {},
                            "result_schema": None,
                        },
                        "result": {
                            "ok": True,
                            "summary": "done",
                            "output": "body",
                            "error": None,
                        },
                    },
                ),
            )
            record_result = {"result_event_id": "result-1"}
            peer.respond(record, result=record_result)

            slot = peer.read()
            self.assertEqual(
                (slot["method"], slot["params"]),
                (
                    "euler/host/update-context-slot",
                    {"slot": "fixture", "content": "bounded"},
                ),
            )
            peer.respond(slot)

            plan = peer.read()
            self.assertEqual(
                (plan["method"], plan["params"]),
                (
                    "euler/host/update-plan-presentation",
                    {
                        "revision": 3,
                        "status": "active",
                        "explanation": "Exercise the canonical client",
                        "items": [
                            {"step": "Inspect", "status": "completed"},
                            {"step": "Verify", "status": "in_progress"},
                        ],
                    },
                ),
            )
            peer.respond(plan)

            spawn = peer.read()
            self.assertEqual(
                (spawn["method"], spawn["params"]),
                (
                    "euler/host/spawn-agent",
                    {
                        "task": "one",
                        "persona": "",
                        "provider": "",
                        "model": "",
                        "system_prompt": "",
                        "explicit_context": None,
                        "include_parent_canvas": False,
                        "capabilities": [],
                        "max_turns": None,
                        "max_tool_calls": None,
                        "max_tokens": None,
                    },
                ),
            )
            child = {"child_agent_id": "child-1"}
            peer.respond(spawn, result=child)

            spawn_many = peer.read()
            self.assertEqual(
                (spawn_many["method"], spawn_many["params"]),
                (
                    "euler/host/spawn-agents",
                    {
                        "tasks": [
                            {
                                "task": "two",
                                "persona": "",
                                "provider": "",
                                "model": "",
                                "system_prompt": "",
                                "explicit_context": None,
                                "include_parent_canvas": False,
                                "capabilities": [],
                                "max_turns": None,
                                "max_tool_calls": None,
                                "max_tokens": None,
                            },
                            {
                                "task": "three",
                                "persona": "",
                                "provider": "",
                                "model": "",
                                "system_prompt": "",
                                "explicit_context": None,
                                "include_parent_canvas": False,
                                "capabilities": [],
                                "max_turns": None,
                                "max_tool_calls": None,
                                "max_tokens": None,
                            },
                        ],
                    },
                ),
            )
            children = [
                {"child_agent_id": "child-2"},
                {"child_agent_id": "child-3"},
            ]
            peer.respond(spawn_many, result=children)

            self.assertEqual(
                peer.read(),
                {
                    "jsonrpc": "2.0",
                    "method": "euler/progress",
                    "params": {"message": "done"},
                },
            )
            result = peer.read()
            self.assertEqual(result["id"], "command")
            self.assertEqual(
                result["result"],
                {
                    "provenance": provenance,
                    "diagnostics": diagnostics_result,
                    "state_directory": "/private/state",
                    "artifact": artifact_result,
                    "checkpoint": None,
                    "record": record_result,
                    "child": child,
                    "children": children,
                },
            )
            peer.finish()

    def test_host_errors_are_available_to_handlers(self):
        with self.peer() as peer:
            peer.initialize()
            peer.invoke("catch-host-error", {})
            request = peer.read()
            peer.respond(request, error="denied by fake host")
            self.assertEqual(
                peer.read()["result"],
                {"caught": "denied by fake host"},
            )
            peer.finish()

    def test_cancel_notification_terminates_the_command_cleanly(self):
        with self.peer() as peer:
            peer.initialize()
            peer.invoke("wait-for-cancel", {})
            request = peer.read()
            self.assertEqual(request["method"], "euler/host/query-provenance")
            peer.write(
                {
                    "jsonrpc": "2.0",
                    "method": "$/cancelRequest",
                    "params": {"id": "command"},
                }
            )
            error = peer.read()["error"]
            self.assertEqual(error["code"], -32800)
            self.assertEqual(error["message"], "extension command cancelled")
            peer.finish()

    def test_malformed_or_wrong_target_cancel_notifications_fail_closed(self):
        malformed_notifications = (
            {
                "jsonrpc": "2.0",
                "method": "$/cancelRequest",
                "params": {"id": "another-command"},
            },
            {"jsonrpc": "2.0", "method": "$/cancelRequest"},
            {
                "jsonrpc": "2.0",
                "method": "$/cancelRequest",
                "params": {},
            },
            {
                "jsonrpc": "2.0",
                "method": "$/cancelRequest",
                "params": [],
            },
            {
                "jsonrpc": "2.0",
                "method": "$/cancelRequest",
                "params": {"id": True},
            },
            {
                "jsonrpc": "2.0",
                "method": "$/cancelRequest",
                "params": {"id": 1.5},
            },
            {
                "jsonrpc": "2.0",
                "method": "$/cancelRequest",
                "params": {"id": -(1 << 63) - 1},
            },
            {
                "jsonrpc": "2.0",
                "method": "$/cancelRequest",
                "params": {"id": 1 << 64},
            },
            {
                "jsonrpc": "2.0",
                "method": "$/cancelRequest",
                "params": {"id": "command", "extra": True},
            },
            {
                "jsonrpc": "2.0",
                "id": "client-1",
                "method": "$/cancelRequest",
                "params": {"id": "command"},
            },
        )
        for notification in malformed_notifications:
            with self.subTest(notification=notification), self.peer() as peer:
                peer.initialize()
                peer.invoke("catch-inbound-protocol-error", {})
                request = peer.read()
                self.assertEqual(request["method"], "euler/host/state-dir")
                peer.write(notification)
                self.assertEqual(
                    peer.read()["result"],
                    {"caught": "invalid cancellation notification"},
                )
                peer.finish()

    def test_invalid_host_results_are_sanitized(self):
        with self.peer() as peer:
            peer.initialize()
            peer.invoke("invalid-state-result", {})
            request = peer.read()
            peer.respond(request, result={"not_path": True})
            error = peer.read()["error"]
            self.assertEqual(error["code"], -32000)
            self.assertEqual(error["message"], "extension command failed")
            peer.finish()

    def test_handler_failures_and_invalid_outputs_are_sanitized(self):
        for command in ("fail", "non-object"):
            with self.subTest(command=command), self.peer() as peer:
                peer.initialize()
                peer.invoke(command, {})
                error = peer.read()["error"]
                self.assertEqual(error["code"], -32000)
                self.assertEqual(error["message"], "extension command failed")
                self.assertNotIn("private implementation detail", str(error))
                peer.finish()

    def test_oversized_handler_output_is_sanitized(self):
        with self.peer(max_message_bytes=512) as peer:
            peer.initialize()
            peer.invoke("oversized", {})
            error = peer.read()["error"]
            self.assertEqual(error["code"], -32000)
            self.assertEqual(error["message"], "extension command failed")
            peer.finish()

    def test_non_finite_host_response_numbers_are_rejected(self):
        for number in ("NaN", "Infinity", "-Infinity", "1e309", "-1e309"):
            with self.subTest(number=number), self.peer() as peer:
                peer.initialize()
                peer.invoke("catch-inbound-protocol-error", {})
                request = peer.read()
                peer.write_raw(
                    (
                        '{"jsonrpc":"2.0","id":'
                        f'"{request["id"]}","result":{number}'
                        "}\n"
                    )
                )
                self.assertEqual(
                    peer.read()["result"],
                    {"caught": "invalid protocol message"},
                )
                peer.finish()

    def test_exponent_overflow_in_command_input_is_rejected(self):
        for number in ("1e309", "-1e309"):
            with self.subTest(number=number), self.peer() as peer:
                peer.initialize()
                peer.write_raw(
                    (
                        '{"jsonrpc":"2.0","id":"command",'
                        '"method":"euler/command","params":'
                        '{"command":"echo-input","input":{"value":'
                        + number
                        + "}}}\n"
                    )
                )
                return_code = peer.wait()
                self.assertNotEqual(return_code, 0)
                self.assertIn("invalid protocol message", peer.stderr())

    def test_finite_decimal_command_input_round_trips(self):
        with self.peer() as peer:
            peer.initialize()
            peer.write_raw(
                (
                    '{"jsonrpc":"2.0","id":"command",'
                    '"method":"euler/command","params":'
                    '{"command":"echo-input","input":{"value":1.25e2}}}\n'
                )
            )
            self.assertEqual(
                peer.read()["result"],
                {"input": {"value": 125.0}},
            )
            peer.finish()

    def test_outbound_non_finite_numbers_raise_protocol_error(self):
        for kind in ("nan", "positive-infinity", "negative-infinity"):
            with self.subTest(kind=kind), self.peer() as peer:
                peer.initialize()
                peer.invoke("catch-outbound-protocol-error", {"kind": kind})
                self.assertEqual(
                    peer.read()["result"],
                    {"caught": "invalid protocol message"},
                )
                peer.finish()

    def test_invalid_request_ids_are_rejected(self):
        request_ids = (
            "true",
            "false",
            "null",
            "1.0",
            "1.5",
            str(-(1 << 63) - 1),
            str(1 << 64),
        )
        for request_id in request_ids:
            with self.subTest(request_id=request_id), self.peer() as peer:
                peer.write_raw(
                    (
                        '{"jsonrpc":"2.0","id":'
                        f"{request_id},"
                        '"method":"initialize","params":'
                        '{"protocol_versions":["euler-managed-process/1"]}}\n'
                    )
                )
                return_code = peer.wait()
                self.assertNotEqual(return_code, 0)
                self.assertIn("expected initialize request", peer.stderr())

    def test_string_and_bounded_integer_request_ids_are_preserved(self):
        request_ids = (
            ('"request-id"', "request-id"),
            (str(-(1 << 63)), -(1 << 63)),
            (str((1 << 64) - 1), (1 << 64) - 1),
        )
        for encoded_id, expected_id in request_ids:
            with self.subTest(request_id=encoded_id), self.peer() as peer:
                peer.write_raw(
                    (
                        '{"jsonrpc":"2.0","id":'
                        f"{encoded_id},"
                        '"method":"initialize","params":'
                        '{"protocol_versions":["euler-managed-process/999"]}}\n'
                    )
                )
                response = peer.read()
                self.assertEqual(response["id"], expected_id)
                self.assertEqual(response["error"]["code"], -32602)
                peer.wait(expected=0)

    def test_protocol_versions_must_be_an_array_of_nonempty_strings(self):
        malformed_versions = (
            "euler-managed-process/1",
            {},
            1,
            True,
            None,
            [],
            [1],
            [""],
            ["euler-managed-process/1", 1],
            ["euler-managed-process/1", ""],
        )
        for versions in malformed_versions:
            with self.subTest(versions=versions), self.peer() as peer:
                peer.write(
                    {
                        "jsonrpc": "2.0",
                        "id": "initialize",
                        "method": "initialize",
                        "params": {"protocol_versions": versions},
                    }
                )
                response = peer.read()
                self.assertEqual(response["id"], "initialize")
                self.assertEqual(response["error"]["code"], -32602)
                peer.wait(expected=0)

    def test_negotiated_framing_limit_is_enforced(self):
        with self.peer(max_message_bytes=128) as peer:
            peer.initialize()
            peer.write_raw(("x" * 130) + "\n")
            return_code = peer.wait()
            self.assertNotEqual(return_code, 0)
            self.assertIn("invalid protocol framing", peer.stderr())


if __name__ == "__main__":
    unittest.main()
