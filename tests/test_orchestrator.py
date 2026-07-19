from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from tests.support import DEFAULT_PANEL, FakeProviderRegistry, orchestration_config

from relentless_inception.errors import ConfigError, ProviderError, RunAborted
from relentless_inception.orchestrator import FusionOrchestrator, _contains_substantive_claim
from relentless_inception.state import RunStore, text_hash


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
        rejected = [row for row in persisted_responses if row["seat_name"] == "grok45_researcher"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["response"]["text"], "# Analysis\n## Risks\n- TBD\n- Unknown")

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
        call_count = len(registry.calls)

        resumed = FusionOrchestrator(config, registry).fuse(
            "Native success fixture",
            run_id="native-openrouter-success",
        )
        self.assertEqual(len(registry.calls), call_count)
        self.assertEqual(resumed.synthesis, first.synthesis)
        self.assertEqual(resumed.ledger["calls"], first.ledger["calls"])

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

    def test_degradation_disabled_rejects_a_partially_live_panel(self) -> None:
        config = orchestration_config(min_live_seats=2, allow_degradation=False)
        registry = FakeProviderRegistry(fail_seats={"grok45_adversary"})

        with self.assertRaisesRegex(ProviderError, "Panel degradation is disabled"):
            FusionOrchestrator(config, registry).fuse("No degradation fixture", run_id="no-degradation")

    def test_empty_kill_file_aborts_run(self) -> None:
        config = orchestration_config()
        store = RunStore("Kill fixture", config, "empty-kill-file")
        kill_file = store.directory / "KILL"
        kill_file.touch()
        self.assertEqual(kill_file.stat().st_size, 0)

        with self.assertRaisesRegex(RunAborted, "stopped by kill switch"):
            store.check_kill()

        manifest = store.read_json("manifest.json")
        self.assertEqual(manifest["status"], "aborted")


if __name__ == "__main__":
    unittest.main()
