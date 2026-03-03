"""
skills/audit — Immutable audit logging with cryptographic hash chain.

Every tool invocation, HTTP request, file modification, and CLI command
executed by the agent is appended to a tamper-evident JSONL ledger.  Each
entry is bound to the previous one via a SHA-256 hash chain so that any
deletion or mutation is immediately detectable.

Quick start
-----------
    from skills.audit import get_logger

    audit = get_logger()
    audit.log_tool_invocation("web_search", inputs={"q": "OpenPango"})
    audit.log_http_request("GET", "https://api.example.com/v1/data", status_code=200)
    audit.log_file_modification("write", "/tmp/output.json", size_bytes=1024)
    audit.log_cli_command("git commit -m 'init'", cwd="/repo", exit_code=0)

Verification
------------
    from skills.audit import verify_audit_log
    from pathlib import Path

    report = verify_audit_log(Path.home() / ".openclaw/workspace/audit.jsonl")
    print(report["valid"], report["entries_checked"], report["errors"])

CLI
---
    openpango audit --verify
    openpango audit --tail 20
    openpango audit --query tool/invoke
    openpango audit --stats
"""

from .audit_logger import AuditLogger, get_logger
from .ledger import HashChainLedger
from .verifier import verify_audit_log

__all__ = [
    "AuditLogger",
    "get_logger",
    "HashChainLedger",
    "verify_audit_log",
]
