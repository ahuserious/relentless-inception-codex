from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "bench"
PINS = json.loads((BENCH / "pins.json").read_text(encoding="utf-8"))

sys.path.insert(0, str(BENCH))
try:
    import run_bench as RUNNER
    import validate_evidence as VALIDATOR
finally:
    sys.path.pop(0)


def flag_value(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


class DeepSWEBenchmarkTests(unittest.TestCase):
    def test_commands_and_validator_use_harness_specific_mounts(self) -> None:
        attempt_directory = Path("/tmp/ri-bench-attempt")
        secret_file = Path("/private/tmp/ri-bench-secret-unit/xai-api-key")
        cases = (
            ("fix-git", None, "--mounts", 3),
            (
                "anko-default-function-arguments",
                Path("/tmp/deep-swe"),
                "--mounts-json",
                6,
            ),
        )

        for task, deep_swe_root, mount_flag, expected_count in cases:
            with self.subTest(task=task):
                command = RUNNER.build_command(
                    PINS,
                    task,
                    attempt_directory,
                    deep_swe_root,
                    secret_file,
                )
                mounts = json.loads(flag_value(command, mount_flag))
                self.assertEqual(len(mounts), expected_count)
                mounts_by_target = {mount["target"]: mount for mount in mounts}
                self.assertEqual(len(mounts_by_target), expected_count)
                expected_targets = {
                    "/opt/relentless-inception",
                    "/opt/relentless-inception-bench",
                    "/run/secrets/relentless-inception-xai",
                }
                if task in PINS["pier"]["tasks"]:
                    expected_targets.update(
                        {"/logs/verifier", "/logs/agent", "/logs/artifacts"}
                    )
                self.assertEqual(set(mounts_by_target), expected_targets)

                harness = RUNNER.task_harness(PINS, task)
                task_pins = PINS[harness]["tasks"][task]
                contract = RUNNER.make_contract(
                    PINS,
                    task,
                    1,
                    command,
                    task_pins["image_digest"],
                    PINS["artifacts"],
                )
                VALIDATOR.validate_contract(
                    contract,
                    {"harness": harness, "task": task, "attempt": 1},
                )

                if harness == "pier":
                    expected_log_sources = {
                        "/logs/verifier": "${HOST_VERIFIER_LOGS_PATH}",
                        "/logs/agent": "${HOST_AGENT_LOGS_PATH}",
                        "/logs/artifacts": "${HOST_ARTIFACTS_PATH}",
                    }
                    for target, source in expected_log_sources.items():
                        self.assertEqual(mounts_by_target[target]["source"], source)
                        self.assertNotIn("read_only", mounts_by_target[target])

    def test_deep_swe_reward_accepts_only_absent_or_literal_false_apply_failure(self) -> None:
        task = "anko-default-function-arguments"
        successful_reward = {
            "reward": 1,
            **PINS["pier"]["tasks"][task]["expected"],
        }
        VALIDATOR.validate_deep_swe_reward(task, successful_reward)
        VALIDATOR.validate_deep_swe_reward(
            task,
            {**successful_reward, "apply_failed": False},
        )

        for invalid_value in (None, 0, 1, True, "false"):
            with self.subTest(invalid_value=invalid_value):
                with self.assertRaises(VALIDATOR.EvidenceError):
                    VALIDATOR.validate_deep_swe_reward(
                        task,
                        {**successful_reward, "apply_failed": invalid_value},
                    )

    def test_deep_swe_checkout_rejects_every_dirty_file_class(self) -> None:
        with DeepSWECheckoutFixture() as (deep_swe_root, pins):
            RUNNER.verify_deep_swe_checkout(pins, "sample-task", deep_swe_root)

        mutations = {
            "tracked": lambda root: (root / "tasks" / "sample-task" / "task.toml").write_text(
                "changed\n", encoding="utf-8"
            ),
            "untracked": lambda root: (root / "untracked.txt").write_text(
                "untracked\n", encoding="utf-8"
            ),
            "ignored": lambda root: (root / "ignored.txt").write_text(
                "ignored\n", encoding="utf-8"
            ),
        }
        for file_class, mutate in mutations.items():
            with self.subTest(file_class=file_class):
                with DeepSWECheckoutFixture() as (deep_swe_root, pins):
                    mutate(deep_swe_root)
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "tracked, untracked, or ignored changes",
                    ):
                        RUNNER.verify_deep_swe_checkout(
                            pins,
                            "sample-task",
                            deep_swe_root,
                        )

    def test_pier_job_uses_custom_adapter_without_factory_bypass(self) -> None:
        job_text = (BENCH / "pier" / "job.yaml").read_text(encoding="utf-8")
        self.assertIn(
            "import_path: bench.pier.codex_agent:BenchmarkCodex",
            job_text,
        )
        self.assertNotIn("name: codex", job_text)

    @unittest.skipUnless(importlib.util.find_spec("pier"), "Pier is not installed")
    def test_pier_adapter_has_exact_network_allowlist(self) -> None:
        import yaml

        from bench.pier.codex_agent import BenchmarkCodex
        from pier.models.job.config import JobConfig

        config = JobConfig.model_validate(
            yaml.safe_load((BENCH / "pier" / "job.yaml").read_text(encoding="utf-8"))
        )
        agent_config = config.agents[0]
        self.assertIsNone(agent_config.name)
        self.assertEqual(
            agent_config.import_path,
            "bench.pier.codex_agent:BenchmarkCodex",
        )

        allowlist = BenchmarkCodex.network_allowlist(None)
        self.assertEqual(
            set(allowlist.domains),
            {
                "api.openai.com",
                "api.x.ai",
                "auth.openai.com",
                "chatgpt.com",
            },
        )
        self.assertEqual(len(allowlist.domains), 4)


class DeepSWECheckoutFixture:
    def __enter__(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        task_directory = root / "tasks" / "sample-task"
        task_directory.mkdir(parents=True)
        (task_directory / "task.toml").write_text(
            'base_commit_hash = "1111111111111111111111111111111111111111"\n'
            'docker_image = "example.invalid/deep-swe:test"\n',
            encoding="utf-8",
        )
        (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "bench@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Benchmark Test"],
            cwd=root,
            check=True,
        )
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "fixture"],
            cwd=root,
            check=True,
        )
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        pins = {
            "pier": {
                "dataset": {"source_commit": commit},
                "tasks": {
                    "sample-task": {
                        "base_commit": "1" * 40,
                        "image": "example.invalid/deep-swe:test",
                    }
                },
            }
        }
        return root, pins

    def __exit__(self, exc_type, exc_value, traceback):
        self.temporary_directory.cleanup()
        return False


if __name__ == "__main__":
    unittest.main()
