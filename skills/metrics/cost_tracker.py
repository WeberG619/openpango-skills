"""
cost_tracker.py - Cost aggregation and time-series reporting.

Reads the append-only JSONL event log and builds aggregated read-models:
  - Per-skill totals (executions, cost, tokens, durations)
  - Per-agent totals
  - Time-bucketed series (hourly / daily / weekly / monthly)
  - Export to CSV or JSON

This is the read side of the metrics architecture, analogous to the SQLite
read-cache in the memory skill — but computed in-memory on demand so there
is no schema migration complexity.
"""
from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import AggregatedStats, EventType, Status
from .metrics_collector import MetricsCollector, _DEFAULT_METRICS_PATH

# Time bucket granularities supported by aggregate_by_period()
VALID_PERIODS = ("hourly", "daily", "weekly", "monthly")


class CostTracker:
    """
    Aggregates and reports on metrics stored in the JSONL event log.

    Instantiate with the same metrics_path you used for MetricsCollector
    (defaults to ~/.openclaw/workspace/metrics.jsonl).
    """

    def __init__(self, metrics_path: Optional[Path] = None) -> None:
        self._path = Path(metrics_path) if metrics_path else _DEFAULT_METRICS_PATH
        self._collector = MetricsCollector(metrics_path=self._path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_end_events(self) -> list:
        """
        Return only SKILL_END events — the canonical source of timing and cost.

        SKILL_START events only carry execution_id for correlation. Error events
        are also included so failure stats are accurate.
        """
        relevant = {EventType.SKILL_END.value, EventType.SKILL_ERROR.value}
        return [
            ev for ev in self._collector.read_events()
            if ev.get("event_type") in relevant
        ]

    def _parse_ts(self, ts_str: str) -> datetime:
        """Parse ISO8601 timestamp, always return UTC-aware datetime."""
        try:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return datetime.now(timezone.utc)

    def _bucket_key(self, dt: datetime, period: str) -> str:
        """Convert a datetime to its period bucket string."""
        if period == "hourly":
            return dt.strftime("%Y-%m-%d %H:00")
        elif period == "daily":
            return dt.strftime("%Y-%m-%d")
        elif period == "weekly":
            return dt.strftime("%G-W%V")
        elif period == "monthly":
            return dt.strftime("%Y-%m")
        raise ValueError(f"Unknown period: {period!r}. Choose from {VALID_PERIODS}")

    # ------------------------------------------------------------------
    # Core aggregations
    # ------------------------------------------------------------------

    def aggregate_by_skill(self, since: Optional[datetime] = None) -> dict:
        """
        Return per-skill AggregatedStats, optionally filtered by start time.

        Parameters
        ----------
        since:
            Only include events with timestamp >= this value.
            Pass None to include all events (default).
        """
        stats = {}

        for ev in self._load_end_events():
            ts = self._parse_ts(ev.get("timestamp", ""))
            if since and ts < since:
                continue

            skill = ev.get("skill_name", "unknown")
            if skill not in stats:
                stats[skill] = AggregatedStats(skill_name=skill)

            s = stats[skill]
            s.total_executions += 1

            if ev.get("status") == Status.SUCCESS.value:
                s.successful_executions += 1
            else:
                s.failed_executions += 1

            cost = float(ev.get("cost_usd", 0.0) or 0.0)
            s.total_cost_usd += cost

            tokens = int(ev.get("total_tokens", 0) or 0)
            s.total_tokens += tokens

            dur = float(ev.get("duration_ms", 0.0) or 0.0)
            s.total_duration_ms += dur
            if s.min_duration_ms is None or dur < s.min_duration_ms:
                s.min_duration_ms = dur
            if s.max_duration_ms is None or dur > s.max_duration_ms:
                s.max_duration_ms = dur

        return stats

    def aggregate_by_agent(self, since: Optional[datetime] = None) -> dict:
        """
        Return per-agent totals: executions, cost, tokens.
        """
        result = defaultdict(lambda: {
            "agent_id": "",
            "total_executions": 0,
            "successful_executions": 0,
            "failed_executions": 0,
            "total_cost_usd": 0.0,
            "total_tokens": 0,
        })

        for ev in self._load_end_events():
            ts = self._parse_ts(ev.get("timestamp", ""))
            if since and ts < since:
                continue

            agent = ev.get("agent_id", "unknown")
            r = result[agent]
            r["agent_id"] = agent
            r["total_executions"] += 1

            if ev.get("status") == Status.SUCCESS.value:
                r["successful_executions"] += 1
            else:
                r["failed_executions"] += 1

            r["total_cost_usd"] += float(ev.get("cost_usd", 0.0) or 0.0)
            r["total_tokens"] += int(ev.get("total_tokens", 0) or 0)

        # Round floats and compute success rate
        for r in result.values():
            r["total_cost_usd"] = round(r["total_cost_usd"], 6)
            n = r["total_executions"]
            r["success_rate_pct"] = round(r["successful_executions"] / n * 100, 2) if n else 0.0

        return dict(result)

    def aggregate_by_period(
        self,
        period: str = "daily",
        skill_name: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> list:
        """
        Return time-bucketed cost and execution counts.

        Parameters
        ----------
        period:
            Granularity — one of "hourly", "daily", "weekly", "monthly".
        skill_name:
            Filter to a single skill. None = all skills.
        since:
            Earliest timestamp to include.

        Returns a list of dicts sorted by bucket ascending:
            [{"period": "2025-01-15", "executions": 12, "cost_usd": 0.045, ...}]
        """
        if period not in VALID_PERIODS:
            raise ValueError(f"period must be one of {VALID_PERIODS}")

        buckets = defaultdict(lambda: {
            "executions": 0,
            "successful": 0,
            "failed": 0,
            "cost_usd": 0.0,
            "total_tokens": 0,
            "total_duration_ms": 0.0,
        })

        for ev in self._load_end_events():
            if skill_name and ev.get("skill_name") != skill_name:
                continue
            ts = self._parse_ts(ev.get("timestamp", ""))
            if since and ts < since:
                continue

            key = self._bucket_key(ts, period)
            b = buckets[key]
            b["executions"] += 1
            if ev.get("status") == Status.SUCCESS.value:
                b["successful"] += 1
            else:
                b["failed"] += 1
            b["cost_usd"] += float(ev.get("cost_usd", 0.0) or 0.0)
            b["total_tokens"] += int(ev.get("total_tokens", 0) or 0)
            b["total_duration_ms"] += float(ev.get("duration_ms", 0.0) or 0.0)

        rows = []
        for key in sorted(buckets.keys()):
            b = buckets[key]
            rows.append({
                "period": key,
                "executions": b["executions"],
                "successful": b["successful"],
                "failed": b["failed"],
                "cost_usd": round(b["cost_usd"], 6),
                "total_tokens": b["total_tokens"],
                "avg_duration_ms": round(
                    b["total_duration_ms"] / b["executions"], 2
                ) if b["executions"] else 0.0,
            })
        return rows

    def total_cost(self, since: Optional[datetime] = None) -> float:
        """Grand total cost across all skills and agents."""
        total = 0.0
        for ev in self._load_end_events():
            if since:
                ts = self._parse_ts(ev.get("timestamp", ""))
                if ts < since:
                    continue
            total += float(ev.get("cost_usd", 0.0) or 0.0)
        return round(total, 6)

    def total_executions(self, since: Optional[datetime] = None) -> int:
        count = 0
        for ev in self._load_end_events():
            if since:
                ts = self._parse_ts(ev.get("timestamp", ""))
                if ts < since:
                    continue
            count += 1
        return count

    # ------------------------------------------------------------------
    # Export helpers
    # ------------------------------------------------------------------

    def export_json(
        self,
        output_path: Optional[Path] = None,
        since: Optional[datetime] = None,
    ) -> str:
        """
        Export aggregated skill stats as JSON.

        Returns the JSON string. If output_path is given, also writes the file.
        """
        skill_stats = {
            k: v.to_dict() for k, v in self.aggregate_by_skill(since=since).items()
        }
        agent_stats = self.aggregate_by_agent(since=since)
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_cost_usd": self.total_cost(since=since),
            "total_executions": self.total_executions(since=since),
            "by_skill": skill_stats,
            "by_agent": agent_stats,
        }
        text = json.dumps(payload, indent=2)
        if output_path:
            Path(output_path).write_text(text, encoding="utf-8")
        return text

    def export_csv(
        self,
        output_path: Optional[Path] = None,
        since: Optional[datetime] = None,
    ) -> str:
        """
        Export per-skill stats as CSV.

        Returns the CSV string. If output_path is given, also writes the file.
        """
        stats = self.aggregate_by_skill(since=since)
        header = [
            "skill_name", "total_executions", "successful_executions",
            "failed_executions", "success_rate_pct", "total_cost_usd",
            "avg_cost_usd", "total_tokens", "avg_duration_ms",
            "min_duration_ms", "max_duration_ms",
        ]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=header)
        writer.writeheader()
        if stats:
            writer.writerows([s.to_dict() for s in stats.values()])
        text = buf.getvalue()

        if output_path:
            Path(output_path).write_text(text, encoding="utf-8")
        return text


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="cost_tracker — aggregate and export metrics",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_skill = sub.add_parser("by-skill", help="Aggregate costs per skill")
    p_skill.add_argument("--since", help="ISO8601 start date (e.g. 2025-01-01)")
    p_skill.add_argument("--json", action="store_true", dest="as_json")

    p_agent = sub.add_parser("by-agent", help="Aggregate costs per agent")
    p_agent.add_argument("--since")
    p_agent.add_argument("--json", action="store_true", dest="as_json")

    p_period = sub.add_parser("by-period", help="Time-series aggregation")
    p_period.add_argument("--period", choices=VALID_PERIODS, default="daily")
    p_period.add_argument("--skill")
    p_period.add_argument("--since")
    p_period.add_argument("--json", action="store_true", dest="as_json")

    p_ej = sub.add_parser("export-json", help="Export full stats as JSON")
    p_ej.add_argument("--out")
    p_ej.add_argument("--since")

    p_ec = sub.add_parser("export-csv", help="Export per-skill stats as CSV")
    p_ec.add_argument("--out")
    p_ec.add_argument("--since")

    args = parser.parse_args()
    tracker = CostTracker()

    def parse_since(s):
        if not s:
            return None
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    since = parse_since(getattr(args, "since", None))

    if args.cmd == "by-skill":
        stats = tracker.aggregate_by_skill(since=since)
        if args.as_json:
            print(json.dumps({k: v.to_dict() for k, v in stats.items()}, indent=2))
        else:
            for name, s in stats.items():
                print(f"{name}: {s.total_executions} execs, ${s.total_cost_usd:.6f}, "
                      f"{s.success_rate:.1f}% ok, {s.avg_duration_ms:.0f}ms avg")

    elif args.cmd == "by-agent":
        stats = tracker.aggregate_by_agent(since=since)
        if args.as_json:
            print(json.dumps(stats, indent=2))
        else:
            for agent, d in stats.items():
                print(f"{agent}: {d['total_executions']} execs, ${d['total_cost_usd']:.6f}")

    elif args.cmd == "by-period":
        rows = tracker.aggregate_by_period(
            period=args.period,
            skill_name=getattr(args, "skill", None),
            since=since,
        )
        if args.as_json:
            print(json.dumps(rows, indent=2))
        else:
            for r in rows:
                print(f"{r['period']}: {r['executions']} execs, ${r['cost_usd']:.6f}")

    elif args.cmd == "export-json":
        out = getattr(args, "out", None)
        text = tracker.export_json(output_path=Path(out) if out else None, since=since)
        if not out:
            print(text)

    elif args.cmd == "export-csv":
        out = getattr(args, "out", None)
        text = tracker.export_csv(output_path=Path(out) if out else None, since=since)
        if not out:
            print(text)


if __name__ == "__main__":
    main()
