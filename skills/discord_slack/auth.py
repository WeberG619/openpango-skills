#!/usr/bin/env python3
"""
auth.py - Authorization layer for the Discord & Slack bot integration.

Enforces that only approved users (and optionally Discord roles) can issue
commands to the OpenPango agent system. Configuration is read from environment
variables and an optional JSON config file.

Authorization hierarchy (Discord):
  1. User ID is in AUTHORIZED_DISCORD_USER_IDS  -> allowed
  2. User has a role ID in AUTHORIZED_DISCORD_ROLE_IDS -> allowed
  3. Otherwise -> denied

Authorization hierarchy (Slack):
  1. User ID is in AUTHORIZED_SLACK_USER_IDS -> allowed
  2. Otherwise -> denied

In mock mode (no IDs configured), ALL users are authorized so tests pass.
"""

import os
import json
import logging
from pathlib import Path
from typing import Optional, Set

logger = logging.getLogger("Auth")


class Auth:
    """
    Authorization gate for Discord and Slack bot commands.

    Attributes:
        discord_user_ids:  Set of authorized Discord user ID strings.
        discord_role_ids:  Set of authorized Discord role ID strings.
        slack_user_ids:    Set of authorized Slack user ID strings.
        mock_mode:         When True, all users are authorized (no IDs configured).
    """

    CONFIG_PATH = Path(__file__).parent / "auth_config.json"

    def __init__(self, config_path: Optional[Path] = None):
        cfg_path = config_path or self.CONFIG_PATH

        # Load from JSON config if it exists, then overlay with env vars
        file_cfg = self._load_file_config(cfg_path)

        self.discord_user_ids: Set[str] = self._parse_ids(
            os.getenv("AUTHORIZED_DISCORD_USER_IDS", ""),
            file_cfg.get("authorized_discord_user_ids", []),
        )
        self.discord_role_ids: Set[str] = self._parse_ids(
            os.getenv("AUTHORIZED_DISCORD_ROLE_IDS", ""),
            file_cfg.get("authorized_discord_role_ids", []),
        )
        self.slack_user_ids: Set[str] = self._parse_ids(
            os.getenv("AUTHORIZED_SLACK_USER_IDS", ""),
            file_cfg.get("authorized_slack_user_ids", []),
        )

        # Mock mode: no explicit IDs configured anywhere
        self.mock_mode = (
            not self.discord_user_ids
            and not self.discord_role_ids
            and not self.slack_user_ids
        )

        if self.mock_mode:
            logger.warning(
                "Auth: No authorized IDs configured. Running in MOCK mode — "
                "all users permitted. Set AUTHORIZED_DISCORD_USER_IDS / "
                "AUTHORIZED_SLACK_USER_IDS to enforce access control."
            )
        else:
            logger.info(
                f"Auth initialized: {len(self.discord_user_ids)} Discord users, "
                f"{len(self.discord_role_ids)} Discord roles, "
                f"{len(self.slack_user_ids)} Slack users"
            )

    # ── Public API ────────────────────────────────────────────────────────────

    def is_discord_authorized(
        self, user_id: str, role_ids: Optional[list] = None
    ) -> bool:
        """
        Return True if the Discord user is authorized to command the agent.

        Args:
            user_id:  The Discord user's snowflake ID (as string).
            role_ids: List of Discord role snowflake IDs the user currently holds.
        """
        if self.mock_mode:
            logger.debug(f"Auth(mock): Discord user {user_id} -> ALLOWED")
            return True

        if user_id in self.discord_user_ids:
            logger.debug(f"Auth: Discord user {user_id} -> ALLOWED (user list)")
            return True

        if role_ids and self.discord_role_ids:
            for rid in role_ids:
                if rid in self.discord_role_ids:
                    logger.debug(
                        f"Auth: Discord user {user_id} role {rid} -> ALLOWED (role list)"
                    )
                    return True

        logger.info(f"Auth: Discord user {user_id} -> DENIED")
        return False

    def is_slack_authorized(self, user_id: str) -> bool:
        """
        Return True if the Slack user is authorized to command the agent.

        Args:
            user_id: The Slack user ID (e.g. 'U01ABCDEF').
        """
        if self.mock_mode:
            logger.debug(f"Auth(mock): Slack user {user_id} -> ALLOWED")
            return True

        if user_id in self.slack_user_ids:
            logger.debug(f"Auth: Slack user {user_id} -> ALLOWED")
            return True

        logger.info(f"Auth: Slack user {user_id} -> DENIED")
        return False

    def add_discord_user(self, user_id: str) -> None:
        """Dynamically add a Discord user to the authorized set (runtime only)."""
        self.discord_user_ids.add(user_id)
        self.mock_mode = False
        logger.info(f"Auth: Added Discord user {user_id}")

    def add_slack_user(self, user_id: str) -> None:
        """Dynamically add a Slack user to the authorized set (runtime only)."""
        self.slack_user_ids.add(user_id)
        self.mock_mode = False
        logger.info(f"Auth: Added Slack user {user_id}")

    def describe(self) -> dict:
        """Return a serializable description of the current auth configuration."""
        return {
            "mock_mode": self.mock_mode,
            "discord_user_ids": sorted(self.discord_user_ids),
            "discord_role_ids": sorted(self.discord_role_ids),
            "slack_user_ids": sorted(self.slack_user_ids),
        }

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _parse_ids(env_value: str, file_list: list) -> Set[str]:
        """
        Merge comma-separated env var value with a list from config file.
        Returns a set of non-empty stripped strings.
        """
        ids: Set[str] = set()
        if env_value:
            ids.update(v.strip() for v in env_value.split(",") if v.strip())
        if file_list:
            ids.update(str(v).strip() for v in file_list if str(v).strip())
        return ids

    @staticmethod
    def _load_file_config(path: Path) -> dict:
        """Load JSON config from disk; silently return empty dict if missing."""
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"Auth: Could not read {path}: {exc}")
            return {}
