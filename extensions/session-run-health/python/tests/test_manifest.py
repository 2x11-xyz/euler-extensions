import json
from pathlib import Path
import unittest


EXTENSION_DIRECTORY = Path(__file__).resolve().parents[2]
REPOSITORY_DIRECTORY = EXTENSION_DIRECTORY.parents[1]


class ManifestTests(unittest.TestCase):
    def test_declares_one_agent_only_request_tick_and_exact_capabilities(self):
        manifest = json.loads(
            (EXTENSION_DIRECTORY / "Euler.extension.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["request_tick"], {"command": "assess-run-health"})
        self.assertEqual(len(manifest["commands"]), 1)
        command = manifest["commands"][0]
        self.assertEqual(command["name"], "assess-run-health")
        self.assertEqual(command["invocation"], "agent-only")
        expected = {
            "provenance-read",
            "extension-state",
            "artifact-write",
            "context-slot",
            "plan-presentation",
        }
        self.assertEqual(set(manifest["capabilities"]), expected)
        self.assertEqual(set(command["required_capabilities"]), expected)
        self.assertNotIn("model_tool", command)
        self.assertNotIn("idle_contribution", manifest)

    def test_extension_uses_the_single_repository_python_sdk(self):
        sdk = (
            REPOSITORY_DIRECTORY
            / "sdks/python/euler-managed-process-sdk/src/euler_managed_process_sdk/server.py"
        )
        self.assertTrue(sdk.is_file())
        self.assertFalse((EXTENSION_DIRECTORY / "python/_sdk").exists())


if __name__ == "__main__":
    unittest.main()
