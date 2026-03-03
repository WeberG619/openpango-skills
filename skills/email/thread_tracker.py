#!/usr/bin/env python3
"""
thread_tracker.py - SQLite-backed email conversation thread tracking.

Tracks email conversations using RFC 2822 thread headers:
  - Message-ID  — unique identifier for each message
  - In-Reply-To — immediate parent message
  - References  — full ancestor chain

All messages are stored in a local SQLite DB with FTS5 full-text search.
"""

import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DB = Path.home() / ".openclaw" / "workspace" / "email" / "threads.db"

# ------------------------------------------------------------------ #
#  Schema                                                               #
# ------------------------------------------------------------------ #

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS threads (
    thread_id   TEXT PRIMARY KEY,
    subject     TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    message_id      TEXT PRIMARY KEY,
    thread_id       TEXT NOT NULL REFERENCES threads(thread_id),
    from_addr       TEXT NOT NULL DEFAULT '',
    to_addr         TEXT NOT NULL DEFAULT '',   -- JSON array
    cc_addr         TEXT NOT NULL DEFAULT '',   -- JSON array
    subject         TEXT NOT NULL DEFAULT '',
    body_preview    TEXT NOT NULL DEFAULT '',
    timestamp       REAL NOT NULL,
    in_reply_to     TEXT NOT NULL DEFAULT '',
    references_json TEXT NOT NULL DEFAULT '[]',
    imap_uid        TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (thread_id) REFERENCES threads(thread_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_in_reply_to ON messages(in_reply_to);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);

-- FTS5 virtual table for full-text search across subject + body_preview
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    message_id UNINDEXED,
    subject,
    body_preview,
    from_addr,
    content='messages',
    content_rowid='rowid'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, message_id, subject, body_preview, from_addr)
    VALUES (new.rowid, new.message_id, new.subject, new.body_preview, new.from_addr);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, message_id, subject, body_preview, from_addr)
    VALUES ('delete', old.rowid, old.message_id, old.subject, old.body_preview, old.from_addr);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, message_id, subject, body_preview, from_addr)
    VALUES ('delete', old.rowid, old.message_id, old.subject, old.body_preview, old.from_addr);
    INSERT INTO messages_fts(rowid, message_id, subject, body_preview, from_addr)
    VALUES (new.rowid, new.message_id, new.subject, new.body_preview, new.from_addr);
END;
"""


class ThreadTracker:
    """
    Tracks email conversations via SQLite with FTS5 search.

    Thread linkage algorithm:
      1. If *in_reply_to* matches a known message_id → same thread.
      2. Else check *references* list (any match) → same thread.
      3. Else check if *subject* (normalised: strip Re:/Fwd:) matches an
         existing thread within 30 days → assume same thread.
      4. Else create a new thread.

    Usage::

        tracker = ThreadTracker()
        tracker.record_message(parsed_msg_dict)
        thread = tracker.get_thread("<abc@example.com>")
        recent = tracker.get_threads(limit=10)
        results = tracker.search_threads("invoice")
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else _DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self):
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------ #
    #  Schema init                                                          #
    # ------------------------------------------------------------------ #

    def _init_schema(self):
        with self._conn:
            self._conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------ #
    #  Recording                                                            #
    # ------------------------------------------------------------------ #

    def record_message(self, message: dict) -> str:
        """
        Store a message and link it to the correct thread.

        Args:
            message: Parsed email dict (from IMAPClient._parse_email or SMTPClient.send_message).
                     Expected keys: message_id, from, to, cc, subject,
                     body_text/body_html, date, in_reply_to, references.

        Returns:
            thread_id that the message was linked to.
        """
        message_id = (message.get("message_id") or "").strip()
        if not message_id:
            message_id = f"<generated-{uuid.uuid4().hex}@openpango.local>"

        # Check for duplicate
        if self._message_exists(message_id):
            row = self._conn.execute(
                "SELECT thread_id FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            return row["thread_id"] if row else ""

        in_reply_to = (message.get("in_reply_to") or "").strip()
        references = message.get("references") or []
        subject = message.get("subject") or ""
        timestamp = self._parse_timestamp(message.get("date"))

        # Resolve thread_id
        thread_id = self._resolve_thread(in_reply_to, references, subject, timestamp)

        # Prepare fields
        to_json = json.dumps(message.get("to") or [])
        cc_json = json.dumps(message.get("cc") or [])
        body = message.get("body_text") or message.get("body_html") or ""
        body_preview = body[:500].strip()
        refs_json = json.dumps(references)

        with self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO messages
                    (message_id, thread_id, from_addr, to_addr, cc_addr, subject,
                     body_preview, timestamp, in_reply_to, references_json, imap_uid)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    thread_id,
                    message.get("from") or "",
                    to_json,
                    cc_json,
                    subject,
                    body_preview,
                    timestamp,
                    in_reply_to,
                    refs_json,
                    message.get("imap_uid") or "",
                ),
            )
            # Update thread metadata
            self._conn.execute(
                """
                UPDATE threads SET
                    updated_at = MAX(updated_at, ?),
                    message_count = (SELECT COUNT(*) FROM messages WHERE thread_id = ?)
                WHERE thread_id = ?
                """,
                (timestamp, thread_id, thread_id),
            )

        logger.debug("Recorded message %s → thread %s", message_id, thread_id)
        return thread_id

    # ------------------------------------------------------------------ #
    #  Thread resolution                                                    #
    # ------------------------------------------------------------------ #

    def _resolve_thread(
        self,
        in_reply_to: str,
        references: List[str],
        subject: str,
        timestamp: float,
    ) -> str:
        """Find an existing thread or create a new one."""

        # 1. In-Reply-To match
        if in_reply_to:
            row = self._conn.execute(
                "SELECT thread_id FROM messages WHERE message_id = ?",
                (in_reply_to,),
            ).fetchone()
            if row:
                return row["thread_id"]

        # 2. References match (any ancestor)
        for ref in references:
            ref = ref.strip()
            if not ref:
                continue
            row = self._conn.execute(
                "SELECT thread_id FROM messages WHERE message_id = ?", (ref,)
            ).fetchone()
            if row:
                return row["thread_id"]

        # 3. Subject match within 30 days (Re:/Fwd: stripped)
        normalised_subject = _normalise_subject(subject)
        if normalised_subject:
            cutoff = timestamp - 30 * 86400
            row = self._conn.execute(
                """
                SELECT t.thread_id FROM threads t
                JOIN messages m ON m.thread_id = t.thread_id
                WHERE ? LIKE '%' || LOWER(m.subject) || '%'
                   OR LOWER(m.subject) LIKE '%' || ? || '%'
                  AND m.timestamp >= ?
                ORDER BY m.timestamp DESC LIMIT 1
                """,
                (normalised_subject, normalised_subject, cutoff),
            ).fetchone()
            if row:
                return row["thread_id"]

        # 4. Create new thread
        return self._create_thread(subject, timestamp)

    def _create_thread(self, subject: str, timestamp: float) -> str:
        thread_id = str(uuid.uuid4())
        with self._conn:
            self._conn.execute(
                "INSERT INTO threads (thread_id, subject, created_at, updated_at, message_count) "
                "VALUES (?, ?, ?, ?, 0)",
                (thread_id, subject, timestamp, timestamp),
            )
        return thread_id

    def _message_exists(self, message_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ #
    #  Retrieval                                                            #
    # ------------------------------------------------------------------ #

    def get_thread(self, message_id: str) -> List[dict]:
        """
        Return all messages belonging to the same thread as *message_id*.

        Args:
            message_id: The Message-ID of any message in the thread.

        Returns:
            Chronologically ordered list of message dicts.
        """
        row = self._conn.execute(
            "SELECT thread_id FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        if not row:
            return []
        return self._get_thread_by_id(row["thread_id"])

    def _get_thread_by_id(self, thread_id: str) -> List[dict]:
        rows = self._conn.execute(
            """
            SELECT message_id, from_addr, to_addr, cc_addr, subject,
                   body_preview, timestamp, in_reply_to, references_json, imap_uid
            FROM messages WHERE thread_id = ?
            ORDER BY timestamp ASC
            """,
            (thread_id,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_threads(self, limit: int = 20) -> List[dict]:
        """
        Return recent thread summaries ordered by last activity.

        Returns:
            List of dicts: thread_id, subject, message_count, updated_at,
            last_from, last_preview.
        """
        rows = self._conn.execute(
            """
            SELECT t.thread_id, t.subject, t.message_count,
                   t.updated_at, t.created_at,
                   m.from_addr as last_from,
                   m.body_preview as last_preview
            FROM threads t
            LEFT JOIN messages m ON m.message_id = (
                SELECT message_id FROM messages
                WHERE thread_id = t.thread_id
                ORDER BY timestamp DESC LIMIT 1
            )
            ORDER BY t.updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "thread_id": r["thread_id"],
                "subject": r["subject"],
                "message_count": r["message_count"],
                "updated_at": datetime.fromtimestamp(r["updated_at"], tz=timezone.utc).isoformat(),
                "created_at": datetime.fromtimestamp(r["created_at"], tz=timezone.utc).isoformat(),
                "last_from": r["last_from"] or "",
                "last_preview": r["last_preview"] or "",
            }
            for r in rows
        ]

    def search_threads(self, query: str) -> List[dict]:
        """
        Full-text search across subject, body_preview, and from_addr.

        Args:
            query: Search string (FTS5 query syntax supported).

        Returns:
            List of matching message dicts ordered by relevance.
        """
        if not query or not query.strip():
            return []
        try:
            rows = self._conn.execute(
                """
                SELECT m.message_id, m.from_addr, m.to_addr, m.cc_addr, m.subject,
                       m.body_preview, m.timestamp, m.in_reply_to, m.references_json,
                       m.imap_uid
                FROM messages_fts fts
                JOIN messages m ON m.rowid = fts.rowid
                WHERE messages_fts MATCH ?
                ORDER BY rank
                LIMIT 50
                """,
                (query,),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        except sqlite3.OperationalError as exc:
            logger.warning("FTS search error for '%s': %s", query, exc)
            return []

    def get_message(self, message_id: str) -> Optional[dict]:
        """Return a single message dict by its Message-ID."""
        row = self._conn.execute(
            """
            SELECT message_id, from_addr, to_addr, cc_addr, subject,
                   body_preview, timestamp, in_reply_to, references_json, imap_uid
            FROM messages WHERE message_id = ?
            """,
            (message_id,),
        ).fetchone()
        return self._row_to_dict(row) if row else None

    # ------------------------------------------------------------------ #
    #  Helpers                                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _row_to_dict(row) -> dict:
        if not row:
            return {}
        d = dict(row)
        for key in ("to_addr", "cc_addr", "references_json"):
            if key in d and d[key]:
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    d[key] = []
        if "timestamp" in d and d["timestamp"]:
            try:
                d["timestamp"] = datetime.fromtimestamp(d["timestamp"], tz=timezone.utc).isoformat()
            except (ValueError, OSError):
                pass
        return d

    @staticmethod
    def _parse_timestamp(date_str: Optional[str]) -> float:
        """Parse email date string to Unix timestamp. Returns current time on failure."""
        if not date_str:
            return time.time()
        from email.utils import parsedate_to_datetime
        try:
            dt = parsedate_to_datetime(date_str)
            return dt.timestamp()
        except Exception:
            return time.time()


def _normalise_subject(subject: str) -> str:
    """Strip Re:/Fwd: prefixes and lowercase for comparison."""
    import re
    s = re.sub(r"^(re|fwd?|aw|sv|fw):\s*", "", subject.strip(), flags=re.IGNORECASE)
    return s.lower().strip()
