from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

from tests.support import MCP_SERVER_PATH, PLUGIN_ROOT, REPOSITORY_ROOT

from relentless_inception import __version__
from relentless_inception.cli import doctor
from relentless_inception.config import deep_merge, load_config, validate_config
from relentless_inception.providers import ProviderRegistry


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

    def test_release_identity_is_consistent_across_install_and_runtime_surfaces(self) -> None:
        plugin_manifest = _load_json(PLUGIN_MANIFEST_PATH)
        default_config = load_config(include_user=False)

        self.assertEqual(__version__, "0.1.4")
        self.assertEqual(plugin_manifest.get("version"), __version__)
        self.assertEqual(doctor(default_config)["version"], __version__)

        with patch.dict(os.environ, {"XAI_API_KEY": "test-only-placeholder"}):
            headers = ProviderRegistry(default_config)._headers(
                default_config["providers"]["xai_direct"]
            )
        self.assertEqual(headers["User-Agent"], f"relentless-inception-codex/{__version__}")

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
        self.assertTrue(
            os.access(server_path, os.X_OK),
            "The MCP entrypoint must remain directly executable for harnesses "
            "that cannot preserve a separate argv array.",
        )

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

    def test_native_grok_role_keeps_instructions_at_root_and_documents_valid_mcp_overrides(self) -> None:
        role_example_path = PLUGIN_ROOT / "examples" / "native-codex-grok-reviewer-agent.toml.example"
        role_example = role_example_path.read_text(encoding="utf-8")
        default_config = load_config(include_user=False)

        self.assertLess(role_example.index("developer_instructions ="), role_example.index("[skills]"))
        self.assertIn("command = \"/same/command/as/the/main/config\"", role_example)
        self.assertIn("url = \"https://same-origin-as-the-main-config.example/mcp\"", role_example)
        self.assertNotIn("# [mcp_servers.example_server]\n# enabled = false", role_example)
        self.assertEqual(default_config["native_codex"]["reviewer_roles"], [])
        self.assertEqual(default_config["native_codex"]["reasoning_only_roles"], [])

    def test_direct_xai_pricing_matches_documented_short_and_long_context_rates(self) -> None:
        default_config = load_config(include_user=False)
        expected_pricing_by_model = {
            "grok-4.5": {
                "input_per_million_usd": 2.0,
                "cached_input_per_million_usd": 0.5,
                "output_per_million_usd": 6.0,
                "long_context_input_per_million_usd": 4.0,
                "long_context_cached_input_per_million_usd": 1.0,
                "long_context_output_per_million_usd": 12.0,
                "base_rate_input_limit_tokens": 200_000,
                "above_base_rate_behavior": "unknown_cost_fail_closed",
            },
        }
        direct_xai_seats = {
            seat_name: seat
            for seat_name, seat in default_config["seats"].items()
            if seat.get("provider") == "xai_direct"
        }

        self.assertEqual(
            {seat["model"] for seat in direct_xai_seats.values()},
            set(expected_pricing_by_model),
        )
        for seat_name, seat in direct_xai_seats.items():
            with self.subTest(seat=seat_name):
                self.assertEqual(seat["pricing"], expected_pricing_by_model[seat["model"]])

        configuration_doc = (REPOSITORY_ROOT / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
        self.assertIn(
            "| Grok 4.5 | $2.00 | $0.50 | $6.00 | $4.00 | $1.00 | $12.00 |",
            configuration_doc,
        )
        self.assertIn(
            "| Grok 4.3 | $1.25 | $0.20 | $2.50 | $2.50 | $0.40 | $5.00 |",
            configuration_doc,
        )
        self.assertIn("https://docs.x.ai/developers/pricing", configuration_doc)

    def test_shipped_maximum_intelligence_defaults_are_frontier_only(self) -> None:
        default_config = load_config(include_user=False)
        profile = default_config["profiles"]["maximum_intelligence"]
        fusion = profile["fusion"]
        gates = profile["gates"]

        active_seat_names = [
            *fusion["panel"],
            fusion["judge"],
            fusion["synthesizer"],
            *gates["reviewers"],
        ]
        for seat_name in active_seat_names:
            with self.subTest(seat=seat_name):
                seat = default_config["seats"][seat_name]
                self.assertTrue(seat["enabled"])
                self.assertEqual(seat["provider"], "xai_direct")
                self.assertEqual(seat["model"], "grok-4.5")
                self.assertEqual(seat["reasoning_effort"], "high")
                self.assertNotIn("grok-4.3", seat["fallback_models"])

        for seat_name, seat in default_config["seats"].items():
            if seat.get("provider") == "xai_direct":
                with self.subTest(default_xai_seat=seat_name):
                    self.assertEqual(seat["model"], "grok-4.5")
                    self.assertNotIn("grok-4.3", seat["fallback_models"])

        native_codex = default_config["native_codex"]
        self.assertEqual(native_codex["executor_model"], "gpt-5.6-sol")
        self.assertEqual(native_codex["reviewer_models"], ["gpt-5.6-sol"])
        self.assertEqual(profile["execution"]["model"], "gpt-5.6-sol")


if __name__ == "__main__":
    unittest.main()
