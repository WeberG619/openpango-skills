#!/usr/bin/env python3
"""
credentials.py - Secure credential store for the OpenPango email skill.

Credentials are stored in a JSON file at ~/.openclaw/workspace/email/credentials.json
with 0600 permissions.  Passwords are base64-encoded (obfuscation against
accidental log/diff exposure — not a substitute for a secrets manager).

When the agent_integrations SQLite database is present (managed by bounty #02),
this module reads from it first and falls back to the local file store.
"""

import base64
import json
import logging
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_STORE = Path.home() / ".openclaw" / "workspace" / "email" / "credentials.json"
# Path to the agent_integrations DB created by bounty #02
_AGENT_INTEGRATIONS_DB = Path.home() / ".openclaw" / "workspace" / "agent_integrations.db"

_EXPECTED_KEYS = {
    "name",
    "imap_host",
    "imap_port",
    "smtp_host",
    "smtp_port",
    "username",
    "password",  # base64-encoded at rest
}


def _encode(value: str) -> str:
    """Base64-encode a string (UTF-8)."""
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _decode(value: str) -> str:
    """Base64-decode a string."""
    try:
        return base64.b64decode(value.encode("ascii")).decode("utf-8")
    except Exception:
        # Already plain text (legacy / misconfigured)
        return value


class CredentialStore:
    """
    Secure per-account email credential store.

    Credential lifecycle:
      1. On *get_account*: check agent_integrations DB first, then local file.
      2. On *save_account*: always write to local file store.

    Usage::

        store = CredentialStore()
        store.save_account(
            name="work",
            imap_host="imap.gmail.com", imap_port=993,
            smtp_host="smtp.gmail.com", smtp_port=587,
            username="me@gmail.com",
            password="app-password",
        )
        config = store.get_account("work")
        # config["password"] is already decoded (plaintext)
    """

    def __init__(self, store_path: Optional[Path] = None):
        self.store_path = Path(store_path) if store_path else _DEFAULT_STORE
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.store_path.exists():
            self._write({})

    # ------------------------------------------------------------------ #
    #  Public API                                                           #
    # ------------------------------------------------------------------ #

    def save_account(
        self,
        name: str,
        imap_host: str,
        imap_port: int,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        use_ssl: bool = True,
        use_tls: bool = True,
    ) -> bool:
        """
        Save (or overwrite) credentials for *name*.

        Args:
            name:      Logical account name, e.g. "work", "personal".
            imap_host: IMAP server hostname.
            imap_port: IMAP port (993 for SSL, 143 for STARTTLS).
            smtp_host: SMTP server hostname.
            smtp_port: SMTP port (587 STARTTLS, 465 SSL).
            username:  Email address / IMAP+SMTP username.
            password:  Plaintext password (base64-encoded at rest).
            use_ssl:   Use SSL for IMAP. Default True.
            use_tls:   Use STARTTLS for SMTP. Default True.

        Returns:
            True on success.
        """
        accounts = self._read()
        accounts[name] = {
            "name": name,
            "imap_host": imap_host,
            "imap_port": int(imap_port),
            "smtp_host": smtp_host,
            "smtp_port": int(smtp_port),
            "username": username,
            "password": _encode(password),
            "use_ssl": use_ssl,
            "use_tls": use_tls,
        }
        self._write(accounts)
        logger.info("Saved credentials for account '%s'", name)
        return True

    def get_account(self, name: str) -> Optional[dict]:
        """
        Retrieve credentials for *name*.  Password is returned in plaintext.

        Checks agent_integrations DB first, then local file store.

        Returns:
            dict with IMAP/SMTP config, or None if not found.
        """
        # Try agent_integrations first
        creds = self._get_from_agent_integrations(name)
        if creds:
            return creds

        # Fall back to local store
        accounts = self._read()
        if name not in accounts:
            return None
        account = dict(accounts[name])
        if "password" in account:
            account["password"] = _decode(account["password"])
        return account

    def list_accounts(self) -> List[str]:
        """Return a list of saved account names."""
        names = list(self._read().keys())
        # Add any from agent_integrations not in local store
        for name in self._list_from_agent_integrations():
            if name not in names:
                names.append(name)
        return sorted(names)

    def delete_account(self, name: str) -> bool:
        """
        Delete saved credentials for *name*.

        Returns:
            True if deleted, False if not found.
        """
        accounts = self._read()
        if name not in accounts:
            return False
        del accounts[name]
        self._write(accounts)
        logger.info("Deleted credentials for account '%s'", name)
        return True

    def account_to_handler_config(self, name: str) -> Optional[dict]:
        """
        Return a config dict shaped for EmailHandler.__init__.

        Returns:
            {
              "imap": {"host": ..., "port": ..., "use_ssl": ...},
              "smtp": {"host": ..., "port": ..., "use_tls": ...},
              "username": ...,
              "password": ...,
            }
            or None if not found.
        """
        account = self.get_account(name)
        if not account:
            return None
        return {
            "imap": {
                "host": account["imap_host"],
                "port": account.get("imap_port", 993),
                "use_ssl": account.get("use_ssl", True),
            },
            "smtp": {
                "host": account["smtp_host"],
                "port": account.get("smtp_port", 587),
                "use_tls": account.get("use_tls", True),
            },
            "username": account["username"],
            "password": account["password"],
        }

    # ------------------------------------------------------------------ #
    #  agent_integrations hook                                              #
    # ------------------------------------------------------------------ #

    def _get_from_agent_integrations(self, name: str) -> Optional[dict]:
        """
        Read credentials from the agent_integrations table if available.
        Expects rows with: integration_type='email', integration_name=name,
        credentials_json='{"imap_host":...}' (base64 password inside JSON).
        """
        if not _AGENT_INTEGRATIONS_DB.exists():
            return None
        try:
            conn = sqlite3.connect(str(_AGENT_INTEGRATIONS_DB))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT credentials_json FROM agent_integrations
                WHERE integration_type = 'email'
                  AND (integration_name = ? OR username = ?)
                LIMIT 1
                """,
                (name, name),
            ).fetchone()
            conn.close()
            if not row:
                return None
            data = json.loads(row["credentials_json"])
            if "password" in data:
                data["password"] = _decode(data["password"])
            return data
        except (sqlite3.Error, json.JSONDecodeError, KeyError) as exc:
            logger.debug("agent_integrations lookup failed: %s", exc)
            return None

    def _list_from_agent_integrations(self) -> List[str]:
        if not _AGENT_INTEGRATIONS_DB.exists():
            return []
        try:
            conn = sqlite3.connect(str(_AGENT_INTEGRATIONS_DB))
            rows = conn.execute(
                "SELECT integration_name FROM agent_integrations WHERE integration_type = 'email'"
            ).fetchall()
            conn.close()
            return [r[0] for r in rows if r[0]]
        except sqlite3.Error:
            return []

    # ------------------------------------------------------------------ #
    #  File I/O with 0600 permissions                                       #
    # ------------------------------------------------------------------ #

    def _read(self) -> dict:
        if not self.store_path.exists():
            return {}
        try:
            text = self.store_path.read_text("utf-8")
            return json.loads(text) if text.strip() else {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to read credential store: %s", exc)
            return {}

    def _write(self, data: dict):
        text = json.dumps(data, indent=2)
        # Write atomically via temp file
        tmp = self.store_path.with_suffix(".tmp")
        try:
            tmp.write_text(text, "utf-8")
            # Set 0600 before rename
            os.chmod(str(tmp), stat.S_IRUSR | stat.S_IWUSR)
            tmp.replace(self.store_path)
        except OSError as exc:
            logger.error("Failed to write credential store: %s", exc)
            raise
