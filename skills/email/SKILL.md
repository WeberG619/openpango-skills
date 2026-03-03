---
name: email
description: "Native email management with IMAP/SMTP, thread tracking, and secure credentials"
version: "1.0.0"
user-invocable: true
metadata:
  capabilities:
    - email/read
    - email/send
    - email/reply
    - email/search
    - email/threads
  author: "WeberG619"
  license: "MIT"
  openclaw:
    emoji: "📧"
    skillKey: "openpango-email"
---

# Email Skill

Native email management skill for OpenPango agents. Enables agents to autonomously monitor inboxes,
send messages, reply in context, and track full conversation threads — all via direct IMAP/SMTP
protocol without browser automation.

## Architecture

```
skills/email/
├── SKILL.md             # This file
├── __init__.py          # Public API exports
├── email_handler.py     # Unified EmailHandler (IMAP + SMTP facade)
├── imap_client.py       # IMAP connection, polling, IDLE support
├── smtp_client.py       # SMTP sending with CC/BCC/attachments
├── thread_tracker.py    # SQLite-backed conversation thread tracking
├── credentials.py       # Secure credential store with agent_integrations hook
├── cli.py               # argparse CLI entry point
└── test_email.py        # 35+ unit tests (unittest + mock)
```

## Quick Start

### Add an account

```bash
python3 skills/email/cli.py accounts add \
  --name myaccount \
  --imap-host imap.gmail.com \
  --smtp-host smtp.gmail.com \
  --username me@gmail.com \
  --password "app-password"
```

### Read unread emails

```bash
python3 skills/email/cli.py read --account myaccount --limit 10
```

### Send an email

```bash
python3 skills/email/cli.py send \
  --account myaccount \
  --to "recipient@example.com" \
  --subject "Hello from OpenPango" \
  --body "Agent-authored message."
```

### Reply to a thread

```bash
python3 skills/email/cli.py reply \
  --account myaccount \
  --message-id "<abc123@mail.gmail.com>" \
  --body "Thanks, following up here."
```

### List recent threads

```bash
python3 skills/email/cli.py threads --limit 20
```

### Show a full thread

```bash
python3 skills/email/cli.py thread "<abc123@mail.gmail.com>"
```

### Search

```bash
python3 skills/email/cli.py search "invoice" --account myaccount
```

## Python API

```python
from skills.email import EmailHandler, ThreadTracker, CredentialStore

# Load saved credentials
store = CredentialStore()
config = store.get_account("myaccount")

# Use as context manager for automatic connect/disconnect
with EmailHandler(config) as handler:
    # Read unread
    messages = handler.fetch_unread(limit=20)
    for msg in messages:
        print(msg["subject"], msg["from"])

    # Send
    result = handler.send(
        to="contact@example.com",
        subject="Hello",
        body="Agent message",
        cc=["cc@example.com"],
        attachments=[Path("/tmp/report.pdf")],
    )

    # Reply maintaining thread
    reply = handler.reply(
        original_message_id="<original@mail.example.com>",
        body="Replying in thread.",
        reply_all=False,
    )

    # Full thread history
    thread = handler.get_thread("<original@mail.example.com>")
```

## Credential Security

Credentials are stored at `~/.openclaw/workspace/email/credentials.json` with `0600` file permissions.
Passwords are base64-encoded (obfuscation against accidental log exposure).

When the `agent_integrations` database table is present (managed by bounty #02), this skill
automatically reads from it first and falls back to the local credential store.

## Thread Tracking

All sent and received messages are recorded in a SQLite database at
`~/.openclaw/workspace/email/threads.db`. Thread linkage follows RFC 2822:

- `Message-ID` — unique identifier for each message
- `In-Reply-To` — parent message ID
- `References` — full ancestor chain

The `get_thread()` method resolves the complete conversation regardless of which message
in the chain you query from.

## Environment Variables (Alternative to stored credentials)

| Variable        | Description                  |
|-----------------|------------------------------|
| `IMAP_HOST`     | IMAP server hostname         |
| `IMAP_PORT`     | IMAP port (default: 993)     |
| `IMAP_USER`     | IMAP username                |
| `IMAP_PASS`     | IMAP password                |
| `SMTP_HOST`     | SMTP server hostname         |
| `SMTP_PORT`     | SMTP port (default: 587)     |
| `SMTP_USER`     | SMTP username                |
| `SMTP_PASS`     | SMTP password                |

## Running Tests

```bash
python3 -m pytest skills/email/test_email.py -v
# or
python3 skills/email/test_email.py
```

All tests use `unittest.mock` — no live server connections required.
