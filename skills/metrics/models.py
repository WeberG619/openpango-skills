"""
models.py - Data models for metrics events.

Uses dataclasses for structured, type-safe metric records.
All models serialize to/from plain dicts for JSONL storage.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class EventType(str, Enum):
    """All possible metric event types."""
    SKILL_START = "skill_start"
    SKILL_END = "skill_end"
    SKILL_ERROR = "skill_error"
    COST_UPDATE = "cost_update"
    BUDGET_ALERT = "budget_alert"


class Status(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    IN_PROGRESS = "in_progress"


@dataclass
class MetricEvent:
    """
    Base immutable event record stored in the JSONL log.

    Every state change is an appended event — never mutated in place.
    This matches the Beads event-sourced architecture used by the memory skill.

    All fields have defaults so that subclasses can override event_type/status
    without triggering the Python 3.10 "non-default argument follows default"
    error in dataclass inheritance.
    """
    # Required in practice — defaulting to "" lets subclasses set per-type defaults.
    event_type: str = ""
    skill_name: str = ""
    agent_id: str = ""
    execution_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # Timing
    duration_ms: Optional[float] = None

    # Token / cost tracking
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0

    # Status
    status: str = Status.IN_PROGRESS.value
    error_message: Optional[str] = None

    # Arbitrary key-value metadata (model name, provider, etc.)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MetricEvent":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class SkillStartEvent(MetricEvent):
    """Emitted when a skill begins executing."""
    event_type: str = EventType.SKILL_START.value
    status: str = Status.IN_PROGRESS.value


@dataclass
class SkillEndEvent(MetricEvent):
    """Emitted when a skill completes (success or failure)."""
    event_type: str = EventType.SKILL_END.value
    status: str = Status.SUCCESS.value


@dataclass
class SkillErrorEvent(MetricEvent):
    """Emitted on unhandled exceptions during skill execution."""
    event_type: str = EventType.SKILL_ERROR.value
    status: str = Status.FAILURE.value


@dataclass
class BudgetAlertEvent(MetricEvent):
    """Emitted when accumulated cost exceeds a configured threshold."""
    event_type: str = EventType.BUDGET_ALERT.value
    threshold_usd: float = 0.0
    accumulated_usd: float = 0.0
    status: str = Status.SUCCESS.value


@dataclass
class AggregatedStats:
    """
    Read-model computed from the JSONL event log.

    Not stored — rebuilt on demand from events (just like the SQLite cache
    in the memory skill).
    """
    skill_name: str
    total_executions: int = 0
    successful_executions: int = 0
    failed_executions: int = 0
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    total_duration_ms: float = 0.0
    min_duration_ms: Optional[float] = None
    max_duration_ms: Optional[float] = None

    @property
    def success_rate(self) -> float:
        if self.total_executions == 0:
            return 0.0
        return self.successful_executions / self.total_executions * 100

    @property
    def avg_duration_ms(self) -> float:
        if self.total_executions == 0:
            return 0.0
        return self.total_duration_ms / self.total_executions

    @property
    def avg_cost_usd(self) -> float:
        if self.total_executions == 0:
            return 0.0
        return self.total_cost_usd / self.total_executions

    def to_dict(self) -> dict:
        return {
            "skill_name": self.skill_name,
            "total_executions": self.total_executions,
            "successful_executions": self.successful_executions,
            "failed_executions": self.failed_executions,
            "success_rate_pct": round(self.success_rate, 2),
            "total_cost_usd": round(self.total_cost_usd, 6),
            "avg_cost_usd": round(self.avg_cost_usd, 6),
            "total_tokens": self.total_tokens,
            "avg_duration_ms": round(self.avg_duration_ms, 2),
            "min_duration_ms": round(self.min_duration_ms, 2) if self.min_duration_ms is not None else None,
            "max_duration_ms": round(self.max_duration_ms, 2) if self.max_duration_ms is not None else None,
        }
