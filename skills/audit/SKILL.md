---
name: audit
description: "Immutable audit logging with cryptographic hash chain for enterprise compliance"
version: "1.0.0"
user-invocable: true
metadata:
  capabilities:
    - audit/log
    - audit/verify
    - audit/query
  author: "WeberG619"
  license: "MIT"
  openclaw:
    emoji: "🔒"
    skillKey: "openpango-audit"
---

# Audit Skill — Immutable Ledger for Enterprise Compliance

The `audit` skill provides tamper-evident, append-only logging of every
significant action the agent takes. Each entry is bound to the previous one via
a SHA-256 hash chain, so any deletion or modification is immediately detectable.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────┐
│                   AuditLogger (singleton)            │
│   log_tool_invocation / log_http_request / …        │
│                         │                            │
│              calls HashChainLedger.append()          │
└─────────────────────────┬────────────────────────────┘
                          │
              ~/.openclaw/workspace/audit.jsonl
              (append-only, never mutated)
                          │
              ┌───────────┴──────────┐
              │     verify_audit_log │  ← verifier.py
              └───────────┬──────────┘
                          │
              openpango audit --verify  ← cli.py
```

### Hash Chain

```
Genesis entry:  prev_hash = "000...000" (64 zeros)
                entry_hash = sha256("000...000" + json(entry_data))

Entry N:        prev_hash = entry_hash of entry N-1
                entry_hash = sha256(prev_hash + json(entry_data))
```

Any mutation to an existing entry changes its `entry_hash`, which breaks the
chain at that point — `verify_chain()` returns the first broken link.

---

## Python API

### AuditLogger (global singleton)

```python
from skills.audit import get_logger

audit = get_logger()

# Log a tool invocation
audit.log_tool_invocation(
    tool_name="web_search",
    inputs={"query": "OpenPango docs"},
    outputs={"results": 10},
)

# Log an outbound HTTP request
audit.log_http_request(
    method="POST",
    url="https://api.example.com/data",
    status_code=200,
    request_body={"key": "value"},
    response_summary="OK",
)

# Log a file write/read/delete
audit.log_file_modification(
    operation="write",
    path="/tmp/output.json",
    size_bytes=1024,
)

# Log a shell/CLI command
audit.log_cli_command(
    command="git commit -m 'fix'",
    cwd="/home/user/project",
    exit_code=0,
)

# Log an arbitrary event
audit.log_event(
    event_type="custom/my_event",
    details={"any": "data"},
)
```

### HashChainLedger (low-level)

```python
from skills.audit import HashChainLedger
from pathlib import Path

ledger = HashChainLedger(Path("/tmp/my_ledger.jsonl"))

ledger.append({"event_type": "tool/invoke", "tool_name": "search"})

entries = ledger.read_all()
valid, error = ledger.verify_chain()
```

### verify_audit_log

```python
from skills.audit import verify_audit_log
from pathlib import Path

report = verify_audit_log(Path.home() / ".openclaw/workspace/audit.jsonl")
print(report)
# {
#   "valid": True,
#   "entries_checked": 42,
#   "errors": [],
#   "first_entry_ts": "2026-01-01T00:00:00Z",
#   "last_entry_ts":  "2026-01-01T01:00:00Z"
# }
```

---

## CLI Usage

```bash
# Verify the full chain integrity
openpango audit --verify

# Show the last 20 entries (default: 10)
openpango audit --tail 20

# Filter entries by event type prefix
openpango audit --query tool/invoke

# Show summary statistics
openpango audit --stats

# Use a custom ledger path
openpango audit --verify --ledger /path/to/audit.jsonl
```

All output is JSON to stdout for easy piping.

---

## Event Types

| Constant            | event_type string  | Logged by                  |
|---------------------|--------------------|----------------------------|
| TOOL_INVOCATION     | `tool/invoke`      | `log_tool_invocation()`    |
| HTTP_REQUEST        | `http/request`     | `log_http_request()`       |
| FILE_MODIFICATION   | `file/modify`      | `log_file_modification()`  |
| CLI_COMMAND         | `cli/command`      | `log_cli_command()`        |
| GENERIC             | caller-supplied    | `log_event()`              |

---

## Running Tests

```bash
python3 skills/audit/test_audit.py
# Runs 30+ tests covering hash chain, concurrency, tamper detection, CLI, and more.
```

---

## Storage

| Path | Format | Mutability |
|------|--------|-----------|
| `~/.openclaw/workspace/audit.jsonl` | JSONL (one JSON object per line) | Append-only |

The file is created automatically on first write. Never edit it by hand —
doing so will break the hash chain and be detected by `--verify`.
