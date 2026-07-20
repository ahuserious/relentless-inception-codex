from __future__ import annotations

import ast
import copy
import importlib.util
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "bench"
PINS = json.loads((BENCH / "pins.json").read_text(encoding="utf-8"))


def load_validator():
    cached_validator = sys.modules.get("validate_evidence")
    if cached_validator is not None:
        return cached_validator
    spec = importlib.util.spec_from_file_location(
        "validate_evidence", BENCH / "validate_evidence.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["validate_evidence"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop("validate_evidence", None)
        raise
    return module


VALIDATOR = load_validator()


class BenchmarkAssetTests(unittest.TestCase):
    @staticmethod
    def _canonical_shell_script(command: str) -> str:
        return (
            VALIDATOR.RECORD_HELPERS[0]
            + "\n"
            + f"run_record {shlex.quote(command)} || exit $?"
        )

    @staticmethod
    def _atif_exec_command_step(
        step_id: int,
        output: str,
        *,
        enveloped: bool = False,
        exit_code: int = 0,
    ) -> dict[str, object]:
        tool_call_id = f"exec-{step_id}"
        command = next(
            (line[2:] for line in output.splitlines() if line.startswith("$ ")),
            "fixture-command",
        )
        shell_script = BenchmarkAssetTests._canonical_shell_script(command)
        retained_output = (
            json.dumps({"exit_code": exit_code, "output": output}, separators=(",", ":"))
            if enveloped
            else output
        )
        content = repr(
            [
                {
                    "type": "input_text",
                    "text": "Script completed\nWall time 0.1 seconds\nOutput:\n",
                },
                {"type": "input_text", "text": retained_output},
            ]
        )
        return {
            "step_id": step_id,
            "tool_calls": [
                {
                    "tool_call_id": tool_call_id,
                    "function_name": "exec",
                    "arguments": {
                        "input": (
                            f"const r = await tools.exec_command({{cmd:{json.dumps(shell_script)}}}); "
                            + (
                                "text(JSON.stringify({exit_code:r.exit_code, output:r.output}));"
                                if enveloped
                                else "text(r.output);"
                            )
                        )
                    },
                }
            ],
            "observation": {
                "results": [
                    {"source_call_id": tool_call_id, "content": content}
                ]
            },
        }

    @staticmethod
    def _atif_ri_step(step_id: int, calls: list[tuple[str, str]]) -> dict[str, object]:
        source = "".join(
            f'await tools.mcp__relentless_inception__{tool}('
            f'{{resume_run_id: "{run_id}"}});'
            for tool, run_id in calls
        )
        return {
            "step_id": step_id,
            "tool_calls": [
                {
                    "tool_call_id": f"ri-wrapper-{step_id}",
                    "function_name": "exec",
                    "arguments": {"input": source},
                }
            ],
        }

    def test_all_immutable_pins_are_complete(self) -> None:
        self.assertEqual(PINS["harbor"]["version"], "0.20.0")
        self.assertEqual(
            PINS["harbor"]["commit"],
            "459ff6ec99417589b7f679d14ddf3b3f0ae4f1dc",
        )
        self.assertEqual(
            PINS["harbor"]["dataset"]["source_commit"],
            "69671fbaac6d67a7ef0dfec016cc38a64ef7a77c",
        )
        self.assertEqual(PINS["pier"]["version"], "0.3.0")
        self.assertEqual(
            PINS["pier"]["dataset"]["source_commit"],
            "6db64a40f3318d8659238ff34a8cc4b491c49205",
        )
        self.assertEqual(PINS["codex"]["version"], "0.145.0-alpha.18")
        self.assertEqual(PINS["codex"]["agent_timeout_seconds"], 3600)
        self.assertEqual(PINS["codex"]["mcp_startup_timeout_seconds"], 60)
        self.assertEqual(PINS["codex"]["mcp_tool_timeout_seconds"], 1800)
        for artifact_hash in PINS["artifacts"].values():
            self.assertRegex(artifact_hash, r"^[0-9a-f]{64}$")
        for harness in ("harbor", "pier"):
            for task in PINS[harness]["tasks"].values():
                self.assertRegex(task["image_digest"], r"^sha256:[0-9a-f]{64}$")
                self.assertRegex(task["task_name"], r"^[a-z0-9-]+/[a-z0-9-]+$")

    def test_mcp_and_pier_network_configs_are_executable_and_secret_free(self) -> None:
        mcp = json.loads((BENCH / "harbor" / "mcp.json").read_text(encoding="utf-8"))
        server = mcp["mcpServers"]["relentless-inception"]
        self.assertEqual(
            server["command"],
            "/opt/relentless-inception-bench/mcp_server_launcher.py",
        )
        self.assertEqual(server["args"], [])

        codex_toml = (BENCH / "pier" / "codex.toml").read_text(encoding="utf-8")
        self.assertIn('base_url = "https://api.x.ai/v1"', codex_toml)
        self.assertIn('env_key = "XAI_API_KEY"', codex_toml)
        self.assertIn(
            'command = "/opt/relentless-inception-bench/mcp_server_launcher.py"',
            codex_toml,
        )
        self.assertIn("args = []", codex_toml)
        self.assertIn("startup_timeout_sec = 60", codex_toml)
        self.assertIn("tool_timeout_sec = 1800", codex_toml)
        launcher = BENCH / "support" / "mcp_server_launcher.py"
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertNotRegex(launcher.read_text(encoding="utf-8"), r"xai-[A-Za-z0-9_-]{12,}")

        adapter = (BENCH / "harbor" / "codex_agent.py").read_text(encoding="utf-8")
        self.assertIn("class BenchmarkCodex(Codex)", adapter)
        self.assertIn("MCP_STARTUP_TIMEOUT_SEC = 60", adapter)
        self.assertIn("MCP_TOOL_TIMEOUT_SEC = 1800", adapter)
        self.assertIn("toml.dumps", adapter)

    def test_configs_pin_single_attempts_no_retries_and_no_forbidden_mounts(self) -> None:
        config_paths = [
            BENCH / "harbor" / "fix-git.yaml",
            BENCH / "harbor" / "regex-log.yaml",
            BENCH / "pier" / "job.yaml",
        ]
        for path in config_paths:
            text = path.read_text(encoding="utf-8")
            self.assertIn("override_timeout_sec: 3600", text)
            self.assertIn("n_attempts: 1", text)
            self.assertIn("max_retries: 0", text)
            self.assertIn("0.145.0-alpha.18", text)
            self.assertNotIn("mounts:", text)
            self.assertNotIn("task-cache", text.lower())
            self.assertNotIn("held-test", text.lower())
            self.assertNotRegex(text, r"(?:xai-|sk-)[A-Za-z0-9_-]{12,}")
        for path in config_paths:
            self.assertNotIn("XAI_API_KEY", path.read_text(encoding="utf-8"))
            self.assertNotIn("CODEX_FORCE_AUTH_JSON", path.read_text(encoding="utf-8"))
        self.assertIn(
            "terminal-bench/fix-git",
            (BENCH / "harbor" / "fix-git.yaml").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "terminal-bench/regex-log",
            (BENCH / "harbor" / "regex-log.yaml").read_text(encoding="utf-8"),
        )
        for name in ("fix-git", "regex-log"):
            harbor_config = (BENCH / "harbor" / f"{name}.yaml").read_text(encoding="utf-8")
            self.assertIn(
                "import_path: bench.harbor.codex_agent:BenchmarkCodex",
                harbor_config,
            )
            self.assertNotIn("name: codex", harbor_config)

    def test_prompt_contract_requires_fusion_and_all_lifecycle_gates(self) -> None:
        for path in (
            BENCH / "harbor" / "extra-instruction.md",
            BENCH / "pier" / "prompt.j2",
        ):
            text = path.read_text(encoding="utf-8")
            for token in (
                "fuse",
                "plan",
                "pre_execution",
                "post_execution",
                "final",
                "summarize",
                "adversarial_gate",
            ):
                self.assertIn(token, text)
            for requirement in (
                "read-only",
                "mechanical_evidence",
                "pre-execution plan",
                "execution evidence is not yet expected",
                "do not claim\nworkspace inspection",
                "replace only the placeholder with the byte-identical\nbenchmark instruction",
                "actual execution evidence",
                "do not reuse the plan-review",
                "benchmark-fuse",
                "benchmark-plan",
                "benchmark-pre-execution",
                "benchmark-post-execution",
                "benchmark-final",
                "benchmark-summarize",
                "do not run\nthe suite during this preflight",
                "expected discovery absences successful shell\noutcomes",
                "no matches\n(expected)",
                "do not invoke a tool after discovering it is\nunavailable",
                "never suppress, normalize, or\nrelabel a genuine",
                "every literal nonzero exit in\nthat evidence is a real blocking failure",
                "guarded, status-zero transcript observations",
                "immutable codex trajectory",
                "pass only that exact final-acceptance\ntranscript",
                "disclose every resolved\nintermediate failure",
                "keeping its original nonzero transcript in the trajectory",
                "never\nomit or reclassify an unresolved failure",
                "if any final-acceptance check is\nnonzero, do not ask a gate to pass",
                "script running with cell id` is not an mcp timeout",
                "reserve `run_status` for an actual mcp timeout or error",
                "never hard-code an exit-status marker",
                "documented no-match status is 1",
                "preserve status 2 or greater as a real failure",
                "the entire performed preflight, including captured output, must satisfy both limits",
                "at most 12 task-relevant command records and at most 12,000 characters",
                "target at most 8,000 characters",
                "keep every command record below 2,500 characters",
                "never dump a whole readme, generated source file",
                "cap discovery output at its source",
                "each command or pipeline inside a batch counts as one record",
                "use at most two host-shell tool calls",
                "store the returned `r.output` unchanged",
                "concatenating those stored outputs in original order with no inserted separator",
                "do not enumerate an entire repository, dangling\nobjects, or whole history",
                "call\n`fuse` as soon as those facts are sufficient",
                "execution_handoff.ready_for_host_workflow: true",
                "status: awaiting_host_gates",
                "mutation_authorized: false",
                "expected successful state, not a blocker",
                "do not call\n`execution_handoff`, wait for its fields to change, or rewrite it",
                "do not call\n`run_status` after a successful mcp result",
                "without any\nintervening shell, file, or external-state operation",
                "authorization\nreceipts; only then may host execution begin",
                "every `adversarial_gate` call must\ncontain exactly these four argument keys",
                "do not pass `context`, `profile`, or\nany other key",
                "run every required\ntest or state check once",
                "do not repeat a successful check or recapture an\nidentical transcript",
                "json.stringify({exit_code:r.exit_code, output:r.output})",
                "json.stringify({exit_code:r.exit_code, output:transcript})",
                "while(r.session_id){r=await tools.write_stdin",
                "review plan coverage for the exact original action request below",
                "review execution-readiness for the exact original action request below",
                "review actual execution for the exact original action request below",
                "review final acceptance for the exact original action request below",
                "review remaining risks for the exact original action request below",
                "each retained marker must correspond one-to-one and in order",
                "run_record",
                "reuse that byte-identical artifact and the byte-identical final-acceptance\nevidence",
                "only the lifecycle-labeled `task`\nmay differ",
                "literal `gate.passed: true`",
                "a false or missing `gate.passed` is a failed gate",
                "do not add a status lookup after a passed call",
                "put exactly one concise `resolved-failure ledger` in the completed-work artifact",
                "its entire body must be one fenced `json` array",
                "resolution_exit_code` (literal integer `0`)",
                "all three completed-work gates must receive that byte-identical ledger",
                "evidence-backed final state",
                "trajectory context",
                "remaining risks",
                "call each required fusion or gate operation once",
                "longest permitted wait interval up to 60 seconds",
                "at most once per 60 seconds",
                "if it reports `running`, wait before checking again and never resume or redispatch",
                "if it reports `completed`, call the same operation with the same deterministic run id once",
                "treat `rejected`, `failed`, or `aborted` as blocking",
                "make no more tool or shell calls: immediately return a concise answer",
                "lifecycle stage: post_execution",
                "lifecycle stage: final",
                "lifecycle stage: summarize",
            ):
                normalized_text = " ".join(text.lower().split())
                normalized_requirement = " ".join(requirement.split())
                self.assertIn(normalized_requirement, normalized_text)
        self.assertIn("{{ instruction }}", (BENCH / "pier" / "prompt.j2").read_text())

    def test_runner_validates_each_attempt_before_returning_success(self) -> None:
        runner = (BENCH / "run_bench.py").read_text(encoding="utf-8")
        self.assertIn("validate_attempt_fn(index_path)", runner)
        verification_index = runner.index("artifact_hashes = verify_artifact_hashes(pins)")
        validator_import_index = runner.index(
            "from validate_evidence import validate_attempt, validate_final"
        )
        self.assertLess(verification_index, validator_import_index)
        self.assertIn('child_environment["CODEX_FORCE_AUTH_JSON"] = "1"', runner)
        self.assertIn('child_environment["PYTHONPATH"] = str(REPOSITORY_ROOT)', runner)
        self.assertIn("write_ephemeral_secret(secret_file, resolve_xai_api_key())", runner)
        self.assertIn("return 1", runner)
        self.assertNotIn("os.environ.copy()", runner)

    def test_validator_handoff_contract_matches_runtime_builder(self) -> None:
        plugin_root = ROOT / "plugins" / "relentless-inception"
        sys.path.insert(0, str(plugin_root))
        try:
            from relentless_inception.execution import build_handoff
        finally:
            sys.path.pop(0)

        config = json.loads(
            (plugin_root / "config" / "default.json").read_text(encoding="utf-8")
        )
        profile = config["profiles"][VALIDATOR.BENCHMARK_PROFILE_NAME]
        synthesis = "Exact runtime/validator handoff fixture."
        artifact_sha256 = sha256(synthesis.encode("utf-8")).hexdigest()
        gate = {"passed": True, "artifact_sha256": artifact_sha256}
        judgment = {
            "minority_findings": ["Retain one supported minority finding."],
            "blind_spots": ["Retain one explicit blind spot."],
        }
        ledger = {
            "calls": 7,
            "input_tokens": 100,
            "output_tokens": 50,
            "reasoning_tokens": 20,
            "tool_calls": 3,
            "wall_seconds": 12.5,
            "known_cost_usd": 0.25,
            "unknown_cost_calls": 0,
            "warnings": [],
            "provider_cost_usd": {"xai_direct": 0.25},
        }
        runtime_handoff = build_handoff(
            synthesis,
            "benchmark-fuse",
            gate,
            profile["execution"],
            profile_name=VALIDATOR.BENCHMARK_PROFILE_NAME,
            judge=judgment,
            ledger=ledger,
            budgets=profile["budgets"],
            gates=profile["gates"],
            native_codex=config["native_codex"],
        )
        validator_handoff = VALIDATOR.expected_fusion_handoff(
            run_id="benchmark-fuse",
            synthesis=synthesis,
            judgment=judgment,
            ledger=ledger,
            artifact_sha256=artifact_sha256,
        )
        self.assertEqual(runtime_handoff, validator_handoff)

    def _write_fixture(
        self,
        root: Path,
        *,
        task: str = "anko-default-function-arguments",
        bad_reward: bool = False,
        bad_provenance: bool = False,
        secret: bool = False,
        attempt: int = 1,
        identity_salt: str = "fixture",
        resolved_failure: bool = False,
        invocation_hash_override: tuple[str, str, str, str] | None = None,
    ) -> Path:
        harness = "pier" if task in PINS["pier"]["tasks"] else "harbor"
        task_pin = PINS[harness]["tasks"][task]
        task_sequence = [*PINS["harbor"]["tasks"], *PINS["pier"]["tasks"]]
        sequence_index = task_sequence.index(task) * 2 + (attempt - 1)
        started_minute = sequence_index * 2
        finished_minute = started_minute + 1
        fixture_mounts = [
            {
                "type": "bind",
                "source": "/workspace/plugins/relentless-inception",
                "target": "/opt/relentless-inception",
                "read_only": True,
            },
            {
                "type": "bind",
                "source": "/workspace/bench/support",
                "target": "/opt/relentless-inception-bench",
                "read_only": True,
            },
            {
                "type": "bind",
                "source": "/private/tmp/ri-bench-secret-fixture/xai-api-key",
                "target": "/run/secrets/relentless-inception-xai",
                "read_only": True,
            },
        ]
        if harness == "pier":
            fixture_mounts = [
                {
                    "type": "bind",
                    "source": "${HOST_VERIFIER_LOGS_PATH}",
                    "target": "/logs/verifier",
                },
                {
                    "type": "bind",
                    "source": "${HOST_AGENT_LOGS_PATH}",
                    "target": "/logs/agent",
                },
                {
                    "type": "bind",
                    "source": "${HOST_ARTIFACTS_PATH}",
                    "target": "/logs/artifacts",
                },
                *fixture_mounts,
            ]
        contract = {
            "schema_version": 1,
            "harness": harness,
            "task": task,
            "attempt": attempt,
            "command": [
                "pier" if harness == "pier" else "harbor",
                "run",
                "--n-attempts",
                "1",
                "--n-concurrent",
                "1",
                "--max-retries",
                "0",
                "--mounts-json",
                json.dumps(fixture_mounts, separators=(",", ":")),
            ],
            "pins": {
                "harness_version": PINS[harness]["version"],
                "harness_commit": PINS[harness].get("commit"),
                "dataset_source_commit": PINS[harness]["dataset"]["source_commit"],
                "image": task_pin["image"],
                "image_digest": task_pin["image_digest"],
                "observed_image_digest": task_pin["image_digest"],
                "base_commit": task_pin.get("base_commit"),
                "codex_version": PINS["codex"]["version"],
                "model": PINS["codex"]["model"],
                "reasoning_effort": PINS["codex"]["reasoning_effort"],
                "agent_timeout_seconds": PINS["codex"]["agent_timeout_seconds"],
                "mcp_startup_timeout_seconds": PINS["codex"]["mcp_startup_timeout_seconds"],
                "mcp_tool_timeout_seconds": PINS["codex"]["mcp_tool_timeout_seconds"],
                "ri_data_directory": "/logs/agent/relentless-inception",
                "artifact_hashes": PINS["artifacts"],
            },
        }
        result = {
            "id": VALIDATOR.canonical_json_hash([task, attempt, identity_salt, "result"]),
            "task_name": task_pin["task_name"],
            "trial_name": f"{task}__{identity_salt}-attempt-{attempt}",
            "started_at": f"2026-01-01T00:{started_minute:02d}:00Z",
            "finished_at": f"2026-01-01T00:{finished_minute:02d}:00Z",
            "exception_info": None,
            "verifier_result": {"rewards": {"reward": 0 if bad_reward else 1}},
            "agent_info": {
                "name": "codex",
                "version": PINS["codex"]["version"],
                "model_info": {"provider": "openai", "name": "gpt-5.6-sol"},
            },
        }
        preflight_evidence = "$ git status --short\n[exit 0]\n"
        intermediate_evidence = "$ failing-check\nknown transient failure\n[exit 1]\n"
        final_evidence = "$ project-test\n27 tests passed\n[exit 0]\n"
        original_request = "Implement and verify the exact fixture benchmark request."
        fused_artifact = (
            "The bounded fused pre-execution plan preserves the exact task scope, "
            "keeps all workspace mutations on the active Codex host, and requires "
            "deterministic verification before acceptance. It identifies the retained "
            "artifacts, lifecycle review points, provenance checks, and final evidence "
            "needed to demonstrate that the requested implementation is complete."
        )
        fusion_task = VALIDATOR.FUSION_TASK_PREFIX + "\n" + original_request
        fusion_context = (
            "The active Codex host, not an external seat, will execute the approved "
            "plan. Provider-hosted tools are isolated from this workspace."
        )
        lifecycle_tasks = {
            stage_name: task_prefix + original_request
            for stage_name, task_prefix in VALIDATOR.LIFECYCLE_TASK_PREFIXES.items()
        }
        fixture_judgment = {
            "consensus": ["Use the bounded fixture plan."],
            "contradictions": [],
            "unique_insights": [],
            "minority_findings": [],
            "blind_spots": [],
            "partial_coverage": [],
            "final_guidance": ["Execute and verify the exact fixture request."],
        }
        resolved_failure_body = "No resolved failures."
        if resolved_failure:
            resolved_failure_body = (
                "```json\n"
                + json.dumps(
                    [
                        {
                            "command": "failing-check",
                            "exit_code": 1,
                            "cause": "The fixture diagnostic intentionally returned a failure.",
                            "corrective_action": "Continued to the independent final acceptance check.",
                            "resolution_command": "project-test",
                            "resolution_exit_code": 0,
                        }
                    ],
                    indent=2,
                )
                + "\n```"
            )
        completed_artifact = (
            "# Completed-work artifact\n\n"
            "## Evidence-backed final state\n\n"
            "All required work and final acceptance completed.\n\n"
            "## Trajectory context\n\n"
            + (
                "The retained intermediate failure was resolved.\n\n"
                if resolved_failure
                else "No trajectory-only claims.\n\n"
            )
            + "## Remaining risks\n\nNo material risks remain in fixture scope.\n\n"
            "## Resolved-failure ledger\n\n"
            + resolved_failure_body
            + "\n"
        )
        trajectory = {
            "schema_version": "ATIF-v1.7",
            "steps": [
                {
                    "step_id": 0,
                    "source": "user",
                    "message": (
                        original_request
                        + "\n\nTreat the instruction above as the exact task scope."
                    ),
                },
                self._atif_exec_command_step(1, preflight_evidence),
                self._atif_ri_step(2, [("fuse", "benchmark-fuse")]),
                self._atif_ri_step(
                    3,
                    [
                        ("adversarial_gate", "benchmark-plan"),
                        ("adversarial_gate", "benchmark-pre-execution"),
                    ],
                ),
                self._atif_exec_command_step(4, final_evidence, enveloped=True),
                self._atif_ri_step(
                    5,
                    [
                        ("adversarial_gate", "benchmark-post-execution"),
                        ("adversarial_gate", "benchmark-final"),
                        ("adversarial_gate", "benchmark-summarize"),
                    ],
                ),
            ],
        }
        if resolved_failure:
            trajectory["steps"].insert(
                4,
                self._atif_exec_command_step(
                    40,
                    intermediate_evidence,
                    enveloped=True,
                    exit_code=1,
                ),
            )
        if secret:
            (root / "unindexed-raw-provider.log").write_text(
                "xai-" + "abcdefghijklmnop",
                encoding="utf-8",
            )

        (root / "ri").mkdir(parents=True)
        ri_refs = []
        decoded_results: dict[str, dict[str, object]] = {}
        config_hash = VALIDATOR.expected_benchmark_config_hash()
        fixture_identity = f"{task}:{attempt}:{identity_salt}"

        def make_receipt(
            run_id: str,
            receipt_stage: str,
            seat: str,
            model: str,
            receipt_index: int,
            expected_invocation: dict[str, object],
            response: dict[str, object] | None = None,
        ) -> tuple[
            dict[str, object],
            dict[str, object],
            dict[str, object],
            dict[str, object],
        ]:
            invocation = json.loads(json.dumps(expected_invocation))
            if invocation_hash_override is not None:
                override_run_id, override_stage, override_seat, override_field = (
                    invocation_hash_override
                )
                if (run_id, receipt_stage, seat) == (
                    override_run_id,
                    override_stage,
                    override_seat,
                ):
                    self.assertIn(
                        override_field,
                        {
                            "system_sha256",
                            "prompt_sha256",
                            "response_schema_sha256",
                        },
                    )
                    invocation[override_field] = VALIDATOR.canonical_json_hash(
                        [
                            "self-consistent-wrong-invocation-material",
                            run_id,
                            receipt_stage,
                            seat,
                            override_field,
                        ]
                    )
            invocation_sha256 = VALIDATOR.canonical_json_hash(invocation)
            attempt_index = receipt_index
            attempt_id = VALIDATOR.canonical_json_hash(
                {
                    "schema_version": 1,
                    "invocation_sha256": invocation_sha256,
                    "attempt_index": attempt_index,
                }
            )
            if response is None:
                response = {
                    "text": f"Synthetic response for {run_id}/{receipt_stage}/{seat}",
                    "provider": "xai_direct",
                    "requested_model": model,
                    "actual_model": model,
                }
            response_sha256 = VALIDATOR.canonical_json_hash(response)
            entry_id = VALIDATOR.call_receipt_entry_id(
                attempt_id,
                invocation_sha256,
                response_sha256,
            )
            entry = {
                "attempt_index": attempt_index,
                "attempt_id": attempt_id,
                "entry_id": entry_id,
                "invocation_sha256": invocation_sha256,
                "response_sha256": response_sha256,
                "response_artifact": f"responses/{entry_id}.json",
                "stage": receipt_stage,
                "seat": seat,
                "provider": "xai_direct",
                "requested_model": model,
                "actual_model": response["actual_model"],
                "usage": response.get("usage"),
                "latency_seconds": response.get("latency_seconds"),
                "request_id": response.get("request_id"),
                "route": response.get("route"),
                "raw_status": response.get("raw_status"),
            }
            attempt = {
                "attempt_index": attempt_index,
                "attempt_id": attempt_id,
                "invocation_sha256": invocation_sha256,
                "stage": receipt_stage,
                "seat": seat,
            }
            evidence = {
                "schema_version": 1,
                "entry_id": entry_id,
                "attempt_id": attempt_id,
                "invocation_sha256": invocation_sha256,
                "response_sha256": response_sha256,
            }
            raw_response_artifact = {
                "schema_version": 1,
                "invocation": invocation,
                "receipt": evidence,
                "response": response,
            }
            return entry, attempt, evidence, raw_response_artifact

        for index, (stage, run_id) in enumerate(VALIDATOR.EXPECTED_RUN_IDS.items()):
            run = root / "ri" / run_id
            run.mkdir()
            reviewed_artifact = (
                fused_artifact
                if stage in {"fuse", "plan", "pre_execution"}
                else completed_artifact
            )
            artifact_hash = sha256(reviewed_artifact.encode("utf-8")).hexdigest()
            mechanical_evidence = (
                preflight_evidence
                if stage in {"fuse", "plan", "pre_execution"}
                else final_evidence
            )
            call_arguments = {
                "task": fusion_task if stage == "fuse" else lifecycle_tasks[stage],
                "mechanical_evidence": mechanical_evidence,
                "resume_run_id": run_id,
            }
            if stage == "fuse":
                call_arguments["context"] = fusion_context
            else:
                call_arguments["artifact"] = reviewed_artifact
            task_hash, input_hash = VALIDATOR.expected_run_identity(stage, call_arguments)
            verdict = {
                "verdict": "PASS",
                "artifact_sha256": artifact_hash,
                "summary": "The synthetic artifact satisfies the fixture criteria.",
                "criteria_reviewed": ["Fixture requirement coverage"],
                "blind_spots": [],
                "blocking_findings": [],
                "non_blocking_findings": [],
                "evidence": ["Synthetic fixture evidence"],
                "required_actions": [],
            }
            ledger_specs = []
            if stage == "fuse":
                ledger_specs.extend(
                    [
                        ("panel", "grok45_researcher", "grok-4.5"),
                        ("panel", "grok45_adversary", "grok-4.5"),
                        ("panel", "grok45_constraint_auditor", "grok-4.5"),
                        ("judge", "grok45_judge", "grok-4.5"),
                        ("synthesis", "grok45_synthesizer", "grok-4.5"),
                    ]
                )
            ledger_specs.extend(
                [
                    ("gate", "grok45_verifier", "grok-4.5"),
                    ("gate", "grok45_constraint_auditor", "grok-4.5"),
                ]
            )
            panel_seats = (
                ("grok45_researcher", "Seat A"),
                ("grok45_adversary", "Seat B"),
                ("grok45_constraint_auditor", "Seat C"),
            )
            semantic_responses: dict[tuple[str, str], dict[str, object]] = {}
            for receipt_index, (receipt_stage, seat, model) in enumerate(ledger_specs):
                actual_model = (
                    "other-model"
                    if bad_provenance and seat == "grok45_verifier"
                    else model
                )
                response = {
                    "text": (
                        json.dumps(verdict, sort_keys=True)
                        if receipt_stage.startswith("gate")
                        else json.dumps(fixture_judgment, sort_keys=True)
                        if receipt_stage == "judge"
                        else fused_artifact
                        if receipt_stage == "synthesis"
                        else (
                            f"Synthetic response for {run_id}/{receipt_stage}/{seat}. "
                            "This independent panel analysis preserves the exact task "
                            "scope, identifies concrete implementation constraints, "
                            "checks model and provider provenance, and recommends "
                            "deterministic verification before acceptance. It also "
                            "examines likely failure modes, lifecycle boundaries, and "
                            "evidence requirements so the active Codex host can execute "
                            "the approved plan safely and completely."
                        )
                    ),
                    "provider": "xai_direct",
                    "requested_model": model,
                    "actual_model": actual_model,
                    "request_id": VALIDATOR.canonical_json_hash(
                        [fixture_identity, stage, receipt_stage, seat, receipt_index, "request"]
                    ),
                    "raw_status": "completed",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "reasoning_tokens": 1,
                        "cached_tokens": 2,
                        "tool_calls": 0,
                        "cost_usd": 0.01,
                        "unknown_cost_fail_closed": False,
                        "input_output_usage_complete": True,
                        "raw_usage_invalid": False,
                        "accounting_error": None,
                    },
                    "latency_seconds": 0.1,
                    "route": {},
                }
                semantic_responses[(receipt_stage, seat)] = response

            if stage == "fuse":
                prompt_panel_results = [
                    {
                        "anonymous_label": anonymous_label,
                        "role": "panel",
                        "response": semantic_responses[("panel", seat)],
                        "status": "completed",
                    }
                    for seat, anonymous_label in panel_seats
                ]
                expected_invocations = VALIDATOR.expected_fusion_invocations(
                    run_id=run_id,
                    input_sha256=input_hash,
                    config_sha256=config_hash,
                    arguments=call_arguments,
                    panel_artifact={"results": prompt_panel_results},
                    judge_artifact={"judgment": fixture_judgment},
                    synthesis_artifacts=[{"text": fused_artifact}],
                    gate_artifacts=[{"reviewers": [{"status": "completed"}]}],
                )
            else:
                expected_invocations = VALIDATOR.expected_lifecycle_invocations(
                    run_id=run_id,
                    input_sha256=input_hash,
                    config_sha256=config_hash,
                    arguments=call_arguments,
                )

            ledger_entries = []
            attempt_entries = []
            reviewer_receipts = {}
            reviewer_responses = {}
            response_receipts = {}
            raw_response_artifacts = []
            for receipt_index, (receipt_stage, seat, model) in enumerate(ledger_specs):
                response = semantic_responses[(receipt_stage, seat)]
                entry, attempt_receipt, evidence, raw_response_artifact = make_receipt(
                    run_id,
                    receipt_stage,
                    seat,
                    model,
                    receipt_index,
                    expected_invocations[(receipt_stage, seat)],
                    response,
                )
                ledger_entries.append(entry)
                attempt_entries.append(attempt_receipt)
                raw_response_artifacts.append((entry["entry_id"], raw_response_artifact))
                response_receipts[(receipt_stage, seat)] = evidence
                if receipt_stage == "gate":
                    reviewer_receipts[seat] = evidence
                    reviewer_responses[seat] = response
            gate = {
                "enabled": True,
                "passed": True,
                "artifact_sha256": artifact_hash,
                "pass_count": 2,
                "required_passes": 2,
                "fail_closed": True,
                "mechanical_failures": [],
                "mechanical_blocked": False,
                "schema_failures": [],
                "schema_blocked": False,
                "negative_verdicts": [],
                "negative_verdict_blocked": False,
                "unresolved_blind_spots": [],
                "blind_spot_blocked": False,
                "deterministic_blockers": [],
                "reviewers": [
                    {
                        "seat_name": "grok45_verifier",
                        "status": "completed",
                        "verdict": verdict,
                        "response": reviewer_responses["grok45_verifier"],
                        "response_evidence": reviewer_receipts["grok45_verifier"],
                    },
                    {
                        "seat_name": "grok45_constraint_auditor",
                        "status": "completed",
                        "verdict": verdict,
                        "response": reviewer_responses["grok45_constraint_auditor"],
                        "response_evidence": reviewer_receipts[
                            "grok45_constraint_auditor"
                        ],
                    },
                ],
            }
            created_at = f"2026-01-01T00:{index * 3:02d}:00+00:00"
            stage_updated_at = f"2026-01-01T00:{index * 3 + 1:02d}:00+00:00"
            manifest_updated_at = f"2026-01-01T00:{index * 3 + 2:02d}:00+00:00"
            stages = {
                "gate-0": {
                    "status": "passed",
                    "artifact": "gate-0.json",
                    "updated_at": stage_updated_at,
                }
            }
            if stage == "fuse":
                stages = {
                    "panel": {
                        "status": "completed",
                        "artifact": "panel.json",
                        "updated_at": stage_updated_at,
                    },
                    "judge": {
                        "status": "completed",
                        "artifact": "judge.json",
                        "updated_at": stage_updated_at,
                    },
                    "synthesis": {
                        "status": "completed",
                        "artifact": "synthesis.json",
                        "updated_at": stage_updated_at,
                    },
                    "gate-0": {
                        "status": "passed",
                        "artifact": "gate-0.json",
                        "updated_at": stage_updated_at,
                    },
                }
            manifest = {
                "run_id": run_id,
                "status": "completed",
                "task_hash": task_hash,
                "config_hash": config_hash,
                "input_hash": input_hash,
                "stages": stages,
                "created_at": created_at,
                "updated_at": manifest_updated_at,
            }
            input_tokens = sum(entry["usage"]["input_tokens"] for entry in ledger_entries)
            output_tokens = sum(entry["usage"]["output_tokens"] for entry in ledger_entries)
            reasoning_tokens = sum(
                entry["usage"]["reasoning_tokens"] for entry in ledger_entries
            )
            cached_tokens = sum(entry["usage"]["cached_tokens"] for entry in ledger_entries)
            tool_calls = sum(entry["usage"]["tool_calls"] for entry in ledger_entries)
            known_cost_usd = sum(entry["usage"]["cost_usd"] for entry in ledger_entries)
            ledger = {
                "schema_version": 3,
                "accounting_failure": None,
                "stop_reason": None,
                "attempts": len(ledger_entries),
                "calls": len(ledger_entries),
                "attempt_entries": attempt_entries,
                "entries": ledger_entries,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "cached_tokens": cached_tokens,
                "total_tokens": input_tokens + output_tokens,
                "tool_calls": tool_calls,
                "known_cost_usd": known_cost_usd,
                "provider_cost_usd": {"xai_direct": known_cost_usd},
                "unknown_cost_calls": 0,
                "wall_seconds": sum(entry["latency_seconds"] for entry in ledger_entries),
                "warnings": [],
            }
            (run / "manifest.json").write_text(json.dumps(manifest))
            (run / "ledger.json").write_text(json.dumps(ledger))
            (run / "gate-0.json").write_text(json.dumps(gate))
            (run / "responses").mkdir()
            for entry_id, raw_response_artifact in raw_response_artifacts:
                (run / "responses" / f"{entry_id}.json").write_text(
                    json.dumps(raw_response_artifact)
                )
            if stage == "fuse":
                panel_attempts = []
                panel_results = []
                for seat, anonymous_label in panel_seats:
                    row = {
                        "anonymous_label": "",
                        "error": None,
                        "response": semantic_responses[("panel", seat)],
                        "response_evidence": response_receipts[("panel", seat)],
                        "role": "panel",
                        "seat_name": seat,
                        "status": "completed",
                    }
                    panel_attempts.append(row)
                    panel_results.append({**row, "anonymous_label": anonymous_label})
                panel_artifact = {
                    "attempts": panel_attempts,
                    "degraded": False,
                    "failed_count": 0,
                    "live_count": 3,
                    "results": panel_results,
                }
                judge_artifact = {
                    "judgment": fixture_judgment,
                    "response": semantic_responses[("judge", "grok45_judge")],
                    "response_evidence": response_receipts[("judge", "grok45_judge")],
                }
                synthesis_artifact = {
                    "author_seat": "grok45_synthesizer",
                    "mode": "client_orchestrated",
                    "response": semantic_responses[("synthesis", "grok45_synthesizer")],
                    "response_evidence": response_receipts[
                        ("synthesis", "grok45_synthesizer")
                    ],
                    "sha256": sha256(fused_artifact.encode("utf-8")).hexdigest(),
                    "text": fused_artifact,
                }
                (run / "panel.json").write_text(json.dumps(panel_artifact))
                (run / "judge.json").write_text(json.dumps(judge_artifact))
                (run / "synthesis.json").write_text(json.dumps(synthesis_artifact))
                handoff = VALIDATOR.expected_fusion_handoff(
                    run_id=run_id,
                    synthesis=fused_artifact,
                    judgment=fixture_judgment,
                    ledger=ledger,
                    artifact_sha256=artifact_hash,
                )
                fusion_result = {
                    "run_id": run_id,
                    "task_hash": task_hash,
                    "config_hash": config_hash,
                    "status": "completed",
                    "synthesis": fused_artifact,
                    "gate": gate,
                    "panel": panel_results,
                    "judge": fixture_judgment,
                    "ledger": ledger,
                    "artifacts_dir": f"/logs/agent/relentless-inception/runs/{run_id}",
                    "execution_handoff": handoff,
                }
                decoded_results[stage] = fusion_result
                (run / "result.json").write_text(json.dumps(fusion_result))
                (run / "execution-handoff.json").write_text(json.dumps(handoff))
            else:
                decoded_results[stage] = {
                    "run_id": run_id,
                    "artifacts_dir": f"/logs/agent/relentless-inception/runs/{run_id}",
                    "gate": gate,
                    "ledger": ledger,
                }
            for retained_path in run.rglob("*"):
                retained_path.chmod(0o700 if retained_path.is_dir() else 0o600)
            run.chmod(0o700)
            ri_refs.append(
                {
                    "run_id": run_id,
                    "manifest": f"ri/{run_id}/manifest.json",
                    "ledger": f"ri/{run_id}/ledger.json",
                }
            )

        (root / "run-contract.json").write_text(json.dumps(contract))
        (root / "result.json").write_text(json.dumps(result))
        (root / "trajectory.json").write_text(json.dumps(trajectory))
        codex_events = [
            {
                "type": "thread.started",
                "thread_id": f"fixture-{fixture_identity}",
            },
            {"type": "turn.started"},
            {
                "type": "item.started",
                "item": {
                    "id": "preflight-command",
                    "type": "command_execution",
                    "status": "in_progress",
                    "command": self._canonical_shell_script("git status --short"),
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "preflight-command",
                    "type": "command_execution",
                    "status": "completed",
                    "command": self._canonical_shell_script("git status --short"),
                    "aggregated_output": preflight_evidence,
                    "exit_code": 0,
                },
            },
        ]
        for call_index, (stage, run_id) in enumerate(VALIDATOR.EXPECTED_RUN_IDS.items()):
            if stage == "post_execution":
                if resolved_failure:
                    codex_events.extend(
                        [
                            {
                                "type": "item.started",
                                "item": {
                                    "id": "resolved-intermediate-command",
                                    "type": "command_execution",
                                    "status": "in_progress",
                                    "command": self._canonical_shell_script("failing-check"),
                                },
                            },
                            {
                                "type": "item.completed",
                                "item": {
                                    "id": "resolved-intermediate-command",
                                    "type": "command_execution",
                                    "status": "failed",
                                    "command": self._canonical_shell_script("failing-check"),
                                    "aggregated_output": intermediate_evidence,
                                    "exit_code": 1,
                                },
                            },
                        ]
                    )
                codex_events.extend(
                    [
                        {
                            "type": "item.started",
                            "item": {
                                "id": "final-acceptance-command",
                                "type": "command_execution",
                                "status": "in_progress",
                                "command": self._canonical_shell_script("project-test"),
                            },
                        },
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "final-acceptance-command",
                                "type": "command_execution",
                                "status": "completed",
                                "command": self._canonical_shell_script("project-test"),
                                "aggregated_output": final_evidence,
                                "exit_code": 0,
                            },
                        },
                    ]
                )
            tool = "fuse" if stage == "fuse" else "adversarial_gate"
            lifecycle_tasks = {
                stage_name: task_prefix + original_request
                for stage_name, task_prefix in VALIDATOR.LIFECYCLE_TASK_PREFIXES.items()
            }
            arguments = {
                "resume_run_id": run_id,
                "task": (
                    VALIDATOR.FUSION_TASK_PREFIX + "\n" + original_request
                    if stage == "fuse"
                    else lifecycle_tasks[stage]
                ),
                "mechanical_evidence": (
                    preflight_evidence
                    if stage in {"fuse", "plan", "pre_execution"}
                    else final_evidence
                ),
            }
            if stage == "fuse":
                arguments["context"] = (
                    "The active Codex host, not an external seat, will execute the approved "
                    "plan. Provider-hosted tools are isolated from this workspace."
                )
            if stage != "fuse":
                arguments["artifact"] = (
                    fused_artifact
                    if stage in {"plan", "pre_execution"}
                    else completed_artifact
                )
            item_id = f"ri-{call_index}"
            common_item = {
                "id": item_id,
                "type": "mcp_tool_call",
                "server": "relentless-inception",
                "tool": tool,
                "arguments": arguments,
            }
            codex_events.append(
                {
                    "type": "item.started",
                    "item": {**common_item, "status": "in_progress"},
                }
            )
            codex_events.append(
                {
                    "type": "item.completed",
                    "item": {
                        **common_item,
                        "status": "completed",
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(decoded_results[stage]),
                                }
                            ],
                            "structured_content": None,
                        },
                        "error": None,
                    },
                }
            )
        codex_events.append(
            {
                "type": "turn.completed",
                "usage": {
                    "cache_write_input_tokens": 0,
                    "cached_input_tokens": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                },
            }
        )
        (root / "codex.txt").write_text(
            "\n".join(json.dumps(event) for event in codex_events) + "\n",
            encoding="utf-8",
        )
        evidence = {
            "schema_version": 1,
            "harness": harness,
            "task": task,
            "attempt": attempt,
            "contract": "run-contract.json",
            "result": "result.json",
            "trajectory": "trajectory.json",
            "codex_log": "codex.txt",
            "ri_runs": ri_refs,
        }
        if harness == "pier":
            reward = {"reward": 1, "apply_failed": False, **task_pin["expected"]}
            (root / "deep-reward.json").write_text(json.dumps(reward))
            evidence["deep_swe_reward"] = "deep-reward.json"
        evidence_path = root / "evidence.json"
        evidence_path.write_text(json.dumps(evidence))
        return evidence_path

    def test_validator_accepts_complete_positive_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            evidence = self._write_fixture(Path(temporary_directory))
            VALIDATOR.validate_attempt(evidence)

    def test_validator_rejects_self_consistent_invocation_material_drift(self) -> None:
        call_classes = (
            ("benchmark-fuse", "panel", "grok45_researcher"),
            ("benchmark-fuse", "judge", "grok45_judge"),
            ("benchmark-fuse", "synthesis", "grok45_synthesizer"),
            ("benchmark-fuse", "gate", "grok45_verifier"),
            *(
                (run_id, "gate", "grok45_verifier")
                for run_id in (
                    "benchmark-plan",
                    "benchmark-pre-execution",
                    "benchmark-post-execution",
                    "benchmark-final",
                    "benchmark-summarize",
                )
            ),
        )
        hash_fields = (
            "system_sha256",
            "prompt_sha256",
            "response_schema_sha256",
        )
        for run_id, receipt_stage, seat in call_classes:
            for hash_field in hash_fields:
                with (
                    self.subTest(
                        run_id=run_id,
                        receipt_stage=receipt_stage,
                        seat=seat,
                        hash_field=hash_field,
                    ),
                    tempfile.TemporaryDirectory() as temporary_directory,
                ):
                    evidence = self._write_fixture(
                        Path(temporary_directory),
                        invocation_hash_override=(
                            run_id,
                            receipt_stage,
                            seat,
                            hash_field,
                        ),
                    )
                    with self.assertRaisesRegex(
                        VALIDATOR.EvidenceError,
                        hash_field,
                    ):
                        VALIDATOR.validate_attempt(evidence)

    def test_expected_invocations_cover_two_amendment_rounds(self) -> None:
        arguments = {
            "task": "Plan the exact synthetic amendment fixture.",
            "context": "Synthetic context with <fenced> Unicode evidence: λ.",
            "mechanical_evidence": "$ fixture-check\n[exit 0]\n",
            "resume_run_id": "benchmark-fuse",
        }
        panel_results = [
            {
                "anonymous_label": anonymous_label,
                "role": "panel",
                "status": "completed",
                "response": {"text": f"Independent report from {anonymous_label}."},
            }
            for anonymous_label in ("Seat A", "Seat B", "Seat C")
        ]
        judgment = {
            "consensus": ["Preserve the exact task."],
            "contradictions": [],
            "partial_coverage": [],
            "unique_insights": [],
            "minority_findings": [],
            "blind_spots": [],
            "final_guidance": ["Amend until both gates pass."],
        }

        def rejected_gate(summary: str) -> dict[str, object]:
            return {
                "deterministic_blockers": [summary],
                "reviewers": [
                    {
                        "status": "completed",
                        "verdict": {
                            "verdict": "NEEDS_WORK",
                            "summary": summary,
                            "blind_spots": [],
                            "blocking_findings": [summary],
                            "required_actions": ["Correct the candidate."],
                            "evidence": ["Synthetic amendment evidence."],
                        },
                    }
                ],
            }

        gate_artifacts = [
            rejected_gate("Base synthesis needs revision."),
            rejected_gate("First amendment still needs revision."),
            {"deterministic_blockers": [], "reviewers": [{"status": "completed"}]},
        ]
        synthesis_artifacts = [
            {"text": "Base synthetic artifact."},
            {"text": "Distinct first amended artifact."},
            {"text": "Distinct second amended artifact that passes."},
        ]
        expected = VALIDATOR.expected_fusion_invocations(
            run_id="benchmark-fuse",
            input_sha256="1" * 64,
            config_sha256=VALIDATOR.expected_benchmark_config_hash(),
            arguments=arguments,
            panel_artifact={"results": panel_results},
            judge_artifact={"judgment": judgment},
            synthesis_artifacts=synthesis_artifacts,
            gate_artifacts=gate_artifacts,
        )
        self.assertEqual(len(expected), 13)
        self.assertEqual(
            {stage for stage, _seat in expected},
            {
                "panel",
                "judge",
                "synthesis",
                "gate",
                "amendment-1",
                "gate-1",
                "amendment-2",
                "gate-2",
            },
        )

        changed_gate_artifacts = copy.deepcopy(gate_artifacts)
        changed_gate_artifacts[0]["reviewers"][0]["verdict"]["summary"] = (
            "Different retained feedback ordering and content."
        )
        changed = VALIDATOR.expected_fusion_invocations(
            run_id="benchmark-fuse",
            input_sha256="1" * 64,
            config_sha256=VALIDATOR.expected_benchmark_config_hash(),
            arguments=arguments,
            panel_artifact={"results": panel_results},
            judge_artifact={"judgment": judgment},
            synthesis_artifacts=synthesis_artifacts,
            gate_artifacts=changed_gate_artifacts,
        )
        amendment_key = ("amendment-1", "grok45_synthesizer")
        self.assertNotEqual(
            expected[amendment_key]["prompt_sha256"],
            changed[amendment_key]["prompt_sha256"],
        )

        for hash_field in (
            "system_sha256",
            "prompt_sha256",
            "response_schema_sha256",
        ):
            with self.subTest(amendment_hash_field=hash_field):
                observed = copy.deepcopy(expected)
                observed[amendment_key][hash_field] = "f" * 64
                with self.assertRaisesRegex(VALIDATOR.EvidenceError, hash_field):
                    VALIDATOR.validate_expected_invocations(
                        observed,
                        expected,
                        label="Synthetic amendment",
                    )

    def test_validator_refuses_tampered_plugin_before_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            temporary_bench = temporary_root / "bench"
            temporary_plugin = temporary_root / "plugins" / "relentless-inception"
            temporary_bench.mkdir(parents=True)
            shutil.copy2(BENCH / "validate_evidence.py", temporary_bench)
            shutil.copy2(BENCH / "pins.json", temporary_bench)
            shutil.copytree(ROOT / "plugins" / "relentless-inception", temporary_plugin)
            marker = temporary_root / "plugin-code-executed"
            prompts_path = (
                temporary_plugin / "relentless_inception" / "prompts.py"
            )
            prompts_source = prompts_path.read_text(encoding="utf-8")
            prompts_source = prompts_source.replace(
                "from __future__ import annotations\n",
                "from __future__ import annotations\n"
                "from pathlib import Path as _TamperPath\n"
                f"_TamperPath({str(marker)!r}).write_text('executed')\n",
                1,
            )
            prompts_path.write_text(prompts_source, encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, str(temporary_bench / "validate_evidence.py")],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("plugin source tree drift before validator", completed.stderr)
            self.assertFalse(marker.exists())

    def test_validator_refuses_every_precached_plugin_module(self) -> None:
        plugin_root = ROOT / "plugins" / "relentless-inception"
        validator_path = BENCH / "validate_evidence.py"
        cases = (
            ("relentless_inception", None),
            ("relentless_inception.orchestrator", None),
            (
                "relentless_inception.prompts",
                plugin_root / "relentless_inception" / "prompts.py",
            ),
        )
        for cached_module_name, claimed_module_path in cases:
            with self.subTest(cached_module_name=cached_module_name):
                script = "\n".join(
                    (
                        "import importlib.util",
                        "import sys",
                        "import types",
                        f"foreign_module = types.ModuleType({cached_module_name!r})",
                        *(
                            (f"foreign_module.__file__ = {str(claimed_module_path)!r}",)
                            if claimed_module_path is not None
                            else ()
                        ),
                        f"sys.modules[{cached_module_name!r}] = foreign_module",
                        "spec = importlib.util.spec_from_file_location(",
                        f"    'foreign_cache_validator', {str(validator_path)!r}",
                        ")",
                        "module = importlib.util.module_from_spec(spec)",
                        "spec.loader.exec_module(module)",
                    )
                )
                completed = subprocess.run(
                    [sys.executable, "-c", script],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(
                    "refuses a pre-cached relentless_inception module",
                    completed.stderr,
                )
                self.assertIn(cached_module_name, completed.stderr)

    def test_validator_refuses_a_monkeypatched_genuine_plugin_import(self) -> None:
        plugin_root = ROOT / "plugins" / "relentless-inception"
        validator_path = BENCH / "validate_evidence.py"
        script = "\n".join(
            (
                "import importlib.util",
                "import sys",
                f"sys.path.insert(0, {str(plugin_root)!r})",
                "import relentless_inception.prompts as prompts",
                "prompts.panel_prompt = lambda *_args, **_kwargs: 'forged'",
                "spec = importlib.util.spec_from_file_location(",
                f"    'monkeypatched_cache_validator', {str(validator_path)!r}",
                ")",
                "module = importlib.util.module_from_spec(spec)",
                "spec.loader.exec_module(module)",
            )
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "refuses a pre-cached relentless_inception module",
            completed.stderr,
        )

    def test_validator_enforces_exact_codex_control_envelope(self) -> None:
        for case_name in (
            "missing_thread",
            "inverted_start",
            "duplicate_turn_start",
            "early_turn_completion",
            "turn_usage_schema_drift",
            "negative_turn_usage",
        ):
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._write_fixture(root)
                codex_path = root / "codex.txt"
                trajectory = json.loads((root / "trajectory.json").read_text())
                events = [json.loads(line) for line in codex_path.read_text().splitlines()]
                if case_name == "missing_thread":
                    events.pop(0)
                elif case_name == "inverted_start":
                    events[0], events[1] = events[1], events[0]
                elif case_name == "duplicate_turn_start":
                    events.insert(2, {"type": "turn.started"})
                elif case_name == "early_turn_completion":
                    completion = events.pop()
                    events.insert(2, completion)
                elif case_name == "turn_usage_schema_drift":
                    events[-1]["usage"]["total_tokens"] = 0
                else:
                    events[-1]["usage"]["input_tokens"] = -1
                codex_path.write_text(
                    "\n".join(json.dumps(event) for event in events) + "\n"
                )
                with self.assertRaises(VALIDATOR.EvidenceError):
                    VALIDATOR.validate_codex_log(codex_path, trajectory)

    def test_validator_enforces_attempt_stage_reservation_order(self) -> None:
        valid_fusion_attempts = [
            {"stage": "panel"},
            {"stage": "panel"},
            {"stage": "judge"},
            {"stage": "synthesis"},
            {"stage": "gate"},
            {"stage": "amendment-1"},
            {"stage": "gate-1"},
        ]
        VALIDATOR.validate_attempt_stage_order(valid_fusion_attempts, fusion_run=True)
        VALIDATOR.validate_attempt_stage_order(
            [{"stage": "gate"}, {"stage": "gate"}], fusion_run=False
        )
        with self.assertRaises(VALIDATOR.EvidenceError):
            VALIDATOR.validate_attempt_stage_order(
                [{"stage": "judge"}, {"stage": "panel"}], fusion_run=True
            )
        with self.assertRaises(VALIDATOR.EvidenceError):
            VALIDATOR.validate_attempt_stage_order(
                [{"stage": "gate"}, {"stage": "panel"}], fusion_run=False
            )

    def test_validator_applies_pinned_fusion_quality_floor(self) -> None:
        good = (
            "This retained analysis identifies exact implementation requirements, "
            "explains the important risks, and provides deterministic verification "
            "steps for the active Codex host. It preserves provider provenance, "
            "workspace boundaries, lifecycle gates, and complete evidence so that "
            "acceptance follows from concrete results rather than unsupported claims. "
            "The recommendation is specific, executable, and independently reviewable."
        )
        VALIDATOR.validate_fusion_quality(good, "fixture quality response")
        bad_outputs = (
            "Too short.",
            ("<tool_call> leaked markup with several substantive words. " * 8),
            ("I cannot assist with this ordinary task. " * 10),
            ("fragment\n" * 30),
        )
        for bad_output in bad_outputs:
            with self.subTest(bad_output=bad_output[:30]):
                with self.assertRaises(VALIDATOR.EvidenceError):
                    VALIDATOR.validate_fusion_quality(bad_output, "fixture quality response")

    def test_validator_rejects_exact_runtime_schema_and_identity_drift(self) -> None:
        cases = (
            "extra_call_argument",
            "manifest_task_hash_drift",
            "missing_stage_timestamp",
            "lifecycle_result_extra_field",
            "lifecycle_artifacts_dir_drift",
            "ledger_missing_counter",
            "ledger_aggregate_drift",
            "invocation_schema_name_drift",
            "raw_response_extra_field",
            "fusion_result_extra_field",
        )
        for case_name in cases:
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence = self._write_fixture(root)
                codex_path = root / "codex.txt"
                events = [json.loads(line) for line in codex_path.read_text().splitlines()]
                plan_run = root / "ri" / "benchmark-plan"
                if case_name == "extra_call_argument":
                    for event in events:
                        item = event.get("item", {})
                        if item.get("id") == "ri-1":
                            item["arguments"]["profile_name"] = "maximum_intelligence"
                    codex_path.write_text(
                        "\n".join(json.dumps(event) for event in events) + "\n"
                    )
                elif case_name in {
                    "manifest_task_hash_drift",
                    "missing_stage_timestamp",
                }:
                    manifest_path = plan_run / "manifest.json"
                    manifest = json.loads(manifest_path.read_text())
                    if case_name == "manifest_task_hash_drift":
                        manifest["task_hash"] = "b" * 64
                    else:
                        manifest["stages"]["gate-0"].pop("updated_at")
                    manifest_path.write_text(json.dumps(manifest))
                elif case_name in {
                    "lifecycle_result_extra_field",
                    "lifecycle_artifacts_dir_drift",
                }:
                    completed = next(
                        event
                        for event in events
                        if event.get("type") == "item.completed"
                        and event.get("item", {}).get("id") == "ri-1"
                    )
                    block = completed["item"]["result"]["content"][0]
                    decoded = json.loads(block["text"])
                    if case_name == "lifecycle_result_extra_field":
                        decoded["status"] = "completed"
                    else:
                        decoded["artifacts_dir"] = "/tmp/unretained-run"
                    block["text"] = json.dumps(decoded)
                    codex_path.write_text(
                        "\n".join(json.dumps(event) for event in events) + "\n"
                    )
                elif case_name in {"ledger_missing_counter", "ledger_aggregate_drift"}:
                    ledger_path = plan_run / "ledger.json"
                    ledger = json.loads(ledger_path.read_text())
                    if case_name == "ledger_missing_counter":
                        ledger.pop("total_tokens")
                    else:
                        ledger["input_tokens"] += 1
                        ledger["total_tokens"] += 1
                    ledger_path.write_text(json.dumps(ledger))
                elif case_name in {
                    "invocation_schema_name_drift",
                    "raw_response_extra_field",
                }:
                    ledger = json.loads((plan_run / "ledger.json").read_text())
                    response_path = plan_run / ledger["entries"][0]["response_artifact"]
                    raw_response = json.loads(response_path.read_text())
                    if case_name == "invocation_schema_name_drift":
                        raw_response["invocation"]["schema_name"] = "structured_response"
                    else:
                        raw_response["response"]["unexpected"] = True
                    response_path.write_text(json.dumps(raw_response))
                else:
                    fusion_run = root / "ri" / "benchmark-fuse"
                    result_path = fusion_run / "result.json"
                    result = json.loads(result_path.read_text())
                    result["unexpected"] = True
                    result_path.write_text(json.dumps(result))
                    completed = next(
                        event
                        for event in events
                        if event.get("type") == "item.completed"
                        and event.get("item", {}).get("id") == "ri-0"
                    )
                    completed["item"]["result"]["content"][0]["text"] = json.dumps(result)
                    codex_path.write_text(
                        "\n".join(json.dumps(event) for event in events) + "\n"
                    )
                with self.assertRaises(VALIDATOR.EvidenceError):
                    VALIDATOR.validate_attempt(evidence)

    def test_gate_validator_accepts_exact_rejected_runtime_shapes(self) -> None:
        artifact_hash = sha256(b"Rejected candidate artifact").hexdigest()
        reviewer_specs = (
            ("grok45_verifier", "grok-4.5", "PASS"),
            ("grok45_constraint_auditor", "grok-4.5", "NEEDS_WORK"),
        )
        reviewers = []
        ledger_entries = []
        responses_by_entry_id = {}
        for reviewer_index, (seat, model, verdict_label) in enumerate(reviewer_specs):
            verdict = {
                "verdict": verdict_label,
                "artifact_sha256": artifact_hash,
                "summary": "The candidate needs revision." if verdict_label != "PASS" else "The reviewed criteria pass.",
                "criteria_reviewed": ["Exact artifact behavior"],
                "blind_spots": [],
                "blocking_findings": (
                    ["One required behavior is missing."] if verdict_label != "PASS" else []
                ),
                "non_blocking_findings": [],
                "evidence": ["Synthetic rejected-gate evidence"],
                "required_actions": (
                    ["Add the missing behavior."] if verdict_label != "PASS" else []
                ),
            }
            response = {
                "text": json.dumps(verdict, sort_keys=True),
                "provider": "xai_direct",
                "requested_model": model,
                "actual_model": model,
                "request_id": f"rejected-gate-{reviewer_index}",
                "raw_status": "completed",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "reasoning_tokens": 1,
                    "cached_tokens": 2,
                    "tool_calls": 0,
                    "cost_usd": 0.01,
                    "unknown_cost_fail_closed": False,
                    "input_output_usage_complete": True,
                    "raw_usage_invalid": False,
                    "accounting_error": None,
                },
                "latency_seconds": 0.1,
                "route": {},
            }
            response_sha256 = VALIDATOR.canonical_json_hash(response)
            evidence = {
                "schema_version": 1,
                "entry_id": f"{reviewer_index + 1:064x}",
                "attempt_id": f"{reviewer_index + 11:064x}",
                "invocation_sha256": f"{reviewer_index + 21:064x}",
                "response_sha256": response_sha256,
            }
            reviewers.append(
                {
                    "seat_name": seat,
                    "status": "completed",
                    "verdict": verdict,
                    "response": response,
                    "response_evidence": evidence,
                }
            )
            ledger_entries.append(
                {
                    **evidence,
                    "stage": "gate-1",
                    "seat": seat,
                    "provider": "xai_direct",
                    "requested_model": model,
                    "actual_model": model,
                }
            )
            responses_by_entry_id[evidence["entry_id"]] = response

        negative_verdict = {
            "seat_name": "grok45_constraint_auditor",
            "verdict": "NEEDS_WORK",
            "summary": "The candidate needs revision.",
            "blocking_findings": ["One required behavior is missing."],
            "required_actions": ["Add the missing behavior."],
            "evidence": ["Synthetic rejected-gate evidence"],
        }
        rejected_gate = {
            "enabled": True,
            "passed": False,
            "artifact_sha256": artifact_hash,
            "pass_count": 1,
            "required_passes": 2,
            "fail_closed": True,
            "mechanical_failures": [],
            "mechanical_blocked": False,
            "schema_failures": [],
            "schema_blocked": False,
            "negative_verdicts": [negative_verdict],
            "negative_verdict_blocked": True,
            "unresolved_blind_spots": [],
            "blind_spot_blocked": False,
            "deterministic_blockers": [
                "At least one reviewer returned a blocking negative verdict: "
                "grok45_constraint_auditor: NEEDS_WORK"
            ],
            "reviewers": reviewers,
        }
        VALIDATOR.validate_gate_artifact(
            rejected_gate,
            ledger_entries=ledger_entries,
            responses_by_entry_id=responses_by_entry_id,
            expected_gate_stage="gate-1",
            expected_hash=artifact_hash,
            expected_passed=False,
        )

        independent_rejection = {
            "enabled": True,
            "passed": False,
            "artifact_sha256": artifact_hash,
            "pass_count": 0,
            "required_passes": 2,
            "fail_closed": True,
            "deterministic_blockers": [
                "The amendment is byte-identical to the rejected artifact; a fresh corrected artifact is required."
            ],
            "reviewers": [],
        }
        VALIDATOR.validate_gate_artifact(
            independent_rejection,
            ledger_entries=[],
            responses_by_entry_id={},
            expected_gate_stage="gate-1",
            expected_hash=artifact_hash,
            expected_passed=False,
        )

        rejected_gate["negative_verdict_blocked"] = False
        with self.assertRaises(VALIDATOR.EvidenceError):
            VALIDATOR.validate_gate_artifact(
                rejected_gate,
                ledger_entries=ledger_entries,
                responses_by_entry_id=responses_by_entry_id,
                expected_gate_stage="gate-1",
                expected_hash=artifact_hash,
                expected_passed=False,
            )

    def test_validator_accepts_sequential_calls_grouped_by_functions_exec(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            evidence = self._write_fixture(root)
            grouped_trajectory = {
                "schema_version": "ATIF-v1.7",
                "steps": [
                    {
                        "step_id": 0,
                        "source": "user",
                        "message": (
                            "Implement and verify the exact fixture benchmark request."
                            "\n\nTreat the instruction above as the exact task scope."
                        ),
                    },
                    self._atif_exec_command_step(
                        1, "$ git status --short\n[exit 0]\n"
                    ),
                    {
                        "function_name": "exec",
                        "arguments": {
                            "input": (
                                "await tools.mcp__relentless_inception__fuse({"
                                'resume_run_id: "benchmark-fuse"});'
                            )
                        },
                    },
                    {
                        "function_name": "exec",
                        "arguments": {
                            "input": (
                                'const retainedPlanId = "benchmark-plan";'
                                "await tools.mcp__relentless_inception__adversarial_gate({"
                                'resume_run_id: "benchmark-plan"});'
                                "await tools.mcp__relentless_inception__adversarial_gate({"
                                'resume_run_id: "benchmark-pre-execution"});'
                            )
                        },
                    },
                    self._atif_exec_command_step(
                        2, "$ project-test\n27 tests passed\n[exit 0]\n"
                    ),
                    {
                        "function_name": "exec",
                        "arguments": {
                            "input": (
                                "await tools.mcp__relentless_inception__adversarial_gate({"
                                'resume_run_id: "benchmark-post-execution"});'
                                "await tools.mcp__relentless_inception__adversarial_gate({"
                                'resume_run_id: "benchmark-final"});'
                                "await tools.mcp__relentless_inception__adversarial_gate({"
                                'resume_run_id: "benchmark-summarize"});'
                            )
                        },
                    },
                ],
            }
            (root / "trajectory.json").write_text(
                json.dumps(grouped_trajectory), encoding="utf-8"
            )
            VALIDATOR.validate_attempt(evidence)

    def test_validator_accepts_streamed_final_wrapper_and_resolved_intermediate_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self._write_fixture(root, resolved_failure=True)
            with self.assertRaises(VALIDATOR.EvidenceError):
                VALIDATOR.validate_resolved_failure_ledger(
                    "## Resolved-failure ledger\n\nNo resolved failures.\n",
                    [
                        ("$ failing-check\nknown transient failure\n[exit 1]\n", 1),
                        ("$ project-test\n27 tests passed\n[exit 0]\n", 0),
                    ],
                )
            trajectory_path = root / "trajectory.json"
            trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
            final_source = trajectory["steps"][5]["tool_calls"][0]["arguments"]["input"]
            final_source = final_source.replace(
                "const r = await",
                "let r = await",
                1,
            ).replace(
                "text(JSON.stringify({exit_code:r.exit_code, output:r.output}));",
                "let transcript=r.output;"
                "while(r.session_id != null){"
                "r=await tools.write_stdin({session_id:r.session_id,chars:'',yield_time_ms:60000});"
                "transcript+=r.output;}"
                "text(JSON.stringify({exit_code:r.exit_code, output:transcript}));",
            )
            trajectory["steps"][5]["tool_calls"][0]["arguments"]["input"] = final_source
            trajectory_path.write_text(json.dumps(trajectory), encoding="utf-8")

            VALIDATOR.validate_attempt(evidence)

    def test_validator_rejects_missing_lifecycle_wrapper_and_substituted_request(self) -> None:
        for run_id in (
            "benchmark-plan",
            "benchmark-final",
            "benchmark-summarize",
        ):
            with self.subTest(run_id=run_id), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence = self._write_fixture(root)
                trajectory_path = root / "trajectory.json"
                trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
                for step in trajectory["steps"]:
                    for call in step.get("tool_calls", []):
                        source = call.get("arguments", {}).get("input")
                        if isinstance(source, str) and run_id in source:
                            call["arguments"]["input"] = source.replace(run_id, "omitted-run")
                trajectory_path.write_text(json.dumps(trajectory), encoding="utf-8")
                with self.assertRaises(VALIDATOR.EvidenceError):
                    VALIDATOR.validate_attempt(evidence)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self._write_fixture(root)
            codex_path = root / "codex.txt"
            events = [json.loads(line) for line in codex_path.read_text().splitlines()]
            original = "Implement and verify the exact fixture benchmark request."
            substituted = "Perform a different substituted benchmark request."
            for event in events:
                arguments = event.get("item", {}).get("arguments", {})
                task = arguments.get("task")
                if isinstance(task, str):
                    arguments["task"] = task.replace(original, substituted)
            codex_path.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(VALIDATOR.EvidenceError, "exact ATIF benchmark"):
                VALIDATOR.validate_attempt(evidence)

        for mutation in ("\nConflicting appended scope.", "\n" + original):
            with self.subTest(task_mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence = self._write_fixture(root)
                codex_path = root / "codex.txt"
                events = [json.loads(line) for line in codex_path.read_text().splitlines()]
                for event in events:
                    item = event.get("item", {})
                    arguments = item.get("arguments", {})
                    if arguments.get("resume_run_id") == "benchmark-final":
                        arguments["task"] += mutation
                codex_path.write_text(
                    "\n".join(json.dumps(event) for event in events) + "\n"
                )
                with self.assertRaisesRegex(VALIDATOR.EvidenceError, "exact lifecycle template"):
                    VALIDATOR.validate_attempt(evidence)

    def test_validator_accepts_read_only_test_discovery_and_rejects_preflight_mutation(self) -> None:
        for command, should_pass in (
            ('rg -n "go test|pytest|git merge|git commit" README.md Makefile', True),
            ("command -v rg", True),
            ("git branch --all --verbose --verbose --no-abbrev", True),
            ("git reflog --all --date=iso --max-count=24", True),
            ("git stash list --date=iso", True),
            ("git diff HEAD --stat && git diff HEAD --name-status", True),
            (
                "rg --files -g AGENTS.md -g package.json | sed -n '1,80p'",
                True,
            ),
            (
                "git show --format=fuller --stat --name-status --patch HEAD | sed -n '1,260p'",
                True,
            ),
            ("touch /tmp/PWNED", False),
            ("git update-ref refs/heads/audit HEAD", False),
            ("printf owned > AUDIT_MUTATION", False),
            ("command touch /tmp/PWNED", False),
            ("sort -o /tmp/PWNED README.md", False),
            ("rg --pre='touch /tmp/PWNED' x README.md", False),
            ("git diff --output=/tmp/PWNED", False),
            ("./git status --short", False),
            ("/tmp/git status --short", False),
            ("git remote -v remove origin", False),
            ("git config --add x.y z --get x.y", False),
            ("git tag --list -d", False),
            ("cat /solution/answer", False),
            ("head -20 ../verifier/reward.json", False),
            ("rg --files /run/secrets", False),
            ("sed -n 'e touch /tmp/PWNED' README.md", False),
            ("sed -n 'w /tmp/PWNED' README.md", False),
            ("git reflog expire --expire=now --all", False),
            ("git reflog delete 'HEAD@{0}'", False),
            ("git branch -D master -a", False),
            ("git branch --edit-description --list master", False),
            ("git grep --open-files-in-pager=touch x", False),
            ("git grep -O touch x", False),
            ("git cat-file --filters HEAD:README.md", False),
            ("sort --compress-program=touch README.md", False),
            ("file --compile -m /tmp/magic", False),
        ):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence = self._write_fixture(root)
                output = f"$ {command}\n[exit 0]\n"
                trajectory_path = root / "trajectory.json"
                trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
                trajectory["steps"][1] = self._atif_exec_command_step(1, output)
                trajectory_path.write_text(json.dumps(trajectory), encoding="utf-8")
                codex_path = root / "codex.txt"
                events = [json.loads(line) for line in codex_path.read_text().splitlines()]
                for event in events:
                    item = event.get("item", {})
                    if item.get("id") == "preflight-command":
                        item["command"] = self._canonical_shell_script(command)
                        if event.get("type") == "item.completed":
                            item["aggregated_output"] = output
                    arguments = item.get("arguments", {})
                    if arguments.get("resume_run_id") in {
                        "benchmark-fuse",
                        "benchmark-plan",
                        "benchmark-pre-execution",
                    }:
                        arguments["mechanical_evidence"] = output
                codex_path.write_text(
                    "\n".join(json.dumps(event) for event in events) + "\n",
                    encoding="utf-8",
                )
                if should_pass:
                    VALIDATOR.validate_codex_log(codex_path, trajectory)
                else:
                    with self.assertRaises(VALIDATOR.EvidenceError):
                        VALIDATOR.validate_codex_log(codex_path, trajectory)

    def test_validator_enforces_exact_preflight_limits(self) -> None:
        prefix = "$ pwd\n"
        suffix = "\n[exit 0]\n"
        for length, should_pass in ((12_000, True), (12_001, False)):
            with self.subTest(length=length), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence_path = self._write_fixture(root)
                exact_evidence = prefix + ("x" * (length - len(prefix) - len(suffix))) + suffix

                trajectory_path = root / "trajectory.json"
                trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
                trajectory["steps"][1] = self._atif_exec_command_step(1, exact_evidence)
                trajectory_path.write_text(json.dumps(trajectory), encoding="utf-8")

                codex_path = root / "codex.txt"
                events = [
                    json.loads(line)
                    for line in codex_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                for event in events:
                    arguments = event.get("item", {}).get("arguments", {})
                    if arguments.get("resume_run_id") in {
                        "benchmark-fuse",
                        "benchmark-plan",
                        "benchmark-pre-execution",
                    }:
                        arguments["mechanical_evidence"] = exact_evidence
                    item = event.get("item", {})
                    if (
                        event.get("type") == "item.completed"
                        and item.get("id") == "preflight-command"
                    ):
                        item["aggregated_output"] = exact_evidence
                    if item.get("id") == "preflight-command":
                        item["command"] = self._canonical_shell_script("pwd")
                codex_path.write_text(
                    "\n".join(json.dumps(event) for event in events) + "\n",
                    encoding="utf-8",
                )
                if should_pass:
                    VALIDATOR.validate_codex_log(codex_path, trajectory)
                else:
                    with self.assertRaisesRegex(
                        VALIDATOR.EvidenceError, "exceeds 12,000 characters"
                    ):
                        VALIDATOR.validate_codex_log(codex_path, trajectory)

    def test_validator_rejects_returned_result_and_event_chronology_drift(self) -> None:
        cases = (
            "returned_run_id",
            "returned_false_gate",
            "completion_before_start",
            "mutation_before_pre_execution",
            "midstream_non_json",
            "dual_result_conflict",
        )
        for case_name in cases:
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence_path = self._write_fixture(root)
                codex_path = root / "codex.txt"
                events = [
                    json.loads(line)
                    for line in codex_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                if case_name in {
                    "returned_run_id",
                    "returned_false_gate",
                    "dual_result_conflict",
                }:
                    completed = next(
                        event
                        for event in events
                        if event.get("type") == "item.completed"
                        and event.get("item", {}).get("id") == "ri-5"
                    )
                    block = completed["item"]["result"]["content"][0]
                    decoded = json.loads(block["text"])
                    if case_name == "dual_result_conflict":
                        completed["item"]["result"]["structured_content"] = decoded
                        contradicted = json.loads(json.dumps(decoded))
                        contradicted["gate"]["passed"] = False
                        block["text"] = json.dumps(contradicted)
                    elif case_name == "returned_run_id":
                        decoded["run_id"] = "benchmark-wrong"
                    else:
                        decoded["gate"]["passed"] = False
                    if case_name != "dual_result_conflict":
                        block["text"] = json.dumps(decoded)
                elif case_name == "completion_before_start":
                    completion_index = next(
                        index
                        for index, event in enumerate(events)
                        if event.get("type") == "item.completed"
                        and event.get("item", {}).get("id") == "ri-1"
                    )
                    completion = events.pop(completion_index)
                    start_index = next(
                        index
                        for index, event in enumerate(events)
                        if event.get("type") == "item.started"
                        and event.get("item", {}).get("id") == "ri-1"
                    )
                    events.insert(start_index, completion)
                elif case_name == "mutation_before_pre_execution":
                    pre_start_index = next(
                        index
                        for index, event in enumerate(events)
                        if event.get("type") == "item.started"
                        and event.get("item", {}).get("id") == "ri-2"
                    )
                    events.insert(
                        pre_start_index,
                        {
                            "type": "item.started",
                            "item": {
                                "id": "premature-file-change",
                                "type": "file_change",
                                "status": "in_progress",
                            },
                        },
                    )
                if case_name == "midstream_non_json":
                    serialized = [json.dumps(event) for event in events]
                    serialized.insert(2, "late warning")
                else:
                    serialized = [json.dumps(event) for event in events]
                codex_path.write_text("\n".join(serialized) + "\n", encoding="utf-8")
                with self.assertRaises(VALIDATOR.EvidenceError):
                    VALIDATOR.validate_attempt(evidence_path)

    def test_validator_rejects_exact_transcript_and_handoff_drift(self) -> None:
        for case_name in (
            "preflight_byte",
            "final_byte",
            "source_call_id",
            "nonzero_envelope",
            "shell_after_post",
            "premature_ready_handoff",
            "self_consistent_weakened_handoff",
        ):
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence_path = self._write_fixture(root)
                if case_name in {
                    "premature_ready_handoff",
                    "self_consistent_weakened_handoff",
                }:
                    run = root / "ri" / "benchmark-fuse"
                    handoff_path = run / "execution-handoff.json"
                    result_path = run / "result.json"
                    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
                    if case_name == "premature_ready_handoff":
                        handoff.update(
                            {
                                "status": "ready_for_execution",
                                "ready": True,
                                "mutation_authorized": True,
                            }
                        )
                    else:
                        handoff["selected_profile"] = "weakened-fixture-profile"
                        execution_contract = handoff["execution_contract"]
                        execution_contract["remote_models_may_write_workspace"] = True
                        execution_contract["require_pre_execution_gate"] = False
                        execution_contract["require_post_execution_gate"] = False
                        execution_contract["run_tests"] = False
                        execution_contract["require_diff_review"] = False
                        handoff["execution_contract_sha256"] = (
                            VALIDATOR.canonical_json_hash(execution_contract)
                        )
                        handoff["handoff_contract_sha256"] = (
                            VALIDATOR.canonical_json_hash(
                                {
                                    "selected_profile": handoff["selected_profile"],
                                    "execution_contract": execution_contract,
                                }
                            )
                        )
                        payload = json.loads(json.dumps(handoff))
                        payload.pop("handoff_payload_sha256")
                        handoff["handoff_payload_sha256"] = (
                            VALIDATOR.canonical_json_hash(payload)
                        )
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    result["execution_handoff"] = handoff
                    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
                    result_path.write_text(json.dumps(result), encoding="utf-8")
                    codex_path = root / "codex.txt"
                    events = [
                        json.loads(line)
                        for line in codex_path.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    completed = next(
                        event
                        for event in events
                        if event.get("type") == "item.completed"
                        and event.get("item", {}).get("id") == "ri-0"
                    )
                    completed["item"]["result"]["content"][0]["text"] = json.dumps(result)
                    codex_path.write_text(
                        "\n".join(json.dumps(event) for event in events) + "\n",
                        encoding="utf-8",
                    )
                else:
                    trajectory_path = root / "trajectory.json"
                    trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
                    if case_name == "shell_after_post":
                        trajectory["steps"].append(
                            self._atif_exec_command_step(
                                99, "$ unexpected\n[exit 0]\n"
                            )
                        )
                    else:
                        step_index = 1 if case_name == "preflight_byte" else 4
                        result = trajectory["steps"][step_index]["observation"]["results"][0]
                        if case_name == "source_call_id":
                            result["source_call_id"] = "wrong-call"
                        else:
                            blocks = ast.literal_eval(result["content"])
                            if case_name == "preflight_byte":
                                blocks[1]["text"] += "x"
                            elif case_name == "final_byte":
                                envelope = json.loads(blocks[1]["text"])
                                envelope["output"] += "x"
                                blocks[1]["text"] = json.dumps(
                                    envelope, separators=(",", ":")
                                )
                            else:
                                envelope = json.loads(blocks[1]["text"])
                                envelope["exit_code"] = 1
                                blocks[1]["text"] = json.dumps(
                                    envelope, separators=(",", ":")
                                )
                            result["content"] = repr(blocks)
                    trajectory_path.write_text(json.dumps(trajectory), encoding="utf-8")
                with self.assertRaises(VALIDATOR.EvidenceError):
                    VALIDATOR.validate_attempt(evidence_path)

    def test_validator_rejects_codex_event_contract_drift(self) -> None:
        for case_name in (
            "oversized_preflight",
            "duplicate_ri_call",
            "completed_artifact_drift",
            "too_many_preflight_shell_calls",
        ):
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence = self._write_fixture(root)
                codex_path = root / "codex.txt"
                events = [
                    json.loads(line)
                    for line in codex_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                if case_name == "oversized_preflight":
                    oversized = "$ git status\n[exit 0]\n" + ("x" * 12_000)
                    for event in events:
                        item = event.get("item", {})
                        if (
                            item.get("type") == "mcp_tool_call"
                            and item.get("arguments", {}).get("resume_run_id")
                            == "benchmark-fuse"
                        ):
                            item["arguments"]["mechanical_evidence"] = oversized
                elif case_name == "duplicate_ri_call":
                    duplicate = [
                        json.loads(json.dumps(event))
                        for event in events
                        if event.get("item", {}).get("id") == "ri-0"
                    ]
                    for event in duplicate:
                        event["item"]["id"] = "ri-duplicate"
                    events.extend(duplicate)
                elif case_name == "completed_artifact_drift":
                    for event in events:
                        item = event.get("item", {})
                        if (
                            item.get("type") == "mcp_tool_call"
                            and item.get("arguments", {}).get("resume_run_id")
                            == "benchmark-final"
                        ):
                            item["arguments"]["artifact"] = "Different final artifact."
                else:
                    extra_events = []
                    for extra_index in range(2):
                        item_id = f"extra-preflight-{extra_index}"
                        extra_events.extend(
                            [
                                {
                                    "type": "item.started",
                                    "item": {
                                        "id": item_id,
                                        "type": "command_execution",
                                        "status": "in_progress",
                                        "command": "git status --short",
                                    },
                                },
                                {
                                    "type": "item.completed",
                                    "item": {
                                        "id": item_id,
                                        "type": "command_execution",
                                        "status": "completed",
                                        "command": "git status --short",
                                        "exit_code": 0,
                                    },
                                },
                            ]
                        )
                    events = [*extra_events, *events]
                codex_path.write_text(
                    "\n".join(json.dumps(event) for event in events) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaises(VALIDATOR.EvidenceError):
                    VALIDATOR.validate_attempt(evidence)

    def test_validator_rejects_reward_provenance_and_secret_failures(self) -> None:
        for kwargs in (
            {"bad_reward": True},
            {"bad_provenance": True},
            {"secret": True},
        ):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as directory:
                evidence = self._write_fixture(Path(directory), **kwargs)
                with self.assertRaises(VALIDATOR.EvidenceError):
                    VALIDATOR.validate_attempt(evidence)

    def test_validator_rejects_nonprivate_ri_artifacts_and_directories(self) -> None:
        for target in ("gate", "responses_directory"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence = self._write_fixture(root)
                run = root / "ri" / "benchmark-fuse"
                retained_path = (
                    run / "gate-0.json"
                    if target == "gate"
                    else run / "responses"
                )
                retained_path.chmod(0o644 if retained_path.is_file() else 0o755)
                with self.assertRaisesRegex(VALIDATOR.EvidenceError, "not owner-only"):
                    VALIDATOR.validate_attempt(evidence)

    def test_validator_rejects_deepswe_total_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self._write_fixture(root)
            reward_path = root / "deep-reward.json"
            reward = json.loads(reward_path.read_text())
            reward["p2p_passed"] -= 1
            reward_path.write_text(json.dumps(reward))
            with self.assertRaises(VALIDATOR.EvidenceError):
                VALIDATOR.validate_attempt(evidence)

    def test_validator_rejects_boolean_reward_and_artifact_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self._write_fixture(root)
            result_path = root / "result.json"
            result = json.loads(result_path.read_text())
            result["verifier_result"]["rewards"]["reward"] = True
            result_path.write_text(json.dumps(result))
            with self.assertRaises(VALIDATOR.EvidenceError):
                VALIDATOR.validate_attempt(evidence)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self._write_fixture(root)
            contract_path = root / "run-contract.json"
            contract = json.loads(contract_path.read_text())
            contract["pins"]["artifact_hashes"]["plugin_tree_sha256"] = "f" * 64
            contract_path.write_text(json.dumps(contract))
            with self.assertRaises(VALIDATOR.EvidenceError):
                VALIDATOR.validate_attempt(evidence)

    def test_validator_rejects_near_match_attempt_retry_and_concurrency_flags(self) -> None:
        for flag, invalid_value in (
            ("--n-attempts", "10"),
            ("--max-retries", "00"),
            ("--n-concurrent", "10"),
        ):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence = self._write_fixture(root)
                contract_path = root / "run-contract.json"
                contract = json.loads(contract_path.read_text(encoding="utf-8"))
                value_index = contract["command"].index(flag) + 1
                contract["command"][value_index] = invalid_value
                contract_path.write_text(json.dumps(contract), encoding="utf-8")
                with self.assertRaises(VALIDATOR.EvidenceError):
                    VALIDATOR.validate_attempt(evidence)

    def test_validator_rejects_duplicate_fusion_and_failed_lifecycle_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_path = self._write_fixture(root)
            evidence = json.loads(evidence_path.read_text())
            duplicate = dict(evidence["ri_runs"][0])
            evidence["ri_runs"][-1] = duplicate
            evidence_path.write_text(json.dumps(evidence))
            with self.assertRaises(VALIDATOR.EvidenceError):
                VALIDATOR.validate_attempt(evidence_path)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self._write_fixture(root)
            gate_path = root / "ri" / "benchmark-final" / "gate-0.json"
            gate = json.loads(gate_path.read_text())
            gate["passed"] = False
            gate_path.write_text(json.dumps(gate))
            with self.assertRaises(VALIDATOR.EvidenceError):
                VALIDATOR.validate_attempt(evidence)

    def test_validator_rejects_path_replay_raw_receipt_and_reviewer_drift(self) -> None:
        cases = (
            "cross_run_ledger",
            "escaping_gate_artifact",
            "raw_response_tamper",
            "reviewer_receipt_drift",
            "ledger_request_id_drift",
            "ledger_raw_status_drift",
            "ledger_usage_drift",
        )
        for case_name in cases:
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence_path = self._write_fixture(root)
                if case_name == "cross_run_ledger":
                    evidence = json.loads(evidence_path.read_text())
                    plan_reference = next(
                        reference
                        for reference in evidence["ri_runs"]
                        if reference["run_id"] == "benchmark-plan"
                    )
                    plan_reference["ledger"] = (
                        "ri/benchmark-pre-execution/ledger.json"
                    )
                    evidence_path.write_text(json.dumps(evidence))
                elif case_name == "escaping_gate_artifact":
                    manifest_path = root / "ri" / "benchmark-plan" / "manifest.json"
                    manifest = json.loads(manifest_path.read_text())
                    manifest["stages"]["gate-0"]["artifact"] = (
                        "../benchmark-pre-execution/gate-0.json"
                    )
                    manifest_path.write_text(json.dumps(manifest))
                elif case_name == "raw_response_tamper":
                    ledger = json.loads(
                        (root / "ri" / "benchmark-plan" / "ledger.json").read_text()
                    )
                    response_path = (
                        root
                        / "ri"
                        / "benchmark-plan"
                        / ledger["entries"][0]["response_artifact"]
                    )
                    raw_response = json.loads(response_path.read_text())
                    raw_response["response"]["text"] += " tampered"
                    response_path.write_text(json.dumps(raw_response))
                elif case_name == "reviewer_receipt_drift":
                    gate_path = root / "ri" / "benchmark-plan" / "gate-0.json"
                    gate = json.loads(gate_path.read_text())
                    gate["reviewers"][0]["response_evidence"] = gate["reviewers"][1][
                        "response_evidence"
                    ]
                    gate_path.write_text(json.dumps(gate))
                else:
                    ledger_path = root / "ri" / "benchmark-plan" / "ledger.json"
                    ledger = json.loads(ledger_path.read_text())
                    field = case_name.removeprefix("ledger_").removesuffix("_drift")
                    if field == "request_id":
                        ledger["entries"][0][field] += "-tampered"
                    elif field == "raw_status":
                        ledger["entries"][0][field] = "failed"
                    else:
                        ledger["entries"][0][field]["input_tokens"] += 1
                    ledger_path.write_text(json.dumps(ledger))
                with self.assertRaises(VALIDATOR.EvidenceError):
                    VALIDATOR.validate_attempt(evidence_path)

    def test_validator_rejects_action_phase_and_event_state_drift(self) -> None:
        cases = (
            "command_during_fuse",
            "file_change_during_fuse",
            "file_change_crosses_final",
            "zero_exit_failed_status",
            "failed_file_change",
            "narrative_with_action_fields",
            "malformed_item_payload",
            "control_event_command",
            "control_event_file_change",
            "control_event_mcp_call",
            "turn_failed",
        )
        for case_name in cases:
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence = self._write_fixture(root)
                codex_path = root / "codex.txt"
                events = [json.loads(line) for line in codex_path.read_text().splitlines()]
                fuse_start = next(
                    index
                    for index, event in enumerate(events)
                    if event.get("type") == "item.started"
                    and event.get("item", {}).get("id") == "ri-0"
                )
                final_start = next(
                    index
                    for index, event in enumerate(events)
                    if event.get("type") == "item.started"
                    and event.get("item", {}).get("id") == "final-acceptance-command"
                )
                if case_name == "command_during_fuse":
                    command = self._canonical_shell_script("git status --short")
                    events[fuse_start + 1 : fuse_start + 1] = [
                        {
                            "type": "item.started",
                            "item": {
                                "id": "overlapping-command",
                                "type": "command_execution",
                                "status": "in_progress",
                                "command": command,
                            },
                        },
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "overlapping-command",
                                "type": "command_execution",
                                "status": "completed",
                                "command": command,
                                "aggregated_output": "$ git status --short\n[exit 0]\n",
                                "exit_code": 0,
                            },
                        },
                    ]
                elif case_name == "file_change_during_fuse":
                    change = [{"path": "/workspace/file", "kind": "update"}]
                    events[fuse_start + 1 : fuse_start + 1] = [
                        {
                            "type": "item.started",
                            "item": {
                                "id": "overlapping-file",
                                "type": "file_change",
                                "status": "in_progress",
                                "changes": change,
                            },
                        },
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "overlapping-file",
                                "type": "file_change",
                                "status": "completed",
                                "changes": change,
                            },
                        },
                    ]
                elif case_name in {"file_change_crosses_final", "failed_file_change"}:
                    change = [{"path": "/workspace/file", "kind": "update"}]
                    events.insert(
                        final_start,
                        {
                            "type": "item.started",
                            "item": {
                                "id": "execution-file",
                                "type": "file_change",
                                "status": "in_progress",
                                "changes": change,
                            },
                        },
                    )
                    completion_status = (
                        "failed" if case_name == "failed_file_change" else "completed"
                    )
                    final_completion = next(
                        index
                        for index, event in enumerate(events)
                        if event.get("type") == "item.completed"
                        and event.get("item", {}).get("id") == "final-acceptance-command"
                    )
                    events.insert(
                        final_completion + 1,
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "execution-file",
                                "type": "file_change",
                                "status": completion_status,
                                "changes": change,
                            },
                        },
                    )
                elif case_name == "zero_exit_failed_status":
                    completed = next(
                        event
                        for event in events
                        if event.get("type") == "item.completed"
                        and event.get("item", {}).get("id") == "final-acceptance-command"
                    )
                    completed["item"]["status"] = "failed"
                elif case_name == "narrative_with_action_fields":
                    events.insert(
                        0,
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "bad-narrative",
                                "type": "agent_message",
                                "text": "Narrative.",
                                "command": "touch /tmp/PWNED",
                                "exit_code": 0,
                            },
                        },
                    )
                elif case_name == "malformed_item_payload":
                    events.insert(0, {"type": "item.completed", "item": "not-an-object"})
                elif case_name.startswith("control_event_"):
                    item_type = {
                        "control_event_command": "command_execution",
                        "control_event_file_change": "file_change",
                        "control_event_mcp_call": "mcp_tool_call",
                    }[case_name]
                    started_item = {
                        "id": f"disguised-{item_type}",
                        "type": item_type,
                        "status": "in_progress",
                    }
                    if item_type == "command_execution":
                        started_item["command"] = "touch /tmp/PWNED"
                    elif item_type == "file_change":
                        started_item["changes"] = [
                            {"path": "/workspace/PWNED", "kind": "add"}
                        ]
                    else:
                        started_item.update(
                            {
                                "server": "external-oracle",
                                "tool": "lookup_solution",
                                "arguments": {},
                            }
                        )
                    completed_item = json.loads(json.dumps(started_item))
                    completed_item["status"] = "completed"
                    if item_type == "command_execution":
                        completed_item.update(
                            {"aggregated_output": "", "exit_code": 0}
                        )
                    events[fuse_start + 1 : fuse_start + 1] = [
                        {"type": "turn.started", "item": started_item},
                        {"type": "turn.completed", "item": completed_item},
                    ]
                else:
                    events.append({"type": "turn.failed", "error": "late failure"})
                codex_path.write_text(
                    "\n".join(json.dumps(event) for event in events) + "\n"
                )
                with self.assertRaises(VALIDATOR.EvidenceError):
                    VALIDATOR.validate_attempt(evidence)

    def test_validator_rejects_unbound_fusion_semantic_artifacts(self) -> None:
        cases = (
            "missing_panel",
            "escaping_panel",
            "duplicate_panel_seat",
            "panel_receipt_swap",
            "result_panel_drift",
            "judge_judgment_drift",
            "synthesis_hash_drift",
            "synthesis_receipt_swap",
            "result_config_hash_drift",
            "bogus_manifest_stage",
            "passed_gate_blocker",
            "gate_verdict_schema_drift",
        )

        def write_fusion_result(root: Path, fusion_result: dict[str, object]) -> None:
            (root / "ri" / "benchmark-fuse" / "result.json").write_text(
                json.dumps(fusion_result)
            )
            codex_path = root / "codex.txt"
            events = [json.loads(line) for line in codex_path.read_text().splitlines()]
            completed_fusion = next(
                event
                for event in events
                if event.get("type") == "item.completed"
                and event.get("item", {}).get("id") == "ri-0"
            )
            completed_fusion["item"]["result"]["content"][0]["text"] = json.dumps(
                fusion_result
            )
            codex_path.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n"
            )

        for case_name in cases:
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence = self._write_fixture(root)
                fusion_run = root / "ri" / "benchmark-fuse"
                if case_name == "missing_panel":
                    (fusion_run / "panel.json").unlink()
                elif case_name == "escaping_panel":
                    manifest_path = fusion_run / "manifest.json"
                    manifest = json.loads(manifest_path.read_text())
                    manifest["stages"]["panel"]["artifact"] = "../benchmark-plan/gate-0.json"
                    manifest_path.write_text(json.dumps(manifest))
                elif case_name in {"duplicate_panel_seat", "panel_receipt_swap"}:
                    panel_path = fusion_run / "panel.json"
                    panel = json.loads(panel_path.read_text())
                    if case_name == "duplicate_panel_seat":
                        panel["results"][1]["seat_name"] = panel["results"][0]["seat_name"]
                    else:
                        panel["results"][0]["response_evidence"] = panel["results"][1][
                            "response_evidence"
                        ]
                    panel_path.write_text(json.dumps(panel))
                elif case_name == "result_panel_drift":
                    result = json.loads((fusion_run / "result.json").read_text())
                    result["panel"] = result["panel"][:-1]
                    write_fusion_result(root, result)
                elif case_name == "judge_judgment_drift":
                    judge_path = fusion_run / "judge.json"
                    judge = json.loads(judge_path.read_text())
                    judge["judgment"]["final_guidance"] = "Tampered guidance."
                    judge_path.write_text(json.dumps(judge))
                    result = json.loads((fusion_run / "result.json").read_text())
                    result["judge"] = judge["judgment"]
                    write_fusion_result(root, result)
                elif case_name in {"synthesis_hash_drift", "synthesis_receipt_swap"}:
                    synthesis_path = fusion_run / "synthesis.json"
                    synthesis = json.loads(synthesis_path.read_text())
                    if case_name == "synthesis_hash_drift":
                        synthesis["sha256"] = "0" * 64
                    else:
                        judge = json.loads((fusion_run / "judge.json").read_text())
                        synthesis["response_evidence"] = judge["response_evidence"]
                    synthesis_path.write_text(json.dumps(synthesis))
                elif case_name == "result_config_hash_drift":
                    result = json.loads((fusion_run / "result.json").read_text())
                    result["config_hash"] = "b" * 64
                    write_fusion_result(root, result)
                elif case_name in {"passed_gate_blocker", "gate_verdict_schema_drift"}:
                    gate_path = fusion_run / "gate-0.json"
                    gate = json.loads(gate_path.read_text())
                    if case_name == "passed_gate_blocker":
                        gate["mechanical_failures"] = ["pytest exit status 1"]
                        gate["mechanical_blocked"] = True
                        gate["deterministic_blockers"] = [
                            "Mechanical evidence reports failure: pytest exit status 1"
                        ]
                    else:
                        gate["reviewers"][0]["verdict"].pop("summary")
                    gate_path.write_text(json.dumps(gate))
                    result = json.loads((fusion_run / "result.json").read_text())
                    result["gate"] = gate
                    write_fusion_result(root, result)
                else:
                    manifest_path = fusion_run / "manifest.json"
                    manifest = json.loads(manifest_path.read_text())
                    manifest["stages"]["bogus"] = {
                        "status": "completed",
                        "artifact": "panel.json",
                    }
                    manifest_path.write_text(json.dumps(manifest))
                with self.assertRaises(VALIDATOR.EvidenceError):
                    VALIDATOR.validate_attempt(evidence)

    def test_validator_rejects_unrelated_aggregated_output_and_post_final_mutation(self) -> None:
        for case_name in ("preflight_suffix", "final_suffix", "post_final_mutation"):
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence = self._write_fixture(root)
                codex_path = root / "codex.txt"
                events = [json.loads(line) for line in codex_path.read_text().splitlines()]
                if case_name.endswith("suffix"):
                    item_id = (
                        "preflight-command"
                        if case_name == "preflight_suffix"
                        else "final-acceptance-command"
                    )
                    completed = next(
                        event
                        for event in events
                        if event.get("type") == "item.completed"
                        and event.get("item", {}).get("id") == item_id
                    )
                    completed["item"]["aggregated_output"] = "unrelated output"
                else:
                    post_start_index = next(
                        index
                        for index, event in enumerate(events)
                        if event.get("type") == "item.started"
                        and event.get("item", {}).get("id") == "ri-3"
                    )
                    events.insert(
                        post_start_index,
                        {
                            "type": "item.started",
                            "item": {
                                "id": "stale-final-evidence-change",
                                "type": "file_change",
                                "status": "in_progress",
                            },
                        },
                    )
                codex_path.write_text(
                    "\n".join(json.dumps(event) for event in events) + "\n"
                )
                with self.assertRaises(VALIDATOR.EvidenceError):
                    VALIDATOR.validate_attempt(evidence)

    def test_validator_rejects_external_tools_and_fabricated_command_markers(self) -> None:
        for position in ("before_fuse", "after_summarize"):
            with self.subTest(position=position), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence = self._write_fixture(root)
                codex_path = root / "codex.txt"
                events = [json.loads(line) for line in codex_path.read_text().splitlines()]
                external_events = [
                    {
                        "type": "item.started",
                        "item": {
                            "id": "external-oracle",
                            "type": "mcp_tool_call",
                            "server": "external-oracle",
                            "tool": "lookup_solution",
                            "status": "in_progress",
                            "arguments": {},
                        },
                    },
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "external-oracle",
                            "type": "mcp_tool_call",
                            "server": "external-oracle",
                            "tool": "lookup_solution",
                            "status": "completed",
                            "arguments": {},
                        },
                    },
                ]
                if position == "before_fuse":
                    insertion_index = next(
                        index
                        for index, event in enumerate(events)
                        if event.get("type") == "item.started"
                        and event.get("item", {}).get("id") == "ri-0"
                    )
                    events[insertion_index:insertion_index] = external_events
                else:
                    events[-1:-1] = external_events
                codex_path.write_text(
                    "\n".join(json.dumps(event) for event in events) + "\n"
                )
                with self.assertRaisesRegex(VALIDATOR.EvidenceError, "external MCP"):
                    VALIDATOR.validate_attempt(evidence)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self._write_fixture(root)
            codex_path = root / "codex.txt"
            events = [json.loads(line) for line in codex_path.read_text().splitlines()]
            events.insert(
                2,
                {
                    "type": "item.completed",
                    "item": {
                        "id": "completed-only-oracle",
                        "type": "web_search",
                        "status": "completed",
                        "query": "benchmark answer",
                    },
                },
            )
            codex_path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            with self.assertRaisesRegex(VALIDATOR.EvidenceError, "unexpected tool action"):
                VALIDATOR.validate_attempt(evidence)

        for case_name, item_id, actual_command, step_index in (
            ("preflight", "preflight-command", "touch /tmp/PWNED", 1),
            ("final", "final-acceptance-command", "printf fabricated", 4),
        ):
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence = self._write_fixture(root)
                trajectory_path = root / "trajectory.json"
                trajectory = json.loads(trajectory_path.read_text())
                source = trajectory["steps"][step_index]["tool_calls"][0]["arguments"]["input"]
                marker_command = (
                    "git status --short" if case_name == "preflight" else "project-test"
                )
                trajectory["steps"][step_index]["tool_calls"][0]["arguments"]["input"] = (
                    source.replace(
                        json.dumps(self._canonical_shell_script(marker_command)),
                        json.dumps(self._canonical_shell_script(actual_command)),
                    )
                )
                trajectory_path.write_text(json.dumps(trajectory))
                codex_path = root / "codex.txt"
                events = [json.loads(line) for line in codex_path.read_text().splitlines()]
                for event in events:
                    if event.get("item", {}).get("id") == item_id:
                        event["item"]["command"] = self._canonical_shell_script(actual_command)
                codex_path.write_text(
                    "\n".join(json.dumps(event) for event in events) + "\n"
                )
                with self.assertRaisesRegex(VALIDATOR.EvidenceError, "transcript marker"):
                    VALIDATOR.validate_attempt(evidence)

    def test_validator_rejects_missing_or_drifted_intermediate_atif_output(self) -> None:
        for case_name in ("missing_observation", "output_drift"):
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence = self._write_fixture(root)
                intermediate_output = "$ failing-check\nknown transient failure\n[exit 1]\n"
                intermediate_step = self._atif_exec_command_step(
                    40,
                    intermediate_output,
                    enveloped=True,
                    exit_code=1,
                )
                if case_name == "missing_observation":
                    intermediate_step.pop("observation")
                else:
                    retained = intermediate_step["observation"]["results"][0]
                    blocks = ast.literal_eval(retained["content"])
                    envelope = json.loads(blocks[1]["text"])
                    envelope["output"] += "drift"
                    blocks[1]["text"] = json.dumps(envelope, separators=(",", ":"))
                    retained["content"] = repr(blocks)
                trajectory_path = root / "trajectory.json"
                trajectory = json.loads(trajectory_path.read_text())
                trajectory["steps"].insert(4, intermediate_step)
                trajectory_path.write_text(json.dumps(trajectory))
                codex_path = root / "codex.txt"
                events = [json.loads(line) for line in codex_path.read_text().splitlines()]
                final_start_index = next(
                    index
                    for index, event in enumerate(events)
                    if event.get("type") == "item.started"
                    and event.get("item", {}).get("id") == "final-acceptance-command"
                )
                events[final_start_index:final_start_index] = [
                    {
                        "type": "item.started",
                        "item": {
                            "id": "intermediate-command",
                            "type": "command_execution",
                            "status": "in_progress",
                            "command": self._canonical_shell_script("failing-check"),
                        },
                    },
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "intermediate-command",
                            "type": "command_execution",
                            "status": "failed",
                            "command": self._canonical_shell_script("failing-check"),
                            "aggregated_output": intermediate_output,
                            "exit_code": 1,
                        },
                    },
                ]
                codex_path.write_text(
                    "\n".join(json.dumps(event) for event in events) + "\n"
                )
                with self.assertRaises(VALIDATOR.EvidenceError):
                    VALIDATOR.validate_attempt(evidence)

    def test_validator_rejects_nonexecuting_helper_and_unrecorded_shell_statement(self) -> None:
        cases = (
            ("preflight_nonexecuting", 1, "preflight-command", "git status --short"),
            ("final_nonexecuting", 4, "final-acceptance-command", "project-test"),
            ("preflight_unrecorded", 1, "preflight-command", "git status --short"),
            ("preflight_trailing_unrecorded", 1, "preflight-command", "git status --short"),
            ("preflight_printf_substitution", 1, "preflight-command", "git status --short"),
            ("preflight_printf_value_trailing", 1, "preflight-command", "git status --short"),
            ("preflight_printf_nomatch_trailing", 1, "preflight-command", "git status --short"),
            ("preflight_printf_failure_trailing", 1, "preflight-command", "git status --short"),
        )
        for case_name, step_index, item_id, inner_command in cases:
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence = self._write_fixture(root)
                good_script = self._canonical_shell_script(inner_command)
                if case_name.endswith("nonexecuting"):
                    bad_script = good_script.replace(
                        'bash -o pipefail -c "$command_text"',
                        "printf 'fabricated output\\n'",
                    )
                elif case_name == "preflight_trailing_unrecorded":
                    bad_script = good_script + " ; touch /tmp/PWNED"
                elif case_name == "preflight_printf_substitution":
                    bad_script = good_script.replace(
                        "}\nrun_record",
                        "}\nprintf '%s\\n' \"$(touch /tmp/PWNED)\"\nrun_record",
                    )
                elif case_name == "preflight_printf_value_trailing":
                    bad_script = good_script.replace(
                        "}\nrun_record",
                        "}\nprintf '%s\\n' safe ; touch /tmp/PWNED\nrun_record",
                    )
                elif case_name == "preflight_printf_nomatch_trailing":
                    bad_script = good_script.replace(
                        "}\nrun_record",
                        "}\nprintf 'no matches (expected)\\n' ; touch /tmp/PWNED\nrun_record",
                    )
                elif case_name == "preflight_printf_failure_trailing":
                    bad_script = good_script.replace(
                        "}\nrun_record",
                        "}\nprintf 'discovery failed with status %d\\n' \"$status\" ; touch /tmp/PWNED\nrun_record",
                    )
                else:
                    bad_script = good_script.replace(
                        "}\nrun_record",
                        "}\ntouch /tmp/PWNED\nrun_record",
                    )
                trajectory_path = root / "trajectory.json"
                trajectory = json.loads(trajectory_path.read_text())
                source = trajectory["steps"][step_index]["tool_calls"][0]["arguments"]["input"]
                trajectory["steps"][step_index]["tool_calls"][0]["arguments"]["input"] = (
                    source.replace(json.dumps(good_script), json.dumps(bad_script))
                )
                trajectory_path.write_text(json.dumps(trajectory))
                codex_path = root / "codex.txt"
                events = [json.loads(line) for line in codex_path.read_text().splitlines()]
                for event in events:
                    if event.get("item", {}).get("id") == item_id:
                        event["item"]["command"] = bad_script
                codex_path.write_text(
                    "\n".join(json.dumps(event) for event in events) + "\n"
                )
                with self.assertRaises(VALIDATOR.EvidenceError):
                    VALIDATOR.validate_attempt(evidence)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self._write_fixture(root)
            good_script = self._canonical_shell_script("git status --short")
            malicious_script = (
                "printf '$ git status --short\\n[exit 0]\\n'; touch /tmp/PWNED"
            )
            trajectory_path = root / "trajectory.json"
            trajectory = json.loads(trajectory_path.read_text())
            source = trajectory["steps"][1]["tool_calls"][0]["arguments"]["input"]
            trajectory["steps"][1]["tool_calls"][0]["arguments"]["input"] = source.replace(
                "cmd:" + json.dumps(good_script),
                "cmd:" + json.dumps(good_script) + ",cmd:" + json.dumps(malicious_script),
            )
            trajectory_path.write_text(json.dumps(trajectory))
            codex_path = root / "codex.txt"
            events = [json.loads(line) for line in codex_path.read_text().splitlines()]
            for event in events:
                if event.get("item", {}).get("id") == "preflight-command":
                    event["item"]["command"] = malicious_script
            codex_path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            with self.assertRaises(VALIDATOR.EvidenceError):
                VALIDATOR.validate_attempt(evidence)

    def test_validator_rejects_glued_exit_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self._write_fixture(root)
            bad_output = "$ project-test\n27 tests passed[exit 0]\n"
            trajectory_path = root / "trajectory.json"
            trajectory = json.loads(trajectory_path.read_text())
            trajectory["steps"][4] = self._atif_exec_command_step(
                4,
                bad_output,
                enveloped=True,
            )
            trajectory_path.write_text(json.dumps(trajectory))
            codex_path = root / "codex.txt"
            events = [json.loads(line) for line in codex_path.read_text().splitlines()]
            for event in events:
                item = event.get("item", {})
                if item.get("id") == "final-acceptance-command":
                    item["command"] = self._canonical_shell_script("project-test")
                    if event.get("type") == "item.completed":
                        item["aggregated_output"] = bad_output
                arguments = item.get("arguments", {})
                if arguments.get("resume_run_id") in {
                    "benchmark-post-execution",
                    "benchmark-final",
                    "benchmark-summarize",
                }:
                    arguments["mechanical_evidence"] = bad_output
            codex_path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            with self.assertRaises(VALIDATOR.EvidenceError):
                VALIDATOR.validate_attempt(evidence)

    def test_validator_rejects_unordered_transcript_mount_and_result_identity(self) -> None:
        for case_name in ("unordered_transcript", "mount_type", "result_task"):
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evidence = self._write_fixture(root)
                if case_name == "unordered_transcript":
                    bad_output = "[exit 0]\n$ git status --short\n"
                    trajectory_path = root / "trajectory.json"
                    trajectory = json.loads(trajectory_path.read_text())
                    trajectory["steps"][1] = self._atif_exec_command_step(1, bad_output)
                    trajectory_path.write_text(json.dumps(trajectory))
                    codex_path = root / "codex.txt"
                    events = [json.loads(line) for line in codex_path.read_text().splitlines()]
                    for event in events:
                        item = event.get("item", {})
                        if item.get("id") == "preflight-command":
                            item["command"] = self._canonical_shell_script("git status --short")
                            if event.get("type") == "item.completed":
                                item["aggregated_output"] = bad_output
                        arguments = item.get("arguments", {})
                        if arguments.get("resume_run_id") in {
                            "benchmark-fuse",
                            "benchmark-plan",
                            "benchmark-pre-execution",
                        }:
                            arguments["mechanical_evidence"] = bad_output
                    codex_path.write_text(
                        "\n".join(json.dumps(event) for event in events) + "\n"
                    )
                elif case_name == "mount_type":
                    contract_path = root / "run-contract.json"
                    contract = json.loads(contract_path.read_text())
                    mounts_index = contract["command"].index("--mounts-json") + 1
                    mounts = json.loads(contract["command"][mounts_index])
                    next(
                        mount
                        for mount in mounts
                        if mount["target"] == "/opt/relentless-inception"
                    )["type"] = "volume"
                    contract["command"][mounts_index] = json.dumps(mounts)
                    contract_path.write_text(json.dumps(contract))
                else:
                    result_path = root / "result.json"
                    result = json.loads(result_path.read_text())
                    result["task_name"] = "datacurve/different-task"
                    result_path.write_text(json.dumps(result))
                with self.assertRaises(VALIDATOR.EvidenceError):
                    VALIDATOR.validate_attempt(evidence)

    def test_validate_final_rejects_cloned_attempt_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for task in (*PINS["harbor"]["tasks"], *PINS["pier"]["tasks"]):
                for attempt in (1, 2):
                    attempt_root = root / task / f"attempt-{attempt}"
                    attempt_root.mkdir(parents=True)
                    self._write_fixture(
                        attempt_root,
                        task=task,
                        attempt=attempt,
                    )
            VALIDATOR.validate_final(root)

            first = root / "fix-git" / "attempt-1"
            second = root / "fix-git" / "attempt-2"
            shutil.rmtree(second)
            shutil.copytree(first, second)
            evidence_path = second / "evidence.json"
            evidence = json.loads(evidence_path.read_text())
            evidence["attempt"] = 2
            evidence_path.write_text(json.dumps(evidence))
            contract_path = second / "run-contract.json"
            contract = json.loads(contract_path.read_text())
            contract["attempt"] = 2
            contract_path.write_text(json.dumps(contract))
            result_path = second / "result.json"
            result = json.loads(result_path.read_text())
            result["trial_name"] = "fix-git__cloned-attempt-2"
            result_path.write_text(json.dumps(result))
            with self.assertRaisesRegex(VALIDATOR.EvidenceError, "replayed"):
                VALIDATOR.validate_final(root)


if __name__ == "__main__":
    unittest.main()
