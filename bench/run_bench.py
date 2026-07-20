#!/usr/bin/env python3
"""Run one immutable benchmark attempt or a final two-attempt campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Callable


BENCH_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = BENCH_ROOT.parent
PINS_PATH = BENCH_ROOT / "pins.json"
SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


def load_pins() -> dict[str, Any]:
    return json.loads(PINS_PATH.read_text(encoding="utf-8"))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(root: Path) -> str:
    """Hash stable source content, relative paths, and executable modes."""
    digest = hashlib.sha256()
    paths: list[Path] = []
    for path in root.rglob("*"):
        relative_parts = path.relative_to(root).parts
        if "__pycache__" in relative_parts or path.name in {".DS_Store"}:
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            raise RuntimeError(f"Pinned source tree contains a symlink: {path}")
        if path.is_file():
            paths.append(path)
    for path in sorted(paths, key=lambda value: value.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        mode = path.stat().st_mode & 0o777
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{mode:o}".encode("ascii"))
        digest.update(b"\0")
        digest.update(file_sha256(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def verify_artifact_hashes(pins: dict[str, Any]) -> dict[str, str]:
    observed = {
        "plugin_tree_sha256": tree_sha256(REPOSITORY_ROOT / "plugins" / "relentless-inception"),
        "runner_sha256": file_sha256(BENCH_ROOT / "run_bench.py"),
        "validator_sha256": file_sha256(BENCH_ROOT / "validate_evidence.py"),
    }
    runtime_digest = hashlib.sha256()
    for directory_name in ("harbor", "pier", "support"):
        runtime_digest.update(directory_name.encode("utf-8"))
        runtime_digest.update(b"\0")
        runtime_digest.update(tree_sha256(BENCH_ROOT / directory_name).encode("ascii"))
        runtime_digest.update(b"\n")
    observed["benchmark_runtime_tree_sha256"] = runtime_digest.hexdigest()
    expected = pins.get("artifacts")
    if observed != expected:
        raise RuntimeError(
            "Pinned plugin or benchmark artifact hash drift: "
            + json.dumps({"expected": expected, "observed": observed}, sort_keys=True)
        )
    return observed


def task_harness(pins: dict[str, Any], task: str) -> str:
    if task in pins["harbor"]["tasks"]:
        return "harbor"
    if task in pins["pier"]["tasks"]:
        return "pier"
    raise ValueError(f"Unknown pinned task: {task}")


def build_command(
    pins: dict[str, Any],
    task: str,
    attempt_directory: Path,
    deep_swe_root: Path | None,
    secret_file: Path | None = None,
) -> list[str]:
    harness = task_harness(pins, task)
    plugin_source = REPOSITORY_ROOT / "plugins" / "relentless-inception"
    benchmark_support = BENCH_ROOT / "support"
    resolved_secret_file = secret_file or Path(
        "/private/tmp/relentless-inception-xai"
    )
    tool_mounts = [
        {
            "type": "bind",
            "source": str(plugin_source),
            "target": "/opt/relentless-inception",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": str(benchmark_support),
            "target": "/opt/relentless-inception-bench",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": str(resolved_secret_file),
            "target": "/run/secrets/relentless-inception-xai",
            "read_only": True,
        },
    ]
    harness_mounts = tool_mounts
    if harness == "pier":
        # Pier treats --mounts-json as a replacement for its default log
        # mounts. Preserve those dynamic host paths so deleting the task
        # container cannot discard the trajectory, verifier, or RI evidence.
        harness_mounts = [
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
            *tool_mounts,
        ]
    retained_mounts = json.dumps(harness_mounts, separators=(",", ":"))
    common = [
        "--jobs-dir",
        str(attempt_directory / "jobs"),
        "--job-name",
        "run",
        "--n-attempts",
        "1",
        "--n-concurrent",
        "1",
        "--max-retries",
        "0",
        "--yes",
    ]
    if harness == "harbor":
        commit = pins["harbor"]["commit"]
        return [
            "uvx",
            "--from",
            f"git+https://github.com/harbor-framework/harbor@{commit}",
            "harbor",
            "jobs",
            "start",
            "--config",
            str(BENCH_ROOT / "harbor" / f"{task}.yaml"),
            "--mounts",
            retained_mounts,
            *common,
        ]

    if deep_swe_root is None:
        raise ValueError("--deep-swe-root is required for Pier tasks")
    return [
        "uvx",
        "--from",
        f"datacurve-pier=={pins['pier']['version']}",
        "pier",
        "run",
        "--config",
        str(BENCH_ROOT / "pier" / "job.yaml"),
        "--path",
        str(deep_swe_root / "tasks" / task),
        "--mounts-json",
        retained_mounts,
        *common,
    ]


def inspect_image_digest(image: str) -> str:
    completed = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", image],
        check=True,
        capture_output=True,
        text=True,
    )
    match = SHA256_PATTERN.search(completed.stdout)
    if match is None:
        raise RuntimeError(f"No image digest reported for {image}")
    return match.group(0)


def verify_deep_swe_checkout(
    pins: dict[str, Any], task: str, deep_swe_root: Path
) -> None:
    expected_commit = pins["pier"]["dataset"]["source_commit"]
    actual_commit = subprocess.run(
        ["git", "-C", str(deep_swe_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if actual_commit != expected_commit:
        raise RuntimeError(
            f"DeepSWE checkout is {actual_commit}; expected {expected_commit}"
        )

    checkout_status = subprocess.run(
        [
            "git",
            "-C",
            str(deep_swe_root),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if checkout_status:
        status_preview = "\n".join(checkout_status.splitlines()[:20])
        raise RuntimeError(
            "DeepSWE checkout has tracked, untracked, or ignored changes:\n"
            + status_preview
        )

    task_toml = deep_swe_root / "tasks" / task / "task.toml"
    task_text = task_toml.read_text(encoding="utf-8")
    base_match = re.search(r'^base_commit_hash\s*=\s*"([0-9a-f]{40})"\s*$', task_text, re.MULTILINE)
    image_match = re.search(r'^docker_image\s*=\s*"([^"]+)"\s*$', task_text, re.MULTILINE)
    expected = pins["pier"]["tasks"][task]
    if base_match is None or base_match.group(1) != expected["base_commit"]:
        raise RuntimeError(f"Unexpected base commit in {task_toml}")
    if image_match is None or image_match.group(1) != expected["image"]:
        raise RuntimeError(f"Unexpected image in {task_toml}")


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def resolve_xai_api_key() -> str:
    """Use the plugin's validated non-shell loader; never persist the value."""
    environment_value = os.environ.get("XAI_API_KEY")
    if environment_value:
        return environment_value

    plugin_root = REPOSITORY_ROOT / "plugins" / "relentless-inception"
    sys.path.insert(0, str(plugin_root))
    try:
        from relentless_inception.config import load_config
        from relentless_inception.providers import ProviderRegistry

        secrets = ProviderRegistry._load_secret_files(load_config())
    finally:
        sys.path.pop(0)
    key = secrets.get("XAI_API_KEY")
    if not key:
        raise RuntimeError(
            "XAI_API_KEY is absent from the environment and validated plugin secret files"
        )
    return key


def build_child_environment() -> dict[str, str]:
    """Forward process basics and opt into the host's Codex auth file."""
    allowed_names = {
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "NO_COLOR",
        "PATH",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "USER",
        "XDG_CACHE_HOME",
    }
    child_environment = {
        name: value for name, value in os.environ.items() if name in allowed_names
    }
    # Keep this out of agents.env: Harbor treats every configured agent env
    # value as a redaction secret, and a value of "1" corrupts numeric JSON.
    child_environment["CODEX_FORCE_AUTH_JSON"] = "1"
    # Harbor imports the pinned custom agent in its host process. uvx console
    # scripts do not reliably put their current working directory on sys.path.
    child_environment["PYTHONPATH"] = str(REPOSITORY_ROOT)
    return child_environment


def write_ephemeral_secret(path: Path, value: str) -> None:
    """Create one owner-only credential file without a world-readable window."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
    except Exception:
        os.close(descriptor)
        if path.exists():
            path.unlink()
        raise
    try:
        with handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if path.exists():
            path.unlink()
        raise


def make_contract(
    pins: dict[str, Any],
    task: str,
    attempt: int,
    command: list[str],
    observed_image_digest: str,
    artifact_hashes: dict[str, str],
) -> dict[str, Any]:
    harness = task_harness(pins, task)
    harness_pins = pins[harness]
    task_pins = harness_pins["tasks"][task]
    return {
        "schema_version": 1,
        "harness": harness,
        "task": task,
        "attempt": attempt,
        "command": command,
        "pins": {
            "harness_version": harness_pins["version"],
            "harness_commit": harness_pins.get("commit"),
            "dataset_source_commit": harness_pins["dataset"]["source_commit"],
            "image": task_pins["image"],
            "image_digest": task_pins["image_digest"],
            "observed_image_digest": observed_image_digest,
            "base_commit": task_pins.get("base_commit"),
            "codex_version": pins["codex"]["version"],
            "model": pins["codex"]["model"],
            "reasoning_effort": pins["codex"]["reasoning_effort"],
            "agent_timeout_seconds": pins["codex"]["agent_timeout_seconds"],
            "mcp_startup_timeout_seconds": pins["codex"]["mcp_startup_timeout_seconds"],
            "mcp_tool_timeout_seconds": pins["codex"]["mcp_tool_timeout_seconds"],
            "ri_data_directory": "/logs/agent/relentless-inception",
            "artifact_hashes": artifact_hashes,
        },
    }


def relative_to_attempt(path: Path, attempt_directory: Path) -> str:
    return path.resolve().relative_to(attempt_directory.resolve()).as_posix()


def build_evidence_index(attempt_directory: Path, contract: dict[str, Any]) -> Path:
    result_candidates = []
    for path in (attempt_directory / "jobs").rglob("result.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("task_name") and value.get("trial_name"):
            result_candidates.append(path)
    if len(result_candidates) != 1:
        raise RuntimeError(
            f"Expected one trial result under {attempt_directory}, found {len(result_candidates)}"
        )

    result_path = result_candidates[0]
    trial_directory = result_path.parent
    trajectory_path = trial_directory / "agent" / "trajectory.json"
    codex_log_path = trial_directory / "agent" / "codex.txt"
    reward_path = trial_directory / "verifier" / "reward.json"
    # Both harnesses retain /logs/agent under the trial directory before deleting
    # the container. RI writes there directly, avoiding a conflicting nested bind.
    ri_root = trial_directory / "agent" / "relentless-inception" / "runs"
    ri_runs = []
    for manifest_path in sorted(ri_root.glob("*/manifest.json")):
        ledger_path = manifest_path.with_name("ledger.json")
        ri_runs.append(
            {
                "run_id": manifest_path.parent.name,
                "manifest": relative_to_attempt(manifest_path, attempt_directory),
                "ledger": relative_to_attempt(ledger_path, attempt_directory),
            }
        )

    index = {
        "schema_version": 1,
        "harness": contract["harness"],
        "task": contract["task"],
        "attempt": contract["attempt"],
        "contract": "run-contract.json",
        "result": relative_to_attempt(result_path, attempt_directory),
        "trajectory": relative_to_attempt(trajectory_path, attempt_directory),
        "codex_log": relative_to_attempt(codex_log_path, attempt_directory),
        "ri_runs": ri_runs,
    }
    if contract["harness"] == "pier":
        index["deep_swe_reward"] = relative_to_attempt(reward_path, attempt_directory)
    index_path = attempt_directory / "evidence.json"
    write_json(index_path, index)
    return index_path


def run_attempt(
    pins: dict[str, Any],
    task: str,
    attempt: int,
    evidence_root: Path,
    deep_swe_root: Path | None,
    dry_run: bool,
    artifact_hashes: dict[str, str],
    validate_attempt_fn: Callable[[Path], None],
) -> Path | None:
    attempt_directory = evidence_root / task / f"attempt-{attempt}"
    if dry_run:
        command = build_command(pins, task, attempt_directory, deep_swe_root)
        print(json.dumps({"task": task, "attempt": attempt, "command": command}))
        return None

    if attempt_directory.exists():
        raise RuntimeError(f"Fresh evidence directory already exists: {attempt_directory}")
    attempt_directory.mkdir(parents=True, mode=0o700)
    attempt_directory.chmod(0o700)

    harness = task_harness(pins, task)
    if harness == "pier":
        assert deep_swe_root is not None
        verify_deep_swe_checkout(pins, task, deep_swe_root)
    task_pins = pins[harness]["tasks"][task]
    observed_digest = inspect_image_digest(task_pins["image"])
    if observed_digest != task_pins["image_digest"]:
        raise RuntimeError(
            f"Image digest drift for {task}: {observed_digest} != {task_pins['image_digest']}"
        )

    with tempfile.TemporaryDirectory(prefix="ri-bench-secret-") as secret_directory:
        secret_file = Path(secret_directory) / "xai-api-key"
        write_ephemeral_secret(secret_file, resolve_xai_api_key())
        command = build_command(
            pins,
            task,
            attempt_directory,
            deep_swe_root,
            secret_file,
        )
        contract = make_contract(
            pins,
            task,
            attempt,
            command,
            observed_digest,
            artifact_hashes,
        )
        write_json(attempt_directory / "run-contract.json", contract)
        subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            env=build_child_environment(),
            check=True,
        )
    index_path = build_evidence_index(attempt_directory, contract)
    validate_attempt_fn(index_path)
    return index_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", action="append", dest="tasks")
    parser.add_argument("--attempt", type=int, choices=(1, 2))
    parser.add_argument("--final-two", action="store_true")
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--deep-swe-root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.final_two == (args.attempt is not None):
        parser.error("choose exactly one of --attempt or --final-two")
    return args


def main() -> int:
    args = parse_args()
    pins = load_pins()
    artifact_hashes = verify_artifact_hashes(pins)
    # The validator imports pinned prompt builders. Do not execute that module
    # until its own hash and the complete runtime source tree have been checked.
    from validate_evidence import validate_attempt, validate_final

    tasks = args.tasks or [
        *pins["harbor"]["tasks"],
        *pins["pier"]["tasks"],
    ]
    for task in tasks:
        task_harness(pins, task)
    if any(task_harness(pins, task) == "pier" for task in tasks) and args.deep_swe_root is None:
        raise ValueError("--deep-swe-root is required before starting a campaign with Pier tasks")
    attempts = (1, 2) if args.final_two else (args.attempt,)
    for task in tasks:
        for attempt in attempts:
            assert attempt is not None
            try:
                run_attempt(
                    pins,
                    task,
                    attempt,
                    args.evidence_root.resolve(),
                    args.deep_swe_root.resolve() if args.deep_swe_root else None,
                    args.dry_run,
                    artifact_hashes,
                    validate_attempt,
                )
            except Exception as exc:
                print(f"ERROR: {task} attempt {attempt}: {exc}", file=sys.stderr)
                return 1
    if args.final_two and args.tasks is None and not args.dry_run:
        validate_final(args.evidence_root.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
