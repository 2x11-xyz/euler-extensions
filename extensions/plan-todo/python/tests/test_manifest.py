import json
from pathlib import Path
import unittest


EXTENSION_DIRECTORY = Path(__file__).resolve().parents[2]
REPOSITORY_DIRECTORY = EXTENSION_DIRECTORY.parents[1]
MANIFEST_PATH = EXTENSION_DIRECTORY / "Euler.extension.json"


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_declares_agent_only_model_tool_and_idle_owner(self):
        self.assertEqual(
            self.manifest["entrypoint"]["command"],
            ["python3", "-B", "-u", "python/extension.py"],
        )
        commands = {
            command["name"]: command for command in self.manifest["commands"]
        }
        update = commands["update-plan"]
        idle = commands["continue-if-needed"]

        self.assertEqual(update["invocation"], "agent-only")
        self.assertEqual(idle["invocation"], "agent-only")
        self.assertEqual(update["model_tool"]["name"], "update_plan")
        self.assertIn("without waiting for an explicit goal", update["model_tool"]["description"])
        self.assertIn(
            "may retain the current in_progress item",
            update["model_tool"]["description"],
        )
        self.assertEqual(
            self.manifest["idle_contribution"],
            {"command": "continue-if-needed"},
        )
        expected_capabilities = {
            "extension-state",
            "context-slot",
            "plan-presentation",
        }
        self.assertEqual(set(self.manifest["capabilities"]), expected_capabilities)
        self.assertEqual(
            set(update["required_capabilities"]), expected_capabilities
        )
        self.assertEqual(
            set(idle["required_capabilities"]),
            {"extension-state", "context-slot", "plan-presentation"},
        )

    def test_model_input_schema_is_recursively_closed_and_bounded(self):
        schema = next(
            command["model_tool"]["input_schema"]
            for command in self.manifest["commands"]
            if command["name"] == "update-plan"
        )
        self._assert_closed_objects(schema)

        plan = schema["properties"]["plan"]
        self.assertEqual((plan["minItems"], plan["maxItems"]), (1, 16))
        item = plan["items"]
        self.assertEqual(
            item["properties"]["status"]["enum"],
            ["pending", "in_progress", "completed"],
        )
        self.assertEqual(
            schema["properties"]["plan_status"]["enum"],
            ["active", "blocked", "waiting"],
        )
        self.assertEqual(item["properties"]["step"]["maxLength"], 256)

    def test_extension_uses_the_single_repository_sdk(self):
        sdk = (
            REPOSITORY_DIRECTORY
            / "sdks"
            / "python"
            / "euler-managed-process-sdk"
            / "src"
            / "euler_managed_process_sdk"
        )
        self.assertTrue((sdk / "server.py").is_file())
        self.assertFalse((EXTENSION_DIRECTORY / "python" / "_sdk").exists())

    def _assert_closed_objects(self, schema):
        if schema.get("type") == "object":
            self.assertIsInstance(schema.get("properties"), dict)
            self.assertIsInstance(schema.get("required"), list)
            self.assertIs(schema.get("additionalProperties"), False)
            for child in schema["properties"].values():
                self._assert_closed_objects(child)
        if schema.get("type") == "array":
            self._assert_closed_objects(schema["items"])


if __name__ == "__main__":
    unittest.main()
