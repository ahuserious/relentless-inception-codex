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


if __name__ == "__main__":
    unittest.main()
