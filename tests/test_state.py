from __future__ import annotations

import concurrent.futures
import os
import tempfile
import unittest
from unittest import mock

from tests.support import PLUGIN_ROOT  # noqa: F401  (adds the plugin package to sys.path)

from relentless_inception.errors import BudgetExceeded
from relentless_inception.state import BudgetTracker, RunStore
from relentless_inception.types import ModelResponse, Usage


def budget_config(**overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "enforcement": "hard_stop",
        "unknown_cost_policy": "fail_closed",
        "max_calls": 100,
        "max_total_tokens": 100,
        "max_input_tokens": 100,
        "max_output_tokens": 100,
        "max_reasoning_tokens": 100,
        "max_tool_calls": 100,
        "max_wall_seconds": 60,
        "max_cost_usd": 100.0,
        "approval_threshold_usd": 25.0,
        "warning_fraction": 0.8,
        "reserve_fraction_for_synthesis_and_gates": 0.0,
        "per_provider_max_cost_usd": {"test_provider": 100.0},
    }
    config.update(overrides)
    return config


def response(*, usage: Usage) -> ModelResponse:
    return ModelResponse(
        text="complete",
        provider="test_provider",
        requested_model="requested",
        actual_model="actual",
        usage=usage,
    )


class BudgetTrackerTests(unittest.TestCase):
    def test_call_attempt_limit_is_atomic_under_concurrency(self) -> None:
        tracker = BudgetTracker(budget_config(max_calls=11))

        def reserve() -> bool:
            try:
                tracker.reserve_attempt("gate", "concurrent-seat")
            except BudgetExceeded:
                return False
            return True

        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
            outcomes = list(executor.map(lambda _: reserve(), range(64)))

        self.assertEqual(sum(outcomes), 11)
        self.assertEqual(tracker.snapshot()["calls"], 11)
        self.assertEqual(tracker.snapshot()["attempts"], 11)

    def test_concurrent_budget_persistence_never_regresses_the_attempt_ledger(self) -> None:
        tracker = BudgetTracker(budget_config(max_calls=64))
        with tempfile.TemporaryDirectory() as temporary_directory, mock.patch.dict(
            os.environ,
            {"RELENTLESS_INCEPTION_DATA_DIR": temporary_directory},
            clear=False,
        ):
            store = RunStore("Concurrent ledger fixture", {"fixture": True}, "concurrent-ledger")

            def reserve_and_persist(_: int) -> None:
                tracker.reserve_attempt("gate", "concurrent-seat")
                store.write_budget_snapshot(tracker)

            with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
                list(executor.map(reserve_and_persist, range(64)))

            persisted = store.read_json("ledger.json")

        self.assertEqual(persisted["calls"], 64)
        self.assertEqual(persisted["attempts"], 64)

    def test_resume_preserves_attempt_exhaustion_before_dispatch(self) -> None:
        config = budget_config(max_calls=2)
        original = BudgetTracker(config)
        original.reserve_attempt("judge", "first")
        original.reserve_attempt("judge", "second")

        resumed = BudgetTracker(config)
        resumed.restore(original.snapshot())

        with self.assertRaisesRegex(BudgetExceeded, "Call-attempt budget of 2 exhausted"):
            resumed.reserve_attempt("judge", "third")
        self.assertEqual(resumed.snapshot()["calls"], 2)

    def test_total_tokens_do_not_double_count_reasoning_and_cached_details(self) -> None:
        tracker = BudgetTracker(budget_config(max_total_tokens=10))
        tracker.reserve_attempt("panel", "seat")

        tracker.record(
            "panel",
            "seat",
            response(
                usage=Usage(
                    input_tokens=6,
                    output_tokens=4,
                    reasoning_tokens=3,
                    cached_tokens=4,
                    cost_usd=0.01,
                )
            ),
        )

        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["total_tokens"], 10)
        self.assertIn("Total token threshold of 10 exhausted", snapshot["stop_reason"])
        with self.assertRaisesRegex(BudgetExceeded, "Total token threshold of 10 exhausted"):
            tracker.reserve_attempt("judge", "next-seat")

    def test_observed_response_can_cross_threshold_but_blocks_every_later_dispatch(self) -> None:
        tracker = BudgetTracker(budget_config(max_total_tokens=9))
        tracker.reserve_attempt("panel", "seat")

        with self.assertRaisesRegex(BudgetExceeded, "Total token threshold of 9 exceeded"):
            tracker.record(
                "panel",
                "seat",
                response(
                    usage=Usage(
                        input_tokens=6,
                        output_tokens=4,
                        reasoning_tokens=3,
                        cached_tokens=4,
                        cost_usd=0.01,
                    )
                ),
            )

        self.assertEqual(tracker.snapshot()["total_tokens"], 10)
        with self.assertRaisesRegex(BudgetExceeded, "Total token threshold of 9 exceeded"):
            tracker.reserve_attempt("gate", "later-seat")
        self.assertEqual(tracker.snapshot()["calls"], 1)

    def test_reasoning_and_tool_details_have_independent_stop_thresholds(self) -> None:
        cases = (
            (
                {"max_reasoning_tokens": 3},
                Usage(output_tokens=4, reasoning_tokens=3, cost_usd=0.01),
                "Reasoning token threshold of 3 exhausted",
            ),
            (
                {"max_tool_calls": 1},
                Usage(output_tokens=1, tool_calls=1, cost_usd=0.01),
                "Server-tool call threshold of 1 exhausted",
            ),
        )
        for overrides, usage, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                tracker = BudgetTracker(budget_config(**overrides))
                tracker.reserve_attempt("panel", "seat")
                tracker.record("panel", "seat", response(usage=usage))
                with self.assertRaisesRegex(BudgetExceeded, expected_reason):
                    tracker.reserve_attempt("judge", "later-seat")

    def test_unknown_cost_fails_closed_and_survives_resume(self) -> None:
        config = budget_config()
        tracker = BudgetTracker(config)
        tracker.reserve_attempt("panel", "unknown-cost-seat")

        with self.assertRaisesRegex(BudgetExceeded, "did not report cost"):
            tracker.record(
                "panel",
                "unknown-cost-seat",
                response(usage=Usage(input_tokens=1, output_tokens=1, cost_usd=None)),
            )

        resumed = BudgetTracker(config)
        resumed.restore(tracker.snapshot())
        with self.assertRaisesRegex(BudgetExceeded, "did not report cost"):
            resumed.reserve_attempt("judge", "later-seat")
        self.assertEqual(resumed.snapshot()["unknown_cost_calls"], 1)

    def test_resume_does_not_round_a_small_cost_below_its_threshold(self) -> None:
        config = budget_config(
            max_cost_usd=0.000000004,
            per_provider_max_cost_usd={"test_provider": 0.000000004},
        )
        tracker = BudgetTracker(config)
        tracker.reserve_attempt("panel", "small-cost-seat")
        tracker.record(
            "panel",
            "small-cost-seat",
            response(usage=Usage(input_tokens=1, cost_usd=0.000000004)),
        )

        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["known_cost_usd"], 0.000000004)
        resumed = BudgetTracker(config)
        resumed.restore(snapshot)
        with self.assertRaisesRegex(BudgetExceeded, "Known cost threshold"):
            resumed.reserve_attempt("gate", "later-seat")

    def test_warn_only_records_thresholds_and_continues(self) -> None:
        tracker = BudgetTracker(
            budget_config(
                enforcement="warn_only",
                unknown_cost_policy="warn",
                max_calls=1,
                max_total_tokens=1,
            )
        )
        tracker.reserve_attempt("judge", "first")
        tracker.record(
            "judge",
            "first",
            response(usage=Usage(input_tokens=2, cost_usd=None)),
        )
        tracker.reserve_attempt("judge", "second")

        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["calls"], 2)
        self.assertIsNone(snapshot["stop_reason"])
        self.assertTrue(any("Call-attempt budget" in warning for warning in snapshot["warnings"]))
        self.assertTrue(any("Total token threshold" in warning for warning in snapshot["warnings"]))
        self.assertTrue(any("did not report cost" in warning for warning in snapshot["warnings"]))

    def test_approval_mode_fails_closed_without_an_explicit_config_change(self) -> None:
        tracker = BudgetTracker(budget_config(enforcement="approval_then_hard_stop", max_calls=1))
        tracker.reserve_attempt("gate", "first")

        with self.assertRaisesRegex(BudgetExceeded, "host approval and an explicit budget configuration change"):
            tracker.reserve_attempt("gate", "second")
        self.assertEqual(tracker.snapshot()["calls"], 1)


if __name__ == "__main__":
    unittest.main()
