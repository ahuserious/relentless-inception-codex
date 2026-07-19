from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any, Dict

from tests.support import MCP_SERVER_PATH, PLUGIN_ROOT, REPOSITORY_ROOT

from relentless_inception.config import deep_merge, load_config, validate_config


PLUGIN_MANIFEST_PATH = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
MCP_MANIFEST_PATH = PLUGIN_ROOT / ".mcp.json"
MARKETPLACE_PATH = REPOSITORY_ROOT / ".agents" / "plugins" / "marketplace.json"
REQUIRED_SKILLS = {
    "relentless-inception",
    "relentless-inception-config",
    "relentless-inception-review",
}


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as file_handle:
        value = json.load(file_handle)
    if not isinstance(value, dict):
        raise AssertionError(f"Expected a JSON object in {path}")
    return value


class PluginPackageIntegrityTests(unittest.TestCase):
    def _resolve_inside(self, root: Path, configured_path: str) -> Path:
        self.assertIsInstance(configured_path, str)
        self.assertFalse(Path(configured_path).is_absolute())
        resolved_root = root.resolve()
        resolved_path = (root / configured_path).resolve()
        try:
            resolved_path.relative_to(resolved_root)
        except ValueError:
            self.fail(f"Configured path escapes {resolved_root}: {configured_path}")
        return resolved_path

    def test_plugin_manifest_references_bundled_mcp_and_skills(self) -> None:
        plugin_manifest = _load_json(PLUGIN_MANIFEST_PATH)

        interface = plugin_manifest.get("interface")
        self.assertIsInstance(interface, dict)
        capabilities = interface.get("capabilities")
        self.assertIsInstance(capabilities, list)
        self.assertTrue(capabilities)
        self.assertTrue(all(isinstance(capability, str) for capability in capabilities))

        skills_root = self._resolve_inside(PLUGIN_ROOT, plugin_manifest["skills"])
        self.assertTrue(skills_root.is_dir(), skills_root)
        skill_directories = {path.name: path for path in skills_root.iterdir() if path.is_dir()}
        self.assertTrue(REQUIRED_SKILLS.issubset(skill_directories), skill_directories)
        for skill_name, skill_directory in skill_directories.items():
            skill_manifest = skill_directory / "SKILL.md"
            self.assertTrue(skill_manifest.is_file(), f"Missing SKILL.md for {skill_name}")

        mcp_manifest = self._resolve_inside(PLUGIN_ROOT, plugin_manifest["mcpServers"])
        self.assertEqual(mcp_manifest, MCP_MANIFEST_PATH.resolve())
        self.assertTrue(mcp_manifest.is_file(), mcp_manifest)

    def test_mcp_manifest_uses_codex_camel_case_and_resolves_server(self) -> None:
        mcp_manifest = _load_json(MCP_MANIFEST_PATH)

        self.assertEqual(set(mcp_manifest), {"mcpServers"})
        mcp_servers = mcp_manifest["mcpServers"]
        self.assertIsInstance(mcp_servers, dict)
        self.assertIn("relentless-inception", mcp_servers)

        server = mcp_servers["relentless-inception"]
        self.assertIsInstance(server, dict)
        self.assertEqual(server.get("command"), "python3")
        self.assertIsInstance(server.get("args"), list)
        self.assertTrue(all(isinstance(argument, str) for argument in server["args"]))
        self.assertEqual(server["args"], ["./mcp_server.py"])

        configured_working_directory = server.get("cwd")
        self.assertIsInstance(configured_working_directory, str)
        working_directory = self._resolve_inside(PLUGIN_ROOT, configured_working_directory)
        self.assertTrue(working_directory.is_dir(), working_directory)
        server_path = self._resolve_inside(working_directory, server["args"][0])
        self.assertEqual(server_path, MCP_SERVER_PATH.resolve())
        self.assertTrue(server_path.is_file(), server_path)

    def test_marketplace_source_resolves_to_manifest_plugin(self) -> None:
        marketplace = _load_json(MARKETPLACE_PATH)
        plugin_manifest = _load_json(PLUGIN_MANIFEST_PATH)
        matching_entries = [
            entry
            for entry in marketplace.get("plugins", [])
            if isinstance(entry, dict) and entry.get("name") == plugin_manifest["name"]
        ]

        self.assertEqual(len(matching_entries), 1, matching_entries)
        source = matching_entries[0].get("source")
        self.assertIsInstance(source, dict)
        self.assertEqual(source.get("source"), "local")
        marketplace_plugin_root = self._resolve_inside(REPOSITORY_ROOT, source["path"])
        self.assertEqual(marketplace_plugin_root, PLUGIN_ROOT.resolve())
        self.assertTrue((marketplace_plugin_root / ".codex-plugin" / "plugin.json").is_file())

    def test_json_examples_merge_into_valid_complete_configuration(self) -> None:
        default_config = load_config(include_user=False)
        example_paths = sorted((PLUGIN_ROOT / "examples").glob("*.json"))
        self.assertTrue(example_paths)

        for example_path in example_paths:
            with self.subTest(example=example_path.name):
                merged_config = deep_merge(default_config, _load_json(example_path))
                self.assertEqual(validate_config(merged_config), [])


if __name__ == "__main__":
    unittest.main()
