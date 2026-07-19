"""Bounded independent deliberation, comparative analysis, synthesis, and gates."""

from __future__ import annotations

import json
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
from .state import BudgetTracker, RunStore, text_hash
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

    try:
        structured_evidence = json.loads(stripped)
    except json.JSONDecodeError:
        structured_evidence = None

    def inspect(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            normalized = {str(key).lower(): child for key, child in value.items()}
            for boolean_key in ("passed", "success", "ok"):
                if normalized.get(boolean_key) is False:
                    failures.append(f"{path}.{boolean_key}=false")
            for numeric_key in ("exit_code", "exit_status", "returncode", "return_code"):
                numeric_value = normalized.get(numeric_key)
                if isinstance(numeric_value, int) and not isinstance(numeric_value, bool) and numeric_value != 0:
                    failures.append(f"{path}.{numeric_key}={numeric_value}")
            status = normalized.get("status")
            if isinstance(status, str) and status.strip().lower() in {"error", "failed", "failure"}:
                failures.append(f"{path}.status={status.strip()}")
            reported_failures = normalized.get("failures")
            if isinstance(reported_failures, list) and reported_failures:
                failures.append(f"{path}.failures contains {len(reported_failures)} item(s)")
            for key, child in value.items():
                inspect(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                inspect(child, f"{path}[{index}]")

    if structured_evidence is not None:
        inspect(structured_evidence, "evidence")

    normalized_text = re.sub(r"\b0\s+failed\b", "", stripped, flags=re.IGNORECASE)
    for match in re.finditer(
        r"\b(?:exit(?:ed)?(?:\s+(?:status|code))?|return(?:\s+code|code))\s*[:=]?\s*(-?\d+)\b",
        normalized_text,
        flags=re.IGNORECASE,
    ):
        if int(match.group(1)) != 0:
            failures.append(match.group(0))
    for match in re.finditer(r"\b([1-9]\d*)\s+(?:tests?\s+)?failed\b", normalized_text, flags=re.IGNORECASE):
        failures.append(match.group(0))
    explicit_failure = re.search(
        r"\b(?:assertionerror|assertion\s+failure|tests?\s+(?:failed|failing)|build\s+failed|command\s+failed)\b"
        r"|traceback \(most recent call last\)",
        normalized_text,
        flags=re.IGNORECASE,
    )
    if explicit_failure:
        failures.append(explicit_failure.group(0))
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


def _validated_synthesis_artifact(
    saved: Mapping[str, Any],
    *,
    artifact_name: str,
    expected_mode: str,
    expected_author_seat: str,
) -> Tuple[str, Dict[str, Any]]:
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
    return text, dict(saved)


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
    if verdict == "PASS" and validated["required_actions"]:
        raise ProviderError("PASS verdict cannot include required_actions")
    return validated


def _validated_cached_gate_reviews(
    saved_reviews: Any,
    reviewer_names: Sequence[str],
    gates: Mapping[str, Any],
    artifact_hash: str,
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
            response_text = saved_response.get("text")
            if not isinstance(response_text, str):
                raise ConfigError("Stored completed gate review raw response text must be a string")
            try:
                canonical_verdict = validate_verdict(
                    parse_json_object(response_text),
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
        elif status == "failed":
            if saved_review.get("failure_kind") not in {"provider_failure", "schema_invalid"}:
                raise ConfigError("Stored failed gate review has an invalid failure_kind")
            if not isinstance(saved_review.get("error"), str):
                raise ConfigError("Stored failed gate review must include a string error")
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
    ) -> ModelResponse:
        store.check_kill()

        def reserve_and_persist_attempt() -> None:
            budget.reserve_call(stage, seat_name)
            # A timed-out request may still have reached the provider and may be
            # billable. Persist the reservation before transport starts so a
            # failed retry or process restart cannot erase it.
            store.write_budget_snapshot(budget)

        def persist_and_record_response(response: ModelResponse) -> None:
            # This callback also handles paid HTTP-success responses that fail
            # provider semantics before a model fallback can be attempted.
            response_artifact = {
                "stage": stage,
                "seat_name": seat_name,
                "response": response.to_dict(),
            }
            response_artifact_hash = text_hash(
                json.dumps(
                    response_artifact,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
            )
            store.write_json(f"responses/{response_artifact_hash}.json", response_artifact)
            try:
                budget.record(stage, seat_name, response)
            finally:
                # An over-threshold response is already billable evidence.
                # Persist its usage and stop latch even when record() fails.
                store.write_budget_snapshot(budget)

        response = self.registry.complete(
            seat_name,
            system=system,
            prompt=prompt,
            response_schema=response_schema,
            schema_name=schema_name,
            before_attempt=reserve_and_persist_attempt,
            on_semantic_failure_response=persist_and_record_response,
        )
        persist_and_record_response(response)
        store.check_kill()
        return response

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
        seat_names = seat_names[:max_panel_seats]
        objective = str(profile.get("objective", "Deliver the most correct, complete, and executable result."))
        stored_panel = store.read_json("panel.json") if store.exists("panel.json") else {}
        stored_results = stored_panel.get("results", [])
        if not isinstance(stored_results, list):
            raise ConfigError("Stored panel artifact results must be an array")
        latest_by_seat: Dict[str, Dict[str, Any]] = {
            str(row["seat_name"]): dict(row)
            for row in stored_results
            if isinstance(row, Mapping) and row.get("seat_name") in seat_names
        }
        stored_attempts = stored_panel.get("attempts", stored_results)
        attempts: List[Dict[str, Any]] = [
            dict(row) for row in stored_attempts if isinstance(row, Mapping)
        ] if isinstance(stored_attempts, list) else []
        pending_seat_names = [
            seat_name
            for seat_name in seat_names
            if latest_by_seat.get(seat_name, {}).get("status") != "completed"
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
            seat = self._seat_config(seat_name)
            role = str(seat.get("role", "domain analyst"))
            persona = str(seat.get("persona", "Find the most important truth other reviewers may miss."))
            response = self._call(
                budget,
                store,
                "panel",
                seat_name,
                panel_system(role, persona, objective),
                panel_prompt(
                    task,
                    _panel_context_bundle(
                        context,
                        mechanical_evidence,
                        str(seat.get("context_bundle", "full_task_and_evidence")),
                        fusion.get("partition_context", True) is True,
                    ),
                ),
            )
            quality_floor = fusion.get("quality_floor", {})
            if isinstance(quality_floor, Mapping):
                _validate_quality_floor(response.text, quality_floor, f"Seat {seat_name}")
            return SeatResult(seat_name=seat_name, anonymous_label="", role=role, status="completed", response=response)

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
        if store.exists("judge.json"):
            return store.read_json("judge.json")["judgment"]
        fusion = profile["fusion"]
        objective = str(profile.get("objective", "Deliver the most correct, complete, and executable result."))
        live_reports = [report for report in reports if report.get("status") == "completed"]
        judge_schema, required_fields = _judge_contract(profile)
        judge_seat = self._seat_config(str(fusion["judge"]))
        response = self._call(
            budget,
            store,
            "judge",
            str(fusion["judge"]),
            judge_system(
                objective,
                str(judge_seat.get("persona", "")),
                str(judge_seat.get("context_bundle", "")),
            ),
            judge_prompt(task, live_reports, mechanical_evidence),
            judge_schema,
            "fusion_judgment",
        )
        judgment = validate_judgment(parse_json_object(response.text), required_fields)
        store.write_json("judge.json", {"judgment": judgment, "response": response.to_dict()})
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
        fusion = profile["fusion"]
        synthesizer_name = str(fusion["synthesizer"])
        if store.exists(artifact_name):
            saved = store.read_json(artifact_name)
            return _validated_synthesis_artifact(
                saved,
                artifact_name=artifact_name,
                expected_mode="client_orchestrated",
                expected_author_seat=synthesizer_name,
            )
        objective = str(profile.get("objective", "Deliver the most correct, complete, and executable result."))
        live_reports = [report for report in reports if report.get("status") == "completed"]
        synthesizer_seat = self._seat_config(synthesizer_name)
        if (
            fusion.get("separate_no_tools_synthesis_turn") is True
            and synthesizer_seat.get("tool_policy") != "none"
        ):
            raise ConfigError("separate_no_tools_synthesis_turn requires a tool-less synthesizer seat")
        response = self._call(
            budget,
            store,
            "synthesis" if round_index == 0 else "amendment",
            synthesizer_name,
            synthesis_system(
                objective,
                str(synthesizer_seat.get("persona", "")),
                str(synthesizer_seat.get("context_bundle", "")),
            ),
            synthesis_prompt(
                task,
                context,
                live_reports,
                judgment,
                mechanical_evidence,
                amendment_feedback,
            ),
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
        if store.exists(artifact_name):
            saved = store.read_json(artifact_name)
            if saved.get("artifact_sha256") != artifact_hash:
                raise ConfigError("Stored gate artifact hash does not match the current synthesis")
            saved_reviews = _validated_cached_gate_reviews(
                saved.get("reviewers"),
                reviewer_names,
                gates,
                artifact_hash,
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
        objective = str(profile.get("objective", "Deliver the most correct, complete, and executable result."))
        max_concurrency = min(int(gates.get("max_concurrency", 2)), len(reviewer_names))

        def review_worker(reviewer_name: str) -> Dict[str, Any]:
            reviewer_seat = self._seat_config(reviewer_name)
            response = self._call(
                budget,
                store,
                "gate",
                reviewer_name,
                gate_system(
                    objective,
                    str(reviewer_seat.get("persona", "")),
                    str(reviewer_seat.get("context_bundle", "")),
                ),
                gate_prompt(task, artifact, artifact_hash, mechanical_evidence),
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
                }
            allowed_verdicts = gates.get("allowed_verdicts", ["PASS", "NEEDS_WORK", "FAIL"])
            if isinstance(allowed_verdicts, list) and verdict["verdict"] not in allowed_verdicts:
                raise ProviderError(f"Reviewer returned verdict {verdict['verdict']!r}, which this profile disallows")
            return {"seat_name": reviewer_name, "status": "completed", "verdict": verdict, "response": response.to_dict()}

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
                native_fallback_started = store.exists("native-openrouter-failure.json") or store.exists("panel.json")
                if native_fallback_started:
                    if not allow_fallback:
                        raise ConfigError("Stored native Fusion fallback conflicts with the selected profile")
                    if store.exists("synthesis.json"):
                        _validated_synthesis_artifact(
                            store.read_json("synthesis.json"),
                            artifact_name="synthesis.json",
                            expected_mode="client_orchestrated",
                            expected_author_seat=str(fusion_config["synthesizer"]),
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
                        artifact_name="synthesis.json",
                        expected_mode="native_openrouter",
                        expected_author_seat=str(seat_name),
                    )
                    artifact_author_seat = str(seat_name)
                    reports = []
                    judgment = _native_openrouter_judgment()
                else:
                    try:
                        native_seat = self._seat_config(str(seat_name))
                        response = self._call(
                            budget,
                            store,
                            "native_openrouter_fusion",
                            str(seat_name),
                            synthesis_system(
                                str(profile.get("objective", "Maximum-correctness answer")),
                                str(native_seat.get("persona", "")),
                                str(native_seat.get("context_bundle", "")),
                            ),
                            panel_prompt(task, context),
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
                            },
                        )
                        store.mark_stage("native-openrouter-fusion", "completed", "synthesis.json")
                    except ProviderError as exc:
                        if not allow_fallback:
                            raise
                        artifact_author_seat = str(fusion_config["synthesizer"])
                        store.write_json(
                            "native-openrouter-failure.json",
                            {"status": "failed", "error": str(exc), "fallback": "client_orchestrated"},
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
