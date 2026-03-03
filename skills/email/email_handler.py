#!/usr/bin/env python3
"""
email_handler.py - Unified IMAP+SMTP facade for the OpenPango email skill.

EmailHandler is the primary interface agents use.  It orchestrates:
  - IMAPClient  — reading, searching, marking
  - SMTPClient  — sending with full MIME support
  - ThreadTracker — conversation history
"""

import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .imap_client import IMAPClient
from .smtp_client import SMTPClient
from .thread_tracker import ThreadTracker

logger = logging.getLogger(__name__)


class EmailHandler:
    """
    Unified email management interface.

    Config dict format::

        {
          "imap": {"host": "imap.gmail.com", "port": 993, "use_ssl": True},
          "smtp": {"host": "smtp.gmail.com", "port": 587, "use_tls": True},
          "username": "me@gmail.com",
          "password": "app-password",
        }

    Usage::

        with EmailHandler(config) as handler:
            msgs = handler.fetch_unread(limit=10)
            handler.send("them@example.com", "Hello", "Message body")
            thread = handler.get_thread("<msgid@example.com>")

    Context manager calls connect() / disconnect() automatically.
    """

    def __init__(self, config: dict):
        """
        Args:
            config: IMAP/SMTP config dict (see class docstring).
        """
        self.config = config
        self._username: str = config.get("username", "")
        self._password: str = config.get("password", "")

        imap_cfg = config.get("imap", {})
        smtp_cfg = config.get("smtp", {})

        self.imap = IMAPClient(
            host=imap_cfg.get("host", ""),
            port=imap_cfg.get("port", 993),
            use_ssl=imap_cfg.get("use_ssl", True),
        )
        self.smtp = SMTPClient(
            host=smtp_cfg.get("host", ""),
            port=smtp_cfg.get("port", 587),
            use_tls=smtp_cfg.get("use_tls", True),
        )
        self.tracker = ThreadTracker()
        self._connected = False

    # ------------------------------------------------------------------ #
    #  Context manager                                                      #
    # ------------------------------------------------------------------ #

    def __enter__(self) -> "EmailHandler":
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()

    # ------------------------------------------------------------------ #
    #  Connection lifecycle                                                 #
    # ------------------------------------------------------------------ #

    def connect(self) -> bool:
        """
        Establish IMAP and SMTP connections and authenticate.

        Returns:
            True if both connections succeed.
        """
        imap_ok = self.imap.login(self._username, self._password)
        smtp_ok = self.smtp.login(self._username, self._password)
        self._connected = imap_ok and smtp_ok
        if not imap_ok:
            logger.error("IMAP connection/login failed for %s", self._username)
        if not smtp_ok:
            logger.error("SMTP connection/login failed for %s", self._username)
        return self._connected

    def disconnect(self):
        """Close IMAP and SMTP connections."""
        self.imap.logout()
        self.smtp.quit()
        self._connected = False

    # ------------------------------------------------------------------ #
    #  Reading                                                              #
    # ------------------------------------------------------------------ #

    def fetch_unread(self, folder: str = "INBOX", limit: int = 50) -> List[dict]:
        """
        Fetch unread messages from *folder*.

        Args:
            folder: Mailbox name (default "INBOX").
            limit:  Maximum number of messages to return.

        Returns:
            List of structured message dicts, newest first.
        """
        self.imap.select_folder(folder)
        messages = self.imap.fetch_messages("UNSEEN", limit=limit)
        for msg in messages:
            try:
                self.tracker.record_message(msg)
            except Exception as exc:
                logger.debug("Thread tracking failed for message: %s", exc)
        return messages

    def fetch_all(self, folder: str = "INBOX", limit: int = 50) -> List[dict]:
        """Fetch all messages (not just unread) from *folder*."""
        self.imap.select_folder(folder)
        messages = self.imap.fetch_messages("ALL", limit=limit)
        for msg in messages:
            try:
                self.tracker.record_message(msg)
            except Exception as exc:
                logger.debug("Thread tracking failed: %s", exc)
        return messages

    # ------------------------------------------------------------------ #
    #  Sending                                                              #
    # ------------------------------------------------------------------ #

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        attachments: Optional[List[Path]] = None,
        html: bool = False,
    ) -> dict:
        """
        Send a new email message.

        Args:
            to:          Recipient address.
            subject:     Email subject line.
            body:        Message body (plain text or HTML).
            cc:          List of CC addresses (optional).
            bcc:         List of BCC addresses (optional).
            attachments: List of Path objects to attach (optional).
            html:        If True, body is treated as HTML.

        Returns:
            Result dict with keys: status, message_id, recipients, timestamp.
        """
        result = self.smtp.send_message(
            from_addr=self._username,
            to=[to],
            subject=subject,
            body=body,
            cc=cc or [],
            bcc=bcc or [],
            attachments=attachments or [],
            html=html,
        )
        if result.get("status") == "sent":
            # Record sent message in thread tracker
            sent_msg = {
                "message_id": result.get("message_id", ""),
                "from": self._username,
                "to": [to] + (cc or []),
                "cc": cc or [],
                "subject": subject,
                "body_text": body if not html else "",
                "body_html": body if html else "",
                "date": result.get("timestamp", ""),
                "in_reply_to": "",
                "references": [],
            }
            try:
                self.tracker.record_message(sent_msg)
            except Exception as exc:
                logger.debug("Thread tracking for sent message failed: %s", exc)
        return result

    # ------------------------------------------------------------------ #
    #  Replying                                                             #
    # ------------------------------------------------------------------ #

    def reply(
        self,
        original_message_id: str,
        body: str,
        reply_all: bool = False,
        html: bool = False,
    ) -> dict:
        """
        Reply to an existing message, maintaining thread headers.

        Args:
            original_message_id: Message-ID of the message to reply to.
            body:                Reply body text.
            reply_all:           If True, CC all original recipients.
            html:                If True, body is treated as HTML.

        Returns:
            Result dict from smtp_client.send_message, or error dict.
        """
        # Look up the original message in thread tracker
        original = self.tracker.get_message(original_message_id)

        if not original:
            # Try fetching from IMAP
            original = self._find_message_in_imap(original_message_id)

        if not original:
            return {
                "status": "error",
                "error": f"Original message not found: {original_message_id}",
            }

        # Determine reply-to address
        reply_to = original.get("from", "")
        to = [reply_to] if reply_to else []

        # Reply-All: add original To + CC minus ourselves
        cc: List[str] = []
        if reply_all:
            original_to = original.get("to_addr") or original.get("to") or []
            original_cc = original.get("cc_addr") or original.get("cc") or []
            if isinstance(original_to, str):
                original_to = [original_to]
            if isinstance(original_cc, str):
                original_cc = [original_cc]
            all_addrs = original_to + original_cc
            cc = [a for a in all_addrs if a and a.lower() != self._username.lower()]

        # Build References header: original References + In-Reply-To
        original_refs = original.get("references_json") or original.get("references") or []
        if isinstance(original_refs, str):
            import json
            try:
                original_refs = json.loads(original_refs)
            except Exception:
                original_refs = []
        references = list(original_refs)
        if original_message_id not in references:
            references.append(original_message_id)

        # Prefix subject
        orig_subject = original.get("subject", "")
        if not orig_subject.lower().startswith("re:"):
            subject = f"Re: {orig_subject}"
        else:
            subject = orig_subject

        result = self.smtp.send_message(
            from_addr=self._username,
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            in_reply_to=original_message_id,
            references=references,
            html=html,
        )

        if result.get("status") == "sent":
            sent_msg = {
                "message_id": result.get("message_id", ""),
                "from": self._username,
                "to": to,
                "cc": cc,
                "subject": subject,
                "body_text": body if not html else "",
                "body_html": body if html else "",
                "date": result.get("timestamp", ""),
                "in_reply_to": original_message_id,
                "references": references,
            }
            try:
                self.tracker.record_message(sent_msg)
            except Exception as exc:
                logger.debug("Thread tracking for reply failed: %s", exc)

        return result

    # ------------------------------------------------------------------ #
    #  Searching                                                            #
    # ------------------------------------------------------------------ #

    def search(self, query: str, folder: str = "INBOX") -> List[dict]:
        """
        Search for messages matching *query* using IMAP SEARCH.

        For subject-based queries use "SUBJECT keyword".
        For full text (where server supports it): "BODY keyword".
        Also searches local thread tracker via FTS.

        Args:
            query:  IMAP search criteria string, e.g. "SUBJECT invoice".
            folder: Mailbox to search (default "INBOX").

        Returns:
            Deduplicated list of message dicts.
        """
        self.imap.select_folder(folder)
        imap_results = self.imap.fetch_messages(query, limit=100)

        # Also search local thread DB
        fts_results = self.tracker.search_threads(query)

        # Merge by message_id, IMAP wins for full content
        seen = {m.get("message_id"): m for m in imap_results}
        for msg in fts_results:
            mid = msg.get("message_id")
            if mid and mid not in seen:
                seen[mid] = msg

        return list(seen.values())

    # ------------------------------------------------------------------ #
    #  Thread retrieval                                                     #
    # ------------------------------------------------------------------ #

    def get_thread(self, message_id: str) -> List[dict]:
        """
        Return all messages in the same conversation as *message_id*.

        Args:
            message_id: The Message-ID of any message in the thread.

        Returns:
            Chronologically ordered list of message dicts.
        """
        return self.tracker.get_thread(message_id)

    def get_threads(self, limit: int = 20) -> List[dict]:
        """Return recent thread summaries."""
        return self.tracker.get_threads(limit=limit)

    # ------------------------------------------------------------------ #
    #  Marking                                                              #
    # ------------------------------------------------------------------ #

    def mark_read(self, message_ids: List[str]) -> bool:
        """Mark *message_ids* as read (\\Seen flag)."""
        return self.imap.mark_seen(message_ids)

    def mark_unread(self, message_ids: List[str]) -> bool:
        """Mark *message_ids* as unread (remove \\Seen flag)."""
        return self.imap.mark_unseen(message_ids)

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _find_message_in_imap(self, message_id: str) -> Optional[dict]:
        """Search IMAP for a message by its Message-ID header."""
        clean = message_id.strip("<>")
        results = self.imap.fetch_messages(f'HEADER Message-ID "{clean}"', limit=5)
        if results:
            return results[0]
        # Broader search
        results = self.imap.fetch_messages(f'HEADER Message-ID "{message_id}"', limit=5)
        return results[0] if results else None
