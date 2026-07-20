"""Verified execution handoffs; local execution remains under Codex policy."""

from __future__ import annotations

import json
import subprocess
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from .config import canonical_json
from .errors import ConfigError


HANDOFF_SCHEMA_VERSION = 2
HANDOFF_PAYLOAD_HASH_FIELD = "handoff_payload_sha256"
SUPPORTED_HANDOFF_SECTIONS = (
    "fused_plan",
    "constraints",
    "minority_findings",
    "blind_spots",
    "required_checks",
    "budget_remaining",
)
_EXECUTION_CONTRACT_FIELDS = (
    "enabled",
    "mode",
    "remote_models_may_write_workspace",
    "require_fused_plan",
    "require_pre_execution_gate",
    "require_post_execution_gate",
    "require_user_approval_for_destructive_actions",
    "require_user_approval_for_external_writes",
    "preserve_unrelated_changes",
    "workspace_scope",
    "sandbox_mode",
    "run_tests",
    "require_diff_review",
    "max_fix_cycles",
    "stop_on_test_failure",
    "handoff_include",
    "completion_requires",
    "allow_recursive_codex_cli",
    "codex_binary",
    "model",
    "reasoning_effort",
    "timeout_seconds",
)


def _json_copy(value: Any) -> Any:
    """Return a detached, JSON-safe value suitable for a persisted contract."""

    return json.loads(canonical_json(value))


def _execution_contract(
    execution: Mapping[str, Any], native_codex: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    contract = {
        field_name: _json_copy(execution[field_name])
        for field_name in _EXECUTION_CONTRACT_FIELDS
        if field_name in execution
    }
    if isinstance(native_codex, Mapping):
        contract["native_codex"] = _json_copy(native_codex)
    return contract


def _contract_hash(contract: Mapping[str, Any]) -> str:
    return sha256(canonical_json(contract).encode("utf-8")).hexdigest()


def _handoff_contract_hash(profile_name: Optional[str], contract: Mapping[str, Any]) -> str:
    return _contract_hash({"selected_profile": profile_name, "execution_contract": contract})


def _handoff_payload_hash(handoff: Mapping[str, Any]) -> str:
    payload = _json_copy(handoff)
    if isinstance(payload, dict):
        payload.pop(HANDOFF_PAYLOAD_HASH_FIELD, None)
    return _contract_hash(payload)


def _configured_backend(execution: Mapping[str, Any]) -> str:
    mode = str(execution.get("mode", "codex_handoff"))
    return {
        "codex_handoff": "active_codex",
        "codex_cli": "codex_cli",
        "none": "none",
    }.get(mode, mode)


def _stage_config(gates: Mapping[str, Any], stage_name: str) -> Mapping[str, Any]:
    stages = gates.get("stages", {})
    if not isinstance(stages, Mapping):
        return {}
    stage = stages.get(stage_name, {})
    return stage if isinstance(stage, Mapping) else {}


def _enabled_host_stages(gates: Mapping[str, Any]) -> list[str]:
    if not bool(gates.get("enabled", False)):
        return []
    return [
        stage_name
        for stage_name in ("plan", "pre_execution", "post_execution", "final", "summarize")
        if bool(_stage_config(gates, stage_name).get("enabled", False))
    ]


def _required_checks(execution: Mapping[str, Any], gates: Mapping[str, Any]) -> Dict[str, Any]:
    lifecycle: Dict[str, Any] = {}
    for stage_name in _enabled_host_stages(gates):
        stage = _stage_config(gates, stage_name)
        lifecycle[stage_name] = {
            "owner": "codex_host",
            "required_evidence": list(stage.get("required_evidence", [])),
            "tool_policy": stage.get("tool_policy", "none"),
            "timeout_seconds": stage.get("timeout_seconds"),
        }
    for stage_name, required in (
        ("pre_execution", execution.get("require_pre_execution_gate", False)),
        ("post_execution", execution.get("require_post_execution_gate", False)),
    ):
        if bool(required) and stage_name not in lifecycle:
            lifecycle[stage_name] = {
                "owner": "codex_host",
                "required_evidence": [],
                "tool_policy": "none",
                "timeout_seconds": None,
            }
    return {
        "lifecycle_gates": lifecycle,
        "run_tests": bool(execution.get("run_tests", False)),
        "require_diff_review": bool(execution.get("require_diff_review", False)),
        "completion_requires": list(execution.get("completion_requires", [])),
    }


def _execution_constraints(execution: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        field_name: _json_copy(execution[field_name])
        for field_name in (
            "remote_models_may_write_workspace",
            "require_user_approval_for_destructive_actions",
            "require_user_approval_for_external_writes",
            "preserve_unrelated_changes",
            "workspace_scope",
            "sandbox_mode",
            "max_fix_cycles",
            "stop_on_test_failure",
        )
        if field_name in execution
    }


def _remaining_budget(budgets: Mapping[str, Any], ledger: Mapping[str, Any]) -> Dict[str, Any]:
    input_tokens = int(ledger.get("input_tokens", 0))
    output_tokens = int(ledger.get("output_tokens", 0))
    reasoning_tokens = int(ledger.get("reasoning_tokens", 0))
    counters = {
        "calls": ("max_calls", int(ledger.get("calls", 0))),
        "total_tokens": (
            "max_total_tokens",
            # Cached tokens are an input detail and reasoning tokens are an
            # output detail in the normalized ledger; adding them would count
            # the same billed tokens twice.
            input_tokens + output_tokens,
        ),
        "input_tokens": ("max_input_tokens", input_tokens),
        "output_tokens": ("max_output_tokens", output_tokens),
        "reasoning_tokens": ("max_reasoning_tokens", reasoning_tokens),
        "tool_calls": ("max_tool_calls", int(ledger.get("tool_calls", 0))),
        "wall_seconds": ("max_wall_seconds", float(ledger.get("wall_seconds", 0.0))),
        "known_cost_usd": ("max_cost_usd", float(ledger.get("known_cost_usd", 0.0))),
    }
    remaining: Dict[str, Any] = {}
    for counter_name, (limit_name, consumed) in counters.items():
        limit = budgets.get(limit_name)
        if not isinstance(limit, (int, float)) or isinstance(limit, bool):
            continue
        remaining[counter_name] = {
            "limit": limit,
            "consumed": consumed,
            "remaining": max(0, limit - consumed),
        }
    remaining["unknown_cost_calls"] = int(ledger.get("unknown_cost_calls", 0))
    remaining["warnings"] = list(ledger.get("warnings", []))
    provider_cost = ledger.get("provider_cost_usd", {})
    provider_limits = budgets.get("per_provider_max_cost_usd", {})
    if isinstance(provider_cost, Mapping) and isinstance(provider_limits, Mapping):
        remaining["provider_cost_usd"] = {
            str(provider_name): {
                "limit": provider_limit,
                "consumed": float(provider_cost.get(provider_name, 0.0)),
                "remaining": max(0.0, float(provider_limit) - float(provider_cost.get(provider_name, 0.0))),
            }
            for provider_name, provider_limit in provider_limits.items()
            if isinstance(provider_limit, (int, float)) and not isinstance(provider_limit, bool)
        }
    return remaining


def _selected_artifacts(
    synthesis: str,
    execution: Mapping[str, Any],
    judge: Mapping[str, Any],
    gates: Mapping[str, Any],
    budgets: Mapping[str, Any],
    ledger: Mapping[str, Any],
) -> Dict[str, Any]:
    requested = execution.get("handoff_include", SUPPORTED_HANDOFF_SECTIONS)
    if not isinstance(requested, Sequence) or isinstance(requested, (str, bytes)):
        raise ConfigError("execution.handoff_include must be an array of supported section names")
    requested_names = [str(name) for name in requested]
    unsupported = sorted(set(requested_names) - set(SUPPORTED_HANDOFF_SECTIONS))
    if unsupported:
        raise ConfigError("Unsupported execution handoff section(s): " + ", ".join(unsupported))

    available: Dict[str, Any] = {
        "fused_plan": synthesis,
        "constraints": _execution_constraints(execution),
        "minority_findings": list(judge.get("minority_findings", [])),
        "blind_spots": list(judge.get("blind_spots", [])),
        "required_checks": _required_checks(execution, gates),
        "budget_remaining": _remaining_budget(budgets, ledger),
    }
    return {section_name: available[section_name] for section_name in requested_names}


def _instruction(
    artifacts: Mapping[str, Any],
    *,
    pending_gates: Sequence[str],
    later_gates: Sequence[str],
) -> str:
    if pending_gates:
        authorization = (
            "Do not mutate files or external state yet. The active Codex host must independently invoke "
            + ", ".join(pending_gates)
            + " lifecycle gate(s) with their required evidence and retain their same-artifact receipts first."
        )
    else:
        authorization = "The configured pre-mutation lifecycle gates are not pending."
    completion = (
        " After implementation, the active Codex host must invoke the configured "
        + ", ".join(later_gates)
        + " gate(s) over the exact resulting artifact and evidence."
        if later_gates
        else ""
    )
    return (
        "This is a persisted Codex host-workflow packet, not independent permission to act. "
        "Re-inspect the current workspace, preserve user changes, honor newer user instructions and the live "
        "sandbox/approval policy, and stop if repository reality contradicts the packet. "
        + authorization
        + completion
        + "\n\nSelected handoff artifacts:\n"
        + json.dumps(artifacts, indent=2, sort_keys=True, ensure_ascii=False)
    )


def build_handoff(
    synthesis: str,
    run_id: str,
    gate: Mapping[str, Any],
    execution: Mapping[str, Any],
    *,
    profile_name: Optional[str] = None,
    judge: Optional[Mapping[str, Any]] = None,
    ledger: Optional[Mapping[str, Any]] = None,
    budgets: Optional[Mapping[str, Any]] = None,
    gates: Optional[Mapping[str, Any]] = None,
    native_codex: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a self-contained, immutable host-workflow packet.

    The supplied ``gate`` is the MCP runtime's synthesis gate only. Named plan,
    pre-execution, post-execution, final, and summarize gates remain owned by the
    active Codex host and are never silently inferred from the synthesis verdict.
    """

    judge = judge or {}
    ledger = ledger or {}
    budgets = budgets or {}
    gates = gates or {}
    contract = _execution_contract(execution, native_codex)
    backend = _configured_backend(execution)
    enabled = bool(execution.get("enabled", True))
    synthesis_gate_passed = gate.get("passed") is True
    artifacts = _selected_artifacts(synthesis, execution, judge, gates, budgets, ledger)
    fused_plan_available = bool(str(artifacts.get("fused_plan", "")).strip())
    fused_plan_required = bool(execution.get("require_fused_plan", True))

    enabled_host_stages = _enabled_host_stages(gates)
    pending_gates = [stage for stage in ("plan", "pre_execution") if stage in enabled_host_stages]
    if bool(execution.get("require_pre_execution_gate", False)) and "pre_execution" not in pending_gates:
        pending_gates.append("pre_execution")
    later_gates = [stage for stage in ("post_execution", "final", "summarize") if stage in enabled_host_stages]
    if bool(execution.get("require_post_execution_gate", False)) and "post_execution" not in later_gates:
        later_gates.insert(0, "post_execution")
    if (
        isinstance(native_codex, Mapping)
        and native_codex.get("require_gate_after_execution") is True
        and "post_execution" not in later_gates
    ):
        later_gates.insert(0, "post_execution")

    blocking_reasons: list[str] = []
    if not enabled:
        blocking_reasons.append("execution_disabled")
    if backend == "none":
        blocking_reasons.append("execution_backend_none")
    if not synthesis_gate_passed:
        blocking_reasons.append("synthesis_gate_not_passed")
    if fused_plan_required and not fused_plan_available:
        blocking_reasons.append("required_fused_plan_not_in_handoff")

    ready_for_host_workflow = not blocking_reasons
    mutation_authorized = ready_for_host_workflow and not pending_gates
    status = (
        "blocked"
        if not ready_for_host_workflow
        else "awaiting_host_gates"
        if pending_gates
        else "ready_for_execution"
    )
    handoff = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "backend": backend,
        "status": status,
        "ready_for_host_workflow": ready_for_host_workflow,
        "ready": mutation_authorized,
        "mutation_authorized": mutation_authorized,
        "requires_explicit_confirmation": backend == "codex_cli",
        "run_id": run_id,
        "selected_profile": profile_name,
        "execution_contract": contract,
        "execution_contract_sha256": _contract_hash(contract),
        "handoff_contract_sha256": _handoff_contract_hash(profile_name, contract),
        "synthesis_gate": {
            "owner": "mcp_runtime",
            "passed": synthesis_gate_passed,
            "artifact_sha256": gate.get("artifact_sha256"),
        },
        "lifecycle": {
            "stage_owner": "codex_host",
            "pending_gates": pending_gates,
            "later_gates": later_gates,
            "host_receipts_required": bool(pending_gates or later_gates),
        },
        "blocking_reasons": blocking_reasons,
        "included_sections": list(artifacts),
        "artifacts": artifacts,
        "instruction": _instruction(artifacts, pending_gates=pending_gates, later_gates=later_gates),
    }
    handoff[HANDOFF_PAYLOAD_HASH_FIELD] = _handoff_payload_hash(handoff)
    return handoff


def persisted_execution_contract(handoff: Mapping[str, Any]) -> Dict[str, Any]:
    """Read and verify the hash-bound execution settings frozen into a handoff."""

    schema_version = handoff.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != HANDOFF_SCHEMA_VERSION
    ):
        raise ConfigError(
            f"Unsupported execution handoff schema_version: {handoff.get('schema_version')!r}"
        )
    expected_payload_hash = handoff.get(HANDOFF_PAYLOAD_HASH_FIELD)
    actual_payload_hash = _handoff_payload_hash(handoff)
    if not isinstance(expected_payload_hash, str) or expected_payload_hash != actual_payload_hash:
        raise ConfigError("Execution handoff payload hash does not match")
    contract = handoff.get("execution_contract")
    if not isinstance(contract, Mapping):
        raise ConfigError("Execution handoff does not contain a persisted execution contract")
    expected_hash = handoff.get("execution_contract_sha256")
    actual_hash = _contract_hash(contract)
    if not isinstance(expected_hash, str) or expected_hash != actual_hash:
        raise ConfigError("Execution handoff execution contract hash does not match")
    expected_handoff_hash = handoff.get("handoff_contract_sha256")
    actual_handoff_hash = _handoff_contract_hash(
        str(handoff["selected_profile"]) if handoff.get("selected_profile") is not None else None,
        contract,
    )
    if not isinstance(expected_handoff_hash, str) or expected_handoff_hash != actual_handoff_hash:
        raise ConfigError("Execution handoff selected profile or contract hash does not match")
    return _json_copy(contract)


def execute_codex_cli(
    handoff: Mapping[str, Any],
    *,
    workdir: str,
    confirmed: bool,
    expected_payload_sha256: str,
) -> Dict[str, Any]:
    """Execute using only the settings persisted with the reviewed handoff."""

    persisted_payload_hash = handoff.get(HANDOFF_PAYLOAD_HASH_FIELD)
    if (
        not isinstance(persisted_payload_hash, str)
        or expected_payload_sha256 != persisted_payload_hash
    ):
        raise ConfigError(
            "Recursive Codex CLI execution requires the expected reviewed handoff payload hash"
        )
    execution = persisted_execution_contract(handoff)
    if handoff.get("backend") != "codex_cli" or execution.get("mode") != "codex_cli":
        raise ConfigError("Recursive CLI execution requested for a non-codex_cli handoff")
    if handoff.get("ready") is not True or handoff.get("mutation_authorized") is not True:
        raise ConfigError("Execution handoff is not authorized for mutation; complete all pending host gates first")
    if execution.get("allow_recursive_codex_cli", False) is not True:
        raise ConfigError("Recursive Codex CLI execution is disabled in the persisted execution contract")
    if not confirmed:
        raise ConfigError("Recursive Codex CLI execution requires an explicit confirmation flag")
    resolved_workdir = Path(workdir).expanduser().resolve()
    if not resolved_workdir.is_dir():
        raise ConfigError(f"Execution workdir is not a directory: {resolved_workdir}")
    sandbox_mode = str(execution.get("sandbox_mode", "workspace_write"))
    sandbox = {"workspace_write": "workspace-write", "read_only": "read-only"}.get(sandbox_mode)
    if sandbox is None:
        raise ConfigError(f"Unsupported persisted sandbox_mode: {sandbox_mode}")
    command = [
        str(execution.get("codex_binary", "codex")),
        "exec",
        "--ephemeral",
        "--sandbox",
        sandbox,
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
        "selected_profile": handoff.get("selected_profile"),
        "execution_contract_sha256": handoff.get("execution_contract_sha256"),
        "handoff_contract_sha256": handoff.get("handoff_contract_sha256"),
        HANDOFF_PAYLOAD_HASH_FIELD: handoff.get(HANDOFF_PAYLOAD_HASH_FIELD),
    }
