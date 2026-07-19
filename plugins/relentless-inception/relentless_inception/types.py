"""Small value types shared by providers, orchestration, and persistence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    tool_calls: int = 0
    cost_usd: Optional[float] = None
    unknown_cost_fail_closed: bool = False
    input_output_usage_complete: bool = True
    raw_usage_invalid: bool = False
    accounting_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ModelResponse:
    text: str
    provider: str
    requested_model: str
    actual_model: str
    usage: Usage = field(default_factory=Usage)
    latency_seconds: float = 0.0
    request_id: Optional[str] = None
    route: Dict[str, Any] = field(default_factory=dict)
    raw_status: str = "completed"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SeatResult:
    seat_name: str
    anonymous_label: str
    role: str
    status: str
    response: Optional[ModelResponse] = None
    response_evidence: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FusionResult:
    run_id: str
    task_hash: str
    config_hash: str
    status: str
    synthesis: str
    gate: Dict[str, Any]
    panel: List[Dict[str, Any]]
    judge: Dict[str, Any]
    ledger: Dict[str, Any]
    artifacts_dir: str
    execution_handoff: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
