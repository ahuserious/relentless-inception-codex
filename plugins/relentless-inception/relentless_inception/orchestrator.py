"""Bounded independent deliberation, comparative analysis, synthesis, and gates."""

from __future__ import annotations

import json
import random
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


def _validate_string_list(value: Any, field: str) -> List[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ProviderError(f"Structured response field {field!r} must be an array of strings")
    return value


def validate_judgment(value: Mapping[str, Any]) -> Dict[str, Any]:
    unexpected = set(value) - set(JUDGE_FIELDS)
    missing = set(JUDGE_FIELDS) - set(value)
    if unexpected or missing:
        raise ProviderError(f"Judge schema mismatch; missing={sorted(missing)}, unexpected={sorted(unexpected)}")
    return {field: _validate_string_list(value[field], field) for field in JUDGE_FIELDS}


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
    for field in ("blocking_findings", "non_blocking_findings", "evidence", "required_actions"):
        validated[field] = _validate_string_list(value[field], field)
    return validated


class FusionOrchestrator:
    def __init__(self, config: Optional[Mapping[str, Any]] = None, registry: Optional[ProviderRegistry] = None) -> None:
        self.config = dict(config) if config is not None else load_config()
        self.registry = registry or ProviderRegistry(self.config)

    def _seat_config(self, seat_name: str) -> Mapping[str, Any]:
        seat = self.config.get("seats", {}).get(seat_name)
        if not isinstance(seat, Mapping):
            raise ConfigError(f"Unknown seat: {seat_name}")
        return seat

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
        budget.reserve_call(stage, seat_name)
        response = self.registry.complete(
            seat_name,
            system=system,
            prompt=prompt,
            response_schema=response_schema,
            schema_name=schema_name,
        )
        budget.record(stage, seat_name, response)
        store.write_json("ledger.json", budget.snapshot())
        store.check_kill()
        return response

    def _run_panel(
        self,
        task: str,
        context: str,
        profile: Mapping[str, Any],
        budget: BudgetTracker,
        store: RunStore,
    ) -> List[Dict[str, Any]]:
        if store.exists("panel.json"):
            return list(store.read_json("panel.json").get("results", []))
        fusion = profile["fusion"]
        seat_names = list(fusion["panel"])
        for optional_seat_name in fusion.get("optional_panel", []):
            optional_seat = self._seat_config(str(optional_seat_name))
            provider = self.config.get("providers", {}).get(optional_seat.get("provider"), {})
            if optional_seat.get("enabled", True) is True and isinstance(provider, Mapping) and provider.get("enabled", True) is True:
                seat_names.append(str(optional_seat_name))
        max_panel_seats = int(fusion.get("max_panel_seats", len(seat_names)))
        seat_names = seat_names[:max_panel_seats]
        objective = str(profile.get("objective", "Deliver the most correct, complete, and executable result."))
        max_concurrency = min(int(fusion.get("max_concurrency", 2)), len(seat_names))

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
                panel_prompt(task, context),
            )
            quality_floor = fusion.get("quality_floor", {})
            minimum_characters = int(quality_floor.get("minimum_characters", 1)) if isinstance(quality_floor, Mapping) else 1
            if len(response.text.strip()) < minimum_characters:
                raise ProviderError(
                    f"Seat {seat_name} response was below the profile quality floor of {minimum_characters} characters"
                )
            if isinstance(quality_floor, Mapping) and quality_floor.get("reject_tool_markup", False):
                lowered = response.text.lower()
                if "<tool_call>" in lowered or "<function_call>" in lowered:
                    raise ProviderError(f"Seat {seat_name} leaked tool-call markup")
            if isinstance(quality_floor, Mapping) and quality_floor.get("reject_refusal_without_policy_reason", False):
                normalized = response.text.strip().lower()
                refusal_prefixes = (
                    "i can't assist",
                    "i cannot assist",
                    "i'm unable to help",
                    "i am unable to help",
                    "sorry, but i can't",
                )
                if normalized.startswith(refusal_prefixes) and "policy" not in normalized:
                    raise ProviderError(f"Seat {seat_name} returned an ungrounded refusal")
            return SeatResult(seat_name=seat_name, anonymous_label="", role=role, status="completed", response=response)

        completed: List[SeatResult] = []
        failures: List[SeatResult] = []
        with ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix="inception-panel") as executor:
            futures: Dict[Future[SeatResult], str] = {
                executor.submit(panel_worker, seat_name): seat_name for seat_name in seat_names
            }
            for future in as_completed(futures):
                seat_name = futures[future]
                try:
                    completed.append(future.result())
                except BudgetExceeded:
                    for pending in futures:
                        pending.cancel()
                    raise
                except Exception as exc:
                    seat = self._seat_config(seat_name)
                    failures.append(
                        SeatResult(
                            seat_name=seat_name,
                            anonymous_label="",
                            role=str(seat.get("role", "domain analyst")),
                            status="failed",
                            error=str(exc),
                        )
                    )

        min_live = int(fusion.get("min_live_seats", len(seat_names)))
        if len(completed) < min_live:
            failure_summary = "; ".join(f"{failure.seat_name}: {failure.error}" for failure in failures)
            raise ProviderError(f"Panel collapsed: {len(completed)}/{len(seat_names)} live; {failure_summary}")
        rescue = profile.get("rescue", {})
        allow_degradation = fusion.get(
            "allow_degradation",
            rescue.get("allow_degraded_single_provider", False) if isinstance(rescue, Mapping) else False,
        )
        if failures and allow_degradation is not True:
            failure_summary = "; ".join(f"{failure.seat_name}: {failure.error}" for failure in failures)
            raise ProviderError(f"Panel degradation is disabled; {failure_summary}")

        # Deterministic task-local shuffle hides model identity without making resumes unstable.
        if fusion.get("randomize_panel_order", True):
            random.Random(store.task_hash).shuffle(completed)
        for index, result in enumerate(completed):
            result.anonymous_label = (
                f"Seat {string.ascii_uppercase[index]}"
                if fusion.get("anonymize_model_identity", True)
                else result.seat_name
            )
        results = [result.to_dict() for result in [*completed, *failures]]
        store.write_json(
            "panel.json",
            {
                "results": results,
                "live_count": len(completed),
                "failed_count": len(failures),
                "degraded": bool(failures),
            },
        )
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
        response = self._call(
            budget,
            store,
            "judge",
            str(fusion["judge"]),
            judge_system(objective),
            judge_prompt(task, live_reports, mechanical_evidence),
            JUDGE_SCHEMA,
            "fusion_judgment",
        )
        judgment = validate_judgment(parse_json_object(response.text))
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
        if store.exists(artifact_name):
            saved = store.read_json(artifact_name)
            return str(saved["text"]), saved
        fusion = profile["fusion"]
        objective = str(profile.get("objective", "Deliver the most correct, complete, and executable result."))
        live_reports = [report for report in reports if report.get("status") == "completed"]
        response = self._call(
            budget,
            store,
            "synthesis" if round_index == 0 else "amendment",
            str(fusion["synthesizer"]),
            synthesis_system(objective),
            synthesis_prompt(
                task,
                context,
                live_reports,
                judgment,
                mechanical_evidence,
                amendment_feedback,
            ),
        )
        saved = {"text": response.text, "sha256": text_hash(response.text), "response": response.to_dict()}
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
        if store.exists(artifact_name):
            saved = store.read_json(artifact_name)
            if saved.get("artifact_sha256") != artifact_hash:
                raise ConfigError("Stored gate artifact hash does not match the current synthesis")
            return saved
        reviewer_names = list(gates.get("reviewers", []))
        if not reviewer_names:
            raise ConfigError("Adversarial gates are enabled but no reviewers are configured")
        synthesizer = str(profile["fusion"]["synthesizer"])
        require_author_separation = gates.get("require_author_separation", gates.get("exclude_artifact_author", True))
        if require_author_separation and synthesizer in reviewer_names:
            raise ConfigError("Gate author separation requires reviewer seats distinct from the synthesizer")
        objective = str(profile.get("objective", "Deliver the most correct, complete, and executable result."))
        max_concurrency = min(int(gates.get("max_concurrency", 2)), len(reviewer_names))

        def review_worker(reviewer_name: str) -> Dict[str, Any]:
            response = self._call(
                budget,
                store,
                "gate",
                reviewer_name,
                gate_system(objective),
                gate_prompt(task, artifact, artifact_hash, mechanical_evidence),
                VERDICT_SCHEMA,
                "adversarial_verdict",
            )
            verdict = validate_verdict(parse_json_object(response.text), artifact_hash)
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
                    reviews.append({"seat_name": reviewer_name, "status": "failed", "error": str(exc)})

        pass_count = sum(
            1 for review in reviews if review.get("status") == "completed" and review["verdict"]["verdict"] == "PASS"
        )
        required_passes = int(gates.get("required_passes", len(reviewer_names)))
        fail_closed = gates.get("fail_closed", True) is True
        failed_reviews = any(review.get("status") != "completed" for review in reviews)
        passed = pass_count >= required_passes and (not failed_reviews or not fail_closed)
        result = {
            "enabled": True,
            "passed": passed,
            "artifact_sha256": artifact_hash,
            "pass_count": pass_count,
            "required_passes": required_passes,
            "fail_closed": fail_closed,
            "reviewers": reviews,
        }
        store.write_json(artifact_name, result)
        store.mark_stage(f"gate-{round_index}", "passed" if passed else "rejected", artifact_name)
        return result

    @staticmethod
    def _gate_feedback(gate: Mapping[str, Any]) -> str:
        feedback: List[Dict[str, Any]] = []
        for review in gate.get("reviewers", []):
            if review.get("status") == "completed":
                verdict = review.get("verdict", {})
                if verdict.get("verdict") != "PASS":
                    feedback.append(
                        {
                            "summary": verdict.get("summary"),
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
        store = RunStore(task, self.config, run_id)
        budget = BudgetTracker(profile.get("budgets", {}))
        if store.exists("ledger.json"):
            budget.restore(store.read_json("ledger.json"))
        store.check_kill()
        try:
            fusion_config = profile["fusion"]
            configured_engine = str(fusion_config.get("mode", fusion_config.get("engine", "client_orchestrated")))
            fusion_mode = {
                "client_orchestrated": "client",
                "openrouter_native": "native_openrouter",
                "native_openrouter_fusion": "native_openrouter",
            }.get(configured_engine, configured_engine)
            if fusion_mode == "native_openrouter":
                seat_name = profile["fusion"].get("native_fusion_seat")
                if not seat_name:
                    raise ConfigError("native_openrouter mode requires fusion.native_fusion_seat")
                try:
                    response = self._call(
                        budget,
                        store,
                        "native_openrouter_fusion",
                        str(seat_name),
                        synthesis_system(str(profile.get("objective", "Maximum-correctness answer"))),
                        panel_prompt(task, context),
                    )
                    reports = []
                    judgment = {
                        "consensus": [],
                        "contradictions": [],
                        "partial_coverage": [],
                        "unique_insights": [],
                        "minority_findings": [],
                        "blind_spots": ["Native OpenRouter Fusion does not expose raw inner-seat artifacts."],
                        "verification_priorities": ["Apply an independent external adversarial gate."],
                        "final_guidance": [],
                    }
                    synthesis = response.text
                    store.write_json("synthesis.json", {"text": synthesis, "sha256": text_hash(synthesis), "response": response.to_dict()})
                except ProviderError as exc:
                    native_settings = profile["fusion"].get("native_openrouter_fusion", {})
                    allow_fallback = isinstance(native_settings, Mapping) and native_settings.get(
                        "fallback_to_client_orchestrated", False
                    )
                    if not allow_fallback:
                        raise
                    store.write_json(
                        "native-openrouter-failure.json",
                        {"status": "failed", "error": str(exc), "fallback": "client_orchestrated"},
                    )
                    reports = self._run_panel(task, context, profile, budget, store)
                    judgment = self._run_judge(task, reports, profile, budget, store, mechanical_evidence)
                    synthesis, _ = self._run_synthesis(
                        task, context, reports, judgment, profile, budget, store, mechanical_evidence
                    )
            elif fusion_mode in {"client", "hybrid"}:
                reports = self._run_panel(task, context, profile, budget, store)
                judgment = self._run_judge(task, reports, profile, budget, store, mechanical_evidence)
                synthesis, _ = self._run_synthesis(
                    task, context, reports, judgment, profile, budget, store, mechanical_evidence
                )
            else:
                raise ConfigError(f"Unsupported fusion mode: {fusion_mode}")

            gate = self._review_artifact(task, synthesis, profile, budget, store, mechanical_evidence, 0)
            gate_config = profile.get("gates", {})
            max_amendments = int(gate_config.get("max_amendment_rounds", gate_config.get("max_revision_cycles", 0)))
            amendment_round = 0
            while not gate.get("passed") and amendment_round < max_amendments:
                amendment_round += 1
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
                gate = self._review_artifact(
                    task, synthesis, profile, budget, store, mechanical_evidence, amendment_round
                )

            status = "completed" if gate.get("passed") else "rejected"
            handoff = build_handoff(synthesis, store.run_id, gate, profile.get("execution", {}))
            ledger = budget.snapshot()
            store.write_json("ledger.json", ledger)
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
            store.write_json("ledger.json", budget.snapshot())
            store.finish("aborted")
            raise
        except Exception:
            store.write_json("ledger.json", budget.snapshot())
            store.finish("failed")
            raise

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
        composite_task = task + "\n\nARTIFACT-SHA256:" + text_hash(artifact)
        store = RunStore(composite_task, self.config, run_id)
        budget = BudgetTracker(profile.get("budgets", {}))
        gate = self._review_artifact(task, artifact, profile, budget, store, mechanical_evidence, 0)
        store.write_json("ledger.json", budget.snapshot())
        store.finish("completed" if gate.get("passed") else "rejected")
        return {"run_id": store.run_id, "artifacts_dir": str(store.directory), "gate": gate, "ledger": budget.snapshot()}

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
