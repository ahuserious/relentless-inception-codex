from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

from tests.support import DEFAULT_PANEL, FakeProviderRegistry, orchestration_config

from relentless_inception.errors import BudgetExceeded, ConfigError, ProviderError, RunAborted
from relentless_inception.orchestrator import (
    FusionOrchestrator,
    _contains_substantive_claim,
)
from relentless_inception.providers import ProviderRegistry
from relentless_inception.state import (
    BudgetTracker,
    RunStore,
    call_receipt_entry_id,
    canonical_json_hash,
    text_hash,
)


class VerdictOverrideRegistry(FakeProviderRegistry):
    def __init__(self, verdict_overrides: Mapping[str, Mapping[str, Any]]) -> None:
        super().__init__()
        self.verdict_overrides = verdict_overrides

    def complete(self, seat_name: str, **kwargs: Any):
        response = super().complete(seat_name, **kwargs)
        if kwargs.get("schema_name") != "adversarial_verdict" or seat_name not in self.verdict_overrides:
            return response

        hash_match = re.search(
            r"Candidate artifact SHA-256: ([0-9a-f]{64})",
            str(kwargs["prompt"]),
        )
        if hash_match is None:
            raise AssertionError("Gate prompt did not supply an exact artifact SHA-256")
        verdict = {
            "verdict": "PASS",
            "artifact_sha256": hash_match.group(1),
            "summary": "The configured test verdict exercises fail-closed aggregation.",
            "criteria_reviewed": ["Gate aggregation semantics"],
            "blind_spots": [],
            "blocking_findings": [],
            "non_blocking_findings": [],
            "evidence": ["Deterministic fake-provider evidence."],
            "required_actions": [],
        }
        verdict.update(copy.deepcopy(dict(self.verdict_overrides[seat_name])))
        response.text = json.dumps(verdict)
        return response


class OrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.environment_patch = mock.patch.dict(
            os.environ,
            {
                "RELENTLESS_INCEPTION_DATA_DIR": self.temporary_directory.name,
                "RELENTLESS_INCEPTION_CONFIG": str(Path(self.temporary_directory.name) / "user-config.json"),
            },
            clear=False,
        )
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)

    def test_external_provider_deny_blocks_fusion_and_gate_before_dispatch(self) -> None:
        config = orchestration_config()
        config["profiles"]["maximum_intelligence"]["privacy"]["external_provider_access"] = "deny"
        registry = FakeProviderRegistry()
        orchestrator = FusionOrchestrator(config, registry)

        with self.assertRaisesRegex(ConfigError, "denies external provider access"):
            orchestrator.fuse("Must not leave the machine.", run_id="privacy-denied-fusion")
        with self.assertRaisesRegex(ConfigError, "denies external provider access"):
            orchestrator.adversarial_gate(
                "Must not leave the machine.",
                "artifact",
                run_id="privacy-denied-gate",
            )

        self.assertEqual(registry.calls, [])
        runs_directory = Path(self.temporary_directory.name) / "runs"
        self.assertFalse((runs_directory / "privacy-denied-fusion").exists())
        self.assertFalse((runs_directory / "privacy-denied-gate").exists())

    def test_runtime_rejects_duplicate_or_overlapping_independent_seats_before_dispatch(self) -> None:
        duplicate_panel_config = orchestration_config(
            panel=["grok45_researcher", "grok45_researcher"]
        )
        duplicate_panel_registry = FakeProviderRegistry()
        with self.assertRaisesRegex(ConfigError, "fusion.panel must not contain duplicate seat names"):
            FusionOrchestrator(duplicate_panel_config, duplicate_panel_registry).fuse(
                "Duplicate panel seats are not independent.",
                run_id="duplicate-panel-runtime",
            )
        self.assertEqual(duplicate_panel_registry.calls, [])

        duplicate_optional_config = orchestration_config()
        duplicate_optional_config["profiles"]["maximum_intelligence"]["fusion"][
            "optional_panel"
        ] = ["openrouter_sol_pro_panel", "openrouter_sol_pro_panel"]
        duplicate_optional_registry = FakeProviderRegistry()
        with self.assertRaisesRegex(
            ConfigError,
            "fusion.optional_panel must not contain duplicate seat names",
        ):
            FusionOrchestrator(duplicate_optional_config, duplicate_optional_registry).fuse(
                "Duplicate optional seats are not independent.",
                run_id="duplicate-optional-panel-runtime",
            )
        self.assertEqual(duplicate_optional_registry.calls, [])

        overlapping_panel_config = orchestration_config()
        overlapping_panel_config["profiles"]["maximum_intelligence"]["fusion"][
            "optional_panel"
        ] = [DEFAULT_PANEL[0]]
        overlapping_panel_registry = FakeProviderRegistry()
        with self.assertRaisesRegex(ConfigError, "fusion.panel and optional_panel must not overlap"):
            FusionOrchestrator(overlapping_panel_config, overlapping_panel_registry).fuse(
                "Required and optional panel seats cannot overlap.",
                run_id="overlapping-panel-runtime",
            )
        self.assertEqual(overlapping_panel_registry.calls, [])

        duplicate_reviewer_config = orchestration_config()
        duplicate_reviewer_config["profiles"]["maximum_intelligence"]["gates"]["reviewers"] = [
            "grok45_verifier",
            "grok45_verifier",
        ]
        duplicate_reviewer_registry = FakeProviderRegistry()
        with self.assertRaisesRegex(ConfigError, "gates.reviewers must not contain duplicate seat names"):
            FusionOrchestrator(duplicate_reviewer_config, duplicate_reviewer_registry).adversarial_gate(
                "Duplicate reviewers cannot satisfy quorum.",
                "Candidate artifact",
                run_id="duplicate-reviewer-runtime",
            )
        self.assertEqual(duplicate_reviewer_registry.calls, [])

    def test_substantive_claim_floor_rejects_heading_only_panel_output(self) -> None:
        self.assertFalse(_contains_substantive_claim("# Analysis\n## Risks\n- TBD\n- Unknown"))
        self.assertTrue(
            _contains_substantive_claim(
                "Use an atomic pre-dispatch reservation so concurrent retries cannot exceed the call ceiling."
            )
        )

        config = orchestration_config()
        config["profiles"]["maximum_intelligence"]["fusion"]["quality_floor"][
            "require_nonempty_claims"
        ] = True
        config["profiles"]["maximum_intelligence"]["fusion"]["min_live_seats"] = 2
        registry = FakeProviderRegistry()
        registry.PANEL_TEXTS = dict(FakeProviderRegistry.PANEL_TEXTS)
        registry.PANEL_TEXTS["grok45_researcher"] = "# Analysis\n## Risks\n- TBD\n- Unknown"

        with self.assertRaisesRegex(ProviderError, "Panel degradation is disabled"):
            FusionOrchestrator(config, registry).fuse(
                "Reject and preserve a paid response with no substantive claim.",
                run_id="substantive-claim-failure",
            )

        response_paths = sorted(
            (Path(self.temporary_directory.name) / "runs" / "substantive-claim-failure" / "responses").glob("*.json")
        )
        self.assertEqual(len(response_paths), 3)
        persisted_responses = [json.loads(path.read_text(encoding="utf-8")) for path in response_paths]
        rejected = [
            row
            for row in persisted_responses
            if row["invocation"]["seat_name"] == "grok45_researcher"
        ]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["response"]["text"], "# Analysis\n## Risks\n- TBD\n- Unknown")

    def test_billed_semantic_failure_is_ledgered_and_budget_latches_before_fallback(self) -> None:
        primary_payload = {
            "id": "paid-primary-request",
            "status": "completed",
            "model": "paid-primary-live",
            "output": [],
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 2000,
                "cost": 12.34,
            },
        }
        fallback_payload = {
            "id": "fallback-request",
            "status": "completed",
            "model": "fallback-live",
            "output_text": "fallback answer",
            "usage": {"input_tokens": 10, "output_tokens": 2, "cost": 0.01},
        }

        def run_call(max_cost_usd: float, run_id: str):
            config = orchestration_config()
            profile = config["profiles"]["maximum_intelligence"]
            profile["budgets"]["max_cost_usd"] = max_cost_usd
            seat = config["seats"]["grok45_researcher"]
            seat["allow_model_fallbacks"] = True
            seat["fallback_models"] = ["grok-fallback"]
            registry = ProviderRegistry(config)
            orchestrator = FusionOrchestrator(config, registry)
            budget = BudgetTracker(profile["budgets"])
            payloads = iter((primary_payload, fallback_payload))

            def fake_post_json(
                _url,
                _payload,
                _provider,
                *,
                before_attempt=None,
                on_invalid_response=None,
            ):
                del on_invalid_response
                self.assertIsNotNone(before_attempt)
                before_attempt()
                return next(payloads), {}, 0.1

            with RunStore("Billed fallback fixture", config, run_id) as store:
                with mock.patch.object(
                    registry,
                    "_post_json",
                    side_effect=fake_post_json,
                ) as post_json:
                    if max_cost_usd < 12.34:
                        with self.assertRaisesRegex(BudgetExceeded, "Known cost threshold"):
                            orchestrator._call(
                                budget,
                                store,
                                "synthesis",
                                "grok45_researcher",
                                "system",
                                "prompt",
                            )
                        result = None
                    else:
                        result = orchestrator._call(
                            budget,
                            store,
                            "synthesis",
                            "grok45_researcher",
                            "system",
                            "prompt",
                        )
                    call_count = post_json.call_count
                    ledger = store.read_json("ledger.json")
                    response_artifacts = [
                        json.loads(path.read_text(encoding="utf-8"))
                        for path in sorted((store.directory / "responses").glob("*.json"))
                    ]
            return result, call_count, ledger, response_artifacts

        blocked_result, blocked_calls, blocked_ledger, blocked_artifacts = run_call(
            10.0,
            "paid-failure-blocks-fallback",
        )
        self.assertIsNone(blocked_result)
        self.assertEqual(blocked_calls, 1)
        self.assertEqual(blocked_ledger["calls"], 1)
        self.assertEqual(len(blocked_ledger["entries"]), 1)
        self.assertEqual(blocked_ledger["total_tokens"], 3000)
        self.assertEqual(blocked_ledger["known_cost_usd"], 12.34)
        self.assertIn("Known cost threshold", blocked_ledger["stop_reason"])
        self.assertEqual(blocked_ledger["entries"][0]["request_id"], "paid-primary-request")
        self.assertEqual(
            blocked_ledger["entries"][0]["route"]["semantic_failure"]["category"],
            "empty_response",
        )
        self.assertEqual(len(blocked_artifacts), 1)
        self.assertEqual(
            blocked_artifacts[0]["response"]["route"]["semantic_failure"]["category"],
            "empty_response",
        )

        allowed_result, allowed_calls, allowed_ledger, allowed_artifacts = run_call(
            20.0,
            "paid-failure-allows-fallback",
        )
        self.assertIsNotNone(allowed_result)
        self.assertEqual(allowed_result[0].text, "fallback answer")
        self.assertEqual(allowed_calls, 2)
        self.assertEqual(allowed_ledger["calls"], 2)
        self.assertEqual(len(allowed_ledger["entries"]), 2)
        self.assertEqual(allowed_ledger["total_tokens"], 3012)
        self.assertAlmostEqual(allowed_ledger["known_cost_usd"], 12.35)
        self.assertEqual(
            [entry["request_id"] for entry in allowed_ledger["entries"]],
            ["paid-primary-request", "fallback-request"],
        )
        self.assertEqual(len(allowed_artifacts), 2)
        semantic_failures = [
            artifact
            for artifact in allowed_artifacts
            if "semantic_failure" in artifact["response"]["route"]
        ]
        self.assertEqual(len(semantic_failures), 1)

    def test_incomplete_usage_is_ledgered_with_known_cost_and_blocks_later_dispatch(self) -> None:
        config = orchestration_config()
        profile = config["profiles"]["maximum_intelligence"]
        profile["budgets"].update(
            {"enforcement": "warn_only", "unknown_cost_policy": "warn"}
        )
        registry = ProviderRegistry(config)
        orchestrator = FusionOrchestrator(config, registry)
        budget = BudgetTracker(profile["budgets"])
        response_payload = {
            "id": "incomplete-usage-request",
            "status": "completed",
            "model": "incomplete-usage-live",
            "output_text": "answer",
            "usage": {"cost": 0.25},
        }

        def fake_post_json(
            _url,
            _payload,
            _provider,
            *,
            before_attempt=None,
            on_invalid_response=None,
        ):
            del on_invalid_response
            self.assertIsNotNone(before_attempt)
            before_attempt()
            return response_payload, {}, 0.1

        with RunStore("Incomplete usage fixture", config, "incomplete-usage-latch") as store:
            with mock.patch.object(
                registry,
                "_post_json",
                side_effect=fake_post_json,
            ) as post_json:
                with self.assertRaisesRegex(BudgetExceeded, "missing input token count"):
                    orchestrator._call(
                        budget,
                        store,
                        "synthesis",
                        "grok45_researcher",
                        "system",
                        "prompt",
                    )
                with self.assertRaisesRegex(BudgetExceeded, "missing input token count"):
                    budget.reserve_attempt("gate", "later-seat")
                ledger = store.read_json("ledger.json")

        self.assertEqual(post_json.call_count, 1)
        self.assertEqual(ledger["calls"], 1)
        self.assertEqual(ledger["known_cost_usd"], 0.25)
        self.assertEqual(ledger["unknown_cost_calls"], 0)
        self.assertEqual(len(ledger["entries"]), 1)
        self.assertIn("missing input token count", ledger["accounting_failure"])

    def test_malformed_resume_ledger_is_not_overwritten_and_releases_lease(self) -> None:
        config = orchestration_config()
        task = "Preserve malformed accounting evidence."
        artifact = "Caller supplied artifact."
        run_id = "malformed-ledger-preserved"
        selected_profile_name = str(config["active_profile"])
        composite_task = task + "\n\nARTIFACT-SHA256:" + text_hash(artifact)
        input_identity = {
            "operation": "adversarial_gate",
            "task": task,
            "artifact_sha256": text_hash(artifact),
            "mechanical_evidence": "",
            "profile_name": selected_profile_name,
        }
        with RunStore(
            composite_task,
            config,
            run_id,
            input_identity=input_identity,
        ) as store:
            ledger_path = store.path("ledger.json")
            ledger_path.write_text("{malformed-ledger\n", encoding="utf-8")
            original_ledger = ledger_path.read_bytes()

        with self.assertRaisesRegex(ConfigError, "Unreadable run artifact"):
            FusionOrchestrator(config, FakeProviderRegistry()).adversarial_gate(
                task,
                artifact,
                run_id=run_id,
            )

        self.assertEqual(ledger_path.read_bytes(), original_ledger)
        with RunStore(
            composite_task,
            config,
            run_id,
            input_identity=input_identity,
        ) as reopened_store:
            self.assertEqual(reopened_store.read_json("manifest.json")["status"], "failed")

    def test_semantically_invalid_resume_ledger_is_not_overwritten_or_dispatched(self) -> None:
        config = orchestration_config()
        task = "Preserve semantically invalid accounting evidence."
        artifact = "Caller supplied artifact."
        run_id = "invalid-json-ledger-preserved"
        selected_profile_name = str(config["active_profile"])
        composite_task = task + "\n\nARTIFACT-SHA256:" + text_hash(artifact)
        input_identity = {
            "operation": "adversarial_gate",
            "task": task,
            "artifact_sha256": text_hash(artifact),
            "mechanical_evidence": "",
            "profile_name": selected_profile_name,
        }
        with RunStore(
            composite_task,
            config,
            run_id,
            input_identity=input_identity,
        ) as store:
            poisoned_ledger = BudgetTracker(
                config["profiles"][selected_profile_name]["budgets"]
            ).snapshot()
            poisoned_ledger["known_cost_usd"] = -500.0
            poisoned_ledger["provider_cost_usd"] = {"poisoned-provider": -500.0}
            store.write_json("ledger.json", poisoned_ledger)
            ledger_path = store.path("ledger.json")
            original_ledger = ledger_path.read_bytes()

        registry = FakeProviderRegistry()
        with self.assertRaisesRegex(ConfigError, "known_cost_usd"):
            FusionOrchestrator(config, registry).adversarial_gate(
                task,
                artifact,
                run_id=run_id,
            )

        self.assertEqual(registry.calls, [])
        self.assertEqual(ledger_path.read_bytes(), original_ledger)
        with RunStore(
            composite_task,
            config,
            run_id,
            input_identity=input_identity,
        ) as reopened_store:
            self.assertEqual(reopened_store.read_json("manifest.json")["status"], "failed")

    def test_full_client_fusion_is_independent_structured_gated_ledgered_and_resumable(self) -> None:
        config = orchestration_config()
        config["profiles"]["alternate"] = copy.deepcopy(config["profiles"]["maximum_intelligence"])
        registry = FakeProviderRegistry()
        orchestrator = FusionOrchestrator(config, registry)
        task = "Create a safe implementation plan and prove every acceptance criterion."

        result = orchestrator.fuse(
            task,
            context="The tests must be deterministic and offline.",
            mechanical_evidence="python -m unittest exits zero",
            run_id="full-fusion-resume",
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.synthesis, FakeProviderRegistry.SYNTHESIS_TEXT)
        self.assertTrue(result.gate["passed"])
        self.assertEqual(result.gate["pass_count"], 2)
        self.assertEqual(result.gate["required_passes"], 2)
        synthesis_hash = text_hash(result.synthesis)
        self.assertEqual(result.gate["artifact_sha256"], synthesis_hash)
        for review in result.gate["reviewers"]:
            self.assertEqual(review["status"], "completed")
            self.assertEqual(review["verdict"]["verdict"], "PASS")
            self.assertEqual(review["verdict"]["artifact_sha256"], synthesis_hash)

        calls = registry.calls
        panel_calls = [call for call in calls if call["seat_name"] in DEFAULT_PANEL and not call["has_schema"]]
        self.assertEqual(len(panel_calls), 3)
        self.assertEqual({call["seat_name"] for call in panel_calls}, set(DEFAULT_PANEL))
        self.assertEqual(len({call["prompt"] for call in panel_calls}), 3)
        self.assertEqual(
            {
                next(
                    bundle
                    for bundle in (
                        "full_task_and_evidence",
                        "requirements_risks_and_counterexamples",
                        "requirements_and_mechanical_evidence",
                    )
                    if bundle in call["prompt"]
                )
                for call in panel_calls
            },
            {
                "full_task_and_evidence",
                "requirements_risks_and_counterexamples",
                "requirements_and_mechanical_evidence",
            },
        )
        for panel_call in panel_calls:
            for panel_output in FakeProviderRegistry.PANEL_TEXTS.values():
                self.assertNotIn(panel_output, panel_call["prompt"])

        judge_calls = [call for call in calls if call["schema_name"] == "fusion_judgment"]
        self.assertEqual(len(judge_calls), 1)
        self.assertTrue(judge_calls[0]["has_schema"])
        for panel_output in FakeProviderRegistry.PANEL_TEXTS.values():
            self.assertIn(panel_output, judge_calls[0]["prompt"])
        for seat_name in DEFAULT_PANEL:
            self.assertNotIn(seat_name, judge_calls[0]["prompt"])
        self.assertIn("Seat A", judge_calls[0]["prompt"])

        synthesis_calls = [call for call in calls if call["seat_name"] == "grok45_synthesizer"]
        self.assertEqual(len(synthesis_calls), 1)
        self.assertFalse(synthesis_calls[0]["has_schema"])
        self.assertIn("Use deterministic verification.", synthesis_calls[0]["prompt"])
        for panel_output in FakeProviderRegistry.PANEL_TEXTS.values():
            self.assertIn(panel_output, synthesis_calls[0]["prompt"])

        gate_calls = [call for call in calls if call["schema_name"] == "adversarial_verdict"]
        self.assertEqual(len(gate_calls), 2)
        self.assertEqual({call["seat_name"] for call in gate_calls}, {"grok45_verifier", "grok43_constraint_auditor"})
        for gate_call in gate_calls:
            self.assertTrue(gate_call["has_schema"])
            self.assertIn(synthesis_hash, gate_call["prompt"])
            self.assertIn(result.synthesis, gate_call["prompt"])

        self.assertEqual(result.ledger["calls"], 7)
        self.assertEqual(len(result.ledger["entries"]), 7)
        self.assertEqual(
            Counter(entry["stage"] for entry in result.ledger["entries"]),
            Counter({"panel": 3, "judge": 1, "synthesis": 1, "gate": 2}),
        )
        self.assertAlmostEqual(result.ledger["known_cost_usd"], 0.007)
        self.assertFalse(result.execution_handoff["ready"])
        self.assertTrue(result.execution_handoff["ready_for_host_workflow"])
        self.assertEqual(
            result.execution_handoff["lifecycle"]["pending_gates"],
            ["plan", "pre_execution"],
        )
        self.assertIn("fused_plan", result.execution_handoff["artifacts"])
        self.assertIn("minority_findings", result.execution_handoff["artifacts"])
        self.assertIn("budget_remaining", result.execution_handoff["artifacts"])

        artifact_directory = Path(result.artifacts_dir)
        expected_artifacts = {
            "manifest.json",
            "panel.json",
            "judge.json",
            "synthesis.json",
            "gate-0.json",
            "ledger.json",
            "execution-handoff.json",
            "result.json",
        }
        self.assertTrue(expected_artifacts.issubset({path.name for path in artifact_directory.iterdir()}))
        synthesis_artifact = json.loads(
            (artifact_directory / "synthesis.json").read_text(encoding="utf-8")
        )
        self.assertEqual(synthesis_artifact["mode"], "client_orchestrated")
        self.assertEqual(synthesis_artifact["author_seat"], "grok45_synthesizer")
        self.assertEqual(synthesis_artifact["sha256"], synthesis_hash)
        manifest = json.loads((artifact_directory / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "completed")
        self.assertRegex(manifest["input_hash"], r"^[0-9a-f]{64}$")

        call_count_before_resume = len(registry.calls)
        resumed = FusionOrchestrator(config, registry).fuse(
            task,
            context="The tests must be deterministic and offline.",
            mechanical_evidence="python -m unittest exits zero",
            run_id="full-fusion-resume",
        )
        self.assertEqual(len(registry.calls), call_count_before_resume)
        self.assertEqual(resumed.synthesis, result.synthesis)
        self.assertEqual(resumed.gate, result.gate)
        self.assertEqual(resumed.ledger["calls"], result.ledger["calls"])
        self.assertEqual(resumed.ledger["entries"], result.ledger["entries"])

        for changed_arguments in (
            {"context": "changed context", "mechanical_evidence": "python -m unittest exits zero"},
            {"context": "The tests must be deterministic and offline.", "mechanical_evidence": "tests now fail"},
            {
                "context": "The tests must be deterministic and offline.",
                "mechanical_evidence": "python -m unittest exits zero",
                "profile_name": "alternate",
            },
        ):
            with self.subTest(changed_arguments=changed_arguments):
                with self.assertRaisesRegex(ConfigError, "task/config/input hash"):
                    FusionOrchestrator(config, registry).fuse(
                        task,
                        run_id="full-fusion-resume",
                        **changed_arguments,
                    )
        self.assertEqual(len(registry.calls), call_count_before_resume)

    def test_standalone_gate_resume_binds_artifact_and_mechanical_evidence(self) -> None:
        config = orchestration_config()
        registry = FakeProviderRegistry()
        orchestrator = FusionOrchestrator(config, registry)

        first = orchestrator.adversarial_gate(
            "Review the release artifact.",
            "exact artifact",
            mechanical_evidence="tests pass",
            run_id="standalone-gate-resume",
        )
        self.assertTrue(first["gate"]["passed"])
        self.assertEqual(first["ledger"]["calls"], 2)
        call_count = len(registry.calls)

        resumed = orchestrator.adversarial_gate(
            "Review the release artifact.",
            "exact artifact",
            mechanical_evidence="tests pass",
            run_id="standalone-gate-resume",
        )
        self.assertEqual(len(registry.calls), call_count)
        self.assertEqual(resumed["ledger"]["calls"], first["ledger"]["calls"])
        self.assertEqual(resumed["ledger"]["entries"], first["ledger"]["entries"])
        self.assertEqual(resumed["ledger"]["known_cost_usd"], first["ledger"]["known_cost_usd"])
        self.assertGreaterEqual(resumed["ledger"]["wall_seconds"], first["ledger"]["wall_seconds"])

        with self.assertRaisesRegex(ConfigError, "task/config/input hash"):
            orchestrator.adversarial_gate(
                "Review the release artifact.",
                "exact artifact",
                mechanical_evidence="tests fail",
                run_id="standalone-gate-resume",
            )
        self.assertEqual(len(registry.calls), call_count)

    def test_cached_gate_requires_the_exact_configured_reviewer_roster(self) -> None:
        config = orchestration_config()
        registry = FakeProviderRegistry()
        orchestrator = FusionOrchestrator(config, registry)
        task = "Reject a cached gate with missing reviewer evidence."
        artifact = "Candidate artifact"

        first = orchestrator.adversarial_gate(
            task,
            artifact,
            run_id="cached-gate-roster",
        )
        gate_path = Path(first["artifacts_dir"]) / "gate-0.json"
        cached_gate = json.loads(gate_path.read_text(encoding="utf-8"))
        cached_gate["passed"] = True
        cached_gate["pass_count"] = 0
        cached_gate["reviewers"] = []
        gate_path.write_text(json.dumps(cached_gate), encoding="utf-8")
        call_count = len(registry.calls)

        with self.assertRaisesRegex(
            ConfigError,
            "reviewer roster must match the configured reviewer seats exactly",
        ):
            orchestrator.adversarial_gate(
                task,
                artifact,
                run_id="cached-gate-roster",
            )

        self.assertEqual(len(registry.calls), call_count)

    def test_cached_completed_gate_review_requires_persisted_raw_response(self) -> None:
        config = orchestration_config()
        registry = FakeProviderRegistry()
        orchestrator = FusionOrchestrator(config, registry)
        task = "Reject a cached completed review without raw evidence."
        artifact = "Candidate artifact"

        first = orchestrator.adversarial_gate(
            task,
            artifact,
            run_id="cached-gate-missing-response",
        )
        gate_path = Path(first["artifacts_dir"]) / "gate-0.json"
        cached_gate = json.loads(gate_path.read_text(encoding="utf-8"))
        cached_gate["reviewers"][0].pop("response")
        gate_path.write_text(json.dumps(cached_gate), encoding="utf-8")
        call_count = len(registry.calls)

        with self.assertRaisesRegex(ConfigError, "missing its raw response"):
            orchestrator.adversarial_gate(
                task,
                artifact,
                run_id="cached-gate-missing-response",
            )

        self.assertEqual(len(registry.calls), call_count)

    def test_cached_completed_gate_review_rejects_raw_response_verdict_contradiction(self) -> None:
        config = orchestration_config()
        registry = FakeProviderRegistry()
        orchestrator = FusionOrchestrator(config, registry)
        task = "Reject contradictory cached reviewer evidence."
        artifact = "Candidate artifact"

        first = orchestrator.adversarial_gate(
            task,
            artifact,
            run_id="cached-gate-contradictory-response",
        )
        gate_path = Path(first["artifacts_dir"]) / "gate-0.json"
        cached_gate = json.loads(gate_path.read_text(encoding="utf-8"))
        cached_gate["reviewers"][0]["verdict"]["verdict"] = "NEEDS_WORK"
        gate_path.write_text(json.dumps(cached_gate), encoding="utf-8")
        call_count = len(registry.calls)

        with self.assertRaisesRegex(ConfigError, "does not match its raw response"):
            orchestrator.adversarial_gate(
                task,
                artifact,
                run_id="cached-gate-contradictory-response",
            )

        self.assertEqual(len(registry.calls), call_count)

    def test_cached_completed_panel_row_requires_bound_raw_response_metadata(self) -> None:
        for case_name in ("incomplete-response", "changed-model"):
            with self.subTest(case_name=case_name):
                config = orchestration_config()
                registry = FakeProviderRegistry(fail_seats={"grok43_judge"})
                orchestrator = FusionOrchestrator(config, registry)
                run_id = f"cached-panel-{case_name}"

                with self.assertRaisesRegex(ProviderError, "synthetic provider failure"):
                    orchestrator.fuse(
                        "Reject malformed cached panel evidence.",
                        run_id=run_id,
                    )

                panel_path = Path(self.temporary_directory.name) / "runs" / run_id / "panel.json"
                cached_panel = json.loads(panel_path.read_text(encoding="utf-8"))
                cached_response = cached_panel["results"][0]["response"]
                if case_name == "incomplete-response":
                    cached_panel["results"][0]["response"] = {
                        "text": cached_response["text"]
                    }
                    expected_error = "response schema mismatch"
                else:
                    cached_response["actual_model"] = "forged/model"
                    expected_error = "response hash does not match"
                panel_path.write_text(json.dumps(cached_panel), encoding="utf-8")
                call_count = len(registry.calls)
                registry.fail_seats.clear()

                with self.assertRaisesRegex(ConfigError, expected_error):
                    orchestrator.fuse(
                        "Reject malformed cached panel evidence.",
                        run_id=run_id,
                    )

                self.assertEqual(len(registry.calls), call_count)

    def test_cached_judge_judgment_must_match_its_raw_response(self) -> None:
        config = orchestration_config()
        registry = FakeProviderRegistry(fail_seats={"grok45_synthesizer"})
        orchestrator = FusionOrchestrator(config, registry)
        task = "Reject contradictory cached judge evidence."
        run_id = "cached-judge-contradiction"

        with self.assertRaisesRegex(ProviderError, "synthetic provider failure"):
            orchestrator.fuse(task, run_id=run_id)

        judge_path = Path(self.temporary_directory.name) / "runs" / run_id / "judge.json"
        cached_judge = json.loads(judge_path.read_text(encoding="utf-8"))
        cached_judge["judgment"]["final_guidance"] = ["Forged cached guidance."]
        judge_path.write_text(json.dumps(cached_judge), encoding="utf-8")
        call_count = len(registry.calls)
        registry.fail_seats.clear()

        with self.assertRaisesRegex(ConfigError, "does not match its raw response"):
            orchestrator.fuse(task, run_id=run_id)

        self.assertEqual(len(registry.calls), call_count)

    def test_cached_synthesis_requires_persisted_author_ledger_evidence(self) -> None:
        config = orchestration_config()
        task = "Reject a synthesis without evidence of its claimed author call."
        run_id = "cached-synthesis-missing-author-call"
        forged_text = "Forged synthesis with self-asserted author provenance."
        forged_response = {
            "text": forged_text,
            "provider": "fake_provider",
            "requested_model": "requested/grok45_synthesizer",
            "actual_model": "actual/grok45_synthesizer",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "reasoning_tokens": 2,
                "cached_tokens": 1,
                "tool_calls": 0,
                "cost_usd": 0.001,
                "unknown_cost_fail_closed": False,
                "input_output_usage_complete": True,
                "raw_usage_invalid": False,
                "accounting_error": None,
            },
            "latency_seconds": 0.01,
            "request_id": "forged-synthesis-request",
            "route": {"fixture": "forged"},
            "raw_status": "completed",
        }
        selected_profile_name = str(config["active_profile"])
        with RunStore(
            task,
            config,
            run_id,
            input_identity={
                "operation": "fuse",
                "task": task,
                "context": "",
                "mechanical_evidence": "",
                "profile_name": selected_profile_name,
            },
        ) as store:
            store.write_json(
                "synthesis.json",
                {
                    "mode": "client_orchestrated",
                    "author_seat": "grok45_synthesizer",
                    "text": forged_text,
                    "sha256": text_hash(forged_text),
                    "response": forged_response,
                },
            )

        registry = FakeProviderRegistry()
        with self.assertRaisesRegex(ConfigError, "response_evidence"):
            FusionOrchestrator(config, registry).fuse(task, run_id=run_id)

        self.assertNotIn(
            "grok45_synthesizer",
            [call["seat_name"] for call in registry.calls],
        )

    def test_cached_synthesis_text_must_match_its_raw_response(self) -> None:
        config = orchestration_config()
        registry = FakeProviderRegistry()
        orchestrator = FusionOrchestrator(config, registry)
        task = "Reject cached synthesis text that contradicts its raw response."
        run_id = "cached-synthesis-text-contradiction"

        first = orchestrator.fuse(task, run_id=run_id)
        synthesis_path = Path(first.artifacts_dir) / "synthesis.json"
        cached_synthesis = json.loads(synthesis_path.read_text(encoding="utf-8"))
        cached_synthesis["text"] = "Tampered synthesis text."
        cached_synthesis["sha256"] = text_hash(cached_synthesis["text"])
        synthesis_path.write_text(json.dumps(cached_synthesis), encoding="utf-8")
        call_count = len(registry.calls)

        with self.assertRaisesRegex(ConfigError, "text does not match its raw response"):
            orchestrator.fuse(task, run_id=run_id)

        self.assertEqual(len(registry.calls), call_count)

    def test_recomputed_raw_artifact_cannot_forge_a_cached_response_without_ledger_receipt(self) -> None:
        config = orchestration_config()
        registry = FakeProviderRegistry()
        orchestrator = FusionOrchestrator(config, registry)
        task = "Bind the complete synthesis text to its exact accounted call."
        run_id = "forged-synthesis-receipt"

        first = orchestrator.fuse(task, run_id=run_id)
        run_directory = Path(first.artifacts_dir)
        synthesis_path = run_directory / "synthesis.json"
        cached_synthesis = json.loads(synthesis_path.read_text(encoding="utf-8"))
        original_evidence = cached_synthesis["response_evidence"]
        original_raw_path = (
            run_directory / "responses" / f"{original_evidence['entry_id']}.json"
        )
        original_raw = json.loads(original_raw_path.read_text(encoding="utf-8"))

        forged_text = "A forged synthesis must not inherit the original paid call receipt."
        forged_response = copy.deepcopy(cached_synthesis["response"])
        forged_response["text"] = forged_text
        response_sha256 = canonical_json_hash(forged_response)
        forged_entry_id = call_receipt_entry_id(
            original_evidence["attempt_id"],
            original_evidence["invocation_sha256"],
            response_sha256,
        )
        forged_evidence = {
            **original_evidence,
            "entry_id": forged_entry_id,
            "response_sha256": response_sha256,
        }
        cached_synthesis.update(
            {
                "text": forged_text,
                "sha256": text_hash(forged_text),
                "response": forged_response,
                "response_evidence": forged_evidence,
            }
        )
        synthesis_path.write_text(json.dumps(cached_synthesis), encoding="utf-8")
        forged_raw = {
            **original_raw,
            "receipt": forged_evidence,
            "response": forged_response,
        }
        (run_directory / "responses" / f"{forged_entry_id}.json").write_text(
            json.dumps(forged_raw),
            encoding="utf-8",
        )
        call_count = len(registry.calls)

        with self.assertRaisesRegex(ConfigError, "no matching persisted ledger entry"):
            orchestrator.fuse(task, run_id=run_id)

        self.assertEqual(len(registry.calls), call_count)

    def test_committed_response_without_cache_fails_before_redispatch(self) -> None:
        cases = (
            ("panel", "panel.json", "omits paid response evidence"),
            ("judge", "judge.json", "Persisted judge response evidence exists"),
            ("synthesis", "synthesis.json", "Persisted synthesis response evidence exists"),
            ("gate", "gate-0.json", "Persisted gate response evidence exists"),
        )
        for case_name, artifact_name, expected_error in cases:
            with self.subTest(case_name=case_name):
                config = orchestration_config()
                registry = FakeProviderRegistry()
                orchestrator = FusionOrchestrator(config, registry)
                task = f"Do not redispatch committed {case_name} responses."
                run_id = f"missing-{case_name}-cache"

                first = orchestrator.fuse(task, run_id=run_id)
                artifact_path = Path(first.artifacts_dir) / artifact_name
                if case_name == "panel":
                    cached_panel = json.loads(artifact_path.read_text(encoding="utf-8"))
                    cached_panel["results"].pop()
                    artifact_path.write_text(json.dumps(cached_panel), encoding="utf-8")
                else:
                    artifact_path.unlink()
                call_count = len(registry.calls)

                with self.assertRaisesRegex(ConfigError, expected_error):
                    orchestrator.fuse(task, run_id=run_id)

                self.assertEqual(len(registry.calls), call_count)

    def test_cached_gate_recomputes_policy_and_blocks_truthy_negative_verdict(self) -> None:
        config = orchestration_config()
        registry = VerdictOverrideRegistry(
            {
                "grok45_verifier": {
                    "verdict": "NEEDS_WORK",
                    "summary": "A cached blocking defect remains.",
                    "blocking_findings": ["The candidate omits required evidence."],
                    "required_actions": ["Add and verify the missing evidence."],
                    "evidence": ["Deterministic cached-gate fixture."],
                }
            }
        )
        orchestrator = FusionOrchestrator(config, registry)
        task = "Recompute cached gate authorization from reviewer evidence."

        first = orchestrator.fuse(task, run_id="cached-gate-recomputed")
        gate_path = Path(first.artifacts_dir) / "gate-0.json"
        cached_gate = json.loads(gate_path.read_text(encoding="utf-8"))
        cached_gate["passed"] = "yes"
        cached_gate["pass_count"] = 2
        cached_gate["required_passes"] = 0
        cached_gate["negative_verdict_blocked"] = False
        gate_path.write_text(json.dumps(cached_gate), encoding="utf-8")
        call_count = len(registry.calls)

        resumed = orchestrator.fuse(task, run_id="cached-gate-recomputed")

        self.assertEqual(len(registry.calls), call_count)
        self.assertEqual(resumed.status, "rejected")
        self.assertFalse(resumed.gate["passed"])
        self.assertEqual(resumed.gate["pass_count"], 1)
        self.assertEqual(resumed.gate["required_passes"], 2)
        self.assertTrue(resumed.gate["negative_verdict_blocked"])
        self.assertFalse(resumed.execution_handoff["ready_for_host_workflow"])
        self.assertFalse(resumed.execution_handoff["ready"])
        self.assertFalse(resumed.execution_handoff["mutation_authorized"])

    def test_panel_collapse_fails_closed_and_marks_manifest_failed(self) -> None:
        panel = ["grok45_researcher", "grok45_adversary"]
        config = orchestration_config(panel=panel, min_live_seats=2, allow_degradation=True)
        registry = FakeProviderRegistry(fail_seats={"grok45_adversary"})

        with self.assertRaisesRegex(ProviderError, r"Panel collapsed: 1/2 live"):
            FusionOrchestrator(config, registry).fuse("Collapse fixture", run_id="panel-collapse")

        manifest_path = Path(self.temporary_directory.name) / "runs" / "panel-collapse" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "failed")

    def test_strict_panel_resume_retries_only_failed_seat_and_preserves_paid_success(self) -> None:
        panel = ["grok45_researcher", "grok45_adversary"]
        config = orchestration_config(panel=panel, min_live_seats=2, allow_degradation=False)
        registry = FakeProviderRegistry(fail_seats={"grok45_adversary"})
        orchestrator = FusionOrchestrator(config, registry)

        with self.assertRaisesRegex(ProviderError, r"Panel collapsed: 1/2 live"):
            orchestrator.fuse("Retry only the failed seat.", run_id="strict-panel-resume")

        panel_path = Path(self.temporary_directory.name) / "runs" / "strict-panel-resume" / "panel.json"
        first_panel = json.loads(panel_path.read_text(encoding="utf-8"))
        self.assertEqual(first_panel["live_count"], 1)
        self.assertEqual(first_panel["failed_count"], 1)
        self.assertEqual(len(first_panel["attempts"]), 2)
        first_ledger = json.loads((panel_path.parent / "ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(first_ledger["calls"], 2)

        registry.fail_seats.clear()
        result = orchestrator.fuse("Retry only the failed seat.", run_id="strict-panel-resume")

        call_counts = Counter(call["seat_name"] for call in registry.calls)
        self.assertEqual(call_counts["grok45_researcher"], 1)
        self.assertEqual(call_counts["grok45_adversary"], 2)
        self.assertEqual(result.status, "completed")
        resumed_panel = json.loads(panel_path.read_text(encoding="utf-8"))
        self.assertEqual(resumed_panel["live_count"], 2)
        self.assertEqual(len(resumed_panel["attempts"]), 3)

    def test_allowed_panel_degradation_records_failure_and_completes(self) -> None:
        config = orchestration_config(min_live_seats=2, allow_degradation=True)
        registry = FakeProviderRegistry(fail_seats={"grok45_adversary"})

        result = FusionOrchestrator(config, registry).fuse("Degradation fixture", run_id="panel-degraded")

        self.assertEqual(result.status, "completed")
        panel_path = Path(result.artifacts_dir) / "panel.json"
        panel_artifact = json.loads(panel_path.read_text(encoding="utf-8"))
        self.assertTrue(panel_artifact["degraded"])
        self.assertEqual(panel_artifact["live_count"], 2)
        self.assertEqual(panel_artifact["failed_count"], 1)
        failed = [row for row in panel_artifact["results"] if row["status"] == "failed"]
        self.assertEqual([row["seat_name"] for row in failed], ["grok45_adversary"])
        self.assertIn("synthetic provider failure", failed[0]["error"])
        self.assertEqual(result.ledger["calls"], 7)
        self.assertEqual(len(result.ledger["entries"]), 6)

    def test_native_openrouter_provider_error_falls_back_to_client_orchestration(self) -> None:
        config = orchestration_config()
        fusion = config["profiles"]["maximum_intelligence"]["fusion"]
        fusion["engine"] = "openrouter_native"
        fusion["native_fusion_seat"] = "openrouter_native_fusion_seat"
        fusion["native_openrouter_fusion"]["enabled"] = True
        fusion["native_openrouter_fusion"]["fallback_to_client_orchestrated"] = True
        registry = FakeProviderRegistry(fail_seats={"openrouter_native_fusion_seat"})

        result = FusionOrchestrator(config, registry).fuse(
            "Native fallback fixture",
            run_id="native-openrouter-fallback",
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.synthesis, FakeProviderRegistry.SYNTHESIS_TEXT)
        self.assertEqual(registry.calls[0]["seat_name"], "openrouter_native_fusion_seat")
        self.assertEqual(result.ledger["calls"], 8)
        self.assertEqual(len(result.ledger["entries"]), 7)
        failure_artifact_path = Path(result.artifacts_dir) / "native-openrouter-failure.json"
        failure_artifact = json.loads(failure_artifact_path.read_text(encoding="utf-8"))
        self.assertEqual(failure_artifact["status"], "failed")
        self.assertEqual(failure_artifact["fallback"], "client_orchestrated")
        self.assertIn("synthetic provider failure", failure_artifact["error"])
        synthesis_artifact = json.loads(
            (Path(result.artifacts_dir) / "synthesis.json").read_text(encoding="utf-8")
        )
        self.assertEqual(synthesis_artifact["mode"], "client_orchestrated")
        self.assertEqual(synthesis_artifact["author_seat"], "grok45_synthesizer")
        self.assertEqual(
            {row["seat_name"] for row in result.panel if row["status"] == "completed"},
            set(DEFAULT_PANEL),
        )
        call_count = len(registry.calls)
        resumed = FusionOrchestrator(config, registry).fuse(
            "Native fallback fixture",
            run_id="native-openrouter-fallback",
        )
        self.assertEqual(len(registry.calls), call_count)
        self.assertEqual(resumed.synthesis, result.synthesis)

    def test_disabled_rescue_blocks_native_fusion_client_fallback(self) -> None:
        config = orchestration_config()
        profile = config["profiles"]["maximum_intelligence"]
        fusion = profile["fusion"]
        fusion["engine"] = "openrouter_native"
        fusion["native_fusion_seat"] = "openrouter_native_fusion_seat"
        fusion["native_openrouter_fusion"]["enabled"] = True
        fusion["native_openrouter_fusion"]["fallback_to_client_orchestrated"] = True
        profile["rescue"]["enabled"] = False
        registry = FakeProviderRegistry(fail_seats={"openrouter_native_fusion_seat"})

        with self.assertRaisesRegex(ProviderError, "synthetic provider failure"):
            FusionOrchestrator(config, registry).fuse(
                "Disabled rescue must not fall back.",
                run_id="native-openrouter-rescue-disabled",
            )

        self.assertEqual(
            [call["seat_name"] for call in registry.calls],
            ["openrouter_native_fusion_seat"],
        )
        run_directory = Path(self.temporary_directory.name) / "runs" / "native-openrouter-rescue-disabled"
        self.assertFalse((run_directory / "native-openrouter-failure.json").exists())

    def test_native_openrouter_success_resume_reuses_paid_synthesis(self) -> None:
        config = orchestration_config()
        fusion = config["profiles"]["maximum_intelligence"]["fusion"]
        fusion["engine"] = "openrouter_native"
        fusion["native_fusion_seat"] = "openrouter_native_fusion_seat"
        fusion["native_openrouter_fusion"]["enabled"] = True
        fusion["native_openrouter_fusion"]["fallback_to_client_orchestrated"] = False
        registry = FakeProviderRegistry()

        first = FusionOrchestrator(config, registry).fuse(
            "Native success fixture",
            run_id="native-openrouter-success",
        )
        self.assertEqual(first.status, "completed")
        self.assertEqual(registry.calls[0]["seat_name"], "openrouter_native_fusion_seat")
        self.assertEqual(first.ledger["calls"], 3)
        synthesis_artifact = json.loads(
            (Path(first.artifacts_dir) / "synthesis.json").read_text(encoding="utf-8")
        )
        self.assertEqual(synthesis_artifact["mode"], "native_openrouter")
        self.assertEqual(synthesis_artifact["author_seat"], "openrouter_native_fusion_seat")
        call_count = len(registry.calls)

        resumed = FusionOrchestrator(config, registry).fuse(
            "Native success fixture",
            run_id="native-openrouter-success",
        )
        self.assertEqual(len(registry.calls), call_count)
        self.assertEqual(resumed.synthesis, first.synthesis)
        self.assertEqual(resumed.ledger["calls"], first.ledger["calls"])

    def test_native_openrouter_fusion_seat_cannot_review_its_own_artifact(self) -> None:
        config = orchestration_config()
        profile = config["profiles"]["maximum_intelligence"]
        fusion = profile["fusion"]
        fusion["engine"] = "openrouter_native"
        fusion["native_fusion_seat"] = "openrouter_native_fusion_seat"
        fusion["native_openrouter_fusion"]["enabled"] = True
        fusion["native_openrouter_fusion"]["fallback_to_client_orchestrated"] = False
        profile["gates"]["reviewers"] = ["openrouter_native_fusion_seat"]
        profile["gates"]["required_passes"] = 1
        registry = FakeProviderRegistry()

        with self.assertRaisesRegex(
            ConfigError,
            "actual artifact author 'openrouter_native_fusion_seat'",
        ):
            FusionOrchestrator(config, registry).fuse(
                "Native Fusion must not review its own synthesis.",
                run_id="native-openrouter-self-review",
            )

        self.assertEqual(
            [call["seat_name"] for call in registry.calls],
            ["openrouter_native_fusion_seat"],
        )

    def test_native_fallback_resume_rejects_cached_native_synthesis_provenance(self) -> None:
        config = orchestration_config()
        profile = config["profiles"]["maximum_intelligence"]
        fusion = profile["fusion"]
        fusion["engine"] = "openrouter_native"
        fusion["native_fusion_seat"] = "openrouter_native_fusion_seat"
        fusion["native_openrouter_fusion"]["enabled"] = True
        fusion["native_openrouter_fusion"]["fallback_to_client_orchestrated"] = True
        profile["gates"]["reviewers"] = ["openrouter_native_fusion_seat"]
        profile["gates"]["required_passes"] = 1
        registry = FakeProviderRegistry()
        orchestrator = FusionOrchestrator(config, registry)
        task = "A fallback marker must not change cached native authorship."
        run_id = "native-fallback-provenance"

        with self.assertRaisesRegex(
            ConfigError,
            "actual artifact author 'openrouter_native_fusion_seat'",
        ):
            orchestrator.fuse(task, run_id=run_id)

        run_directory = Path(self.temporary_directory.name) / "runs" / run_id
        synthesis_artifact = json.loads(
            (run_directory / "synthesis.json").read_text(encoding="utf-8")
        )
        self.assertEqual(synthesis_artifact["mode"], "native_openrouter")
        self.assertEqual(synthesis_artifact["author_seat"], "openrouter_native_fusion_seat")
        (run_directory / "native-openrouter-failure.json").write_text(
            json.dumps(
                {
                    "status": "failed",
                    "error": "malformed fallback marker",
                    "fallback": "client_orchestrated",
                }
            ),
            encoding="utf-8",
        )
        call_count = len(registry.calls)

        with self.assertRaisesRegex(
            ConfigError,
            "fallback marker schema mismatch",
        ):
            orchestrator.fuse(task, run_id=run_id)

        self.assertEqual(len(registry.calls), call_count)

    def test_unproven_native_fallback_marker_is_rejected_before_dispatch(self) -> None:
        config = orchestration_config()
        profile = config["profiles"]["maximum_intelligence"]
        fusion = profile["fusion"]
        fusion["engine"] = "openrouter_native"
        fusion["native_fusion_seat"] = "openrouter_native_fusion_seat"
        fusion["native_openrouter_fusion"]["enabled"] = True
        fusion["native_openrouter_fusion"]["fallback_to_client_orchestrated"] = True
        task = "Reject an empty native fallback marker."
        run_id = "malformed-native-fallback-marker"
        selected_profile_name = str(config["active_profile"])

        with RunStore(
            task,
            config,
            run_id,
            input_identity={
                "operation": "fuse",
                "task": task,
                "context": "",
                "mechanical_evidence": "",
                "profile_name": selected_profile_name,
            },
        ) as store:
            store.write_json(
                "native-openrouter-failure.json",
                {
                    "schema_version": 1,
                    "status": "failed",
                    "error": "fabricated transport failure",
                    "fallback": "client_orchestrated",
                    "failure_phase": "transport",
                    "invocation_sha256": "0" * 64,
                    "attempt_ids": ["1" * 64],
                },
            )

        registry = FakeProviderRegistry()
        with self.assertRaisesRegex(ConfigError, "bound to a different invocation"):
            FusionOrchestrator(config, registry).fuse(task, run_id=run_id)

        self.assertEqual(registry.calls, [])

    def test_standalone_gate_does_not_attribute_external_artifact_to_native_fusion_seat(self) -> None:
        config = orchestration_config()
        profile = config["profiles"]["maximum_intelligence"]
        fusion = profile["fusion"]
        fusion["engine"] = "openrouter_native"
        fusion["native_fusion_seat"] = "openrouter_native_fusion_seat"
        fusion["native_openrouter_fusion"]["enabled"] = True
        profile["gates"]["reviewers"] = ["openrouter_native_fusion_seat"]
        profile["gates"]["required_passes"] = 1
        registry = FakeProviderRegistry()

        result = FusionOrchestrator(config, registry).adversarial_gate(
            "Review a caller-supplied artifact.",
            "This artifact was supplied externally and was not generated by a configured seat.",
            run_id="standalone-external-author",
        )

        self.assertTrue(result["gate"]["passed"])
        self.assertEqual(
            [call["seat_name"] for call in registry.calls],
            ["openrouter_native_fusion_seat"],
        )

    def test_mechanical_failure_and_reported_blind_spot_override_pass_votes(self) -> None:
        config = orchestration_config()
        mechanical_registry = FakeProviderRegistry()
        mechanical = FusionOrchestrator(config, mechanical_registry).adversarial_gate(
            "Release only when deterministic checks pass.",
            "Candidate artifact",
            mechanical_evidence="pytest: exit status 1; assertion failure",
            run_id="mechanical-failure-blocks",
        )
        self.assertFalse(mechanical["gate"]["passed"])
        self.assertTrue(mechanical["gate"]["mechanical_blocked"])
        self.assertEqual(mechanical["gate"]["pass_count"], 2)

        blind_spot_registry = FakeProviderRegistry(verdict_blind_spots={"Live deployment behavior was not checked."})
        blind_spot = FusionOrchestrator(config, blind_spot_registry).adversarial_gate(
            "Release only after targeted review.",
            "Candidate artifact",
            mechanical_evidence="23 passed, 0 failed; exit status 0",
            run_id="blind-spot-blocks",
        )
        self.assertFalse(blind_spot["gate"]["passed"])
        self.assertTrue(blind_spot["gate"]["blind_spot_blocked"])
        self.assertFalse(blind_spot["gate"]["mechanical_blocked"])

    def test_mechanical_evidence_parses_exit_codes_without_zero_failure_false_positives(self) -> None:
        passing_evidence = (
            "0 tests failed",
            "23 passed, 0 failed; exit status 0",
            "pytest exited with code 0",
            '{"passed": true, "exit_code": 0, "tests_failed": 0, "failures": []}',
        )
        failing_evidence = (
            "pytest exited with code 1",
            "command returned exit code 2",
            "1 test failed",
            '{"passed": "false", "exit_code": "1"}',
        )

        for index, evidence in enumerate(passing_evidence):
            with self.subTest(expected="pass", evidence=evidence):
                result = FusionOrchestrator(
                    orchestration_config(),
                    FakeProviderRegistry(),
                ).adversarial_gate(
                    "Release only when explicit deterministic checks pass.",
                    "Candidate artifact",
                    mechanical_evidence=evidence,
                    run_id=f"mechanical-pass-{index}",
                )
                self.assertTrue(result["gate"]["passed"])
                self.assertFalse(result["gate"]["mechanical_blocked"])
                self.assertEqual(result["gate"]["mechanical_failures"], [])

        for index, evidence in enumerate(failing_evidence):
            with self.subTest(expected="fail", evidence=evidence):
                result = FusionOrchestrator(
                    orchestration_config(),
                    FakeProviderRegistry(),
                ).adversarial_gate(
                    "Release only when explicit deterministic checks pass.",
                    "Candidate artifact",
                    mechanical_evidence=evidence,
                    run_id=f"mechanical-fail-{index}",
                )
                self.assertFalse(result["gate"]["passed"])
                self.assertTrue(result["gate"]["mechanical_blocked"])
                self.assertTrue(result["gate"]["mechanical_failures"])
                self.assertEqual(result["gate"]["pass_count"], 2)

    def test_invalid_structured_verdict_blocks_even_when_transport_failures_may_degrade(self) -> None:
        config = orchestration_config()
        gates = config["profiles"]["maximum_intelligence"]["gates"]
        gates["fail_closed"] = False
        gates["required_passes"] = 1
        registry = FakeProviderRegistry(invalid_verdict_seats={"grok45_verifier"})

        result = FusionOrchestrator(config, registry).adversarial_gate(
            "Reject malformed structured review evidence.",
            "Candidate artifact",
            run_id="schema-failure-blocks",
        )

        self.assertFalse(result["gate"]["passed"])
        self.assertEqual(result["gate"]["pass_count"], 1)
        self.assertTrue(result["gate"]["schema_blocked"])
        self.assertEqual(
            [failure["seat_name"] for failure in result["gate"]["schema_failures"]],
            ["grok45_verifier"],
        )
        self.assertTrue(
            any("invalid structured verdict" in blocker for blocker in result["gate"]["deterministic_blockers"])
        )

    def test_pass_verdict_with_blocking_content_is_rejected_as_schema_invalid(self) -> None:
        config = orchestration_config()
        registry = VerdictOverrideRegistry(
            {
                "grok45_verifier": {
                    "blocking_findings": ["The required safety check failed."],
                    "required_actions": ["Repair and rerun the safety check."],
                }
            }
        )

        result = FusionOrchestrator(config, registry).fuse(
            "A contradictory pass verdict must fail closed.",
            run_id="contradictory-pass-verdict",
        )

        self.assertEqual(result.status, "rejected")
        self.assertFalse(result.gate["passed"])
        self.assertEqual(result.gate["pass_count"], 1)
        self.assertTrue(result.gate["schema_blocked"])
        self.assertFalse(result.execution_handoff["ready_for_host_workflow"])
        self.assertFalse(result.execution_handoff["ready"])
        self.assertFalse(result.execution_handoff["mutation_authorized"])

    def test_minority_blocking_fail_prevents_gate_and_handoff_despite_numeric_quorum(self) -> None:
        config = orchestration_config()
        config["profiles"]["maximum_intelligence"]["gates"]["required_passes"] = 1
        registry = VerdictOverrideRegistry(
            {
                "grok45_verifier": {
                    "verdict": "FAIL",
                    "summary": "A supported blocking defect remains in the candidate.",
                    "blocking_findings": ["The candidate omits the required rollback path."],
                    "required_actions": ["Add and verify a rollback path."],
                    "evidence": ["The artifact contains no rollback procedure."],
                }
            }
        )

        result = FusionOrchestrator(config, registry).fuse(
            "A minority blocking failure must override numeric quorum.",
            run_id="minority-blocking-fail",
        )

        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.gate["pass_count"], 1)
        self.assertEqual(result.gate["required_passes"], 1)
        self.assertTrue(result.gate["negative_verdict_blocked"])
        self.assertFalse(result.gate["passed"])
        self.assertEqual(
            result.gate["negative_verdicts"],
            [
                {
                    "seat_name": "grok45_verifier",
                    "verdict": "FAIL",
                    "summary": "A supported blocking defect remains in the candidate.",
                    "blocking_findings": ["The candidate omits the required rollback path."],
                    "required_actions": ["Add and verify a rollback path."],
                    "evidence": ["The artifact contains no rollback procedure."],
                }
            ],
        )
        self.assertFalse(result.execution_handoff["ready_for_host_workflow"])
        self.assertFalse(result.execution_handoff["ready"])
        self.assertFalse(result.execution_handoff["mutation_authorized"])

    def test_identical_amendment_is_rejected_without_spending_on_re_review(self) -> None:
        config = orchestration_config()
        profile = config["profiles"]["maximum_intelligence"]
        profile["gates"]["max_revision_cycles"] = 1
        registry = FakeProviderRegistry()

        result = FusionOrchestrator(config, registry).fuse(
            "Do not accept a byte-identical amendment.",
            mechanical_evidence="test failed",
            run_id="identical-amendment",
        )

        self.assertEqual(result.status, "rejected")
        self.assertIn("byte-identical", result.gate["deterministic_blockers"][0])
        gate_calls = [call for call in registry.calls if call["schema_name"] == "adversarial_verdict"]
        self.assertEqual(len(gate_calls), 2)
        call_count = len(registry.calls)

        resumed = FusionOrchestrator(config, registry).fuse(
            "Do not accept a byte-identical amendment.",
            mechanical_evidence="test failed",
            run_id="identical-amendment",
        )

        self.assertEqual(len(registry.calls), call_count)
        self.assertEqual(resumed.status, "rejected")
        self.assertIn("byte-identical", resumed.gate["deterministic_blockers"][0])

    def test_degradation_disabled_rejects_a_partially_live_panel(self) -> None:
        config = orchestration_config(min_live_seats=2, allow_degradation=False)
        registry = FakeProviderRegistry(fail_seats={"grok45_adversary"})

        with self.assertRaisesRegex(ProviderError, "Panel degradation is disabled"):
            FusionOrchestrator(config, registry).fuse("No degradation fixture", run_id="no-degradation")

    def test_empty_kill_file_aborts_run(self) -> None:
        config = orchestration_config()
        with RunStore("Kill fixture", config, "empty-kill-file") as store:
            kill_file = store.directory / "KILL"
            kill_file.touch()
            self.assertEqual(kill_file.stat().st_size, 0)

            with self.assertRaisesRegex(RunAborted, "stopped by kill switch"):
                store.check_kill()

            manifest = store.read_json("manifest.json")
            self.assertEqual(manifest["status"], "aborted")


if __name__ == "__main__":
    unittest.main()
