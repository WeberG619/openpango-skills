"""
skills/metrics - Skill Execution Metrics & Cost Tracking

Public API surface:

    from skills.metrics import MetricsCollector, CostTracker, Dashboard
    from skills.metrics.models import MetricEvent, AggregatedStats

The MetricsCollector is the write side (event recording).
The CostTracker is the read side (aggregation and export).
The Dashboard renders colour terminal output.
"""
from .metrics_collector import MetricsCollector, _DEFAULT_METRICS_PATH
from .cost_tracker import CostTracker
from .dashboard import Dashboard
from .models import MetricEvent, AggregatedStats, EventType, Status

__all__ = [
    "MetricsCollector",
    "CostTracker",
    "Dashboard",
    "MetricEvent",
    "AggregatedStats",
    "EventType",
    "Status",
    "_DEFAULT_METRICS_PATH",
]
