"""Verified execution handoffs; local execution remains under Codex policy."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .errors import ConfigError


def build_handoff(synthesis: str, run_id: str, gate: Mapping[str, Any], execution: Mapping[str, Any]) -> Dict[str, Any]:
    configured_backend = execution.get("backend") or execution.get("mode", "codex_handoff")
    backend = "active_codex" if configured_backend == "codex_handoff" else str(configured_backend)
    gate_passed = bool(gate.get("passed", False))
    require_gate = bool(execution.get("require_passing_gate", execution.get("require_post_execution_gate", True)))
    enabled = bool(execution.get("enabled", True))
    allowed = enabled and (gate_passed or not require_gate)
    return {
        "backend": backend,
        "ready": allowed and backend != "none",
        "requires_explicit_confirmation": backend == "codex_cli",
        "run_id": run_id,
        "instruction": (
            "Execute the verified synthesis below in the active Codex session. Re-inspect the current workspace, "
            "preserve user changes, use the active sandbox/approval policy, run mechanical verification, and stop "
            "if reality contradicts the plan.\n\n" + synthesis
        ) if allowed else "Execution blocked because the configured adversarial gate did not pass.",
    }


def execute_codex_cli(
    handoff: Mapping[str, Any],
    execution: Mapping[str, Any],
    *,
    workdir: str,
    confirmed: bool,
) -> Dict[str, Any]:
    if handoff.get("backend") != "codex_cli":
        raise ConfigError("Recursive CLI execution requested for a non-codex_cli handoff")
    if execution.get("allow_recursive_codex_cli", False) is not True:
        raise ConfigError("Recursive Codex CLI execution is disabled in configuration")
    if not confirmed:
        raise ConfigError("Recursive Codex CLI execution requires an explicit confirmation flag")
    resolved_workdir = Path(workdir).expanduser().resolve()
    if not resolved_workdir.is_dir():
        raise ConfigError(f"Execution workdir is not a directory: {resolved_workdir}")
    command = [
        str(execution.get("codex_binary", "codex")),
        "exec",
        "--ephemeral",
        "--sandbox",
        str(execution.get("sandbox", "workspace-write")),
        "-C",
        str(resolved_workdir),
    ]
    model = execution.get("model")
    if model:
        command.extend(["--model", str(model)])
    effort = execution.get("reasoning_effort")
    if effort:
        command.extend(["-c", f"model_reasoning_effort={json.dumps(str(effort))}"])
    command.append(str(handoff["instruction"]))
    timeout = float(execution.get("timeout_seconds", 3600))
    completed = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
    return {
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "workdir": str(resolved_workdir),
    }
