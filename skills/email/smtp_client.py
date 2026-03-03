#!/usr/bin/env python3
"""
smtp_client.py - SMTP sending with CC/BCC/attachments for OpenPango email skill.

Provides SMTPClient with:
  - STARTTLS (port 587) and SSL (port 465) support
  - Multipart MIME message construction
  - CC, BCC, file attachments with correct MIME types
  - In-Reply-To / References headers for thread continuity
"""

import base64
import mimetypes
import smtplib
import ssl
import uuid
import logging
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


class SMTPClient:
    """
    SMTP client for sending email messages.

    Usage::

        client = SMTPClient("smtp.gmail.com", port=587)
        client.login("user@gmail.com", "app-password")
        result = client.send_message(
            from_addr="user@gmail.com",
            to=["recipient@example.com"],
            subject="Hello",
            body="Message body",
            cc=["cc@example.com"],
            attachments=[Path("/tmp/report.pdf")],
        )
        client.quit()
    """

    def __init__(self, host: str, port: int = 587, use_tls: bool = True):
        """
        Args:
            host:    SMTP server hostname.
            port:    587 for STARTTLS, 465 for SSL. Defaults to 587.
            use_tls: True = STARTTLS on connect. False = plain/SSL (use port 465).
        """
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self._conn: Optional[smtplib.SMTP] = None
        self._username: Optional[str] = None

    # ------------------------------------------------------------------ #
    #  Connection                                                           #
    # ------------------------------------------------------------------ #

    def connect(self) -> bool:
        """Open the SMTP connection (without logging in)."""
        try:
            if not self.use_tls and self.port == 465:
                ctx = ssl.create_default_context()
                self._conn = smtplib.SMTP_SSL(self.host, self.port, context=ctx)
            else:
                self._conn = smtplib.SMTP(self.host, self.port)
                self._conn.ehlo()
                if self.use_tls:
                    ctx = ssl.create_default_context()
                    self._conn.starttls(context=ctx)
                    self._conn.ehlo()
            logger.debug("SMTP connected to %s:%d", self.host, self.port)
            return True
        except (smtplib.SMTPException, OSError) as exc:
            logger.error("SMTP connection failed: %s", exc)
            return False

    def login(self, username: str, password: str) -> bool:
        """
        Authenticate.  Calls connect() automatically if needed.

        Returns:
            True on success.
        """
        if self._conn is None:
            if not self.connect():
                return False
        try:
            self._conn.login(username, password)
            self._username = username
            logger.debug("SMTP login OK for %s", username)
            return True
        except smtplib.SMTPAuthenticationError as exc:
            logger.error("SMTP auth failed for %s: %s", username, exc)
            return False
        except smtplib.SMTPException as exc:
            logger.error("SMTP login error: %s", exc)
            return False

    def quit(self):
        """Close the SMTP connection gracefully."""
        if self._conn:
            try:
                self._conn.quit()
            except Exception:
                pass
            finally:
                self._conn = None

    # ------------------------------------------------------------------ #
    #  Sending                                                              #
    # ------------------------------------------------------------------ #

    def send_message(
        self,
        from_addr: str,
        to: List[str],
        subject: str,
        body: str,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        attachments: Optional[List[Path]] = None,
        html: bool = False,
        in_reply_to: Optional[str] = None,
        references: Optional[List[str]] = None,
        message_id: Optional[str] = None,
    ) -> dict:
        """
        Build and send an email message.

        Args:
            from_addr:   Sender address, e.g. "Alice <alice@example.com>".
            to:          List of primary recipient addresses.
            subject:     Email subject.
            body:        Message body (plain text or HTML depending on *html* flag).
            cc:          List of CC addresses (optional).
            bcc:         List of BCC addresses (optional).
            attachments: List of Path objects to attach (optional).
            html:        If True, send body as text/html with a plain-text fallback.
            in_reply_to: Message-ID of the message being replied to.
            references:  List of ancestor Message-IDs for thread header.
            message_id:  Override the generated Message-ID (optional).

        Returns:
            dict with keys: message_id, status, recipients, timestamp.
            On failure: adds "error" key.
        """
        if self._conn is None:
            return {"status": "error", "error": "Not connected to SMTP server"}

        msg = self._build_mime_message(
            from_addr=from_addr,
            to=to,
            subject=subject,
            body=body,
            cc=cc or [],
            bcc=bcc or [],
            attachments=attachments or [],
            html=html,
            in_reply_to=in_reply_to,
            references=references or [],
            message_id=message_id,
        )

        # All recipients including BCC (RCPT TO must include BCC)
        all_recipients = list(to) + list(cc or []) + list(bcc or [])

        try:
            refused = self._conn.sendmail(from_addr, all_recipients, msg.as_bytes())
            if refused:
                logger.warning("Some recipients refused: %s", refused)
            sent_message_id = msg["Message-ID"]
            logger.info("SMTP sent '%s' → %s", subject, all_recipients)
            return {
                "status": "sent",
                "message_id": sent_message_id,
                "recipients": all_recipients,
                "refused": refused,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        except smtplib.SMTPRecipientsRefused as exc:
            logger.error("All recipients refused: %s", exc)
            return {"status": "error", "error": f"Recipients refused: {exc}"}
        except smtplib.SMTPException as exc:
            logger.error("SMTP send error: %s", exc)
            return {"status": "error", "error": str(exc)}

    # ------------------------------------------------------------------ #
    #  MIME construction                                                    #
    # ------------------------------------------------------------------ #

    def _build_mime_message(
        self,
        from_addr: str,
        to: List[str],
        subject: str,
        body: str,
        cc: List[str],
        bcc: List[str],
        attachments: List[Path],
        html: bool,
        in_reply_to: Optional[str],
        references: List[str],
        message_id: Optional[str],
    ) -> MIMEMultipart:
        """Construct the complete MIME message object."""
        # Choose the right container type
        if attachments:
            container = MIMEMultipart("mixed")
            body_container = MIMEMultipart("alternative") if html else None
        elif html:
            container = MIMEMultipart("alternative")
            body_container = None
        else:
            container = MIMEMultipart("alternative")
            body_container = None

        # Headers
        mid = message_id or f"<{uuid.uuid4().hex}@openpango.local>"
        container["Message-ID"] = mid
        container["From"] = from_addr
        container["To"] = ", ".join(to)
        container["Subject"] = subject
        container["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

        if cc:
            container["Cc"] = ", ".join(cc)
        # BCC is deliberately NOT added to headers (kept server-side only)

        if in_reply_to:
            container["In-Reply-To"] = in_reply_to
        if references:
            container["References"] = " ".join(references)

        # Body parts
        plain_part = MIMEText(body, "plain", "utf-8")
        html_part = MIMEText(body, "html", "utf-8") if html else None

        if attachments:
            # mixed → alternative → text/html + text/plain
            alt = MIMEMultipart("alternative")
            alt.attach(plain_part)
            if html_part:
                alt.attach(html_part)
            container.attach(alt)
        else:
            container.attach(plain_part)
            if html_part:
                container.attach(html_part)

        # Attachments
        for filepath in attachments:
            self._attach_file(container, filepath)

        return container

    def _attach_file(self, msg: MIMEMultipart, filepath: Path):
        """
        Attach a file to *msg* with the correct MIME type.

        Args:
            msg:      The parent MIMEMultipart message.
            filepath: Path to the file to attach.
        """
        filepath = Path(filepath)
        if not filepath.exists():
            logger.warning("Attachment not found, skipping: %s", filepath)
            return

        ctype, encoding = mimetypes.guess_type(str(filepath))
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"

        maintype, subtype = ctype.split("/", 1)
        filename = filepath.name

        try:
            with open(filepath, "rb") as fh:
                payload = fh.read()
        except OSError as exc:
            logger.error("Could not read attachment %s: %s", filepath, exc)
            return

        if maintype == "text":
            part = MIMEText(payload.decode("utf-8", errors="replace"), _subtype=subtype)
        else:
            part = MIMEBase(maintype, subtype)
            part.set_payload(payload)
            encoders.encode_base64(part)

        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)
        logger.debug("Attached %s (%d bytes, %s)", filename, len(payload), ctype)
