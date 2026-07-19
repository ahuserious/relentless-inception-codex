from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from tests.support import DEFAULT_PANEL, FakeProviderRegistry, orchestration_config

from relentless_inception.errors import ProviderError, RunAborted
from relentless_inception.orchestrator import FusionOrchestrator
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

    def test_full_client_fusion_is_independent_structured_gated_ledgered_and_resumable(self) -> None:
        config = orchestration_config()
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
        self.assertEqual(len({call["prompt"] for call in panel_calls}), 1)
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
        self.assertTrue(result.execution_handoff["ready"])

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

    def test_panel_collapse_fails_closed_and_marks_manifest_failed(self) -> None:
        panel = ["grok45_researcher", "grok45_adversary"]
        config = orchestration_config(panel=panel, min_live_seats=2, allow_degradation=True)
        registry = FakeProviderRegistry(fail_seats={"grok45_adversary"})

        with self.assertRaisesRegex(ProviderError, r"Panel collapsed: 1/2 live"):
            FusionOrchestrator(config, registry).fuse("Collapse fixture", run_id="panel-collapse")

        manifest_path = Path(self.temporary_directory.name) / "runs" / "panel-collapse" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "failed")

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
