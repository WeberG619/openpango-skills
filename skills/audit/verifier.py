"""
skills/audit/verifier.py

High-level verification of an audit ledger file.

    from skills.audit.verifier import verify_audit_log
    from pathlib import Path

    report = verify_audit_log(Path.home() / ".openclaw/workspace/audit.jsonl")
    if not report["valid"]:
        print("INTEGRITY VIOLATION:", report["errors"])
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .ledger import HashChainLedger, _GENESIS_PREV_HASH, _recompute_entry_hash


def verify_audit_log(path: Path) -> Dict[str, Any]:
    """
    Perform a comprehensive integrity check of the audit ledger at *path*.

    Checks performed
    ----------------
    1. File exists and is readable.
    2. Every line is valid JSON.
    3. Genesis entry has ``prev_hash == "000...000"``.
    4. Every ``entry_hash`` matches ``sha256(prev_hash + canonical_json(entry))``.
    5. Each ``prev_hash`` equals the ``entry_hash`` of the preceding entry.
    6. Timestamps are present and in ascending order.
    7. All required fields are present on every entry.

    Parameters
    ----------
    path:
        Absolute or relative ``Path`` to the ``audit.jsonl`` file.

    Returns
    -------
    dict with keys:

    - ``valid`` (bool): ``True`` only when all checks pass.
    - ``entries_checked`` (int): Total number of entries read.
    - ``errors`` (List[str]): Human-readable descriptions of every violation.
    - ``first_entry_ts`` (str | None): ISO timestamp of the first entry.
    - ``last_entry_ts``  (str | None): ISO timestamp of the last entry.
    - ``event_type_counts`` (dict): Counts per event_type string.
    """
    report: Dict[str, Any] = {
        "valid": False,
        "entries_checked": 0,
        "errors": [],
        "first_entry_ts": None,
        "last_entry_ts": None,
        "event_type_counts": {},
    }
    errors: List[str] = report["errors"]

    # ---------------------------------------------------------------
    # 1. File existence / readability
    # ---------------------------------------------------------------
    path = Path(path)
    if not path.exists():
        errors.append(f"Ledger file not found: {path}")
        return report

    if not path.is_file():
        errors.append(f"Path is not a regular file: {path}")
        return report

    # ---------------------------------------------------------------
    # 2. Read all lines, validating JSON
    # ---------------------------------------------------------------
    entries: List[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError as exc:
                    errors.append(f"Line {lineno}: invalid JSON — {exc}")
                    # Continue reading to find all JSON errors
                    continue
                entries.append(entry)
    except OSError as exc:
        errors.append(f"Cannot read ledger: {exc}")
        return report

    report["entries_checked"] = len(entries)

    if not entries:
        # Empty ledger is valid
        report["valid"] = True
        return report

    # ---------------------------------------------------------------
    # 3. Required fields
    # ---------------------------------------------------------------
    _REQUIRED = {"entry_id", "timestamp", "event_type", "agent_id",
                 "details", "prev_hash", "entry_hash"}
    for idx, entry in enumerate(entries):
        missing = _REQUIRED - entry.keys()
        if missing:
            errors.append(
                f"Entry {idx}: missing required fields {sorted(missing)}"
            )

    # ---------------------------------------------------------------
    # 4. Genesis prev_hash
    # ---------------------------------------------------------------
    if entries[0].get("prev_hash") != _GENESIS_PREV_HASH:
        errors.append(
            f"Entry 0: prev_hash should be {_GENESIS_PREV_HASH!r}, "
            f"got {entries[0].get('prev_hash')!r}"
        )

    # ---------------------------------------------------------------
    # 5 & 6. Hash chain integrity + chain linking
    # ---------------------------------------------------------------
    for idx, entry in enumerate(entries):
        # 5. Verify this entry's own hash
        try:
            expected = _recompute_entry_hash(entry)
            if entry.get("entry_hash") != expected:
                errors.append(
                    f"Entry {idx} (id={entry.get('entry_id', '?')}): "
                    f"entry_hash mismatch — stored {entry.get('entry_hash')!r}, "
                    f"expected {expected!r}"
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Entry {idx}: cannot recompute hash — {exc}")

        # 6. Verify chain link to next entry
        if idx + 1 < len(entries):
            next_prev = entries[idx + 1].get("prev_hash")
            this_hash = entry.get("entry_hash")
            if next_prev != this_hash:
                errors.append(
                    f"Chain break between entry {idx} and {idx + 1}: "
                    f"entry {idx + 1} prev_hash={next_prev!r} != "
                    f"entry {idx} entry_hash={this_hash!r}"
                )

    # ---------------------------------------------------------------
    # 7. Timestamp ordering (best-effort — warn, don't fail hard)
    # ---------------------------------------------------------------
    prev_ts: Optional[str] = None
    for idx, entry in enumerate(entries):
        ts = entry.get("timestamp")
        if ts is None:
            errors.append(f"Entry {idx}: missing timestamp")
            continue
        if prev_ts is not None and ts < prev_ts:
            errors.append(
                f"Entry {idx}: timestamp {ts!r} is before previous {prev_ts!r}"
            )
        prev_ts = ts

    # ---------------------------------------------------------------
    # Summary statistics
    # ---------------------------------------------------------------
    report["first_entry_ts"] = entries[0].get("timestamp")
    report["last_entry_ts"] = entries[-1].get("timestamp")

    counts: Dict[str, int] = {}
    for entry in entries:
        et = entry.get("event_type", "unknown")
        counts[et] = counts.get(et, 0) + 1
    report["event_type_counts"] = counts

    report["valid"] = len(errors) == 0
    return report
