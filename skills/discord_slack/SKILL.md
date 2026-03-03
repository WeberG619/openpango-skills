---
name: discord-slack
description: "Native Discord & Slack bot integrations for agent command routing and HITL workflows."
version: "1.0.0"
user-invocable: true
metadata:
  capabilities:
    - comms/discord-bot
    - comms/slack-bot
    - hitl/chat-approval
  author: "WeberG619"
  license: "MIT"
---

# Discord & Slack Bot Integration

Deep integration layer that bridges Discord and Slack with the OpenPango orchestration system.
Users can command agents directly from chat, receive live status updates, and approve/reject
HITL (human-in-the-loop) requests — all without leaving their messaging platform.

This skill differs from `skills/comms` (which handles basic send/receive) by providing:
- Persistent bot gateway connections (Discord WebSocket, Slack Events API)
- Mention-triggered task routing via `router.py`
- Thread-native HITL approval dialogs
- Per-server/workspace authorization enforcement

## Architecture

```
Discord Gateway (WSS)          Slack Events API (HTTP)
       |                                |
  discord_handler.py            slack_handler.py
       |                                |
       +----------+  +----------------+
                  |  |
              router_bridge.py  <-- auth.py (gate)
                  |
              router.py (spawn/append/wait)
                  |
              Agent Output
                  |
       +----------+------------------+
       |                             |
  Discord thread reply        Slack thread reply
```

## Usage

### Start the dual-platform bot server

```bash
# With real tokens:
export DISCORD_BOT_TOKEN="your-discord-token"
export DISCORD_APPLICATION_ID="your-app-id"
export SLACK_BOT_TOKEN="xoxb-your-slack-token"
export SLACK_SIGNING_SECRET="your-signing-secret"
export AUTHORIZED_DISCORD_USER_IDS="123456789,987654321"
export AUTHORIZED_SLACK_USER_IDS="U01ABC,U02DEF"

python -m skills.discord_slack.bot_server

# Without tokens (mock mode — logs to stdout):
python -m skills.discord_slack.bot_server
```

### Invoke an agent from Discord

```
@Agent build a new landing page for OpenPango
```

### Invoke an agent from Slack

```
@agent research the best Python async frameworks
```

### HITL approval flow (both platforms)

When an agent requires approval, the bot posts an interactive message:

```
[Agent] Coder wants to write 47 files. Approve? Reply YES <id> or NO <id>
```

Replying `YES <id>` or `NO <id>` in-thread resumes or cancels the task.

## Environment Variables

| Variable                      | Required For       |
|-------------------------------|--------------------|
| `DISCORD_BOT_TOKEN`           | Discord live mode  |
| `DISCORD_APPLICATION_ID`      | Discord live mode  |
| `SLACK_BOT_TOKEN`             | Slack live mode    |
| `SLACK_SIGNING_SECRET`        | Slack verification |
| `SLACK_PORT`                  | Slack HTTP server (default: 3000) |
| `AUTHORIZED_DISCORD_USER_IDS` | Auth (comma-separated) |
| `AUTHORIZED_SLACK_USER_IDS`   | Auth (comma-separated) |
| `AUTHORIZED_DISCORD_ROLE_IDS` | Optional role-based auth |
| `HITL_TIMEOUT_SECONDS`        | HITL approval window (default: 300) |

In mock mode (no tokens), all operations simulate success and log to stdout.
