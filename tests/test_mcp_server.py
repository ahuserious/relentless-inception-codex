from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from tests.support import MCP_SERVER_PATH, PLUGIN_ROOT
from relentless_inception.execution import build_handoff


class McpServerSmokeTests(unittest.TestCase):
    def _call_execution_handoff(self, data_directory: str, run_id: str) -> tuple[dict, dict]:
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "execution_handoff", "arguments": {"run_id": run_id}},
        }
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "RELENTLESS_INCEPTION_DATA_DIR": data_directory,
                "PYTHONPATH": str(PLUGIN_ROOT),
            }
        )
        completed = subprocess.run(
            [sys.executable, str(MCP_SERVER_PATH)],
            input=json.dumps(message) + "\n",
            text=True,
            capture_output=True,
            cwd=str(PLUGIN_ROOT),
            env=environment,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        response = json.loads(completed.stdout.strip())
        payload = json.loads(response["result"]["content"][0]["text"])
        return response, payload

    @staticmethod
    def _write_handoff(data_directory: str, run_id: str, handoff: dict) -> None:
        run_directory = os.path.join(data_directory, "runs", run_id)
        os.makedirs(run_directory)
        with open(os.path.join(run_directory, "execution-handoff.json"), "w", encoding="utf-8") as handle:
            json.dump(handoff, handle)

    @staticmethod
    def _valid_handoff(run_id: str) -> dict:
        return build_handoff(
            "Implement the verified plan and run its focused tests.",
            run_id,
            {"passed": True, "artifact_sha256": "a" * 64},
            {
                "enabled": True,
                "mode": "codex_handoff",
                "require_fused_plan": True,
                "handoff_include": ["fused_plan"],
            },
            profile_name="maximum_intelligence",
        )

    def test_initialize_tools_list_and_config_validate_over_stdio(self) -> None:
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "config_validate", "arguments": {}},
            },
        ]
        stdin = "\n".join(json.dumps(message) for message in messages) + "\n"

        with tempfile.TemporaryDirectory() as temporary_directory:
            environment = os.environ.copy()
            environment.update(
                {
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "RELENTLESS_INCEPTION_DATA_DIR": temporary_directory,
                    "RELENTLESS_INCEPTION_CONFIG": os.path.join(temporary_directory, "missing-user-config.json"),
                }
            )
            existing_python_path = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = (
                str(PLUGIN_ROOT)
                if not existing_python_path
                else str(PLUGIN_ROOT) + os.pathsep + existing_python_path
            )
            completed = subprocess.run(
                [sys.executable, str(MCP_SERVER_PATH)],
                input=stdin,
                text=True,
                capture_output=True,
                cwd=str(PLUGIN_ROOT),
                env=environment,
                timeout=10,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        self.assertEqual([response["id"] for response in responses], [1, 2, 3])

        initialize = responses[0]["result"]
        self.assertEqual(initialize["protocolVersion"], "2025-06-18")
        self.assertEqual(
            initialize["serverInfo"],
            {"name": "relentless-inception", "version": "0.1.4"},
        )

        tool_names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertTrue({"config_validate", "fuse", "adversarial_gate"}.issubset(tool_names))

        validation_result = responses[2]["result"]
        self.assertFalse(validation_result["isError"])
        validation_payload = json.loads(validation_result["content"][0]["text"])
        self.assertEqual(validation_payload, {"errors": [], "ok": True})

    def test_invalid_config_can_be_diagnosed_and_cannot_disable_run_abort(self) -> None:
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "config_validate", "arguments": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "doctor", "arguments": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "run_abort", "arguments": {"run_id": "active-run"}},
            },
        ]
        stdin = "\n".join(json.dumps(message) for message in messages) + "\n"

        with tempfile.TemporaryDirectory() as temporary_directory:
            user_config_path = os.path.join(temporary_directory, "invalid-user-config.json")
            with open(user_config_path, "w", encoding="utf-8") as user_config:
                json.dump({"providers": {"xai_direct": {"max_concurrency": "invalid"}}}, user_config)
            run_directory = os.path.join(temporary_directory, "runs", "active-run")
            os.makedirs(run_directory)
            environment = os.environ.copy()
            environment.update(
                {
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "RELENTLESS_INCEPTION_DATA_DIR": temporary_directory,
                    "RELENTLESS_INCEPTION_CONFIG": user_config_path,
                    "PYTHONPATH": str(PLUGIN_ROOT),
                }
            )
            completed = subprocess.run(
                [sys.executable, str(MCP_SERVER_PATH)],
                input=stdin,
                text=True,
                capture_output=True,
                cwd=str(PLUGIN_ROOT),
                env=environment,
                timeout=10,
                check=False,
            )
            kill_file_created = os.path.exists(os.path.join(run_directory, "KILL"))

        self.assertEqual(completed.returncode, 0, completed.stderr)
        responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        payloads = [json.loads(response["result"]["content"][0]["text"]) for response in responses]
        self.assertFalse(responses[0]["result"]["isError"])
        self.assertFalse(payloads[0]["ok"])
        self.assertTrue(payloads[0]["errors"])
        self.assertFalse(responses[1]["result"]["isError"])
        self.assertFalse(payloads[1]["ok"])
        self.assertEqual(payloads[1]["version"], "0.1.4")
        self.assertFalse(responses[2]["result"]["isError"])
        self.assertTrue(kill_file_created)

    def test_execution_handoff_returns_a_validated_packet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            handoff = self._valid_handoff("validated-run")
            self._write_handoff(temporary_directory, "validated-run", handoff)
            response, payload = self._call_execution_handoff(temporary_directory, "validated-run")

        self.assertFalse(response["result"]["isError"])
        self.assertEqual(payload, handoff)

    def test_execution_handoff_rejects_a_tampered_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            handoff = self._valid_handoff("tampered-run")
            handoff["instruction"] = "tampered after review"
            self._write_handoff(temporary_directory, "tampered-run", handoff)
            response, payload = self._call_execution_handoff(temporary_directory, "tampered-run")

        self.assertTrue(response["result"]["isError"])
        self.assertIn("payload hash does not match", payload["error"])

    def test_execution_handoff_rejects_an_embedded_run_id_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            handoff = self._valid_handoff("different-run")
            self._write_handoff(temporary_directory, "requested-run", handoff)
            response, payload = self._call_execution_handoff(temporary_directory, "requested-run")

        self.assertTrue(response["result"]["isError"])
        self.assertIn("run_id does not match", payload["error"])


if __name__ == "__main__":
    unittest.main()
