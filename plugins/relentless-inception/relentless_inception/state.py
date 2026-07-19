"""Atomic run persistence, kill checks, and thread-safe budget accounting."""

from __future__ import annotations

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

from .config import canonical_hash, runtime_data_dir
from .errors import BudgetExceeded, ConfigError, RunAborted
from .types import ModelResponse


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


class RunStore:
    def __init__(self, task: str, config: Mapping[str, Any], run_id: Optional[str] = None) -> None:
        self.task_hash = text_hash(task)
        self.config_hash = canonical_hash(config)
        if run_id:
            if not run_id.replace("-", "").isalnum():
                raise ConfigError("run_id may contain only letters, digits, and hyphens")
            self.run_id = run_id
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            self.run_id = f"{stamp}-{self.task_hash[:10]}"
        self.directory = runtime_data_dir() / "runs" / self.run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(runtime_data_dir(), 0o700)
        os.chmod(runtime_data_dir() / "runs", 0o700)
        os.chmod(self.directory, 0o700)
        self.manifest_path = self.directory / "manifest.json"
        if self.manifest_path.exists():
            manifest = self.read_json("manifest.json")
            if manifest.get("task_hash") != self.task_hash or manifest.get("config_hash") != self.config_hash:
                raise ConfigError("Resume refused: run_id task/config hash does not match the current request")
        else:
            self.write_json(
                "manifest.json",
                {
                    "run_id": self.run_id,
                    "task_hash": self.task_hash,
                    "config_hash": self.config_hash,
                    "status": "running",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "stages": {},
                },
            )

    def path(self, relative_name: str) -> Path:
        candidate = (self.directory / relative_name).resolve()
        if self.directory.resolve() not in candidate.parents and candidate != self.directory.resolve():
            raise ConfigError("Artifact path escapes the run directory")
        return candidate

    def write_json(self, relative_name: str, value: Mapping[str, Any]) -> None:
        _atomic_json(self.path(relative_name), value)

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
    def __init__(self, budget_config: Mapping[str, Any]) -> None:
        self.config = dict(budget_config)
        self.started = time.monotonic()
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
        self.entries: list[Dict[str, Any]] = []
        self.warnings: list[str] = []

    def restore(self, snapshot: Mapping[str, Any]) -> None:
        """Restore cumulative accounting when a matching run is resumed."""
        with self.lock:
            self.calls = int(snapshot.get("calls", 0))
            self.input_tokens = int(snapshot.get("input_tokens", 0))
            self.output_tokens = int(snapshot.get("output_tokens", 0))
            self.reasoning_tokens = int(snapshot.get("reasoning_tokens", 0))
            self.cached_tokens = int(snapshot.get("cached_tokens", 0))
            self.tool_calls = int(snapshot.get("tool_calls", 0))
            self.known_cost_usd = float(snapshot.get("known_cost_usd", 0.0))
            provider_cost = snapshot.get("provider_cost_usd", {})
            self.provider_cost_usd = {str(key): float(value) for key, value in provider_cost.items()} if isinstance(provider_cost, Mapping) else {}
            self.unknown_cost_calls = int(snapshot.get("unknown_cost_calls", 0))
            entries = snapshot.get("entries", [])
            self.entries = list(entries) if isinstance(entries, list) else []
            warnings = snapshot.get("warnings", [])
            self.warnings = list(warnings) if isinstance(warnings, list) else []

    def _check_time(self) -> None:
        limit = self.config.get("max_wall_seconds")
        if isinstance(limit, (int, float)) and time.monotonic() - self.started >= float(limit):
            raise BudgetExceeded(f"Wall-time budget of {limit} seconds exhausted")

    def reserve_call(self, stage: str, seat_name: str) -> None:
        with self.lock:
            self._check_time()
            max_calls = self.config.get("max_calls")
            if isinstance(max_calls, int) and self.calls >= max_calls:
                raise BudgetExceeded(f"Call budget of {max_calls} exhausted before seat {seat_name}")
            reserve_fraction = self.config.get("reserve_fraction_for_synthesis_and_gates", 0)
            if stage == "panel" and isinstance(max_calls, int) and isinstance(reserve_fraction, (int, float)):
                reserved_calls = math.ceil(max_calls * float(reserve_fraction))
                if self.calls >= max_calls - reserved_calls:
                    raise BudgetExceeded(
                        f"Panel call blocked to preserve {reserved_calls} calls for synthesis and verification"
                    )
            self.calls += 1

    def record(self, stage: str, seat_name: str, response: ModelResponse) -> None:
        with self.lock:
            usage = response.usage
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens
            self.reasoning_tokens += usage.reasoning_tokens
            self.cached_tokens += usage.cached_tokens
            self.tool_calls += usage.tool_calls
            unknown_cost_failure: Optional[str] = None
            if usage.cost_usd is None:
                self.unknown_cost_calls += 1
                if usage.unknown_cost_fail_closed:
                    unknown_cost_failure = (
                        f"Seat {seat_name} exceeded its base-rate context threshold without configured long-context pricing"
                    )
                elif self.config.get("unknown_cost_policy", "warn") == "fail_closed":
                    unknown_cost_failure = f"Seat {seat_name} did not report cost and has no configured pricing"
            else:
                self.known_cost_usd += usage.cost_usd
                self.provider_cost_usd[response.provider] = self.provider_cost_usd.get(response.provider, 0.0) + usage.cost_usd
            self.entries.append(
                {
                    "stage": stage,
                    "seat": seat_name,
                    "provider": response.provider,
                    "requested_model": response.requested_model,
                    "actual_model": response.actual_model,
                    "request_id": response.request_id,
                    "route": response.route,
                    "latency_seconds": response.latency_seconds,
                    "usage": usage.to_dict(),
                }
            )
            if unknown_cost_failure:
                raise BudgetExceeded(unknown_cost_failure)
            total_tokens = self.input_tokens + self.output_tokens
            max_tokens = self.config.get("max_total_tokens")
            if isinstance(max_tokens, int) and total_tokens > max_tokens:
                raise BudgetExceeded(f"Token budget of {max_tokens} exceeded")
            token_limits = {
                "input": (self.input_tokens, self.config.get("max_input_tokens")),
                "output": (self.output_tokens, self.config.get("max_output_tokens")),
                "reasoning": (self.reasoning_tokens, self.config.get("max_reasoning_tokens")),
            }
            for token_kind, (actual, limit) in token_limits.items():
                if isinstance(limit, int) and actual > limit:
                    raise BudgetExceeded(f"{token_kind.capitalize()} token budget of {limit} exceeded")
            max_tool_calls = self.config.get("max_tool_calls")
            if isinstance(max_tool_calls, int) and self.tool_calls > max_tool_calls:
                raise BudgetExceeded(f"Server-tool call budget of {max_tool_calls} exceeded")
            max_cost = self.config.get("max_cost_usd")
            if isinstance(max_cost, (int, float)) and self.known_cost_usd > float(max_cost):
                raise BudgetExceeded(f"Known cost budget of ${float(max_cost):.2f} exceeded")
            per_provider_limits = self.config.get("per_provider_max_cost_usd", {})
            provider_limit = per_provider_limits.get(response.provider) if isinstance(per_provider_limits, Mapping) else None
            if isinstance(provider_limit, (int, float)) and self.provider_cost_usd.get(response.provider, 0.0) > float(provider_limit):
                raise BudgetExceeded(
                    f"Provider {response.provider} cost budget of ${float(provider_limit):.2f} exceeded"
                )
            warning_fraction = self.config.get("warning_fraction")
            if isinstance(warning_fraction, (int, float)) and isinstance(max_cost, (int, float)):
                threshold = float(max_cost) * float(warning_fraction)
                warning = f"Known cost reached {float(warning_fraction):.0%} of the configured maximum"
                if self.known_cost_usd >= threshold and warning not in self.warnings:
                    self.warnings.append(warning)
            self._check_time()

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "calls": self.calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "reasoning_tokens": self.reasoning_tokens,
                "cached_tokens": self.cached_tokens,
                "tool_calls": self.tool_calls,
                "known_cost_usd": round(self.known_cost_usd, 8),
                "provider_cost_usd": {key: round(value, 8) for key, value in self.provider_cost_usd.items()},
                "unknown_cost_calls": self.unknown_cost_calls,
                "wall_seconds": round(time.monotonic() - self.started, 3),
                "entries": list(self.entries),
                "warnings": list(self.warnings),
            }
