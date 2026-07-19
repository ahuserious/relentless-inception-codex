from __future__ import annotations

import copy
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import PLUGIN_ROOT

from relentless_inception.config import (
    canonical_hash,
    load_config,
    load_schema,
    redact_config,
    set_user_config,
    validate_config,
)
from relentless_inception.errors import ConfigError


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.user_config_path = Path(self.temporary_directory.name) / "user-config.json"
        self.environment_patch = mock.patch.dict(
            os.environ,
            {
                "RELENTLESS_INCEPTION_DATA_DIR": self.temporary_directory.name,
                "RELENTLESS_INCEPTION_CONFIG": str(self.user_config_path),
            },
            clear=False,
        )
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)

    def test_default_configuration_and_schema_are_loadable(self) -> None:
        config = load_config(include_user=False)
        self.assertEqual(validate_config(config), [])
        self.assertEqual(
            config["profiles"]["maximum_intelligence"]["budgets"]["unknown_cost_policy"],
            "fail_closed",
        )

        schema = load_schema()
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertIn("providers", schema["properties"])
        self.assertTrue((PLUGIN_ROOT / "schemas" / "config.schema.json").is_file())

    def test_redaction_hides_literal_secrets_but_preserves_environment_references(self) -> None:
        unsafe = {
            "api_key": "sk-live-value",
            "api_key_env": "SAFE_KEY_ENV",
            "nested": {"password": "correct horse battery staple", "token_file_env": "TOKEN_FILE"},
        }
        redacted = redact_config(unsafe)

        self.assertEqual(redacted["api_key"], "<redacted>")
        self.assertEqual(redacted["nested"]["password"], "<redacted>")
        self.assertEqual(redacted["api_key_env"], "SAFE_KEY_ENV")
        self.assertEqual(redacted["nested"]["token_file_env"], "TOKEN_FILE")
        self.assertNotIn("sk-live-value", json.dumps(redacted))

        same_shape_different_secrets = copy.deepcopy(unsafe)
        same_shape_different_secrets["api_key"] = "another-secret"
        same_shape_different_secrets["nested"]["password"] = "different-password"
        self.assertEqual(canonical_hash(unsafe), canonical_hash(same_shape_different_secrets))

    def test_validation_and_user_override_reject_plaintext_secrets(self) -> None:
        config = load_config(include_user=False)
        config["providers"]["xai_direct"]["api_key"] = "must-not-be-stored"

        errors = validate_config(config)
        self.assertTrue(
            any("providers.xai_direct.api_key looks like a plaintext secret" in error for error in errors),
            errors,
        )
        self.assertFalse(any("must-not-be-stored" in error for error in errors))

        with self.assertRaisesRegex(ConfigError, "Refusing to store a plaintext secret"):
            set_user_config("providers.xai_direct.api_key", "must-not-be-stored")
        self.assertFalse(self.user_config_path.exists())

    def test_safe_user_override_is_validated_and_written_privately(self) -> None:
        merged = set_user_config("providers.xai_direct.api_key_env", "TEST_XAI_API_KEY")

        self.assertEqual(merged["providers"]["xai_direct"]["api_key_env"], "TEST_XAI_API_KEY")
        persisted = json.loads(self.user_config_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted, {"providers": {"xai_direct": {"api_key_env": "TEST_XAI_API_KEY"}}})
        file_mode = stat.S_IMODE(self.user_config_path.stat().st_mode)
        self.assertEqual(file_mode, 0o600)
        self.assertEqual(load_config()["providers"]["xai_direct"]["api_key_env"], "TEST_XAI_API_KEY")

    def test_validation_reports_cross_reference_and_xai_effort_errors(self) -> None:
        config = load_config(include_user=False)
        config["seats"]["grok45_researcher"]["provider"] = "missing_provider"
        config["seats"]["grok45_adversary"]["reasoning_effort"] = "ultra"
        config["profiles"]["maximum_intelligence"]["fusion"]["judge"] = "missing_judge"

        errors = validate_config(config)
        self.assertTrue(any("references unknown provider 'missing_provider'" in error for error in errors), errors)
        self.assertTrue(
            any("reasoning_effort" in error and "grok-4.5" in error for error in errors),
            errors,
        )
        self.assertTrue(any("fusion.judge references unknown seat 'missing_judge'" in error for error in errors), errors)

    def test_runtime_enforces_schema_types_unknown_properties_and_header_secret_redaction(self) -> None:
        config = load_config(include_user=False)
        config["providers"]["xai_direct"]["max_concurrency"] = "not-an-integer"
        config["providers"]["xai_direct"]["headers"] = {"X-Auth-Token": "literal-secret-value"}

        errors = validate_config(config)
        self.assertTrue(any("max_concurrency must have JSON type integer" in error for error in errors), errors)
        self.assertTrue(any("headers is not an allowed configuration property" in error for error in errors), errors)
        self.assertEqual(
            redact_config(config)["providers"]["xai_direct"]["headers"]["X-Auth-Token"],
            "<redacted>",
        )

        with self.assertRaisesRegex(ConfigError, "max_concurrency must have JSON type integer"):
            set_user_config("providers.xai_direct.max_concurrency", "not-an-integer")
        self.assertFalse(self.user_config_path.exists())

    def test_header_environment_references_are_allowed_and_remain_displayable(self) -> None:
        merged = set_user_config("providers.xai_direct.header_env.X-Auth-Token", "XAI_SECONDARY_TOKEN_ENV")
        self.assertEqual(
            merged["providers"]["xai_direct"]["header_env"]["X-Auth-Token"],
            "XAI_SECONDARY_TOKEN_ENV",
        )
        self.assertEqual(
            redact_config(merged)["providers"]["xai_direct"]["header_env"]["X-Auth-Token"],
            "XAI_SECONDARY_TOKEN_ENV",
        )

    def test_execution_lifecycle_and_recursive_cli_settings_are_cross_validated(self) -> None:
        config = load_config(include_user=False)
        profile = config["profiles"]["maximum_intelligence"]
        profile["gates"]["stages"]["pre_execution"]["enabled"] = False
        profile["execution"]["allow_recursive_codex_cli"] = True

        errors = validate_config(config)

        self.assertTrue(
            any("require_pre_execution_gate requires an enabled gates.stages.pre_execution" in error for error in errors),
            errors,
        )
        self.assertTrue(
            any("allow_recursive_codex_cli may be true only when mode='codex_cli'" in error for error in errors),
            errors,
        )

        profile["execution"]["mode"] = "codex_cli"
        profile["execution"]["require_pre_execution_gate"] = False
        profile["gates"]["stages"]["plan"]["tool_policy"] = "none"
        config["seats"]["grok45_verifier"]["tool_policy"] = "provider_server_tools"
        errors = validate_config(config)
        self.assertTrue(
            any("grok45_verifier" in error and "tool_policy='none'" in error for error in errors),
            errors,
        )

    def test_fail_closed_invariants_and_provider_tool_support_are_validated(self) -> None:
        config = load_config(include_user=False)
        profile = config["profiles"]["maximum_intelligence"]
        profile["rescue"]["semantic_failure_detection"] = False
        profile["rescue"]["preserve_failed_attempts"] = False
        profile["privacy"]["persist_raw_prompts"] = True
        profile["privacy"]["persist_metadata_and_hashes"] = False
        config["seats"]["openrouter_sol_pro_panel"].update(
            {
                "tool_policy": "provider_server_tools",
                "server_tools": ["web_search"],
            }
        )
        config["seats"]["grok45_researcher"]["server_tools"] = []
        config["providers"]["xai_direct"]["capabilities"]["reasoning"] = False
        config["providers"]["xai_direct"]["capabilities"]["structured_outputs"] = False

        errors = validate_config(config)

        self.assertTrue(any("semantic_failure_detection must equal True" in error for error in errors), errors)
        self.assertTrue(any("preserve_failed_attempts must equal True" in error for error in errors), errors)
        self.assertTrue(any("persist_raw_prompts must equal False" in error for error in errors), errors)
        self.assertTrue(any("persist_metadata_and_hashes must equal True" in error for error in errors), errors)
        self.assertTrue(
            any("openrouter_sol_pro_panel.tool_policy='provider_server_tools'" in error for error in errors),
            errors,
        )
        self.assertTrue(
            any("grok45_researcher.server_tools must be non-empty" in error for error in errors),
            errors,
        )
        self.assertTrue(any("requests reasoning" in error for error in errors), errors)
        self.assertTrue(any("capabilities.structured_outputs=true" in error for error in errors), errors)

    def test_validation_rejects_duplicate_or_overlapping_independent_seats(self) -> None:
        cases = (
            (
                "duplicate panel",
                "fusion",
                "panel",
                ["grok45_researcher", "grok45_researcher"],
                "fusion.panel must not contain duplicate seat names",
            ),
            (
                "duplicate optional panel",
                "fusion",
                "optional_panel",
                ["openrouter_sol_pro_panel", "openrouter_sol_pro_panel"],
                "fusion.optional_panel must not contain duplicate seat names",
            ),
            (
                "panel overlap",
                "fusion",
                "optional_panel",
                ["grok45_researcher"],
                "fusion.panel and optional_panel must not overlap",
            ),
            (
                "duplicate reviewers",
                "gates",
                "reviewers",
                ["grok45_verifier", "grok45_verifier"],
                "gates.reviewers must not contain duplicate seat names",
            ),
        )

        for label, section, field, value, expected_error in cases:
            with self.subTest(case=label):
                config = load_config(include_user=False)
                profile = config["profiles"]["maximum_intelligence"]
                profile[section][field] = value

                errors = validate_config(config)

                self.assertTrue(any(expected_error in error for error in errors), errors)

    def test_validation_rejects_panel_cap_smaller_than_required_roster(self) -> None:
        config = load_config(include_user=False)
        fusion = config["profiles"]["maximum_intelligence"]["fusion"]
        fusion["max_panel_seats"] = len(fusion["panel"]) - 1

        errors = validate_config(config)

        self.assertTrue(
            any(
                "fusion.max_panel_seats cannot be smaller than required panel length"
                in error
                for error in errors
            ),
            errors,
        )

    def test_malformed_cross_reference_values_report_errors_without_crashing(self) -> None:
        malformed_cases = []

        provider_case = load_config(include_user=False)
        provider_case["seats"]["grok45_researcher"]["provider"] = ["not", "hashable"]
        malformed_cases.append(provider_case)

        panel_case = load_config(include_user=False)
        panel_case["profiles"]["maximum_intelligence"]["fusion"]["panel"][0] = {"bad": "seat"}
        malformed_cases.append(panel_case)

        reviewer_case = load_config(include_user=False)
        reviewer_case["profiles"]["maximum_intelligence"]["gates"]["reviewers"][0] = ["bad", "reviewer"]
        malformed_cases.append(reviewer_case)

        for malformed_config in malformed_cases:
            with self.subTest(malformed_config=malformed_config):
                errors = validate_config(malformed_config)
                self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
