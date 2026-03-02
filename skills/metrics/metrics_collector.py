"""
metrics_collector.py - Thread-safe event recording for skill executions.

Records start, end, cost, and token usage events to the append-only JSONL
log at ~/.openclaw/workspace/metrics.jsonl. This is the write side of the
event-sourced metrics architecture.

Usage as a context manager:
    from skills.metrics.metrics_collector import MetricsCollector

    collector = MetricsCollector()
    with collector.track("my_skill", agent_id="agent-1") as ctx:
        result = do_work()
        ctx.add_tokens(input=512, output=256, cost_usd=0.0018)

Usage as a decorator:
    @collector.instrument(agent_id="agent-1")
    def my_skill_function(*args, **kwargs):
        ...
"""
from __future__ import annotations

import functools
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Generator, Optional

from .models import (
    AggregatedStats,
    BudgetAlertEvent,
    EventType,
    MetricEvent,
    SkillEndEvent,
    SkillErrorEvent,
    SkillStartEvent,
    Status,
)

# ---------------------------------------------------------------------------
# Storage location — matches the spec: ~/.openclaw/workspace/metrics.jsonl
# ---------------------------------------------------------------------------
_DEFAULT_METRICS_PATH = Path.home() / ".openclaw" / "workspace" / "metrics.jsonl"


class _ExecutionContext:
    """
    Mutable context object injected into the `with` block.

    Callers use this to record token counts and costs that are only known
    AFTER the work completes.
    """

    def __init__(self) -> None:
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.cost_usd: float = 0.0
        self.extra_metadata: dict = {}

    def add_tokens(
        self,
        input: int = 0,
        output: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Record token usage and associated API cost."""
        self.input_tokens += input
        self.output_tokens += output
        self.cost_usd += cost_usd

    def set_metadata(self, **kwargs) -> None:
        """Attach arbitrary key-value pairs to the end event (model, provider, etc.)."""
        self.extra_metadata.update(kwargs)


class MetricsCollector:
    """
    Thread-safe, append-only event recorder.

    All public methods are safe to call from multiple threads simultaneously.
    A single reentrant lock guards the JSONL write to prevent interleaved lines.

    Parameters
    ----------
    metrics_path:
        Override the default storage path (useful in tests).
    budget_alert_usd:
        If the rolling total cost across ALL skills exceeds this value, a
        BUDGET_ALERT event is appended and a warning is printed to stderr.
        Set to None (default) to disable budget alerts.
    """

    def __init__(
        self,
        metrics_path: Optional[Path] = None,
        budget_alert_usd: Optional[float] = None,
    ) -> None:
        self._path = Path(metrics_path) if metrics_path else _DEFAULT_METRICS_PATH
        self._lock = threading.RLock()
        self._budget_usd = budget_alert_usd
        self._session_cost: float = 0.0  # In-memory running total for budget alerts

        # Ensure parent directory exists on first use
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Core write primitive
    # ------------------------------------------------------------------

    def _append(self, event: MetricEvent) -> None:
        """Append one event as a single JSON line. Thread-safe."""
        with self._lock:
            line = json.dumps(event.to_dict(), separators=(",", ":")) + "\n"
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_start(
        self,
        skill_name: str,
        agent_id: str,
        execution_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> SkillStartEvent:
        """
        Emit a SKILL_START event.

        Returns the event so callers can pass execution_id to record_end.
        """
        ev = SkillStartEvent(
            skill_name=skill_name,
            agent_id=agent_id,
            metadata=metadata or {},
        )
        if execution_id:
            ev.execution_id = execution_id
        self._append(ev)
        return ev

    def record_end(
        self,
        skill_name: str,
        agent_id: str,
        execution_id: str,
        duration_ms: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        status: str = Status.SUCCESS.value,
        metadata: Optional[dict] = None,
    ) -> SkillEndEvent:
        """
        Emit a SKILL_END event with timing, token usage, and cost.
        Also checks/updates the budget alert threshold.
        """
        ev = SkillEndEvent(
            skill_name=skill_name,
            agent_id=agent_id,
            execution_id=execution_id,
            duration_ms=round(duration_ms, 3),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost_usd=round(cost_usd, 8),
            status=status,
            metadata=metadata or {},
        )
        self._append(ev)

        # Budget alert check
        if self._budget_usd is not None:
            with self._lock:
                self._session_cost += cost_usd
                if self._session_cost >= self._budget_usd:
                    self._emit_budget_alert(
                        skill_name=skill_name,
                        agent_id=agent_id,
                        threshold=self._budget_usd,
                        accumulated=self._session_cost,
                    )

        return ev

    def record_error(
        self,
        skill_name: str,
        agent_id: str,
        execution_id: str,
        duration_ms: float,
        error_message: str,
        metadata: Optional[dict] = None,
    ) -> SkillErrorEvent:
        """Emit a SKILL_ERROR event for unhandled exceptions."""
        ev = SkillErrorEvent(
            skill_name=skill_name,
            agent_id=agent_id,
            execution_id=execution_id,
            duration_ms=round(duration_ms, 3),
            error_message=str(error_message)[:2048],  # Cap error message length
            metadata=metadata or {},
        )
        self._append(ev)
        return ev

    def _emit_budget_alert(
        self,
        skill_name: str,
        agent_id: str,
        threshold: float,
        accumulated: float,
    ) -> None:
        import sys
        alert_ev = BudgetAlertEvent(
            skill_name=skill_name,
            agent_id=agent_id,
            threshold_usd=round(threshold, 6),
            accumulated_usd=round(accumulated, 6),
        )
        self._append(alert_ev)
        print(
            f"\033[33m[metrics] BUDGET ALERT: ${accumulated:.4f} spent "
            f"(threshold ${threshold:.4f})\033[0m",
            file=sys.stderr,
        )

    # ------------------------------------------------------------------
    # Context manager: with collector.track(...) as ctx:
    # ------------------------------------------------------------------

    @contextmanager
    def track(
        self,
        skill_name: str,
        agent_id: str = "unknown",
        metadata: Optional[dict] = None,
    ) -> Generator[_ExecutionContext, None, None]:
        """
        Context manager that automatically records start/end/error events.

        Example:
            with collector.track("researcher", agent_id="agent-1") as ctx:
                data = fetch_data()
                ctx.add_tokens(input=300, output=150, cost_usd=0.0009)
        """
        start_ev = self.record_start(skill_name, agent_id, metadata=metadata)
        ctx = _ExecutionContext()
        t0 = time.perf_counter()
        try:
            yield ctx
            duration_ms = (time.perf_counter() - t0) * 1000
            merged_meta = dict(metadata or {})
            merged_meta.update(ctx.extra_metadata)
            self.record_end(
                skill_name=skill_name,
                agent_id=agent_id,
                execution_id=start_ev.execution_id,
                duration_ms=duration_ms,
                input_tokens=ctx.input_tokens,
                output_tokens=ctx.output_tokens,
                cost_usd=ctx.cost_usd,
                status=Status.SUCCESS.value,
                metadata=merged_meta,
            )
        except Exception as exc:
            duration_ms = (time.perf_counter() - t0) * 1000
            self.record_error(
                skill_name=skill_name,
                agent_id=agent_id,
                execution_id=start_ev.execution_id,
                duration_ms=duration_ms,
                error_message=str(exc),
                metadata=metadata or {},
            )
            raise

    # ------------------------------------------------------------------
    # Decorator: @collector.instrument(...)
    # ------------------------------------------------------------------

    def instrument(
        self,
        skill_name: Optional[str] = None,
        agent_id: str = "unknown",
        metadata: Optional[dict] = None,
    ) -> Callable:
        """
        Decorator factory that auto-instruments any callable.

        Example:
            @collector.instrument(agent_id="planner-1")
            def run_planner(task: str) -> str:
                ...

        The skill_name defaults to the decorated function's __name__.
        """
        def decorator(fn: Callable) -> Callable:
            name = skill_name or fn.__name__

            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                with self.track(name, agent_id=agent_id, metadata=metadata):
                    return fn(*args, **kwargs)

            return wrapper
        return decorator

    # ------------------------------------------------------------------
    # Simple stats helper (used by the CLI; full aggregation is in cost_tracker)
    # ------------------------------------------------------------------

    def read_events(self) -> list:
        """Return all events from the JSONL log as a list of dicts."""
        if not self._path.exists():
            return []
        events = []
        with open(self._path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events


# ---------------------------------------------------------------------------
# CLI entry point — quick raw event dump
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="metrics_collector — low-level event recorder",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # record-start
    p_start = sub.add_parser("record-start", help="Emit a SKILL_START event")
    p_start.add_argument("skill_name")
    p_start.add_argument("--agent", default="cli")
    p_start.add_argument("--meta", nargs="*", metavar="KEY=VAL",
                         help="Extra metadata as KEY=VAL pairs")

    # record-end
    p_end = sub.add_parser("record-end", help="Emit a SKILL_END event")
    p_end.add_argument("skill_name")
    p_end.add_argument("execution_id")
    p_end.add_argument("--agent", default="cli")
    p_end.add_argument("--duration-ms", type=float, default=0.0)
    p_end.add_argument("--input-tokens", type=int, default=0)
    p_end.add_argument("--output-tokens", type=int, default=0)
    p_end.add_argument("--cost-usd", type=float, default=0.0)
    p_end.add_argument("--status", default="success")

    # dump
    sub.add_parser("dump", help="Print all raw events as JSON")

    args = parser.parse_args()
    collector = MetricsCollector()

    def parse_meta(pairs):
        if not pairs:
            return {}
        meta = {}
        for pair in pairs:
            if "=" in pair:
                k, v = pair.split("=", 1)
                meta[k.strip()] = v.strip()
        return meta

    if args.cmd == "record-start":
        ev = collector.record_start(
            skill_name=args.skill_name,
            agent_id=args.agent,
            metadata=parse_meta(getattr(args, "meta", None)),
        )
        print(json.dumps({"execution_id": ev.execution_id, "timestamp": ev.timestamp}))

    elif args.cmd == "record-end":
        collector.record_end(
            skill_name=args.skill_name,
            agent_id=args.agent,
            execution_id=args.execution_id,
            duration_ms=args.duration_ms,
            input_tokens=args.input_tokens,
            output_tokens=args.output_tokens,
            cost_usd=args.cost_usd,
            status=args.status,
        )
        print("OK")

    elif args.cmd == "dump":
        for ev in collector.read_events():
            print(json.dumps(ev))


if __name__ == "__main__":
    main()
