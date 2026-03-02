---
name: metrics
description: "Skill execution metrics & cost tracking — monitors performance, API costs, and agent efficiency for the A2A Economy."
version: "1.0.0"
user-invocable: true
metadata:
  capabilities:
    - metrics/collect
    - metrics/aggregate
    - metrics/dashboard
  author: "WeberG619"
  license: "MIT"
---

## Cross-Skill Integration

This skill integrates with the OpenPango ecosystem:
- **Memory**: Metrics events use the same append-only JSONL event-sourced pattern as the memory skill — compatible storage path at `~/.openclaw/workspace/`.
- **Orchestration**: The orchestration router can wrap task dispatch with `MetricsCollector.track()` to automatically record every skill invocation.
- **A2A**: Agent-to-agent calls can carry `agent_id` metadata so per-agent cost accounting works across the full A2A graph.
- **Self-Improvement**: The dashboard surface exposes failure rates, latency outliers, and high-cost skills — inputs for the self-improvement skill's optimisation loop.

---

# Metrics & Cost Tracking

The **metrics skill** provides event-sourced performance monitoring and API cost tracking for every skill execution in the OpenPango ecosystem. It answers:

- *Which skills are costing the most?*
- *Where are failures clustering?*
- *Is this agent staying within budget?*
- *How has throughput changed week-over-week?*

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Skill Execution                             │
│                                                                 │
│   ┌───────────┐   record_start()   ┌──────────────────────┐    │
│   │  Any Skill│ ──────────────────▶│  MetricsCollector    │    │
│   │  Function │   record_end()     │  (thread-safe write) │    │
│   └───────────┘ ──────────────────▶└──────────┬───────────┘    │
│                                               │ append          │
│                                               ▼                 │
│                              ~/.openclaw/workspace/metrics.jsonl│
│                                (append-only event log)          │
│                                               │ read            │
│                                               ▼                 │
│                                    ┌──────────────────┐        │
│                                    │   CostTracker    │        │
│                                    │  (aggregation)   │        │
│                                    └────────┬─────────┘        │
│                                             │                   │
│                               ┌─────────────┴──────────────┐   │
│                               │         Dashboard           │   │
│                               │   (ANSI terminal output)   │   │
│                               └────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### Event-Sourced Design

All metrics are stored as immutable, append-only JSONL events — the same pattern used by the **memory** skill's Beads architecture. This means:

- **No database locks** — concurrent agent writes are safe
- **Full audit trail** — replay the log to reconstruct any historical view
- **Git-friendly** — append-only files merge cleanly
- **Zero schema migration** — new event types coexist with old ones

### Storage

All events are stored at:
```
~/.openclaw/workspace/metrics.jsonl
```

Each line is a single JSON event:
```json
{
  "event_type": "skill_end",
  "skill_name": "researcher",
  "agent_id": "orchestrator-1",
  "execution_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "timestamp": "2025-03-01T14:30:00.123456+00:00",
  "duration_ms": 1234.5,
  "input_tokens": 512,
  "output_tokens": 256,
  "total_tokens": 768,
  "cost_usd": 0.001152,
  "status": "success",
  "error_message": null,
  "metadata": {"model": "gpt-4o", "provider": "openai"}
}
```

## File Structure

```
skills/metrics/
├── SKILL.md                # This file — documentation and usage
├── __init__.py             # Public API exports
├── models.py               # Dataclass models (MetricEvent, AggregatedStats)
├── metrics_collector.py    # Write side — event recording (thread-safe)
├── cost_tracker.py         # Read side — aggregation, time-series, export
├── dashboard.py            # ANSI terminal dashboard with --watch mode
└── test_metrics.py         # 30+ unit and integration tests
```

## Usage

### 1. Context Manager (recommended)

```python
from skills.metrics import MetricsCollector

collector = MetricsCollector()

with collector.track("researcher", agent_id="orchestrator-1") as ctx:
    result = run_web_search(query)
    ctx.add_tokens(input=512, output=256, cost_usd=0.00115)
    ctx.set_metadata(model="gpt-4o", provider="openai")
```

### 2. Decorator

```python
from skills.metrics import MetricsCollector

collector = MetricsCollector()

@collector.instrument(agent_id="planner-1")
def plan_tasks(objective: str) -> list:
    ...
```

The decorator uses the function name as the `skill_name` automatically.

### 3. Manual event recording

```python
from skills.metrics import MetricsCollector

collector = MetricsCollector()

# Record start
start_ev = collector.record_start("coder", agent_id="agent-x")

try:
    result = compile_and_run(code)
    collector.record_end(
        skill_name="coder",
        agent_id="agent-x",
        execution_id=start_ev.execution_id,
        duration_ms=890.3,
        input_tokens=1024,
        output_tokens=512,
        cost_usd=0.0023,
    )
except Exception as e:
    collector.record_error(
        skill_name="coder",
        agent_id="agent-x",
        execution_id=start_ev.execution_id,
        duration_ms=15.0,
        error_message=str(e),
    )
```

### 4. Budget Alerts

```python
# Raise an alert (and append a BUDGET_ALERT event) when session cost
# exceeds $0.50:
collector = MetricsCollector(budget_alert_usd=0.50)
```

### 5. Integrating with Orchestration

```python
# In skills/orchestration/router.py
from skills.metrics import MetricsCollector

_metrics = MetricsCollector()

def dispatch(skill_name: str, agent_id: str, task: dict):
    with _metrics.track(skill_name, agent_id=agent_id, metadata={"task_id": task["id"]}):
        return _execute(skill_name, task)
```

## CLI — openpango metrics

The dashboard is invoked as a subcommand of the `openpango` CLI:

```
openpango metrics [OPTIONS]
```

### One-shot snapshot (default)

```bash
openpango metrics
```

### Live refresh (--watch)

```bash
# Refresh every 5 seconds (default)
openpango metrics --watch

# Custom refresh interval (2 seconds)
openpango metrics --watch 2
```

### Time period filtering

```bash
# Show hourly breakdown
openpango metrics --period hourly

# Show weekly trend
openpango metrics --period weekly

# Show data since a specific date
openpango metrics --since 2025-01-01
```

### Drill down to a single skill

```bash
openpango metrics --skill researcher
```

### Budget tracking

```bash
# Show budget progress bar; warn when $1.00 is exceeded
openpango metrics --budget 1.00
```

### Export

```bash
# Export full report as JSON
openpango metrics --export-json metrics_report.json

# Export per-skill table as CSV
openpango metrics --export-csv metrics_report.csv

# Export with date filter
openpango metrics --export-csv this_week.csv --since 2025-03-01
```

### Direct Python invocation

```bash
python3 -m skills.metrics.dashboard
python3 -m skills.metrics.dashboard --watch
python3 -m skills.metrics.dashboard --period weekly --budget 5.00
```

## Dashboard Output

```
  ╔══ OpenPango  Metrics & Cost Dashboard ════════════════════════════╗
  ║                                                                   ║
  ╚═══════════════════════════════════════════════════════════════════╝

  ├─ Summary
  │  Total executions : 142
  │  Total cost       : $0.2341
  │  Total tokens     : 184.3k
  │  Global success   : 97.2%
  │  Budget           : ✓ ████████████░░░░░░░░░░░░  $0.2341 / $1.0000
  │  Log              : ✓ ~/.openclaw/workspace/metrics.jsonl (284 events)
  │
  ├─ Skills
  │  Skill                    Exec    OK%       Cost   Tokens   Avg ms  Bar (cost)
  │  ─────────────────────────────────────────────────────────────────
  │  ✓ researcher               89  98.9%    $0.1876   148.3k    892ms  ████████████████
  │     min 312ms  max 4210ms  fail 1
  │  ✓ coder                    41  97.6%    $0.0421    32.1k    234ms  ████░░░░░░░░░░░░
  │     min 89ms  max 1890ms  fail 1
  │  ⚠ planner                  12  75.0%    $0.0044     3.9k    156ms  ░░░░░░░░░░░░░░░░
  │     min 44ms  max 890ms  fail 3
  │
  ├─ Trend  (daily)
  │  Period              Execs       Cost  Timeline
  │  2025-02-24             18    $0.0312  ████████░░░░░░░░░░░░
  │  2025-02-25             22    $0.0418  ██████████░░░░░░░░░░
  │  2025-03-01             31    $0.0621  ███████████████░░░░░
  │  2025-03-02             71    $0.0990  ████████████████████
  │
  ├─ Agents
  │  Agent                        Exec    OK%       Cost   Tokens
  │  ────────────────────────────────────────────────────────────
  │  ✓ orchestrator-1              112  98.2%    $0.2012   161.4k
  │  ⚠ planner-agent-2              30  86.7%    $0.0329    22.9k
  │
  └─ Tip
     Run with --watch for live updates  |  --export-json/--export-csv to save
     Generated: 2025-03-02 14:30:00
```

**Color coding:**
- Green `✓` — success rate >= 95%, cost bar < 50% of max
- Yellow `⚠` — success rate 75–94%, cost bar 50–80% of max
- Red `✗` — success rate < 75%, cost bar >= 80% of max

## Running the Tests

```bash
# From the repo root
python3 -m pytest skills/metrics/test_metrics.py -v

# Or directly
python3 skills/metrics/test_metrics.py
```

The test suite includes:
- Model serialization round-trips
- Thread-safety stress test (20 threads x 10 executions)
- Context manager success and exception paths
- Decorator auto-naming
- Aggregation: by-skill, by-agent, hourly/daily/weekly/monthly
- `since` date filtering
- Budget alert triggering
- CSV/JSON export
- Dashboard rendering (empty and populated)
- Malformed JSONL line tolerance
- Integration pipeline (collector → tracker)
- Concurrent multi-collector writes to shared log

## CostTracker Python API

```python
from skills.metrics import CostTracker
from datetime import datetime, timezone

tracker = CostTracker()

# Per-skill aggregation
stats = tracker.aggregate_by_skill()
for skill_name, s in stats.items():
    print(f"{skill_name}: {s.total_executions} runs, ${s.total_cost_usd:.4f}, {s.success_rate:.1f}% ok")

# Per-agent aggregation
agents = tracker.aggregate_by_agent()

# Time-series (daily breakdown)
rows = tracker.aggregate_by_period(period="daily")

# Filter by time window
since = datetime(2025, 3, 1, tzinfo=timezone.utc)
stats = tracker.aggregate_by_skill(since=since)

# Totals
print(f"Total cost: ${tracker.total_cost():.6f}")
print(f"Total executions: {tracker.total_executions()}")

# Export
tracker.export_json(output_path="report.json")
tracker.export_csv(output_path="report.csv")
```
