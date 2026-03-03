#!/usr/bin/env python3
"""
imap_client.py - IMAP connection, polling, and IDLE support for OpenPango email skill.

Provides IMAPClient with:
  - SSL/TLS connections to any IMAP server
  - Folder selection and message fetching
  - RFC 2822 email parsing into structured dicts
  - Polling loop and IMAP IDLE support
  - Full attachment extraction
"""

import imaplib
import email
import email.policy
import email.header
import ssl
import sys
import threading
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


def _decode_header_value(value: Optional[str]) -> str:
    """Decode a potentially RFC 2047-encoded header value to a plain string."""
    if not value:
        return ""
    parts = []
    for chunk, charset in email.header.decode_header(value):
        if isinstance(chunk, bytes):
            try:
                parts.append(chunk.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                parts.append(chunk.decode("latin-1", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts)


def _parse_address_list(header_value: str) -> List[str]:
    """Parse a comma-separated address header into a list of strings."""
    if not header_value:
        return []
    return [addr.strip() for addr in header_value.split(",") if addr.strip()]


class IMAPClient:
    """
    Low-level IMAP client.

    Usage::

        client = IMAPClient("imap.gmail.com")
        client.login("user@gmail.com", "app-password")
        client.select_folder("INBOX")
        messages = client.fetch_messages("UNSEEN", limit=20)
        client.logout()
    """

    def __init__(self, host: str, port: int = 993, use_ssl: bool = True):
        """
        Args:
            host:    IMAP server hostname.
            port:    Server port. Defaults to 993 (imaps).
            use_ssl: Use SSL/TLS from the start. Set False for STARTTLS on port 143.
        """
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self._conn: Optional[imaplib.IMAP4] = None
        self._lock = threading.Lock()
        self._polling = False
        self._current_folder: Optional[str] = None

    # ------------------------------------------------------------------ #
    #  Connection                                                           #
    # ------------------------------------------------------------------ #

    def connect(self) -> bool:
        """Open the IMAP connection (without logging in)."""
        try:
            if self.use_ssl:
                ctx = ssl.create_default_context()
                self._conn = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ctx)
            else:
                self._conn = imaplib.IMAP4(self.host, self.port)
                self._conn.starttls()
            logger.debug("IMAP connected to %s:%d", self.host, self.port)
            return True
        except (imaplib.IMAP4.error, OSError) as exc:
            logger.error("IMAP connection failed: %s", exc)
            return False

    def login(self, username: str, password: str) -> bool:
        """
        Authenticate with the server.  Calls connect() automatically if needed.

        Args:
            username: Email address / IMAP username.
            password: Password or app-specific password.

        Returns:
            True on success, False on authentication failure.
        """
        if self._conn is None:
            if not self.connect():
                return False
        try:
            self._conn.login(username, password)
            logger.debug("IMAP login OK for %s", username)
            return True
        except imaplib.IMAP4.error as exc:
            logger.error("IMAP login failed for %s: %s", username, exc)
            return False

    def logout(self):
        """Close the IMAP connection gracefully."""
        if self._conn is None:
            return
        try:
            self._polling = False
            self._conn.logout()
        except Exception:  # pragma: no cover
            pass
        finally:
            self._conn = None
            self._current_folder = None

    # ------------------------------------------------------------------ #
    #  Folder management                                                    #
    # ------------------------------------------------------------------ #

    def list_folders(self) -> List[str]:
        """Return a list of available folder/mailbox names."""
        if self._conn is None:
            return []
        try:
            status, data = self._conn.list()
            if status != "OK":
                return []
            folders = []
            for item in data:
                if isinstance(item, bytes):
                    # Format: (\HasNoChildren) "/" "INBOX"
                    parts = item.decode("utf-8").split('"')
                    name = parts[-1].strip().strip('"') if len(parts) >= 2 else item.decode()
                    folders.append(name)
            return folders
        except imaplib.IMAP4.error as exc:
            logger.error("IMAP list folders failed: %s", exc)
            return []

    def select_folder(self, folder: str = "INBOX") -> int:
        """
        Select a folder and return the total message count.

        Args:
            folder: Folder name (e.g. "INBOX", "[Gmail]/Sent Mail").

        Returns:
            Number of messages in the folder, or -1 on error.
        """
        if self._conn is None:
            return -1
        try:
            status, data = self._conn.select(f'"{folder}"')
            if status != "OK":
                logger.warning("Could not select folder %s: %s", folder, data)
                return -1
            self._current_folder = folder
            count = int(data[0]) if data and data[0] else 0
            logger.debug("Selected folder %s (%d messages)", folder, count)
            return count
        except (imaplib.IMAP4.error, ValueError) as exc:
            logger.error("IMAP select folder error: %s", exc)
            return -1

    # ------------------------------------------------------------------ #
    #  Fetching                                                             #
    # ------------------------------------------------------------------ #

    def fetch_messages(
        self,
        criteria: str = "UNSEEN",
        limit: int = 50,
        mark_seen: bool = False,
    ) -> List[dict]:
        """
        Search and fetch messages matching *criteria*.

        Args:
            criteria:  IMAP search string, e.g. "UNSEEN", "ALL", "SUBJECT hello".
            limit:     Maximum number of messages to return (most recent first).
            mark_seen: If True, set the \\Seen flag on fetched messages.

        Returns:
            List of parsed message dicts (see _parse_email).
        """
        if self._conn is None:
            return []
        try:
            with self._lock:
                status, data = self._conn.search(None, criteria)
            if status != "OK":
                return []

            message_ids = data[0].split() if data and data[0] else []
            if not message_ids:
                return []

            # Most recent first, bounded by limit
            message_ids = message_ids[-limit:][::-1]

            results = []
            for uid in message_ids:
                msg_dict = self._fetch_single(uid, mark_seen=mark_seen)
                if msg_dict:
                    results.append(msg_dict)
            return results

        except imaplib.IMAP4.error as exc:
            logger.error("IMAP fetch error: %s", exc)
            return []

    def _fetch_single(self, uid: bytes, mark_seen: bool = False) -> Optional[dict]:
        """Fetch and parse one message by UID."""
        try:
            with self._lock:
                fetch_parts = "(RFC822)" if mark_seen else "(BODY.PEEK[])"
                status, data = self._conn.fetch(uid, fetch_parts)
            if status != "OK" or not data or data[0] is None:
                return None
            raw = data[0][1] if isinstance(data[0], tuple) else data[0]
            parsed = self._parse_email(raw)
            parsed["imap_uid"] = uid.decode("utf-8") if isinstance(uid, bytes) else str(uid)
            return parsed
        except (imaplib.IMAP4.error, IndexError, TypeError) as exc:
            logger.warning("Failed to fetch message %s: %s", uid, exc)
            return None

    def _parse_email(self, raw: bytes) -> dict:
        """
        Parse a raw RFC 2822 email message into a structured dict.

        Returns a dict with keys:
          message_id, from, to, cc, bcc, reply_to, subject, date,
          body_text, body_html, attachments, in_reply_to, references,
          raw_size
        """
        try:
            msg = email.message_from_bytes(raw, policy=email.policy.compat32)
        except Exception as exc:
            logger.error("Failed to parse raw email: %s", exc)
            return {}

        body_text = ""
        body_html = ""
        attachments = []

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                disposition = str(part.get("Content-Disposition", ""))
                filename = part.get_filename()

                if filename or "attachment" in disposition:
                    attachments.append(self._extract_attachment_meta(part))
                elif content_type == "text/plain" and not body_text:
                    body_text = self._decode_part(part)
                elif content_type == "text/html" and not body_html:
                    body_html = self._decode_part(part)
        else:
            content_type = msg.get_content_type()
            if content_type == "text/html":
                body_html = self._decode_part(msg)
            else:
                body_text = self._decode_part(msg)

        # Normalise the References header into a list
        refs_raw = msg.get("References", "")
        references = [r.strip() for r in refs_raw.split() if r.strip()]

        return {
            "message_id": _decode_header_value(msg.get("Message-ID", "")).strip(),
            "from": _decode_header_value(msg.get("From", "")),
            "to": _parse_address_list(_decode_header_value(msg.get("To", ""))),
            "cc": _parse_address_list(_decode_header_value(msg.get("Cc", ""))),
            "bcc": _parse_address_list(_decode_header_value(msg.get("Bcc", ""))),
            "reply_to": _decode_header_value(msg.get("Reply-To", "")),
            "subject": _decode_header_value(msg.get("Subject", "(no subject)")),
            "date": _decode_header_value(msg.get("Date", "")),
            "body_text": body_text,
            "body_html": body_html,
            "attachments": attachments,
            "in_reply_to": _decode_header_value(msg.get("In-Reply-To", "")).strip(),
            "references": references,
            "raw_size": len(raw),
        }

    @staticmethod
    def _decode_part(part) -> str:
        """Decode a message part's payload to a string."""
        payload = part.get_payload(decode=True)
        if not payload:
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            return payload.decode("utf-8", errors="replace")

    @staticmethod
    def _extract_attachment_meta(part) -> dict:
        """Return metadata dict for an attachment part (does not include raw bytes)."""
        filename = _decode_header_value(part.get_filename() or "attachment")
        content_type = part.get_content_type()
        payload = part.get_payload(decode=True) or b""
        return {
            "filename": filename,
            "content_type": content_type,
            "size": len(payload),
        }

    # ------------------------------------------------------------------ #
    #  Mark read / unread                                                   #
    # ------------------------------------------------------------------ #

    def mark_seen(self, uids: List[str]) -> bool:
        """Mark messages as \\Seen."""
        return self._store_flags(uids, "+FLAGS", "\\Seen")

    def mark_unseen(self, uids: List[str]) -> bool:
        """Remove the \\Seen flag."""
        return self._store_flags(uids, "-FLAGS", "\\Seen")

    def _store_flags(self, uids: List[str], command: str, flag: str) -> bool:
        if not self._conn or not uids:
            return False
        uid_str = ",".join(uids)
        try:
            with self._lock:
                status, _ = self._conn.store(uid_str, command, flag)
            return status == "OK"
        except imaplib.IMAP4.error as exc:
            logger.error("IMAP store flags failed: %s", exc)
            return False

    # ------------------------------------------------------------------ #
    #  Polling                                                              #
    # ------------------------------------------------------------------ #

    def poll(self, interval: int = 30, callback: Optional[Callable] = None):
        """
        Blocking polling loop.  Checks for UNSEEN messages every *interval* seconds.
        Calls *callback(messages)* when new messages arrive.

        Stop by setting self._polling = False from another thread, or Ctrl-C.
        """
        self._polling = True
        logger.info("IMAP polling started (interval=%ds)", interval)
        try:
            while self._polling:
                try:
                    messages = self.fetch_messages("UNSEEN", limit=50)
                    if messages and callback:
                        callback(messages)
                except Exception as exc:
                    logger.warning("Poll cycle error: %s", exc)
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
        finally:
            self._polling = False
            logger.info("IMAP polling stopped")

    def stop_polling(self):
        """Signal the polling loop to stop."""
        self._polling = False

    # ------------------------------------------------------------------ #
    #  IMAP IDLE                                                            #
    # ------------------------------------------------------------------ #

    def idle(self, timeout: int = 1740, callback: Optional[Callable] = None):
        """
        Send IMAP IDLE command (RFC 2177).  Blocks until a server notification
        or *timeout* seconds (default 29 min — IDLE must be re-issued before
        servers drop the connection at 30 min).

        After each IDLE cycle the folder is re-fetched for UNSEEN messages
        and *callback(messages)* is called if new messages arrived.

        Args:
            timeout:  Seconds before re-issuing IDLE (max 1740 = 29 min).
            callback: Called with list of new messages on each wake.
        """
        if self._conn is None:
            logger.error("IDLE: not connected")
            return

        logger.info("Starting IMAP IDLE (timeout=%ds)", timeout)
        self._polling = True
        try:
            while self._polling:
                # Issue IDLE
                try:
                    tag = self._conn._new_tag().decode()
                    self._conn.send(f"{tag} IDLE\r\n".encode())
                    # Read the "+ idling" continuation
                    self._conn.readline()
                except Exception as exc:
                    logger.warning("IDLE send error: %s — falling back to poll", exc)
                    time.sleep(30)
                    continue

                # Wait for server push or timeout
                deadline = time.monotonic() + timeout
                self._conn.sock.settimeout(min(timeout, 60))
                try:
                    while time.monotonic() < deadline and self._polling:
                        try:
                            line = self._conn.readline()
                            if line and b"EXISTS" in line:
                                break  # new message arrived
                        except (OSError, imaplib.IMAP4.abort):
                            break
                finally:
                    self._conn.sock.settimeout(None)

                # Send DONE to exit IDLE
                try:
                    self._conn.send(b"DONE\r\n")
                    self._conn.readline()  # "tag OK IDLE terminated"
                except Exception:
                    pass

                # Fetch new UNSEEN
                if self._polling and callback:
                    try:
                        msgs = self.fetch_messages("UNSEEN", limit=50)
                        if msgs:
                            callback(msgs)
                    except Exception as exc:
                        logger.warning("IDLE post-fetch error: %s", exc)

        except KeyboardInterrupt:
            pass
        finally:
            self._polling = False
            logger.info("IMAP IDLE stopped")
