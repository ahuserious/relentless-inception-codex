from __future__ import annotations

import copy
import concurrent.futures
import errno
import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests.support import PLUGIN_ROOT  # noqa: F401  (adds the plugin package to sys.path)

from relentless_inception.errors import BudgetExceeded, ConfigError
from relentless_inception import state as state_module
from relentless_inception.state import (
    BudgetTracker,
    RunStore,
    call_receipt_entry_id,
    canonical_json_hash,
)
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


def _race_run_reservation(
    data_directory: str,
    start_event,
    release_event,
    outcome_queue,
) -> None:
    os.environ["RELENTLESS_INCEPTION_DATA_DIR"] = data_directory
    try:
        start_event.wait(timeout=10)
        with RunStore(
            "Multiprocess max-calls fixture",
            {"fixture": True},
            "multiprocess-max-calls",
        ) as store:
            tracker = BudgetTracker(budget_config(max_calls=1))
            if store.exists("ledger.json"):
                tracker.restore(store.read_json("ledger.json"))
            tracker.reserve_attempt("gate", "shared-seat")
            store.write_budget_snapshot(tracker)
            outcome_queue.put(("reserved", tracker.snapshot()["calls"]))
            release_event.wait(timeout=10)
    except ConfigError as exc:
        outcome_queue.put(("active", str(exc)))
    except BaseException as exc:
        outcome_queue.put(("unexpected", f"{type(exc).__name__}: {exc}"))


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
            try:
                def reserve_and_persist(_: int) -> None:
                    tracker.reserve_attempt("gate", "concurrent-seat")
                    store.write_budget_snapshot(tracker)

                with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
                    list(executor.map(reserve_and_persist, range(64)))

                persisted = store.read_json("ledger.json")
            finally:
                store.close()

        self.assertEqual(persisted["calls"], 64)
        self.assertEqual(persisted["attempts"], 64)

    @unittest.skipUnless(os.name == "posix", "POSIX flock regression")
    def test_multiprocess_resume_has_one_owner_and_one_max_call_reservation(self) -> None:
        process_context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as temporary_directory:
            start_event = process_context.Event()
            release_event = process_context.Event()
            outcome_queue = process_context.Queue()
            processes = [
                process_context.Process(
                    target=_race_run_reservation,
                    args=(
                        temporary_directory,
                        start_event,
                        release_event,
                        outcome_queue,
                    ),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            start_event.set()
            try:
                outcomes = [outcome_queue.get(timeout=15) for _ in processes]
            finally:
                release_event.set()
                for process in processes:
                    process.join(timeout=15)
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)

            self.assertEqual([outcome[0] for outcome in outcomes].count("reserved"), 1)
            self.assertEqual([outcome[0] for outcome in outcomes].count("active"), 1)
            active_error = next(outcome[1] for outcome in outcomes if outcome[0] == "active")
            self.assertIn("already active; concurrent resume refused", active_error)
            self.assertTrue(all(process.exitcode == 0 for process in processes))

            ledger_path = (
                Path(temporary_directory)
                / "runs"
                / "multiprocess-max-calls"
                / "ledger.json"
            )
            persisted = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["calls"], 1)
            self.assertEqual(persisted["attempts"], 1)

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

    def test_attempt_and_response_receipts_form_a_deterministic_hash_chain(self) -> None:
        tracker = BudgetTracker(budget_config())
        invocation_sha256 = canonical_json_hash(
            {
                "stage": "panel",
                "seat": "receipt-seat",
                "system": "independent analysis",
                "prompt": "evaluate",
            }
        )
        reservation = tracker.reserve_attempt(
            "panel",
            "receipt-seat",
            invocation_sha256,
        )
        model_response = response(
            usage=Usage(input_tokens=2, output_tokens=3, cost_usd=0.25)
        )
        response_sha256 = canonical_json_hash(model_response.to_dict())
        entry_id = call_receipt_entry_id(
            reservation["attempt_id"],
            invocation_sha256,
            response_sha256,
        )
        response_artifact = f"responses/{entry_id}.json"

        recorded_entry_index = tracker.record(
            "panel",
            "receipt-seat",
            model_response,
            attempt_index=reservation["attempt_index"],
            attempt_id=reservation["attempt_id"],
            invocation_sha256=invocation_sha256,
            response_sha256=response_sha256,
            response_artifact=response_artifact,
        )

        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["schema_version"], 3)
        self.assertEqual(recorded_entry_index, 0)
        self.assertEqual(
            snapshot["attempt_entries"],
            [
                {
                    "attempt_index": 0,
                    "attempt_id": reservation["attempt_id"],
                    "stage": "panel",
                    "seat": "receipt-seat",
                    "invocation_sha256": invocation_sha256,
                }
            ],
        )
        self.assertEqual(snapshot["entries"][0]["attempt_index"], 0)
        self.assertEqual(snapshot["entries"][0]["attempt_id"], reservation["attempt_id"])
        self.assertEqual(snapshot["entries"][0]["entry_id"], entry_id)
        self.assertEqual(snapshot["entries"][0]["response_sha256"], response_sha256)
        self.assertEqual(snapshot["entries"][0]["response_artifact"], response_artifact)
        self.assertEqual(snapshot["entries"][0]["raw_status"], "completed")

        resumed = BudgetTracker(budget_config())
        resumed.restore(snapshot)
        self.assertEqual(resumed.snapshot()["entries"][0]["entry_id"], entry_id)

    def test_record_rejects_receipt_mismatch_without_consuming_the_attempt(self) -> None:
        tracker = BudgetTracker(budget_config())
        invocation_sha256 = canonical_json_hash({"fixture": "record-mismatch"})
        reservation = tracker.reserve_call(
            "judge",
            "receipt-seat",
            invocation_sha256,
        )
        model_response = response(
            usage=Usage(input_tokens=1, output_tokens=1, cost_usd=0.1)
        )

        with self.assertRaisesRegex(ConfigError, "response_sha256 does not match"):
            tracker.record(
                "judge",
                "receipt-seat",
                model_response,
                attempt_index=reservation["attempt_index"],
                attempt_id=reservation["attempt_id"],
                invocation_sha256=invocation_sha256,
                response_sha256="0" * 64,
            )
        self.assertEqual(tracker.snapshot()["entries"], [])

        response_sha256 = canonical_json_hash(model_response.to_dict())
        entry_id = call_receipt_entry_id(
            reservation["attempt_id"],
            invocation_sha256,
            response_sha256,
        )
        with self.assertRaisesRegex(ConfigError, "response_artifact does not match"):
            tracker.record(
                "judge",
                "receipt-seat",
                model_response,
                attempt_index=reservation["attempt_index"],
                attempt_id=reservation["attempt_id"],
                invocation_sha256=invocation_sha256,
                response_sha256=response_sha256,
                response_artifact=f"responses/{'f' * 64}.json",
            )
        self.assertEqual(tracker.snapshot()["entries"], [])

        tracker.record(
            "judge",
            "receipt-seat",
            model_response,
            attempt_index=reservation["attempt_index"],
            attempt_id=reservation["attempt_id"],
            invocation_sha256=invocation_sha256,
            response_sha256=response_sha256,
            response_artifact=f"responses/{entry_id}.json",
        )
        with self.assertRaisesRegex(ConfigError, "unrecorded reserved attempt"):
            tracker.record(
                "judge",
                "receipt-seat",
                model_response,
                attempt_index=reservation["attempt_index"],
                attempt_id=reservation["attempt_id"],
                invocation_sha256=invocation_sha256,
                response_sha256=response_sha256,
                response_artifact=f"responses/{entry_id}.json",
            )

    def test_record_rejects_invalid_response_envelope_without_mutating_accounting(self) -> None:
        invalid_responses = (
            ModelResponse(
                text="invalid latency",
                provider="test_provider",
                requested_model="requested",
                actual_model="actual",
                usage=Usage(input_tokens=1, output_tokens=1, cost_usd=0.1),
                latency_seconds=float("nan"),
            ),
            ModelResponse(
                text="invalid route",
                provider="test_provider",
                requested_model="requested",
                actual_model="actual",
                usage=Usage(input_tokens=1, output_tokens=1, cost_usd=0.1),
                route={"not_json": object()},
            ),
        )
        for invalid_response in invalid_responses:
            with self.subTest(response_text=invalid_response.text):
                tracker = BudgetTracker(budget_config())
                tracker.reserve_attempt("panel", "envelope-seat")
                before = tracker.snapshot()

                with self.assertRaises(ConfigError):
                    tracker.record("panel", "envelope-seat", invalid_response)

                after = tracker.snapshot()
                before.pop("wall_seconds")
                after.pop("wall_seconds")
                self.assertEqual(after, before)

    def test_record_and_snapshot_detach_mutable_response_containers(self) -> None:
        tracker = BudgetTracker(budget_config())
        tracker.reserve_attempt("panel", "detached-seat")
        model_response = response(
            usage=Usage(input_tokens=1, output_tokens=1, cost_usd=0.1)
        )
        model_response.route = {"nested": {"models": ["original"]}}
        tracker.record("panel", "detached-seat", model_response)

        model_response.route["nested"]["models"].append("caller-mutation")
        first_snapshot = tracker.snapshot()
        self.assertEqual(
            first_snapshot["entries"][0]["route"]["nested"]["models"],
            ["original"],
        )
        first_snapshot["entries"][0]["route"]["nested"]["models"].append(
            "snapshot-mutation"
        )
        self.assertEqual(
            tracker.snapshot()["entries"][0]["route"]["nested"]["models"],
            ["original"],
        )

    def test_receipt_schema_versions_reject_bool_and_float(self) -> None:
        source = BudgetTracker(budget_config())
        valid_snapshot = source.snapshot()
        for invalid_schema_version in (True, 3.0):
            with self.subTest(schema_version=invalid_schema_version):
                candidate = copy.deepcopy(valid_snapshot)
                candidate["schema_version"] = invalid_schema_version
                with self.assertRaisesRegex(ConfigError, "Unsupported budget snapshot schema"):
                    BudgetTracker(budget_config()).restore(candidate)

    def test_atomic_json_fsyncs_parent_directory_after_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory, mock.patch.object(
            state_module.os,
            "fsync",
            wraps=os.fsync,
        ) as fsync:
            state_module._atomic_json(
                Path(temporary_directory) / "atomic.json",
                {"persisted": True},
            )

        self.assertGreaterEqual(fsync.call_count, 2)

    def test_atomic_json_propagates_parent_directory_fsync_eio(self) -> None:
        real_fsync = os.fsync
        fsync_calls = 0

        def fail_parent_fsync(descriptor: int) -> None:
            nonlocal fsync_calls
            fsync_calls += 1
            if fsync_calls == 2:
                raise OSError(errno.EIO, "synthetic directory fsync failure")
            real_fsync(descriptor)

        with tempfile.TemporaryDirectory() as temporary_directory, mock.patch.object(
            state_module.os,
            "fsync",
            side_effect=fail_parent_fsync,
        ):
            with self.assertRaisesRegex(OSError, "synthetic directory fsync failure"):
                state_module._atomic_json(
                    Path(temporary_directory) / "atomic.json",
                    {"persisted": True},
                )

    def test_restore_rejects_tampered_attempt_and_response_receipts(self) -> None:
        source = BudgetTracker(budget_config())
        for fixture_index in range(2):
            invocation_sha256 = canonical_json_hash(
                {"fixture": "restore-tamper", "index": fixture_index}
            )
            reservation = source.reserve_attempt(
                "panel",
                f"seat-{fixture_index}",
                invocation_sha256,
            )
            source.record(
                "panel",
                f"seat-{fixture_index}",
                response(
                    usage=Usage(
                        input_tokens=1,
                        output_tokens=1,
                        cost_usd=0.1,
                    )
                ),
                attempt_index=reservation["attempt_index"],
                attempt_id=reservation["attempt_id"],
                invocation_sha256=invocation_sha256,
            )
        valid_snapshot = source.snapshot()

        def delete_last_attempt(candidate: dict[str, Any]) -> None:
            candidate["attempt_entries"].pop()

        def reverse_attempts(candidate: dict[str, Any]) -> None:
            candidate["attempt_entries"].reverse()

        def corrupt_attempt_invocation(candidate: dict[str, Any]) -> None:
            candidate["attempt_entries"][0]["invocation_sha256"] = "X" * 64

        def corrupt_attempt_id(candidate: dict[str, Any]) -> None:
            candidate["attempt_entries"][0]["attempt_id"] = "0" * 64

        def duplicate_recorded_attempt(candidate: dict[str, Any]) -> None:
            candidate["entries"][1]["attempt_index"] = 0

        def mismatch_entry_invocation(candidate: dict[str, Any]) -> None:
            candidate["entries"][0]["invocation_sha256"] = "0" * 64

        def corrupt_response_hash(candidate: dict[str, Any]) -> None:
            candidate["entries"][0]["response_sha256"] = "not-a-hash"

        def corrupt_entry_id(candidate: dict[str, Any]) -> None:
            candidate["entries"][0]["entry_id"] = "0" * 64

        def redirect_response_artifact(candidate: dict[str, Any]) -> None:
            candidate["entries"][0]["response_artifact"] = "../forged.json"

        def empty_raw_status(candidate: dict[str, Any]) -> None:
            candidate["entries"][0]["raw_status"] = ""

        tamper_cases = (
            (delete_last_attempt, "attempt_entries length must equal attempts"),
            (reverse_attempts, "zero-based attempt_index order"),
            (corrupt_attempt_invocation, "lowercase SHA-256 digest"),
            (corrupt_attempt_id, "attempt_id does not match its invocation"),
            (duplicate_recorded_attempt, "more than one recorded response"),
            (mismatch_entry_invocation, "receipt does not match its reserved attempt"),
            (corrupt_response_hash, "lowercase SHA-256 digest"),
            (corrupt_entry_id, "entry_id does not match its receipt"),
            (redirect_response_artifact, "response_artifact does not match its entry_id"),
            (empty_raw_status, "raw_status must be a nonempty string"),
        )
        for tamper, expected_error in tamper_cases:
            with self.subTest(expected_error=expected_error):
                candidate = copy.deepcopy(valid_snapshot)
                tamper(candidate)
                with self.assertRaisesRegex(ConfigError, expected_error):
                    BudgetTracker(budget_config()).restore(candidate)

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

    def test_unknown_cost_hard_latches_even_when_thresholds_only_warn(self) -> None:
        cases = (
            (
                budget_config(enforcement="warn_only", unknown_cost_policy="fail_closed"),
                Usage(input_tokens=1, output_tokens=1, cost_usd=None),
                "did not report cost",
            ),
            (
                budget_config(enforcement="warn_only", unknown_cost_policy="warn"),
                Usage(
                    input_tokens=1,
                    output_tokens=1,
                    cost_usd=None,
                    unknown_cost_fail_closed=True,
                ),
                "exceeded its base-rate context threshold",
            ),
        )
        for config, usage, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                tracker = BudgetTracker(config)
                tracker.reserve_attempt("panel", "unknown-cost-seat")
                with self.assertRaisesRegex(BudgetExceeded, expected_error):
                    tracker.record("panel", "unknown-cost-seat", response(usage=usage))
                with self.assertRaisesRegex(BudgetExceeded, expected_error):
                    tracker.reserve_attempt("gate", "same-process-seat")

                snapshot = tracker.snapshot()
                self.assertIn(expected_error, snapshot["accounting_failure"])
                unlatched_snapshot = copy.deepcopy(snapshot)
                unlatched_snapshot["accounting_failure"] = None
                unlatched_snapshot["stop_reason"] = None
                unlatched_snapshot["entries"][0]["usage"]["accounting_error"] = None
                with self.assertRaisesRegex(ConfigError, "unlatched usage integrity failure"):
                    BudgetTracker(config).restore(unlatched_snapshot)
                resumed = BudgetTracker(config)
                resumed.restore(snapshot)
                with self.assertRaisesRegex(BudgetExceeded, expected_error):
                    resumed.reserve_attempt("gate", "resumed-seat")

    def test_inconsistent_known_and_unknown_cost_status_preserves_cost_and_latches(self) -> None:
        config = budget_config(enforcement="warn_only", unknown_cost_policy="warn")
        tracker = BudgetTracker(config)
        tracker.reserve_attempt("panel", "inconsistent-cost-seat")

        with self.assertRaisesRegex(BudgetExceeded, "known cost cannot also be marked"):
            tracker.record(
                "panel",
                "inconsistent-cost-seat",
                response(
                    usage=Usage(
                        input_tokens=1,
                        output_tokens=1,
                        cost_usd=0.25,
                        unknown_cost_fail_closed=True,
                    )
                ),
            )

        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["known_cost_usd"], 0.25)
        self.assertEqual(snapshot["unknown_cost_calls"], 0)
        self.assertTrue(snapshot["entries"][0]["usage"]["raw_usage_invalid"])
        resumed = BudgetTracker(config)
        resumed.restore(snapshot)
        with self.assertRaisesRegex(BudgetExceeded, "known cost cannot also be marked"):
            resumed.reserve_attempt("gate", "later-seat")

    def test_invalid_provider_usage_latches_across_later_dispatch(self) -> None:
        tracker = BudgetTracker(budget_config())
        tracker.reserve_attempt("panel", "invalid-usage-seat")

        with self.assertRaisesRegex(BudgetExceeded, "invalid input token usage"):
            tracker.record(
                "panel",
                "invalid-usage-seat",
                response(usage=Usage(input_tokens=-1, cost_usd=0.01)),
            )

        snapshot = tracker.snapshot()
        self.assertIn("invalid input token usage", snapshot["stop_reason"])
        self.assertEqual(snapshot["known_cost_usd"], 0.01)
        self.assertEqual(snapshot["provider_cost_usd"], {"test_provider": 0.01})
        self.assertEqual(len(snapshot["entries"]), 1)
        self.assertEqual(snapshot["entries"][0]["usage"]["input_tokens"], 0)
        self.assertTrue(snapshot["entries"][0]["usage"]["raw_usage_invalid"])
        with self.assertRaisesRegex(BudgetExceeded, "invalid input token usage"):
            tracker.reserve_attempt("gate", "later-seat")

        resumed = BudgetTracker(budget_config())
        resumed.restore(snapshot)
        with self.assertRaisesRegex(BudgetExceeded, "invalid input token usage"):
            resumed.reserve_attempt("gate", "resumed-seat")

    def test_incomplete_token_usage_preserves_known_cost_and_hard_latches(self) -> None:
        config = budget_config(enforcement="warn_only", unknown_cost_policy="warn")
        tracker = BudgetTracker(config)
        tracker.reserve_attempt("panel", "incomplete-usage-seat")
        accounting_error = (
            "Provider returned invalid or incomplete usage: missing output token count"
        )

        with self.assertRaisesRegex(BudgetExceeded, "missing output token count"):
            tracker.record(
                "panel",
                "incomplete-usage-seat",
                response(
                    usage=Usage(
                        input_tokens=10,
                        cost_usd=0.25,
                        input_output_usage_complete=False,
                        accounting_error=accounting_error,
                    )
                ),
            )

        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["known_cost_usd"], 0.25)
        self.assertEqual(snapshot["provider_cost_usd"], {"test_provider": 0.25})
        self.assertEqual(snapshot["unknown_cost_calls"], 0)
        self.assertEqual(len(snapshot["entries"]), 1)
        self.assertEqual(snapshot["accounting_failure"], accounting_error)

        missing_latch = copy.deepcopy(snapshot)
        del missing_latch["accounting_failure"]
        with self.assertRaisesRegex(ConfigError, "missing required fields accounting_failure"):
            BudgetTracker(config).restore(missing_latch)

        unlatched_entry = copy.deepcopy(snapshot)
        unlatched_entry["accounting_failure"] = None
        unlatched_entry["stop_reason"] = None
        unlatched_entry["entries"][0]["usage"]["accounting_error"] = None
        with self.assertRaisesRegex(ConfigError, "unlatched usage integrity failure"):
            BudgetTracker(config).restore(unlatched_entry)

        resumed = BudgetTracker(config)
        resumed.restore(snapshot)
        with self.assertRaisesRegex(BudgetExceeded, "missing output token count"):
            resumed.reserve_attempt("gate", "later-seat")
        self.assertEqual(resumed.snapshot()["calls"], 1)

    def test_first_accounting_failure_wins_across_in_flight_responses(self) -> None:
        config = budget_config(enforcement="warn_only", unknown_cost_policy="warn")
        tracker = BudgetTracker(config)
        tracker.reserve_attempt("panel", "first-seat")
        tracker.reserve_attempt("panel", "second-seat")
        first_error = "Provider returned incomplete usage: first failure"
        second_error = "Provider returned invalid usage: second failure"

        with self.assertRaisesRegex(BudgetExceeded, "first failure"):
            tracker.record(
                "panel",
                "first-seat",
                response(
                    usage=Usage(
                        input_tokens=1,
                        cost_usd=0.1,
                        input_output_usage_complete=False,
                        accounting_error=first_error,
                    )
                ),
            )
        with self.assertRaisesRegex(BudgetExceeded, "first failure"):
            tracker.record(
                "panel",
                "second-seat",
                response(
                    usage=Usage(
                        input_tokens=1,
                        output_tokens=1,
                        cost_usd=0.2,
                        raw_usage_invalid=True,
                        accounting_error=second_error,
                    )
                ),
            )

        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["accounting_failure"], first_error)
        self.assertEqual(snapshot["stop_reason"], first_error)
        self.assertEqual(
            [entry["usage"]["accounting_error"] for entry in snapshot["entries"]],
            [first_error, second_error],
        )
        resumed = BudgetTracker(config)
        resumed.restore(snapshot)
        with self.assertRaisesRegex(BudgetExceeded, "first failure"):
            resumed.reserve_attempt("gate", "later-seat")

    def test_accounting_failure_can_follow_a_prior_threshold_stop(self) -> None:
        config = budget_config(max_total_tokens=1)
        tracker = BudgetTracker(config)
        tracker.reserve_attempt("panel", "threshold-seat")
        tracker.reserve_attempt("panel", "in-flight-seat")
        tracker.record(
            "panel",
            "threshold-seat",
            response(usage=Usage(input_tokens=1, output_tokens=0, cost_usd=0.1)),
        )
        threshold_stop = tracker.snapshot()["stop_reason"]
        accounting_error = "Provider returned incomplete usage after threshold"

        with self.assertRaisesRegex(BudgetExceeded, "after threshold"):
            tracker.record(
                "panel",
                "in-flight-seat",
                response(
                    usage=Usage(
                        input_tokens=1,
                        cost_usd=0.2,
                        input_output_usage_complete=False,
                        accounting_error=accounting_error,
                    )
                ),
            )

        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["stop_reason"], threshold_stop)
        self.assertEqual(snapshot["accounting_failure"], accounting_error)
        resumed = BudgetTracker(config)
        resumed.restore(snapshot)
        with self.assertRaisesRegex(BudgetExceeded, "after threshold"):
            resumed.reserve_attempt("gate", "later-seat")

    def test_restore_validates_every_accounting_field_before_mutating(self) -> None:
        source = BudgetTracker(budget_config())
        source.reserve_attempt("panel", "source-seat")
        source.record(
            "panel",
            "source-seat",
            response(usage=Usage(input_tokens=2, output_tokens=3, cost_usd=0.25)),
        )
        valid_snapshot = source.snapshot()
        invalid_cases = (
            ("missing fields", {}, "Unsupported budget snapshot schema"),
            ("boolean schema", {"schema_version": True}, "Unsupported budget snapshot schema"),
            ("fractional schema", {"schema_version": 3.0}, "Unsupported budget snapshot schema"),
            ("negative counter", {"calls": -1}, "calls"),
            ("boolean counter", {"input_tokens": True}, "input_tokens"),
            ("fractional counter", {"output_tokens": 1.5}, "output_tokens"),
            ("string cost", {"known_cost_usd": "0.25"}, "known_cost_usd"),
            ("overflowing cost", {"known_cost_usd": 10**10_000}, "known_cost_usd"),
            (
                "nonfinite provider cost",
                {"provider_cost_usd": {"test_provider": float("nan")}},
                "provider_cost",
            ),
            ("nonfinite wall time", {"wall_seconds": float("inf")}, "wall_seconds"),
            ("attempt mismatch", {"attempts": 2}, "attempts must equal calls"),
            ("token total mismatch", {"total_tokens": 99}, "total_tokens"),
            (
                "provider cost mismatch",
                {"provider_cost_usd": {"test_provider": 0.2}},
                "provider_cost_usd must match entries",
            ),
            (
                "redistributed provider cost",
                {"provider_cost_usd": {"different-provider": 0.25}},
                "provider_cost_usd must match entries",
            ),
            (
                "forged aggregate cost",
                {"known_cost_usd": 0.0, "provider_cost_usd": {}},
                "known_cost_usd must match entries",
            ),
            (
                "forged aggregate tokens",
                {"input_tokens": 0, "total_tokens": 3},
                "aggregate usage counters must match entries",
            ),
            (
                "forged unknown cost count",
                {"unknown_cost_calls": 1},
                "unknown_cost_calls must match entries",
            ),
            ("invalid entries", {"entries": ["not-an-object"]}, "entries"),
            ("invalid stop reason", {"stop_reason": False}, "stop_reason"),
            ("invalid accounting failure", {"accounting_failure": []}, "accounting_failure"),
            (
                "late validation failure",
                {"warnings": [1]},
                "warnings",
            ),
        )

        for label, updates, expected_error in invalid_cases:
            with self.subTest(label=label):
                candidate = {} if label == "missing fields" else copy.deepcopy(valid_snapshot)
                candidate.update(updates)
                target = BudgetTracker(budget_config())
                target.reserve_attempt("panel", "existing-seat")
                target.record(
                    "panel",
                    "existing-seat",
                    response(usage=Usage(input_tokens=1, output_tokens=1, cost_usd=0.5)),
                )
                before = target.snapshot()

                with self.assertRaisesRegex(ConfigError, expected_error):
                    target.restore(candidate)

                after = target.snapshot()
                before.pop("wall_seconds")
                after.pop("wall_seconds")
                self.assertEqual(after, before)

    def test_restore_copies_untrusted_containers_before_mutating(self) -> None:
        class ExplodingRoute(dict):
            def __deepcopy__(self, memo):
                del memo
                raise RuntimeError("synthetic deepcopy failure")

        source = BudgetTracker(budget_config())
        source.reserve_attempt("panel", "source-seat")
        source.record(
            "panel",
            "source-seat",
            response(usage=Usage(input_tokens=1, output_tokens=1, cost_usd=0.25)),
        )
        candidate = copy.deepcopy(source.snapshot())
        candidate["entries"][0]["route"] = ExplodingRoute()

        target = BudgetTracker(budget_config())
        target.reserve_attempt("panel", "existing-seat")
        target.record(
            "panel",
            "existing-seat",
            response(usage=Usage(input_tokens=2, output_tokens=2, cost_usd=0.5)),
        )
        before = target.snapshot()

        with self.assertRaisesRegex(ConfigError, "could not be safely copied"):
            target.restore(candidate)

        after = target.snapshot()
        before.pop("wall_seconds")
        after.pop("wall_seconds")
        self.assertEqual(after, before)

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
