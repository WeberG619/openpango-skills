"""
dashboard.py - Terminal dashboard with ANSI color output.

Matches the style established by the health check in src/cli.js:
  - Box-drawing characters for panels
  - check / x / warning status icons
  - Bold/cyan section headers
  - dim helper text

Supports:
  - One-shot snapshot mode (default)
  - Live --watch mode that refreshes every N seconds
  - Time-period filtering (--period hourly/daily/weekly/monthly)
  - Single-skill drill-down (--skill NAME)
  - Budget threshold warning (--budget USD)
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .cost_tracker import CostTracker, VALID_PERIODS
from .models import AggregatedStats
from .metrics_collector import _DEFAULT_METRICS_PATH

# ---------------------------------------------------------------------------
# ANSI helpers — zero external dependencies, matches cli.js c.* style
# ---------------------------------------------------------------------------

def _supports_color() -> bool:
    """Return True when stdout is a TTY that supports ANSI codes."""
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


_COLOR = _supports_color()


def _esc(code: str, text: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if _COLOR else text


def c_green(s: str) -> str:   return _esc("32", s)
def c_red(s: str) -> str:     return _esc("31", s)
def c_yellow(s: str) -> str:  return _esc("33", s)
def c_cyan(s: str) -> str:    return _esc("36", s)
def c_dim(s: str) -> str:     return _esc("2", s)
def c_bold(s: str) -> str:    return _esc("1", s)
def c_white(s: str) -> str:   return _esc("97", s)

OK   = c_green("v")
FAIL = c_red("x")
WARN = c_yellow("!")

# ---------------------------------------------------------------------------
# Bar chart helper
# ---------------------------------------------------------------------------

BAR_MAX_WIDTH = 24


def _bar(value: float, max_value: float, width: int = BAR_MAX_WIDTH) -> str:
    """Render a compact proportional bar using block characters."""
    if max_value <= 0 or value < 0:
        return c_dim("-" * width)
    ratio = min(value / max_value, 1.0)
    filled = int(ratio * width)
    bar = "#" * filled
    bar += "-" * (width - filled)
    if ratio < 0.5:
        return c_green(bar)
    elif ratio < 0.8:
        return c_yellow(bar)
    else:
        return c_red(bar)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_usd(value: float) -> str:
    if value == 0.0:
        return c_dim("$0.000000")
    elif value < 0.001:
        return c_yellow(f"${value:.6f}")
    elif value < 0.10:
        return c_cyan(f"${value:.4f}")
    else:
        return c_red(f"${value:.4f}")


def _fmt_ms(ms) -> str:
    if ms is None:
        return c_dim("--")
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms / 1000:.2f}s"


def _fmt_pct(pct: float) -> str:
    if pct >= 95:
        return c_green(f"{pct:.1f}%")
    elif pct >= 75:
        return c_yellow(f"{pct:.1f}%")
    else:
        return c_red(f"{pct:.1f}%")


def _fmt_tokens(n: int) -> str:
    if n == 0:
        return c_dim("0")
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _term_width() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


# ---------------------------------------------------------------------------
# Dashboard renderer
# ---------------------------------------------------------------------------

class Dashboard:
    """
    Renders the metrics dashboard to stdout.

    All render methods return the number of lines printed so that --watch mode
    can use ANSI cursor movement to redraw in place.
    """

    def __init__(
        self,
        metrics_path: Optional[Path] = None,
        budget_usd: Optional[float] = None,
    ) -> None:
        self._tracker = CostTracker(metrics_path=metrics_path)
        self._budget_usd = budget_usd
        self._path = metrics_path or _DEFAULT_METRICS_PATH

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def render_snapshot(
        self,
        period: str = "daily",
        skill_filter: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> int:
        """Print a full dashboard snapshot. Returns line count."""
        lines = self._build_lines(period=period, skill_filter=skill_filter, since=since)
        print("\n".join(lines))
        return len(lines)

    def watch(
        self,
        interval: float = 5.0,
        period: str = "daily",
        skill_filter: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> None:
        """
        Live-refresh mode.

        Clears the terminal and redraws every `interval` seconds.
        Press Ctrl-C to exit.
        """
        try:
            while True:
                sys.stdout.write("\x1b[H\x1b[2J")
                sys.stdout.flush()

                self.render_snapshot(period=period, skill_filter=skill_filter, since=since)

                ts = datetime.now().strftime("%H:%M:%S")
                footer = c_dim(f"  Auto-refresh every {interval:.0f}s  |  Last update: {ts}  |  Ctrl-C to exit")
                print(footer)

                time.sleep(interval)
        except KeyboardInterrupt:
            print(c_dim("\n  Dashboard stopped."))

    # ------------------------------------------------------------------
    # Internal build
    # ------------------------------------------------------------------

    def _build_lines(
        self,
        period: str,
        skill_filter: Optional[str],
        since: Optional[datetime],
    ) -> list:
        lines = []
        w = _term_width()
        inner_w = min(w - 4, 74)

        lines += self._section_title("OpenPango  Metrics & Cost Dashboard", inner_w)

        all_stats = self._tracker.aggregate_by_skill(since=since)
        if skill_filter:
            all_stats = {k: v for k, v in all_stats.items() if k == skill_filter}

        total_exec = sum(s.total_executions for s in all_stats.values())
        total_cost = sum(s.total_cost_usd for s in all_stats.values())
        total_tokens = sum(s.total_tokens for s in all_stats.values())
        total_ok = sum(s.successful_executions for s in all_stats.values())
        global_sr = (total_ok / total_exec * 100) if total_exec else 0.0

        lines.append(c_bold(c_cyan("  +-- Summary")))
        lines.append(f"  |  Total executions : {c_white(c_bold(str(total_exec)))}")
        lines.append(f"  |  Total cost       : {_fmt_usd(total_cost)}")
        lines.append(f"  |  Total tokens     : {c_cyan(_fmt_tokens(total_tokens))}")
        lines.append(f"  |  Global success   : {_fmt_pct(global_sr)}")

        if self._budget_usd is not None:
            used_pct = total_cost / self._budget_usd * 100 if self._budget_usd > 0 else 0
            bar = _bar(total_cost, self._budget_usd)
            icon = OK if used_pct < 80 else (WARN if used_pct < 100 else FAIL)
            lines.append(
                f"  |  Budget           : {icon} {bar} "
                + c_dim(f"${total_cost:.4f} / ${self._budget_usd:.4f}")
            )

        exists = Path(self._path).exists()
        ev_count = len(self._tracker._collector.read_events())
        path_label = c_dim(str(self._path))
        lines.append(f"  |  Log              : {OK if exists else WARN} {path_label} {c_dim(f'({ev_count} events)')}")
        lines.append(c_dim("  |"))

        if all_stats:
            lines += self._render_skill_table(all_stats)
        else:
            lines.append(c_bold(c_cyan("  +-- Skills")))
            lines.append(f"  |  {WARN} {c_dim('No data yet. Instrument a skill to start collecting metrics.')}")
            lines.append(c_dim("  |"))

        lines += self._render_time_series(period=period, skill_filter=skill_filter, since=since)
        lines += self._render_agent_table(since=since)
        lines += self._section_footer()
        return lines

    # ------------------------------------------------------------------
    # Sub-renderers
    # ------------------------------------------------------------------

    def _section_title(self, title: str, width: int) -> list:
        pad = max(0, width - len(title) - 2)
        top = c_bold("  /== " + title + " " + "=" * pad + "\\")
        bot = c_bold("  \\" + "=" * (width + 4) + "/")
        return [
            "",
            top,
            c_bold("  |" + " " * (width + 4) + "|"),
            bot,
            "",
        ]

    def _render_skill_table(self, stats: dict) -> list:
        lines = []
        lines.append(c_bold(c_cyan("  +-- Skills")))

        lines.append(
            "  |  "
            + c_dim(f"{'Skill':<24} {'Exec':>6} {'OK%':>7} {'Cost':>10} {'Tokens':>8} {'Avg ms':>8} {'Bar (cost)'}")
        )
        lines.append("  |  " + c_dim("-" * 70))

        sorted_stats = sorted(stats.items(), key=lambda x: x[1].total_cost_usd, reverse=True)
        max_cost = max((s.total_cost_usd for _, s in sorted_stats), default=1.0) or 1.0

        for name, s in sorted_stats:
            sr_str = _fmt_pct(s.success_rate)
            cost_str = _fmt_usd(s.total_cost_usd)
            tok_str = c_cyan(_fmt_tokens(s.total_tokens))
            avg_str = c_dim(_fmt_ms(s.avg_duration_ms))
            bar = _bar(s.total_cost_usd, max_cost, width=16)
            icon = OK if s.success_rate >= 95 else (WARN if s.success_rate >= 75 else FAIL)

            display_name = (name[:21] + "...") if len(name) > 24 else name

            lines.append(
                f"  |  {icon} {c_bold(display_name):<24} "
                f"{s.total_executions:>6} "
                f"{sr_str:>7} "
                f"{cost_str:>10} "
                f"{tok_str:>8} "
                f"{avg_str:>8}  {bar}"
            )

            dur_detail = c_dim(
                f"  |      min {_fmt_ms(s.min_duration_ms)}  "
                f"max {_fmt_ms(s.max_duration_ms)}  "
                f"fail {s.failed_executions}"
            )
            lines.append(dur_detail)

        lines.append(c_dim("  |"))
        return lines

    def _render_time_series(
        self,
        period: str,
        skill_filter: Optional[str],
        since: Optional[datetime],
    ) -> list:
        lines = []
        rows = self._tracker.aggregate_by_period(
            period=period,
            skill_name=skill_filter,
            since=since,
        )
        rows = rows[-10:]

        lines.append(c_bold(c_cyan(f"  +-- Trend  {c_dim(f'({period})')}")))

        if not rows:
            lines.append(f"  |  {c_dim('No data for this time range.')}")
            lines.append(c_dim("  |"))
            return lines

        max_cost = max((r["cost_usd"] for r in rows), default=1.0) or 1.0

        lines.append(
            "  |  " + c_dim(
                f"{'Period':<18} {'Execs':>6} {'Cost':>10}  {'Timeline'}"
            )
        )
        for r in rows:
            bar = _bar(r["cost_usd"], max_cost, width=20)
            lines.append(
                f"  |  {c_dim(r['period']):<18} "
                f"{r['executions']:>6} "
                f"{_fmt_usd(r['cost_usd']):>10}  "
                f"{bar}"
            )

        lines.append(c_dim("  |"))
        return lines

    def _render_agent_table(self, since: Optional[datetime]) -> list:
        lines = []
        agents = self._tracker.aggregate_by_agent(since=since)

        lines.append(c_bold(c_cyan("  +-- Agents")))

        if not agents:
            lines.append(f"  |  {c_dim('No agent data yet.')}")
            lines.append(c_dim("  |"))
            return lines

        lines.append(
            "  |  " + c_dim(f"{'Agent':<28} {'Exec':>6} {'OK%':>7} {'Cost':>10} {'Tokens':>8}")
        )
        lines.append("  |  " + c_dim("-" * 64))

        for agent, d in sorted(agents.items(), key=lambda x: x[1]["total_cost_usd"], reverse=True):
            display = (agent[:25] + "...") if len(agent) > 28 else agent
            icon = OK if d["success_rate_pct"] >= 95 else (WARN if d["success_rate_pct"] >= 75 else FAIL)
            lines.append(
                f"  |  {icon} {c_bold(display):<28} "
                f"{d['total_executions']:>6} "
                f"{_fmt_pct(d['success_rate_pct']):>7} "
                f"{_fmt_usd(d['total_cost_usd']):>10} "
                f"{c_cyan(_fmt_tokens(d['total_tokens'])):>8}"
            )

        lines.append(c_dim("  |"))
        return lines

    def _section_footer(self) -> list:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            c_bold(c_cyan("  +-- Tip")),
            f"     {c_dim('Run with --watch for live updates  |  --export-json/--export-csv to save')}",
            f"     {c_dim(f'Generated: {ts}')}",
            "",
        ]
        return lines


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python3 -m skills.metrics.dashboard",
        description="OpenPango metrics dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 -m skills.metrics.dashboard
  python3 -m skills.metrics.dashboard --watch
  python3 -m skills.metrics.dashboard --period weekly --skill researcher
  python3 -m skills.metrics.dashboard --since 2025-01-01 --budget 1.00
  python3 -m skills.metrics.dashboard --export-json metrics_report.json
  python3 -m skills.metrics.dashboard --export-csv metrics_report.csv
""",
    )
    parser.add_argument(
        "--period",
        choices=VALID_PERIODS,
        default="daily",
        help="Time aggregation granularity (default: daily)",
    )
    parser.add_argument("--skill", metavar="NAME", help="Filter to a single skill name")
    parser.add_argument(
        "--since",
        metavar="ISO8601",
        help="Only include events on or after this date (e.g. 2025-01-01)",
    )
    parser.add_argument(
        "--budget",
        type=float,
        metavar="USD",
        help="Show budget progress bar and alert when exceeded",
    )
    parser.add_argument(
        "--watch",
        nargs="?",
        const=5.0,
        type=float,
        metavar="SECONDS",
        help="Live refresh mode; optionally specify interval (default 5s)",
    )
    parser.add_argument(
        "--export-json",
        metavar="FILE",
        help="Export aggregated stats to JSON and exit",
    )
    parser.add_argument(
        "--export-csv",
        metavar="FILE",
        help="Export per-skill stats to CSV and exit",
    )
    parser.add_argument(
        "--metrics-file",
        metavar="PATH",
        help=f"Override metrics log path (default: {_DEFAULT_METRICS_PATH})",
    )

    args = parser.parse_args()

    metrics_path = Path(args.metrics_file) if args.metrics_file else None
    tracker = CostTracker(metrics_path=metrics_path)

    since: Optional[datetime] = None
    if args.since:
        since = datetime.fromisoformat(args.since)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)

    if args.export_json:
        tracker.export_json(output_path=Path(args.export_json), since=since)
        print(c_green(f"{OK} Exported to {args.export_json}"))
        return

    if args.export_csv:
        tracker.export_csv(output_path=Path(args.export_csv), since=since)
        print(c_green(f"{OK} Exported to {args.export_csv}"))
        return

    dashboard = Dashboard(metrics_path=metrics_path, budget_usd=args.budget)

    if args.watch is not None:
        dashboard.watch(
            interval=float(args.watch),
            period=args.period,
            skill_filter=args.skill,
            since=since,
        )
    else:
        dashboard.render_snapshot(
            period=args.period,
            skill_filter=args.skill,
            since=since,
        )


if __name__ == "__main__":
    main()
