"""Atomic persistence, single-owner run leasing, and thread-safe budgets."""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import math
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .config import canonical_hash, canonical_json, runtime_data_dir
from .errors import BudgetExceeded, ConfigError, RunAborted
from .types import ModelResponse, Usage

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    _fcntl = None  # type: ignore[assignment]

try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - exercised only on non-Windows hosts
    _msvcrt = None  # type: ignore[assignment]


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json_hash(value: Any) -> str:
    """Return the SHA-256 digest of a value's canonical JSON representation."""

    try:
        encoded = canonical_json(value)
    except ConfigError as exc:
        raise ConfigError("Receipt evidence must be valid canonical JSON") from exc
    return text_hash(encoded)


def _validated_sha256(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ConfigError(
            f"Invalid budget receipt: {field_name} must be a lowercase SHA-256 digest"
        )
    return value


def attempt_receipt_id(invocation_sha256: str, attempt_index: int) -> str:
    """Derive the stable identifier for one reserved provider attempt."""

    _validated_sha256(invocation_sha256, "invocation_sha256")
    if not isinstance(attempt_index, int) or isinstance(attempt_index, bool) or attempt_index < 0:
        raise ConfigError("Invalid budget receipt: attempt_index must be a nonnegative integer")
    return canonical_json_hash(
        {
            "schema_version": BudgetTracker.RECEIPT_SCHEMA_VERSION,
            "invocation_sha256": invocation_sha256,
            "attempt_index": attempt_index,
        }
    )


def call_receipt_entry_id(
    attempt_id: str,
    invocation_sha256: str,
    response_sha256: str,
) -> str:
    """Derive the stable identifier binding an attempt to its raw response."""

    _validated_sha256(attempt_id, "attempt_id")
    _validated_sha256(invocation_sha256, "invocation_sha256")
    _validated_sha256(response_sha256, "response_sha256")
    return canonical_json_hash(
        {
            "schema_version": BudgetTracker.RECEIPT_SCHEMA_VERSION,
            "attempt_id": attempt_id,
            "invocation_sha256": invocation_sha256,
            "response_sha256": response_sha256,
        }
    )


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        directory_descriptor: Optional[int] = None
        try:
            directory_descriptor = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            os.fsync(directory_descriptor)
        except OSError as exc:
            # Directory descriptors and directory fsync are not available on
            # every supported platform. The file itself was fsynced above.
            unsupported_errors = {
                errno.EINVAL,
                getattr(errno, "ENOTSUP", errno.EINVAL),
                getattr(errno, "EOPNOTSUPP", errno.EINVAL),
            }
            if os.name == "nt":
                unsupported_errors.add(errno.EACCES)
            if exc.errno not in unsupported_errors:
                raise
        finally:
            if directory_descriptor is not None:
                os.close(directory_descriptor)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


class RunStore:
    def __init__(
        self,
        task: str,
        config: Mapping[str, Any],
        run_id: Optional[str] = None,
        *,
        input_identity: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self._write_lock = threading.RLock()
        self.task_hash = text_hash(task)
        self.config_hash = canonical_hash(config)
        self.input_hash = canonical_hash(input_identity or {"operation": "task", "task": task})
        if run_id:
            if not run_id.replace("-", "").isalnum():
                raise ConfigError("run_id may contain only letters, digits, and hyphens")
            self.run_id = run_id
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            self.run_id = f"{stamp}-{self.input_hash[:10]}"
        self.directory = runtime_data_dir() / "runs" / self.run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(runtime_data_dir(), 0o700)
        os.chmod(runtime_data_dir() / "runs", 0o700)
        os.chmod(self.directory, 0o700)
        self.manifest_path = self.directory / "manifest.json"
        self._lease_handle: Optional[Any] = None
        self._acquire_run_lease()
        try:
            if self.manifest_path.exists():
                manifest = self.read_json("manifest.json")
                if (
                    manifest.get("task_hash") != self.task_hash
                    or manifest.get("config_hash") != self.config_hash
                    or manifest.get("input_hash") != self.input_hash
                ):
                    raise ConfigError(
                        "Resume refused: run_id task/config/input hash does not match the current request"
                    )
            else:
                self.write_json(
                    "manifest.json",
                    {
                        "run_id": self.run_id,
                        "task_hash": self.task_hash,
                        "config_hash": self.config_hash,
                        "input_hash": self.input_hash,
                        "status": "running",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "stages": {},
                    },
                )
        except BaseException:
            self.close()
            raise

    def _acquire_run_lease(self) -> None:
        if _fcntl is None and _msvcrt is None:
            raise ConfigError(
                "RunStore requires OS file locking; this platform cannot safely resume runs"
            )
        lease_path = self.directory / ".run.lock"
        lease_handle = None
        try:
            lease_handle = lease_path.open("a+b")
            os.chmod(lease_path, 0o600)
            os.set_inheritable(lease_handle.fileno(), False)
            if _fcntl is not None:
                _fcntl.flock(lease_handle.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            else:
                # Windows byte-range locks require the byte to exist and lock
                # from the current file position.
                lease_handle.seek(0, os.SEEK_END)
                if lease_handle.tell() == 0:
                    lease_handle.write(b"\0")
                    lease_handle.flush()
                    os.fsync(lease_handle.fileno())
                lease_handle.seek(0)
                _msvcrt.locking(lease_handle.fileno(), _msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if lease_handle is not None:
                lease_handle.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise ConfigError(
                    f"Run {self.run_id!r} is already active; concurrent resume refused"
                ) from exc
            raise ConfigError(
                f"Unable to acquire the run lease for {self.run_id!r}: {exc}"
            ) from exc
        self._lease_handle = lease_handle

    def close(self) -> None:
        """Explicitly release this run's single-active-owner lease."""

        with self._write_lock:
            lease_handle = getattr(self, "_lease_handle", None)
            self._lease_handle = None
            if lease_handle is None:
                return
            try:
                if _fcntl is not None:
                    _fcntl.flock(lease_handle.fileno(), _fcntl.LOCK_UN)
                elif _msvcrt is not None:
                    lease_handle.seek(0)
                    _msvcrt.locking(lease_handle.fileno(), _msvcrt.LK_UNLCK, 1)
            except OSError:
                # Closing the descriptor still releases the lease. Cleanup must
                # not mask the orchestration result or its original exception.
                pass
            try:
                lease_handle.close()
            except OSError:
                pass

    def __enter__(self) -> "RunStore":
        return self

    def __exit__(self, exception_type: Any, exception: Any, traceback: Any) -> None:
        del exception_type, exception, traceback
        self.close()

    def __del__(self) -> None:
        # Defensive fallback for direct library callers. Orchestration entry
        # points release the lease explicitly in finally blocks.
        try:
            self.close()
        except Exception:
            pass

    def path(self, relative_name: str) -> Path:
        candidate = (self.directory / relative_name).resolve()
        if self.directory.resolve() not in candidate.parents and candidate != self.directory.resolve():
            raise ConfigError("Artifact path escapes the run directory")
        return candidate

    def write_json(self, relative_name: str, value: Mapping[str, Any]) -> None:
        with self._write_lock:
            _atomic_json(self.path(relative_name), value)

    def write_budget_snapshot(self, budget: "BudgetTracker") -> Dict[str, Any]:
        """Serialize snapshot creation and replacement so stale threads cannot regress the ledger."""

        with self._write_lock:
            snapshot = budget.snapshot()
            _atomic_json(self.path("ledger.json"), snapshot)
            return snapshot

    def read_json(self, relative_name: str) -> Dict[str, Any]:
        path = self.path(relative_name)
        try:
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Unreadable run artifact: {path}") from exc
        if not isinstance(value, dict):
            raise ConfigError(f"Run artifact must be a JSON object: {path}")
        return value

    def exists(self, relative_name: str) -> bool:
        return self.path(relative_name).exists()

    def mark_stage(self, stage: str, status: str, artifact: Optional[str] = None) -> None:
        with self._write_lock:
            manifest = self.read_json("manifest.json")
            stages = manifest.setdefault("stages", {})
            stages[stage] = {
                "status": status,
                "artifact": artifact,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
            self.write_json("manifest.json", manifest)

    def finish(self, status: str) -> None:
        with self._write_lock:
            manifest = self.read_json("manifest.json")
            manifest["status"] = status
            manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
            self.write_json("manifest.json", manifest)

    def check_kill(self) -> None:
        # Existence is enough: `touch KILL` must work, unlike the source plugin.
        if (runtime_data_dir() / "KILL").exists() or (self.directory / "KILL").exists():
            self.finish("aborted")
            raise RunAborted(f"Run {self.run_id} stopped by kill switch")


class BudgetTracker:
    SNAPSHOT_SCHEMA_VERSION = 3
    RECEIPT_SCHEMA_VERSION = 1

    def __init__(self, budget_config: Mapping[str, Any]) -> None:
        self.config = dict(budget_config)
        self.started = time.monotonic()
        self.restored_wall_seconds = 0.0
        self.lock = threading.Lock()
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.reasoning_tokens = 0
        self.cached_tokens = 0
        self.tool_calls = 0
        self.known_cost_usd = 0.0
        self.provider_cost_usd: Dict[str, float] = {}
        self.unknown_cost_calls = 0
        self.accounting_failure: Optional[str] = None
        self.stop_reason: Optional[str] = None
        self.attempt_entries: list[Dict[str, Any]] = []
        self.entries: list[Dict[str, Any]] = []
        self.warnings: list[str] = []

    @staticmethod
    def _snapshot_nonnegative_integer(snapshot: Mapping[str, Any], field_name: str) -> int:
        value = snapshot.get(field_name, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(
                f"Invalid budget snapshot: {field_name} must be a nonnegative integer"
            )
        return value

    @staticmethod
    def _snapshot_nonnegative_number(snapshot: Mapping[str, Any], field_name: str) -> float:
        value = snapshot.get(field_name, 0.0)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ConfigError(
                f"Invalid budget snapshot: {field_name} must be a nonnegative finite number"
            )
        try:
            normalized = float(value)
        except (OverflowError, ValueError) as exc:
            raise ConfigError(
                f"Invalid budget snapshot: {field_name} must be a nonnegative finite number"
            ) from exc
        if not math.isfinite(normalized) or normalized < 0:
            raise ConfigError(
                f"Invalid budget snapshot: {field_name} must be a nonnegative finite number"
            )
        return normalized

    @staticmethod
    def _snapshot_optional_message(snapshot: Mapping[str, Any], field_name: str) -> Optional[str]:
        value = snapshot.get(field_name)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ConfigError(
                f"Invalid budget snapshot: {field_name} must be a nonempty string or null"
            )
        return value

    def restore(self, snapshot: Mapping[str, Any]) -> None:
        """Restore cumulative accounting when a matching run is resumed."""
        with self.lock:
            if not isinstance(snapshot, Mapping):
                raise ConfigError("Invalid budget snapshot: expected a JSON object")
            schema_version = snapshot.get("schema_version")
            if (
                not isinstance(schema_version, int)
                or isinstance(schema_version, bool)
                or schema_version != self.SNAPSHOT_SCHEMA_VERSION
            ):
                raise ConfigError(
                    "Unsupported budget snapshot schema; safe resume requires schema_version "
                    f"{self.SNAPSHOT_SCHEMA_VERSION}"
                )
            required_fields = {
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
            missing_fields = sorted(required_fields.difference(snapshot))
            if missing_fields:
                raise ConfigError(
                    "Invalid budget snapshot: missing required fields "
                    + ", ".join(missing_fields)
                )

            calls = self._snapshot_nonnegative_integer(snapshot, "calls")
            input_tokens = self._snapshot_nonnegative_integer(snapshot, "input_tokens")
            output_tokens = self._snapshot_nonnegative_integer(snapshot, "output_tokens")
            reasoning_tokens = self._snapshot_nonnegative_integer(snapshot, "reasoning_tokens")
            cached_tokens = self._snapshot_nonnegative_integer(snapshot, "cached_tokens")
            tool_calls = self._snapshot_nonnegative_integer(snapshot, "tool_calls")
            unknown_cost_calls = self._snapshot_nonnegative_integer(snapshot, "unknown_cost_calls")
            known_cost_usd = self._snapshot_nonnegative_number(snapshot, "known_cost_usd")
            restored_wall_seconds = self._snapshot_nonnegative_number(snapshot, "wall_seconds")

            attempts = self._snapshot_nonnegative_integer(snapshot, "attempts")
            if attempts != calls:
                raise ConfigError("Invalid budget snapshot: attempts must equal calls")
            attempt_entries = snapshot.get("attempt_entries")
            if not isinstance(attempt_entries, list) or any(
                not isinstance(attempt_entry, Mapping)
                for attempt_entry in attempt_entries
            ):
                raise ConfigError(
                    "Invalid budget snapshot: attempt_entries must be an array of objects"
                )
            if len(attempt_entries) != attempts:
                raise ConfigError(
                    "Invalid budget snapshot: attempt_entries length must equal attempts"
                )
            required_attempt_fields = {
                "attempt_index",
                "attempt_id",
                "stage",
                "seat",
                "invocation_sha256",
            }
            for expected_attempt_index, attempt_entry in enumerate(attempt_entries):
                if required_attempt_fields.difference(attempt_entry):
                    raise ConfigError(
                        f"Invalid budget snapshot: attempt entry {expected_attempt_index} "
                        "is missing required fields"
                    )
                attempt_index = attempt_entry["attempt_index"]
                if (
                    not isinstance(attempt_index, int)
                    or isinstance(attempt_index, bool)
                    or attempt_index != expected_attempt_index
                ):
                    raise ConfigError(
                        "Invalid budget snapshot: attempt_entries must be in zero-based "
                        "attempt_index order"
                    )
                for field_name in ("stage", "seat"):
                    if (
                        not isinstance(attempt_entry[field_name], str)
                        or not attempt_entry[field_name]
                    ):
                        raise ConfigError(
                            f"Invalid budget snapshot: attempt entry {attempt_index} "
                            f"{field_name} must be a nonempty string"
                        )
                invocation_sha256 = _validated_sha256(
                    attempt_entry["invocation_sha256"],
                    f"attempt_entries[{attempt_index}].invocation_sha256",
                )
                attempt_id = _validated_sha256(
                    attempt_entry["attempt_id"],
                    f"attempt_entries[{attempt_index}].attempt_id",
                )
                if attempt_id != attempt_receipt_id(invocation_sha256, attempt_index):
                    raise ConfigError(
                        f"Invalid budget snapshot: attempt entry {attempt_index} "
                        "attempt_id does not match its invocation"
                    )
            total_tokens = self._snapshot_nonnegative_integer(snapshot, "total_tokens")
            if total_tokens != input_tokens + output_tokens:
                raise ConfigError(
                    "Invalid budget snapshot: total_tokens must equal input_tokens plus output_tokens"
                )

            provider_cost = snapshot.get("provider_cost_usd", {})
            if not isinstance(provider_cost, Mapping):
                raise ConfigError("Invalid budget snapshot: provider_cost_usd must be an object")
            normalized_provider_cost: Dict[str, float] = {}
            for provider, value in provider_cost.items():
                if not isinstance(provider, str) or not provider:
                    raise ConfigError(
                        "Invalid budget snapshot: provider_cost_usd keys must be nonempty strings"
                    )
                normalized_provider_cost[provider] = self._snapshot_nonnegative_number(
                    {"provider_cost": value}, "provider_cost"
                )

            accounting_failure = self._snapshot_optional_message(snapshot, "accounting_failure")
            stop_reason = self._snapshot_optional_message(snapshot, "stop_reason")
            entries = snapshot.get("entries", [])
            if not isinstance(entries, list) or any(not isinstance(entry, Mapping) for entry in entries):
                raise ConfigError("Invalid budget snapshot: entries must be an array of objects")
            if len(entries) > calls:
                raise ConfigError("Invalid budget snapshot: entries cannot exceed calls")
            if unknown_cost_calls > len(entries):
                raise ConfigError(
                    "Invalid budget snapshot: unknown_cost_calls cannot exceed recorded entries"
                )

            recomputed_counters = {
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "tool_calls": 0,
            }
            recomputed_known_cost_usd = 0.0
            recomputed_provider_cost_usd: Dict[str, float] = {}
            recomputed_unknown_cost_calls = 0
            entry_accounting_failures: list[str] = []
            required_entry_fields = {
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
            required_usage_fields = {
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
            recorded_attempt_indices: set[int] = set()
            for entry_index, entry in enumerate(entries):
                missing_entry_fields = required_entry_fields.difference(entry)
                if missing_entry_fields:
                    raise ConfigError(
                        f"Invalid budget snapshot: entry {entry_index} is missing required fields"
                    )
                entry_attempt_index = entry["attempt_index"]
                if (
                    not isinstance(entry_attempt_index, int)
                    or isinstance(entry_attempt_index, bool)
                    or entry_attempt_index < 0
                    or entry_attempt_index >= len(attempt_entries)
                ):
                    raise ConfigError(
                        f"Invalid budget snapshot: entry {entry_index} attempt_index "
                        "must identify a reserved attempt"
                    )
                if entry_attempt_index in recorded_attempt_indices:
                    raise ConfigError(
                        f"Invalid budget snapshot: reserved attempt {entry_attempt_index} "
                        "has more than one recorded response"
                    )
                recorded_attempt_indices.add(entry_attempt_index)
                reserved_attempt = attempt_entries[entry_attempt_index]

                for field_name in (
                    "stage",
                    "seat",
                    "provider",
                    "requested_model",
                    "actual_model",
                ):
                    if not isinstance(entry[field_name], str) or not entry[field_name]:
                        raise ConfigError(
                            f"Invalid budget snapshot: entry {entry_index} {field_name} "
                            "must be a nonempty string"
                        )
                if (
                    entry["stage"] != reserved_attempt["stage"]
                    or entry["seat"] != reserved_attempt["seat"]
                ):
                    raise ConfigError(
                        f"Invalid budget snapshot: entry {entry_index} does not match "
                        "its reserved attempt stage and seat"
                    )
                invocation_sha256 = _validated_sha256(
                    entry["invocation_sha256"],
                    f"entries[{entry_index}].invocation_sha256",
                )
                attempt_id = _validated_sha256(
                    entry["attempt_id"],
                    f"entries[{entry_index}].attempt_id",
                )
                if (
                    invocation_sha256 != reserved_attempt["invocation_sha256"]
                    or attempt_id != reserved_attempt["attempt_id"]
                ):
                    raise ConfigError(
                        f"Invalid budget snapshot: entry {entry_index} receipt does not "
                        "match its reserved attempt"
                    )
                response_sha256 = _validated_sha256(
                    entry["response_sha256"],
                    f"entries[{entry_index}].response_sha256",
                )
                entry_id = _validated_sha256(
                    entry["entry_id"],
                    f"entries[{entry_index}].entry_id",
                )
                if entry_id != call_receipt_entry_id(
                    attempt_id,
                    invocation_sha256,
                    response_sha256,
                ):
                    raise ConfigError(
                        f"Invalid budget snapshot: entry {entry_index} entry_id does not "
                        "match its receipt"
                    )
                expected_response_artifact = f"responses/{entry_id}.json"
                if entry["response_artifact"] != expected_response_artifact:
                    raise ConfigError(
                        f"Invalid budget snapshot: entry {entry_index} response_artifact "
                        "does not match its entry_id"
                    )
                if not isinstance(entry["raw_status"], str) or not entry["raw_status"]:
                    raise ConfigError(
                        f"Invalid budget snapshot: entry {entry_index} raw_status "
                        "must be a nonempty string"
                    )
                request_id = entry["request_id"]
                if request_id is not None and (
                    not isinstance(request_id, str) or not request_id
                ):
                    raise ConfigError(
                        f"Invalid budget snapshot: entry {entry_index} request_id "
                        "must be a nonempty string or null"
                    )
                if not isinstance(entry["route"], Mapping):
                    raise ConfigError(
                        f"Invalid budget snapshot: entry {entry_index} route must be an object"
                    )
                self._snapshot_nonnegative_number(
                    {"latency_seconds": entry["latency_seconds"]}, "latency_seconds"
                )

                entry_usage = entry["usage"]
                if not isinstance(entry_usage, Mapping):
                    raise ConfigError(
                        f"Invalid budget snapshot: entry {entry_index} usage must be an object"
                    )
                if required_usage_fields.difference(entry_usage):
                    raise ConfigError(
                        f"Invalid budget snapshot: entry {entry_index} usage is missing required fields"
                    )
                entry_counters: Dict[str, int] = {}
                for counter_name in recomputed_counters:
                    entry_counters[counter_name] = self._snapshot_nonnegative_integer(
                        entry_usage, counter_name
                    )
                    recomputed_counters[counter_name] += entry_counters[counter_name]
                if entry_counters["cached_tokens"] > entry_counters["input_tokens"]:
                    raise ConfigError(
                        f"Invalid budget snapshot: entry {entry_index} cached_tokens "
                        "cannot exceed input_tokens"
                    )
                if entry_counters["reasoning_tokens"] > entry_counters["output_tokens"]:
                    raise ConfigError(
                        f"Invalid budget snapshot: entry {entry_index} reasoning_tokens "
                        "cannot exceed output_tokens"
                    )
                if not isinstance(entry_usage["unknown_cost_fail_closed"], bool):
                    raise ConfigError(
                        f"Invalid budget snapshot: entry {entry_index} "
                        "unknown_cost_fail_closed must be a boolean"
                    )
                for usage_boolean in (
                    "input_output_usage_complete",
                    "raw_usage_invalid",
                ):
                    if not isinstance(entry_usage[usage_boolean], bool):
                        raise ConfigError(
                            f"Invalid budget snapshot: entry {entry_index} "
                            f"{usage_boolean} must be a boolean"
                        )
                entry_accounting_error = self._snapshot_optional_message(
                    entry_usage, "accounting_error"
                )
                entry_requires_accounting_latch = (
                    not entry_usage["input_output_usage_complete"]
                    or entry_usage["raw_usage_invalid"]
                    or entry_usage["unknown_cost_fail_closed"]
                )

                entry_cost = entry_usage["cost_usd"]
                if entry_cost is None:
                    recomputed_unknown_cost_calls += 1
                    if self.config.get("unknown_cost_policy", "fail_closed") == "fail_closed":
                        entry_requires_accounting_latch = True
                else:
                    normalized_entry_cost = self._snapshot_nonnegative_number(
                        {"cost_usd": entry_cost}, "cost_usd"
                    )
                    provider = str(entry["provider"])
                    recomputed_known_cost_usd += normalized_entry_cost
                    recomputed_provider_cost_usd[provider] = (
                        recomputed_provider_cost_usd.get(provider, 0.0)
                        + normalized_entry_cost
                    )
                if entry_requires_accounting_latch and entry_accounting_error is None:
                    raise ConfigError(
                        f"Invalid budget snapshot: entry {entry_index} has an unlatched "
                        "usage integrity failure"
                    )
                if entry_accounting_error is not None:
                    entry_accounting_failures.append(entry_accounting_error)

            expected_counters = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "cached_tokens": cached_tokens,
                "tool_calls": tool_calls,
            }
            if recomputed_counters != expected_counters:
                raise ConfigError(
                    "Invalid budget snapshot: aggregate usage counters must match entries"
                )
            if recomputed_known_cost_usd != known_cost_usd:
                raise ConfigError(
                    "Invalid budget snapshot: known_cost_usd must match entries"
                )
            if recomputed_provider_cost_usd != normalized_provider_cost:
                raise ConfigError(
                    "Invalid budget snapshot: provider_cost_usd must match entries"
                )
            if recomputed_unknown_cost_calls != unknown_cost_calls:
                raise ConfigError(
                    "Invalid budget snapshot: unknown_cost_calls must match entries"
                )
            if entry_accounting_failures and accounting_failure != entry_accounting_failures[0]:
                raise ConfigError(
                    "Invalid budget snapshot: accounting_failure must match entry usage"
                )
            if not entry_accounting_failures and accounting_failure is not None:
                raise ConfigError(
                    "Invalid budget snapshot: accounting_failure has no matching entry usage"
                )

            warnings = snapshot.get("warnings", [])
            if not isinstance(warnings, list) or any(
                not isinstance(warning, str) or not warning for warning in warnings
            ):
                raise ConfigError(
                    "Invalid budget snapshot: warnings must be an array of nonempty strings"
                )

            # Copy untrusted containers before assignment as custom Mapping or
            # list subclasses may raise while being copied.
            try:
                restored_attempt_entries = copy.deepcopy(attempt_entries)
                restored_entries = copy.deepcopy(entries)
                restored_warnings = list(warnings)
            except Exception as exc:
                raise ConfigError(
                    "Invalid budget snapshot: accounting containers could not be safely copied"
                ) from exc

            # Assign only after every validation and fallible copy completes.
            self.calls = calls
            self.input_tokens = input_tokens
            self.output_tokens = output_tokens
            self.reasoning_tokens = reasoning_tokens
            self.cached_tokens = cached_tokens
            self.tool_calls = tool_calls
            self.known_cost_usd = known_cost_usd
            self.provider_cost_usd = normalized_provider_cost
            self.unknown_cost_calls = unknown_cost_calls
            self.accounting_failure = accounting_failure
            self.stop_reason = stop_reason
            self.attempt_entries = restored_attempt_entries
            self.entries = restored_entries
            self.warnings = restored_warnings
            self.restored_wall_seconds = restored_wall_seconds

    def _elapsed_wall_seconds(self) -> float:
        return self.restored_wall_seconds + (time.monotonic() - self.started)

    def _enforcement(self) -> str:
        configured = self.config.get("enforcement", "hard_stop")
        if configured in {"hard_stop", "approval_then_hard_stop", "warn_only"}:
            return str(configured)
        # Configuration validation should reject this. Fail closed if a tracker is
        # constructed directly with an invalid value.
        return "hard_stop"

    def _append_warning(self, warning: str) -> None:
        if warning not in self.warnings:
            self.warnings.append(warning)

    def _block_dispatch(self, reason: str) -> None:
        enforcement = self._enforcement()
        if enforcement == "warn_only":
            self._append_warning(reason)
            return
        if self.stop_reason is None:
            self.stop_reason = reason
        if enforcement == "approval_then_hard_stop":
            raise BudgetExceeded(
                f"{reason}; host approval and an explicit budget configuration change are required"
            )
        raise BudgetExceeded(reason)

    def _check_time_before_dispatch(self) -> None:
        limit = self.config.get("max_wall_seconds")
        if isinstance(limit, (int, float)) and self._elapsed_wall_seconds() >= float(limit):
            self._block_dispatch(f"Wall-time budget of {limit} seconds exhausted before dispatch")

    def _observed_usage(self) -> Dict[str, float]:
        return {
            # Provider usage APIs report cached tokens as an input-token detail
            # and reasoning tokens as an output-token detail. Adding either
            # breakdown again would double-count billed tokens.
            "total_tokens": float(self.input_tokens + self.output_tokens),
            "input_tokens": float(self.input_tokens),
            "output_tokens": float(self.output_tokens),
            "reasoning_tokens": float(self.reasoning_tokens),
            "tool_calls": float(self.tool_calls),
            "cost_usd": self.known_cost_usd,
        }

    def _check_observed_thresholds_before_dispatch(self) -> None:
        if self.accounting_failure is not None:
            # Provider accounting integrity failures are never downgraded by a
            # warn-only budget policy.
            raise BudgetExceeded(self.accounting_failure)
        if self.stop_reason is not None:
            self._block_dispatch(self.stop_reason)

        observed = self._observed_usage()
        configured_limits = (
            ("total_tokens", "max_total_tokens", "Total token"),
            ("input_tokens", "max_input_tokens", "Input token"),
            ("output_tokens", "max_output_tokens", "Output token"),
            ("reasoning_tokens", "max_reasoning_tokens", "Reasoning token"),
            ("tool_calls", "max_tool_calls", "Server-tool call"),
            ("cost_usd", "max_cost_usd", "Known cost"),
        )
        for observed_key, config_key, label in configured_limits:
            limit = self.config.get(config_key)
            if isinstance(limit, (int, float)) and observed[observed_key] >= float(limit):
                rendered_limit = f"${float(limit):.2f}" if config_key == "max_cost_usd" else str(limit)
                self._block_dispatch(
                    f"{label} threshold of {rendered_limit} exhausted before dispatch"
                )

        per_provider_limits = self.config.get("per_provider_max_cost_usd", {})
        if isinstance(per_provider_limits, Mapping):
            for provider, limit in per_provider_limits.items():
                observed_cost = self.provider_cost_usd.get(str(provider), 0.0)
                if isinstance(limit, (int, float)) and observed_cost >= float(limit):
                    self._block_dispatch(
                        f"Provider {provider} cost threshold of ${float(limit):.2f} exhausted before dispatch"
                    )

        if self.unknown_cost_calls and self.config.get("unknown_cost_policy", "fail_closed") == "fail_closed":
            self._block_dispatch("A prior model call has unknown cost; further dispatch is blocked")

    def reserve_attempt(
        self,
        stage: str,
        seat_name: str,
        invocation_sha256: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Atomically reserve one actual provider HTTP attempt before dispatch.

        Transport retries and model fallbacks must each call this method. A
        failed or timed-out attempt remains counted because it may have reached
        the provider and may still be billable.
        """

        with self.lock:
            if not isinstance(stage, str) or not stage:
                raise ConfigError("Invalid budget receipt: stage must be a nonempty string")
            if not isinstance(seat_name, str) or not seat_name:
                raise ConfigError("Invalid budget receipt: seat must be a nonempty string")
            self._check_time_before_dispatch()
            self._check_observed_thresholds_before_dispatch()
            max_calls = self.config.get("max_calls")
            if isinstance(max_calls, int) and self.calls >= max_calls:
                self._block_dispatch(f"Call-attempt budget of {max_calls} exhausted before seat {seat_name}")
            reserve_fraction = self.config.get("reserve_fraction_for_synthesis_and_gates", 0)
            if stage == "panel" and isinstance(max_calls, int) and isinstance(reserve_fraction, (int, float)):
                reserved_calls = math.ceil(max_calls * float(reserve_fraction))
                if self.calls >= max_calls - reserved_calls:
                    self._block_dispatch(
                        f"Panel attempt blocked to preserve {reserved_calls} attempts for synthesis and verification"
                    )
            attempt_index = self.calls
            if invocation_sha256 is None:
                invocation_sha256 = canonical_json_hash(
                    {
                        "schema_version": self.RECEIPT_SCHEMA_VERSION,
                        "kind": "budget_tracker_default_invocation",
                        "attempt_index": attempt_index,
                        "stage": stage,
                        "seat": seat_name,
                    }
                )
            else:
                invocation_sha256 = _validated_sha256(
                    invocation_sha256,
                    "invocation_sha256",
                )
            attempt_id = attempt_receipt_id(invocation_sha256, attempt_index)
            self.attempt_entries.append(
                {
                    "attempt_index": attempt_index,
                    "attempt_id": attempt_id,
                    "stage": stage,
                    "seat": seat_name,
                    "invocation_sha256": invocation_sha256,
                }
            )
            self.calls += 1
            return {"attempt_index": attempt_index, "attempt_id": attempt_id}

    def reserve_call(
        self,
        stage: str,
        seat_name: str,
        invocation_sha256: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compatibility alias; one call here means one provider HTTP attempt."""

        return self.reserve_attempt(stage, seat_name, invocation_sha256)

    @staticmethod
    def _nonnegative_usage_integer(value: Any, label: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise BudgetExceeded(f"Provider returned invalid {label} usage: expected a nonnegative integer")
        return value

    @staticmethod
    def _known_cost(value: Any) -> Optional[float]:
        if value is None:
            return None
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise BudgetExceeded("Provider returned invalid cost usage: expected a nonnegative finite number")
        try:
            normalized = float(value)
        except (OverflowError, ValueError) as exc:
            raise BudgetExceeded(
                "Provider returned invalid cost usage: expected a nonnegative finite number"
            ) from exc
        if not math.isfinite(normalized) or normalized < 0:
            raise BudgetExceeded("Provider returned invalid cost usage: expected a nonnegative finite number")
        return normalized

    def record(
        self,
        stage: str,
        seat_name: str,
        response: ModelResponse,
        *,
        attempt_index: Optional[int] = None,
        attempt_id: Optional[str] = None,
        invocation_sha256: Optional[str] = None,
        response_sha256: Optional[str] = None,
        response_artifact: Optional[str] = None,
    ) -> int:
        with self.lock:
            if not isinstance(stage, str) or not stage:
                raise ConfigError("Invalid budget receipt: stage must be a nonempty string")
            if not isinstance(seat_name, str) or not seat_name:
                raise ConfigError("Invalid budget receipt: seat must be a nonempty string")
            if attempt_index is not None and (
                not isinstance(attempt_index, int)
                or isinstance(attempt_index, bool)
                or attempt_index < 0
            ):
                raise ConfigError(
                    "Invalid budget receipt: attempt_index must be a nonnegative integer"
                )
            if attempt_id is not None:
                attempt_id = _validated_sha256(attempt_id, "attempt_id")
            if invocation_sha256 is not None:
                invocation_sha256 = _validated_sha256(
                    invocation_sha256,
                    "invocation_sha256",
                )

            recorded_attempt_indices = {
                entry["attempt_index"] for entry in self.entries
            }
            matching_attempts = [
                reserved_attempt
                for reserved_attempt in self.attempt_entries
                if reserved_attempt["attempt_index"] not in recorded_attempt_indices
                and reserved_attempt["stage"] == stage
                and reserved_attempt["seat"] == seat_name
                and (
                    attempt_index is None
                    or reserved_attempt["attempt_index"] == attempt_index
                )
                and (
                    attempt_id is None
                    or reserved_attempt["attempt_id"] == attempt_id
                )
                and (
                    invocation_sha256 is None
                    or reserved_attempt["invocation_sha256"] == invocation_sha256
                )
            ]
            if not matching_attempts:
                raise ConfigError(
                    "Invalid budget receipt: response does not match an unrecorded "
                    "reserved attempt"
                )
            reserved_attempt = matching_attempts[0]
            resolved_attempt_index = int(reserved_attempt["attempt_index"])
            resolved_attempt_id = str(reserved_attempt["attempt_id"])
            resolved_invocation_sha256 = str(reserved_attempt["invocation_sha256"])

            try:
                response_dict = response.to_dict()
                detached_response = copy.deepcopy(response_dict)
            except Exception as exc:
                raise ConfigError(
                    "Invalid budget receipt: response envelope could not be safely copied"
                ) from exc
            expected_response_fields = {
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
            if not isinstance(detached_response, dict) or set(detached_response) != expected_response_fields:
                raise ConfigError("Invalid budget receipt: response envelope schema mismatch")
            for field_name in (
                "provider",
                "requested_model",
                "actual_model",
                "raw_status",
            ):
                if (
                    not isinstance(detached_response[field_name], str)
                    or not detached_response[field_name]
                ):
                    raise ConfigError(
                        f"Invalid budget receipt: {field_name} must be a nonempty string"
                    )
            if not isinstance(detached_response["text"], str):
                raise ConfigError("Invalid budget receipt: text must be a string")
            request_id = detached_response["request_id"]
            if request_id is not None and (
                not isinstance(request_id, str) or not request_id
            ):
                raise ConfigError(
                    "Invalid budget receipt: request_id must be a nonempty string or null"
                )
            latency_seconds = detached_response["latency_seconds"]
            if (
                not isinstance(latency_seconds, (int, float))
                or isinstance(latency_seconds, bool)
                or not math.isfinite(float(latency_seconds))
                or float(latency_seconds) < 0
            ):
                raise ConfigError(
                    "Invalid budget receipt: latency_seconds must be a nonnegative finite number"
                )
            if not isinstance(detached_response["route"], Mapping):
                raise ConfigError("Invalid budget receipt: route must be an object")
            expected_usage_fields = {
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
            detached_usage = detached_response["usage"]
            if not isinstance(detached_usage, dict) or set(detached_usage) != expected_usage_fields:
                raise ConfigError("Invalid budget receipt: usage envelope schema mismatch")
            # Canonical encoding rejects non-JSON and non-finite values before
            # any accounting counter or entry can be committed.
            expected_response_sha256 = canonical_json_hash(detached_response)
            if response_sha256 is None:
                response_sha256 = expected_response_sha256
            else:
                response_sha256 = _validated_sha256(
                    response_sha256,
                    "response_sha256",
                )
                if response_sha256 != expected_response_sha256:
                    raise ConfigError(
                        "Invalid budget receipt: response_sha256 does not match the response"
                    )
            entry_id = call_receipt_entry_id(
                resolved_attempt_id,
                resolved_invocation_sha256,
                response_sha256,
            )
            expected_response_artifact = f"responses/{entry_id}.json"
            if response_artifact is None:
                response_artifact = expected_response_artifact
            elif response_artifact != expected_response_artifact:
                raise ConfigError(
                    "Invalid budget receipt: response_artifact does not match the entry_id"
                )
            usage = Usage(**detached_usage)
            response_accounting_failure: Optional[str] = None
            if usage.accounting_error is not None:
                if not isinstance(usage.accounting_error, str) or not usage.accounting_error:
                    response_accounting_failure = (
                        "Provider returned invalid usage accounting status"
                    )
                else:
                    response_accounting_failure = usage.accounting_error
            raw_usage_invalid = usage.raw_usage_invalid
            if not isinstance(raw_usage_invalid, bool):
                response_accounting_failure = "Provider returned invalid raw-usage status"
                raw_usage_invalid = True
            elif raw_usage_invalid and response_accounting_failure is None:
                response_accounting_failure = "Provider returned invalid raw usage"
            input_output_usage_complete = usage.input_output_usage_complete
            if not isinstance(input_output_usage_complete, bool):
                response_accounting_failure = (
                    "Provider returned invalid input/output usage completeness status"
                )
                input_output_usage_complete = False
                raw_usage_invalid = True
            elif not input_output_usage_complete and response_accounting_failure is None:
                response_accounting_failure = (
                    "Provider returned incomplete usage: input and output token counts are required"
                )
            unknown_cost_fail_closed = usage.unknown_cost_fail_closed
            if not isinstance(unknown_cost_fail_closed, bool):
                response_accounting_failure = (
                    response_accounting_failure
                    or "Provider returned invalid unknown-cost status"
                )
                unknown_cost_fail_closed = False
                raw_usage_invalid = True
            elif unknown_cost_fail_closed and usage.cost_usd is not None:
                response_accounting_failure = response_accounting_failure or (
                    "Provider returned inconsistent cost usage: a known cost cannot also "
                    "be marked unknown-cost fail-closed"
                )
                raw_usage_invalid = True

            normalized_usage_integers: Dict[str, int] = {}
            usage_integer_fields = (
                ("input_tokens", usage.input_tokens, "input token"),
                ("output_tokens", usage.output_tokens, "output token"),
                ("reasoning_tokens", usage.reasoning_tokens, "reasoning token"),
                ("cached_tokens", usage.cached_tokens, "cached token"),
                ("tool_calls", usage.tool_calls, "tool call"),
            )
            for field_name, field_value, field_label in usage_integer_fields:
                try:
                    normalized_usage_integers[field_name] = self._nonnegative_usage_integer(
                        field_value, field_label
                    )
                except BudgetExceeded as exc:
                    normalized_usage_integers[field_name] = 0
                    response_accounting_failure = response_accounting_failure or str(exc)
                    raw_usage_invalid = True
            try:
                cost_usd = self._known_cost(usage.cost_usd)
            except BudgetExceeded as exc:
                cost_usd = None
                response_accounting_failure = response_accounting_failure or str(exc)
                raw_usage_invalid = True

            input_tokens = normalized_usage_integers["input_tokens"]
            output_tokens = normalized_usage_integers["output_tokens"]
            reasoning_tokens = normalized_usage_integers["reasoning_tokens"]
            cached_tokens = normalized_usage_integers["cached_tokens"]
            tool_calls = normalized_usage_integers["tool_calls"]
            if cached_tokens > input_tokens:
                response_accounting_failure = response_accounting_failure or (
                    "Provider returned invalid cached token usage: cannot exceed input tokens"
                )
                raw_usage_invalid = True
                cached_tokens = input_tokens
            if reasoning_tokens > output_tokens:
                response_accounting_failure = response_accounting_failure or (
                    "Provider returned invalid reasoning token usage: cannot exceed output tokens"
                )
                raw_usage_invalid = True
                reasoning_tokens = output_tokens
            recorded_usage = Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                cached_tokens=cached_tokens,
                tool_calls=tool_calls,
                cost_usd=cost_usd,
                unknown_cost_fail_closed=unknown_cost_fail_closed,
                input_output_usage_complete=input_output_usage_complete,
                raw_usage_invalid=raw_usage_invalid,
                accounting_error=response_accounting_failure,
            )

            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.reasoning_tokens += reasoning_tokens
            self.cached_tokens += cached_tokens
            self.tool_calls += tool_calls
            unknown_cost_failure: Optional[str] = None
            if cost_usd is None:
                self.unknown_cost_calls += 1
                if recorded_usage.unknown_cost_fail_closed:
                    unknown_cost_failure = (
                        f"Seat {seat_name} exceeded its base-rate context threshold without configured long-context pricing"
                    )
                elif self.config.get("unknown_cost_policy", "fail_closed") == "fail_closed":
                    unknown_cost_failure = f"Seat {seat_name} did not report cost and has no configured pricing"
                else:
                    self._append_warning(
                        f"Seat {seat_name} did not report cost; dollar thresholds exclude this call"
                    )
            else:
                self.known_cost_usd += cost_usd
                provider_name = detached_response["provider"]
                self.provider_cost_usd[provider_name] = self.provider_cost_usd.get(provider_name, 0.0) + cost_usd
            if unknown_cost_failure is not None and response_accounting_failure is None:
                # A configured fail-closed unknown-cost outcome is an accounting
                # integrity latch, even when threshold enforcement is warn-only.
                response_accounting_failure = unknown_cost_failure
                recorded_usage.accounting_error = unknown_cost_failure
            recorded_entry_index = len(self.entries)
            self.entries.append(
                {
                    "attempt_index": resolved_attempt_index,
                    "attempt_id": resolved_attempt_id,
                    "entry_id": entry_id,
                    "invocation_sha256": resolved_invocation_sha256,
                    "response_sha256": response_sha256,
                    "response_artifact": response_artifact,
                    "stage": stage,
                    "seat": seat_name,
                    "provider": detached_response["provider"],
                    "requested_model": detached_response["requested_model"],
                    "actual_model": detached_response["actual_model"],
                    "request_id": detached_response["request_id"],
                    "route": detached_response["route"],
                    "raw_status": detached_response["raw_status"],
                    "latency_seconds": detached_response["latency_seconds"],
                    "usage": recorded_usage.to_dict(),
                }
            )
            if response_accounting_failure is not None:
                if self.accounting_failure is None:
                    self.accounting_failure = response_accounting_failure
                if self.stop_reason is None:
                    self.stop_reason = self.accounting_failure
                raise BudgetExceeded(self.accounting_failure)
            if unknown_cost_failure:
                if self.stop_reason is None:
                    self.stop_reason = unknown_cost_failure
                raise BudgetExceeded(unknown_cost_failure)
            total_tokens = self.input_tokens + self.output_tokens
            exceeded_limits: list[str] = []
            exhausted_limits: list[str] = []
            max_tokens = self.config.get("max_total_tokens")
            if isinstance(max_tokens, int):
                if total_tokens > max_tokens:
                    exceeded_limits.append(f"Total token threshold of {max_tokens} exceeded")
                elif total_tokens == max_tokens:
                    exhausted_limits.append(f"Total token threshold of {max_tokens} exhausted")
            token_limits = {
                "input": (self.input_tokens, self.config.get("max_input_tokens")),
                "output": (self.output_tokens, self.config.get("max_output_tokens")),
                "reasoning": (self.reasoning_tokens, self.config.get("max_reasoning_tokens")),
            }
            for token_kind, (actual, limit) in token_limits.items():
                if isinstance(limit, int):
                    if actual > limit:
                        exceeded_limits.append(f"{token_kind.capitalize()} token threshold of {limit} exceeded")
                    elif actual == limit:
                        exhausted_limits.append(f"{token_kind.capitalize()} token threshold of {limit} exhausted")
            max_tool_calls = self.config.get("max_tool_calls")
            if isinstance(max_tool_calls, int):
                if self.tool_calls > max_tool_calls:
                    exceeded_limits.append(f"Server-tool call threshold of {max_tool_calls} exceeded")
                elif self.tool_calls == max_tool_calls:
                    exhausted_limits.append(f"Server-tool call threshold of {max_tool_calls} exhausted")
            max_cost = self.config.get("max_cost_usd")
            if isinstance(max_cost, (int, float)):
                if self.known_cost_usd > float(max_cost):
                    exceeded_limits.append(f"Known cost threshold of ${float(max_cost):.2f} exceeded")
                elif self.known_cost_usd == float(max_cost):
                    exhausted_limits.append(f"Known cost threshold of ${float(max_cost):.2f} exhausted")
            per_provider_limits = self.config.get("per_provider_max_cost_usd", {})
            provider_name = detached_response["provider"]
            provider_limit = per_provider_limits.get(provider_name) if isinstance(per_provider_limits, Mapping) else None
            if isinstance(provider_limit, (int, float)):
                provider_cost = self.provider_cost_usd.get(provider_name, 0.0)
                if provider_cost > float(provider_limit):
                    exceeded_limits.append(
                        f"Provider {provider_name} cost threshold of ${float(provider_limit):.2f} exceeded"
                    )
                elif provider_cost == float(provider_limit):
                    exhausted_limits.append(
                        f"Provider {provider_name} cost threshold of ${float(provider_limit):.2f} exhausted"
                    )
            warning_fraction = self.config.get("warning_fraction")
            if isinstance(warning_fraction, (int, float)) and isinstance(max_cost, (int, float)):
                threshold = float(max_cost) * float(warning_fraction)
                warning = f"Known cost reached {float(warning_fraction):.0%} of the configured maximum"
                if self.known_cost_usd >= threshold and warning not in self.warnings:
                    self.warnings.append(warning)

            all_thresholds = exceeded_limits + exhausted_limits
            if all_thresholds:
                if self._enforcement() == "warn_only":
                    for threshold_message in all_thresholds:
                        self._append_warning(threshold_message)
                else:
                    if self.stop_reason is None:
                        self.stop_reason = all_thresholds[0]
                    if exceeded_limits:
                        if self._enforcement() == "approval_then_hard_stop":
                            raise BudgetExceeded(
                                "; ".join(exceeded_limits)
                                + "; host approval and an explicit budget configuration change are required"
                            )
                        raise BudgetExceeded("; ".join(exceeded_limits))

            wall_limit = self.config.get("max_wall_seconds")
            if isinstance(wall_limit, (int, float)) and self._elapsed_wall_seconds() >= float(wall_limit):
                wall_message = f"Wall-time threshold of {wall_limit} seconds exhausted after response"
                if self._enforcement() == "warn_only":
                    self._append_warning(wall_message)
                elif self.stop_reason is None:
                    self.stop_reason = wall_message
            return recorded_entry_index

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "schema_version": self.SNAPSHOT_SCHEMA_VERSION,
                "calls": self.calls,
                "attempts": self.calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "reasoning_tokens": self.reasoning_tokens,
                "cached_tokens": self.cached_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
                "tool_calls": self.tool_calls,
                # Persist full numeric precision: rounding a tiny charge or a
                # short elapsed interval down would reopen budget on resume.
                "known_cost_usd": self.known_cost_usd,
                "provider_cost_usd": dict(self.provider_cost_usd),
                "unknown_cost_calls": self.unknown_cost_calls,
                "accounting_failure": self.accounting_failure,
                "stop_reason": self.stop_reason,
                "wall_seconds": self._elapsed_wall_seconds(),
                "attempt_entries": copy.deepcopy(self.attempt_entries),
                "entries": copy.deepcopy(self.entries),
                "warnings": list(self.warnings),
            }
