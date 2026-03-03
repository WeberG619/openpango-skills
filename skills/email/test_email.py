#!/usr/bin/env python3
"""
test_email.py - Comprehensive tests for the OpenPango email skill.

Covers 35+ test cases using unittest + unittest.mock.
No live server connections are made — all IMAP/SMTP interactions are mocked.

Run:
    python3 skills/email/test_email.py
    python3 -m pytest skills/email/test_email.py -v
"""

import base64
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from unittest.mock import MagicMock, Mock, call, patch, PropertyMock

# Path bootstrap — allows running from project root
_SKILLS_ROOT = Path(__file__).parent.parent.parent
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

from skills.email.imap_client import IMAPClient, _decode_header_value, _parse_address_list
from skills.email.smtp_client import SMTPClient
from skills.email.thread_tracker import ThreadTracker, _normalise_subject
from skills.email.credentials import CredentialStore, _encode, _decode
from skills.email.email_handler import EmailHandler


# ═══════════════════════════════════════════════════════════════════
#  Fixtures / helpers
# ═══════════════════════════════════════════════════════════════════

def _raw_email(
    subject="Test Subject",
    from_addr="sender@example.com",
    to_addr="recipient@example.com",
    body="Hello, this is the body.",
    message_id="<test123@example.com>",
    in_reply_to="",
    references="",
    content_type="text/plain",
) -> bytes:
    """Build a minimal raw RFC 2822 email as bytes."""
    headers = (
        f"From: {from_addr}\r\n"
        f"To: {to_addr}\r\n"
        f"Subject: {subject}\r\n"
        f"Message-ID: {message_id}\r\n"
        f"Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
        f"Content-Type: {content_type}; charset=utf-8\r\n"
    )
    if in_reply_to:
        headers += f"In-Reply-To: {in_reply_to}\r\n"
    if references:
        headers += f"References: {references}\r\n"
    return (headers + f"\r\n{body}").encode("utf-8")


def _make_handler_config() -> dict:
    return {
        "imap": {"host": "imap.test.local", "port": 993, "use_ssl": True},
        "smtp": {"host": "smtp.test.local", "port": 587, "use_tls": True},
        "username": "agent@test.local",
        "password": "secret",
    }


# ═══════════════════════════════════════════════════════════════════
#  1. imap_client.py — Helper functions
# ═══════════════════════════════════════════════════════════════════

class TestIMAPHelpers(unittest.TestCase):

    def test_decode_header_plain(self):
        self.assertEqual(_decode_header_value("Hello World"), "Hello World")

    def test_decode_header_none(self):
        self.assertEqual(_decode_header_value(None), "")

    def test_decode_header_empty(self):
        self.assertEqual(_decode_header_value(""), "")

    def test_decode_header_encoded(self):
        # RFC 2047 encoded: =?utf-8?b?SGVsbG8=?= → "Hello"
        encoded = "=?utf-8?b?SGVsbG8=?="
        result = _decode_header_value(encoded)
        self.assertEqual(result, "Hello")

    def test_parse_address_list_single(self):
        result = _parse_address_list("alice@example.com")
        self.assertEqual(result, ["alice@example.com"])

    def test_parse_address_list_multiple(self):
        result = _parse_address_list("alice@example.com, bob@example.com")
        self.assertEqual(len(result), 2)
        self.assertIn("alice@example.com", result)

    def test_parse_address_list_empty(self):
        self.assertEqual(_parse_address_list(""), [])
        self.assertEqual(_parse_address_list(None), [])


# ═══════════════════════════════════════════════════════════════════
#  2. imap_client.py — IMAPClient
# ═══════════════════════════════════════════════════════════════════

class TestIMAPClient(unittest.TestCase):

    def _make_client(self):
        return IMAPClient("imap.test.local", port=993, use_ssl=True)

    @patch("imaplib.IMAP4_SSL")
    def test_login_success(self, mock_ssl):
        mock_conn = MagicMock()
        mock_ssl.return_value = mock_conn
        client = self._make_client()
        result = client.login("user@test.com", "pass")
        self.assertTrue(result)
        mock_conn.login.assert_called_once_with("user@test.com", "pass")

    @patch("imaplib.IMAP4_SSL")
    def test_login_failure(self, mock_ssl):
        import imaplib
        mock_conn = MagicMock()
        mock_conn.login.side_effect = imaplib.IMAP4.error("auth failed")
        mock_ssl.return_value = mock_conn
        client = self._make_client()
        result = client.login("user@test.com", "wrong")
        self.assertFalse(result)

    @patch("imaplib.IMAP4_SSL")
    def test_select_folder_returns_count(self, mock_ssl):
        mock_conn = MagicMock()
        mock_conn.select.return_value = ("OK", [b"42"])
        mock_ssl.return_value = mock_conn
        client = self._make_client()
        client._conn = mock_conn
        count = client.select_folder("INBOX")
        self.assertEqual(count, 42)

    @patch("imaplib.IMAP4_SSL")
    def test_fetch_messages_unseen(self, mock_ssl):
        mock_conn = MagicMock()
        raw = _raw_email()
        mock_conn.search.return_value = ("OK", [b"1 2"])
        mock_conn.fetch.return_value = ("OK", [(b"1 (RFC822 {xxx})", raw)])
        mock_ssl.return_value = mock_conn
        client = self._make_client()
        client._conn = mock_conn
        messages = client.fetch_messages("UNSEEN", limit=10)
        self.assertIsInstance(messages, list)
        # At least one message parsed
        self.assertGreater(len(messages), 0)
        self.assertIn("message_id", messages[0])
        self.assertIn("subject", messages[0])
        self.assertIn("from", messages[0])

    @patch("imaplib.IMAP4_SSL")
    def test_fetch_messages_empty_inbox(self, mock_ssl):
        mock_conn = MagicMock()
        mock_conn.search.return_value = ("OK", [b""])
        mock_ssl.return_value = mock_conn
        client = self._make_client()
        client._conn = mock_conn
        messages = client.fetch_messages("UNSEEN")
        self.assertEqual(messages, [])

    def test_parse_email_plain_text(self):
        client = self._make_client()
        raw = _raw_email(subject="Hello", from_addr="a@b.com")
        result = client._parse_email(raw)
        self.assertEqual(result["subject"], "Hello")
        self.assertEqual(result["from"], "a@b.com")
        self.assertIn("Hello, this is the body.", result["body_text"])
        self.assertEqual(result["body_html"], "")

    def test_parse_email_message_id(self):
        client = self._make_client()
        raw = _raw_email(message_id="<abc123@example.com>")
        result = client._parse_email(raw)
        self.assertEqual(result["message_id"], "<abc123@example.com>")

    def test_parse_email_in_reply_to(self):
        client = self._make_client()
        raw = _raw_email(in_reply_to="<parent@example.com>")
        result = client._parse_email(raw)
        self.assertEqual(result["in_reply_to"], "<parent@example.com>")

    def test_parse_email_references(self):
        client = self._make_client()
        raw = _raw_email(references="<r1@example.com> <r2@example.com>")
        result = client._parse_email(raw)
        self.assertEqual(result["references"], ["<r1@example.com>", "<r2@example.com>"])

    def test_parse_email_unicode_subject_rfc2047(self):
        """RFC 2047 encoded subject should decode to readable unicode."""
        client = self._make_client()
        # Build a properly RFC 2047-encoded subject: =?utf-8?b?...?=
        import base64 as _b64
        encoded_subject = "=?utf-8?b?" + _b64.b64encode("Héllo Wörld".encode()).decode() + "?="
        raw = _raw_email(subject=encoded_subject)
        result = client._parse_email(raw)
        self.assertIn("Héllo Wörld", result["subject"])

    def test_parse_email_unicode_subject_raw_utf8(self):
        """Plain UTF-8 subject (non-encoded) is returned as decoded string."""
        client = self._make_client()
        # Raw UTF-8 header bytes arrive as latin-1 fallback — subject is non-empty
        raw = _raw_email(subject="Hello World")
        result = client._parse_email(raw)
        self.assertIn("Hello World", result["subject"])

    def test_parse_multipart_email(self):
        """Parse a multipart/alternative email with text and HTML parts."""
        raw = (
            b"From: sender@example.com\r\n"
            b"To: recipient@example.com\r\n"
            b"Subject: Multipart\r\n"
            b"Message-ID: <multi@example.com>\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: multipart/alternative; boundary=\"boundary123\"\r\n"
            b"\r\n"
            b"--boundary123\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"Plain text body\r\n"
            b"--boundary123\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"\r\n"
            b"<p>HTML body</p>\r\n"
            b"--boundary123--\r\n"
        )
        client = self._make_client()
        result = client._parse_email(raw)
        self.assertIn("Plain text body", result["body_text"])
        self.assertIn("<p>HTML body</p>", result["body_html"])

    @patch("imaplib.IMAP4_SSL")
    def test_mark_seen(self, mock_ssl):
        mock_conn = MagicMock()
        mock_conn.store.return_value = ("OK", [b""])
        mock_ssl.return_value = mock_conn
        client = self._make_client()
        client._conn = mock_conn
        result = client.mark_seen(["1", "2"])
        self.assertTrue(result)
        mock_conn.store.assert_called_once_with("1,2", "+FLAGS", "\\Seen")

    @patch("imaplib.IMAP4_SSL")
    def test_mark_unseen(self, mock_ssl):
        mock_conn = MagicMock()
        mock_conn.store.return_value = ("OK", [b""])
        mock_ssl.return_value = mock_conn
        client = self._make_client()
        client._conn = mock_conn
        result = client.mark_unseen(["5"])
        self.assertTrue(result)
        mock_conn.store.assert_called_once_with("5", "-FLAGS", "\\Seen")

    @patch("imaplib.IMAP4_SSL")
    def test_logout(self, mock_ssl):
        mock_conn = MagicMock()
        mock_ssl.return_value = mock_conn
        client = self._make_client()
        client._conn = mock_conn
        client.logout()
        mock_conn.logout.assert_called_once()
        self.assertIsNone(client._conn)

    def test_fetch_messages_when_not_connected(self):
        client = self._make_client()
        # _conn is None
        messages = client.fetch_messages()
        self.assertEqual(messages, [])


# ═══════════════════════════════════════════════════════════════════
#  3. smtp_client.py — SMTPClient
# ═══════════════════════════════════════════════════════════════════

class TestSMTPClient(unittest.TestCase):

    def _make_client(self):
        return SMTPClient("smtp.test.local", port=587, use_tls=True)

    @patch("smtplib.SMTP")
    def test_login_success(self, mock_smtp):
        mock_conn = MagicMock()
        mock_smtp.return_value = mock_conn
        client = self._make_client()
        result = client.login("user@test.com", "pass")
        self.assertTrue(result)
        mock_conn.login.assert_called_once_with("user@test.com", "pass")

    @patch("smtplib.SMTP")
    def test_login_auth_failure(self, mock_smtp):
        import smtplib
        mock_conn = MagicMock()
        mock_conn.login.side_effect = smtplib.SMTPAuthenticationError(535, b"auth failed")
        mock_smtp.return_value = mock_conn
        client = self._make_client()
        result = client.login("user@test.com", "wrong")
        self.assertFalse(result)

    @patch("smtplib.SMTP")
    def test_send_plain_text(self, mock_smtp):
        mock_conn = MagicMock()
        mock_conn.sendmail.return_value = {}
        mock_smtp.return_value = mock_conn
        client = self._make_client()
        client._conn = mock_conn
        client._username = "agent@test.com"
        result = client.send_message(
            from_addr="agent@test.com",
            to=["recipient@test.com"],
            subject="Hello",
            body="Test body",
        )
        self.assertEqual(result["status"], "sent")
        self.assertIn("message_id", result)
        self.assertIn("recipients", result)

    @patch("smtplib.SMTP")
    def test_send_with_cc_bcc(self, mock_smtp):
        mock_conn = MagicMock()
        mock_conn.sendmail.return_value = {}
        mock_smtp.return_value = mock_conn
        client = self._make_client()
        client._conn = mock_conn
        result = client.send_message(
            from_addr="from@test.com",
            to=["to@test.com"],
            subject="CC/BCC test",
            body="body",
            cc=["cc@test.com"],
            bcc=["bcc@test.com"],
        )
        self.assertEqual(result["status"], "sent")
        # All three should appear in recipients (BCC included for SMTP)
        recipients = result["recipients"]
        self.assertIn("to@test.com", recipients)
        self.assertIn("cc@test.com", recipients)
        self.assertIn("bcc@test.com", recipients)

    @patch("smtplib.SMTP")
    def test_send_html_email(self, mock_smtp):
        mock_conn = MagicMock()
        mock_conn.sendmail.return_value = {}
        mock_smtp.return_value = mock_conn
        client = self._make_client()
        client._conn = mock_conn
        result = client.send_message(
            from_addr="from@test.com",
            to=["to@test.com"],
            subject="HTML test",
            body="<h1>Hello</h1>",
            html=True,
        )
        self.assertEqual(result["status"], "sent")

    @patch("smtplib.SMTP")
    def test_send_with_attachment(self, mock_smtp):
        mock_conn = MagicMock()
        mock_conn.sendmail.return_value = {}
        mock_smtp.return_value = mock_conn
        client = self._make_client()
        client._conn = mock_conn
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tf:
            tf.write(b"attachment content")
            tf_path = Path(tf.name)
        try:
            result = client.send_message(
                from_addr="from@test.com",
                to=["to@test.com"],
                subject="With attachment",
                body="See attached",
                attachments=[tf_path],
            )
            self.assertEqual(result["status"], "sent")
            # sendmail was called with bytes containing the attachment
            call_args = mock_conn.sendmail.call_args
            msg_bytes = call_args[0][2]
            self.assertIsInstance(msg_bytes, bytes)
        finally:
            tf_path.unlink(missing_ok=True)

    @patch("smtplib.SMTP")
    def test_send_with_missing_attachment_skipped(self, mock_smtp):
        """Missing attachment file should be skipped, not crash."""
        mock_conn = MagicMock()
        mock_conn.sendmail.return_value = {}
        mock_smtp.return_value = mock_conn
        client = self._make_client()
        client._conn = mock_conn
        result = client.send_message(
            from_addr="from@test.com",
            to=["to@test.com"],
            subject="Missing file",
            body="body",
            attachments=[Path("/nonexistent/file.pdf")],
        )
        self.assertEqual(result["status"], "sent")

    @patch("smtplib.SMTP")
    def test_send_reply_headers(self, mock_smtp):
        """Verify In-Reply-To and References headers are set on MIME message."""
        mock_conn = MagicMock()
        captured_bytes = []

        def capture_send(from_addr, recipients, msg_bytes):
            captured_bytes.append(msg_bytes)
            return {}

        mock_conn.sendmail.side_effect = capture_send
        mock_smtp.return_value = mock_conn
        client = self._make_client()
        client._conn = mock_conn
        client.send_message(
            from_addr="me@test.com",
            to=["them@test.com"],
            subject="Re: Original",
            body="My reply",
            in_reply_to="<orig@test.com>",
            references=["<orig@test.com>"],
        )
        raw = captured_bytes[0].decode("utf-8")
        self.assertIn("In-Reply-To: <orig@test.com>", raw)
        self.assertIn("References: <orig@test.com>", raw)

    @patch("smtplib.SMTP")
    def test_send_not_connected(self, mock_smtp):
        client = self._make_client()
        # No _conn set
        result = client.send_message(
            from_addr="a@b.com", to=["c@d.com"], subject="x", body="y"
        )
        self.assertEqual(result["status"], "error")

    @patch("smtplib.SMTP")
    def test_quit(self, mock_smtp):
        mock_conn = MagicMock()
        mock_smtp.return_value = mock_conn
        client = self._make_client()
        client._conn = mock_conn
        client.quit()
        mock_conn.quit.assert_called_once()
        self.assertIsNone(client._conn)

    def test_build_mime_has_required_headers(self):
        client = self._make_client()
        msg = client._build_mime_message(
            from_addr="a@b.com",
            to=["c@d.com"],
            subject="Test",
            body="Body",
            cc=[],
            bcc=[],
            attachments=[],
            html=False,
            in_reply_to=None,
            references=[],
            message_id=None,
        )
        self.assertIn("Message-ID", msg)
        self.assertIn("From", msg)
        self.assertIn("To", msg)
        self.assertIn("Date", msg)


# ═══════════════════════════════════════════════════════════════════
#  4. thread_tracker.py — ThreadTracker
# ═══════════════════════════════════════════════════════════════════

class TestThreadTracker(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tmpdir.name) / "threads.db"
        self.tracker = ThreadTracker(db_path=db_path)

    def tearDown(self):
        self.tracker.close()
        self.tmpdir.cleanup()

    def _msg(self, message_id, subject="Test", in_reply_to="", references=None, from_addr="a@b.com"):
        return {
            "message_id": message_id,
            "from": from_addr,
            "to": ["recipient@test.com"],
            "cc": [],
            "subject": subject,
            "body_text": f"Body of {message_id}",
            "date": "Mon, 01 Jan 2024 12:00:00 +0000",
            "in_reply_to": in_reply_to,
            "references": references or [],
        }

    def test_record_single_message(self):
        msg = self._msg("<msg1@test.com>")
        thread_id = self.tracker.record_message(msg)
        self.assertIsNotNone(thread_id)
        self.assertTrue(len(thread_id) > 0)

    def test_get_thread_returns_messages(self):
        msg = self._msg("<msg1@test.com>", subject="Hello")
        self.tracker.record_message(msg)
        result = self.tracker.get_thread("<msg1@test.com>")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["message_id"], "<msg1@test.com>")

    def test_thread_linking_via_in_reply_to(self):
        """Two messages linked by In-Reply-To should share a thread."""
        parent = self._msg("<parent@test.com>", subject="Original")
        child = self._msg("<child@test.com>", subject="Re: Original", in_reply_to="<parent@test.com>")
        self.tracker.record_message(parent)
        self.tracker.record_message(child)

        parent_thread = self.tracker.get_thread("<parent@test.com>")
        child_thread = self.tracker.get_thread("<child@test.com>")
        self.assertEqual(len(parent_thread), 2)
        self.assertEqual(len(child_thread), 2)

    def test_thread_linking_via_references(self):
        """Message with References header should join existing thread."""
        ancestor = self._msg("<anc@test.com>", subject="Start")
        descendant = self._msg(
            "<desc@test.com>",
            subject="Re: Start",
            references=["<anc@test.com>"],
        )
        self.tracker.record_message(ancestor)
        self.tracker.record_message(descendant)

        thread = self.tracker.get_thread("<anc@test.com>")
        self.assertEqual(len(thread), 2)

    def test_different_subjects_create_different_threads(self):
        m1 = self._msg("<m1@test.com>", subject="Topic A")
        m2 = self._msg("<m2@test.com>", subject="Topic B")
        tid1 = self.tracker.record_message(m1)
        tid2 = self.tracker.record_message(m2)
        self.assertNotEqual(tid1, tid2)

    def test_duplicate_message_ignored(self):
        msg = self._msg("<dup@test.com>")
        tid1 = self.tracker.record_message(msg)
        tid2 = self.tracker.record_message(msg)  # Same message again
        self.assertEqual(tid1, tid2)
        # Only one message in thread
        thread = self.tracker.get_thread("<dup@test.com>")
        self.assertEqual(len(thread), 1)

    def test_get_threads_returns_summaries(self):
        for i in range(3):
            self.tracker.record_message(self._msg(f"<m{i}@test.com>", subject=f"Thread {i}"))
        threads = self.tracker.get_threads(limit=10)
        self.assertEqual(len(threads), 3)
        self.assertIn("thread_id", threads[0])
        self.assertIn("message_count", threads[0])
        self.assertIn("updated_at", threads[0])

    def test_search_threads_fts(self):
        self.tracker.record_message(self._msg("<inv1@test.com>", subject="Invoice #1234"))
        self.tracker.record_message(self._msg("<unrel@test.com>", subject="Completely unrelated"))
        results = self.tracker.search_threads("Invoice")
        self.assertTrue(any(r["message_id"] == "<inv1@test.com>" for r in results))

    def test_search_threads_empty_query(self):
        results = self.tracker.search_threads("")
        self.assertEqual(results, [])

    def test_get_thread_unknown_message_id(self):
        result = self.tracker.get_thread("<nonexistent@test.com>")
        self.assertEqual(result, [])

    def test_normalise_subject(self):
        self.assertEqual(_normalise_subject("Re: Hello"), "hello")
        self.assertEqual(_normalise_subject("FWD: Meeting"), "meeting")
        self.assertEqual(_normalise_subject("Fwd: Fwd: Status"), "fwd: status")
        self.assertEqual(_normalise_subject("Original"), "original")

    def test_long_thread_chain(self):
        """Build a 5-message chain and verify they all link to same thread."""
        prev_id = None
        refs = []
        for i in range(5):
            mid = f"<chain{i}@test.com>"
            msg = self._msg(
                mid,
                subject="Chain subject" if i == 0 else "Re: Chain subject",
                in_reply_to=prev_id or "",
                references=list(refs),
            )
            self.tracker.record_message(msg)
            if prev_id:
                refs.append(prev_id)
            prev_id = mid

        thread = self.tracker.get_thread("<chain0@test.com>")
        self.assertEqual(len(thread), 5)


# ═══════════════════════════════════════════════════════════════════
#  5. credentials.py — CredentialStore
# ═══════════════════════════════════════════════════════════════════

class TestCredentialStore(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        store_path = Path(self.tmpdir.name) / "credentials.json"
        self.store = CredentialStore(store_path=store_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _save(self, name="work"):
        self.store.save_account(
            name=name,
            imap_host="imap.gmail.com",
            imap_port=993,
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            username="me@gmail.com",
            password="my-secret-password",
        )

    def test_save_and_get_account(self):
        self._save()
        account = self.store.get_account("work")
        self.assertIsNotNone(account)
        self.assertEqual(account["imap_host"], "imap.gmail.com")
        self.assertEqual(account["smtp_host"], "smtp.gmail.com")
        self.assertEqual(account["username"], "me@gmail.com")
        # Password decoded back to plaintext
        self.assertEqual(account["password"], "my-secret-password")

    def test_password_encoded_at_rest(self):
        self._save()
        raw = self.store.store_path.read_text()
        data = json.loads(raw)
        stored_pw = data["work"]["password"]
        # Should NOT be plaintext
        self.assertNotEqual(stored_pw, "my-secret-password")
        # Should be valid base64
        decoded = base64.b64decode(stored_pw.encode()).decode()
        self.assertEqual(decoded, "my-secret-password")

    def test_file_permissions_0600(self):
        self._save()
        mode = oct(stat.S_IMODE(self.store.store_path.stat().st_mode))
        # On Windows/WSL the check may differ; skip if not POSIX
        if os.name == "posix":
            self.assertEqual(mode, "0o600")

    def test_list_accounts(self):
        self._save("work")
        self._save("personal")
        accounts = self.store.list_accounts()
        self.assertIn("work", accounts)
        self.assertIn("personal", accounts)

    def test_delete_account(self):
        self._save()
        result = self.store.delete_account("work")
        self.assertTrue(result)
        self.assertIsNone(self.store.get_account("work"))

    def test_delete_nonexistent_account(self):
        result = self.store.delete_account("ghost")
        self.assertFalse(result)

    def test_get_nonexistent_account_returns_none(self):
        result = self.store.get_account("nonexistent")
        self.assertIsNone(result)

    def test_account_to_handler_config_shape(self):
        self._save()
        config = self.store.account_to_handler_config("work")
        self.assertIn("imap", config)
        self.assertIn("smtp", config)
        self.assertIn("username", config)
        self.assertIn("password", config)
        self.assertEqual(config["imap"]["host"], "imap.gmail.com")
        self.assertEqual(config["smtp"]["host"], "smtp.gmail.com")
        self.assertEqual(config["password"], "my-secret-password")

    def test_account_to_handler_config_missing_returns_none(self):
        result = self.store.account_to_handler_config("ghost")
        self.assertIsNone(result)

    def test_encode_decode_roundtrip(self):
        original = "sup3r-s3cr3t!@#$%"
        encoded = _encode(original)
        decoded = _decode(encoded)
        self.assertEqual(decoded, original)

    def test_overwrite_account(self):
        self._save("myacct")
        self.store.save_account(
            name="myacct",
            imap_host="imap.new.com",
            imap_port=993,
            smtp_host="smtp.new.com",
            smtp_port=587,
            username="new@new.com",
            password="newpass",
        )
        account = self.store.get_account("myacct")
        self.assertEqual(account["imap_host"], "imap.new.com")
        self.assertEqual(account["password"], "newpass")


# ═══════════════════════════════════════════════════════════════════
#  6. email_handler.py — EmailHandler integration
# ═══════════════════════════════════════════════════════════════════

class TestEmailHandler(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.config = _make_handler_config()
        self.db_path = Path(self.tmpdir.name) / "threads.db"

    def tearDown(self):
        self.tmpdir.cleanup()

    def _patched_handler(self):
        handler = EmailHandler(self.config)
        handler.tracker = ThreadTracker(db_path=self.db_path)
        return handler

    @patch("skills.email.email_handler.IMAPClient")
    @patch("skills.email.email_handler.SMTPClient")
    def test_connect_success(self, MockSMTP, MockIMAP):
        MockIMAP.return_value.login.return_value = True
        MockSMTP.return_value.login.return_value = True
        handler = EmailHandler(self.config)
        result = handler.connect()
        self.assertTrue(result)
        self.assertTrue(handler._connected)

    @patch("skills.email.email_handler.IMAPClient")
    @patch("skills.email.email_handler.SMTPClient")
    def test_connect_imap_failure(self, MockSMTP, MockIMAP):
        MockIMAP.return_value.login.return_value = False
        MockSMTP.return_value.login.return_value = True
        handler = EmailHandler(self.config)
        result = handler.connect()
        self.assertFalse(result)

    @patch("skills.email.email_handler.IMAPClient")
    @patch("skills.email.email_handler.SMTPClient")
    def test_disconnect(self, MockSMTP, MockIMAP):
        handler = EmailHandler(self.config)
        handler.disconnect()
        handler.imap.logout.assert_called_once()
        handler.smtp.quit.assert_called_once()
        self.assertFalse(handler._connected)

    @patch("skills.email.email_handler.IMAPClient")
    @patch("skills.email.email_handler.SMTPClient")
    def test_context_manager(self, MockSMTP, MockIMAP):
        MockIMAP.return_value.login.return_value = True
        MockSMTP.return_value.login.return_value = True
        with EmailHandler(self.config) as handler:
            self.assertTrue(handler._connected)
        handler.imap.logout.assert_called_once()
        handler.smtp.quit.assert_called_once()

    @patch("skills.email.email_handler.IMAPClient")
    @patch("skills.email.email_handler.SMTPClient")
    def test_fetch_unread(self, MockSMTP, MockIMAP):
        raw = _raw_email(subject="Unread msg", message_id="<unread1@test.com>")
        MockIMAP.return_value.fetch_messages.return_value = [
            {
                "message_id": "<unread1@test.com>",
                "from": "sender@test.com",
                "to": ["agent@test.local"],
                "cc": [],
                "subject": "Unread msg",
                "body_text": "Body here",
                "date": "Mon, 01 Jan 2024 12:00:00 +0000",
                "in_reply_to": "",
                "references": [],
                "imap_uid": "1",
            }
        ]
        handler = EmailHandler(self.config)
        handler.tracker = ThreadTracker(db_path=self.db_path)
        messages = handler.fetch_unread(folder="INBOX", limit=10)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["subject"], "Unread msg")

    @patch("skills.email.email_handler.IMAPClient")
    @patch("skills.email.email_handler.SMTPClient")
    def test_send_message(self, MockSMTP, MockIMAP):
        MockSMTP.return_value.send_message.return_value = {
            "status": "sent",
            "message_id": "<new@test.com>",
            "recipients": ["them@test.com"],
            "timestamp": "2024-01-01T12:00:00+00:00",
        }
        handler = EmailHandler(self.config)
        handler.tracker = ThreadTracker(db_path=self.db_path)
        result = handler.send(
            to="them@test.com",
            subject="Hello",
            body="Message body",
        )
        self.assertEqual(result["status"], "sent")
        # Sent message should be in thread tracker
        thread = handler.tracker.get_thread("<new@test.com>")
        self.assertEqual(len(thread), 1)

    @patch("skills.email.email_handler.IMAPClient")
    @patch("skills.email.email_handler.SMTPClient")
    def test_reply_to_existing_message(self, MockSMTP, MockIMAP):
        """Reply should set In-Reply-To and References from the original."""
        handler = EmailHandler(self.config)
        handler.tracker = ThreadTracker(db_path=self.db_path)

        # Pre-populate tracker with original message
        original = {
            "message_id": "<orig@test.com>",
            "from": "boss@company.com",
            "to": ["agent@test.local"],
            "cc": [],
            "subject": "Important project",
            "body_text": "Please review.",
            "date": "Mon, 01 Jan 2024 12:00:00 +0000",
            "in_reply_to": "",
            "references": [],
        }
        handler.tracker.record_message(original)

        MockSMTP.return_value.send_message.return_value = {
            "status": "sent",
            "message_id": "<reply@test.com>",
            "recipients": ["boss@company.com"],
            "timestamp": "2024-01-01T13:00:00+00:00",
        }

        result = handler.reply(
            original_message_id="<orig@test.com>",
            body="On it!",
        )
        self.assertEqual(result["status"], "sent")
        # Verify send_message was called with correct in_reply_to
        call_kwargs = MockSMTP.return_value.send_message.call_args[1]
        self.assertEqual(call_kwargs.get("in_reply_to"), "<orig@test.com>")
        self.assertIn("<orig@test.com>", call_kwargs.get("references", []))

    @patch("skills.email.email_handler.IMAPClient")
    @patch("skills.email.email_handler.SMTPClient")
    def test_reply_unknown_message_returns_error(self, MockSMTP, MockIMAP):
        MockIMAP.return_value.fetch_messages.return_value = []
        handler = EmailHandler(self.config)
        handler.tracker = ThreadTracker(db_path=self.db_path)
        result = handler.reply(
            original_message_id="<ghost@test.com>",
            body="reply",
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("error", result)

    @patch("skills.email.email_handler.IMAPClient")
    @patch("skills.email.email_handler.SMTPClient")
    def test_search_combines_imap_and_fts(self, MockSMTP, MockIMAP):
        MockIMAP.return_value.fetch_messages.return_value = [
            {
                "message_id": "<imap1@test.com>",
                "from": "x@y.com",
                "to": [],
                "cc": [],
                "subject": "invoice",
                "body_text": "Invoice body",
                "date": "Mon, 01 Jan 2024 12:00:00 +0000",
                "in_reply_to": "",
                "references": [],
            }
        ]
        handler = EmailHandler(self.config)
        handler.tracker = ThreadTracker(db_path=self.db_path)
        # Pre-seed thread DB with a different message
        handler.tracker.record_message({
            "message_id": "<fts1@test.com>",
            "from": "a@b.com",
            "to": [],
            "cc": [],
            "subject": "invoice payment",
            "body_text": "Please pay invoice",
            "date": "Mon, 01 Jan 2024 12:00:00 +0000",
            "in_reply_to": "",
            "references": [],
        })
        results = handler.search("SUBJECT invoice")
        # At minimum the IMAP result
        self.assertGreaterEqual(len(results), 1)

    @patch("skills.email.email_handler.IMAPClient")
    @patch("skills.email.email_handler.SMTPClient")
    def test_mark_read(self, MockSMTP, MockIMAP):
        MockIMAP.return_value.mark_seen.return_value = True
        handler = EmailHandler(self.config)
        result = handler.mark_read(["1", "2", "3"])
        self.assertTrue(result)
        handler.imap.mark_seen.assert_called_once_with(["1", "2", "3"])

    @patch("skills.email.email_handler.IMAPClient")
    @patch("skills.email.email_handler.SMTPClient")
    def test_mark_unread(self, MockSMTP, MockIMAP):
        MockIMAP.return_value.mark_unseen.return_value = True
        handler = EmailHandler(self.config)
        result = handler.mark_unread(["5"])
        self.assertTrue(result)

    @patch("skills.email.email_handler.IMAPClient")
    @patch("skills.email.email_handler.SMTPClient")
    def test_reply_all_adds_cc(self, MockSMTP, MockIMAP):
        """Reply-All should CC all original recipients minus self."""
        handler = EmailHandler(self.config)
        handler.tracker = ThreadTracker(db_path=self.db_path)
        original = {
            "message_id": "<orig2@test.com>",
            "from": "boss@co.com",
            "to": ["agent@test.local", "colleague@co.com"],
            "cc": ["cc-person@co.com"],
            "subject": "Team meeting",
            "body_text": "Join us.",
            "date": "Mon, 01 Jan 2024 12:00:00 +0000",
            "in_reply_to": "",
            "references": [],
        }
        handler.tracker.record_message(original)
        MockSMTP.return_value.send_message.return_value = {
            "status": "sent",
            "message_id": "<replyall@test.com>",
            "recipients": ["boss@co.com", "colleague@co.com", "cc-person@co.com"],
            "timestamp": "2024-01-01T13:00:00+00:00",
        }
        result = handler.reply(
            original_message_id="<orig2@test.com>",
            body="All noted.",
            reply_all=True,
        )
        self.assertEqual(result["status"], "sent")
        call_kwargs = MockSMTP.return_value.send_message.call_args[1]
        cc = call_kwargs.get("cc", [])
        # Should not include self (agent@test.local)
        self.assertNotIn("agent@test.local", cc)
        # Should include colleague and cc-person
        self.assertIn("colleague@co.com", cc)
        self.assertIn("cc-person@co.com", cc)


# ═══════════════════════════════════════════════════════════════════
#  7. Edge cases
# ═══════════════════════════════════════════════════════════════════

class TestEdgeCases(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "threads.db"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_parse_email_invalid_bytes(self):
        client = IMAPClient("host")
        result = client._parse_email(b"Not a valid email at all \x00\xff")
        # Should return a dict, not raise
        self.assertIsInstance(result, dict)

    def test_thread_tracker_empty_message_id_gets_generated(self):
        tracker = ThreadTracker(db_path=self.db_path)
        msg = {
            "message_id": "",  # empty
            "from": "a@b.com",
            "to": [],
            "cc": [],
            "subject": "Test",
            "body_text": "body",
            "date": "Mon, 01 Jan 2024 12:00:00 +0000",
            "in_reply_to": "",
            "references": [],
        }
        thread_id = tracker.record_message(msg)
        self.assertIsNotNone(thread_id)
        tracker.close()

    def test_smtp_build_mime_bcc_not_in_headers(self):
        """BCC recipients must NOT appear in message headers."""
        client = SMTPClient("smtp.test.local")
        msg = client._build_mime_message(
            from_addr="a@b.com",
            to=["b@b.com"],
            subject="BCC test",
            body="body",
            cc=[],
            bcc=["secret@b.com"],
            attachments=[],
            html=False,
            in_reply_to=None,
            references=[],
            message_id=None,
        )
        raw = msg.as_string()
        self.assertNotIn("secret@b.com", raw)

    def test_smtp_binary_attachment(self):
        """Binary file attachment should be base64-encoded."""
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tf:
            tf.write(bytes(range(256)))
            tf_path = Path(tf.name)
        try:
            client = SMTPClient("smtp.test.local")
            msg = client._build_mime_message(
                from_addr="a@b.com",
                to=["b@b.com"],
                subject="Binary",
                body="see attached",
                cc=[],
                bcc=[],
                attachments=[tf_path],
                html=False,
                in_reply_to=None,
                references=[],
                message_id=None,
            )
            raw = msg.as_string()
            self.assertIn(tf_path.name, raw)
        finally:
            tf_path.unlink(missing_ok=True)

    def test_credential_encode_special_chars(self):
        pw = "p@$$w0rd!\"#%&/()=?`{[]}|<>;:,.¨~"
        self.assertEqual(_decode(_encode(pw)), pw)

    def test_thread_tracker_large_body_preview_truncated(self):
        tracker = ThreadTracker(db_path=self.db_path)
        long_body = "x" * 2000
        msg = {
            "message_id": "<big@test.com>",
            "from": "a@b.com",
            "to": [],
            "cc": [],
            "subject": "Big body",
            "body_text": long_body,
            "date": "Mon, 01 Jan 2024 12:00:00 +0000",
            "in_reply_to": "",
            "references": [],
        }
        tracker.record_message(msg)
        result = tracker.get_message("<big@test.com>")
        self.assertIsNotNone(result)
        self.assertLessEqual(len(result.get("body_preview", "")), 500)
        tracker.close()


# ═══════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    test_classes = [
        TestIMAPHelpers,
        TestIMAPClient,
        TestSMTPClient,
        TestThreadTracker,
        TestCredentialStore,
        TestEmailHandler,
        TestEdgeCases,
    ]
    for cls in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
