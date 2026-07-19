"""Shared deterministic fixtures for the dependency-free test suite."""

from __future__ import annotations

import copy
import json
import re
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPOSITORY_ROOT / "plugins" / "relentless-inception"
MCP_SERVER_PATH = PLUGIN_ROOT / "mcp_server.py"

if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from relentless_inception.config import load_config  # noqa: E402
from relentless_inception.errors import ProviderError  # noqa: E402
from relentless_inception.types import ModelResponse, Usage  # noqa: E402


DEFAULT_PANEL = [
    "grok45_researcher",
    "grok45_adversary",
    "grok43_constraint_auditor",
]
DEFAULT_REVIEWERS = ["grok45_verifier", "grok43_constraint_auditor"]


def orchestration_config(
    *,
    panel: Optional[Iterable[str]] = None,
    min_live_seats: Optional[int] = None,
    allow_degradation: bool = False,
) -> Dict[str, Any]:
    """Return a default-derived profile made fast and deterministic for tests."""

    config = copy.deepcopy(load_config(include_user=False))
    profile = config["profiles"]["maximum_intelligence"]
    panel_names = list(panel or DEFAULT_PANEL)
    profile["fusion"].update(
        {
            "engine": "client_orchestrated",
            "panel": panel_names,
            "optional_panel": [],
            "min_live_seats": min_live_seats if min_live_seats is not None else len(panel_names),
            "max_panel_seats": len(panel_names),
            "max_concurrency": len(panel_names),
            "allow_degradation": allow_degradation,
            "quality_floor": {
                "minimum_characters": 200,
                "reject_tool_markup": True,
            },
        }
    )
    profile["gates"].update(
        {
            "enabled": True,
            "fail_closed": True,
            "reviewers": list(DEFAULT_REVIEWERS),
            "required_passes": 2,
            "max_concurrency": 2,
            "max_revision_cycles": 0,
        }
    )
    profile["budgets"].update(
        {
            "max_calls": 50,
            "max_total_tokens": 100_000,
            "max_wall_seconds": 60,
            "max_cost_usd": 10.0,
        }
    )
    return config


class FakeProviderRegistry:
    """Thread-safe provider double that preserves every orchestration prompt."""

    SYNTHESIS_TEXT = (
        "Use a bounded, evidence-led implementation plan with explicit failure handling, "
        "then verify each stated requirement mechanically before execution. Preserve the "
        "independent minority concern and stop if the observed workspace contradicts the plan."
    )
    PANEL_TEXTS = {
        "grok45_researcher": (
            "ALPHA_REPORT recommends an evidence-first implementation with explicit acceptance "
            "criteria. It inventories dependencies, tests the happy path and malformed inputs, "
            "and records commands and outputs. The main uncertainty is provider availability. "
            "Verification should use deterministic fixtures, exact hashes, and persisted ledgers. "
            "This independent report intentionally contains enough detail to pass the quality floor."
        ),
        "grok45_adversary": (
            "BETA_REPORT challenges silent fallback, correlated reviewers, stale resumes, and weak "
            "secret handling. It recommends fail-closed schema checks, explicit degradation policy, "
            "empty-file kill switches, and assertions that failed calls remain visible in accounting. "
            "A release should be blocked whenever exact artifact identity is not proven by reviewers."
        ),
        "grok43_constraint_auditor": (
            "GAMMA_REPORT traces the requested behavior to independent panel calls, structured judging, "
            "fresh synthesis, two exact-hash verdicts, and a resumable ledger. It identifies no safe basis "
            "for network access in unit tests and requires all state to live under a temporary data root. "
            "It preserves the minority warning that successful HTTP status is not semantic success."
        ),
    }

    def __init__(
        self,
        *,
        fail_seats: Iterable[str] = (),
        verdict_blind_spots: Iterable[str] = (),
        invalid_verdict_seats: Iterable[str] = (),
    ) -> None:
        self.fail_seats = set(fail_seats)
        self.verdict_blind_spots = list(verdict_blind_spots)
        self.invalid_verdict_seats = set(invalid_verdict_seats)
        self._calls: list[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._sequence = 0

    @property
    def calls(self) -> list[Dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._calls)

    def complete(
        self,
        seat_name: str,
        *,
        system: str,
        prompt: str,
        response_schema: Optional[Mapping[str, Any]] = None,
        schema_name: str = "structured_response",
        before_attempt: Optional[Any] = None,
        on_semantic_failure_response: Optional[Any] = None,
    ) -> ModelResponse:
        del on_semantic_failure_response
        if before_attempt is not None:
            before_attempt()
        with self._lock:
            self._sequence += 1
            request_number = self._sequence
            self._calls.append(
                {
                    "seat_name": seat_name,
                    "system": system,
                    "prompt": prompt,
                    "schema_name": schema_name,
                    "has_schema": response_schema is not None,
                }
            )

        if seat_name in self.fail_seats:
            raise ProviderError(f"synthetic provider failure for {seat_name}")

        if schema_name == "fusion_judgment":
            judgment = {
                "consensus": ["Use deterministic verification."],
                "contradictions": ["The panel differs on how aggressively to degrade."],
                "partial_coverage": ["Live provider discovery is outside this offline test."],
                "unique_insights": ["An empty kill file must be sufficient."],
                "minority_findings": ["HTTP success can still be semantic failure."],
                "blind_spots": ["External provider behavior remains untested here."],
                "verification_priorities": ["Bind both verdicts to the exact synthesis hash."],
                "final_guidance": ["Fuse evidence, then require two independent passes."],
            }
            required_fields = response_schema.get("required", []) if isinstance(response_schema, Mapping) else []
            if required_fields:
                judgment = {field: judgment[field] for field in required_fields}
            text = json.dumps(judgment)
        elif schema_name == "adversarial_verdict":
            if seat_name in self.invalid_verdict_seats:
                text = "not a JSON verdict"
            else:
                hash_match = re.search(r"Candidate artifact SHA-256: ([0-9a-f]{64})", prompt)
                if hash_match is None:
                    raise AssertionError("Gate prompt did not supply an exact artifact SHA-256")
                text = json.dumps(
                    {
                        "verdict": "PASS",
                        "artifact_sha256": hash_match.group(1),
                        "summary": "The candidate is bound to the supplied hash and meets the test goal.",
                        "criteria_reviewed": ["Original requirements", "mechanical evidence", "artifact identity"],
                        "blind_spots": list(self.verdict_blind_spots),
                        "blocking_findings": [],
                        "non_blocking_findings": [],
                        "evidence": ["Deterministic fake-provider evidence."],
                        "required_actions": [],
                    }
                )
        elif seat_name in {"grok45_synthesizer", "openrouter_native_fusion_seat"}:
            text = self.SYNTHESIS_TEXT
        else:
            try:
                text = self.PANEL_TEXTS[seat_name]
            except KeyError as exc:
                raise AssertionError(f"Unexpected unstructured fake call for {seat_name}") from exc

        return ModelResponse(
            text=text,
            provider="fake_provider",
            requested_model=f"requested/{seat_name}",
            actual_model=f"actual/{seat_name}",
            usage=Usage(
                input_tokens=10,
                output_tokens=5,
                reasoning_tokens=2,
                cached_tokens=1,
                cost_usd=0.001,
            ),
            latency_seconds=0.01,
            request_id=f"fake-{request_number}",
            route={"fixture": "offline"},
        )
