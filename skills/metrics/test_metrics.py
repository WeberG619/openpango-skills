"""
test_metrics.py - Comprehensive test suite for the metrics skill.

Covers:
  - MetricEvent model serialization round-trips
  - MetricsCollector: thread-safety, context manager, decorator
  - CostTracker: per-skill, per-agent, time-series aggregations
  - CostTracker: CSV/JSON export
  - Dashboard: renders without crashing on empty + populated data
  - Budget alert triggering
  - Edge cases: empty log, malformed lines, missing fields

Run with:
    python3 -m pytest skills/metrics/test_metrics.py -v
or:
    python3 skills/metrics/test_metrics.py
"""
from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

# Make the skill importable without an editable install
_SKILLS_ROOT = Path(__file__).parent.parent.parent
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

from skills.metrics.models import (
    AggregatedStats,
    BudgetAlertEvent,
    EventType,
    MetricEvent,
    SkillEndEvent,
    SkillErrorEvent,
    SkillStartEvent,
    Status,
)
from skills.metrics.metrics_collector import MetricsCollector
from skills.metrics.cost_tracker import CostTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_collector():
    """Return a MetricsCollector backed by a temp file, and the path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
    tmp.close()
    path = Path(tmp.name)
    return MetricsCollector(metrics_path=path), path


def _tmp_tracker(events):
    """Write events to a temp JSONL file and return a CostTracker for it."""
    tmp = tempfile.NamedTemporaryFile(
        suffix=".jsonl", delete=False, mode="w", encoding="utf-8"
    )
    for ev in events:
        tmp.write(json.dumps(ev) + "\n")
    tmp.close()
    path = Path(tmp.name)
    return CostTracker(metrics_path=path), path


# ---------------------------------------------------------------------------
# 1. Model tests
# ---------------------------------------------------------------------------

class TestMetricEventModel(unittest.TestCase):

    def test_to_dict_round_trip(self):
        ev = SkillStartEvent(skill_name="researcher", agent_id="agent-1")
        d = ev.to_dict()
        self.assertEqual(d["skill_name"], "researcher")
        self.assertEqual(d["event_type"], EventType.SKILL_START.value)
        self.assertEqual(d["status"], Status.IN_PROGRESS.value)

    def test_from_dict_ignores_unknown_keys(self):
        """from_dict must not crash on extra keys (forward-compat)."""
        d = {
            "event_type": "skill_end",
            "skill_name": "coder",
            "agent_id": "a1",
            "execution_id": "eid-1",
            "timestamp": "2025-01-01T00:00:00+00:00",
            "duration_ms": 42.0,
            "input_tokens": 10,
            "output_tokens": 20,
            "total_tokens": 30,
            "cost_usd": 0.001,
            "status": "success",
            "error_message": None,
            "metadata": {},
            "UNKNOWN_FUTURE_FIELD": "some value",
        }
        ev = MetricEvent.from_dict(d)
        self.assertEqual(ev.skill_name, "coder")

    def test_aggregated_stats_properties(self):
        s = AggregatedStats(skill_name="planner")
        s.total_executions = 10
        s.successful_executions = 8
        s.failed_executions = 2
        s.total_cost_usd = 0.05
        s.total_duration_ms = 5000.0
        s.min_duration_ms = 100.0
        s.max_duration_ms = 1200.0

        self.assertAlmostEqual(s.success_rate, 80.0)
        self.assertAlmostEqual(s.avg_duration_ms, 500.0)
        self.assertAlmostEqual(s.avg_cost_usd, 0.005)

    def test_aggregated_stats_zero_division(self):
        s = AggregatedStats(skill_name="empty")
        self.assertEqual(s.success_rate, 0.0)
        self.assertEqual(s.avg_duration_ms, 0.0)
        self.assertEqual(s.avg_cost_usd, 0.0)

    def test_aggregated_stats_to_dict(self):
        s = AggregatedStats(skill_name="test")
        s.total_executions = 5
        s.successful_executions = 5
        s.total_cost_usd = 0.0025
        s.total_duration_ms = 2500.0
        s.min_duration_ms = 400.0
        s.max_duration_ms = 700.0
        d = s.to_dict()
        self.assertIn("skill_name", d)
        self.assertIn("success_rate_pct", d)
        self.assertEqual(d["success_rate_pct"], 100.0)


# ---------------------------------------------------------------------------
# 2. MetricsCollector tests
# ---------------------------------------------------------------------------

class TestMetricsCollector(unittest.TestCase):

    def test_record_start_appends_event(self):
        collector, path = _tmp_collector()
        try:
            ev = collector.record_start("browser", "agent-x")
            events = collector.read_events()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], EventType.SKILL_START.value)
            self.assertEqual(events[0]["skill_name"], "browser")
            self.assertEqual(events[0]["agent_id"], "agent-x")
            self.assertIn("execution_id", events[0])
        finally:
            path.unlink(missing_ok=True)

    def test_record_end_appends_event(self):
        collector, path = _tmp_collector()
        try:
            start_ev = collector.record_start("coder", "agent-1")
            collector.record_end(
                skill_name="coder",
                agent_id="agent-1",
                execution_id=start_ev.execution_id,
                duration_ms=123.45,
                input_tokens=100,
                output_tokens=50,
                cost_usd=0.0015,
                status=Status.SUCCESS.value,
            )
            events = collector.read_events()
            self.assertEqual(len(events), 2)
            end_ev = events[1]
            self.assertEqual(end_ev["event_type"], EventType.SKILL_END.value)
            self.assertAlmostEqual(end_ev["duration_ms"], 123.45, places=1)
            self.assertEqual(end_ev["total_tokens"], 150)
            self.assertAlmostEqual(end_ev["cost_usd"], 0.0015, places=6)
        finally:
            path.unlink(missing_ok=True)

    def test_record_error_appends_event(self):
        collector, path = _tmp_collector()
        try:
            start_ev = collector.record_start("researcher", "agent-2")
            collector.record_error(
                skill_name="researcher",
                agent_id="agent-2",
                execution_id=start_ev.execution_id,
                duration_ms=50.0,
                error_message="ConnectionTimeout",
            )
            events = collector.read_events()
            err_ev = events[-1]
            self.assertEqual(err_ev["event_type"], EventType.SKILL_ERROR.value)
            self.assertEqual(err_ev["status"], Status.FAILURE.value)
            self.assertIn("ConnectionTimeout", err_ev["error_message"])
        finally:
            path.unlink(missing_ok=True)

    def test_context_manager_success(self):
        collector, path = _tmp_collector()
        try:
            with collector.track("planner", agent_id="agent-3") as ctx:
                ctx.add_tokens(input=200, output=100, cost_usd=0.0009)
                ctx.set_metadata(model="gpt-4o")

            events = collector.read_events()
            self.assertEqual(len(events), 2)

            start_e = events[0]
            end_e = events[1]
            self.assertEqual(start_e["event_type"], EventType.SKILL_START.value)
            self.assertEqual(end_e["event_type"], EventType.SKILL_END.value)
            self.assertEqual(end_e["status"], Status.SUCCESS.value)
            self.assertEqual(end_e["input_tokens"], 200)
            self.assertEqual(end_e["output_tokens"], 100)
            self.assertAlmostEqual(end_e["cost_usd"], 0.0009, places=6)
            self.assertIn("model", end_e["metadata"])
        finally:
            path.unlink(missing_ok=True)

    def test_context_manager_exception_emits_error(self):
        collector, path = _tmp_collector()
        try:
            with self.assertRaises(ValueError):
                with collector.track("failer", agent_id="agent-4"):
                    raise ValueError("something went wrong")

            events = collector.read_events()
            self.assertEqual(len(events), 2)
            err_e = events[-1]
            self.assertEqual(err_e["event_type"], EventType.SKILL_ERROR.value)
            self.assertIn("something went wrong", err_e["error_message"])
        finally:
            path.unlink(missing_ok=True)

    def test_decorator(self):
        collector, path = _tmp_collector()
        try:
            @collector.instrument(skill_name="decorated_skill", agent_id="agent-5")
            def my_function(x):
                return x * 2

            result = my_function(21)
            self.assertEqual(result, 42)

            events = collector.read_events()
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["skill_name"], "decorated_skill")
        finally:
            path.unlink(missing_ok=True)

    def test_decorator_uses_function_name_as_default(self):
        collector, path = _tmp_collector()
        try:
            @collector.instrument(agent_id="agent-6")
            def auto_named():
                pass

            auto_named()
            events = collector.read_events()
            self.assertEqual(events[0]["skill_name"], "auto_named")
        finally:
            path.unlink(missing_ok=True)

    def test_thread_safety(self):
        """Concurrent writes from N threads must not corrupt the JSONL file."""
        collector, path = _tmp_collector()
        N_THREADS = 20
        N_EVENTS_PER_THREAD = 10
        errors = []

        def worker(tid):
            for i in range(N_EVENTS_PER_THREAD):
                try:
                    with collector.track(f"skill_{tid}", agent_id=f"agent-{tid}"):
                        time.sleep(0.001)
                except Exception as e:
                    errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread errors: {errors}")

        events = collector.read_events()
        expected = N_THREADS * N_EVENTS_PER_THREAD * 2
        self.assertEqual(len(events), expected)
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    def test_empty_log_returns_empty_list(self):
        collector, path = _tmp_collector()
        try:
            events = collector.read_events()
            self.assertEqual(events, [])
        finally:
            path.unlink(missing_ok=True)

    def test_malformed_jsonl_lines_are_skipped(self):
        """Lines that are not valid JSON should be silently skipped."""
        tmp = tempfile.NamedTemporaryFile(
            suffix=".jsonl", delete=False, mode="w", encoding="utf-8"
        )
        tmp.write('{"event_type": "skill_end", "skill_name": "ok"}\n')
        tmp.write("THIS IS NOT JSON\n")
        tmp.write("\n")
        tmp.write('{"event_type": "skill_end", "skill_name": "also_ok"}\n')
        tmp.close()
        path = Path(tmp.name)
        try:
            collector = MetricsCollector(metrics_path=path)
            events = collector.read_events()
            self.assertEqual(len(events), 2)
        finally:
            path.unlink(missing_ok=True)

    def test_budget_alert_event_appended(self):
        collector, path = _tmp_collector()
        collector._budget_usd = 0.01
        try:
            start = collector.record_start("expensive", "agent-x")
            collector.record_end(
                skill_name="expensive",
                agent_id="agent-x",
                execution_id=start.execution_id,
                duration_ms=100.0,
                cost_usd=0.02,
            )
            events = collector.read_events()
            budget_events = [e for e in events if e["event_type"] == EventType.BUDGET_ALERT.value]
            self.assertEqual(len(budget_events), 1)
            self.assertGreaterEqual(budget_events[0]["accumulated_usd"], 0.01)
        finally:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 3. CostTracker tests
# ---------------------------------------------------------------------------

def _make_end_event(
    skill,
    agent="a1",
    status="success",
    cost=0.001,
    tokens=100,
    duration_ms=200.0,
    ts=None,
):
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    return {
        "event_type": EventType.SKILL_END.value,
        "skill_name": skill,
        "agent_id": agent,
        "execution_id": "eid",
        "timestamp": ts,
        "duration_ms": duration_ms,
        "input_tokens": tokens // 2,
        "output_tokens": tokens // 2,
        "total_tokens": tokens,
        "cost_usd": cost,
        "status": status,
        "error_message": None,
        "metadata": {},
    }


class TestCostTracker(unittest.TestCase):

    def test_aggregate_by_skill_empty(self):
        tracker, path = _tmp_tracker([])
        try:
            stats = tracker.aggregate_by_skill()
            self.assertEqual(stats, {})
        finally:
            path.unlink(missing_ok=True)

    def test_aggregate_by_skill_counts(self):
        events = [
            _make_end_event("researcher", cost=0.002, tokens=200),
            _make_end_event("researcher", cost=0.003, tokens=300),
            _make_end_event("coder", status="failure", cost=0.001, tokens=50),
        ]
        tracker, path = _tmp_tracker(events)
        try:
            stats = tracker.aggregate_by_skill()
            self.assertEqual(len(stats), 2)

            r = stats["researcher"]
            self.assertEqual(r.total_executions, 2)
            self.assertEqual(r.successful_executions, 2)
            self.assertEqual(r.failed_executions, 0)
            self.assertAlmostEqual(r.total_cost_usd, 0.005, places=6)
            self.assertEqual(r.total_tokens, 500)

            c = stats["coder"]
            self.assertEqual(c.failed_executions, 1)
            self.assertAlmostEqual(c.success_rate, 0.0)
        finally:
            path.unlink(missing_ok=True)

    def test_aggregate_by_skill_respects_since_filter(self):
        old_ts = "2024-01-01T00:00:00+00:00"
        new_ts = "2025-06-01T00:00:00+00:00"
        events = [
            _make_end_event("browser", cost=0.01, ts=old_ts),
            _make_end_event("browser", cost=0.02, ts=new_ts),
        ]
        tracker, path = _tmp_tracker(events)
        try:
            since = datetime(2025, 1, 1, tzinfo=timezone.utc)
            stats = tracker.aggregate_by_skill(since=since)
            self.assertAlmostEqual(stats["browser"].total_cost_usd, 0.02, places=6)
            self.assertEqual(stats["browser"].total_executions, 1)
        finally:
            path.unlink(missing_ok=True)

    def test_aggregate_by_agent(self):
        events = [
            _make_end_event("skill1", agent="alice", cost=0.01),
            _make_end_event("skill2", agent="alice", cost=0.02),
            _make_end_event("skill1", agent="bob", cost=0.005, status="failure"),
        ]
        tracker, path = _tmp_tracker(events)
        try:
            agents = tracker.aggregate_by_agent()
            self.assertIn("alice", agents)
            self.assertIn("bob", agents)
            self.assertEqual(agents["alice"]["total_executions"], 2)
            self.assertAlmostEqual(agents["alice"]["total_cost_usd"], 0.03, places=6)
            self.assertEqual(agents["bob"]["failed_executions"], 1)
            self.assertAlmostEqual(agents["bob"]["success_rate_pct"], 0.0)
        finally:
            path.unlink(missing_ok=True)

    def test_aggregate_by_period_daily(self):
        events = [
            _make_end_event("s", cost=0.01, ts="2025-03-01T10:00:00+00:00"),
            _make_end_event("s", cost=0.02, ts="2025-03-01T22:00:00+00:00"),
            _make_end_event("s", cost=0.05, ts="2025-03-02T08:00:00+00:00"),
        ]
        tracker, path = _tmp_tracker(events)
        try:
            rows = tracker.aggregate_by_period(period="daily")
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["period"], "2025-03-01")
            self.assertEqual(rows[0]["executions"], 2)
            self.assertAlmostEqual(rows[0]["cost_usd"], 0.03, places=6)
            self.assertEqual(rows[1]["period"], "2025-03-02")
            self.assertAlmostEqual(rows[1]["cost_usd"], 0.05, places=6)
        finally:
            path.unlink(missing_ok=True)

    def test_aggregate_by_period_monthly(self):
        events = [
            _make_end_event("s", cost=0.01, ts="2025-01-15T00:00:00+00:00"),
            _make_end_event("s", cost=0.01, ts="2025-01-20T00:00:00+00:00"),
            _make_end_event("s", cost=0.05, ts="2025-02-10T00:00:00+00:00"),
        ]
        tracker, path = _tmp_tracker(events)
        try:
            rows = tracker.aggregate_by_period(period="monthly")
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["period"], "2025-01")
            self.assertEqual(rows[0]["executions"], 2)
        finally:
            path.unlink(missing_ok=True)

    def test_aggregate_by_period_skill_filter(self):
        events = [
            _make_end_event("researcher", cost=0.01, ts="2025-03-01T10:00:00+00:00"),
            _make_end_event("coder", cost=0.02, ts="2025-03-01T10:00:00+00:00"),
        ]
        tracker, path = _tmp_tracker(events)
        try:
            rows = tracker.aggregate_by_period(period="daily", skill_name="researcher")
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]["cost_usd"], 0.01, places=6)
        finally:
            path.unlink(missing_ok=True)

    def test_aggregate_by_period_invalid_raises(self):
        tracker, path = _tmp_tracker([])
        try:
            with self.assertRaises(ValueError):
                tracker.aggregate_by_period(period="invalid")
        finally:
            path.unlink(missing_ok=True)

    def test_total_cost(self):
        events = [
            _make_end_event("a", cost=0.01),
            _make_end_event("b", cost=0.02),
            _make_end_event("c", cost=0.03),
        ]
        tracker, path = _tmp_tracker(events)
        try:
            self.assertAlmostEqual(tracker.total_cost(), 0.06, places=6)
        finally:
            path.unlink(missing_ok=True)

    def test_export_json_structure(self):
        events = [_make_end_event("researcher", cost=0.005)]
        tracker, path = _tmp_tracker(events)
        try:
            json_str = tracker.export_json()
            data = json.loads(json_str)
            self.assertIn("generated_at", data)
            self.assertIn("total_cost_usd", data)
            self.assertIn("by_skill", data)
            self.assertIn("by_agent", data)
            self.assertIn("researcher", data["by_skill"])
        finally:
            path.unlink(missing_ok=True)

    def test_export_json_to_file(self):
        events = [_make_end_event("researcher", cost=0.005)]
        tracker, path = _tmp_tracker(events)
        out = Path(tempfile.mktemp(suffix=".json"))
        try:
            tracker.export_json(output_path=out)
            self.assertTrue(out.exists())
            data = json.loads(out.read_text())
            self.assertIn("by_skill", data)
        finally:
            path.unlink(missing_ok=True)
            out.unlink(missing_ok=True)

    def test_export_csv_structure(self):
        events = [
            _make_end_event("researcher", cost=0.005),
            _make_end_event("coder", cost=0.002),
        ]
        tracker, path = _tmp_tracker(events)
        try:
            csv_str = tracker.export_csv()
            reader = csv.DictReader(io.StringIO(csv_str))
            rows = list(reader)
            self.assertEqual(len(rows), 2)
            skill_names = {r["skill_name"] for r in rows}
            self.assertIn("researcher", skill_names)
            self.assertIn("coder", skill_names)
        finally:
            path.unlink(missing_ok=True)

    def test_export_csv_empty_data(self):
        """Export CSV on empty log should return a header-only CSV, not crash."""
        tracker, path = _tmp_tracker([])
        try:
            csv_str = tracker.export_csv()
            self.assertIn("skill_name", csv_str)
        finally:
            path.unlink(missing_ok=True)

    def test_start_events_excluded_from_aggregation(self):
        """SKILL_START events must not be counted in cost aggregation."""
        start_ev = {
            "event_type": EventType.SKILL_START.value,
            "skill_name": "researcher",
            "agent_id": "a1",
            "execution_id": "eid-1",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cost_usd": 99.99,
            "status": "in_progress",
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "duration_ms": None, "error_message": None, "metadata": {},
        }
        end_ev = _make_end_event("researcher", cost=0.001)
        tracker, path = _tmp_tracker([start_ev, end_ev])
        try:
            self.assertAlmostEqual(tracker.total_cost(), 0.001, places=6)
        finally:
            path.unlink(missing_ok=True)

    def test_aggregate_by_period_hourly(self):
        events = [
            _make_end_event("s", cost=0.01, ts="2025-03-01T10:15:00+00:00"),
            _make_end_event("s", cost=0.02, ts="2025-03-01T10:55:00+00:00"),
            _make_end_event("s", cost=0.05, ts="2025-03-01T11:05:00+00:00"),
        ]
        tracker, path = _tmp_tracker(events)
        try:
            rows = tracker.aggregate_by_period(period="hourly")
            self.assertEqual(len(rows), 2)
            # Both 10:xx events land in the same hour bucket
            self.assertTrue(rows[0]["period"].endswith(":00"))
            self.assertEqual(rows[0]["executions"], 2)
            self.assertAlmostEqual(rows[0]["cost_usd"], 0.03, places=6)
            self.assertEqual(rows[1]["executions"], 1)
        finally:
            path.unlink(missing_ok=True)

    def test_aggregate_by_period_weekly(self):
        # 2025-03-01 is Saturday (ISO week 2025-W09),
        # 2025-03-10 is Monday (ISO week 2025-W11).
        events = [
            _make_end_event("s", ts="2025-03-01T00:00:00+00:00"),
            _make_end_event("s", ts="2025-03-10T00:00:00+00:00"),
        ]
        tracker, path = _tmp_tracker(events)
        try:
            rows = tracker.aggregate_by_period(period="weekly")
            self.assertEqual(len(rows), 2)
            periods = [r["period"] for r in rows]
            self.assertEqual(periods, sorted(periods))
        finally:
            path.unlink(missing_ok=True)

    def test_aggregate_by_period_rows_sorted_ascending(self):
        events = [
            _make_end_event("s", ts="2025-03-03T00:00:00+00:00"),
            _make_end_event("s", ts="2025-03-01T00:00:00+00:00"),
            _make_end_event("s", ts="2025-03-02T00:00:00+00:00"),
        ]
        tracker, path = _tmp_tracker(events)
        try:
            rows = tracker.aggregate_by_period(period="daily")
            periods = [r["period"] for r in rows]
            self.assertEqual(periods, sorted(periods))
        finally:
            path.unlink(missing_ok=True)

    def test_aggregate_by_period_since_filter(self):
        events = [
            _make_end_event("s", cost=0.50, ts="2024-01-01T00:00:00+00:00"),
            _make_end_event("s", cost=0.01, ts="2025-06-01T00:00:00+00:00"),
        ]
        tracker, path = _tmp_tracker(events)
        try:
            since = datetime(2025, 1, 1, tzinfo=timezone.utc)
            rows = tracker.aggregate_by_period(period="daily", since=since)
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]["cost_usd"], 0.01, places=6)
        finally:
            path.unlink(missing_ok=True)

    def test_aggregate_by_agent_since_filter(self):
        events = [
            _make_end_event("s", agent="alice", cost=0.10, ts="2024-01-01T00:00:00+00:00"),
            _make_end_event("s", agent="alice", cost=0.02, ts="2025-06-01T00:00:00+00:00"),
        ]
        tracker, path = _tmp_tracker(events)
        try:
            since = datetime(2025, 1, 1, tzinfo=timezone.utc)
            agents = tracker.aggregate_by_agent(since=since)
            self.assertEqual(agents["alice"]["total_executions"], 1)
            self.assertAlmostEqual(agents["alice"]["total_cost_usd"], 0.02, places=6)
        finally:
            path.unlink(missing_ok=True)

    def test_total_executions(self):
        events = [_make_end_event("a"), _make_end_event("b"), _make_end_event("c")]
        tracker, path = _tmp_tracker(events)
        try:
            self.assertEqual(tracker.total_executions(), 3)
        finally:
            path.unlink(missing_ok=True)

    def test_total_executions_since_filter(self):
        events = [
            _make_end_event("s", ts="2024-01-01T00:00:00+00:00"),
            _make_end_event("s", ts="2025-06-01T00:00:00+00:00"),
        ]
        tracker, path = _tmp_tracker(events)
        try:
            since = datetime(2025, 1, 1, tzinfo=timezone.utc)
            self.assertEqual(tracker.total_executions(since=since), 1)
            self.assertEqual(tracker.total_executions(), 2)
        finally:
            path.unlink(missing_ok=True)

    def test_multiple_malformed_lines_are_all_skipped(self):
        """All malformed lines are silently skipped; valid lines are kept."""
        tmp = tempfile.NamedTemporaryFile(
            suffix=".jsonl", delete=False, mode="w", encoding="utf-8"
        )
        tmp.write("{broken\n")
        tmp.write("not json at all\n")
        tmp.write("\n")
        tmp.write(_make_end_event("planner", cost=0.007).__class__.__name__ + "\n")  # not JSON
        good = _make_end_event("planner", cost=0.007)
        tmp.write(json.dumps(good) + "\n")
        tmp.close()
        path = Path(tmp.name)
        try:
            tracker = CostTracker(metrics_path=path)
            stats = tracker.aggregate_by_skill()
            self.assertEqual(stats["planner"].total_executions, 1)
            self.assertAlmostEqual(stats["planner"].total_cost_usd, 0.007, places=6)
        finally:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 4. Dashboard smoke tests
# ---------------------------------------------------------------------------

class TestDashboard(unittest.TestCase):

    def test_render_snapshot_empty(self):
        """Dashboard must render without error on an empty log."""
        from skills.metrics.dashboard import Dashboard
        tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        tmp.close()
        path = Path(tmp.name)
        try:
            dash = Dashboard(metrics_path=path)
            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            try:
                dash.render_snapshot()
            finally:
                sys.stdout = old_stdout
        finally:
            path.unlink(missing_ok=True)

    def test_render_snapshot_with_data(self):
        """Dashboard renders all sections when data is present."""
        from skills.metrics.dashboard import Dashboard

        events = [
            _make_end_event("researcher", agent="agent-1", cost=0.005, tokens=500, ts="2025-03-01T10:00:00+00:00"),
            _make_end_event("coder",      agent="agent-2", cost=0.002, tokens=200, ts="2025-03-01T11:00:00+00:00"),
            _make_end_event("researcher", agent="agent-1", status="failure", cost=0.001, ts="2025-03-02T09:00:00+00:00"),
        ]
        tracker, path = _tmp_tracker(events)
        try:
            dash = Dashboard(metrics_path=path, budget_usd=1.00)
            old_stdout = sys.stdout
            captured = io.StringIO()
            sys.stdout = captured
            try:
                dash.render_snapshot(period="daily")
            finally:
                sys.stdout = old_stdout

            output = captured.getvalue()
            self.assertIn("researcher", output)
            self.assertIn("coder", output)
        finally:
            path.unlink(missing_ok=True)

    def test_render_snapshot_with_skill_filter(self):
        from skills.metrics.dashboard import Dashboard

        events = [
            _make_end_event("researcher", cost=0.005),
            _make_end_event("coder", cost=0.002),
        ]
        tracker, path = _tmp_tracker(events)
        try:
            dash = Dashboard(metrics_path=path)
            old_stdout = sys.stdout
            captured = io.StringIO()
            sys.stdout = captured
            try:
                dash.render_snapshot(skill_filter="researcher")
            finally:
                sys.stdout = old_stdout

            output = captured.getvalue()
            self.assertIn("researcher", output)
        finally:
            path.unlink(missing_ok=True)

    def test_budget_bar_renders_when_budget_set(self):
        from skills.metrics.dashboard import Dashboard

        events = [_make_end_event("s", cost=0.5)]
        tracker, path = _tmp_tracker(events)
        try:
            dash = Dashboard(metrics_path=path, budget_usd=1.00)
            old_stdout = sys.stdout
            captured = io.StringIO()
            sys.stdout = captured
            try:
                dash.render_snapshot()
            finally:
                sys.stdout = old_stdout
            output = captured.getvalue()
            self.assertIn("Budget", output)
        finally:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 5. Integration: end-to-end workflow
# ---------------------------------------------------------------------------

class TestIntegration(unittest.TestCase):

    def test_collector_to_tracker_pipeline(self):
        """Record events with the collector, then read back with the tracker."""
        tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        tmp.close()
        path = Path(tmp.name)
        try:
            collector = MetricsCollector(metrics_path=path)

            for i in range(5):
                with collector.track("researcher", agent_id="orchestrator") as ctx:
                    ctx.add_tokens(input=100 * (i + 1), output=50 * (i + 1), cost_usd=0.0005 * (i + 1))

            for _ in range(2):
                try:
                    with collector.track("coder", agent_id="orchestrator"):
                        raise RuntimeError("compile error")
                except RuntimeError:
                    pass

            tracker = CostTracker(metrics_path=path)
            stats = tracker.aggregate_by_skill()

            self.assertIn("researcher", stats)
            self.assertIn("coder", stats)

            r = stats["researcher"]
            self.assertEqual(r.total_executions, 5)
            self.assertEqual(r.successful_executions, 5)
            self.assertEqual(r.failed_executions, 0)
            self.assertAlmostEqual(r.success_rate, 100.0)

            c = stats["coder"]
            self.assertEqual(c.total_executions, 2)
            self.assertEqual(c.failed_executions, 2)
            self.assertAlmostEqual(c.success_rate, 0.0)

            # Total cost: 0.0005 * (1+2+3+4+5) = 0.0075
            self.assertAlmostEqual(r.total_cost_usd, 0.0075, places=6)
            self.assertAlmostEqual(tracker.total_cost(), 0.0075, places=6)
        finally:
            path.unlink(missing_ok=True)

    def test_concurrent_collectors_share_one_log(self):
        """Two independent MetricsCollector instances writing to the same file."""
        tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        tmp.close()
        path = Path(tmp.name)
        try:
            c1 = MetricsCollector(metrics_path=path)
            c2 = MetricsCollector(metrics_path=path)
            errors = []

            def writer(collector, skill):
                for _ in range(10):
                    try:
                        with collector.track(skill, agent_id="shared"):
                            pass
                    except Exception as e:
                        errors.append(str(e))

            t1 = threading.Thread(target=writer, args=(c1, "skill_a"))
            t2 = threading.Thread(target=writer, args=(c2, "skill_b"))
            t1.start(); t2.start()
            t1.join(); t2.join()

            self.assertEqual(errors, [])
            tracker = CostTracker(metrics_path=path)
            all_events = tracker._collector.read_events()
            self.assertEqual(len(all_events), 40)
        finally:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
