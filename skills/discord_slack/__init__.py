"""
skills.discord_slack — Native Discord & Slack bot integrations for OpenPango.

Deep integration layer providing:
- Mention-triggered agent routing via router.py
- Live status streaming back to chat threads
- HITL (human-in-the-loop) approval dialogs in-thread
- Unified authorization enforcement across both platforms

Public API:
    BotServer       — Run Discord + Slack concurrently (main entry point)
    DiscordHandler  — Discord Gateway WebSocket handler
    SlackHandler    — Slack Events API HTTP handler
    RouterBridge    — Bridge between chat events and router.py
    Auth            — User/role authorization layer
"""

from .bot_server import BotServer
from .discord_handler import DiscordHandler
from .slack_handler import SlackHandler
from .router_bridge import RouterBridge
from .auth import Auth

__all__ = [
    "BotServer",
    "DiscordHandler",
    "SlackHandler",
    "RouterBridge",
    "Auth",
]
