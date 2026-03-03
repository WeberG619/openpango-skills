#!/usr/bin/env python3
"""
bot_server.py - Main dual-platform bot server for OpenPango Discord & Slack integration.

Runs DiscordHandler and SlackHandler concurrently using asyncio.gather so both
platforms are served from a single process. Handles graceful shutdown on
SIGINT / SIGTERM.

Usage:
    # With real tokens:
    export DISCORD_BOT_TOKEN="..."
    export SLACK_BOT_TOKEN="xoxb-..."
    export SLACK_SIGNING_SECRET="..."
    python -m skills.discord_slack.bot_server

    # Mock mode (no tokens needed):
    python -m skills.discord_slack.bot_server

When no tokens are set both handlers run in mock mode and log activity to stdout.
This makes it safe to run in CI / test environments without live credentials.
"""

import asyncio
import logging
import os
import signal
import sys
from typing import Optional

from .auth import Auth
from .router_bridge import RouterBridge
from .discord_handler import DiscordHandler
from .slack_handler import SlackHandler

logger = logging.getLogger("BotServer")


def _configure_logging(level: str = "INFO") -> None:
    """Set up a simple human-readable log format for the bot server."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


class BotServer:
    """
    Dual-platform bot server that runs Discord and Slack handlers concurrently.

    Instantiates a shared Auth instance and RouterBridge instance so both handlers
    share the same authorization rules and the same router interface.

    Attributes:
        discord: The DiscordHandler instance.
        slack:   The SlackHandler instance.
        mock_mode: True when both handlers are in mock mode.
    """

    def __init__(
        self,
        auth: Optional[Auth] = None,
        bridge: Optional[RouterBridge] = None,
        discord_token: Optional[str] = None,
        slack_token: Optional[str] = None,
        slack_signing_secret: Optional[str] = None,
        slack_port: Optional[int] = None,
    ):
        shared_auth = auth or Auth()
        shared_bridge = bridge or RouterBridge()

        self.discord = DiscordHandler(
            bridge=shared_bridge,
            auth=shared_auth,
            token=discord_token,
        )
        self.slack = SlackHandler(
            bridge=shared_bridge,
            auth=shared_auth,
            token=slack_token,
            signing_secret=slack_signing_secret,
            port=slack_port,
        )

        self.mock_mode = self.discord.mock_mode and self.slack.mock_mode
        self._running = False

    # ── Public API ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Start both platform handlers concurrently.

        Registers signal handlers for SIGINT and SIGTERM so the server can be
        stopped cleanly with Ctrl-C or a process signal.

        This coroutine runs until stop() is called or a signal is received.
        """
        self._running = True

        if self.mock_mode:
            logger.warning(
                "BotServer: Running in MOCK mode. "
                "No Discord/Slack tokens configured. "
                "Set DISCORD_BOT_TOKEN, SLACK_BOT_TOKEN, and SLACK_SIGNING_SECRET "
                "to connect to real services."
            )
        else:
            platform_list = []
            if not self.discord.mock_mode:
                platform_list.append("Discord (Gateway)")
            if not self.slack.mock_mode:
                platform_list.append(f"Slack (Events API on :{self.slack.port})")
            logger.info(f"BotServer: Starting — {', '.join(platform_list)}")

        # Install signal handlers (only works from the main thread)
        loop = asyncio.get_event_loop()
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(
                    sig,
                    lambda: asyncio.create_task(self.stop()),
                )
        except (NotImplementedError, RuntimeError):
            # add_signal_handler not available on Windows; ignore
            pass

        try:
            await asyncio.gather(
                self.discord.start(),
                self.slack.start(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("BotServer: All handlers stopped.")

    async def stop(self) -> None:
        """Gracefully stop both handlers."""
        if not self._running:
            return
        self._running = False
        logger.info("BotServer: Shutdown requested.")
        await asyncio.gather(
            self.discord.stop(),
            self.slack.stop(),
            return_exceptions=True,
        )

    def status(self) -> dict:
        """Return a summary of the current server status."""
        return {
            "mock_mode": self.mock_mode,
            "discord_mock": self.discord.mock_mode,
            "discord_bot_user_id": self.discord.bot_user_id,
            "slack_mock": self.slack.mock_mode,
            "slack_port": self.slack.port,
            "running": self._running,
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO")
    _configure_logging(log_level)

    server = BotServer()
    logger.info("BotServer: Initializing...")
    logger.info(f"BotServer: Status = {server.status()}")

    await server.start()


if __name__ == "__main__":
    asyncio.run(_main())
