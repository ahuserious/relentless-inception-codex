#!/usr/bin/env python3
"""Fail-closed validation for immutable benchmark evidence."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import shlex
import sys
from typing import Any, Iterable, Mapping, Sequence


BENCH_ROOT = Path(__file__).resolve().parent
PINS = json.loads((BENCH_ROOT / "pins.json").read_text(encoding="utf-8"))
PLUGIN_ROOT = BENCH_ROOT.parent / "plugins" / "relentless-inception"


def _source_tree_sha256(root: Path) -> str:
    """Match the runner's path/mode/content tree pin before importing plugin code."""

    digest = sha256()
    paths: list[Path] = []
    for path in root.rglob("*"):
        relative_parts = path.relative_to(root).parts
        if "__pycache__" in relative_parts or path.name == ".DS_Store":
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            raise RuntimeError(f"Pinned plugin source tree contains a symlink: {path}")
        if path.is_file():
            paths.append(path)
    for path in sorted(paths, key=lambda value: value.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        mode = path.stat().st_mode & 0o777
        file_digest = sha256(path.read_bytes()).hexdigest()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{mode:o}".encode("ascii"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


expected_plugin_tree_sha256 = PINS.get("artifacts", {}).get("plugin_tree_sha256")
observed_plugin_tree_sha256 = _source_tree_sha256(PLUGIN_ROOT)
if observed_plugin_tree_sha256 != expected_plugin_tree_sha256:
    raise RuntimeError(
        "Pinned plugin source tree drift before validator prompt-contract import: "
        f"expected {expected_plugin_tree_sha256}, observed {observed_plugin_tree_sha256}"
    )

DEFAULT_PLUGIN_CONFIG = json.loads(
    (PLUGIN_ROOT / "config" / "default.json").read_text(encoding="utf-8")
)
expected_runtime_package_root = (PLUGIN_ROOT / "relentless_inception").resolve()
for cached_module_name in tuple(sys.modules):
    if not (
        cached_module_name == "relentless_inception"
        or cached_module_name.startswith("relentless_inception.")
    ):
        continue
    # A cached module may have been monkeypatched or may lie about __file__.
    # The validator must construct prompt receipts only from a fresh import of
    # the source tree whose content was checked immediately above.
    raise RuntimeError(
        "Validator refuses a pre-cached relentless_inception module before its "
        f"pinned prompt-contract import: {cached_module_name}"
    )
plugin_import_path = str(PLUGIN_ROOT)
sys.path.insert(0, plugin_import_path)
try:
    import relentless_inception as runtime_plugin_package  # noqa: E402
    import relentless_inception.orchestrator as runtime_orchestrator_module  # noqa: E402
    import relentless_inception.prompts as runtime_prompts_module  # noqa: E402
    from relentless_inception.orchestrator import (  # noqa: E402
        FusionOrchestrator as RuntimeFusionOrchestrator,
        VERDICT_SCHEMA as RUNTIME_VERDICT_SCHEMA,
        _judge_contract as runtime_judge_contract,
        _panel_context_bundle as runtime_panel_context_bundle,
    )
    from relentless_inception.prompts import (  # noqa: E402
        gate_prompt as runtime_gate_prompt,
        gate_system as runtime_gate_system,
        judge_prompt as runtime_judge_prompt,
        judge_system as runtime_judge_system,
        panel_prompt as runtime_panel_prompt,
        panel_system as runtime_panel_system,
        synthesis_prompt as runtime_synthesis_prompt,
        synthesis_system as runtime_synthesis_system,
    )
finally:
    if sys.path and sys.path[0] == plugin_import_path:
        sys.path.pop(0)
    elif plugin_import_path in sys.path:
        sys.path.remove(plugin_import_path)

for runtime_module_name, runtime_module, expected_runtime_module_path in (
    (
        "relentless_inception",
        runtime_plugin_package,
        expected_runtime_package_root / "__init__.py",
    ),
    (
        "relentless_inception.orchestrator",
        runtime_orchestrator_module,
        expected_runtime_package_root / "orchestrator.py",
    ),
    (
        "relentless_inception.prompts",
        runtime_prompts_module,
        expected_runtime_package_root / "prompts.py",
    ),
):
    runtime_module_file = getattr(runtime_module, "__file__", None)
    if not isinstance(runtime_module_file, str) or (
        Path(runtime_module_file).resolve() != expected_runtime_module_path.resolve()
    ):
        raise RuntimeError(
            "Validator imported a prompt-contract module outside the pinned plugin "
            f"tree: {runtime_module_name} from {runtime_module_file!r}"
        )
BENCHMARK_PROFILE_NAME = "maximum_intelligence"
HASH = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
SECRET_PATTERNS = (
    re.compile(r"\bxai-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{12,}", re.IGNORECASE),
)
SAFE_SECRET_VALUES = {"", "[REDACTED]", "<REDACTED>", "***"}
LIFECYCLE_STAGES = ("plan", "pre_execution", "post_execution", "final", "summarize")
EXPECTED_RUN_IDS = {
    "fuse": "benchmark-fuse",
    "plan": "benchmark-plan",
    "pre_execution": "benchmark-pre-execution",
    "post_execution": "benchmark-post-execution",
    "final": "benchmark-final",
    "summarize": "benchmark-summarize",
}
MAX_PREFLIGHT_COMMAND_RECORDS = 12
MAX_PREFLIGHT_EVIDENCE_CHARACTERS = 12_000
MAX_COMPLETED_ARTIFACT_CHARACTERS = 12_000
FUSION_TASK_PREFIX = (
    "Produce a pre-execution plan for the active Codex host to fulfill the exact "
    "benchmark instruction below. The current artifact is reviewed as a plan; "
    "execution has not begun and execution evidence is not yet expected. Do not claim "
    "workspace inspection, commands, tests, or mutation beyond supplied mechanical "
    "evidence."
)
LIFECYCLE_TASK_PREFIXES = {
    "plan": (
        "Lifecycle stage: plan\nReview plan coverage for the exact original action "
        "request below. Host execution is still pending.\n"
    ),
    "pre_execution": (
        "Lifecycle stage: pre_execution\nReview execution-readiness for the exact "
        "original action request below. Host execution is still pending.\n"
    ),
    "post_execution": (
        "Lifecycle stage: post_execution\nReview actual execution for the exact original "
        "action request below. Execution is complete.\n"
    ),
    "final": (
        "Lifecycle stage: final\nReview final acceptance for the exact original action "
        "request below. Execution is complete.\n"
    ),
    "summarize": (
        "Lifecycle stage: summarize\nReview remaining risks for the exact original action "
        "request below. Execution is complete.\n"
    ),
}
RECORD_HELPERS = (
    """set -o pipefail
run_record() {
  command_text=$1
  printf '$ %s\\n' "$command_text"
  bash -o pipefail -c "$command_text"
  command_status=$?
  printf '\\n[exit %d]\\n' "$command_status"
  return "$command_status"
}""",
    # Accepted for diagnostic compatibility with the first physical trace. It
    # still executes and captures the command exactly; the parser handles its
    # legacy missing fresh-line delimiter fail-closed.
    """set -o pipefail
run_record() {
  cmd_text=$1
  printf '$ %s\\n' "$cmd_text"
  bash -o pipefail -c "$cmd_text"
  cmd_status=$?
  printf '[exit %d]\\n' "$cmd_status"
  return "$cmd_status"
}""",
)
EXPECTED_GATE_REVIEWERS = {
    "grok45_verifier": "grok-4.5",
    "grok45_constraint_auditor": "grok-4.5",
}
EXPECTED_FUSION_CALLS = Counter(
    {
        ("panel", "grok45_researcher", "grok-4.5"): 1,
        ("panel", "grok45_adversary", "grok-4.5"): 1,
        ("panel", "grok45_constraint_auditor", "grok-4.5"): 1,
        ("judge", "grok45_judge", "grok-4.5"): 1,
        ("synthesis", "grok45_synthesizer", "grok-4.5"): 1,
        ("gate", "grok45_verifier", "grok-4.5"): 1,
        ("gate", "grok45_constraint_auditor", "grok-4.5"): 1,
    }
)
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
EXECUTION_CONTRACT_FIELDS = (
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
MODEL_RESPONSE_FIELDS = {
    "text",
    "provider",
    "requested_model",
    "actual_model",
    "usage",
    "latency_seconds",
    "request_id",
    "route",
    "raw_status",
}
USAGE_FIELDS = {
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "tool_calls",
    "cost_usd",
    "unknown_cost_fail_closed",
    "input_output_usage_complete",
    "raw_usage_invalid",
    "accounting_error",
}
LEDGER_FIELDS = {
    "schema_version",
    "calls",
    "attempts",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "total_tokens",
    "tool_calls",
    "known_cost_usd",
    "provider_cost_usd",
    "unknown_cost_calls",
    "accounting_failure",
    "stop_reason",
    "wall_seconds",
    "attempt_entries",
    "entries",
    "warnings",
}
ATTEMPT_ENTRY_FIELDS = {
    "attempt_index",
    "attempt_id",
    "stage",
    "seat",
    "invocation_sha256",
}
LEDGER_ENTRY_FIELDS = {
    "attempt_index",
    "attempt_id",
    "entry_id",
    "invocation_sha256",
    "response_sha256",
    "response_artifact",
    "stage",
    "seat",
    "provider",
    "requested_model",
    "actual_model",
    "request_id",
    "route",
    "raw_status",
    "latency_seconds",
    "usage",
}
MANIFEST_FIELDS = {
    "run_id",
    "task_hash",
    "config_hash",
    "input_hash",
    "status",
    "created_at",
    "updated_at",
    "stages",
}
FUSION_RESULT_FIELDS = {
    "run_id",
    "task_hash",
    "config_hash",
    "status",
    "synthesis",
    "gate",
    "panel",
    "judge",
    "ledger",
    "artifacts_dir",
    "execution_handoff",
}
LIFECYCLE_RESULT_FIELDS = {"run_id", "artifacts_dir", "gate", "ledger"}
CODEX_TURN_USAGE_FIELDS = {
    "cache_write_input_tokens",
    "cached_input_tokens",
    "input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
}
FUSION_ATTEMPT_STAGE_RANKS = {
    "panel": 0,
    "judge": 1,
    "synthesis": 2,
    "gate": 3,
    "amendment-1": 4,
    "gate-1": 5,
    "amendment-2": 6,
    "gate-2": 7,
}


class EvidenceError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def normalized_whitespace(value: str) -> str:
    return " ".join(value.split())


def canonical_json_hash(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EvidenceError("Receipt evidence is not canonical JSON") from exc
    return sha256(encoded.encode("utf-8")).hexdigest()


def nonnegative_integer(value: Any, label: str) -> int:
    require(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0,
        f"{label} must be a nonnegative integer",
    )
    return value


def nonnegative_number(value: Any, label: str) -> float:
    require(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0,
        f"{label} must be a nonnegative finite number",
    )
    return float(value)


def optional_message(value: Any, label: str) -> str | None:
    require(
        value is None or (isinstance(value, str) and bool(value)),
        f"{label} must be a nonempty string or null",
    )
    return value


def validate_usage(value: Any, label: str) -> dict[str, Any]:
    require(
        isinstance(value, dict) and set(value) == USAGE_FIELDS,
        f"{label} usage schema is invalid",
    )
    for counter_name in (
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "tool_calls",
    ):
        nonnegative_integer(value[counter_name], f"{label} {counter_name}")
    require(
        value["cached_tokens"] <= value["input_tokens"],
        f"{label} cached_tokens exceeds input_tokens",
    )
    require(
        value["reasoning_tokens"] <= value["output_tokens"],
        f"{label} reasoning_tokens exceeds output_tokens",
    )
    for flag_name in (
        "unknown_cost_fail_closed",
        "input_output_usage_complete",
        "raw_usage_invalid",
    ):
        require(isinstance(value[flag_name], bool), f"{label} {flag_name} is not boolean")
    accounting_error = optional_message(value["accounting_error"], f"{label} accounting_error")
    cost_usd = value["cost_usd"]
    if cost_usd is not None:
        nonnegative_number(cost_usd, f"{label} cost_usd")
    requires_accounting_latch = (
        not value["input_output_usage_complete"]
        or value["raw_usage_invalid"]
        or value["unknown_cost_fail_closed"]
        or cost_usd is None
    )
    require(
        not requires_accounting_latch or accounting_error is not None,
        f"{label} has an unlatched usage integrity failure",
    )
    return value


def validate_model_response(value: Any, label: str) -> dict[str, Any]:
    require(
        isinstance(value, dict) and set(value) == MODEL_RESPONSE_FIELDS,
        f"{label} ModelResponse schema is invalid",
    )
    for field_name in ("text", "provider", "requested_model", "actual_model"):
        require(
            isinstance(value[field_name], str) and bool(value[field_name]),
            f"{label} {field_name} is missing",
        )
    validate_usage(value["usage"], label)
    nonnegative_number(value["latency_seconds"], f"{label} latency_seconds")
    require(
        isinstance(value["request_id"], str) and bool(value["request_id"]),
        f"{label} request_id is missing",
    )
    require(isinstance(value["route"], dict), f"{label} route is not an object")
    require(value["raw_status"] == "completed", f"{label} did not complete")
    return value


def parse_utc_timestamp(value: Any, label: str) -> datetime:
    require(isinstance(value, str) and bool(value), f"{label} timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError(f"{label} timestamp is invalid") from exc
    require(
        parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed),
        f"{label} timestamp is not UTC",
    )
    return parsed


def expected_run_identity(
    stage: str,
    arguments: dict[str, Any],
) -> tuple[str, str]:
    task = arguments["task"]
    if stage == "fuse":
        runtime_task = task
        input_identity = {
            "operation": "fuse",
            "task": task,
            "context": arguments["context"],
            "mechanical_evidence": arguments["mechanical_evidence"],
            "profile_name": BENCHMARK_PROFILE_NAME,
        }
    else:
        artifact_hash = sha256(arguments["artifact"].encode("utf-8")).hexdigest()
        runtime_task = task + "\n\nARTIFACT-SHA256:" + artifact_hash
        input_identity = {
            "operation": "adversarial_gate",
            "task": task,
            "artifact_sha256": artifact_hash,
            "mechanical_evidence": arguments["mechanical_evidence"],
            "profile_name": BENCHMARK_PROFILE_NAME,
        }
    return (
        sha256(runtime_task.encode("utf-8")).hexdigest(),
        canonical_json_hash(input_identity),
    )


def validate_attempt_stage_order(
    attempts: list[dict[str, Any]], *, fusion_run: bool
) -> None:
    """Enforce the runtime reservation order while allowing peers within a stage."""

    stages = [attempt.get("stage") for attempt in attempts]
    if not fusion_run:
        require(
            all(stage == "gate" for stage in stages),
            "Lifecycle attempt reservation contains a non-gate stage",
        )
        return
    require(
        all(stage in FUSION_ATTEMPT_STAGE_RANKS for stage in stages),
        "Fusion attempt reservation contains an unexpected stage",
    )
    ranks = [FUSION_ATTEMPT_STAGE_RANKS[stage] for stage in stages]
    require(
        ranks == sorted(ranks),
        "Fusion attempt stages are not in runtime reservation order",
    )


def expected_benchmark_config_hash() -> str:
    """Bind retained RI runs to the exact pinned benchmark configuration."""

    return canonical_json_hash(DEFAULT_PLUGIN_CONFIG)


def _benchmark_profile() -> dict[str, Any]:
    profiles = DEFAULT_PLUGIN_CONFIG.get("profiles")
    require(isinstance(profiles, dict), "Pinned plugin configuration has no profiles")
    profile = profiles.get(BENCHMARK_PROFILE_NAME)
    require(isinstance(profile, dict), "Pinned benchmark profile is missing")
    require(
        DEFAULT_PLUGIN_CONFIG.get("active_profile") == BENCHMARK_PROFILE_NAME,
        "Pinned benchmark profile is not active",
    )
    return profile


def _benchmark_seat(seat_name: str) -> dict[str, Any]:
    seats = DEFAULT_PLUGIN_CONFIG.get("seats")
    require(isinstance(seats, dict), "Pinned plugin configuration has no seats")
    seat = seats.get(seat_name)
    require(
        isinstance(seat, dict),
        f"Pinned benchmark seat is missing: {seat_name}",
    )
    return seat


def expected_invocation_payload(
    *,
    run_id: str,
    input_sha256: str,
    config_sha256: str,
    stage: str,
    seat_name: str,
    system: str,
    prompt: str,
    response_schema: Mapping[str, Any] | None,
    schema_name: str,
) -> dict[str, Any]:
    """Reconstruct the runtime's privacy-preserving logical call receipt."""

    return {
        "schema_version": 1,
        "run_id": run_id,
        "input_sha256": input_sha256,
        "config_sha256": config_sha256,
        "stage": stage,
        "seat_name": seat_name,
        "system_sha256": sha256(system.encode("utf-8")).hexdigest(),
        "prompt_sha256": sha256(prompt.encode("utf-8")).hexdigest(),
        "response_schema_sha256": canonical_json_hash(response_schema),
        "schema_name": schema_name,
    }


def _record_expected_invocation(
    expected: dict[tuple[str, str], dict[str, Any]],
    *,
    run_id: str,
    input_sha256: str,
    config_sha256: str,
    stage: str,
    seat_name: str,
    system: str,
    prompt: str,
    response_schema: Mapping[str, Any] | None,
    schema_name: str,
) -> None:
    call_key = (stage, seat_name)
    require(call_key not in expected, f"Duplicate expected RI invocation: {call_key}")
    expected[call_key] = expected_invocation_payload(
        run_id=run_id,
        input_sha256=input_sha256,
        config_sha256=config_sha256,
        stage=stage,
        seat_name=seat_name,
        system=system,
        prompt=prompt,
        response_schema=response_schema,
        schema_name=schema_name,
    )


def expected_fusion_invocations(
    *,
    run_id: str,
    input_sha256: str,
    config_sha256: str,
    arguments: Mapping[str, Any],
    panel_artifact: Mapping[str, Any],
    judge_artifact: Mapping[str, Any],
    synthesis_artifacts: Sequence[Mapping[str, Any]],
    gate_artifacts: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Build every logical call made by the pinned client-orchestrated profile."""

    require(
        bool(synthesis_artifacts) and len(synthesis_artifacts) == len(gate_artifacts),
        "Fusion invocation reconstruction has inconsistent synthesis/gate rounds",
    )
    task = arguments.get("task")
    context = arguments.get("context")
    mechanical_evidence = arguments.get("mechanical_evidence")
    require(
        isinstance(task, str)
        and isinstance(context, str)
        and isinstance(mechanical_evidence, str),
        "Fusion invocation reconstruction lacks exact Codex arguments",
    )
    profile = _benchmark_profile()
    fusion = profile.get("fusion")
    gates = profile.get("gates")
    require(isinstance(fusion, dict), "Pinned fusion configuration is missing")
    require(isinstance(gates, dict), "Pinned gate configuration is missing")
    objective = str(
        profile.get(
            "objective",
            "Deliver the most correct, complete, and executable result.",
        )
    )
    expected: dict[tuple[str, str], dict[str, Any]] = {}

    panel_seat_names = fusion.get("panel")
    require(
        isinstance(panel_seat_names, list)
        and all(isinstance(seat_name, str) for seat_name in panel_seat_names),
        "Pinned fusion panel is invalid",
    )
    for seat_name in panel_seat_names:
        seat = _benchmark_seat(seat_name)
        panel_context = runtime_panel_context_bundle(
            context,
            mechanical_evidence,
            str(seat.get("context_bundle", "full_task_and_evidence")),
            fusion.get("partition_context", True) is True,
        )
        _record_expected_invocation(
            expected,
            run_id=run_id,
            input_sha256=input_sha256,
            config_sha256=config_sha256,
            stage="panel",
            seat_name=seat_name,
            system=runtime_panel_system(
                str(seat.get("role", "domain analyst")),
                str(
                    seat.get(
                        "persona",
                        "Find the most important truth other reviewers may miss.",
                    )
                ),
                objective,
            ),
            prompt=runtime_panel_prompt(task, panel_context),
            response_schema=None,
            schema_name="structured_response",
        )

    panel_results = panel_artifact.get("results")
    require(isinstance(panel_results, list), "Fusion panel results are missing")
    live_reports = [
        report
        for report in panel_results
        if isinstance(report, Mapping) and report.get("status") == "completed"
    ]
    judge_name = str(fusion.get("judge"))
    judge_seat = _benchmark_seat(judge_name)
    judge_schema, required_judgment_fields = runtime_judge_contract(profile)
    _record_expected_invocation(
        expected,
        run_id=run_id,
        input_sha256=input_sha256,
        config_sha256=config_sha256,
        stage="judge",
        seat_name=judge_name,
        system=runtime_judge_system(
            objective,
            str(judge_seat.get("persona", "")),
            str(judge_seat.get("context_bundle", "")),
        ),
        prompt=runtime_judge_prompt(task, live_reports, mechanical_evidence),
        response_schema=judge_schema,
        schema_name="fusion_judgment",
    )

    judgment = judge_artifact.get("judgment")
    require(isinstance(judgment, Mapping), "Fusion judgment is missing")
    require(
        all(field_name in judgment for field_name in required_judgment_fields),
        "Fusion judgment lacks a configured prompt field",
    )
    # RunStore sorts persisted JSON keys, but the runtime originally constructed
    # this mapping in the configured required-field order before prompting.
    ordered_judgment = {
        field_name: judgment[field_name]
        for field_name in required_judgment_fields
    }
    synthesizer_name = str(fusion.get("synthesizer"))
    synthesizer_seat = _benchmark_seat(synthesizer_name)
    synthesis_system = runtime_synthesis_system(
        objective,
        str(synthesizer_seat.get("persona", "")),
        str(synthesizer_seat.get("context_bundle", "")),
    )
    reviewer_names = gates.get("reviewers")
    require(
        isinstance(reviewer_names, list)
        and all(isinstance(reviewer_name, str) for reviewer_name in reviewer_names),
        "Pinned gate reviewer roster is invalid",
    )

    for round_index, (synthesis_artifact, gate_artifact) in enumerate(
        zip(synthesis_artifacts, gate_artifacts)
    ):
        synthesis_stage = "synthesis" if round_index == 0 else f"amendment-{round_index}"
        amendment_feedback = (
            ""
            if round_index == 0
            else RuntimeFusionOrchestrator._gate_feedback(gate_artifacts[round_index - 1])
        )
        _record_expected_invocation(
            expected,
            run_id=run_id,
            input_sha256=input_sha256,
            config_sha256=config_sha256,
            stage=synthesis_stage,
            seat_name=synthesizer_name,
            system=synthesis_system,
            prompt=runtime_synthesis_prompt(
                task,
                context,
                live_reports,
                ordered_judgment,
                mechanical_evidence,
                amendment_feedback,
            ),
            response_schema=None,
            schema_name="structured_response",
        )

        artifact = synthesis_artifact.get("text")
        require(isinstance(artifact, str), f"{synthesis_stage} artifact text is missing")
        gate_reviews = gate_artifact.get("reviewers")
        require(isinstance(gate_reviews, list), "Fusion gate reviewers are missing")
        # A byte-identical independent amendment is rejected before dispatch and
        # therefore legitimately has no gate-N provider invocation.
        if not gate_reviews:
            continue
        gate_stage = "gate" if round_index == 0 else f"gate-{round_index}"
        artifact_hash = sha256(artifact.encode("utf-8")).hexdigest()
        gate_user_prompt = runtime_gate_prompt(
            task,
            artifact,
            artifact_hash,
            mechanical_evidence,
        )
        for reviewer_name in reviewer_names:
            reviewer_seat = _benchmark_seat(reviewer_name)
            _record_expected_invocation(
                expected,
                run_id=run_id,
                input_sha256=input_sha256,
                config_sha256=config_sha256,
                stage=gate_stage,
                seat_name=reviewer_name,
                system=runtime_gate_system(
                    objective,
                    str(reviewer_seat.get("persona", "")),
                    str(reviewer_seat.get("context_bundle", "")),
                ),
                prompt=gate_user_prompt,
                response_schema=RUNTIME_VERDICT_SCHEMA,
                schema_name="adversarial_verdict",
            )
    return expected


def expected_lifecycle_invocations(
    *,
    run_id: str,
    input_sha256: str,
    config_sha256: str,
    arguments: Mapping[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Build the two exact reviewer calls for one host-owned lifecycle gate."""

    task = arguments.get("task")
    artifact = arguments.get("artifact")
    mechanical_evidence = arguments.get("mechanical_evidence")
    require(
        isinstance(task, str)
        and isinstance(artifact, str)
        and isinstance(mechanical_evidence, str),
        "Lifecycle invocation reconstruction lacks exact Codex arguments",
    )
    profile = _benchmark_profile()
    gates = profile.get("gates")
    require(isinstance(gates, dict), "Pinned gate configuration is missing")
    reviewer_names = gates.get("reviewers")
    require(
        isinstance(reviewer_names, list)
        and all(isinstance(reviewer_name, str) for reviewer_name in reviewer_names),
        "Pinned gate reviewer roster is invalid",
    )
    objective = str(
        profile.get(
            "objective",
            "Deliver the most correct, complete, and executable result.",
        )
    )
    artifact_hash = sha256(artifact.encode("utf-8")).hexdigest()
    prompt = runtime_gate_prompt(task, artifact, artifact_hash, mechanical_evidence)
    expected: dict[tuple[str, str], dict[str, Any]] = {}
    for reviewer_name in reviewer_names:
        reviewer_seat = _benchmark_seat(reviewer_name)
        _record_expected_invocation(
            expected,
            run_id=run_id,
            input_sha256=input_sha256,
            config_sha256=config_sha256,
            stage="gate",
            seat_name=reviewer_name,
            system=runtime_gate_system(
                objective,
                str(reviewer_seat.get("persona", "")),
                str(reviewer_seat.get("context_bundle", "")),
            ),
            prompt=prompt,
            response_schema=RUNTIME_VERDICT_SCHEMA,
            schema_name="adversarial_verdict",
        )
    return expected


def validate_expected_invocations(
    observed: Mapping[tuple[str, str], Mapping[str, Any]],
    expected: Mapping[tuple[str, str], Mapping[str, Any]],
    *,
    label: str,
) -> None:
    require(
        set(observed) == set(expected),
        f"{label} invocation roster differs from the reconstructed runtime calls",
    )
    for call_key in sorted(expected):
        observed_invocation = observed[call_key]
        expected_invocation = expected[call_key]
        mismatched_fields = sorted(
            field_name
            for field_name in set(observed_invocation) | set(expected_invocation)
            if observed_invocation.get(field_name) != expected_invocation.get(field_name)
        )
        require(
            not mismatched_fields,
            f"{label} invocation {call_key[0]}/{call_key[1]} differs in "
            + ", ".join(mismatched_fields),
        )


def _contains_substantive_claim(text: str) -> bool:
    for line in text.splitlines():
        candidate = re.sub(
            r"^\s*(?:#{1,6}\s*|[-*+]\s+|\d+[.)]\s+)", "", line
        ).strip()
        words = re.findall(r"[A-Za-z0-9][A-Za-z0-9_'’-]*", candidate)
        if len(words) >= 6:
            return True
    return False


def validate_fusion_quality(text: Any, label: str) -> str:
    """Apply the exact pinned runtime quality floor to retained fusion output."""

    require(isinstance(text, str), f"{label} is not text")
    fusion = _benchmark_profile().get("fusion")
    require(isinstance(fusion, dict), "Pinned fusion configuration is missing")
    quality_floor = fusion.get("quality_floor")
    require(isinstance(quality_floor, dict), "Pinned fusion quality floor is missing")
    minimum_characters = quality_floor.get("minimum_characters")
    require(
        isinstance(minimum_characters, int)
        and not isinstance(minimum_characters, bool)
        and minimum_characters >= 1,
        "Pinned fusion minimum-character quality floor is invalid",
    )
    for flag_name in (
        "require_nonempty_claims",
        "reject_tool_markup",
        "reject_refusal_without_policy_reason",
    ):
        require(
            isinstance(quality_floor.get(flag_name), bool),
            f"Pinned fusion quality-floor flag is invalid: {flag_name}",
        )
    require(
        len(text.strip()) >= minimum_characters,
        f"{label} is below the pinned fusion quality floor",
    )
    if quality_floor["require_nonempty_claims"]:
        require(_contains_substantive_claim(text), f"{label} has no substantive claim")
    lowered = text.lower()
    if quality_floor["reject_tool_markup"]:
        require(
            "<tool_call>" not in lowered and "<function_call>" not in lowered,
            f"{label} leaks tool-call markup",
        )
    if quality_floor["reject_refusal_without_policy_reason"]:
        refusal_prefixes = (
            "i can't assist",
            "i cannot assist",
            "i'm unable to help",
            "i am unable to help",
            "sorry, but i can't",
        )
        require(
            not lowered.strip().startswith(refusal_prefixes) or "policy" in lowered,
            f"{label} is an ungrounded refusal",
        )
    return text


def _expected_execution_contract() -> dict[str, Any]:
    profile = _benchmark_profile()
    execution = profile.get("execution")
    native_codex = DEFAULT_PLUGIN_CONFIG.get("native_codex")
    require(isinstance(execution, dict), "Pinned benchmark execution policy is missing")
    require(isinstance(native_codex, dict), "Pinned native Codex policy is missing")
    contract = {
        field_name: execution[field_name]
        for field_name in EXECUTION_CONTRACT_FIELDS
        if field_name in execution
    }
    contract["native_codex"] = native_codex
    return json.loads(json.dumps(contract, ensure_ascii=False, allow_nan=False))


def _expected_required_checks(
    execution: dict[str, Any], gates: dict[str, Any]
) -> dict[str, Any]:
    stages = gates.get("stages")
    require(isinstance(stages, dict), "Pinned lifecycle gate policy is missing")
    lifecycle: dict[str, Any] = {}
    if gates.get("enabled") is True:
        for stage_name in LIFECYCLE_STAGES:
            stage = stages.get(stage_name)
            require(isinstance(stage, dict), f"Pinned {stage_name} gate policy is missing")
            if stage.get("enabled") is True:
                lifecycle[stage_name] = {
                    "owner": "codex_host",
                    "required_evidence": list(stage.get("required_evidence", [])),
                    "tool_policy": stage.get("tool_policy", "none"),
                    "timeout_seconds": stage.get("timeout_seconds"),
                }
    for stage_name, required in (
        ("pre_execution", execution.get("require_pre_execution_gate")),
        ("post_execution", execution.get("require_post_execution_gate")),
    ):
        if required is True and stage_name not in lifecycle:
            lifecycle[stage_name] = {
                "owner": "codex_host",
                "required_evidence": [],
                "tool_policy": "none",
                "timeout_seconds": None,
            }
    return {
        "lifecycle_gates": lifecycle,
        "run_tests": execution.get("run_tests") is True,
        "require_diff_review": execution.get("require_diff_review") is True,
        "completion_requires": list(execution.get("completion_requires", [])),
    }


def _expected_remaining_budget(
    budgets: dict[str, Any], ledger: dict[str, Any]
) -> dict[str, Any]:
    input_tokens = int(ledger.get("input_tokens", 0))
    output_tokens = int(ledger.get("output_tokens", 0))
    reasoning_tokens = int(ledger.get("reasoning_tokens", 0))
    counters = {
        "calls": ("max_calls", int(ledger.get("calls", 0))),
        "total_tokens": ("max_total_tokens", input_tokens + output_tokens),
        "input_tokens": ("max_input_tokens", input_tokens),
        "output_tokens": ("max_output_tokens", output_tokens),
        "reasoning_tokens": ("max_reasoning_tokens", reasoning_tokens),
        "tool_calls": ("max_tool_calls", int(ledger.get("tool_calls", 0))),
        "wall_seconds": ("max_wall_seconds", float(ledger.get("wall_seconds", 0.0))),
        "known_cost_usd": (
            "max_cost_usd",
            float(ledger.get("known_cost_usd", 0.0)),
        ),
    }
    remaining: dict[str, Any] = {}
    for counter_name, (limit_name, consumed) in counters.items():
        limit = budgets.get(limit_name)
        require(
            isinstance(limit, (int, float)) and not isinstance(limit, bool),
            f"Pinned budget limit is invalid: {limit_name}",
        )
        remaining[counter_name] = {
            "limit": limit,
            "consumed": consumed,
            "remaining": max(0, limit - consumed),
        }
    remaining["unknown_cost_calls"] = int(ledger.get("unknown_cost_calls", 0))
    remaining["warnings"] = list(ledger.get("warnings", []))
    provider_cost = ledger.get("provider_cost_usd", {})
    provider_limits = budgets.get("per_provider_max_cost_usd")
    require(isinstance(provider_cost, dict), "RI provider cost ledger is invalid")
    require(isinstance(provider_limits, dict), "Pinned provider budget policy is missing")
    remaining["provider_cost_usd"] = {
        str(provider_name): {
            "limit": provider_limit,
            "consumed": float(provider_cost.get(provider_name, 0.0)),
            "remaining": max(
                0.0,
                float(provider_limit) - float(provider_cost.get(provider_name, 0.0)),
            ),
        }
        for provider_name, provider_limit in provider_limits.items()
        if isinstance(provider_limit, (int, float)) and not isinstance(provider_limit, bool)
    }
    return remaining


def _expected_handoff_instruction(
    artifacts: dict[str, Any], pending_gates: list[str], later_gates: list[str]
) -> str:
    authorization = (
        "Do not mutate files or external state yet. The active Codex host must "
        "independently invoke "
        + ", ".join(pending_gates)
        + " lifecycle gate(s) with their required evidence and retain their "
        "same-artifact receipts first."
    )
    completion = (
        " After implementation, the active Codex host must invoke the configured "
        + ", ".join(later_gates)
        + " gate(s) over the exact resulting artifact and evidence."
    )
    return (
        "This is a persisted Codex host-workflow packet, not independent permission "
        "to act. Re-inspect the current workspace, preserve user changes, honor newer "
        "user instructions and the live sandbox/approval policy, and stop if repository "
        "reality contradicts the packet. "
        + authorization
        + completion
        + "\n\nSelected handoff artifacts:\n"
        + json.dumps(artifacts, indent=2, sort_keys=True, ensure_ascii=False)
    )


def expected_fusion_handoff(
    *,
    run_id: str,
    synthesis: str,
    judgment: dict[str, Any],
    ledger: dict[str, Any],
    artifact_sha256: str,
) -> dict[str, Any]:
    """Reconstruct the only acceptable maximum-intelligence host handoff."""

    profile = _benchmark_profile()
    execution = profile.get("execution")
    gates = profile.get("gates")
    budgets = profile.get("budgets")
    require(isinstance(execution, dict), "Pinned benchmark execution policy is missing")
    require(isinstance(gates, dict), "Pinned benchmark gate policy is missing")
    require(isinstance(budgets, dict), "Pinned benchmark budget policy is missing")
    requested_sections = execution.get("handoff_include")
    require(
        requested_sections == list(SUPPORTED_HANDOFF_SECTIONS),
        "Pinned benchmark handoff section policy is invalid",
    )
    constraints = {
        field_name: execution[field_name]
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
    }
    artifacts = {
        "fused_plan": synthesis,
        "constraints": constraints,
        "minority_findings": list(judgment.get("minority_findings", [])),
        "blind_spots": list(judgment.get("blind_spots", [])),
        "required_checks": _expected_required_checks(execution, gates),
        "budget_remaining": _expected_remaining_budget(budgets, ledger),
    }
    pending_gates = ["plan", "pre_execution"]
    later_gates = ["post_execution", "final", "summarize"]
    execution_contract = _expected_execution_contract()
    execution_contract_sha256 = canonical_json_hash(execution_contract)
    handoff = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "backend": "active_codex",
        "status": "awaiting_host_gates",
        "ready_for_host_workflow": True,
        "ready": False,
        "mutation_authorized": False,
        "requires_explicit_confirmation": False,
        "run_id": run_id,
        "selected_profile": BENCHMARK_PROFILE_NAME,
        "execution_contract": execution_contract,
        "execution_contract_sha256": execution_contract_sha256,
        "handoff_contract_sha256": canonical_json_hash(
            {
                "selected_profile": BENCHMARK_PROFILE_NAME,
                "execution_contract": execution_contract,
            }
        ),
        "synthesis_gate": {
            "owner": "mcp_runtime",
            "passed": True,
            "artifact_sha256": artifact_sha256,
        },
        "lifecycle": {
            "stage_owner": "codex_host",
            "pending_gates": pending_gates,
            "later_gates": later_gates,
            "host_receipts_required": True,
        },
        "blocking_reasons": [],
        "included_sections": list(SUPPORTED_HANDOFF_SECTIONS),
        "artifacts": artifacts,
        "instruction": _expected_handoff_instruction(
            artifacts, pending_gates, later_gates
        ),
    }
    handoff[HANDOFF_PAYLOAD_HASH_FIELD] = canonical_json_hash(handoff)
    return handoff


def call_receipt_entry_id(
    attempt_id: str,
    invocation_sha256: str,
    response_sha256: str,
) -> str:
    return canonical_json_hash(
        {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "invocation_sha256": invocation_sha256,
            "response_sha256": response_sha256,
        }
    )


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"Cannot read JSON evidence {path}: {exc}") from exc


def resolve_evidence_path(root: Path, relative: str) -> Path:
    require(isinstance(relative, str) and relative, "Evidence path must be nonempty")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise EvidenceError(f"Evidence path escapes attempt directory: {relative}") from exc
    require(candidate.is_file(), f"Missing evidence file: {relative}")
    return candidate


def walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for nested in value.values():
            yield from walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from walk(nested)


def reject_secrets(values: Iterable[Any]) -> None:
    for value in values:
        serialized = json.dumps(value, sort_keys=True)
        for pattern in SECRET_PATTERNS:
            require(pattern.search(serialized) is None, "Secret-like value found in evidence")
        for node in walk(value):
            if not isinstance(node, dict):
                continue
            for key, nested in node.items():
                normalized = str(key).lower().replace("-", "_")
                if normalized not in {"api_key", "xai_api_key", "authorization", "token"}:
                    continue
                if nested is None:
                    continue
                text = str(nested)
                require(
                    text in SAFE_SECRET_VALUES or text.startswith("${"),
                    f"Unredacted secret field found: {key}",
                )


def reject_secrets_in_tree(root: Path) -> None:
    """Scan retained raw responses and logs, not only indexed JSON summaries."""
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise EvidenceError(f"Symlink is not allowed in retained evidence: {path}")
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            raise EvidenceError(f"Cannot scan retained evidence {path}: {exc}") from exc
        for pattern in SECRET_PATTERNS:
            require(
                pattern.search(text) is None,
                f"Secret-like value found in retained evidence: {path}",
            )


def validate_contract(contract: dict[str, Any], index: dict[str, Any]) -> None:
    harness = index["harness"]
    task = index["task"]
    expected_harness = "harbor" if task in PINS["harbor"]["tasks"] else "pier"
    require(harness == expected_harness, "Task is assigned to the wrong harness")
    contract_schema = contract.get("schema_version")
    require(
        isinstance(contract_schema, int)
        and not isinstance(contract_schema, bool)
        and contract_schema == 1,
        "Unsupported run contract",
    )
    require(contract.get("harness") == harness, "Contract harness mismatch")
    require(contract.get("task") == task, "Contract task mismatch")
    require(contract.get("attempt") == index["attempt"], "Contract attempt mismatch")

    expected = PINS[harness]
    task_expected = expected["tasks"][task]
    observed = contract.get("pins", {})
    require(observed.get("harness_version") == expected["version"], "Harness version drift")
    require(observed.get("harness_commit") == expected.get("commit"), "Harness commit drift")
    require(
        observed.get("dataset_source_commit") == expected["dataset"]["source_commit"],
        "Dataset source commit drift",
    )
    require(observed.get("image") == task_expected["image"], "Task image drift")
    require(observed.get("image_digest") == task_expected["image_digest"], "Image pin drift")
    require(
        observed.get("observed_image_digest") == task_expected["image_digest"],
        "Observed image digest drift",
    )
    require(observed.get("base_commit") == task_expected.get("base_commit"), "Base commit drift")
    require(observed.get("codex_version") == PINS["codex"]["version"], "Codex version drift")
    require(observed.get("model") == PINS["codex"]["model"], "Model drift")
    require(
        observed.get("reasoning_effort") == PINS["codex"]["reasoning_effort"],
        "Reasoning effort drift",
    )
    require(
        observed.get("agent_timeout_seconds")
        == PINS["codex"]["agent_timeout_seconds"],
        "Agent timeout drift",
    )
    require(
        observed.get("mcp_startup_timeout_seconds")
        == PINS["codex"]["mcp_startup_timeout_seconds"],
        "MCP startup timeout drift",
    )
    require(
        observed.get("mcp_tool_timeout_seconds")
        == PINS["codex"]["mcp_tool_timeout_seconds"],
        "MCP tool timeout drift",
    )
    require(
        observed.get("ri_data_directory") == "/logs/agent/relentless-inception",
        "RI retained-data directory drift",
    )
    require(
        observed.get("artifact_hashes") == PINS.get("artifacts"),
        "Plugin or benchmark artifact hash drift",
    )

    command = contract.get("command")
    require(isinstance(command, list) and all(isinstance(x, str) for x in command), "Bad command receipt")
    joined = " ".join(command)
    flag_values = {
        flag: [command[index + 1] for index, value in enumerate(command[:-1]) if value == flag]
        for flag in ("--n-attempts", "--max-retries", "--n-concurrent")
    }
    require(flag_values["--n-attempts"] == ["1"], "Each harness invocation must use n=1")
    require(flag_values["--max-retries"] == ["0"], "Retries must be disabled")
    require(flag_values["--n-concurrent"] == ["1"], "Benchmark attempts must be serialized")
    mount_flags = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value in {"--mounts", "--mounts-json"}
    ]
    require(len(mount_flags) == 1, "Expected one explicit mount contract")
    try:
        mounts = json.loads(mount_flags[0])
    except json.JSONDecodeError as exc:
        raise EvidenceError("Invalid mount receipt") from exc
    expected_mount_count = 6 if harness == "pier" else 3
    require(
        isinstance(mounts, list) and len(mounts) == expected_mount_count,
        "Unexpected mount count",
    )
    mount_by_target = {mount.get("target"): mount for mount in mounts if isinstance(mount, dict)}
    require(len(mount_by_target) == len(mounts), "Duplicate or invalid mount target")
    if harness == "pier":
        expected_log_sources = {
            "/logs/verifier": "${HOST_VERIFIER_LOGS_PATH}",
            "/logs/agent": "${HOST_AGENT_LOGS_PATH}",
            "/logs/artifacts": "${HOST_ARTIFACTS_PATH}",
        }
        for target, source in expected_log_sources.items():
            log_mount = mount_by_target.get(target)
            require(isinstance(log_mount, dict), f"Missing Pier log mount: {target}")
            require(log_mount.get("type") == "bind", f"Pier log mount must be a bind: {target}")
            require(log_mount.get("source") == source, f"Bad Pier log mount source: {target}")
            require("read_only" not in log_mount, f"Pier log mount must be writable: {target}")
    plugin_mount = mount_by_target.get("/opt/relentless-inception")
    require(isinstance(plugin_mount, dict), "Missing plugin source mount")
    require(plugin_mount.get("type") == "bind", "Plugin source mount must be a bind")
    require(plugin_mount.get("read_only") is True, "Plugin source mount must be read-only")
    require(
        str(plugin_mount.get("source", "")).endswith("/plugins/relentless-inception"),
        "Bad plugin source mount",
    )
    support_mount = mount_by_target.get("/opt/relentless-inception-bench")
    require(isinstance(support_mount, dict), "Missing benchmark support mount")
    require(support_mount.get("type") == "bind", "Benchmark support mount must be a bind")
    require(support_mount.get("read_only") is True, "Benchmark support mount must be read-only")
    require(
        str(support_mount.get("source", "")).endswith("/bench/support"),
        "Bad benchmark support mount",
    )
    secret_mount = mount_by_target.get("/run/secrets/relentless-inception-xai")
    require(isinstance(secret_mount, dict), "Missing ephemeral xAI secret mount")
    require(secret_mount.get("type") == "bind", "xAI secret mount must be a bind")
    require(secret_mount.get("read_only") is True, "xAI secret mount must be read-only")
    secret_source = Path(str(secret_mount.get("source", "")))
    require(secret_source.name == "xai-api-key", "Bad ephemeral xAI secret filename")
    require(
        secret_source.parent.name.startswith("ri-bench-secret-"),
        "xAI secret must come from an ephemeral benchmark directory",
    )
    forbidden = ("task-cache", "task_cache", "oracle", "held-test", "held_test", "/solution")
    require(not any(token in joined.lower() for token in forbidden), "Forbidden run input or mount")


def validate_result(result: dict[str, Any], expected_task: str, harness: str) -> None:
    expected_result_task = PINS[harness]["tasks"][expected_task].get("task_name")
    require(
        isinstance(expected_result_task, str) and bool(expected_result_task),
        "Pinned harness task identity is missing",
    )
    require(
        result.get("task_name") == expected_result_task,
        "Harness result task identity mismatch",
    )
    require(
        isinstance(result.get("trial_name"), str) and result["trial_name"].startswith(f"{expected_task}__"),
        "Harness result trial identity mismatch",
    )
    require(isinstance(result.get("id"), str) and bool(result["id"]), "Harness result id is missing")
    for timestamp_name in ("started_at", "finished_at"):
        timestamp = result.get(timestamp_name)
        require(isinstance(timestamp, str) and bool(timestamp), f"Harness {timestamp_name} is missing")
        try:
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EvidenceError(f"Harness {timestamp_name} is invalid") from exc
    require(
        datetime.fromisoformat(result["started_at"].replace("Z", "+00:00"))
        < datetime.fromisoformat(result["finished_at"].replace("Z", "+00:00")),
        "Harness result timestamps are not chronological",
    )
    require(result.get("exception_info") is None, "Harness recorded an exception")
    verifier = result.get("verifier_result")
    require(isinstance(verifier, dict), "Missing verifier result")
    rewards = verifier.get("rewards")
    require(isinstance(rewards, dict) and rewards, "Missing reward evidence")
    reward = rewards.get("reward")
    if reward is None and len(rewards) == 1:
        reward = next(iter(rewards.values()))
    require(
        not isinstance(reward, bool)
        and isinstance(reward, (int, float))
        and reward == 1,
        "Attempt reward is not numeric 1",
    )
    agent_info = result.get("agent_info")
    require(isinstance(agent_info, dict), "Missing agent provenance")
    require(agent_info.get("version") == PINS["codex"]["version"], "Executed Codex version drift")
    model_info = agent_info.get("model_info")
    require(isinstance(model_info, dict), "Missing model provenance")
    require(model_info.get("provider") == "openai", "Outer model provider is not OpenAI")
    require(model_info.get("name") == "gpt-5.6-sol", "Outer actual model is not gpt-5.6-sol")


def trajectory_steps(trajectory: Any) -> list[dict[str, Any]]:
    """Return retained ATIF steps without inferring nested call semantics."""
    require(isinstance(trajectory, dict), "Trajectory must be a JSON object")
    require(trajectory.get("schema_version") == "ATIF-v1.7", "Unsupported trajectory schema")
    steps = trajectory.get("steps")
    require(isinstance(steps, list) and steps, "Trajectory has no steps")
    require(all(isinstance(step, dict) for step in steps), "Trajectory contains a bad step")
    return steps


def step_tool_calls(step: dict[str, Any]) -> list[dict[str, Any]]:
    tool_calls = step.get("tool_calls")
    if isinstance(tool_calls, list):
        return [call for call in tool_calls if isinstance(call, dict)]
    if any(key in step for key in ("function_name", "name", "tool_name")):
        return [step]
    return []


def step_payload(step: dict[str, Any]) -> str:
    return json.dumps(step_tool_calls(step), sort_keys=True)


def trajectory_original_request(trajectory: Any) -> str:
    sentinels = (
        "\n\nTreat the benchmark instruction as the exact task scope.",
        "\n\nTreat the instruction above as the exact task scope.",
    )
    matches: list[str] = []
    for step in trajectory_steps(trajectory):
        if step.get("source") != "user" or not isinstance(step.get("message"), str):
            continue
        message = step["message"]
        for sentinel in sentinels:
            if sentinel in message:
                matches.append(message.split(sentinel, 1)[0].strip())
                break
    require(len(matches) == 1 and bool(matches[0]), "ATIF benchmark instruction is ambiguous")
    return matches[0]


def has_nested_exec_command(step: dict[str, Any]) -> bool:
    for call in step_tool_calls(step):
        name = call.get("function_name") or call.get("name") or call.get("tool_name")
        arguments = call.get("arguments")
        source = arguments.get("input") if isinstance(arguments, dict) else None
        if str(name).lower() == "exec" and isinstance(source, str) and "tools.exec_command" in source:
            return True
    return False


def nested_exec_source(step: dict[str, Any]) -> str:
    sources = []
    for call in step_tool_calls(step):
        name = call.get("function_name") or call.get("name") or call.get("tool_name")
        arguments = call.get("arguments")
        source = arguments.get("input") if isinstance(arguments, dict) else None
        if str(name).lower() == "exec" and isinstance(source, str) and "tools.exec_command" in source:
            sources.append(source)
    require(len(sources) == 1, "ATIF host-shell wrapper source is ambiguous")
    return sources[0]


def command_is_bound_to_wrapper(command: Any, source: str) -> bool:
    if not isinstance(command, str) or not command:
        return False
    try:
        retained_script = wrapper_shell_script(source)
    except EvidenceError:
        return False
    if command == retained_script:
        return True
    try:
        command_parts = shlex.split(command)
    except ValueError:
        return False
    if (
        len(command_parts) != 3
        or command_parts[0] not in {"bash", "/bin/bash", "zsh", "/bin/zsh"}
        or command_parts[1] not in {"-c", "-lc"}
    ):
        return False
    return retained_script == command_parts[2]


def wrapper_shell_script(source: str) -> str:
    require(
        source.count("tools.exec_command(") == 1,
        "ATIF wrapper must contain exactly one host-shell dispatch",
    )
    json_string_matches = re.findall(
        r'\bcmd\s*:\s*("(?:\\.|[^"\\])*")',
        source,
    )
    template_matches = re.findall(r"\bcmd\s*:\s*`([\s\S]*?)`\s*,", source)
    require(
        len(json_string_matches) + len(template_matches) == 1,
        "ATIF wrapper must expose exactly one shell command property",
    )
    if json_string_matches:
        try:
            decoded = json.loads(json_string_matches[0])
        except json.JSONDecodeError as exc:
            raise EvidenceError("ATIF wrapper command string is not JSON") from exc
        require(isinstance(decoded, str) and decoded, "ATIF wrapper command is empty")
        return decoded
    return template_matches[0].replace("\\\\", "\\")


def normalized_shell_tokens(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError as exc:
        raise EvidenceError("Transcript command is not valid shell syntax") from exc


def recorded_wrapper_commands(source: str) -> list[str]:
    script = wrapper_shell_script(source)
    matched_helper = next(
        (helper for helper in RECORD_HELPERS if script == helper or script.startswith(helper + "\n")),
        None,
    )
    require(matched_helper is not None, "ATIF wrapper does not use the canonical run_record helper")
    remainder = script[len(matched_helper) :].lstrip("\n")
    commands: list[str] = []
    for line in remainder.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("run_record "):
            try:
                lexer = shlex.shlex(
                    stripped,
                    posix=True,
                    punctuation_chars=";&|<>",
                )
                lexer.whitespace_split = True
                lexer.commenters = ""
                parts = list(lexer)
            except ValueError as exc:
                raise EvidenceError("run_record invocation is not valid shell syntax") from exc
            require(
                len(parts) == 2
                or (len(parts) == 5 and parts[2:] == ["||", "exit", "$?"]),
                "run_record invocation contains unrecorded shell tokens",
            )
            commands.append(parts[1])
            continue
        substitution_match = re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*=\$\((.*)\)",
            stripped,
        )
        if substitution_match is not None:
            commands.append(substitution_match.group(1))
            continue
        require(
            "$(" not in stripped and "`" not in stripped,
            "ATIF wrapper control line contains hidden command substitution",
        )
        try:
            control_lexer = shlex.shlex(
                stripped,
                posix=True,
                punctuation_chars=";&|<>",
            )
            control_lexer.whitespace_split = True
            control_lexer.commenters = ""
            control_tokens = list(control_lexer)
        except ValueError as exc:
            raise EvidenceError("ATIF wrapper control line is not valid shell syntax") from exc
        variable_reference = re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*$")
        allowed_control_line = bool(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\$\?", stripped)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=0", stripped)
            or control_tokens in (["else"], ["fi"])
            or (
                len(control_tokens) == 8
                and control_tokens[0] in {"if", "elif"}
                and control_tokens[1] == "["
                and variable_reference.fullmatch(control_tokens[2])
                and control_tokens[3] == "-eq"
                and re.fullmatch(r"-?\d+", control_tokens[4])
                and control_tokens[5:] == ["]", ";", "then"]
            )
            or (
                control_tokens[0:1] == ["printf"]
                and (
                    (
                        len(control_tokens) == 2
                        and (
                            (
                                control_tokens[1].startswith("$ ")
                                and control_tokens[1].endswith("\\n")
                            )
                            or control_tokens[1] == "no matches (expected)\\n"
                        )
                    )
                    or (
                        len(control_tokens) == 3
                        and control_tokens[1]
                        in {
                            "%s\\n",
                            "[exit %d]\\n",
                            "discovery failed with status %d\\n",
                        }
                        and variable_reference.fullmatch(control_tokens[2])
                    )
                )
            )
            or (
                len(control_tokens) == 2
                and control_tokens[0] == "exit"
                and variable_reference.fullmatch(control_tokens[1])
            )
        )
        require(allowed_control_line, "ATIF wrapper contains an unrecorded executable statement")
    require(commands, "ATIF wrapper contains no auditable command records")
    return commands


def validate_wrapper_records(
    source: str,
    transcript: str,
    *,
    wrapper_exit_code: int,
) -> list[str]:
    transcript_commands, transcript_statuses = exit_markers(transcript)
    executed_commands = recorded_wrapper_commands(source)
    if wrapper_exit_code == 0:
        require(
            len(executed_commands) == len(transcript_commands),
            "ATIF wrapper command count differs from its transcript records",
        )
    else:
        require(
            1 <= len(transcript_commands) <= len(executed_commands)
            and int(transcript_statuses[-1]) == wrapper_exit_code,
            "Failed ATIF wrapper transcript is not an ordered failing prefix",
        )
    for executed_command, transcript_command in zip(executed_commands, transcript_commands):
        require(
            normalized_shell_tokens(executed_command)
            == normalized_shell_tokens(transcript_command[2:]),
            "ATIF wrapper executed a command different from its transcript marker",
        )
    return transcript_commands


def validate_preflight_record(command_marker: str) -> None:
    require(command_marker.startswith("$ "), "Preflight command marker is invalid")
    raw_command = command_marker[2:]
    require(
        "$(" not in raw_command and "`" not in raw_command,
        "Preflight command contains hidden command substitution",
    )
    try:
        lexer = shlex.shlex(
            raw_command,
            posix=True,
            punctuation_chars=";&|<>",
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError as exc:
        raise EvidenceError("Preflight command marker is not valid shell syntax") from exc
    forbidden_path_components = re.compile(
        r"(?:^|/)(?:solutions?|verifier|evaluator|oracle|task[-_]?cache|"
        r"held[-_]?tests?|hidden[-_]?tests?)(?:/|$)",
        re.IGNORECASE,
    )
    for token in tokens:
        require(
            not token.startswith("/")
            and re.search(r"(?:^|/)\.\.(?:/|$)", token) is None
            and forbidden_path_components.search(token) is None
            and "/run/secrets" not in token.lower()
            and "/logs/verifier" not in token.lower()
            and "/logs/artifacts" not in token.lower(),
            "Preflight command may read outside the task workspace or access oracle data",
        )
    segments: list[list[str]] = [[]]
    for token in tokens:
        require(
            not token or not set(token).issubset({"<", ">"}),
            "Preflight command contains shell redirection",
        )
        if token and set(token).issubset({";", "&", "|"}):
            if segments[-1]:
                segments.append([])
            continue
        segments[-1].append(token)
    for segment in (segment for segment in segments if segment):
        require(
            not segment
            or not ("=" in segment[0] and not segment[0].startswith(("=", "-"))),
            "Preflight command contains an environment assignment",
        )
        executable = segment[0].lower()
        require(
            segment[0] == executable and "/" not in segment[0] and "\\" not in segment[0],
            "Preflight executable must be a bare lowercase allowlisted command",
        )
        arguments = segment[1:]
        lowered_arguments = [argument.lower() for argument in arguments]
        simple_readers = {
            "pwd",
            "grep",
            "cat",
            "head",
            "tail",
            "cut",
            "wc",
            "ls",
            "stat",
            "readlink",
            "realpath",
            "jq",
            "which",
            "test",
            "[",
            "cd",
        }
        if executable in simple_readers:
            continue
        if executable == "command":
            require(
                len(arguments) == 2
                and arguments[0] == "-v"
                and re.fullmatch(r"[a-z0-9][a-z0-9._+-]*", arguments[1]) is not None,
                "Preflight command probe must be exactly command -v <bare-tool>",
            )
            continue
        if executable == "sed":
            require(
                len(arguments) >= 2
                and arguments[0] == "-n"
                and re.fullmatch(r"\d+(?:,\d+)?p", arguments[1]) is not None
                and all(not argument.startswith("-") for argument in arguments[2:]),
                "Preflight sed must be a numeric print-only range",
            )
            continue
        if executable == "rg":
            rg_no_value_options = {
                "--files",
                "--hidden",
                "--no-ignore",
                "--no-ignore-vcs",
                "--follow",
                "--smart-case",
                "--ignore-case",
                "--fixed-strings",
                "--line-number",
                "--files-with-matches",
                "--count",
                "--count-matches",
                "--json",
                "--no-heading",
                "--heading",
                "--type-list",
                "--stats",
                "--pcre2",
                "--multiline",
                "--no-messages",
            }
            rg_value_options = {
                "-g",
                "--glob",
                "-e",
                "--regexp",
                "-t",
                "--type",
                "-m",
                "--max-count",
                "-a",
                "--after-context",
                "-b",
                "--before-context",
                "-c",
                "--context",
                "--color",
                "--sort",
                "--sortr",
                "--max-depth",
                "--path-separator",
                "--threads",
                "-j",
            }
            option_index = 0
            positional_mode = False
            while option_index < len(arguments):
                argument = arguments[option_index]
                lowered_argument = argument.lower()
                if positional_mode:
                    require(not argument.startswith("-"), "Preflight rg path requires --")
                    option_index += 1
                    continue
                if argument == "--":
                    positional_mode = True
                    option_index += 1
                    continue
                if lowered_argument in rg_no_value_options or re.fullmatch(
                    r"-[nlifs]+", lowered_argument
                ):
                    option_index += 1
                    continue
                if lowered_argument in rg_value_options:
                    require(option_index + 1 < len(arguments), "Preflight rg option lacks a value")
                    option_index += 2
                    continue
                if any(
                    lowered_argument.startswith(option + "=")
                    for option in rg_value_options
                    if option.startswith("--")
                ):
                    option_index += 1
                    continue
                require(not argument.startswith("-"), "Preflight rg option is not approved")
                positional_mode = True
                option_index += 1
            continue
        require(executable == "git", f"Preflight executable is not approved: {executable}")
        require(arguments, "Preflight Git command lacks a subcommand")
        git_subcommand = lowered_arguments[0]
        git_arguments = lowered_arguments[1:]
        require(
            git_subcommand
            in {
                "rev-parse",
                "status",
                "branch",
                "log",
                "reflog",
                "stash",
                "diff",
                "show",
                "cat-file",
                "merge-base",
                "ls-files",
                "ls-tree",
                "for-each-ref",
                "name-rev",
                "describe",
                "grep",
                "remote",
                "config",
                "tag",
                "rev-list",
                "diff-index",
                "diff-tree",
                "diff-files",
                "worktree",
                "submodule",
            },
            f"Preflight Git subcommand is not approved: {git_subcommand}",
        )
        dangerous_git_options = {
            "--ext-diff",
            "--textconv",
            "--paginate",
            "--filters",
            "--use-mailmap",
        }
        require(
            not any(
                argument in dangerous_git_options
                or argument in {"--output", "-o"}
                or argument.startswith("--output=")
                or argument.startswith("--open-files-in-pager=")
                for argument in git_arguments
            ),
            "Preflight Git command may execute a helper or write output",
        )
        if git_subcommand == "branch":
            safe_branch_options = {
                "--all",
                "-a",
                "--remotes",
                "-r",
                "--list",
                "-l",
                "--verbose",
                "-v",
                "-vv",
                "--no-abbrev",
                "--contains",
                "--no-contains",
                "--merged",
                "--no-merged",
                "--sort",
                "--format",
                "--column",
                "--color",
                "--no-color",
                "--points-at",
                "--show-current",
            }
            require(
                not git_arguments
                or git_arguments == ["--show-current"]
                or (
                    any(
                        argument in {"--all", "-a", "--remotes", "-r", "--list", "-l"}
                        for argument in git_arguments
                    )
                    and all(
                        not argument.startswith("-")
                        or argument.split("=", 1)[0] in safe_branch_options
                        for argument in git_arguments
                    )
                ),
                "Preflight Git branch command is not list-only",
            )
        elif git_subcommand == "reflog":
            require(
                not git_arguments
                or git_arguments[0] == "show"
                or all(argument.startswith("-") for argument in git_arguments),
                "Preflight Git reflog command is not list-only",
            )
        elif git_subcommand == "stash":
            require(git_arguments[0:1] == ["list"], "Preflight Git stash command is not list-only")
        elif git_subcommand == "cat-file":
            require(
                len(git_arguments) == 2
                and git_arguments[0] in {"-e", "-t", "-s", "-p"},
                "Preflight Git cat-file mode is not approved",
            )
        elif git_subcommand == "grep":
            require(
                "-o" not in git_arguments
                and "--open-files-in-pager" not in git_arguments,
                "Preflight Git grep may open an external pager",
            )
        elif git_subcommand == "remote":
            require(
                not git_arguments
                or git_arguments in (["-v"], ["--verbose"])
                or (
                    git_arguments[0] == "get-url"
                    and len(git_arguments) in {2, 3}
                    and all(
                        not argument.startswith("-") or argument in {"--all", "--push"}
                        for argument in git_arguments[1:]
                    )
                    and not git_arguments[-1].startswith("-")
                ),
                "Preflight Git remote command is not read-only",
            )
        elif git_subcommand == "config":
            config_scope_flags = {
                "--global",
                "--system",
                "--local",
                "--worktree",
                "--show-origin",
                "--show-scope",
                "--name-only",
                "--null",
                "-z",
            }
            config_index = 0
            while (
                config_index < len(git_arguments)
                and (
                    git_arguments[config_index] in config_scope_flags
                    or git_arguments[config_index].startswith("--type=")
                )
            ):
                config_index += 1
            config_operation = git_arguments[config_index : config_index + 1]
            config_values = git_arguments[config_index + 1 :]
            require(
                (config_operation in (["--list"], ["-l"]) and not config_values)
                or (
                    config_operation in (["--get"], ["--get-all"])
                    and len(config_values) == 1
                )
                or (
                    config_operation == ["--get-regexp"]
                    and len(config_values) in {1, 2}
                ),
                "Preflight Git config command is not an exact read operation",
            )
        elif git_subcommand == "tag":
            safe_tag_options = {
                "--list",
                "-l",
                "--contains",
                "--no-contains",
                "--merged",
                "--no-merged",
                "--points-at",
                "--column",
                "--sort",
                "--format",
                "--color",
                "--ignore-case",
            }
            require(
                not git_arguments
                or (
                    any(
                        argument in {"--list", "-l", "--contains", "--points-at"}
                        for argument in git_arguments
                    )
                    and all(
                        not argument.startswith("-")
                        or argument.split("=", 1)[0] in safe_tag_options
                        for argument in git_arguments
                    )
                ),
                "Preflight Git tag command is not list-only",
            )
        elif git_subcommand == "worktree":
            require(git_arguments[0:1] == ["list"], "Preflight Git worktree command is not list-only")
        elif git_subcommand == "submodule":
            require(git_arguments[0:1] == ["status"], "Preflight Git submodule command is not status-only")


def extract_exec_command_output(step: dict[str, Any]) -> tuple[str, int | None] | None:
    """Decode the exact ``r.output`` retained by a functions.exec ATIF step."""
    nested_exec_calls = []
    for call in step_tool_calls(step):
        name = call.get("function_name") or call.get("name") or call.get("tool_name")
        arguments = call.get("arguments")
        source = arguments.get("input") if isinstance(arguments, dict) else None
        if str(name).lower() == "exec" and isinstance(source, str) and "tools.exec_command" in source:
            nested_exec_calls.append(call)
    if not nested_exec_calls:
        return None
    require(len(nested_exec_calls) == 1, "ATIF step contains multiple nested host-shell calls")
    call = nested_exec_calls[0]
    arguments = call.get("arguments")
    source = arguments.get("input") if isinstance(arguments, dict) else ""
    compact_source = re.sub(r"\s+", "", source)
    direct_output = "text(r.output)" in compact_source
    direct_envelope = (
        "text(JSON.stringify({exit_code:r.exit_code,output:r.output}))" in compact_source
    )
    streamed_envelope = all(
        marker in compact_source
        for marker in (
            "lettranscript=r.output",
            "tools.write_stdin",
            "transcript+=r.output",
            "text(JSON.stringify({exit_code:r.exit_code,output:transcript}))",
        )
    ) and ("while(r.session_id" in compact_source or "while(r.sessionId" in compact_source)
    enveloped_output = direct_envelope or streamed_envelope
    require(
        direct_output or enveloped_output,
        "ATIF host-shell wrapper did not emit the untouched command output",
    )
    call_id = call.get("tool_call_id")
    require(isinstance(call_id, str) and call_id, "ATIF host-shell wrapper has no call id")
    observation = step.get("observation")
    require(isinstance(observation, dict), "ATIF host-shell wrapper has no observation")
    results = observation.get("results")
    require(isinstance(results, list), "ATIF host-shell observation has no results")
    matching = [
        result
        for result in results
        if isinstance(result, dict) and result.get("source_call_id") == call_id
    ]
    require(len(matching) == 1, "ATIF host-shell observation is missing or duplicated")
    content = matching[0].get("content")
    try:
        blocks = ast.literal_eval(content) if isinstance(content, str) else content
    except (SyntaxError, ValueError) as exc:
        raise EvidenceError("ATIF host-shell observation is not a retained content-block list") from exc
    require(isinstance(blocks, list) and len(blocks) == 2, "Bad ATIF host-shell content blocks")
    header = blocks[0]
    require(
        isinstance(header, dict)
        and header.get("type") == "input_text"
        and isinstance(header.get("text"), str)
        and header["text"].endswith("Output:\n"),
        "ATIF host-shell completion header is missing",
    )
    output_block = blocks[1]
    require(
        isinstance(output_block, dict)
        and output_block.get("type") == "input_text"
        and isinstance(output_block.get("text"), str),
        "Bad ATIF host-shell output block",
    )
    output = output_block["text"]
    # The benchmark prompt asks the final wrapper to expose both the outer exit
    # code and the untouched output. Decode only that exact envelope shape.
    if enveloped_output:
        try:
            envelope = json.loads(output)
        except json.JSONDecodeError as exc:
            raise EvidenceError("Retained host-shell envelope is not JSON") from exc
        require(
            isinstance(envelope, dict)
            and set(envelope) == {"exit_code", "output"}
            and isinstance(envelope.get("output"), str),
            "Retained host-shell envelope has the wrong shape",
        )
        exit_code = envelope.get("exit_code")
        require(
            isinstance(exit_code, int) and not isinstance(exit_code, bool),
            "Retained host-shell envelope exit code is invalid",
        )
        return envelope["output"], exit_code
    return output, None


def exact_shell_transcripts(
    trajectory: Any,
) -> tuple[
    list[str],
    str,
    int,
    list[str],
    list[str],
    list[tuple[str, int | None]],
]:
    """Bind gate evidence to the exact ATIF-retained host-shell outputs."""
    steps = trajectory_steps(trajectory)
    ri_positions: dict[str, list[int]] = {stage: [] for stage in EXPECTED_RUN_IDS}
    shell_positions: list[int] = []
    for step_index, step in enumerate(steps):
        payload = step_payload(step)
        for stage, run_id in EXPECTED_RUN_IDS.items():
            if run_id in payload:
                expected_tool = (
                    "mcp__relentless_inception__fuse"
                    if stage == "fuse"
                    else "mcp__relentless_inception__adversarial_gate"
                )
                require(expected_tool in payload, f"ATIF {stage} wrapper uses the wrong RI tool")
                ri_positions[stage].append(step_index)
        if has_nested_exec_command(step):
            require(
                "mcp__relentless_inception__" not in payload,
                "Host-shell and RI gate activity share one ATIF wrapper",
            )
            shell_positions.append(step_index)
    for stage, positions in ri_positions.items():
        require(len(positions) == 1, f"ATIF must retain exactly one {stage} wrapper occurrence")
    fuse_position = ri_positions["fuse"][0]
    plan_position = ri_positions["plan"][0]
    pre_execution_position = ri_positions["pre_execution"][0]
    post_position = ri_positions["post_execution"][0]
    final_position = ri_positions["final"][0]
    summarize_position = ri_positions["summarize"][0]
    require(
        fuse_position
        < plan_position
        <= pre_execution_position
        < post_position
        <= final_position
        <= summarize_position,
        "ATIF fusion/lifecycle order is invalid",
    )
    preflight_positions = [index for index in shell_positions if index < fuse_position]
    require(
        1 <= len(preflight_positions) <= 2,
        "ATIF must retain one or two preflight host-shell outputs",
    )
    preflight_outputs = []
    for position in preflight_positions:
        decoded = extract_exec_command_output(steps[position])
        require(decoded is not None, "ATIF preflight host-shell output is missing")
        preflight_outputs.append(decoded)
    require(
        all(exit_code in {None, 0} for _, exit_code in preflight_outputs),
        "Retained preflight shell envelope was not successful",
    )
    execution_positions = [
        index
        for index in shell_positions
        if pre_execution_position < index < post_position
    ]
    require(execution_positions, "ATIF lacks a final-acceptance host-shell output")
    execution_outputs = []
    for position in execution_positions:
        decoded_output = extract_exec_command_output(steps[position])
        require(decoded_output is not None, "ATIF host-execution output is missing")
        execution_outputs.append(decoded_output)
    final_output = execution_outputs[-1]
    require(
        final_output[1] in {None, 0},
        "Retained final-acceptance shell envelope was not successful",
    )
    require(
        not any(index > post_position for index in shell_positions),
        "Host-shell work occurred after the post-execution gate began",
    )
    return (
        [output for output, _ in preflight_outputs],
        final_output[0],
        len(execution_positions),
        [nested_exec_source(steps[position]) for position in preflight_positions],
        [nested_exec_source(steps[position]) for position in execution_positions],
        execution_outputs,
    )


def validate_trajectory(trajectory: Any) -> None:
    # ATIF is authoritative for retained wrapper output, not nested MCP-call
    # identity. The Codex JSON event stream supplies exact atomic call order.
    exact_shell_transcripts(trajectory)


def load_codex_events(path: Path) -> list[dict[str, Any]]:
    """Load JSON events, allowing CLI warnings only before the JSON stream."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EvidenceError(f"Cannot read Codex event log {path}: {exc}") from exc
    events: list[dict[str, Any]] = []
    json_stream_started = False
    for line_number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("{"):
            require(
                not json_stream_started,
                f"Non-JSON content interrupted Codex event stream at {path}:{line_number}",
            )
            continue
        json_stream_started = True
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise EvidenceError(
                f"Malformed Codex JSON event at {path}:{line_number}: {exc}"
            ) from exc
        require(isinstance(value, dict), "Codex event must be an object")
        events.append(value)
    require(events, "Codex event log has no JSON events")
    return events


def is_relentless_inception_item(item: Any) -> bool:
    if not isinstance(item, dict) or item.get("type") != "mcp_tool_call":
        return False
    server = str(item.get("server", "")).lower().replace("_", "-")
    return server == "relentless-inception"


def exit_markers(evidence: str) -> tuple[list[str], list[str]]:
    records: list[tuple[str, str]] = []
    pending_command: str | None = None
    for line in evidence.splitlines():
        if line.startswith("$ "):
            require(pending_command is None, "Transcript command lacks an ordered exit marker")
            pending_command = line
            continue
        status_match = re.fullmatch(r"\[exit (-?\d+)\]", line)
        if status_match:
            require(pending_command is not None, "Transcript exit marker precedes its command")
            records.append((pending_command, status_match.group(1)))
            pending_command = None
    require(pending_command is None, "Transcript command lacks an ordered exit marker")
    return [command for command, _ in records], [status for _, status in records]


def validate_resolved_failure_ledger(
    artifact: str,
    execution_outputs: list[tuple[str, int | None]],
) -> None:
    """Bind every claimed resolved failure to ordered host-shell evidence."""
    execution_history: list[tuple[str, int]] = []
    for transcript, _ in execution_outputs:
        commands, statuses = exit_markers(transcript)
        require(
            len(commands) == len(statuses),
            "Execution transcript has an incomplete command record",
        )
        execution_history.extend(
            (command[2:], int(status)) for command, status in zip(commands, statuses)
        )

    failures = [
        (history_index, command, status)
        for history_index, (command, status) in enumerate(execution_history)
        if status != 0
    ]
    headings = list(
        re.finditer(r"(?im)^## Resolved-failure ledger\s*$", artifact)
    )
    require(len(headings) == 1, "Completed-work artifact must contain one resolved-failure ledger")
    section_start = headings[0].end()
    next_heading = re.search(r"(?im)^## ", artifact[section_start:])
    section_end = (
        section_start + next_heading.start() if next_heading is not None else len(artifact)
    )
    ledger_section = artifact[section_start:section_end].strip()

    if not failures:
        require(
            ledger_section == "No resolved failures.",
            "Resolved-failure ledger must state exactly that no failures occurred",
        )
        return

    fenced_json = re.fullmatch(r"```json\s*(\[[\s\S]*\])\s*```", ledger_section)
    require(fenced_json is not None, "Resolved-failure ledger must be one JSON array fence")
    try:
        entries = json.loads(fenced_json.group(1))
    except json.JSONDecodeError as exc:
        raise EvidenceError("Resolved-failure ledger is not valid JSON") from exc
    require(
        isinstance(entries, list) and len(entries) == len(failures),
        "Resolved-failure ledger does not enumerate every nonzero command exactly once",
    )
    expected_keys = {
        "command",
        "exit_code",
        "cause",
        "corrective_action",
        "resolution_command",
        "resolution_exit_code",
    }
    empty_explanations = {"", "none", "n/a", "na", "unknown", "not applicable"}
    for entry, (failure_index, failure_command, failure_status) in zip(entries, failures):
        require(
            isinstance(entry, dict) and set(entry) == expected_keys,
            "Resolved-failure entry has the wrong schema",
        )
        require(
            entry["command"] == failure_command and entry["exit_code"] == failure_status,
            "Resolved-failure entry does not match the original failed command",
        )
        for field_name in ("cause", "corrective_action"):
            explanation = entry[field_name]
            require(
                isinstance(explanation, str)
                and explanation.strip().lower() not in empty_explanations
                and len(explanation.strip()) >= 8,
                f"Resolved-failure {field_name} is missing or non-substantive",
            )
        resolution_command = entry["resolution_command"]
        require(
            isinstance(resolution_command, str)
            and bool(resolution_command)
            and entry["resolution_exit_code"] == 0,
            "Resolved-failure entry lacks status-zero resolution evidence",
        )
        require(
            any(
                command == resolution_command and status == 0
                for command, status in execution_history[failure_index + 1 :]
            ),
            "Resolved-failure resolution command is not a later status-zero record",
        )


def decode_mcp_result(item: dict[str, Any], stage: str) -> dict[str, Any]:
    """Decode the structured MCP result retained in a completed Codex item."""
    result = item.get("result")
    require(isinstance(result, dict), f"RI {stage} completed result is missing")
    snake_present = "structured_content" in result
    camel_present = "structuredContent" in result
    if snake_present and camel_present:
        require(
            result["structured_content"] == result["structuredContent"],
            f"RI {stage} structured result aliases contradict each other",
        )
    structured = (
        result.get("structured_content")
        if snake_present
        else result.get("structuredContent")
    )
    if structured is not None:
        require(isinstance(structured, dict), f"RI {stage} structured result is not an object")

    decoded_content: dict[str, Any] | None = None
    if "content" in result:
        content = result.get("content")
        require(
            isinstance(content, list) and len(content) == 1,
            f"RI {stage} result must contain one JSON text block",
        )
        block = content[0]
        require(
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str),
            f"RI {stage} result text block is invalid",
        )
        try:
            decoded_content = json.loads(block["text"])
        except json.JSONDecodeError as exc:
            raise EvidenceError(f"RI {stage} result text is not JSON") from exc
        require(
            isinstance(decoded_content, dict),
            f"RI {stage} result text does not decode to an object",
        )
    require(
        isinstance(structured, dict) or isinstance(decoded_content, dict),
        f"RI {stage} result has no decodable representation",
    )
    if isinstance(structured, dict) and isinstance(decoded_content, dict):
        require(
            structured == decoded_content,
            f"RI {stage} structured and text results contradict each other",
        )
    decoded = structured if isinstance(structured, dict) else decoded_content
    require(isinstance(decoded, dict), f"RI {stage} decoded result is not an object")
    return decoded


def validate_codex_log(path: Path, trajectory: Any) -> dict[str, Any]:
    """Validate the call-level contract hidden by functions.exec in ATIF."""
    events = load_codex_events(path)
    require(len(events) >= 3, "Codex log is missing its control envelope")
    thread_started = events[0]
    turn_started = events[1]
    turn_completed = events[-1]
    require(
        isinstance(thread_started, dict)
        and set(thread_started) == {"type", "thread_id"}
        and thread_started.get("type") == "thread.started"
        and isinstance(thread_started.get("thread_id"), str)
        and bool(thread_started["thread_id"]),
        "Codex log has an invalid thread.started envelope",
    )
    require(
        turn_started == {"type": "turn.started"},
        "Codex log has an invalid turn.started envelope",
    )
    turn_usage = turn_completed.get("usage") if isinstance(turn_completed, dict) else None
    require(
        isinstance(turn_completed, dict)
        and set(turn_completed) == {"type", "usage"}
        and turn_completed.get("type") == "turn.completed"
        and isinstance(turn_usage, dict)
        and set(turn_usage) == CODEX_TURN_USAGE_FIELDS,
        "Codex log has an invalid turn.completed envelope",
    )
    for usage_name in CODEX_TURN_USAGE_FIELDS:
        nonnegative_integer(turn_usage[usage_name], f"Codex turn usage {usage_name}")
    require(
        all(
            isinstance(event, dict)
            and event.get("type") in {"item.started", "item.completed"}
            for event in events[2:-1]
        ),
        "Codex log control events are duplicated, inverted, or misplaced",
    )
    started: list[tuple[int, dict[str, Any]]] = []
    completed: list[tuple[int, dict[str, Any]]] = []
    completed_by_id: dict[str, tuple[int, dict[str, Any]]] = {}
    action_starts: dict[str, dict[str, Any]] = {}
    action_start_indexes: dict[str, int] = {}
    action_completions: set[str] = set()
    action_completion_indexes: dict[str, int] = {}
    paired_action_types = {"command_execution", "file_change", "mcp_tool_call"}
    for event_index, event in enumerate(events):
        event_type = event.get("type")
        item = event.get("item")
        if event_index in {0, 1, len(events) - 1}:
            continue
        require(
            event_type in {"item.started", "item.completed"},
            "Codex log contains an unexpected top-level event",
        )
        require(isinstance(item, dict), "Codex item event payload is not an object")
        item_type = item.get("type")
        if item_type in {"agent_message", "reasoning"}:
            require(
                event_type == "item.completed",
                "Codex narrative item unexpectedly has an action start",
            )
            item_id = item.get("id")
            require(isinstance(item_id, str) and item_id, "Codex narrative item has no id")
            narrative_fields = (
                {"id", "type", "text"}
                if item_type == "agent_message"
                else {"id", "type", "text", "summary"}
            )
            require(
                set(item).issubset(narrative_fields)
                and (
                    item_type == "reasoning"
                    or (isinstance(item.get("text"), str) and bool(item["text"]))
                ),
                "Codex narrative item carries action fields or invalid text",
            )
            require(item_id not in completed_by_id, f"Duplicate completed Codex item: {item_id}")
            completed_by_id[item_id] = (event_index, item)
            continue
        require(item_type in paired_action_types, "Codex log contains an unexpected tool action")
        if item_type == "mcp_tool_call":
            require(
                is_relentless_inception_item(item),
                "Codex log contains an unexpected external MCP call",
            )
        item_id = item.get("id")
        require(isinstance(item_id, str) and item_id, "Codex action item has no id")
        if event_type == "item.started":
            require(item_id not in action_starts, f"Duplicate started Codex item: {item_id}")
            require(item.get("status") == "in_progress", "Codex action did not start in progress")
            action_starts[item_id] = item
            action_start_indexes[item_id] = event_index
        else:
            require(item_id in action_starts, f"Completed Codex action has no prior start: {item_id}")
            require(item_id not in action_completions, f"Duplicate completed Codex action: {item_id}")
            started_action = action_starts[item_id]
            require(
                item_type == started_action.get("type"),
                "Codex action type changed between start and completion",
            )
            immutable_fields = {
                "command_execution": ("command",),
                "file_change": ("changes",),
                "mcp_tool_call": ("server", "tool", "arguments"),
            }[item_type]
            require(
                all(item.get(field) == started_action.get(field) for field in immutable_fields),
                "Codex action identity changed between start and completion",
            )
            if item_type == "command_execution":
                exit_code = item.get("exit_code")
                require(
                    isinstance(exit_code, int)
                    and not isinstance(exit_code, bool)
                    and item.get("status")
                    == ("completed" if exit_code == 0 else "failed"),
                    "Codex command completion status contradicts its exit code",
                )
            elif item_type == "file_change":
                require(
                    item.get("status") == "completed",
                    "Codex file change did not complete successfully",
                )
            else:
                require(
                    item.get("status") in {"completed", "failed"},
                    "Codex MCP completion status is invalid",
                )
            action_completions.add(item_id)
            action_completion_indexes[item_id] = event_index
            require(item_id not in completed_by_id, f"Duplicate completed Codex item: {item_id}")
            completed_by_id[item_id] = (event_index, item)
        if not is_relentless_inception_item(item):
            continue
        if event_type == "item.started":
            started.append((event_index, item))
        else:
            completed.append((event_index, item))
    require(
        set(action_starts) == action_completions,
        "Codex log contains an action that never completed",
    )

    expected = [
        ("fuse" if stage == "fuse" else "adversarial_gate", run_id, stage)
        for stage, run_id in EXPECTED_RUN_IDS.items()
    ]
    require(
        len(started) == len(expected) and len(completed) == len(expected),
        "Codex log must contain exactly six started and six completed RI calls",
    )

    started_ids = [item.get("id") for _, item in started]
    require(
        len(set(started_ids)) == len(started_ids) and all(isinstance(item_id, str) for item_id in started_ids),
        "RI started item ids are missing or duplicated",
    )
    arguments_by_stage: dict[str, dict[str, Any]] = {}
    decoded_by_stage: dict[str, dict[str, Any]] = {}
    for call_index, (expected_tool, expected_run_id, stage) in enumerate(expected):
        start_index, started_item = started[call_index]
        item_id = started_item.get("id")
        completion = completed_by_id.get(item_id)
        require(completion is not None, f"RI {stage} call has no matching completion")
        completion_index, completed_item = completion
        require(start_index < completion_index, f"RI {stage} completed before it started")
        if call_index + 1 < len(started):
            require(
                completion_index < started[call_index + 1][0],
                f"RI {stage} overlapped the next lifecycle call",
            )
        require(started_item.get("tool") == expected_tool, f"Unexpected RI tool for {stage}")
        require(completed_item.get("tool") == expected_tool, f"Completed RI tool mismatch for {stage}")
        require(
            started_item.get("id") == completed_item.get("id"),
            f"RI started/completed item mismatch for {stage}",
        )
        arguments = started_item.get("arguments")
        require(isinstance(arguments, dict), f"RI {stage} arguments are missing")
        expected_argument_keys = (
            {"task", "context", "mechanical_evidence", "resume_run_id"}
            if stage == "fuse"
            else {"task", "artifact", "mechanical_evidence", "resume_run_id"}
        )
        require(
            set(arguments) == expected_argument_keys,
            f"RI {stage} arguments do not match the exact benchmark call schema",
        )
        require(
            completed_item.get("arguments") == arguments,
            f"RI {stage} completed arguments differ from its start",
        )
        require(
            arguments.get("resume_run_id") == expected_run_id,
            f"RI {stage} run id is not deterministic",
        )
        require(completed_item.get("status") == "completed", f"RI {stage} call did not complete")
        require(completed_item.get("error") is None, f"RI {stage} call recorded an error")
        decoded = decode_mcp_result(completed_item, stage)
        require(decoded.get("run_id") == expected_run_id, f"RI {stage} returned a different run id")
        gate = decoded.get("gate")
        require(
            isinstance(gate, dict) and gate.get("passed") is True,
            f"RI {stage} returned a failed or missing gate",
        )
        if stage == "fuse":
            require(decoded.get("status") == "completed", "Fusion result status is not completed")
        if stage != "fuse":
            task = arguments.get("task")
            require(
                isinstance(task, str) and task.startswith(f"Lifecycle stage: {stage}"),
                f"RI {stage} task lacks its exact lifecycle label",
            )
        arguments_by_stage[stage] = arguments
        decoded_by_stage[stage] = decoded

    fusion_task = arguments_by_stage["fuse"].get("task")
    require(isinstance(fusion_task, str), "Fusion task is missing")
    prefix_end = fusion_task.find("evidence.")
    require(prefix_end >= 0, "Fusion task lacks the required plan-only prefix")
    prefix_end += len("evidence.")
    require(
        normalized_whitespace(fusion_task[:prefix_end]) == FUSION_TASK_PREFIX,
        "Fusion task changed the required plan-only objective",
    )
    original_request = fusion_task[prefix_end:].strip()
    require(bool(original_request), "Fusion task omitted the benchmark instruction")
    require(
        original_request == trajectory_original_request(trajectory),
        "Fusion task is not bound to the exact ATIF benchmark instruction",
    )
    require(
        fusion_task == FUSION_TASK_PREFIX + "\n" + original_request,
        "Fusion task is not the exact plan-only template",
    )
    fusion_context = arguments_by_stage["fuse"].get("context")
    require(
        isinstance(fusion_context, str)
        and "active Codex host" in fusion_context
        and "not an external seat" in fusion_context
        and "isolated from this workspace" in fusion_context,
        "Fusion context does not bind execution to the active Codex host",
    )
    for stage, task_prefix in LIFECYCLE_TASK_PREFIXES.items():
        task = arguments_by_stage[stage]["task"]
        require(
            task == task_prefix + original_request,
            f"RI {stage} task is not its exact lifecycle template",
        )

    first_ri_event_index = started[0][0]
    preflight_commands: list[tuple[int, dict[str, Any]]] = []
    preflight_completed_items: list[dict[str, Any]] = []
    for event_index, event in enumerate(events[:first_ri_event_index]):
        if event.get("type") != "item.started":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        require(item.get("type") != "file_change", "Preflight mutated the workspace")
        if item.get("type") == "command_execution":
            preflight_commands.append((event_index, item))
    require(
        1 <= len(preflight_commands) <= 2,
        "Preflight must use one or two host-shell calls before fusion",
    )
    for command_start_index, command_item in preflight_commands:
        item_id = command_item.get("id")
        require(isinstance(item_id, str), "Preflight command has no item id")
        completion = completed_by_id.get(item_id)
        require(completion is not None, "Preflight command did not complete")
        completion_index, completed_item = completion
        require(
            command_start_index < completion_index,
            "Preflight command completed before it started",
        )
        require(completion_index < first_ri_event_index, "Preflight command overlapped fusion")
        exit_code = completed_item.get("exit_code")
        require(
            isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code == 0,
            "Preflight host-shell call was not successful",
        )
        preflight_completed_items.append(completed_item)

    (
        preflight_outputs,
        final_transcript,
        atif_execution_count,
        preflight_sources,
        execution_sources,
        atif_execution_outputs,
    ) = exact_shell_transcripts(trajectory)
    preflight_transcript = "".join(preflight_outputs)
    require(
        len(preflight_outputs) == len(preflight_commands),
        "ATIF and Codex log disagree on the preflight shell-call count",
    )
    bound_preflight_records: list[str] = []
    for atif_output, source, (_, command_item), completed_item in zip(
        preflight_outputs,
        preflight_sources,
        preflight_commands,
        preflight_completed_items,
    ):
        require(
            command_is_bound_to_wrapper(command_item.get("command"), source),
            "Codex preflight command differs from its ATIF wrapper",
        )
        bound_preflight_records.extend(
            validate_wrapper_records(source, atif_output, wrapper_exit_code=0)
        )
        aggregated_output = completed_item.get("aggregated_output")
        require(
            isinstance(aggregated_output, str)
            and bool(aggregated_output)
            and atif_output.endswith(aggregated_output),
            "Codex preflight output is not a retained suffix of its ATIF transcript",
        )

    preflight_evidence = arguments_by_stage["fuse"].get("mechanical_evidence")
    require(isinstance(preflight_evidence, str), "Fusion preflight evidence is missing")
    require(
        preflight_evidence == preflight_transcript,
        "Fusion evidence is not byte-identical to the retained preflight transcript",
    )
    require(
        len(preflight_evidence) <= MAX_PREFLIGHT_EVIDENCE_CHARACTERS,
        "Fusion preflight evidence exceeds 12,000 characters",
    )
    preflight_records, preflight_statuses = exit_markers(preflight_evidence)
    require(
        preflight_records == bound_preflight_records,
        "Fusion evidence command records differ from their ATIF wrappers",
    )
    for preflight_record in preflight_records:
        validate_preflight_record(preflight_record)
    require(
        1 <= len(preflight_records) <= MAX_PREFLIGHT_COMMAND_RECORDS,
        "Fusion preflight evidence exceeds 12 command records",
    )
    require(
        len(preflight_statuses) == len(preflight_records)
        and all(status == "0" for status in preflight_statuses),
        "Fusion preflight command statuses are missing or nonzero",
    )

    fusion_synthesis = decoded_by_stage["fuse"].get("synthesis")
    require(isinstance(fusion_synthesis, str) and fusion_synthesis, "Fusion synthesis is missing")
    plan = arguments_by_stage["plan"]
    pre_execution = arguments_by_stage["pre_execution"]
    require(
        isinstance(plan.get("artifact"), str)
        and bool(plan["artifact"])
        and plan.get("artifact") == pre_execution.get("artifact") == fusion_synthesis,
        "Plan and pre-execution gates did not review the exact fused synthesis",
    )
    require(
        plan.get("mechanical_evidence") == preflight_evidence
        and pre_execution.get("mechanical_evidence") == preflight_evidence,
        "Plan and pre-execution gates did not reuse exact fusion evidence",
    )

    fuse_completion_index = completed_by_id[started[0][1]["id"]][0]
    pre_execution_completion_index = completed_by_id[started[2][1]["id"]][0]
    post_start_index = started[3][0]
    for action_id, action_item in action_starts.items():
        if action_item.get("type") not in {"command_execution", "file_change"}:
            continue
        action_start_index = action_start_indexes[action_id]
        action_completion_index = action_completion_indexes[action_id]
        require(
            action_completion_index < first_ri_event_index
            or (
                pre_execution_completion_index < action_start_index
                and action_completion_index < post_start_index
            ),
            "Host action occurred during fusion/gating or crossed a lifecycle boundary",
        )
    for event in events[fuse_completion_index + 1 : pre_execution_completion_index]:
        if event.get("type") != "item.started":
            continue
        item = event.get("item")
        if isinstance(item, dict):
            require(
                item.get("type") not in {"command_execution", "file_change"},
                "Host execution began before plan and pre-execution receipts passed",
            )

    execution_commands: list[tuple[int, dict[str, Any]]] = []
    for event_index, event in enumerate(events[pre_execution_completion_index + 1 : post_start_index], pre_execution_completion_index + 1):
        if event.get("type") != "item.started":
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "command_execution":
            execution_commands.append((event_index, item))
    require(execution_commands, "No host command produced final-acceptance evidence")
    require(
        len(execution_commands) == atif_execution_count,
        "ATIF and Codex log disagree on the host-execution shell-call count",
    )
    for (_, command_item), source in zip(execution_commands, execution_sources):
        require(
            command_is_bound_to_wrapper(command_item.get("command"), source),
            "Codex host-execution command differs from its ATIF wrapper",
        )
    execution_completions: list[tuple[int, dict[str, Any]]] = []
    execution_item_ids: set[str] = set()
    for command_start_index, command_item in execution_commands:
        item_id = command_item.get("id")
        require(
            isinstance(item_id, str) and item_id not in execution_item_ids,
            "Host-execution command id is missing or duplicated",
        )
        execution_item_ids.add(item_id)
        completion = completed_by_id.get(item_id)
        require(completion is not None, "Host-execution command did not complete")
        completion_index, completed_command = completion
        require(
            command_start_index < completion_index < post_start_index,
            "Host-execution command overlapped post-execution review",
        )
        exit_code = completed_command.get("exit_code")
        require(
            isinstance(exit_code, int) and not isinstance(exit_code, bool),
            "Host-execution command has no integer exit code",
        )
        execution_completions.append(completion)
    for source, decoded_output, (_, completed_command) in zip(
        execution_sources,
        atif_execution_outputs,
        execution_completions,
    ):
        validate_wrapper_records(
            source,
            decoded_output[0],
            wrapper_exit_code=completed_command["exit_code"],
        )
        aggregated_output = completed_command.get("aggregated_output")
        require(
            isinstance(aggregated_output, str)
            and bool(aggregated_output)
            and decoded_output[0].endswith(aggregated_output),
            "Codex host-execution output is not a retained suffix of its ATIF transcript",
        )
        if decoded_output[1] is not None:
            require(
                decoded_output[1] == completed_command.get("exit_code"),
                "ATIF host-shell envelope exit code differs from Codex",
            )
    last_command_start = execution_commands[-1][0]
    last_command_completion_index, last_completed_command = execution_completions[-1]
    require(
        last_completed_command.get("exit_code") == 0,
        "Final-acceptance host command was not successful",
    )
    final_aggregated_output = last_completed_command.get("aggregated_output")
    require(
        isinstance(final_aggregated_output, str)
        and bool(final_aggregated_output)
        and final_transcript.endswith(final_aggregated_output),
        "Codex final output is not a retained suffix of its ATIF transcript",
    )
    for event in events[last_command_start + 1 : post_start_index]:
        item = event.get("item")
        if isinstance(item, dict):
            require(
                item.get("type") != "file_change",
                "Workspace changed after final acceptance began",
            )
    for event in events[post_start_index:]:
        item = event.get("item")
        if isinstance(item, dict):
            require(
                item.get("type") not in {"command_execution", "file_change"},
                "Host execution continued after post-execution review began",
            )

    completed_stages = ("post_execution", "final", "summarize")
    completed_artifacts = [arguments_by_stage[stage].get("artifact") for stage in completed_stages]
    completed_evidence = [
        arguments_by_stage[stage].get("mechanical_evidence") for stage in completed_stages
    ]
    require(
        all(isinstance(value, str) and value for value in completed_artifacts),
        "Completed-work artifact is missing",
    )
    require(
        all(value == completed_artifacts[0] for value in completed_artifacts[1:]),
        "Completed-work gates did not reuse a byte-identical artifact",
    )
    require(
        len(completed_artifacts[0]) <= MAX_COMPLETED_ARTIFACT_CHARACTERS,
        "Completed-work artifact exceeds 12,000 characters",
    )
    require(
        "resolved-failure ledger" in completed_artifacts[0].lower(),
        "Completed-work artifact lacks a resolved-failure ledger",
    )
    validate_resolved_failure_ledger(completed_artifacts[0], atif_execution_outputs)
    require(
        "evidence-backed final state" in completed_artifacts[0].lower(),
        "Completed-work artifact lacks an evidence-backed final-state section",
    )
    require(
        "trajectory context" in completed_artifacts[0].lower(),
        "Completed-work artifact lacks an explicit trajectory-context section",
    )
    require(
        "remaining risks" in completed_artifacts[0].lower(),
        "Completed-work artifact lacks a remaining-risks section",
    )
    require(
        all(isinstance(value, str) and value for value in completed_evidence)
        and all(value == completed_evidence[0] for value in completed_evidence[1:]),
        "Completed-work gates did not reuse byte-identical mechanical evidence",
    )
    require(
        completed_evidence[0] == final_transcript,
        "Completed-work evidence is not byte-identical to final acceptance",
    )
    final_records, final_statuses = exit_markers(completed_evidence[0])
    require(
        bool(final_records)
        and len(final_statuses) == len(final_records)
        and all(status == "0" for status in final_statuses),
        "Final-acceptance command statuses are missing or nonzero",
    )

    artifact_hashes: dict[str, str] = {}
    for stage in EXPECTED_RUN_IDS:
        artifact = fusion_synthesis if stage == "fuse" else arguments_by_stage[stage].get("artifact")
        require(isinstance(artifact, str) and artifact, f"RI {stage} reviewed artifact is missing")
        artifact_hash = sha256(artifact.encode("utf-8")).hexdigest()
        artifact_hashes[stage] = artifact_hash
        require(
            decoded_by_stage[stage].get("gate", {}).get("artifact_sha256") == artifact_hash,
            f"RI {stage} returned gate is bound to a different artifact",
        )
    return {
        "arguments_by_stage": arguments_by_stage,
        "decoded_by_stage": decoded_by_stage,
        "artifact_hashes": artifact_hashes,
    }


def load_run_file(root: Path, manifest_path: Path, relative: Any) -> tuple[Path, Any]:
    require(isinstance(relative, str) and relative, "Run artifact path must be nonempty")
    candidate = (manifest_path.parent / relative).resolve()
    try:
        candidate.relative_to(manifest_path.parent.resolve())
    except ValueError as exc:
        raise EvidenceError(f"Run artifact escapes its run directory: {relative}") from exc
    require(candidate.is_file(), f"Missing RI run artifact: {candidate}")
    require(
        candidate.stat().st_mode & 0o077 == 0,
        f"RI run artifact is not owner-only: {relative}",
    )
    current_parent = candidate.parent
    while True:
        require(
            current_parent.stat().st_mode & 0o077 == 0,
            f"RI run artifact directory is not owner-only: {current_parent.name}",
        )
        if current_parent == manifest_path.parent:
            break
        current_parent = current_parent.parent
    return candidate, load_json(candidate)


def validate_ledger(
    ledger: Any,
    *,
    fusion_run: bool,
    manifest_path: Path,
    expected_run_id: str,
    expected_input_hash: str,
    expected_config_hash: str,
    global_attempt_ids: set[str],
    global_entry_ids: set[str],
    global_request_ids: set[str],
) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[tuple[str, str], dict[str, Any]],
]:
    require(
        isinstance(ledger, dict) and set(ledger) == LEDGER_FIELDS,
        "RI ledger schema is invalid",
    )
    ledger_schema = ledger.get("schema_version")
    require(
        isinstance(ledger_schema, int)
        and not isinstance(ledger_schema, bool)
        and ledger_schema == 3,
        "Unsupported RI ledger schema",
    )
    accounting_failure = optional_message(ledger.get("accounting_failure"), "RI accounting_failure")
    stop_reason = optional_message(ledger.get("stop_reason"), "RI stop_reason")
    require(accounting_failure is None, "RI accounting failure")
    require(stop_reason is None, "RI budget stop")
    entries = ledger.get("entries")
    attempts = ledger.get("attempt_entries")
    require(
        isinstance(entries, list)
        and bool(entries)
        and all(isinstance(entry, dict) for entry in entries),
        "Empty or invalid RI ledger entries",
    )
    require(
        isinstance(attempts, list)
        and all(isinstance(attempt, dict) for attempt in attempts)
        and len(attempts) == len(entries),
        "RI ledger contains a hidden retry or unmatched attempt",
    )
    calls = nonnegative_integer(ledger.get("calls"), "RI calls")
    attempt_count = nonnegative_integer(ledger.get("attempts"), "RI attempts")
    require(
        attempt_count == calls == len(entries),
        "RI ledger attempt/call totals do not match its receipts",
    )
    aggregate_counters = {
        counter_name: nonnegative_integer(ledger.get(counter_name), f"RI {counter_name}")
        for counter_name in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "tool_calls",
        )
    }
    total_tokens = nonnegative_integer(ledger.get("total_tokens"), "RI total_tokens")
    require(
        total_tokens == aggregate_counters["input_tokens"] + aggregate_counters["output_tokens"],
        "RI total_tokens does not equal input_tokens plus output_tokens",
    )
    known_cost_usd = nonnegative_number(ledger.get("known_cost_usd"), "RI known_cost_usd")
    unknown_cost_calls = nonnegative_integer(
        ledger.get("unknown_cost_calls"), "RI unknown_cost_calls"
    )
    wall_seconds = nonnegative_number(ledger.get("wall_seconds"), "RI wall_seconds")
    provider_cost = ledger.get("provider_cost_usd")
    require(isinstance(provider_cost, dict), "RI provider_cost_usd is not an object")
    normalized_provider_cost: dict[str, float] = {}
    for provider_name, provider_value in provider_cost.items():
        require(
            isinstance(provider_name, str) and bool(provider_name),
            "RI provider cost key is invalid",
        )
        normalized_provider_cost[provider_name] = nonnegative_number(
            provider_value, f"RI provider cost for {provider_name}"
        )
    warnings = ledger.get("warnings")
    require(
        isinstance(warnings, list)
        and all(isinstance(warning, str) and bool(warning) for warning in warnings),
        "RI warnings schema is invalid",
    )

    attempt_ids: set[str] = set()
    attempts_by_index: dict[int, dict[str, Any]] = {}
    for expected_attempt_index, attempt in enumerate(attempts):
        require(
            set(attempt) == ATTEMPT_ENTRY_FIELDS,
            "RI attempt receipt schema is invalid",
        )
        attempt_index = attempt.get("attempt_index")
        require(
            isinstance(attempt_index, int)
            and not isinstance(attempt_index, bool)
            and attempt_index == expected_attempt_index,
            "RI attempt receipts are not in zero-based order",
        )
        require(
            isinstance(attempt.get("stage"), str)
            and bool(attempt["stage"])
            and isinstance(attempt.get("seat"), str)
            and bool(attempt["seat"]),
            "RI attempt stage or seat is invalid",
        )
        invocation_sha256 = attempt.get("invocation_sha256")
        attempt_id = attempt.get("attempt_id")
        require(
            isinstance(invocation_sha256, str)
            and HASH.fullmatch(invocation_sha256) is not None
            and isinstance(attempt_id, str)
            and HASH.fullmatch(attempt_id) is not None,
            "RI attempt receipt hash is invalid",
        )
        require(
            attempt_id
            == canonical_json_hash(
                {
                    "schema_version": 1,
                    "invocation_sha256": invocation_sha256,
                    "attempt_index": attempt_index,
                }
            ),
            "RI attempt receipt id is invalid",
        )
        require(attempt_id not in attempt_ids, "Duplicate RI attempt receipt")
        require(
            attempt_id not in global_attempt_ids,
            "RI attempt receipt was replayed across runs",
        )
        attempt_ids.add(attempt_id)
        global_attempt_ids.add(attempt_id)
        attempts_by_index[attempt_index] = attempt

    validate_attempt_stage_order(attempts, fusion_run=fusion_run)

    seat_models = {
        "grok45_researcher": "grok-4.5",
        "grok45_adversary": "grok-4.5",
        "grok45_constraint_auditor": "grok-4.5",
        "grok45_judge": "grok-4.5",
        "grok45_synthesizer": "grok-4.5",
        "grok45_verifier": "grok-4.5",
    }
    entry_ids: set[str] = set()
    recomputed_counters = {counter_name: 0 for counter_name in aggregate_counters}
    recomputed_known_cost_usd = 0.0
    recomputed_provider_cost: dict[str, float] = {}
    recomputed_unknown_cost_calls = 0
    entry_accounting_failures: list[str] = []
    maximum_latency_seconds = 0.0
    normalized_entries: list[dict[str, Any]] = []
    responses_by_entry_id: dict[str, dict[str, Any]] = {}
    invocations_by_call: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        require(set(entry) == LEDGER_ENTRY_FIELDS, "Bad RI ledger entry schema")
        for key in ("attempt_id", "entry_id", "invocation_sha256", "response_sha256"):
            require(
                isinstance(entry.get(key), str) and HASH.match(entry[key]),
                f"Bad RI {key}",
            )
        attempt_index = entry.get("attempt_index")
        require(
            isinstance(attempt_index, int)
            and not isinstance(attempt_index, bool)
            and attempt_index in attempts_by_index,
            "Bad RI attempt_index",
        )
        reserved_attempt = attempts_by_index[attempt_index]
        require(
            entry.get("attempt_id") == reserved_attempt["attempt_id"]
            and entry.get("invocation_sha256") == reserved_attempt["invocation_sha256"]
            and entry.get("stage") == reserved_attempt["stage"]
            and entry.get("seat") == reserved_attempt["seat"],
            "RI call receipt does not match its reserved attempt",
        )
        expected_attempt_id = canonical_json_hash(
            {
                "schema_version": 1,
                "invocation_sha256": entry["invocation_sha256"],
                "attempt_index": attempt_index,
            }
        )
        require(entry["attempt_id"] == expected_attempt_id, "RI attempt receipt id is invalid")
        require(
            entry["entry_id"]
            == call_receipt_entry_id(
                entry["attempt_id"],
                entry["invocation_sha256"],
                entry["response_sha256"],
            ),
            "RI call receipt entry id is invalid",
        )
        require(entry["entry_id"] not in entry_ids, "Duplicate RI call receipt")
        require(
            entry["entry_id"] not in global_entry_ids,
            "RI call receipt was replayed across runs",
        )
        entry_ids.add(entry["entry_id"])
        global_entry_ids.add(entry["entry_id"])

        seat = entry.get("seat")
        model = seat_models.get(seat)
        require(model is not None, "RI ledger contains an unexpected benchmark seat")
        require(entry.get("provider") == "xai_direct", "RI provider is not direct xAI")
        require(
            entry.get("requested_model") == model and entry.get("actual_model") == model,
            f"RI model provenance mismatch for seat {seat}",
        )
        require(
            isinstance(entry.get("request_id"), str) and bool(entry["request_id"]),
            "RI ledger request_id is missing",
        )
        require(entry.get("raw_status") == "completed", "RI ledger response did not complete")
        require(isinstance(entry.get("route"), dict), "RI ledger route is not an object")
        latency_seconds = nonnegative_number(
            entry.get("latency_seconds"), "RI ledger latency_seconds"
        )
        maximum_latency_seconds = max(maximum_latency_seconds, latency_seconds)
        usage = validate_usage(entry.get("usage"), "RI ledger entry")
        for counter_name in recomputed_counters:
            recomputed_counters[counter_name] += usage[counter_name]
        usage_cost = usage["cost_usd"]
        if usage_cost is None:
            recomputed_unknown_cost_calls += 1
        else:
            normalized_cost = float(usage_cost)
            recomputed_known_cost_usd += normalized_cost
            recomputed_provider_cost[entry["provider"]] = (
                recomputed_provider_cost.get(entry["provider"], 0.0) + normalized_cost
            )
        if usage["accounting_error"] is not None:
            entry_accounting_failures.append(usage["accounting_error"])
        require(
            entry.get("response_artifact") == f"responses/{entry['entry_id']}.json",
            "RI ledger response artifact is not bound to its receipt",
        )
        _, response_artifact = load_run_file(
            manifest_path.parent,
            manifest_path,
            entry["response_artifact"],
        )
        require(
            isinstance(response_artifact, dict)
            and set(response_artifact) == {"schema_version", "invocation", "receipt", "response"}
            and response_artifact.get("schema_version") == 1,
            "RI raw-response artifact schema is invalid",
        )
        invocation = response_artifact.get("invocation")
        require(
            isinstance(invocation, dict)
            and set(invocation)
            == {
                "schema_version",
                "run_id",
                "input_sha256",
                "config_sha256",
                "stage",
                "seat_name",
                "system_sha256",
                "prompt_sha256",
                "response_schema_sha256",
                "schema_name",
            }
            and invocation.get("schema_version") == 1,
            "RI raw-response invocation schema is invalid",
        )
        require(
            invocation.get("run_id") == expected_run_id
            and invocation.get("input_sha256") == expected_input_hash
            and invocation.get("config_sha256") == expected_config_hash
            and invocation.get("stage") == entry.get("stage")
            and invocation.get("seat_name") == seat,
            "RI raw-response invocation identity mismatch",
        )
        for key in ("system_sha256", "prompt_sha256", "response_schema_sha256"):
            require(
                isinstance(invocation.get(key), str) and HASH.match(invocation[key]),
                f"Bad RI invocation {key}",
            )
        invocation_stage = str(invocation.get("stage"))
        if invocation_stage == "judge":
            expected_schema_name = "fusion_judgment"
        elif invocation_stage in {"panel", "synthesis"} or re.fullmatch(
            r"amendment-[12]", invocation_stage
        ):
            expected_schema_name = "structured_response"
        elif invocation_stage == "gate" or re.fullmatch(r"gate-[12]", invocation_stage):
            expected_schema_name = "adversarial_verdict"
        else:
            raise EvidenceError(f"RI invocation stage is unexpected: {invocation_stage}")
        require(
            invocation.get("schema_name") == expected_schema_name,
            "RI invocation schema name does not match its stage",
        )
        require(
            canonical_json_hash(invocation) == entry["invocation_sha256"],
            "RI invocation hash mismatch",
        )
        invocation_key = (invocation_stage, str(seat))
        require(
            invocation_key not in invocations_by_call,
            f"Duplicate RI logical invocation: {invocation_key}",
        )
        invocations_by_call[invocation_key] = dict(invocation)
        receipt = response_artifact.get("receipt")
        expected_receipt = {
            "schema_version": 1,
            "entry_id": entry["entry_id"],
            "attempt_id": entry["attempt_id"],
            "invocation_sha256": entry["invocation_sha256"],
            "response_sha256": entry["response_sha256"],
        }
        require(receipt == expected_receipt, "RI raw-response receipt differs from its ledger")
        response = response_artifact.get("response")
        validate_model_response(response, "RI raw response")
        require(
            canonical_json_hash(response) == entry["response_sha256"],
            "RI raw-response hash mismatch",
        )
        require(
            response.get("provider") == entry.get("provider")
            and response.get("requested_model") == entry.get("requested_model")
            and response.get("actual_model") == entry.get("actual_model"),
            "RI raw-response provider/model provenance mismatch",
        )
        response_metadata_fields = (
            "provider",
            "requested_model",
            "actual_model",
            "usage",
            "latency_seconds",
            "request_id",
            "route",
            "raw_status",
        )
        require(
            all(entry.get(key) == response.get(key) for key in response_metadata_fields),
            "RI ledger response metadata differs from its raw-response artifact",
        )
        request_id = response.get("request_id")
        require(request_id not in global_request_ids, "RI response request id was replayed")
        global_request_ids.add(request_id)
        responses_by_entry_id[entry["entry_id"]] = response
        normalized_entries.append(entry)

    require(
        {entry["attempt_index"] for entry in normalized_entries} == set(attempts_by_index),
        "RI attempt receipts do not match completed call receipts",
    )
    require(
        recomputed_counters == aggregate_counters,
        "RI aggregate usage counters do not match its entries",
    )
    require(
        recomputed_known_cost_usd == known_cost_usd,
        "RI known_cost_usd does not match its entries",
    )
    require(
        recomputed_provider_cost == normalized_provider_cost,
        "RI provider_cost_usd does not match its entries",
    )
    require(
        recomputed_unknown_cost_calls == unknown_cost_calls,
        "RI unknown_cost_calls does not match its entries",
    )
    require(
        wall_seconds >= maximum_latency_seconds,
        "RI wall_seconds is shorter than a recorded provider call",
    )
    require(
        (
            accounting_failure == entry_accounting_failures[0]
            if entry_accounting_failures
            else accounting_failure is None
        ),
        "RI accounting_failure does not match entry usage",
    )

    observed_calls = Counter(
        (entry.get("stage"), entry.get("seat"), entry.get("actual_model"))
        for entry in normalized_entries
    )
    if not fusion_run:
        expected_calls = Counter(
            ("gate", seat, model) for seat, model in EXPECTED_GATE_REVIEWERS.items()
        )
        require(observed_calls == expected_calls, "Lifecycle ledger call set is invalid")
        return normalized_entries, responses_by_entry_id, invocations_by_call

    for expected_call, expected_count in EXPECTED_FUSION_CALLS.items():
        require(
            observed_calls[expected_call] == expected_count,
            f"Fusion ledger lacks exact base call {expected_call}",
        )
    for (stage, seat, model), count in observed_calls.items():
        if (stage, seat, model) in EXPECTED_FUSION_CALLS:
            continue
        amendment_match = re.fullmatch(r"amendment-([12])", str(stage))
        amendment_gate_match = re.fullmatch(r"gate-([12])", str(stage))
        if amendment_match:
            require(
                seat == "grok45_synthesizer" and model == "grok-4.5" and count == 1,
                "Fusion amendment ledger call is invalid",
            )
        elif amendment_gate_match:
            require(
                seat in EXPECTED_GATE_REVIEWERS
                and model == EXPECTED_GATE_REVIEWERS[seat]
                and count == 1,
                "Fusion amendment-gate ledger call is invalid",
            )
        else:
            raise EvidenceError(f"Fusion ledger contains unexpected call {(stage, seat, model)}")
    for round_index in (1, 2):
        amendment_key = (f"amendment-{round_index}", "grok45_synthesizer", "grok-4.5")
        reviewer_counts = [
            observed_calls[(f"gate-{round_index}", seat, model)]
            for seat, model in EXPECTED_GATE_REVIEWERS.items()
        ]
        require(
            reviewer_counts in ([0, 0], [1, 1]),
            f"Fusion gate-{round_index} reviewer quorum is incomplete",
        )
        if observed_calls[amendment_key] or any(reviewer_counts):
            require(
                observed_calls[amendment_key] == 1,
                f"Fusion gate-{round_index} lacks its amendment call",
            )
        if round_index == 2 and observed_calls[amendment_key]:
            require(
                observed_calls[("amendment-1", "grok45_synthesizer", "grok-4.5")] == 1,
                "Fusion amendment rounds are not sequential",
            )
    return normalized_entries, responses_by_entry_id, invocations_by_call


def validate_semantic_response(
    response: Any,
    evidence: Any,
    *,
    ledger_entries: list[dict[str, Any]],
    responses_by_entry_id: dict[str, dict[str, Any]],
    expected_stage: str,
    expected_seat: str,
    expected_model: str,
) -> None:
    validate_model_response(response, "RI semantic response")
    require(
        isinstance(evidence, dict)
        and set(evidence)
        == {
            "schema_version",
            "entry_id",
            "attempt_id",
            "invocation_sha256",
            "response_sha256",
        }
        and evidence.get("schema_version") == 1,
        "RI semantic response receipt schema is invalid",
    )
    for key in ("entry_id", "attempt_id", "invocation_sha256", "response_sha256"):
        require(
            isinstance(evidence.get(key), str) and HASH.match(evidence[key]),
            f"Bad RI semantic response receipt {key}",
        )
    require(
        evidence["response_sha256"] == canonical_json_hash(response),
        "RI semantic response hash mismatch",
    )
    matching_entries = [
        entry for entry in ledger_entries if entry.get("entry_id") == evidence["entry_id"]
    ]
    require(len(matching_entries) == 1, "RI semantic response receipt lacks one ledger entry")
    ledger_entry = matching_entries[0]
    require(
        responses_by_entry_id.get(evidence["entry_id"]) == response,
        "RI semantic response differs from its raw-response artifact",
    )
    for key in ("entry_id", "attempt_id", "invocation_sha256", "response_sha256"):
        require(
            ledger_entry.get(key) == evidence[key],
            f"RI semantic response receipt {key} differs from its ledger entry",
        )
    require(
        ledger_entry.get("stage") == expected_stage
        and ledger_entry.get("seat") == expected_seat
        and ledger_entry.get("provider") == "xai_direct"
        and ledger_entry.get("requested_model") == expected_model
        and ledger_entry.get("actual_model") == expected_model,
        "RI semantic response ledger provenance mismatch",
    )
    require(
        response.get("provider") == "xai_direct"
        and response.get("requested_model") == expected_model
        and response.get("actual_model") == expected_model,
        "RI semantic response provenance or completion status is invalid",
    )


def validate_panel_artifact(
    panel: Any,
    *,
    ledger_entries: list[dict[str, Any]],
    responses_by_entry_id: dict[str, dict[str, Any]],
    fusion_result: dict[str, Any],
) -> None:
    require(
        isinstance(panel, dict)
        and set(panel) == {"attempts", "degraded", "failed_count", "live_count", "results"},
        "RI panel artifact schema is invalid",
    )
    attempts = panel.get("attempts")
    results = panel.get("results")
    require(
        panel.get("degraded") is False
        and panel.get("failed_count") == 0
        and panel.get("live_count") == 3
        and isinstance(attempts, list)
        and isinstance(results, list)
        and len(attempts) == len(results) == 3,
        "RI benchmark panel did not complete all three seats",
    )
    expected_seats = {
        "grok45_researcher": "grok-4.5",
        "grok45_adversary": "grok-4.5",
        "grok45_constraint_auditor": "grok-4.5",
    }
    row_keys = {
        "anonymous_label",
        "error",
        "response",
        "response_evidence",
        "role",
        "seat_name",
        "status",
    }
    attempt_by_seat: dict[str, dict[str, Any]] = {}
    result_by_seat: dict[str, dict[str, Any]] = {}
    for rows, destination, expected_labels in (
        (attempts, attempt_by_seat, {""}),
        (results, result_by_seat, {"Seat A", "Seat B", "Seat C"}),
    ):
        observed_labels: set[str] = set()
        for row in rows:
            require(isinstance(row, dict) and set(row) == row_keys, "RI panel row schema is invalid")
            seat = row.get("seat_name")
            require(
                seat in expected_seats and seat not in destination,
                "RI panel seat is unexpected or duplicated",
            )
            require(
                row.get("role") == "panel"
                and row.get("status") == "completed"
                and row.get("error") is None,
                "RI panel seat did not complete cleanly",
            )
            label = row.get("anonymous_label")
            require(label in expected_labels, "RI panel anonymous label is invalid")
            observed_labels.add(label)
            destination[seat] = row
        require(set(destination) == set(expected_seats), "RI panel seat set is invalid")
        if rows is results:
            require(observed_labels == expected_labels, "RI panel anonymous labels are not unique")
    for seat, expected_model in expected_seats.items():
        attempt_row = attempt_by_seat[seat]
        result_row = result_by_seat[seat]
        require(
            {key: value for key, value in attempt_row.items() if key != "anonymous_label"}
            == {key: value for key, value in result_row.items() if key != "anonymous_label"},
            "RI panel attempt/result evidence diverged",
        )
        validate_semantic_response(
            result_row["response"],
            result_row["response_evidence"],
            ledger_entries=ledger_entries,
            responses_by_entry_id=responses_by_entry_id,
            expected_stage="panel",
            expected_seat=seat,
            expected_model=expected_model,
        )
        validate_fusion_quality(
            result_row["response"]["text"], f"RI panel response for {seat}"
        )
    require(fusion_result.get("panel") == results, "Fusion result panel differs from panel.json")


def validate_judge_artifact(
    judge_artifact: Any,
    *,
    ledger_entries: list[dict[str, Any]],
    responses_by_entry_id: dict[str, dict[str, Any]],
    fusion_result: dict[str, Any],
) -> None:
    require(
        isinstance(judge_artifact, dict)
        and set(judge_artifact) == {"judgment", "response", "response_evidence"},
        "RI judge artifact schema is invalid",
    )
    judgment = judge_artifact.get("judgment")
    fusion_config = _benchmark_profile().get("fusion")
    require(isinstance(fusion_config, dict), "Pinned fusion configuration is missing")
    required_judge_fields = fusion_config.get("judge_required_fields")
    require(
        isinstance(required_judge_fields, list)
        and bool(required_judge_fields)
        and all(isinstance(field_name, str) for field_name in required_judge_fields),
        "Pinned judge field contract is invalid",
    )
    require(
        isinstance(judgment, dict) and set(judgment) == set(required_judge_fields),
        "RI judgment schema is invalid",
    )
    for field_name in required_judge_fields:
        field_value = judgment[field_name]
        require(
            isinstance(field_value, list)
            and all(isinstance(item, str) for item in field_value),
            f"RI judgment {field_name} is not a string list",
        )
    response = judge_artifact.get("response")
    validate_semantic_response(
        response,
        judge_artifact.get("response_evidence"),
        ledger_entries=ledger_entries,
        responses_by_entry_id=responses_by_entry_id,
        expected_stage="judge",
        expected_seat="grok45_judge",
        expected_model="grok-4.5",
    )
    try:
        raw_judgment = json.loads(response["text"])
    except json.JSONDecodeError as exc:
        raise EvidenceError("RI judge raw response is not JSON") from exc
    require(raw_judgment == judgment, "RI judge judgment differs from its raw response")
    require(fusion_result.get("judge") == judgment, "Fusion result judge differs from judge.json")


def validate_synthesis_artifact(
    synthesis_artifact: Any,
    *,
    ledger_entries: list[dict[str, Any]],
    responses_by_entry_id: dict[str, dict[str, Any]],
    expected_stage: str,
) -> str:
    require(
        isinstance(synthesis_artifact, dict)
        and set(synthesis_artifact)
        == {"author_seat", "mode", "response", "response_evidence", "sha256", "text"},
        "RI synthesis artifact schema is invalid",
    )
    text = synthesis_artifact.get("text")
    require(isinstance(text, str) and bool(text), "RI synthesis text is missing")
    validate_fusion_quality(text, f"RI {expected_stage} synthesis")
    require(
        synthesis_artifact.get("mode") == "client_orchestrated"
        and synthesis_artifact.get("author_seat") == "grok45_synthesizer",
        "RI synthesis provenance is invalid",
    )
    require(
        synthesis_artifact.get("sha256") == sha256(text.encode("utf-8")).hexdigest(),
        "RI synthesis text hash mismatch",
    )
    response = synthesis_artifact.get("response")
    validate_semantic_response(
        response,
        synthesis_artifact.get("response_evidence"),
        ledger_entries=ledger_entries,
        responses_by_entry_id=responses_by_entry_id,
        expected_stage=expected_stage,
        expected_seat="grok45_synthesizer",
        expected_model="grok-4.5",
    )
    require(response.get("text") == text, "RI synthesis text differs from its raw response")
    return text


def validate_gate_artifact(
    gate: Any,
    *,
    ledger_entries: list[dict[str, Any]],
    responses_by_entry_id: dict[str, dict[str, Any]],
    expected_gate_stage: str,
    expected_hash: str | None = None,
    expected_passed: bool = True,
) -> None:
    require(isinstance(gate, dict), "RI gate artifact must be an object")
    full_gate_keys = {
        "artifact_sha256",
        "blind_spot_blocked",
        "deterministic_blockers",
        "enabled",
        "fail_closed",
        "mechanical_blocked",
        "mechanical_failures",
        "negative_verdict_blocked",
        "negative_verdicts",
        "pass_count",
        "passed",
        "required_passes",
        "reviewers",
        "schema_blocked",
        "schema_failures",
        "unresolved_blind_spots",
    }
    independent_amendment_rejection_keys = {
        "artifact_sha256",
        "deterministic_blockers",
        "enabled",
        "fail_closed",
        "pass_count",
        "passed",
        "required_passes",
        "reviewers",
    }
    require(gate.get("enabled") is True, "RI benchmark gate is not enabled")
    require(gate.get("passed") is expected_passed, "RI gate pass state is invalid")
    artifact_hash = gate.get("artifact_sha256")
    require(isinstance(artifact_hash, str) and HASH.match(artifact_hash), "Bad gate artifact hash")
    if expected_hash is not None:
        require(artifact_hash == expected_hash, "RI gate artifact hash mismatch")
    pass_count = gate.get("pass_count")
    required_passes = gate.get("required_passes")
    require(
        required_passes == 2
        and isinstance(pass_count, int)
        and not isinstance(pass_count, bool)
        and (pass_count == 2 if expected_passed else 0 <= pass_count < 2),
        "RI gate quorum is invalid",
    )
    reviewers = gate.get("reviewers")
    if set(gate) == independent_amendment_rejection_keys:
        require(
            not expected_passed
            and re.fullmatch(r"gate-[12]", expected_gate_stage) is not None
            and gate.get("fail_closed") is True
            and pass_count == 0
            and reviewers == [],
            "RI independent-amendment rejection shape is invalid",
        )
        blockers = gate.get("deterministic_blockers")
        require(
            blockers
            == [
                "The amendment is byte-identical to the rejected artifact; a fresh corrected artifact is required."
            ],
            "RI independent-amendment rejection has an unexpected blocker",
        )
        return
    require(set(gate) == full_gate_keys, "RI gate artifact schema is invalid")
    require(gate.get("fail_closed") is True, "RI benchmark gate is not fail-closed")
    require(
        isinstance(reviewers, list) and len(reviewers) == len(EXPECTED_GATE_REVIEWERS),
        "RI gate reviewer count is invalid",
    )
    reviewer_seats: set[str] = set()
    observed_passes = 0
    expected_negative_verdicts: list[dict[str, Any]] = []
    expected_blind_spots: list[str] = []
    verdict_keys = {
        "artifact_sha256",
        "blind_spots",
        "blocking_findings",
        "criteria_reviewed",
        "evidence",
        "non_blocking_findings",
        "required_actions",
        "summary",
        "verdict",
    }
    string_list_fields = (
        "criteria_reviewed",
        "blind_spots",
        "blocking_findings",
        "non_blocking_findings",
        "evidence",
        "required_actions",
    )
    for reviewer in reviewers:
        require(
            isinstance(reviewer, dict)
            and set(reviewer)
            == {"response", "response_evidence", "seat_name", "status", "verdict"},
            "Bad RI gate reviewer schema",
        )
        seat = reviewer.get("seat_name")
        expected_model = EXPECTED_GATE_REVIEWERS.get(seat)
        require(expected_model is not None, "RI gate used an unexpected reviewer")
        require(seat not in reviewer_seats, "RI gate duplicated a reviewer")
        reviewer_seats.add(seat)
        require(reviewer.get("status") == "completed", "RI reviewer did not complete")
        verdict = reviewer.get("verdict")
        require(
            isinstance(verdict, dict) and set(verdict) == verdict_keys,
            "RI reviewer verdict schema is invalid",
        )
        verdict_label = verdict.get("verdict")
        reviewer_hash = verdict.get("artifact_sha256")
        require(
            verdict_label in {"PASS", "FAIL", "NEEDS_WORK"}
            and (not expected_passed or verdict_label == "PASS"),
            "RI reviewer verdict is invalid for the gate outcome",
        )
        require(isinstance(verdict.get("summary"), str), "RI reviewer verdict summary is invalid")
        for field_name in string_list_fields:
            field_value = verdict.get(field_name)
            require(
                isinstance(field_value, list)
                and all(isinstance(value, str) for value in field_value),
                f"RI reviewer verdict {field_name} is invalid",
            )
        require(
            bool(verdict["criteria_reviewed"]),
            "RI reviewer verdict has no reviewed criteria",
        )
        if verdict_label == "PASS":
            require(
                verdict["blind_spots"] == []
                and verdict["blocking_findings"] == []
                and verdict["required_actions"] == [],
                "RI PASS verdict contains a blocking finding",
            )
        else:
            expected_negative_verdicts.append(
                {
                    "seat_name": seat,
                    "verdict": verdict_label,
                    "summary": verdict["summary"],
                    "blocking_findings": verdict["blocking_findings"],
                    "required_actions": verdict["required_actions"],
                    "evidence": verdict["evidence"],
                }
            )
        expected_blind_spots.extend(verdict["blind_spots"])
        observed_passes += int(verdict_label == "PASS")
        require(reviewer_hash == artifact_hash, "RI reviewer checked a different artifact")
        response = reviewer.get("response")
        validate_model_response(response, "RI reviewer response")
        require(
            response.get("provider") == "xai_direct"
            and response.get("requested_model") == expected_model
            and response.get("actual_model") == expected_model,
            f"RI reviewer response provenance mismatch for {seat}",
        )
        response_text = response.get("text")
        require(isinstance(response_text, str), "RI reviewer raw verdict text is missing")
        try:
            raw_verdict = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise EvidenceError("RI reviewer raw verdict is not JSON") from exc
        require(raw_verdict == verdict, "RI reviewer verdict differs from its raw response")
        evidence = reviewer.get("response_evidence")
        require(
            isinstance(evidence, dict)
            and set(evidence)
            == {
                "schema_version",
                "entry_id",
                "attempt_id",
                "invocation_sha256",
                "response_sha256",
            }
            and evidence.get("schema_version") == 1,
            "RI reviewer response receipt schema is invalid",
        )
        for key in ("entry_id", "attempt_id", "invocation_sha256", "response_sha256"):
            require(
                isinstance(evidence.get(key), str) and HASH.match(evidence[key]),
                f"Bad RI reviewer receipt {key}",
            )
        require(
            evidence["response_sha256"] == canonical_json_hash(response),
            "RI reviewer response hash mismatch",
        )
        matching_entries = [
            entry for entry in ledger_entries if entry.get("entry_id") == evidence["entry_id"]
        ]
        require(len(matching_entries) == 1, "RI reviewer receipt lacks one ledger entry")
        ledger_entry = matching_entries[0]
        require(
            responses_by_entry_id.get(evidence["entry_id"]) == response,
            "RI gate response differs from its raw-response artifact",
        )
        for key in ("entry_id", "attempt_id", "invocation_sha256", "response_sha256"):
            require(
                ledger_entry.get(key) == evidence[key],
                f"RI reviewer receipt {key} differs from its ledger entry",
            )
        require(
            ledger_entry.get("stage") == expected_gate_stage
            and ledger_entry.get("seat") == seat
            and ledger_entry.get("provider") == "xai_direct"
            and ledger_entry.get("requested_model") == expected_model
            and ledger_entry.get("actual_model") == expected_model,
            "RI reviewer ledger provenance mismatch",
        )
    require(reviewer_seats == set(EXPECTED_GATE_REVIEWERS), "RI gate reviewer set is invalid")
    require(observed_passes == pass_count, "RI gate pass_count differs from reviewer verdicts")
    require(
        gate.get("mechanical_failures") == [] and gate.get("mechanical_blocked") is False,
        "RI benchmark gate contradicts its successful mechanical evidence",
    )
    require(
        gate.get("schema_failures") == [] and gate.get("schema_blocked") is False,
        "RI benchmark gate records a schema failure",
    )
    require(
        gate.get("negative_verdicts") == expected_negative_verdicts
        and gate.get("negative_verdict_blocked") is bool(expected_negative_verdicts),
        "RI gate negative-verdict summary is inconsistent",
    )
    require(
        gate.get("unresolved_blind_spots") == expected_blind_spots
        and gate.get("blind_spot_blocked") is bool(expected_blind_spots),
        "RI gate blind-spot summary is inconsistent",
    )
    expected_blockers: list[str] = []
    if expected_negative_verdicts:
        expected_blockers.append(
            "At least one reviewer returned a blocking negative verdict: "
            + "; ".join(
                f"{review['seat_name']}: {review['verdict']}"
                for review in expected_negative_verdicts
            )
        )
    if expected_blind_spots:
        expected_blockers.append(
            "Targeted review is required for unresolved blind spots: "
            + "; ".join(expected_blind_spots)
        )
    require(
        gate.get("deterministic_blockers") == expected_blockers,
        "RI gate deterministic blockers are inconsistent",
    )
    recomputed_passed = (
        observed_passes >= required_passes
        and not expected_negative_verdicts
        and not expected_blind_spots
    )
    require(
        gate.get("passed") is recomputed_passed,
        "RI gate pass state contradicts its reviewer evidence",
    )


def validate_ri_runs(
    root: Path,
    entries: Any,
    codex_contract: dict[str, Any],
) -> tuple[list[Any], set[str], set[str], set[str]]:
    require(
        isinstance(entries, list) and len(entries) == len(EXPECTED_RUN_IDS),
        "Expected exactly one fuse plus five lifecycle RI runs",
    )
    loaded: list[Any] = []
    entries_by_run_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        require(
            isinstance(entry, dict) and set(entry) == {"run_id", "manifest", "ledger"},
            "Bad RI run reference",
        )
        run_id = entry.get("run_id")
        require(isinstance(run_id, str) and run_id, "RI run reference has no run_id")
        require(run_id not in entries_by_run_id, f"Duplicate RI run reference: {run_id}")
        entries_by_run_id[run_id] = entry
    require(
        set(entries_by_run_id) == set(EXPECTED_RUN_IDS.values()),
        "RI run ids do not match the deterministic lifecycle contract",
    )

    task_hashes: set[str] = set()
    input_hashes: set[str] = set()
    config_hashes: set[str] = set()
    global_attempt_ids: set[str] = set()
    global_entry_ids: set[str] = set()
    global_request_ids: set[str] = set()
    decoded_by_stage = codex_contract["decoded_by_stage"]
    arguments_by_stage = codex_contract["arguments_by_stage"]
    artifact_hashes = codex_contract["artifact_hashes"]
    for stage, expected_run_id in EXPECTED_RUN_IDS.items():
        entry = entries_by_run_id[expected_run_id]
        manifest_path = resolve_evidence_path(root, entry.get("manifest"))
        ledger_path = resolve_evidence_path(root, entry.get("ledger"))
        require(
            ledger_path.parent == manifest_path.parent,
            "RI ledger is not colocated with its manifest",
        )
        require(manifest_path.stat().st_mode & 0o077 == 0, "RI manifest is not owner-only")
        require(ledger_path.stat().st_mode & 0o077 == 0, "RI ledger is not owner-only")
        manifest = load_json(manifest_path)
        ledger = load_json(ledger_path)
        loaded.extend((manifest, ledger))
        require(
            isinstance(manifest, dict) and set(manifest) == MANIFEST_FIELDS,
            "RI manifest schema is invalid",
        )
        require(manifest.get("run_id") == expected_run_id, "RI manifest run_id mismatch")
        require(manifest_path.parent.name == expected_run_id, "RI run directory mismatch")
        require(manifest.get("status") == "completed", "RI manifest is not completed")
        for key in ("task_hash", "config_hash", "input_hash"):
            require(isinstance(manifest.get(key), str) and HASH.match(manifest[key]), f"Bad RI {key}")
        expected_task_hash, expected_input_hash = expected_run_identity(
            stage, arguments_by_stage[stage]
        )
        require(
            manifest["task_hash"] == expected_task_hash
            and manifest["input_hash"] == expected_input_hash,
            "RI manifest task/input identity differs from the exact Codex call",
        )
        require(
            manifest["config_hash"] == expected_benchmark_config_hash(),
            "RI run did not use the exact pinned benchmark configuration",
        )
        created_at = parse_utc_timestamp(manifest.get("created_at"), "RI manifest created_at")
        updated_at = parse_utc_timestamp(manifest.get("updated_at"), "RI manifest updated_at")
        require(created_at <= updated_at, "RI manifest timestamps are not chronological")
        stages = manifest.get("stages")
        require(isinstance(stages, dict) and stages, "RI manifest has no stages")
        for stage_name, stage_receipt in stages.items():
            require(
                isinstance(stage_name, str)
                and bool(stage_name)
                and isinstance(stage_receipt, dict)
                and set(stage_receipt) == {"artifact", "status", "updated_at"}
                and isinstance(stage_receipt.get("artifact"), str)
                and bool(stage_receipt["artifact"])
                and isinstance(stage_receipt.get("status"), str)
                and bool(stage_receipt["status"]),
                f"RI manifest stage receipt is invalid: {stage_name}",
            )
            stage_updated_at = parse_utc_timestamp(
                stage_receipt.get("updated_at"), f"RI manifest {stage_name} updated_at"
            )
            require(
                created_at <= stage_updated_at <= updated_at,
                f"RI manifest stage timestamp is not chronological: {stage_name}",
            )
        ledger_entries, responses_by_entry_id, invocations_by_call = validate_ledger(
            ledger,
            fusion_run=stage == "fuse",
            manifest_path=manifest_path,
            expected_run_id=expected_run_id,
            expected_input_hash=manifest["input_hash"],
            expected_config_hash=manifest["config_hash"],
            global_attempt_ids=global_attempt_ids,
            global_entry_ids=global_entry_ids,
            global_request_ids=global_request_ids,
        )
        task_hashes.add(manifest["task_hash"])
        input_hashes.add(manifest["input_hash"])
        config_hashes.add(manifest["config_hash"])
        if stage == "fuse":
            amendment_rounds = sorted(
                {
                    int(match.group(1))
                    for ledger_entry in ledger_entries
                    for match in [
                        re.fullmatch(r"amendment-([12])", str(ledger_entry.get("stage")))
                    ]
                    if match is not None
                }
            )
            require(
                amendment_rounds == list(range(1, len(amendment_rounds) + 1)),
                "Fusion amendment rounds are not sequential",
            )
            expected_stage_names = {"panel", "judge", "synthesis", "gate-0"}
            for round_index in amendment_rounds:
                expected_stage_names.update(
                    {f"amendment-{round_index}", f"gate-{round_index}"}
                )
            require(
                set(stages) == expected_stage_names,
                "Fusion manifest stage set does not match its ledger rounds",
            )
            semantic_artifact_names = {
                "panel": "panel.json",
                "judge": "judge.json",
                "synthesis": "synthesis.json",
                **{
                    f"amendment-{round_index}": f"synthesis-amendment-{round_index}.json"
                    for round_index in amendment_rounds
                },
            }
            for stage_name, artifact_name in semantic_artifact_names.items():
                stage_receipt = stages.get(stage_name)
                require(
                    isinstance(stage_receipt, dict)
                    and stage_receipt.get("status") == "completed"
                    and stage_receipt.get("artifact") == artifact_name,
                    f"Fusion semantic stage receipt is invalid: {stage_name}",
                )

            _, fusion_result = load_run_file(root, manifest_path, "result.json")
            _, handoff = load_run_file(root, manifest_path, "execution-handoff.json")
            require(
                isinstance(fusion_result, dict) and set(fusion_result) == FUSION_RESULT_FIELDS,
                "Fusion result artifact schema is invalid",
            )
            expected_artifacts_dir = (
                f"/logs/agent/relentless-inception/runs/{expected_run_id}"
            )
            require(
                fusion_result.get("artifacts_dir") == expected_artifacts_dir,
                "Fusion result artifacts_dir is not the retained benchmark run directory",
            )
            require(
                decoded_by_stage[stage] == fusion_result,
                "Codex fusion result differs from its persisted result",
            )
            require(
                fusion_result.get("config_hash") == manifest["config_hash"]
                and fusion_result.get("task_hash") == manifest["task_hash"],
                "Fusion result identity hashes differ from its manifest",
            )
            _, panel_artifact = load_run_file(
                root, manifest_path, stages["panel"].get("artifact")
            )
            _, judge_artifact = load_run_file(
                root, manifest_path, stages["judge"].get("artifact")
            )
            _, base_synthesis_artifact = load_run_file(
                root, manifest_path, stages["synthesis"].get("artifact")
            )
            validate_panel_artifact(
                panel_artifact,
                ledger_entries=ledger_entries,
                responses_by_entry_id=responses_by_entry_id,
                fusion_result=fusion_result,
            )
            validate_judge_artifact(
                judge_artifact,
                ledger_entries=ledger_entries,
                responses_by_entry_id=responses_by_entry_id,
                fusion_result=fusion_result,
            )
            synthesis_texts = [
                validate_synthesis_artifact(
                    base_synthesis_artifact,
                    ledger_entries=ledger_entries,
                    responses_by_entry_id=responses_by_entry_id,
                    expected_stage="synthesis",
                )
            ]
            amendment_artifacts: list[Any] = []
            for round_index in amendment_rounds:
                _, amendment_artifact = load_run_file(
                    root,
                    manifest_path,
                    stages[f"amendment-{round_index}"].get("artifact"),
                )
                amendment_artifacts.append(amendment_artifact)
                synthesis_texts.append(
                    validate_synthesis_artifact(
                        amendment_artifact,
                        ledger_entries=ledger_entries,
                        responses_by_entry_id=responses_by_entry_id,
                        expected_stage=f"amendment-{round_index}",
                    )
                )
            require(
                fusion_result.get("synthesis") == synthesis_texts[-1],
                "Fusion result synthesis differs from the latest synthesis artifact",
            )

            gate_names = ["gate-0", *[f"gate-{round_index}" for round_index in amendment_rounds]]
            gate_artifacts: list[Any] = []
            for gate_position, gate_name in enumerate(gate_names):
                gate_stage = stages.get(gate_name)
                expected_passed = gate_position == len(gate_names) - 1
                require(
                    isinstance(gate_stage, dict)
                    and gate_stage.get("status")
                    == ("passed" if expected_passed else "rejected")
                    and gate_stage.get("artifact") == f"{gate_name}.json",
                    f"Fusion gate stage receipt is invalid: {gate_name}",
                )
                _, gate_artifact = load_run_file(
                    root,
                    manifest_path,
                    gate_stage.get("artifact"),
                )
                gate_artifacts.append(gate_artifact)
                validate_gate_artifact(
                    gate_artifact,
                    ledger_entries=ledger_entries,
                    responses_by_entry_id=responses_by_entry_id,
                    expected_gate_stage=("gate" if gate_name == "gate-0" else gate_name),
                    expected_hash=sha256(
                        synthesis_texts[gate_position].encode("utf-8")
                    ).hexdigest(),
                    expected_passed=expected_passed,
                )
            expected_invocations = expected_fusion_invocations(
                run_id=expected_run_id,
                input_sha256=manifest["input_hash"],
                config_sha256=manifest["config_hash"],
                arguments=arguments_by_stage[stage],
                panel_artifact=panel_artifact,
                judge_artifact=judge_artifact,
                synthesis_artifacts=[
                    base_synthesis_artifact,
                    *amendment_artifacts,
                ],
                gate_artifacts=gate_artifacts,
            )
            validate_expected_invocations(
                invocations_by_call,
                expected_invocations,
                label="Fusion",
            )
            final_gate = gate_artifacts[-1]
            final_gate_name = gate_names[-1]
            final_gate_stage = stages[final_gate_name]
            require(
                isinstance(final_gate_stage, dict)
                and final_gate_stage.get("status") == "passed",
                "Final fusion gate stage did not pass",
            )
            require(
                final_gate.get("artifact_sha256") == artifact_hashes[stage],
                "Final fusion gate is bound to a different returned synthesis",
            )
            loaded.extend(
                (
                    panel_artifact,
                    judge_artifact,
                    base_synthesis_artifact,
                    *amendment_artifacts,
                    *gate_artifacts,
                    fusion_result,
                    handoff,
                )
            )
            require(fusion_result.get("run_id") == expected_run_id, "Fusion result run_id mismatch")
            require(fusion_result.get("status") == "completed", "Fusion result is not completed")
            require(fusion_result.get("gate", {}).get("passed") is True, "Fusion result gate is not passed")
            require(fusion_result.get("gate") == final_gate, "Fusion result gate differs from its artifact")
            require(fusion_result.get("ledger") == ledger, "Fusion result ledger differs from its artifact")
            require(
                fusion_result.get("execution_handoff") == handoff,
                "Fusion result and persisted handoff differ",
            )
            synthesis = fusion_result.get("synthesis")
            require(isinstance(synthesis, str) and synthesis, "Fusion result synthesis is missing")
            require(
                handoff.get("artifacts", {}).get("fused_plan") == synthesis,
                "Fusion handoff does not contain the exact fused synthesis",
            )
            require(handoff.get("run_id") == expected_run_id, "Fusion handoff run_id mismatch")
            handoff_schema = handoff.get("schema_version")
            require(
                isinstance(handoff_schema, int)
                and not isinstance(handoff_schema, bool)
                and handoff_schema == 2,
                "Unsupported fusion handoff schema",
            )
            require(
                handoff.get("status") == "awaiting_host_gates",
                "Fusion handoff is not awaiting its host-owned gates",
            )
            require(handoff.get("ready") is False, "Fusion handoff ready flag must remain false")
            require(
                handoff.get("ready_for_host_workflow") is True,
                "Fusion handoff is not ready for host workflow",
            )
            require(
                handoff.get("mutation_authorized") is False,
                "Fusion handoff prematurely authorized host mutation",
            )
            require(handoff.get("blocking_reasons") == [], "Fusion handoff has blocking reasons")
            lifecycle = handoff.get("lifecycle")
            require(isinstance(lifecycle, dict), "Fusion handoff lifecycle is missing")
            require(
                lifecycle.get("pending_gates") == ["plan", "pre_execution"],
                "Fusion handoff has the wrong pending host gates",
            )
            require(
                lifecycle.get("later_gates") == ["post_execution", "final", "summarize"],
                "Fusion handoff has the wrong later host gates",
            )
            require(
                lifecycle.get("stage_owner") == "codex_host"
                and lifecycle.get("host_receipts_required") is True,
                "Fusion handoff host-receipt ownership is invalid",
            )
            require(
                handoff.get("synthesis_gate", {}).get("passed") is True,
                "Fusion handoff synthesis gate is not passed",
            )
            require(
                handoff.get("synthesis_gate", {}).get("artifact_sha256")
                == artifact_hashes[stage],
                "Fusion handoff synthesis gate is bound to a different artifact",
            )
            expected_handoff = expected_fusion_handoff(
                run_id=expected_run_id,
                synthesis=synthesis,
                judgment=judge_artifact["judgment"],
                ledger=ledger,
                artifact_sha256=artifact_hashes[stage],
            )
            require(
                handoff == expected_handoff,
                "Fusion handoff differs from the exact pinned host-workflow packet",
            )
        else:
            require(set(stages) == {"gate-0"}, f"{stage} lifecycle run has unexpected stages")
            gate_stage = stages["gate-0"]
            require(
                isinstance(gate_stage, dict) and gate_stage.get("status") == "passed",
                f"{stage} lifecycle gate did not pass",
            )
            _, gate = load_run_file(root, manifest_path, gate_stage.get("artifact"))
            loaded.append(gate)
            validate_gate_artifact(
                gate,
                ledger_entries=ledger_entries,
                responses_by_entry_id=responses_by_entry_id,
                expected_gate_stage="gate",
                expected_hash=artifact_hashes[stage],
            )
            validate_expected_invocations(
                invocations_by_call,
                expected_lifecycle_invocations(
                    run_id=expected_run_id,
                    input_sha256=manifest["input_hash"],
                    config_sha256=manifest["config_hash"],
                    arguments=arguments_by_stage[stage],
                ),
                label=f"{stage} lifecycle",
            )
            require(
                isinstance(decoded_by_stage[stage], dict)
                and set(decoded_by_stage[stage]) == LIFECYCLE_RESULT_FIELDS,
                f"Codex {stage} result schema is invalid",
            )
            require(
                decoded_by_stage[stage].get("run_id") == expected_run_id
                and decoded_by_stage[stage].get("artifacts_dir")
                == f"/logs/agent/relentless-inception/runs/{expected_run_id}",
                f"Codex {stage} result identity is invalid",
            )
            require(
                decoded_by_stage[stage].get("gate") == gate,
                f"Codex {stage} gate differs from its persisted artifact",
            )
            require(
                decoded_by_stage[stage].get("ledger") == ledger,
                f"Codex {stage} ledger differs from its persisted artifact",
            )
    require(len(config_hashes) == 1, "RI lifecycle runs used different configurations")
    require(
        len(task_hashes) == len(EXPECTED_RUN_IDS)
        and len(input_hashes) == len(EXPECTED_RUN_IDS),
        "RI lifecycle task/input identities are not unique",
    )
    return loaded, global_attempt_ids, global_entry_ids, global_request_ids


def validate_deep_swe_reward(task: str, reward: dict[str, Any]) -> None:
    expected = PINS["pier"]["tasks"][task]["expected"]
    binary_reward = reward.get("reward")
    require(
        isinstance(binary_reward, (int, float))
        and not isinstance(binary_reward, bool)
        and binary_reward == 1,
        "DeepSWE binary reward is not numeric 1",
    )
    require(
        "apply_failed" not in reward or reward["apply_failed"] is False,
        "DeepSWE patch did not apply",
    )
    for key, value in expected.items():
        observed = reward.get(key)
        require(
            isinstance(observed, int)
            and not isinstance(observed, bool)
            and observed == value,
            f"DeepSWE {key} mismatch",
        )


def validate_attempt(index_path: Path) -> dict[str, Any]:
    index = load_json(index_path)
    require(isinstance(index, dict), "Evidence index must be an object")
    index_schema = index.get("schema_version")
    require(
        isinstance(index_schema, int)
        and not isinstance(index_schema, bool)
        and index_schema == 1,
        "Unsupported evidence index",
    )
    attempt = index.get("attempt")
    require(
        isinstance(attempt, int)
        and not isinstance(attempt, bool)
        and attempt in {1, 2},
        "Attempt must be integer 1 or 2",
    )
    root = index_path.resolve().parent
    contract = load_json(resolve_evidence_path(root, index.get("contract")))
    result = load_json(resolve_evidence_path(root, index.get("result")))
    trajectory = load_json(resolve_evidence_path(root, index.get("trajectory")))
    codex_log_path = resolve_evidence_path(root, index.get("codex_log"))
    validate_contract(contract, index)
    validate_result(result, index["task"], index["harness"])
    validate_trajectory(trajectory)
    codex_contract = validate_codex_log(codex_log_path, trajectory)
    ri_values, attempt_ids, entry_ids, request_ids = validate_ri_runs(
        root,
        index.get("ri_runs"),
        codex_contract,
    )
    values = [index, contract, result, trajectory, *ri_values]
    if index["harness"] == "pier":
        reward = load_json(resolve_evidence_path(root, index.get("deep_swe_reward")))
        validate_deep_swe_reward(index["task"], reward)
        values.append(reward)
    reject_secrets(values)
    reject_secrets_in_tree(root)
    return {
        "task": index["task"],
        "attempt": index["attempt"],
        "trial_name": result["trial_name"],
        "attempt_ids": attempt_ids,
        "entry_ids": entry_ids,
        "request_ids": request_ids,
        "result_id": result["id"],
        "started_at": result["started_at"],
        "finished_at": result["finished_at"],
    }


def validate_final(root: Path) -> None:
    expected_tasks = [*PINS["harbor"]["tasks"], *PINS["pier"]["tasks"]]
    trial_names: set[str] = set()
    result_ids: set[str] = set()
    entry_ids: set[str] = set()
    request_ids: set[str] = set()
    previous_finish: datetime | None = None
    for task in expected_tasks:
        for attempt in (1, 2):
            path = root / task / f"attempt-{attempt}" / "evidence.json"
            identity = validate_attempt(path)
            require(identity["trial_name"] not in trial_names, "Harness trial was replayed")
            require(
                identity["result_id"] not in result_ids,
                "Harness result id was replayed",
            )
            require(
                entry_ids.isdisjoint(identity["entry_ids"]),
                "RI call receipts were replayed across benchmark attempts",
            )
            require(
                request_ids.isdisjoint(identity["request_ids"]),
                "Provider request ids were replayed across benchmark attempts",
            )
            started_at = datetime.fromisoformat(identity["started_at"].replace("Z", "+00:00"))
            finished_at = datetime.fromisoformat(identity["finished_at"].replace("Z", "+00:00"))
            if previous_finish is not None:
                require(started_at >= previous_finish, "Benchmark attempts were not serialized")
            previous_finish = finished_at
            trial_names.add(identity["trial_name"])
            result_ids.add(identity["result_id"])
            entry_ids.update(identity["entry_ids"])
            request_ids.update(identity["request_ids"])


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    attempt_parser = subparsers.add_parser("attempt")
    attempt_parser.add_argument("evidence", type=Path)
    final_parser = subparsers.add_parser("final")
    final_parser.add_argument("evidence_root", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "attempt":
            validate_attempt(args.evidence)
            print(f"clean attempt: {args.evidence}")
        else:
            validate_final(args.evidence_root)
            print("clean attempts: 2 consecutive per task (each independently pass@1)")
    except EvidenceError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
