"""Bounded independent deliberation, comparative analysis, synthesis, and gates."""

from __future__ import annotations

import json
import math
import random
import re
import string
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .config import PLUGIN_ROOT, active_profile, load_config
from .errors import BudgetExceeded, ConfigError, ProviderError, RunAborted
from .execution import build_handoff
from .prompts import (
    gate_prompt,
    gate_system,
    judge_prompt,
    judge_system,
    panel_prompt,
    panel_system,
    synthesis_prompt,
    synthesis_system,
)
from .providers import ProviderRegistry, parse_json_object
from .state import (
    BudgetTracker,
    RunStore,
    call_receipt_entry_id,
    canonical_json_hash,
    text_hash,
)
from .types import FusionResult, ModelResponse, SeatResult


def _load_schema(name: str) -> Dict[str, Any]:
    path = PLUGIN_ROOT / "schemas" / name
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ConfigError(f"Schema root must be an object: {path}")
    return value


JUDGE_SCHEMA = _load_schema("judge.schema.json")
VERDICT_SCHEMA = _load_schema("verdict.schema.json")
JUDGE_FIELDS = tuple(JUDGE_SCHEMA["required"])
VERDICT_FIELDS = tuple(VERDICT_SCHEMA["required"])
MODEL_RESPONSE_FIELDS = (
    "text",
    "provider",
    "requested_model",
    "actual_model",
    "usage",
    "latency_seconds",
    "request_id",
    "route",
    "raw_status",
)
USAGE_FIELDS = (
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
)
PANEL_RESULT_FIELDS = (
    "seat_name",
    "anonymous_label",
    "role",
    "status",
    "response",
    "response_evidence",
    "error",
)
FAILED_PANEL_RESULT_FIELDS = (*PANEL_RESULT_FIELDS, "attempt_ids")
RESPONSE_EVIDENCE_FIELDS = (
    "schema_version",
    "entry_id",
    "attempt_id",
    "invocation_sha256",
    "response_sha256",
)


def _native_openrouter_judgment() -> Dict[str, Any]:
    return {
        "consensus": [],
        "contradictions": [],
        "partial_coverage": [],
        "unique_insights": [],
        "minority_findings": [],
        "blind_spots": ["Native OpenRouter Fusion does not expose raw inner-seat artifacts."],
        "verification_priorities": ["Apply an independent external adversarial gate."],
        "final_guidance": [],
    }


def _mechanical_failures(mechanical_evidence: str) -> List[str]:
    """Extract only explicit deterministic failures; ambiguous prose is left to reviewers."""

    failures: List[str] = []
    stripped = mechanical_evidence.strip()
    if not stripped:
        return failures

    def integer_value(value: Any) -> Optional[int]:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
            return int(value)
        return None

    def reports_problem(value: Any) -> bool:
        if value is None or value is False:
            return False
        if value is True:
            return True
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            normalized_value = value.strip().lower()
            if normalized_value in {
                "",
                "0",
                "false",
                "n/a",
                "na",
                "no error",
                "no errors",
                "none",
                "not applicable",
                "null",
                "ok",
                "success",
            }:
                return False
            if re.fullmatch(
                r"(?:0|no|zero)\s+(?:tests?\s+)?(?:errors?|failed|failures?)",
                normalized_value,
            ):
                return False
            numeric_value = integer_value(value)
            return numeric_value != 0 if numeric_value is not None else True
        if isinstance(value, Mapping):
            return any(reports_problem(child) for child in value.values())
        if isinstance(value, list):
            return any(reports_problem(child) for child in value)
        return True

    def inspect_text(value: str, *, inspect_terminal_failures: bool = True) -> None:
        # Canonical benchmark transcripts prefix command echoes with ``$ `` and
        # record the command's actual status separately as ``[exit N]``. Shell
        # source such as a guarded ``then exit 1`` is not execution evidence;
        # scanning it would contradict the authoritative status marker.
        diagnostic_value = "\n".join(
            "" if line.lstrip().startswith("$ ") else line
            for line in value.splitlines()
        )
        for match in re.finditer(
            r"\b(?:exit(?:ed)?|return(?:ed)?)\s*(?:with\s+)?(?:status|code)?\s*[:=]?\s*(-?\d+)\b"
            r"|\b(?:exit_code|exit_status|returncode|return_code)\s*[:=]\s*[\"']?(-?\d+)\b",
            diagnostic_value,
            flags=re.IGNORECASE,
        ):
            rendered_code = match.group(1) or match.group(2)
            if rendered_code is not None and int(rendered_code) != 0:
                failures.append(match.group(0))

        counted_failure_pattern = re.compile(
            r"\b(\d+)\s+(?:(?:tests?|examples?)\s+)?(?:failed|failures?)\b"
            r"|\b(?:tests?_?)?fail(?:ed|ures?)\s*[:=]\s*(\d+)\b",
            flags=re.IGNORECASE,
        )
        for match in counted_failure_pattern.finditer(diagnostic_value):
            rendered_count = match.group(1) or match.group(2)
            if rendered_count is not None and int(rendered_count) > 0:
                failures.append(match.group(0))

        residual = counted_failure_pattern.sub("", diagnostic_value)
        counted_error_pattern = re.compile(
            r"\b(\d+)\s+(?:tests?\s+)?errors?\b"
            r"|\b(?:tests?_?)?errors?\s*[:=]\s*(\d+)\b",
            flags=re.IGNORECASE,
        )
        for match in counted_error_pattern.finditer(residual):
            rendered_count = match.group(1) or match.group(2)
            if rendered_count is not None and int(rendered_count) > 0:
                failures.append(match.group(0))
        residual = counted_error_pattern.sub("", residual)
        line_failure_count_pattern = re.compile(
            r"(?:^|\n)\s*(?:ℹ\s*)?fail\s+(\d+)\b[^\n]*",
            flags=re.IGNORECASE,
        )
        for match in line_failure_count_pattern.finditer(residual):
            if int(match.group(1)) > 0:
                failures.append(match.group(0).strip())
        residual = line_failure_count_pattern.sub("", residual)
        residual = re.sub(
            r"\b(?:no|zero)\s+(?:tests?\s+)?(?:failed|errors?)\b",
            "",
            residual,
            flags=re.IGNORECASE,
        )
        explicit_failure = re.search(
            r"\b(?:assertion\s+failure|tests?\s+(?:failed|failing)|pytest\s+(?:failed|failing)|build\s+(?:failed|failure)|command\s+failed)\b"
            r"|\btest result:\s*FAILED\b"
            r"|(?:^|\n)\s*FAIL(?:ED)?(?:\b|:)"
            r"|(?:^|\n)\s*-{3}\s+FAIL:\s+\S+"
            r"|\bERROR\s+collecting\s+\S+"
            r"|\bERROR\s+at\s+(?:setup|teardown)\b"
            r"|(?:^|\n)\s*=+\s*ERRORS?\s*=+"
            r"|(?:^|\n)\s*\[(?:ERROR|FATAL)\]"
            r"|(?:^|\n)\s*##\[(?:error|fatal)\]"
            r"|(?:^|\n)\s*INTERNALERROR(?:>|:)"
            r"|(?:^|\n)\s*!*\s*Interrupted:"
            r"|(?:^|\n)\s*(?:g?make)(?:\[\d+\])?:\s+\*{3}"
            r"|(?:^|\n)\s*npm\s+(?:ERR!|error)\b"
            r"|(?:^|\n)\s*not ok\b"
            r"|(?:^|\n)\s*Bail out!"
            r"|(?:^|\n)\s*(?:fatal\s+)?error(?:\[[^\]\n]+\])?:"
            r"|(?:^|\n)[^\n]+(?:\(\d+,\d+\)|:\d+(?::\d+)?)\s*:\s*(?:fatal\s+)?error(?:\s+[A-Z]+\d+)?\s*:"
            r"|(?:^|\n)\s*CMake Error(?: at [^:\n]+)?:"
            r"|(?:^|\n)\s*fatal:",
            residual,
            flags=re.IGNORECASE,
        )
        if explicit_failure:
            failures.append(explicit_failure.group(0))
        if inspect_terminal_failures:
            terminal_failure_pattern = re.compile(
                r"(?:^|\n)\s*(?:[A-Za-z_][\w.]*(?:Error|Exception)|Error|Exception)(?:\s*\[[^\]\n]+\])?:\s*\S+"
                r"|(?:^|\n)\s*(?:KeyboardInterrupt|SystemExit)(?::\s*\S+|(?=\s*(?:\n|$)))"
                r"|(?:^|\n)\s*Exception in thread\s+['\"][^'\"]+['\"]\s+[\w.$]*(?:Error|Exception)(?::\s*\S+|(?=\s*(?:\n|$)))"
                r"|(?:^|\n)\s*(?:UnhandledPromiseRejection(?:Warning|Error)?|ERR_UNHANDLED_REJECTION)(?:\b|:)"
                r"|(?:^|\n)\s*panic:\s*\S+"
                r"|(?:^|\n)\s*thread\s+['\"][^'\"]+['\"]\s+panicked\s+at\b"
                r"|(?:^|\n)\s*(?:Aborted|Bus error|Segmentation fault)(?::\s*\d+)?(?:\s+\(core dumped\))?(?=\s|$)",
                flags=re.IGNORECASE,
            )
            terminal_failure = terminal_failure_pattern.search(value)
            if terminal_failure:
                failures.append(terminal_failure.group(0).strip())

    try:
        structured_evidence = json.loads(stripped)
    except json.JSONDecodeError:
        structured_evidence = None

    def inspect(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            normalized = {str(key).lower(): child for key, child in value.items()}
            compact_normalized = {
                re.sub(r"[-_]", "", str(key).lower()): child
                for key, child in value.items()
            }
            for boolean_key in ("passed", "success", "ok"):
                boolean_value = normalized.get(boolean_key)
                if boolean_value is False or (
                    isinstance(boolean_value, str)
                    and boolean_value.strip().lower() == "false"
                ):
                    failures.append(f"{path}.{boolean_key}=false")
            failed_value = normalized.get("failed")
            if failed_value is True or (
                isinstance(failed_value, str)
                and failed_value.strip().lower() == "true"
            ):
                failures.append(f"{path}.failed=true")
            for numeric_key in ("exit_code", "exit_status", "returncode", "return_code"):
                numeric_value = integer_value(normalized.get(numeric_key))
                if numeric_value is not None and numeric_value != 0:
                    failures.append(f"{path}.{numeric_key}={numeric_value}")
            status = normalized.get("status")
            if isinstance(status, str) and status.strip().lower() in {
                "error",
                "failed",
                "failure",
                "fatal",
            }:
                failures.append(f"{path}.status={status.strip()}")
            for result_key in ("action", "conclusion", "event", "outcome", "result", "state"):
                result_value = normalized.get(result_key)
                if isinstance(result_value, str) and result_value.strip().lower() in {
                    "action_required",
                    "cancelled",
                    "error",
                    "fail",
                    "failed",
                    "failure",
                    "fatal",
                    "startup_failure",
                    "timed_out",
                }:
                    failures.append(f"{path}.{result_key}={result_value.strip()}")
            for problem_key in ("failures", "errors"):
                reported_problems = normalized.get(problem_key)
                if (
                    integer_value(reported_problems) is None
                    and reports_problem(reported_problems)
                ):
                    failures.append(
                        f"{path}.{problem_key} reports one or more problems"
                    )
            for problem_key in ("error", "exception"):
                reported_problem = normalized.get(problem_key)
                if reports_problem(reported_problem):
                    failures.append(f"{path}.{problem_key} reports a problem")
            for compact_count_key in (
                "errorcount",
                "errors",
                "errorscount",
                "failed",
                "failurecount",
                "failures",
                "failurescount",
                "fatalerrorcount",
                "numfailedtests",
                "numfailedtestsuites",
                "numruntimeerrortestsuites",
                "testserrors",
                "testsfailed",
            ):
                failure_count = integer_value(compact_normalized.get(compact_count_key))
                if failure_count is not None and failure_count > 0:
                    failures.append(f"{path}.{compact_count_key}={failure_count}")
            for key, child in value.items():
                inspect(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                inspect(child, f"{path}[{index}]")
        elif isinstance(value, str):
            inspect_text(value, inspect_terminal_failures=False)

    if structured_evidence is not None:
        inspect(structured_evidence, "evidence")
    else:
        inspect_text(stripped)
    return list(dict.fromkeys(failures))


def _panel_context_bundle(
    context: str,
    mechanical_evidence: str,
    bundle_name: str,
    partition_context: bool,
) -> str:
    shared_context = context.strip() or "(none supplied)"
    shared_evidence = mechanical_evidence.strip() or "(none supplied)"
    if not partition_context:
        return f"Shared context:\n{shared_context}\n\nMechanical evidence:\n{shared_evidence}"

    lens_by_bundle = {
        "full_task_and_evidence": "Use the complete supplied context and evidence; seek a self-contained solution.",
        "requirements_risks_and_counterexamples": "Extract requirements, risks, counterexamples, and unsafe assumptions.",
        "requirements_and_mechanical_evidence": "Trace each requirement to the supplied deterministic evidence and flag gaps.",
    }
    lens = lens_by_bundle.get(
        bundle_name,
        f"Apply the explicitly configured context bundle named {bundle_name!r}.",
    )
    return (
        f"Context partition: {bundle_name}\n"
        f"Assigned lens: {lens}\n\n"
        f"Supplied context:\n{shared_context}\n\n"
        f"Mechanical evidence:\n{shared_evidence}"
    )


def _contains_substantive_claim(text: str) -> bool:
    """Reject heading-only or fragment-only output without pretending to fact-check it."""

    for line in text.splitlines():
        candidate = re.sub(r"^\s*(?:#{1,6}\s*|[-*+]\s+|\d+[.)]\s+)", "", line).strip()
        words = re.findall(r"[A-Za-z0-9][A-Za-z0-9_'’-]*", candidate)
        if len(words) >= 6:
            return True
    return False


def _validate_quality_floor(text: str, quality_floor: Mapping[str, Any], label: str) -> None:
    minimum_characters = int(quality_floor.get("minimum_characters", 1))
    if len(text.strip()) < minimum_characters:
        raise ProviderError(
            f"{label} response was below the profile quality floor of {minimum_characters} characters"
        )
    if quality_floor.get("require_nonempty_claims", False) and not _contains_substantive_claim(text):
        raise ProviderError(f"{label} returned no substantive claim or recommendation")
    if quality_floor.get("reject_tool_markup", False):
        lowered = text.lower()
        if "<tool_call>" in lowered or "<function_call>" in lowered:
            raise ProviderError(f"{label} leaked tool-call markup")
    if quality_floor.get("reject_refusal_without_policy_reason", False):
        normalized = text.strip().lower()
        refusal_prefixes = (
            "i can't assist",
            "i cannot assist",
            "i'm unable to help",
            "i am unable to help",
            "sorry, but i can't",
        )
        if normalized.startswith(refusal_prefixes) and "policy" not in normalized:
            raise ProviderError(f"{label} returned an ungrounded refusal")


def _validate_string_list(value: Any, field: str) -> List[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ProviderError(f"Structured response field {field!r} must be an array of strings")
    return value


def _duplicate_seat_names(seat_names: Sequence[Any]) -> List[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for seat_name in seat_names:
        if not isinstance(seat_name, str):
            continue
        if seat_name in seen:
            duplicates.add(seat_name)
        seen.add(seat_name)
    return sorted(duplicates)


def _validated_cached_response(value: Any, *, label: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"Stored {label} response must be an object")
    unexpected = set(value) - set(MODEL_RESPONSE_FIELDS)
    missing = set(MODEL_RESPONSE_FIELDS) - set(value)
    if unexpected or missing:
        raise ConfigError(
            f"Stored {label} response schema mismatch; "
            f"missing={sorted(missing)}, unexpected={sorted(unexpected)}"
        )

    for field_name in ("text", "provider", "requested_model", "actual_model", "raw_status"):
        field_value = value.get(field_name)
        if not isinstance(field_value, str) or (
            field_name != "text" and not field_value
        ):
            raise ConfigError(f"Stored {label} response {field_name} must be a string")
    request_id = value.get("request_id")
    if request_id is not None and not isinstance(request_id, str):
        raise ConfigError(f"Stored {label} response request_id must be a string or null")
    route = value.get("route")
    if not isinstance(route, Mapping):
        raise ConfigError(f"Stored {label} response route must be an object")
    latency_seconds = value.get("latency_seconds")
    if (
        not isinstance(latency_seconds, (int, float))
        or isinstance(latency_seconds, bool)
        or not math.isfinite(float(latency_seconds))
        or float(latency_seconds) < 0
    ):
        raise ConfigError(
            f"Stored {label} response latency_seconds must be a nonnegative finite number"
        )

    usage = value.get("usage")
    if not isinstance(usage, Mapping):
        raise ConfigError(f"Stored {label} response usage must be an object")
    unexpected_usage = set(usage) - set(USAGE_FIELDS)
    missing_usage = set(USAGE_FIELDS) - set(usage)
    if unexpected_usage or missing_usage:
        raise ConfigError(
            f"Stored {label} response usage schema mismatch; "
            f"missing={sorted(missing_usage)}, unexpected={sorted(unexpected_usage)}"
        )
    for field_name in (
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "tool_calls",
    ):
        field_value = usage.get(field_name)
        if not isinstance(field_value, int) or isinstance(field_value, bool) or field_value < 0:
            raise ConfigError(
                f"Stored {label} response usage {field_name} must be a nonnegative integer"
            )
    if usage["cached_tokens"] > usage["input_tokens"]:
        raise ConfigError(
            f"Stored {label} response cached_tokens cannot exceed input_tokens"
        )
    if usage["reasoning_tokens"] > usage["output_tokens"]:
        raise ConfigError(
            f"Stored {label} response reasoning_tokens cannot exceed output_tokens"
        )
    cost_usd = usage.get("cost_usd")
    if cost_usd is not None and (
        not isinstance(cost_usd, (int, float))
        or isinstance(cost_usd, bool)
        or not math.isfinite(float(cost_usd))
        or float(cost_usd) < 0
    ):
        raise ConfigError(
            f"Stored {label} response usage cost_usd must be a nonnegative finite number or null"
        )
    for field_name in (
        "unknown_cost_fail_closed",
        "input_output_usage_complete",
        "raw_usage_invalid",
    ):
        if not isinstance(usage.get(field_name), bool):
            raise ConfigError(
                f"Stored {label} response usage {field_name} must be a boolean"
            )
    accounting_error = usage.get("accounting_error")
    if accounting_error is not None and (
        not isinstance(accounting_error, str) or not accounting_error
    ):
        raise ConfigError(
            f"Stored {label} response usage accounting_error must be a nonempty string or null"
        )
    if (
        usage["unknown_cost_fail_closed"] is True
        or usage["input_output_usage_complete"] is not True
        or usage["raw_usage_invalid"] is True
        or accounting_error is not None
    ):
        raise ConfigError(f"Stored {label} response contains invalid accounting evidence")

    normalized = dict(value)
    normalized["usage"] = dict(usage)
    normalized["route"] = dict(route)
    return normalized


def _invocation_payload(
    store: RunStore,
    stage: str,
    seat_name: str,
    system: str,
    prompt: str,
    response_schema: Optional[Mapping[str, Any]],
    schema_name: str,
) -> Dict[str, Any]:
    """Bind a call receipt to its exact run and prompt without persisting prompt text."""

    return {
        "schema_version": 1,
        "run_id": store.run_id,
        "input_sha256": store.input_hash,
        "config_sha256": store.config_hash,
        "stage": stage,
        "seat_name": seat_name,
        "system_sha256": text_hash(system),
        "prompt_sha256": text_hash(prompt),
        "response_schema_sha256": canonical_json_hash(response_schema),
        "schema_name": schema_name,
    }


def _entry_id(
    attempt_id: str,
    invocation_sha256: str,
    response_sha256: str,
) -> str:
    return call_receipt_entry_id(
        attempt_id,
        invocation_sha256,
        response_sha256,
    )


def _response_evidence(
    attempt_id: str,
    invocation_sha256: str,
    response: Mapping[str, Any],
) -> Dict[str, Any]:
    response_sha256 = canonical_json_hash(response)
    return {
        "schema_version": 1,
        "entry_id": _entry_id(attempt_id, invocation_sha256, response_sha256),
        "attempt_id": attempt_id,
        "invocation_sha256": invocation_sha256,
        "response_sha256": response_sha256,
    }


def _response_artifact_name(evidence: Mapping[str, Any]) -> str:
    return f"responses/{evidence['entry_id']}.json"


def _response_artifact_payload(
    invocation: Mapping[str, Any],
    evidence: Mapping[str, Any],
    response: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "invocation": dict(invocation),
        "receipt": dict(evidence),
        "response": dict(response),
    }


def _validated_response_evidence(
    value: Any,
    *,
    expected_invocation_sha256: str,
    response: Mapping[str, Any],
    label: str,
) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"Stored {label} response evidence must be an object")
    unexpected = set(value) - set(RESPONSE_EVIDENCE_FIELDS)
    missing = set(RESPONSE_EVIDENCE_FIELDS) - set(value)
    if unexpected or missing:
        raise ConfigError(
            f"Stored {label} response evidence schema mismatch; "
            f"missing={sorted(missing)}, unexpected={sorted(unexpected)}"
        )
    schema_version = value.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        raise ConfigError(f"Stored {label} response evidence has an unsupported schema")
    for field_name in RESPONSE_EVIDENCE_FIELDS[1:]:
        field_value = value.get(field_name)
        if not isinstance(field_value, str) or re.fullmatch(r"[0-9a-f]{64}", field_value) is None:
            raise ConfigError(
                f"Stored {label} response evidence {field_name} must be a lowercase SHA-256"
            )
    if value["invocation_sha256"] != expected_invocation_sha256:
        raise ConfigError(f"Stored {label} response evidence is bound to a different invocation")
    response_sha256 = canonical_json_hash(response)
    if value["response_sha256"] != response_sha256:
        raise ConfigError(f"Stored {label} response hash does not match its response")
    expected_entry_id = _entry_id(
        value["attempt_id"],
        value["invocation_sha256"],
        value["response_sha256"],
    )
    if value["entry_id"] != expected_entry_id:
        raise ConfigError(f"Stored {label} response entry id is invalid")
    return dict(value)


def _validated_ledger_response_entry(
    store: RunStore,
    entry: Mapping[str, Any],
    attempt_entries: Sequence[Any],
    *,
    invocation: Mapping[str, Any],
    label: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    invocation_sha256 = canonical_json_hash(invocation)
    entry_id = entry.get("entry_id")
    response_artifact_name = entry.get("response_artifact")
    if (
        not isinstance(entry_id, str)
        or not isinstance(response_artifact_name, str)
        or response_artifact_name != f"responses/{entry_id}.json"
        or not store.exists(response_artifact_name)
    ):
        raise ConfigError(
            f"Stored {label} response has no matching persisted raw-response evidence"
        )
    persisted_response_artifact = store.read_json(response_artifact_name)
    persisted_artifact_schema = persisted_response_artifact.get("schema_version")
    if (
        not isinstance(persisted_artifact_schema, int)
        or isinstance(persisted_artifact_schema, bool)
        or persisted_artifact_schema != 1
    ):
        raise ConfigError(f"Stored {label} raw-response evidence has an unsupported schema")
    for nested_name in ("invocation", "receipt"):
        nested_value = persisted_response_artifact.get(nested_name)
        nested_schema_version = (
            nested_value.get("schema_version")
            if isinstance(nested_value, Mapping)
            else None
        )
        if (
            not isinstance(nested_schema_version, int)
            or isinstance(nested_schema_version, bool)
            or nested_schema_version != 1
        ):
            raise ConfigError(
                f"Stored {label} raw-response {nested_name} has an unsupported schema"
            )
    response = _validated_cached_response(
        persisted_response_artifact.get("response"),
        label=label,
    )
    evidence = _validated_response_evidence(
        persisted_response_artifact.get("receipt"),
        expected_invocation_sha256=invocation_sha256,
        response=response,
        label=label,
    )
    response_artifact = _response_artifact_payload(invocation, evidence, response)
    if canonical_json_hash(persisted_response_artifact) != canonical_json_hash(
        response_artifact
    ):
        raise ConfigError(f"Stored {label} response evidence does not match the cached response")
    expected_entry_fields = {
        "stage": invocation["stage"],
        "seat": invocation["seat_name"],
        "provider": response["provider"],
        "requested_model": response["requested_model"],
        "actual_model": response["actual_model"],
        "request_id": response["request_id"],
        "route": response["route"],
        "latency_seconds": response["latency_seconds"],
        "usage": response["usage"],
        "raw_status": response["raw_status"],
        "entry_id": evidence["entry_id"],
        "attempt_id": evidence["attempt_id"],
        "invocation_sha256": evidence["invocation_sha256"],
        "response_sha256": evidence["response_sha256"],
        "response_artifact": response_artifact_name,
    }
    persisted_entry_fields = {
        field_name: entry.get(field_name) for field_name in expected_entry_fields
    }
    if canonical_json_hash(persisted_entry_fields) != canonical_json_hash(
        expected_entry_fields
    ):
        raise ConfigError(f"Stored {label} response does not match its ledger receipt")
    matching_attempts = [
        attempt
        for attempt in attempt_entries
        if isinstance(attempt, Mapping) and attempt.get("attempt_id") == evidence["attempt_id"]
    ]
    if len(matching_attempts) != 1:
        raise ConfigError(f"Stored {label} response has no matching persisted attempt")
    attempt = matching_attempts[0]
    if (
        attempt.get("invocation_sha256") != invocation_sha256
        or attempt.get("stage") != invocation["stage"]
        or attempt.get("seat") != invocation["seat_name"]
    ):
        raise ConfigError(f"Stored {label} response attempt is bound to a different invocation")
    return response, evidence


def _validated_persisted_call_response(
    store: RunStore,
    value: Any,
    evidence_value: Any,
    *,
    invocation: Mapping[str, Any],
    label: str,
) -> Dict[str, Any]:
    response = _validated_cached_response(value, label=label)
    invocation_sha256 = canonical_json_hash(invocation)
    evidence = _validated_response_evidence(
        evidence_value,
        expected_invocation_sha256=invocation_sha256,
        response=response,
        label=label,
    )
    if not store.exists("ledger.json"):
        raise ConfigError(f"Stored {label} response has no persisted ledger evidence")
    ledger = store.read_json("ledger.json")
    entries = ledger.get("entries")
    if not isinstance(entries, list):
        raise ConfigError(f"Stored {label} response has no persisted ledger entries")
    attempt_entries = ledger.get("attempt_entries")
    if not isinstance(attempt_entries, list):
        raise ConfigError(f"Stored {label} response has no persisted attempt entries")
    invocation_entries = [
        entry
        for entry in entries
        if isinstance(entry, Mapping)
        and entry.get("invocation_sha256") == invocation_sha256
    ]
    validated_entries = [
        (
            entry,
            *_validated_ledger_response_entry(
                store,
                entry,
                attempt_entries,
                invocation=invocation,
                label=label,
            ),
        )
        for entry in invocation_entries
    ]
    matching_entries = [
        validated_entry
        for validated_entry in validated_entries
        if validated_entry[0].get("entry_id") == evidence["entry_id"]
    ]
    if len(matching_entries) != 1:
        raise ConfigError(
            f"Stored {label} response has no matching persisted ledger entry"
        )
    _entry, persisted_response, persisted_evidence = matching_entries[0]
    if (
        canonical_json_hash(persisted_response) != canonical_json_hash(response)
        or canonical_json_hash(persisted_evidence) != canonical_json_hash(evidence)
    ):
        raise ConfigError(f"Stored {label} response evidence does not match the cached response")
    return response


def _has_persisted_invocation(store: RunStore, invocation: Mapping[str, Any]) -> bool:
    if not store.exists("ledger.json"):
        return False
    invocation_sha256 = canonical_json_hash(invocation)
    entries = store.read_json("ledger.json").get("entries")
    return isinstance(entries, list) and any(
        isinstance(entry, Mapping)
        and entry.get("invocation_sha256") == invocation_sha256
        for entry in entries
    )


def _has_persisted_raw_invocation(
    store: RunStore,
    invocation: Mapping[str, Any],
) -> bool:
    responses_directory = store.path("responses")
    if not responses_directory.exists():
        return False
    expected_invocation_sha256 = canonical_json_hash(invocation)
    for artifact_path in responses_directory.glob("*.json"):
        relative_name = artifact_path.relative_to(store.directory).as_posix()
        saved = store.read_json(relative_name)
        saved_invocation = saved.get("invocation")
        if (
            isinstance(saved_invocation, Mapping)
            and canonical_json_hash(saved_invocation) == expected_invocation_sha256
        ):
            return True
    return False


def _has_response_evidence_for_invocation(
    store: RunStore,
    invocation: Mapping[str, Any],
) -> bool:
    return _has_persisted_invocation(store, invocation) or _has_persisted_raw_invocation(
        store,
        invocation,
    )


def _persisted_attempt_ids(
    store: RunStore,
    invocation: Mapping[str, Any],
) -> List[str]:
    if not store.exists("ledger.json"):
        return []
    invocation_sha256 = canonical_json_hash(invocation)
    attempts = store.read_json("ledger.json").get("attempt_entries")
    if not isinstance(attempts, list):
        return []
    return [
        str(attempt["attempt_id"])
        for attempt in attempts
        if isinstance(attempt, Mapping)
        and attempt.get("invocation_sha256") == invocation_sha256
        and isinstance(attempt.get("attempt_id"), str)
    ]


def _has_persisted_attempt_for_invocation(
    store: RunStore,
    invocation: Mapping[str, Any],
) -> bool:
    return bool(_persisted_attempt_ids(store, invocation))


def _validated_synthesis_artifact(
    saved: Mapping[str, Any],
    *,
    store: RunStore,
    artifact_name: str,
    expected_mode: str,
    expected_author_seat: str,
    expected_invocation: Mapping[str, Any],
) -> Tuple[str, Dict[str, Any]]:
    expected_fields = {
        "mode",
        "author_seat",
        "text",
        "sha256",
        "response",
        "response_evidence",
    }
    unexpected = set(saved) - expected_fields
    missing = expected_fields - set(saved)
    if unexpected or missing:
        raise ConfigError(
            f"Stored {artifact_name} schema mismatch; "
            f"missing={sorted(missing)}, unexpected={sorted(unexpected)}"
        )
    text = saved.get("text")
    if not isinstance(text, str):
        raise ConfigError(f"Stored {artifact_name} text must be a string")
    if saved.get("sha256") != text_hash(text):
        raise ConfigError(f"Stored {artifact_name} hash does not match its text")
    if saved.get("mode") != expected_mode:
        raise ConfigError(
            f"Stored {artifact_name} provenance mode must be {expected_mode!r}"
        )
    if saved.get("author_seat") != expected_author_seat:
        raise ConfigError(
            f"Stored {artifact_name} provenance author must be {expected_author_seat!r}"
        )
    response = _validated_persisted_call_response(
        store,
        saved.get("response"),
        saved.get("response_evidence"),
        invocation=expected_invocation,
        label=artifact_name,
    )
    if response["text"] != text:
        raise ConfigError(f"Stored {artifact_name} text does not match its raw response")
    normalized = dict(saved)
    normalized["response"] = response
    normalized["response_evidence"] = dict(saved["response_evidence"])
    return text, normalized


def _validated_native_fallback_marker(
    saved: Mapping[str, Any],
    *,
    store: RunStore,
    expected_invocation: Mapping[str, Any],
) -> Dict[str, Any]:
    required_fields = {
        "schema_version",
        "status",
        "error",
        "fallback",
        "failure_phase",
        "invocation_sha256",
        "attempt_ids",
    }
    allowed_fields = required_fields | {"response_entry_ids"}
    unexpected = set(saved) - allowed_fields
    missing = required_fields - set(saved)
    if unexpected or missing:
        raise ConfigError(
            "Stored native Fusion fallback marker schema mismatch; "
            f"missing={sorted(missing)}, unexpected={sorted(unexpected)}"
        )
    if saved.get("status") != "failed":
        raise ConfigError("Stored native Fusion fallback marker status must be 'failed'")
    schema_version = saved.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        raise ConfigError("Stored native Fusion fallback marker has an unsupported schema")
    if saved.get("fallback") != "client_orchestrated":
        raise ConfigError(
            "Stored native Fusion fallback marker must select 'client_orchestrated'"
        )
    error = saved.get("error")
    if not isinstance(error, str) or not error:
        raise ConfigError("Stored native Fusion fallback marker error must be a nonempty string")
    invocation_sha256 = canonical_json_hash(expected_invocation)
    if saved.get("invocation_sha256") != invocation_sha256:
        raise ConfigError("Stored native Fusion fallback marker is bound to a different invocation")
    attempt_ids = saved.get("attempt_ids")
    if (
        not isinstance(attempt_ids, list)
        or not attempt_ids
        or any(
            not isinstance(attempt_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", attempt_id) is None
            for attempt_id in attempt_ids
        )
        or len(set(attempt_ids)) != len(attempt_ids)
    ):
        raise ConfigError(
            "Stored native Fusion fallback marker attempt_ids must be unique SHA-256 values"
        )
    if not store.exists("ledger.json"):
        raise ConfigError("Stored native Fusion fallback marker has no attempt ledger")
    ledger = store.read_json("ledger.json")
    attempt_entries = ledger.get("attempt_entries")
    if not isinstance(attempt_entries, list):
        raise ConfigError("Stored native Fusion fallback marker has no valid attempt ledger")
    expected_attempts = [
        attempt
        for attempt in attempt_entries
        if isinstance(attempt, Mapping)
        and attempt.get("invocation_sha256") == invocation_sha256
    ]
    expected_attempt_ids = [attempt.get("attempt_id") for attempt in expected_attempts]
    if attempt_ids != expected_attempt_ids:
        raise ConfigError(
            "Stored native Fusion fallback marker does not match the reserved native attempts"
        )
    if any(
        attempt.get("stage") != expected_invocation["stage"]
        or attempt.get("seat") != expected_invocation["seat_name"]
        for attempt in expected_attempts
    ):
        raise ConfigError("Stored native Fusion fallback attempts have invalid provenance")
    entries = ledger.get("entries")
    response_entries = [
        entry
        for entry in entries
        if isinstance(entry, Mapping)
        and entry.get("invocation_sha256") == invocation_sha256
        and entry.get("attempt_id") in attempt_ids
    ] if isinstance(entries, list) else []
    response_attempt_ids = {entry.get("attempt_id") for entry in response_entries}
    expected_failure_phase = (
        "semantic"
        if any(attempt_id in response_attempt_ids for attempt_id in attempt_ids)
        else "transport"
    )
    if saved.get("failure_phase") != expected_failure_phase:
        raise ConfigError("Stored native Fusion fallback marker has an invalid failure phase")
    response_entry_ids = saved.get("response_entry_ids")
    if expected_failure_phase == "semantic":
        expected_response_entry_ids = [entry.get("entry_id") for entry in response_entries]
        if (
            not isinstance(response_entry_ids, list)
            or not response_entry_ids
            or any(
                not isinstance(entry_id, str)
                or re.fullmatch(r"[0-9a-f]{64}", entry_id) is None
                for entry_id in response_entry_ids
            )
            or len(set(response_entry_ids)) != len(response_entry_ids)
        ):
            raise ConfigError(
                "Stored native semantic fallback marker must bind response_entry_ids"
            )
        if response_entry_ids != expected_response_entry_ids:
            raise ConfigError(
                "Stored native semantic fallback marker does not match its response receipts"
            )
        for entry in response_entries:
            response_artifact_name = entry.get("response_artifact")
            if not isinstance(response_artifact_name, str) or not store.exists(
                response_artifact_name
            ):
                raise ConfigError(
                    "Stored native semantic fallback marker has no matching raw-response evidence"
                )
            response_artifact = store.read_json(response_artifact_name)
            _validated_persisted_call_response(
                store,
                response_artifact.get("response"),
                response_artifact.get("receipt"),
                invocation=expected_invocation,
                label="native semantic fallback",
            )
    elif response_entry_ids not in (None, []):
        raise ConfigError(
            "Stored native transport fallback marker cannot bind response receipts"
        )
    return dict(saved)


def validate_judgment(value: Mapping[str, Any], required_fields: Sequence[str] = JUDGE_FIELDS) -> Dict[str, Any]:
    expected_fields = tuple(str(field) for field in required_fields)
    unexpected = set(value) - set(expected_fields)
    missing = set(expected_fields) - set(value)
    if unexpected or missing:
        raise ProviderError(f"Judge schema mismatch; missing={sorted(missing)}, unexpected={sorted(unexpected)}")
    return {field: _validate_string_list(value[field], field) for field in expected_fields}


def _judge_contract(profile: Mapping[str, Any]) -> Tuple[Dict[str, Any], Tuple[str, ...]]:
    fusion = profile.get("fusion", {})
    configured_fields = fusion.get("judge_required_fields", list(JUDGE_FIELDS)) if isinstance(fusion, Mapping) else list(JUDGE_FIELDS)
    if not isinstance(configured_fields, list) or not configured_fields:
        raise ConfigError("fusion.judge_required_fields must be a non-empty array")
    required_fields = tuple(str(field) for field in configured_fields)
    unsupported = set(required_fields) - set(JUDGE_FIELDS)
    if unsupported:
        raise ConfigError(f"fusion.judge_required_fields contains unsupported fields: {sorted(unsupported)}")
    contract = dict(JUDGE_SCHEMA)
    contract["required"] = list(required_fields)
    contract["properties"] = {
        field: JUDGE_SCHEMA["properties"][field]
        for field in required_fields
    }
    return contract, required_fields


def validate_verdict(value: Mapping[str, Any], artifact_hash: str) -> Dict[str, Any]:
    unexpected = set(value) - set(VERDICT_FIELDS)
    missing = set(VERDICT_FIELDS) - set(value)
    if unexpected or missing:
        raise ProviderError(f"Verdict schema mismatch; missing={sorted(missing)}, unexpected={sorted(unexpected)}")
    verdict = value.get("verdict")
    if verdict not in {"PASS", "FAIL", "NEEDS_WORK"}:
        raise ProviderError("Verdict must be PASS, FAIL, or NEEDS_WORK")
    if value.get("artifact_sha256") != artifact_hash:
        raise ProviderError("Reviewer did not bind its verdict to the exact candidate artifact hash")
    summary = value.get("summary")
    if not isinstance(summary, str):
        raise ProviderError("Verdict summary must be a string")
    validated = {"verdict": verdict, "artifact_sha256": artifact_hash, "summary": summary}
    for field in (
        "criteria_reviewed",
        "blind_spots",
        "blocking_findings",
        "non_blocking_findings",
        "evidence",
        "required_actions",
    ):
        validated[field] = _validate_string_list(value[field], field)
    if not validated["criteria_reviewed"]:
        raise ProviderError("Verdict criteria_reviewed must identify at least one checked criterion")
    if verdict == "PASS" and validated["blocking_findings"]:
        raise ProviderError("PASS verdict cannot include blocking_findings")
    if verdict == "PASS" and validated["blind_spots"]:
        raise ProviderError("PASS verdict cannot include blind_spots")
    if verdict == "PASS" and validated["required_actions"]:
        raise ProviderError("PASS verdict cannot include required_actions")
    return validated


def _validated_cached_gate_reviews(
    store: RunStore,
    saved_reviews: Any,
    reviewer_names: Sequence[str],
    gates: Mapping[str, Any],
    artifact_hash: str,
    invocations_by_reviewer: Mapping[str, Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    if not isinstance(saved_reviews, list):
        raise ConfigError("Stored gate reviewers must be an array")

    normalized_reviews: List[Dict[str, Any]] = []
    saved_reviewer_names: List[str] = []
    allowed_verdicts = gates.get("allowed_verdicts", ["PASS", "NEEDS_WORK", "FAIL"])
    for saved_review in saved_reviews:
        if not isinstance(saved_review, Mapping):
            raise ConfigError("Stored gate reviewer entries must be objects")
        reviewer_name = saved_review.get("seat_name")
        if not isinstance(reviewer_name, str):
            raise ConfigError("Stored gate reviewer entries must identify a string seat_name")
        saved_reviewer_names.append(reviewer_name)

        status = saved_review.get("status")
        normalized_review = dict(saved_review)
        if status == "completed":
            saved_response = saved_review.get("response")
            if not isinstance(saved_response, Mapping):
                raise ConfigError("Stored completed gate review is missing its raw response")
            response = _validated_persisted_call_response(
                store,
                saved_response,
                saved_review.get("response_evidence"),
                invocation=invocations_by_reviewer[reviewer_name],
                label=f"gate review for {reviewer_name}",
            )
            try:
                canonical_verdict = validate_verdict(
                    parse_json_object(response["text"]),
                    artifact_hash,
                )
            except ProviderError as exc:
                raise ConfigError(f"Stored gate reviewer raw response is invalid: {exc}") from exc

            saved_verdict = saved_review.get("verdict")
            if not isinstance(saved_verdict, Mapping):
                raise ConfigError("Stored completed gate review is missing its verdict")
            try:
                validated_verdict = validate_verdict(saved_verdict, artifact_hash)
            except ProviderError as exc:
                raise ConfigError(f"Stored gate reviewer verdict is invalid: {exc}") from exc
            if validated_verdict != canonical_verdict:
                raise ConfigError("Stored gate reviewer verdict does not match its raw response")
            if isinstance(allowed_verdicts, list) and canonical_verdict["verdict"] not in allowed_verdicts:
                raise ConfigError(
                    f"Stored gate reviewer returned disallowed verdict {canonical_verdict['verdict']!r}"
                )
            normalized_review["verdict"] = canonical_verdict
            normalized_review["response"] = response
            normalized_review["response_evidence"] = dict(saved_review["response_evidence"])
        elif status == "failed":
            failure_kind = saved_review.get("failure_kind")
            if failure_kind not in {"provider_failure", "schema_invalid"}:
                raise ConfigError("Stored failed gate review has an invalid failure_kind")
            if not isinstance(saved_review.get("error"), str):
                raise ConfigError("Stored failed gate review must include a string error")
            if failure_kind == "schema_invalid":
                normalized_review["response"] = _validated_persisted_call_response(
                    store,
                    saved_review.get("response"),
                    saved_review.get("response_evidence"),
                    invocation=invocations_by_reviewer[reviewer_name],
                    label=f"failed gate review for {reviewer_name}",
                )
                normalized_review["response_evidence"] = dict(
                    saved_review["response_evidence"]
                )
            elif (
                saved_review.get("response") is not None
                or saved_review.get("response_evidence") is not None
            ):
                raise ConfigError(
                    "Stored provider-failed gate review cannot contain completed response evidence"
                )
        else:
            raise ConfigError("Stored gate reviewer status must be 'completed' or 'failed'")
        normalized_reviews.append(normalized_review)

    duplicate_saved_reviewers = _duplicate_seat_names(saved_reviewer_names)
    if duplicate_saved_reviewers or sorted(saved_reviewer_names) != sorted(reviewer_names):
        raise ConfigError(
            "Stored gate reviewer roster must match the configured reviewer seats exactly"
        )
    return normalized_reviews


def _gate_result_from_reviews(
    reviews: Sequence[Mapping[str, Any]],
    reviewer_names: Sequence[str],
    gates: Mapping[str, Any],
    artifact_hash: str,
    mechanical_evidence: str,
) -> Dict[str, Any]:
    review_rows = [dict(review) for review in reviews]
    pass_count = sum(
        1
        for review in review_rows
        if review.get("status") == "completed"
        and review["verdict"]["verdict"] == "PASS"
    )
    negative_verdicts = [
        {
            "seat_name": str(review.get("seat_name", "unknown")),
            "verdict": str(review["verdict"]["verdict"]),
            "summary": str(review["verdict"]["summary"]),
            "blocking_findings": list(review["verdict"]["blocking_findings"]),
            "required_actions": list(review["verdict"]["required_actions"]),
            "evidence": list(review["verdict"]["evidence"]),
        }
        for review in review_rows
        if review.get("status") == "completed"
        and review["verdict"]["verdict"] in {"FAIL", "NEEDS_WORK"}
    ]
    negative_verdict_blocked = bool(negative_verdicts)
    required_passes = int(gates.get("required_passes", len(reviewer_names)))
    fail_closed = gates.get("fail_closed", True) is True
    failed_reviews = any(review.get("status") != "completed" for review in review_rows)
    schema_failures = [
        {
            "seat_name": str(review.get("seat_name", "unknown")),
            "error": str(review.get("error", "invalid structured verdict")),
        }
        for review in review_rows
        if review.get("failure_kind") == "schema_invalid"
    ]
    schema_blocked = (
        bool(schema_failures)
        and gates.get("schema_failure_is_blocking", True) is True
    )
    mechanical_failures = _mechanical_failures(mechanical_evidence)
    mechanical_blocked = (
        bool(mechanical_failures)
        and gates.get("mechanical_failure_is_blocking", True) is True
    )
    unresolved_blind_spots = [
        str(blind_spot)
        for review in review_rows
        if review.get("status") == "completed"
        for blind_spot in review.get("verdict", {}).get("blind_spots", [])
    ]
    blind_spot_blocked = (
        bool(unresolved_blind_spots)
        and gates.get("blind_spot_requires_targeted_review", True) is True
    )
    deterministic_blockers = []
    if negative_verdict_blocked:
        deterministic_blockers.append(
            "At least one reviewer returned a blocking negative verdict: "
            + "; ".join(
                f"{review['seat_name']}: {review['verdict']}"
                for review in negative_verdicts
            )
        )
    if mechanical_blocked:
        deterministic_blockers.append(
            "Mechanical evidence reports failure: " + "; ".join(mechanical_failures)
        )
    if blind_spot_blocked:
        deterministic_blockers.append(
            "Targeted review is required for unresolved blind spots: "
            + "; ".join(unresolved_blind_spots)
        )
    if schema_blocked:
        deterministic_blockers.append(
            "At least one reviewer returned an invalid structured verdict: "
            + "; ".join(
                f"{failure['seat_name']}: {failure['error']}"
                for failure in schema_failures
            )
        )
    passed = (
        pass_count >= required_passes
        and (not failed_reviews or not fail_closed)
        and not mechanical_blocked
        and not blind_spot_blocked
        and not schema_blocked
        and not negative_verdict_blocked
    )
    return {
        "enabled": True,
        "passed": passed,
        "artifact_sha256": artifact_hash,
        "pass_count": pass_count,
        "required_passes": required_passes,
        "fail_closed": fail_closed,
        "mechanical_failures": mechanical_failures,
        "mechanical_blocked": mechanical_blocked,
        "schema_failures": schema_failures,
        "schema_blocked": schema_blocked,
        "negative_verdicts": negative_verdicts,
        "negative_verdict_blocked": negative_verdict_blocked,
        "unresolved_blind_spots": unresolved_blind_spots,
        "blind_spot_blocked": blind_spot_blocked,
        "deterministic_blockers": deterministic_blockers,
        "reviewers": review_rows,
    }


class FusionOrchestrator:
    def __init__(self, config: Optional[Mapping[str, Any]] = None, registry: Optional[ProviderRegistry] = None) -> None:
        self.config = dict(config) if config is not None else load_config()
        self._registry_injected = registry is not None
        self.registry = registry or ProviderRegistry(self.config)

    def _bind_selected_profile(self, profile_name: str) -> None:
        if not self._registry_injected:
            self.registry = ProviderRegistry(self.config, profile_name=profile_name)

    def _seat_config(self, seat_name: str) -> Mapping[str, Any]:
        seat = self.config.get("seats", {}).get(seat_name)
        if not isinstance(seat, Mapping):
            raise ConfigError(f"Unknown seat: {seat_name}")
        return seat

    @staticmethod
    def _assert_external_provider_access(profile: Mapping[str, Any]) -> None:
        privacy = profile.get("privacy", {})
        if isinstance(privacy, Mapping) and privacy.get("external_provider_access") == "deny":
            raise ConfigError(
                "Selected profile denies external provider access; fuse and adversarial_gate cannot dispatch"
            )

    def _call(
        self,
        budget: BudgetTracker,
        store: RunStore,
        stage: str,
        seat_name: str,
        system: str,
        prompt: str,
        response_schema: Optional[Mapping[str, Any]] = None,
        schema_name: str = "structured_response",
    ) -> Tuple[ModelResponse, Dict[str, Any]]:
        store.check_kill()
        invocation = _invocation_payload(
            store,
            stage,
            seat_name,
            system,
            prompt,
            response_schema,
            schema_name,
        )
        invocation_sha256 = canonical_json_hash(invocation)
        current_attempt: Optional[Dict[str, Any]] = None

        def reserve_and_persist_attempt() -> None:
            nonlocal current_attempt
            current_attempt = budget.reserve_call(
                stage,
                seat_name,
                invocation_sha256=invocation_sha256,
            )
            # A timed-out request may still have reached the provider and may be
            # billable. Persist the reservation before transport starts so a
            # failed retry or process restart cannot erase it.
            store.write_budget_snapshot(budget)

        def persist_and_record_response(response: ModelResponse) -> Dict[str, Any]:
            # This callback also handles paid HTTP-success responses that fail
            # provider semantics before a model fallback can be attempted.
            if current_attempt is None:
                raise ConfigError(
                    f"Provider returned a response for {seat_name!r} without reserving an attempt"
                )
            response_dict = response.to_dict()
            evidence = _response_evidence(
                str(current_attempt["attempt_id"]),
                invocation_sha256,
                response_dict,
            )
            response_artifact = _response_artifact_payload(
                invocation,
                evidence,
                response_dict,
            )
            response_artifact_name = _response_artifact_name(evidence)
            store.write_json(
                response_artifact_name,
                response_artifact,
            )
            try:
                budget.record(
                    stage,
                    seat_name,
                    response,
                    attempt_index=int(current_attempt["attempt_index"]),
                    attempt_id=str(current_attempt["attempt_id"]),
                    invocation_sha256=invocation_sha256,
                    response_sha256=str(evidence["response_sha256"]),
                    response_artifact=response_artifact_name,
                )
            finally:
                # An over-threshold response is already billable evidence.
                # Persist its usage and stop latch even when record() fails.
                store.write_budget_snapshot(budget)
            return evidence

        response = self.registry.complete(
            seat_name,
            system=system,
            prompt=prompt,
            response_schema=response_schema,
            schema_name=schema_name,
            before_attempt=reserve_and_persist_attempt,
            on_semantic_failure_response=persist_and_record_response,
        )
        evidence = persist_and_record_response(response)
        store.check_kill()
        return response, evidence

    def _run_panel(
        self,
        task: str,
        context: str,
        mechanical_evidence: str,
        profile: Mapping[str, Any],
        budget: BudgetTracker,
        store: RunStore,
    ) -> List[Dict[str, Any]]:
        fusion = profile["fusion"]
        panel_seat_names = list(fusion["panel"])
        optional_seat_names = list(fusion.get("optional_panel", []))
        duplicate_panel_seats = _duplicate_seat_names(panel_seat_names)
        duplicate_optional_seats = _duplicate_seat_names(optional_seat_names)
        overlapping_seats = sorted(
            {seat_name for seat_name in panel_seat_names if isinstance(seat_name, str)}
            & {seat_name for seat_name in optional_seat_names if isinstance(seat_name, str)}
        )
        if duplicate_panel_seats:
            raise ConfigError(
                f"fusion.panel must not contain duplicate seat names {duplicate_panel_seats}"
            )
        if duplicate_optional_seats:
            raise ConfigError(
                "fusion.optional_panel must not contain duplicate seat names "
                f"{duplicate_optional_seats}"
            )
        if overlapping_seats:
            raise ConfigError(
                f"fusion.panel and optional_panel must not overlap {overlapping_seats}"
            )

        seat_names = list(panel_seat_names)
        for optional_seat_name in optional_seat_names:
            optional_seat = self._seat_config(str(optional_seat_name))
            provider = self.config.get("providers", {}).get(optional_seat.get("provider"), {})
            if optional_seat.get("enabled", True) is True and isinstance(provider, Mapping) and provider.get("enabled", True) is True:
                seat_names.append(str(optional_seat_name))
        max_panel_seats = int(fusion.get("max_panel_seats", len(seat_names)))
        if max_panel_seats < len(panel_seat_names):
            raise ConfigError(
                "fusion.max_panel_seats cannot be smaller than the required panel length"
            )
        seat_names = seat_names[:max_panel_seats]
        objective = str(profile.get("objective", "Deliver the most correct, complete, and executable result."))

        def panel_request(seat_name: str) -> Tuple[Mapping[str, Any], str, str, str, Dict[str, Any]]:
            seat = self._seat_config(seat_name)
            role = str(seat.get("role", "domain analyst"))
            system = panel_system(
                role,
                str(seat.get("persona", "Find the most important truth other reviewers may miss.")),
                objective,
            )
            prompt = panel_prompt(
                task,
                _panel_context_bundle(
                    context,
                    mechanical_evidence,
                    str(seat.get("context_bundle", "full_task_and_evidence")),
                    fusion.get("partition_context", True) is True,
                ),
            )
            invocation = _invocation_payload(
                store,
                "panel",
                seat_name,
                system,
                prompt,
                None,
                "structured_response",
            )
            return seat, role, system, prompt, invocation

        invocations_by_seat = {
            seat_name: panel_request(seat_name)[4]
            for seat_name in seat_names
        }
        stored_panel = store.read_json("panel.json") if store.exists("panel.json") else {}
        stored_results = stored_panel.get("results", [])
        if not isinstance(stored_results, list):
            raise ConfigError("Stored panel artifact results must be an array")
        latest_by_seat: Dict[str, Dict[str, Any]] = {}
        quality_floor = fusion.get("quality_floor", {})
        for saved_result in stored_results:
            if not isinstance(saved_result, Mapping):
                raise ConfigError("Stored panel result entries must be objects")
            status = saved_result.get("status")
            allowed_result_fields = (
                FAILED_PANEL_RESULT_FIELDS if status == "failed" else PANEL_RESULT_FIELDS
            )
            unexpected = set(saved_result) - set(allowed_result_fields)
            missing = set(PANEL_RESULT_FIELDS) - set(saved_result)
            if unexpected or missing:
                raise ConfigError(
                    "Stored panel result schema mismatch; "
                    f"missing={sorted(missing)}, unexpected={sorted(unexpected)}"
                )
            seat_name = saved_result.get("seat_name")
            if not isinstance(seat_name, str) or seat_name not in seat_names:
                raise ConfigError("Stored panel result references an unexpected seat")
            if seat_name in latest_by_seat:
                raise ConfigError("Stored panel results must not contain duplicate seat names")
            seat = self._seat_config(seat_name)
            expected_role = str(seat.get("role", "domain analyst"))
            if saved_result.get("role") != expected_role:
                raise ConfigError(
                    f"Stored panel result role does not match configured seat {seat_name!r}"
                )
            if not isinstance(saved_result.get("anonymous_label"), str):
                raise ConfigError("Stored panel result anonymous_label must be a string")

            normalized_result = dict(saved_result)
            if status == "completed":
                if saved_result.get("error") is not None:
                    raise ConfigError("Stored completed panel result cannot contain an error")
                response = _validated_persisted_call_response(
                    store,
                    saved_result.get("response"),
                    saved_result.get("response_evidence"),
                    invocation=invocations_by_seat[seat_name],
                    label=f"panel result for {seat_name}",
                )
                if isinstance(quality_floor, Mapping):
                    try:
                        _validate_quality_floor(
                            response["text"],
                            quality_floor,
                            f"Stored seat {seat_name}",
                        )
                    except ProviderError as exc:
                        raise ConfigError(str(exc)) from exc
                normalized_result["response"] = response
                normalized_result["response_evidence"] = dict(
                    saved_result["response_evidence"]
                )
            elif status == "failed":
                error = saved_result.get("error")
                if not isinstance(error, str) or not error:
                    raise ConfigError("Stored failed panel result must contain a nonempty error")
                saved_response = saved_result.get("response")
                saved_evidence = saved_result.get("response_evidence")
                if (saved_response is None) != (saved_evidence is None):
                    raise ConfigError(
                        "Stored failed panel result must include both response and response evidence, or neither"
                    )
                if saved_response is not None:
                    normalized_result["response"] = _validated_persisted_call_response(
                        store,
                        saved_response,
                        saved_evidence,
                        invocation=invocations_by_seat[seat_name],
                        label=f"failed panel result for {seat_name}",
                    )
                    normalized_result["response_evidence"] = dict(saved_evidence)
                saved_attempt_ids = saved_result.get("attempt_ids")
                if (
                    not isinstance(saved_attempt_ids, list)
                    or any(
                        not isinstance(attempt_id, str)
                        or re.fullmatch(r"[0-9a-f]{64}", attempt_id) is None
                        for attempt_id in saved_attempt_ids
                    )
                    or len(set(saved_attempt_ids)) != len(saved_attempt_ids)
                ):
                    raise ConfigError(
                        "Stored failed panel result must bind unique attempt_ids"
                    )
                represented_attempt_ids = _persisted_attempt_ids(
                    store,
                    invocations_by_seat[seat_name],
                )
                if saved_attempt_ids != represented_attempt_ids:
                    raise ConfigError(
                        f"Stored failed panel result for {seat_name!r} does not represent the exact persisted attempts"
                    )
                normalized_result["attempt_ids"] = list(saved_attempt_ids)
            else:
                raise ConfigError("Stored panel result status must be 'completed' or 'failed'")
            latest_by_seat[seat_name] = normalized_result

        stored_attempts = stored_panel.get("attempts", stored_results)
        if not isinstance(stored_attempts, list) or any(
            not isinstance(row, Mapping) for row in stored_attempts
        ):
            raise ConfigError("Stored panel artifact attempts must be an array of objects")
        attempts: List[Dict[str, Any]] = [dict(row) for row in stored_attempts]
        for seat_name in seat_names:
            saved_result = latest_by_seat.get(seat_name)
            reusable_response = (
                isinstance(saved_result, Mapping)
                and saved_result.get("response_evidence") is not None
            )
            if not reusable_response and _has_response_evidence_for_invocation(
                store,
                invocations_by_seat[seat_name],
            ):
                raise ConfigError(
                    f"Stored panel artifact omits paid response evidence for seat {seat_name!r}"
                )
            if saved_result is None and _has_persisted_attempt_for_invocation(
                store,
                invocations_by_seat[seat_name],
            ):
                raise ConfigError(
                    f"Persisted panel attempt evidence exists without a reusable semantic cache for seat {seat_name!r}"
                )
        pending_seat_names = [
            seat_name
            for seat_name in seat_names
            if latest_by_seat.get(seat_name, {}).get("status") != "completed"
            and latest_by_seat.get(seat_name, {}).get("response_evidence") is None
        ]

        def persist_panel_snapshot(results: Sequence[Mapping[str, Any]]) -> None:
            result_rows = [dict(row) for row in results]
            live_count = sum(row.get("status") == "completed" for row in result_rows)
            failed_count = sum(row.get("status") == "failed" for row in result_rows)
            store.write_json(
                "panel.json",
                {
                    "results": result_rows,
                    "attempts": attempts,
                    "live_count": live_count,
                    "failed_count": failed_count,
                    "degraded": failed_count > 0,
                },
            )

        def panel_worker(seat_name: str) -> SeatResult:
            _seat, role, system, prompt, _invocation = panel_request(seat_name)
            response, response_evidence = self._call(
                budget,
                store,
                "panel",
                seat_name,
                system,
                prompt,
            )
            quality_floor = fusion.get("quality_floor", {})
            if isinstance(quality_floor, Mapping):
                try:
                    _validate_quality_floor(response.text, quality_floor, f"Seat {seat_name}")
                except ProviderError as exc:
                    return SeatResult(
                        seat_name=seat_name,
                        anonymous_label="",
                        role=role,
                        status="failed",
                        response=response,
                        response_evidence=response_evidence,
                        error=str(exc),
                    )
            return SeatResult(
                seat_name=seat_name,
                anonymous_label="",
                role=role,
                status="completed",
                response=response,
                response_evidence=response_evidence,
            )

        if pending_seat_names:
            max_concurrency = min(int(fusion.get("max_concurrency", 2)), len(pending_seat_names))
            with ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix="inception-panel") as executor:
                futures: Dict[Future[SeatResult], str] = {
                    executor.submit(panel_worker, seat_name): seat_name for seat_name in pending_seat_names
                }
                for future in as_completed(futures):
                    seat_name = futures[future]
                    try:
                        result = future.result()
                    except BudgetExceeded:
                        for pending in futures:
                            pending.cancel()
                        raise
                    except Exception as exc:
                        seat = self._seat_config(seat_name)
                        result = SeatResult(
                            seat_name=seat_name,
                            anonymous_label="",
                            role=str(seat.get("role", "domain analyst")),
                            status="failed",
                            error=str(exc),
                        )
                    result_row = result.to_dict()
                    if result_row["status"] == "failed":
                        result_row["attempt_ids"] = _persisted_attempt_ids(
                            store,
                            invocations_by_seat[seat_name],
                        )
                    latest_by_seat[seat_name] = result_row
                    attempts.append(result_row)
                    persist_panel_snapshot(
                        [latest_by_seat[name] for name in seat_names if name in latest_by_seat]
                    )

        completed = [dict(latest_by_seat[name]) for name in seat_names if latest_by_seat.get(name, {}).get("status") == "completed"]
        failures = [dict(latest_by_seat[name]) for name in seat_names if latest_by_seat.get(name, {}).get("status") == "failed"]

        min_live = int(fusion.get("min_live_seats", len(seat_names)))
        if len(completed) < min_live:
            persist_panel_snapshot([*completed, *failures])
            store.mark_stage("panel", "failed", "panel.json")
            failure_summary = "; ".join(
                f"{failure.get('seat_name')}: {failure.get('error')}" for failure in failures
            )
            raise ProviderError(f"Panel collapsed: {len(completed)}/{len(seat_names)} live; {failure_summary}")
        allow_degradation = fusion.get("allow_degradation", False)
        if failures and allow_degradation is not True:
            persist_panel_snapshot([*completed, *failures])
            store.mark_stage("panel", "failed", "panel.json")
            failure_summary = "; ".join(
                f"{failure.get('seat_name')}: {failure.get('error')}" for failure in failures
            )
            raise ProviderError(f"Panel degradation is disabled; {failure_summary}")

        # Deterministic task-local shuffle hides model identity without making resumes unstable.
        if fusion.get("randomize_panel_order", True):
            random.Random(store.task_hash).shuffle(completed)
        for index, result in enumerate(completed):
            result["anonymous_label"] = (
                f"Seat {string.ascii_uppercase[index]}"
                if fusion.get("anonymize_model_identity", True)
                else result["seat_name"]
            )
        results = [*completed, *failures]
        persist_panel_snapshot(results)
        store.mark_stage("panel", "completed", "panel.json")
        return results

    def _run_judge(
        self,
        task: str,
        reports: Sequence[Mapping[str, Any]],
        profile: Mapping[str, Any],
        budget: BudgetTracker,
        store: RunStore,
        mechanical_evidence: str,
    ) -> Dict[str, Any]:
        fusion = profile["fusion"]
        judge_name = str(fusion["judge"])
        judge_schema, required_fields = _judge_contract(profile)
        objective = str(profile.get("objective", "Deliver the most correct, complete, and executable result."))
        live_reports = [report for report in reports if report.get("status") == "completed"]
        judge_seat = self._seat_config(judge_name)
        judge_system_text = judge_system(
            objective,
            str(judge_seat.get("persona", "")),
            str(judge_seat.get("context_bundle", "")),
        )
        judge_prompt_text = judge_prompt(task, live_reports, mechanical_evidence)
        judge_invocation = _invocation_payload(
            store,
            "judge",
            judge_name,
            judge_system_text,
            judge_prompt_text,
            judge_schema,
            "fusion_judgment",
        )
        if store.exists("judge.json"):
            saved = store.read_json("judge.json")
            if set(saved) != {"judgment", "response", "response_evidence"}:
                raise ConfigError(
                    "Stored judge artifact must contain only judgment, response, and response_evidence"
                )
            response = _validated_persisted_call_response(
                store,
                saved.get("response"),
                saved.get("response_evidence"),
                invocation=judge_invocation,
                label="judge",
            )
            try:
                canonical_judgment = validate_judgment(
                    parse_json_object(response["text"]),
                    required_fields,
                )
            except ProviderError as exc:
                raise ConfigError(f"Stored judge raw response is invalid: {exc}") from exc
            saved_judgment = saved.get("judgment")
            if not isinstance(saved_judgment, Mapping):
                raise ConfigError("Stored judge artifact is missing its judgment")
            try:
                validated_judgment = validate_judgment(
                    saved_judgment,
                    required_fields,
                )
            except ProviderError as exc:
                raise ConfigError(f"Stored judge judgment is invalid: {exc}") from exc
            if validated_judgment != canonical_judgment:
                raise ConfigError("Stored judge judgment does not match its raw response")
            return canonical_judgment
        if _has_response_evidence_for_invocation(store, judge_invocation):
            raise ConfigError(
                "Persisted judge response evidence exists without a reusable judge artifact"
            )
        if _has_persisted_attempt_for_invocation(store, judge_invocation):
            raise ConfigError(
                "Persisted judge attempt evidence exists without a reusable judge artifact"
            )
        response, response_evidence = self._call(
            budget,
            store,
            "judge",
            judge_name,
            judge_system_text,
            judge_prompt_text,
            judge_schema,
            "fusion_judgment",
        )
        judgment = validate_judgment(parse_json_object(response.text), required_fields)
        store.write_json(
            "judge.json",
            {
                "judgment": judgment,
                "response": response.to_dict(),
                "response_evidence": response_evidence,
            },
        )
        store.mark_stage("judge", "completed", "judge.json")
        return judgment

    def _run_synthesis(
        self,
        task: str,
        context: str,
        reports: Sequence[Mapping[str, Any]],
        judgment: Mapping[str, Any],
        profile: Mapping[str, Any],
        budget: BudgetTracker,
        store: RunStore,
        mechanical_evidence: str,
        *,
        round_index: int = 0,
        amendment_feedback: str = "",
    ) -> Tuple[str, Dict[str, Any]]:
        artifact_name = "synthesis.json" if round_index == 0 else f"synthesis-amendment-{round_index}.json"
        synthesis_stage = "synthesis" if round_index == 0 else f"amendment-{round_index}"
        fusion = profile["fusion"]
        synthesizer_name = str(fusion["synthesizer"])
        objective = str(profile.get("objective", "Deliver the most correct, complete, and executable result."))
        live_reports = [report for report in reports if report.get("status") == "completed"]
        synthesizer_seat = self._seat_config(synthesizer_name)
        synthesis_system_text = synthesis_system(
            objective,
            str(synthesizer_seat.get("persona", "")),
            str(synthesizer_seat.get("context_bundle", "")),
        )
        synthesis_prompt_text = synthesis_prompt(
            task,
            context,
            live_reports,
            judgment,
            mechanical_evidence,
            amendment_feedback,
        )
        synthesis_invocation = _invocation_payload(
            store,
            synthesis_stage,
            synthesizer_name,
            synthesis_system_text,
            synthesis_prompt_text,
            None,
            "structured_response",
        )
        if store.exists(artifact_name):
            saved = store.read_json(artifact_name)
            text, normalized_saved = _validated_synthesis_artifact(
                saved,
                store=store,
                artifact_name=artifact_name,
                expected_mode="client_orchestrated",
                expected_author_seat=synthesizer_name,
                expected_invocation=synthesis_invocation,
            )
            quality_floor = fusion.get("quality_floor", {})
            if isinstance(quality_floor, Mapping):
                try:
                    _validate_quality_floor(
                        text,
                        quality_floor,
                        f"Stored synthesizer {synthesizer_name}",
                    )
                except ProviderError as exc:
                    raise ConfigError(str(exc)) from exc
            return text, normalized_saved
        if _has_response_evidence_for_invocation(store, synthesis_invocation):
            raise ConfigError(
                f"Persisted {synthesis_stage} response evidence exists without a reusable synthesis artifact"
            )
        if _has_persisted_attempt_for_invocation(store, synthesis_invocation):
            raise ConfigError(
                f"Persisted {synthesis_stage} attempt evidence exists without a reusable synthesis artifact"
            )
        if (
            fusion.get("separate_no_tools_synthesis_turn") is True
            and synthesizer_seat.get("tool_policy") != "none"
        ):
            raise ConfigError("separate_no_tools_synthesis_turn requires a tool-less synthesizer seat")
        response, response_evidence = self._call(
            budget,
            store,
            synthesis_stage,
            synthesizer_name,
            synthesis_system_text,
            synthesis_prompt_text,
        )
        quality_floor = fusion.get("quality_floor", {})
        if isinstance(quality_floor, Mapping):
            _validate_quality_floor(response.text, quality_floor, f"Synthesizer {synthesizer_name}")
        saved = {
            "mode": "client_orchestrated",
            "author_seat": synthesizer_name,
            "text": response.text,
            "sha256": text_hash(response.text),
            "response": response.to_dict(),
            "response_evidence": response_evidence,
        }
        store.write_json(artifact_name, saved)
        store.mark_stage("synthesis" if round_index == 0 else f"amendment-{round_index}", "completed", artifact_name)
        return response.text, saved

    def _review_artifact(
        self,
        task: str,
        artifact: str,
        profile: Mapping[str, Any],
        budget: BudgetTracker,
        store: RunStore,
        mechanical_evidence: str,
        round_index: int,
        artifact_author_seat: Optional[str],
    ) -> Dict[str, Any]:
        gates = profile.get("gates", {})
        artifact_hash = text_hash(artifact)
        if not isinstance(gates, Mapping) or gates.get("enabled", True) is not True:
            return {
                "enabled": False,
                "passed": True,
                "artifact_sha256": artifact_hash,
                "reviewers": [],
                "required_passes": 0,
            }
        artifact_name = f"gate-{round_index}.json"
        gate_stage = "gate" if round_index == 0 else f"gate-{round_index}"
        reviewer_names = list(gates.get("reviewers", []))
        if not reviewer_names:
            raise ConfigError("Adversarial gates are enabled but no reviewers are configured")
        if any(not isinstance(reviewer_name, str) for reviewer_name in reviewer_names):
            raise ConfigError("gates.reviewers must contain only string seat names")
        duplicate_reviewers = _duplicate_seat_names(reviewer_names)
        if duplicate_reviewers:
            raise ConfigError(
                f"gates.reviewers must not contain duplicate seat names {duplicate_reviewers}"
            )
        for reviewer_name in reviewer_names:
            self._seat_config(reviewer_name)
        require_author_separation = gates.get("exclude_artifact_author", True)
        if (
            require_author_separation
            and artifact_author_seat is not None
            and artifact_author_seat in reviewer_names
        ):
            raise ConfigError(
                "Gate author separation requires reviewer seats distinct from the actual artifact author "
                f"{artifact_author_seat!r}"
            )
        objective = str(profile.get("objective", "Deliver the most correct, complete, and executable result."))

        def gate_request(reviewer_name: str) -> Tuple[str, str, Dict[str, Any]]:
            reviewer_seat = self._seat_config(reviewer_name)
            system = gate_system(
                objective,
                str(reviewer_seat.get("persona", "")),
                str(reviewer_seat.get("context_bundle", "")),
            )
            prompt = gate_prompt(task, artifact, artifact_hash, mechanical_evidence)
            invocation = _invocation_payload(
                store,
                gate_stage,
                reviewer_name,
                system,
                prompt,
                VERDICT_SCHEMA,
                "adversarial_verdict",
            )
            return system, prompt, invocation

        invocations_by_reviewer = {
            reviewer_name: gate_request(reviewer_name)[2]
            for reviewer_name in reviewer_names
        }
        if store.exists(artifact_name):
            saved = store.read_json(artifact_name)
            if saved.get("artifact_sha256") != artifact_hash:
                raise ConfigError("Stored gate artifact hash does not match the current synthesis")
            saved_reviews = _validated_cached_gate_reviews(
                store,
                saved.get("reviewers"),
                reviewer_names,
                gates,
                artifact_hash,
                invocations_by_reviewer,
            )
            result = _gate_result_from_reviews(
                saved_reviews,
                reviewer_names,
                gates,
                artifact_hash,
                mechanical_evidence,
            )
            store.write_json(artifact_name, result)
            store.mark_stage(
                f"gate-{round_index}",
                "passed" if result["passed"] else "rejected",
                artifact_name,
            )
            return result
        committed_reviewers = [
            reviewer_name
            for reviewer_name in reviewer_names
            if _has_response_evidence_for_invocation(
                store,
                invocations_by_reviewer[reviewer_name],
            )
        ]
        if committed_reviewers:
            raise ConfigError(
                "Persisted gate response evidence exists without a reusable gate artifact for "
                + ", ".join(committed_reviewers)
            )
        reserved_reviewers = [
            reviewer_name
            for reviewer_name in reviewer_names
            if _has_persisted_attempt_for_invocation(
                store,
                invocations_by_reviewer[reviewer_name],
            )
        ]
        if reserved_reviewers:
            raise ConfigError(
                "Persisted gate attempt evidence exists without a reusable gate artifact for "
                + ", ".join(reserved_reviewers)
            )
        max_concurrency = min(int(gates.get("max_concurrency", 2)), len(reviewer_names))

        def review_worker(reviewer_name: str) -> Dict[str, Any]:
            system, prompt, _invocation = gate_request(reviewer_name)
            response, response_evidence = self._call(
                budget,
                store,
                gate_stage,
                reviewer_name,
                system,
                prompt,
                VERDICT_SCHEMA,
                "adversarial_verdict",
            )
            try:
                verdict = validate_verdict(parse_json_object(response.text), artifact_hash)
            except ProviderError as exc:
                return {
                    "seat_name": reviewer_name,
                    "status": "failed",
                    "failure_kind": "schema_invalid",
                    "error": str(exc),
                    "response": response.to_dict(),
                    "response_evidence": response_evidence,
                }
            allowed_verdicts = gates.get("allowed_verdicts", ["PASS", "NEEDS_WORK", "FAIL"])
            if isinstance(allowed_verdicts, list) and verdict["verdict"] not in allowed_verdicts:
                raise ProviderError(f"Reviewer returned verdict {verdict['verdict']!r}, which this profile disallows")
            return {
                "seat_name": reviewer_name,
                "status": "completed",
                "verdict": verdict,
                "response": response.to_dict(),
                "response_evidence": response_evidence,
            }

        reviews: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix="inception-gate") as executor:
            futures = {executor.submit(review_worker, name): name for name in reviewer_names}
            for future in as_completed(futures):
                reviewer_name = futures[future]
                try:
                    reviews.append(future.result())
                except BudgetExceeded:
                    for pending in futures:
                        pending.cancel()
                    raise
                except Exception as exc:
                    reviews.append(
                        {
                            "seat_name": reviewer_name,
                            "status": "failed",
                            "failure_kind": "provider_failure",
                            "error": str(exc),
                        }
                    )

        result = _gate_result_from_reviews(
            reviews,
            reviewer_names,
            gates,
            artifact_hash,
            mechanical_evidence,
        )
        store.write_json(artifact_name, result)
        store.mark_stage(
            f"gate-{round_index}",
            "passed" if result["passed"] else "rejected",
            artifact_name,
        )
        return result

    @staticmethod
    def _gate_feedback(gate: Mapping[str, Any]) -> str:
        feedback: List[Dict[str, Any]] = []
        for blocker in gate.get("deterministic_blockers", []):
            feedback.append({"deterministic_blocker": str(blocker)})
        for review in gate.get("reviewers", []):
            if review.get("status") == "completed":
                verdict = review.get("verdict", {})
                if verdict.get("verdict") != "PASS":
                    feedback.append(
                        {
                            "summary": verdict.get("summary"),
                            "blind_spots": verdict.get("blind_spots", []),
                            "blocking_findings": verdict.get("blocking_findings", []),
                            "required_actions": verdict.get("required_actions", []),
                            "evidence": verdict.get("evidence", []),
                        }
                    )
            else:
                feedback.append({"reviewer_failure": review.get("error", "unknown reviewer failure")})
        return json.dumps(feedback, ensure_ascii=False, indent=2)

    def fuse(
        self,
        task: str,
        *,
        context: str = "",
        mechanical_evidence: str = "",
        profile_name: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> FusionResult:
        if not task.strip():
            raise ConfigError("Fusion task must not be empty")
        profile = active_profile(self.config, profile_name)
        self._assert_external_provider_access(profile)
        selected_profile_name = str(profile_name or self.config.get("active_profile", ""))
        self._bind_selected_profile(selected_profile_name)
        store = RunStore(
            task,
            self.config,
            run_id,
            input_identity={
                "operation": "fuse",
                "task": task,
                "context": context,
                "mechanical_evidence": mechanical_evidence,
                "profile_name": selected_profile_name,
            },
        )
        budget = BudgetTracker(profile.get("budgets", {}))
        accounting_initialized = False
        try:
            if store.exists("ledger.json"):
                budget.restore(store.read_json("ledger.json"))
            accounting_initialized = True
            store.check_kill()
            fusion_config = profile["fusion"]
            artifact_author_seat = str(fusion_config["synthesizer"])
            configured_engine = str(fusion_config.get("engine", "client_orchestrated"))
            fusion_mode = {
                "client_orchestrated": "client",
                "openrouter_native": "native_openrouter",
            }.get(configured_engine, configured_engine)
            if fusion_mode == "native_openrouter":
                seat_name = profile["fusion"].get("native_fusion_seat")
                if not seat_name:
                    raise ConfigError("native_openrouter mode requires fusion.native_fusion_seat")
                native_settings = profile["fusion"].get("native_openrouter_fusion", {})
                if not isinstance(native_settings, Mapping) or native_settings.get("enabled") is not True:
                    raise ConfigError("native_openrouter mode requires fusion.native_openrouter_fusion.enabled=true")
                rescue_settings = profile.get("rescue", {})
                rescue_enabled = (
                    isinstance(rescue_settings, Mapping)
                    and rescue_settings.get("enabled", True) is True
                )
                allow_fallback = (
                    rescue_enabled
                    and isinstance(native_settings, Mapping)
                    and native_settings.get("fallback_to_client_orchestrated", False) is True
                )
                native_seat = self._seat_config(str(seat_name))
                native_system_text = synthesis_system(
                    str(profile.get("objective", "Maximum-correctness answer")),
                    str(native_seat.get("persona", "")),
                    str(native_seat.get("context_bundle", "")),
                )
                native_prompt_text = panel_prompt(task, context)
                native_invocation = _invocation_payload(
                    store,
                    "native_openrouter_fusion",
                    str(seat_name),
                    native_system_text,
                    native_prompt_text,
                    None,
                    "structured_response",
                )
                fallback_marker_exists = store.exists("native-openrouter-failure.json")
                if store.exists("panel.json") and not fallback_marker_exists:
                    raise ConfigError(
                        "Stored native Fusion panel is missing its validated fallback marker"
                    )
                if fallback_marker_exists:
                    _validated_native_fallback_marker(
                        store.read_json("native-openrouter-failure.json"),
                        store=store,
                        expected_invocation=native_invocation,
                    )
                    if not allow_fallback:
                        raise ConfigError("Stored native Fusion fallback conflicts with the selected profile")
                    if store.exists("synthesis.json"):
                        fallback_synthesis = store.read_json("synthesis.json")
                        expected_fallback_author = str(fusion_config["synthesizer"])
                        if fallback_synthesis.get("mode") != "client_orchestrated":
                            raise ConfigError(
                                "Stored synthesis.json provenance mode must be 'client_orchestrated' for native fallback"
                            )
                        if fallback_synthesis.get("author_seat") != expected_fallback_author:
                            raise ConfigError(
                                "Stored synthesis.json provenance author must match the client fallback synthesizer"
                            )
                    reports = self._run_panel(task, context, mechanical_evidence, profile, budget, store)
                    judgment = self._run_judge(task, reports, profile, budget, store, mechanical_evidence)
                    synthesis, _ = self._run_synthesis(
                        task, context, reports, judgment, profile, budget, store, mechanical_evidence
                    )
                elif store.exists("synthesis.json"):
                    saved_synthesis = store.read_json("synthesis.json")
                    synthesis, _ = _validated_synthesis_artifact(
                        saved_synthesis,
                        store=store,
                        artifact_name="synthesis.json",
                        expected_mode="native_openrouter",
                        expected_author_seat=str(seat_name),
                        expected_invocation=native_invocation,
                    )
                    quality_floor = fusion_config.get("quality_floor", {})
                    if isinstance(quality_floor, Mapping):
                        try:
                            _validate_quality_floor(
                                synthesis,
                                quality_floor,
                                f"Stored native Fusion seat {seat_name}",
                            )
                        except ProviderError as exc:
                            raise ConfigError(str(exc)) from exc
                    artifact_author_seat = str(seat_name)
                    reports = []
                    judgment = _native_openrouter_judgment()
                else:
                    if _persisted_attempt_ids(store, native_invocation):
                        raise ConfigError(
                            "Persisted native Fusion attempt evidence exists without a reusable synthesis or fallback artifact"
                        )
                    try:
                        response, response_evidence = self._call(
                            budget,
                            store,
                            "native_openrouter_fusion",
                            str(seat_name),
                            native_system_text,
                            native_prompt_text,
                        )
                        reports = []
                        judgment = _native_openrouter_judgment()
                        synthesis = response.text
                        artifact_author_seat = str(seat_name)
                        quality_floor = fusion_config.get("quality_floor", {})
                        if isinstance(quality_floor, Mapping):
                            _validate_quality_floor(
                                synthesis,
                                quality_floor,
                                f"Native Fusion seat {seat_name}",
                            )
                        store.write_json(
                            "synthesis.json",
                            {
                                "mode": "native_openrouter",
                                "author_seat": str(seat_name),
                                "text": synthesis,
                                "sha256": text_hash(synthesis),
                                "response": response.to_dict(),
                                "response_evidence": response_evidence,
                            },
                        )
                        store.mark_stage("native-openrouter-fusion", "completed", "synthesis.json")
                    except ProviderError as exc:
                        if not allow_fallback:
                            raise
                        native_attempt_ids = _persisted_attempt_ids(
                            store,
                            native_invocation,
                        )
                        if not native_attempt_ids:
                            raise ConfigError(
                                "Native Fusion failed before any provider attempt; client fallback is not authorized"
                            ) from exc
                        ledger_entries = store.read_json("ledger.json").get("entries", [])
                        native_response_entries = [
                            entry
                            for entry in ledger_entries
                            if isinstance(entry, Mapping)
                            and entry.get("invocation_sha256")
                            == canonical_json_hash(native_invocation)
                            and entry.get("attempt_id") in native_attempt_ids
                        ] if isinstance(ledger_entries, list) else []
                        response_attempt_ids = {
                            entry.get("attempt_id") for entry in native_response_entries
                        }
                        failure_phase = (
                            "semantic"
                            if any(
                                attempt_id in response_attempt_ids
                                for attempt_id in native_attempt_ids
                            )
                            else "transport"
                        )
                        artifact_author_seat = str(fusion_config["synthesizer"])
                        fallback_marker = {
                            "schema_version": 1,
                            "status": "failed",
                            "error": str(exc),
                            "fallback": "client_orchestrated",
                            "failure_phase": failure_phase,
                            "invocation_sha256": canonical_json_hash(native_invocation),
                            "attempt_ids": native_attempt_ids,
                        }
                        if failure_phase == "semantic":
                            fallback_marker["response_entry_ids"] = [
                                entry["entry_id"] for entry in native_response_entries
                            ]
                        store.write_json(
                            "native-openrouter-failure.json",
                            fallback_marker,
                        )
                        _validated_native_fallback_marker(
                            fallback_marker,
                            store=store,
                            expected_invocation=native_invocation,
                        )
                        store.mark_stage("native-openrouter-fusion", "failed", "native-openrouter-failure.json")
                        reports = self._run_panel(task, context, mechanical_evidence, profile, budget, store)
                        judgment = self._run_judge(task, reports, profile, budget, store, mechanical_evidence)
                        synthesis, _ = self._run_synthesis(
                            task, context, reports, judgment, profile, budget, store, mechanical_evidence
                        )
            elif fusion_mode == "client":
                reports = self._run_panel(task, context, mechanical_evidence, profile, budget, store)
                judgment = self._run_judge(task, reports, profile, budget, store, mechanical_evidence)
                synthesis, _ = self._run_synthesis(
                    task, context, reports, judgment, profile, budget, store, mechanical_evidence
                )
            else:
                raise ConfigError(f"Unsupported fusion mode: {fusion_mode}")

            gate = self._review_artifact(
                task,
                synthesis,
                profile,
                budget,
                store,
                mechanical_evidence,
                0,
                artifact_author_seat,
            )
            gate_config = profile.get("gates", {})
            max_amendments = int(gate_config.get("max_revision_cycles", 0))
            amendment_round = 0
            while not gate.get("passed") and amendment_round < max_amendments:
                amendment_round += 1
                prior_artifact_hash = str(gate.get("artifact_sha256", text_hash(synthesis)))
                artifact_author_seat = str(fusion_config["synthesizer"])
                synthesis, _ = self._run_synthesis(
                    task,
                    context,
                    reports,
                    judgment,
                    profile,
                    budget,
                    store,
                    mechanical_evidence,
                    round_index=amendment_round,
                    amendment_feedback=self._gate_feedback(gate),
                )
                amendment_hash = text_hash(synthesis)
                if (
                    gate_config.get("require_independent_amendment", True) is True
                    and amendment_hash == prior_artifact_hash
                ):
                    gate = {
                        "enabled": True,
                        "passed": False,
                        "artifact_sha256": amendment_hash,
                        "pass_count": 0,
                        "required_passes": int(gate_config.get("required_passes", 1)),
                        "fail_closed": True,
                        "deterministic_blockers": [
                            "The amendment is byte-identical to the rejected artifact; a fresh corrected artifact is required."
                        ],
                        "reviewers": [],
                    }
                    amendment_gate_name = f"gate-{amendment_round}.json"
                    store.write_json(amendment_gate_name, gate)
                    store.mark_stage(f"gate-{amendment_round}", "rejected", amendment_gate_name)
                else:
                    gate = self._review_artifact(
                        task,
                        synthesis,
                        profile,
                        budget,
                        store,
                        mechanical_evidence,
                        amendment_round,
                        artifact_author_seat,
                    )

            status = "completed" if gate.get("passed") else "rejected"
            ledger = store.write_budget_snapshot(budget)
            handoff = build_handoff(
                synthesis,
                store.run_id,
                gate,
                profile.get("execution", {}),
                profile_name=selected_profile_name,
                judge=judgment,
                ledger=ledger,
                budgets=profile.get("budgets", {}),
                gates=profile.get("gates", {}),
                native_codex=self.config.get("native_codex", {}),
            )
            store.write_json("execution-handoff.json", handoff)
            panel_metadata = store.read_json("panel.json") if store.exists("panel.json") else {"results": reports}
            result = FusionResult(
                run_id=store.run_id,
                task_hash=store.task_hash,
                config_hash=store.config_hash,
                status=status,
                synthesis=synthesis,
                gate=gate,
                panel=list(panel_metadata.get("results", [])),
                judge=judgment,
                ledger=ledger,
                artifacts_dir=str(store.directory),
                execution_handoff=handoff,
            )
            store.write_json("result.json", result.to_dict())
            store.finish(status)
            return result
        except RunAborted:
            if accounting_initialized:
                store.write_budget_snapshot(budget)
            store.finish("aborted")
            raise
        except Exception:
            if accounting_initialized:
                store.write_budget_snapshot(budget)
            store.finish("failed")
            raise
        finally:
            store.close()

    def adversarial_gate(
        self,
        task: str,
        artifact: str,
        *,
        mechanical_evidence: str = "",
        profile_name: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not task.strip() or not artifact.strip():
            raise ConfigError("Gate task and artifact must not be empty")
        profile = active_profile(self.config, profile_name)
        self._assert_external_provider_access(profile)
        selected_profile_name = str(profile_name or self.config.get("active_profile", ""))
        self._bind_selected_profile(selected_profile_name)
        composite_task = task + "\n\nARTIFACT-SHA256:" + text_hash(artifact)
        store = RunStore(
            composite_task,
            self.config,
            run_id,
            input_identity={
                "operation": "adversarial_gate",
                "task": task,
                "artifact_sha256": text_hash(artifact),
                "mechanical_evidence": mechanical_evidence,
                "profile_name": selected_profile_name,
            },
        )
        budget = BudgetTracker(profile.get("budgets", {}))
        accounting_initialized = False
        try:
            if store.exists("ledger.json"):
                budget.restore(store.read_json("ledger.json"))
            accounting_initialized = True
            store.check_kill()
            # Standalone artifacts are caller-supplied, so no configured seat can
            # truthfully be attributed as their author. Generated Fusion artifacts
            # pass the concrete author seat through fuse() instead.
            gate = self._review_artifact(
                task,
                artifact,
                profile,
                budget,
                store,
                mechanical_evidence,
                0,
                None,
            )
            ledger = store.write_budget_snapshot(budget)
            store.finish("completed" if gate.get("passed") else "rejected")
            return {
                "run_id": store.run_id,
                "artifacts_dir": str(store.directory),
                "gate": gate,
                "ledger": ledger,
            }
        except RunAborted:
            if accounting_initialized:
                store.write_budget_snapshot(budget)
            store.finish("aborted")
            raise
        except Exception:
            if accounting_initialized:
                store.write_budget_snapshot(budget)
            store.finish("failed")
            raise
        finally:
            store.close()

    def run_status(self, run_id: str) -> Dict[str, Any]:
        if not run_id.replace("-", "").isalnum():
            raise ConfigError("Invalid run_id")
        path = Path(self._runtime_runs_dir()) / run_id / "manifest.json"
        if not path.exists():
            raise ConfigError(f"Unknown run_id: {run_id}")
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ConfigError("Run manifest is malformed")
        return value

    @staticmethod
    def _runtime_runs_dir() -> str:
        from .config import runtime_data_dir

        return str(runtime_data_dir() / "runs")
