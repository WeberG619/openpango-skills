"""
skills/audit/audit_logger.py

Global singleton AuditLogger.

Usage:
    from skills.audit import get_logger

    audit = get_logger()
    audit.log_tool_invocation("web_search", inputs={"q": "foo"}, outputs={"n": 5})
    audit.log_http_request("GET", "https://example.com", status_code=200)
    audit.log_file_modification("write", "/tmp/out.json", size_bytes=512)
    audit.log_cli_command("git status", cwd="/repo", exit_code=0)
    audit.log_event("custom/my_event", details={"key": "value"})
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .ledger import HashChainLedger

# ---------------------------------------------------------------------------
# Default ledger path
# ---------------------------------------------------------------------------

_DEFAULT_LEDGER_PATH = Path.home() / ".openclaw" / "workspace" / "audit.jsonl"

# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------


class EventType:
    TOOL_INVOCATION = "tool/invoke"
    HTTP_REQUEST = "http/request"
    FILE_MODIFICATION = "file/modify"
    CLI_COMMAND = "cli/command"


# ---------------------------------------------------------------------------
# AuditLogger
# ---------------------------------------------------------------------------


class AuditLogger:
    """
    Thread-safe, append-only audit logger backed by a HashChainLedger.

    Instantiate via ``get_logger()`` to use the process-wide singleton.
    Pass a custom *ledger_path* only for testing.
    """

    def __init__(
        self,
        ledger_path: Optional[Path] = None,
        agent_id: Optional[str] = None,
    ) -> None:
        path = Path(ledger_path) if ledger_path else _DEFAULT_LEDGER_PATH
        self._ledger = HashChainLedger(path)
        self._agent_id: str = agent_id or os.environ.get(
            "OPENCLAW_AGENT_ID", "default-agent"
        )
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # High-level convenience methods
    # ------------------------------------------------------------------

    def log_tool_invocation(
        self,
        tool_name: str,
        inputs: Optional[Dict[str, Any]] = None,
        outputs: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> dict:
        """
        Record that the agent invoked a tool.

        Args:
            tool_name: Name of the tool (e.g. ``"web_search"``).
            inputs:    Serialisable dict of arguments passed to the tool.
            outputs:   Serialisable dict of the tool's return value.
            error:     If the tool raised, pass the error message here.

        Returns:
            The stored ledger entry.
        """
        details: Dict[str, Any] = {"tool_name": tool_name}
        if inputs is not None:
            details["inputs"] = inputs
        if outputs is not None:
            details["outputs"] = outputs
        if error is not None:
            details["error"] = error
        return self.log_event(EventType.TOOL_INVOCATION, details=details)

    def log_http_request(
        self,
        method: str,
        url: str,
        status_code: Optional[int] = None,
        request_body: Optional[Any] = None,
        response_summary: Optional[str] = None,
        error: Optional[str] = None,
    ) -> dict:
        """
        Record an outbound HTTP request.

        Args:
            method:           HTTP verb (``"GET"``, ``"POST"``, …).
            url:              Full URL.
            status_code:      HTTP response status code, if received.
            request_body:     Serialisable request payload (omit large blobs).
            response_summary: Short description or excerpt of the response.
            error:            Exception message if the request failed.

        Returns:
            The stored ledger entry.
        """
        details: Dict[str, Any] = {"method": method.upper(), "url": url}
        if status_code is not None:
            details["status_code"] = status_code
        if request_body is not None:
            details["request_body"] = request_body
        if response_summary is not None:
            details["response_summary"] = response_summary
        if error is not None:
            details["error"] = error
        return self.log_event(EventType.HTTP_REQUEST, details=details)

    def log_file_modification(
        self,
        operation: str,
        path: str,
        size_bytes: Optional[int] = None,
        checksum: Optional[str] = None,
    ) -> dict:
        """
        Record a filesystem operation.

        Args:
            operation:  One of ``"read"``, ``"write"``, ``"delete"``,
                        ``"rename"``, or any other descriptive string.
            path:       Absolute or relative filesystem path.
            size_bytes: File size after operation, if available.
            checksum:   Optional SHA-256 or MD5 of file contents.

        Returns:
            The stored ledger entry.
        """
        details: Dict[str, Any] = {"operation": operation, "path": str(path)}
        if size_bytes is not None:
            details["size_bytes"] = size_bytes
        if checksum is not None:
            details["checksum"] = checksum
        return self.log_event(EventType.FILE_MODIFICATION, details=details)

    def log_cli_command(
        self,
        command: str,
        cwd: Optional[str] = None,
        exit_code: Optional[int] = None,
        stdout_excerpt: Optional[str] = None,
        stderr_excerpt: Optional[str] = None,
    ) -> dict:
        """
        Record a shell / CLI command execution.

        Args:
            command:        The command string as executed.
            cwd:            Working directory, if known.
            exit_code:      Process exit code.
            stdout_excerpt: First N characters of stdout (keep small).
            stderr_excerpt: First N characters of stderr (keep small).

        Returns:
            The stored ledger entry.
        """
        details: Dict[str, Any] = {"command": command}
        if cwd is not None:
            details["cwd"] = str(cwd)
        if exit_code is not None:
            details["exit_code"] = exit_code
        if stdout_excerpt is not None:
            details["stdout_excerpt"] = stdout_excerpt
        if stderr_excerpt is not None:
            details["stderr_excerpt"] = stderr_excerpt
        return self.log_event(EventType.CLI_COMMAND, details=details)

    def log_event(
        self,
        event_type: str,
        details: Optional[Dict[str, Any]] = None,
        agent_id: Optional[str] = None,
    ) -> dict:
        """
        Append a generic event to the ledger.

        Args:
            event_type: Free-form type string (``"tool/invoke"``, …).
            details:    Arbitrary serialisable metadata dict.
            agent_id:   Override the instance's default agent_id.

        Returns:
            The stored ledger entry (includes ``entry_hash`` / ``prev_hash``).
        """
        entry_data: Dict[str, Any] = {
            "entry_id": str(uuid.uuid4()),
            "timestamp": _utc_now(),
            "event_type": event_type,
            "agent_id": agent_id or self._agent_id,
            "details": details or {},
        }
        return self._ledger.append(entry_data)

    # ------------------------------------------------------------------
    # Delegated ledger operations
    # ------------------------------------------------------------------

    def get_ledger(self) -> HashChainLedger:
        """Return the underlying HashChainLedger (for advanced use)."""
        return self._ledger

    def verify(self):
        """Shortcut: ``(valid, error_msg) = audit.verify()``."""
        return self._ledger.verify_chain()


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_singleton_lock = threading.Lock()
_singleton: Optional[AuditLogger] = None


def get_logger(
    ledger_path: Optional[Path] = None,
    agent_id: Optional[str] = None,
) -> AuditLogger:
    """
    Return the process-wide AuditLogger singleton.

    The first call creates the instance; subsequent calls return the same
    object regardless of the arguments passed (singleton semantics).

    If you need a fresh instance with custom settings (e.g. in tests), call
    ``AuditLogger(ledger_path=…)`` directly instead of this function.
    """
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = AuditLogger(
                    ledger_path=ledger_path,
                    agent_id=agent_id,
                )
    return _singleton


def _reset_singleton() -> None:
    """Reset the singleton — for testing purposes ONLY."""
    global _singleton
    with _singleton_lock:
        _singleton = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    """Current UTC timestamp as an ISO-8601 string with microseconds."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
