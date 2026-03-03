"""
skills/audit/cli.py

CLI entry point for the audit skill.

Usage:
    openpango audit --verify
    openpango audit --tail 20
    openpango audit --query tool/invoke
    openpango audit --stats
    openpango audit --verify --ledger /custom/path/audit.jsonl

All output is JSON to stdout.
Exit codes:
    0 — success / chain is valid
    1 — verification failed or error
    2 — argument error
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Resolve the default ledger path without importing the full package at module
# level (keeps this file usable even in partial installations).
# ---------------------------------------------------------------------------
_DEFAULT_LEDGER = Path.home() / ".openclaw" / "workspace" / "audit.jsonl"


def _get_ledger_path(args: argparse.Namespace) -> Path:
    return Path(args.ledger) if args.ledger else _DEFAULT_LEDGER


# ---------------------------------------------------------------------------
# Sub-command handlers
# ---------------------------------------------------------------------------


def cmd_verify(args: argparse.Namespace) -> int:
    from .verifier import verify_audit_log

    path = _get_ledger_path(args)
    report = verify_audit_log(path)
    _print_json(report)
    return 0 if report["valid"] else 1


def cmd_tail(args: argparse.Namespace) -> int:
    from .ledger import HashChainLedger

    path = _get_ledger_path(args)
    ledger = HashChainLedger(path)
    # args.tail is set via nargs="?" — value is N (int), or const=10 if bare flag
    n = args.tail if args.tail is not None else 10
    entries = ledger.read_last(n)
    _print_json({"entries": entries, "count": len(entries)})
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    from .ledger import HashChainLedger

    path = _get_ledger_path(args)
    ledger = HashChainLedger(path)
    all_entries = ledger.read_all()

    # args.query holds the event_type filter string
    prefix = args.query.lower()
    matched = [e for e in all_entries if e.get("event_type", "").lower().startswith(prefix)]
    _print_json({"query": args.query, "entries": matched, "count": len(matched)})
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    from .ledger import HashChainLedger

    path = _get_ledger_path(args)
    ledger = HashChainLedger(path)
    all_entries = ledger.read_all()

    if not all_entries:
        _print_json({
            "total_entries": 0,
            "first_entry_ts": None,
            "last_entry_ts": None,
            "event_type_counts": {},
            "agent_ids": [],
            "ledger_path": str(path),
            "ledger_size_bytes": path.stat().st_size if path.exists() else 0,
        })
        return 0

    counts: dict = {}
    agent_ids: set = set()
    for entry in all_entries:
        et = entry.get("event_type", "unknown")
        counts[et] = counts.get(et, 0) + 1
        aid = entry.get("agent_id")
        if aid:
            agent_ids.add(aid)

    _print_json({
        "total_entries": len(all_entries),
        "first_entry_ts": all_entries[0].get("timestamp"),
        "last_entry_ts": all_entries[-1].get("timestamp"),
        "event_type_counts": counts,
        "agent_ids": sorted(agent_ids),
        "ledger_path": str(path),
        "ledger_size_bytes": path.stat().st_size if path.exists() else 0,
    })
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openpango audit",
        description="Immutable audit log management — verify, query, and inspect the hash-chained ledger.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  openpango audit --verify
  openpango audit --tail 20
  openpango audit --query tool/invoke
  openpango audit --stats
  openpango audit --verify --ledger /path/to/audit.jsonl
""",
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--verify",
        action="store_true",
        help="Verify the cryptographic integrity of the entire ledger.",
    )
    group.add_argument(
        "--tail",
        metavar="N",
        type=int,
        help="Print the last N entries (default: 10 if flag given without value).",
        nargs="?",
        const=10,
    )
    group.add_argument(
        "--query",
        metavar="EVENT_TYPE",
        type=str,
        help="Filter entries by event_type prefix (e.g. 'tool/invoke', 'http').",
    )
    group.add_argument(
        "--stats",
        action="store_true",
        help="Print summary statistics for the ledger.",
    )

    parser.add_argument(
        "--ledger",
        metavar="PATH",
        type=str,
        default=None,
        help=(
            f"Path to the audit JSONL ledger file. "
            f"Default: {_DEFAULT_LEDGER}"
        ),
    )
    return parser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _print_json(obj: object) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.verify:
            return cmd_verify(args)
        elif args.tail is not None:
            return cmd_tail(args)
        elif args.query is not None:
            return cmd_query(args)
        elif args.stats:
            return cmd_stats(args)
        else:  # pragma: no cover
            parser.print_help()
            return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        _print_json({"error": str(exc), "type": type(exc).__name__})
        return 1


if __name__ == "__main__":
    sys.exit(main())
