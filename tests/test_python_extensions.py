import base64
import json
from pathlib import Path
import unittest


from tests.managed_process_harness import ManagedProcessPeer


REPOSITORY_DIRECTORY = Path(__file__).resolve().parents[1]


class MigratedPythonExtensionTests(unittest.TestCase):
    def test_migrated_examples_launch_from_their_manifest_working_directory(self):
        cases = (
            {
                "package": "python-proof",
                "command": "inspect",
                "query": {"limit": 8, "scan_limit": 32},
                "artifact": "python-proof-summary.json",
            },
            {
                "package": "python-session-summary",
                "command": "summarize",
                "query": {"limit": 256, "scan_limit": 1024},
                "artifact": "python-session-summary.json",
            },
        )
        for case in cases:
            with self.subTest(package=case["package"]):
                self._exercise(case)

    def _exercise(self, case):
        package_directory = REPOSITORY_DIRECTORY / "extensions" / case["package"]
        manifest = json.loads(
            (package_directory / "Euler.extension.json").read_text(encoding="utf-8")
        )
        argv = manifest["entrypoint"]["command"]
        with ManagedProcessPeer(argv, cwd=package_directory) as peer:
            peer.initialize()
            peer.invoke(case["command"], {})

            progress = peer.read()
            self.assertEqual(progress["method"], "euler/progress")

            query = peer.read()
            self.assertEqual(query["method"], "euler/host/query-provenance")
            self.assertEqual(query["params"]["limit"], case["query"]["limit"])
            self.assertEqual(
                query["params"]["scan_limit"],
                case["query"]["scan_limit"],
            )
            peer.respond(
                query,
                result={
                    "events": [
                        {"id": "event-1", "kind": "tool.call"},
                        {"id": "event-2", "kind": "tool.result"},
                    ],
                    "truncated": False,
                },
            )

            artifact = peer.read()
            self.assertEqual(artifact["method"], "euler/host/write-artifact")
            self.assertEqual(
                artifact["params"]["display_name"],
                case["artifact"],
            )
            self.assertEqual(
                artifact["params"]["media_type"],
                "application/json",
            )
            artifact_bytes = base64.b64decode(artifact["params"]["bytes_base64"])
            self.assertTrue(artifact_bytes)
            json.loads(artifact_bytes)
            artifact_result = {
                "persisted_event_id": "artifact-1",
                "relative_path": "extensions/artifact-1",
                "sha256": "0" * 64,
                "byte_len": len(artifact_bytes),
            }
            peer.respond(artifact, result=artifact_result)

            done = peer.read()
            self.assertEqual(done["method"], "euler/progress")
            result = peer.read()
            self.assertEqual(result["id"], "command")
            self.assertEqual(result["result"]["artifact"], artifact_result)
            peer.finish()


if __name__ == "__main__":
    unittest.main()
