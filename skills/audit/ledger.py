"""
skills/audit/ledger.py

Low-level append-only JSONL ledger with SHA-256 hash chain.

Each entry written to disk looks like:
    {
        "timestamp": "2026-01-01T12:00:00.000000Z",
        "event_type": "tool/invoke",
        ...caller-supplied fields...,
        "prev_hash": "abc123...",   # SHA-256 of previous entry (64 hex chars)
        "entry_hash": "def456..."   # SHA-256 of (prev_hash + canonical JSON)
    }

The genesis entry uses prev_hash = "0" * 64.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GENESIS_PREV_HASH = "0" * 64


def _canonical_json(obj: dict) -> str:
    """Deterministic JSON serialisation (sorted keys, no extra whitespace)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _compute_hash(prev_hash: str, entry_data: dict) -> str:
    """
    SHA-256( prev_hash + canonical_json(entry_data) )

    entry_data must NOT yet contain 'prev_hash' or 'entry_hash' fields —
    those are injected after this function runs.
    """
    payload = prev_hash + _canonical_json(entry_data)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _recompute_entry_hash(entry: dict) -> str:
    """
    Recompute the expected entry_hash for a stored entry.

    Strips 'entry_hash' from the entry before hashing so the calculation
    matches what was done at write time.
    """
    prev_hash = entry["prev_hash"]
    data_without_hashes = {k: v for k, v in entry.items() if k != "entry_hash"}
    return _compute_hash(prev_hash, data_without_hashes)


# ---------------------------------------------------------------------------
# HashChainLedger
# ---------------------------------------------------------------------------


class HashChainLedger:
    """
    Append-only JSONL ledger with SHA-256 hash chaining.

    Thread-safe via an internal threading.Lock.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    def append(self, entry_data: dict) -> dict:
        """
        Hash-chain the entry_data dict and append it to the ledger.

        Returns the full stored entry (with prev_hash and entry_hash).
        The caller's dict is NOT mutated.
        """
        with self._lock:
            prev_hash = self._last_hash()
            # Build the entry without entry_hash first (used in hash input)
            entry = dict(entry_data)
            entry["prev_hash"] = prev_hash
            # Compute hash BEFORE adding entry_hash to the dict
            entry_hash = _compute_hash(prev_hash, entry)
            entry["entry_hash"] = entry_hash
            line = _canonical_json(entry)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            return entry

    def read_all(self) -> List[dict]:
        """Return all entries in append order."""
        with self._lock:
            return self._read_all_unlocked()

    def read_last(self, n: int) -> List[dict]:
        """Return the last *n* entries in append order."""
        if n <= 0:
            return []
        with self._lock:
            entries = self._read_all_unlocked()
        return entries[-n:]

    def verify_chain(self) -> Tuple[bool, Optional[str]]:
        """
        Walk the entire chain and validate every hash link.

        Returns:
            (True, None)                  — chain is intact
            (False, "human-readable msg") — first integrity violation found
        """
        with self._lock:
            entries = self._read_all_unlocked()

        if not entries:
            return True, None

        # Verify genesis prev_hash
        if entries[0]["prev_hash"] != _GENESIS_PREV_HASH:
            return False, (
                f"Entry 0: expected prev_hash={_GENESIS_PREV_HASH!r}, "
                f"got {entries[0]['prev_hash']!r}"
            )

        for idx, entry in enumerate(entries):
            expected_hash = _recompute_entry_hash(entry)
            if entry.get("entry_hash") != expected_hash:
                return False, (
                    f"Entry {idx}: entry_hash mismatch — "
                    f"expected {expected_hash!r}, stored {entry.get('entry_hash')!r}"
                )

            # Verify chain link (prev_hash of next == entry_hash of current)
            if idx + 1 < len(entries):
                next_entry = entries[idx + 1]
                if next_entry["prev_hash"] != entry["entry_hash"]:
                    return False, (
                        f"Entry {idx + 1}: prev_hash does not match "
                        f"entry_hash of entry {idx}"
                    )

        return True, None

    def entry_count(self) -> int:
        """Return total number of entries (fast path — just counts lines)."""
        with self._lock:
            return self._count_lines()

    # ------------------------------------------------------------------
    # Internal helpers (must be called with self._lock held)
    # ------------------------------------------------------------------

    def _read_all_unlocked(self) -> List[dict]:
        if not self._path.exists():
            return []
        entries: List[dict] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entries.append(json.loads(raw))
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Corrupt JSONL at line {lineno}: {exc}"
                    ) from exc
        return entries

    def _last_hash(self) -> str:
        """Return the entry_hash of the last entry, or GENESIS_PREV_HASH."""
        if not self._path.exists():
            return _GENESIS_PREV_HASH
        last_line: Optional[str] = None
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    last_line = stripped
        if last_line is None:
            return _GENESIS_PREV_HASH
        try:
            last_entry = json.loads(last_line)
            return last_entry["entry_hash"]
        except (json.JSONDecodeError, KeyError):
            return _GENESIS_PREV_HASH

    def _count_lines(self) -> int:
        if not self._path.exists():
            return 0
        count = 0
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    count += 1
        return count
