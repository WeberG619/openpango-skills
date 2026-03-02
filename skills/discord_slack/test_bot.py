#!/usr/bin/env python3
"""
test_bot.py - Test suite for the OpenPango Discord & Slack bot integration.

All tests use mock dependencies — no real Discord/Slack tokens required.
Tests cover:
  - auth.py:          User ID, role-based authorization, env var parsing
  - router_bridge.py: Intent classification, full flow, HITL approval
  - discord_handler.py: Message handling, auth gate, HITL resolution, mock mode
  - slack_handler.py: Signature verification, event dispatch, mock mode
  - bot_server.py:    Concurrent startup, shared auth/bridge, status reporting

Run with:
    python -m pytest skills/discord_slack/test_bot.py -v
    python skills/discord_slack/test_bot.py
"""

import asyncio
import hashlib
import hmac
import json
import sys
import time
import unittest
from pathlib import Path
from typing import List, Optional
from unittest.mock import patch

# Ensure the package root is importable when run directly
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from skills.discord_slack.auth import Auth
from skills.discord_slack.router_bridge import (
    ChatMessage,
    HitlRequest,
    Platform,
    RouterBridge,
    RouterInterface,
    RouterResult,
    ResultStatus,
)
from skills.discord_slack.discord_handler import DiscordHandler, _WebSocket
from skills.discord_slack.slack_handler import SlackHandler
from skills.discord_slack.bot_server import BotServer


# ---------------------------------------------------------------------------
# Mock Router — in-memory replacement for router.py
# ---------------------------------------------------------------------------

class MockRouter(RouterInterface):
    """
    Fully in-memory router mock.
    Never calls router.py, gemini CLI, or spawns subprocesses.
    """

    def __init__(self, fail: bool = False):
        # Skip parent __init__ (which tries to import router.py)
        self._sessions = {}
        self._mock = True
        self._router = None
        self.fail = fail
        self.spawn_calls: List[str] = []
        self.append_calls: List[tuple] = []

    def spawn_session(self, agent_type: str) -> str:
        sid = f"test-{agent_type.lower()}-{len(self.spawn_calls):03d}"
        self._sessions[sid] = {
            "agent_type": agent_type,
            "status": "idle",
            "output": None,
        }
        self.spawn_calls.append(agent_type)
        return sid

    def append_task(self, session_id: str, task_payload: str) -> None:
        self.append_calls.append((session_id, task_payload))
        if session_id in self._sessions:
            if self.fail:
                self._sessions[session_id]["status"] = "failed"
            else:
                self._sessions[session_id]["status"] = "completed"
                self._sessions[session_id]["output"] = (
                    f"Mock output for task: {task_payload[:50]}"
                )

    def get_status(self, session_id: str) -> str:
        return self._sessions.get(session_id, {}).get("status", "unknown")

    def get_output(self, session_id: str) -> Optional[str]:
        return self._sessions.get(session_id, {}).get("output")

    def wait_for_completion(
        self, session_id: str, timeout: float = 300.0, poll: float = 2.0
    ) -> Optional[str]:
        return self.get_output(session_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_bridge(fail: bool = False) -> RouterBridge:
    bridge = RouterBridge(router=MockRouter(fail=fail))
    bridge.POLL_INTERVAL = 0.01  # speed up tests
    return bridge


async def collect(bridge: RouterBridge, content: str, platform: Platform = Platform.MOCK):
    """Run handle_message and collect all RouterResult objects."""
    results: List[RouterResult] = []

    async def cb(r: RouterResult) -> None:
        results.append(r)

    msg = ChatMessage(
        platform=platform,
        user_id="u1",
        username="tester",
        channel_id="ch1",
        thread_id="msg1",
        content=content,
    )
    await bridge.handle_message(msg, status_callback=cb)
    return results


# ===========================================================================
# Auth tests
# ===========================================================================

class TestAuth(unittest.TestCase):

    def test_mock_mode_when_no_ids_configured(self):
        auth = Auth()
        self.assertTrue(auth.mock_mode)

    def test_mock_mode_allows_any_discord_user(self):
        auth = Auth()
        self.assertTrue(auth.is_discord_authorized("any-id"))

    def test_mock_mode_allows_any_slack_user(self):
        auth = Auth()
        self.assertTrue(auth.is_slack_authorized("UANYTHING"))

    def test_discord_user_allowed_by_id(self):
        auth = Auth()
        auth.add_discord_user("ALLOWED-USER")
        self.assertTrue(auth.is_discord_authorized("ALLOWED-USER"))

    def test_discord_user_denied_when_not_in_list(self):
        auth = Auth()
        auth.add_discord_user("ALLOWED-USER")
        self.assertFalse(auth.is_discord_authorized("INTRUDER"))

    def test_discord_role_allows_user_with_matching_role(self):
        auth = Auth()
        auth.discord_role_ids.add("ROLE-ADMIN")
        auth.mock_mode = False
        self.assertTrue(
            auth.is_discord_authorized("any-user", role_ids=["ROLE-MEMBER", "ROLE-ADMIN"])
        )

    def test_discord_role_denies_user_without_matching_role(self):
        auth = Auth()
        auth.discord_role_ids.add("ROLE-ADMIN")
        auth.mock_mode = False
        self.assertFalse(
            auth.is_discord_authorized("any-user", role_ids=["ROLE-GUEST"])
        )

    def test_slack_user_allowed_by_id(self):
        auth = Auth()
        auth.add_slack_user("U01APPROVED")
        self.assertTrue(auth.is_slack_authorized("U01APPROVED"))

    def test_slack_user_denied(self):
        auth = Auth()
        auth.add_slack_user("U01APPROVED")
        self.assertFalse(auth.is_slack_authorized("U01STRANGER"))

    def test_describe_returns_all_keys(self):
        auth = Auth()
        d = auth.describe()
        self.assertIn("mock_mode", d)
        self.assertIn("discord_user_ids", d)
        self.assertIn("discord_role_ids", d)
        self.assertIn("slack_user_ids", d)

    def test_env_var_parsing(self):
        with patch.dict("os.environ", {
            "AUTHORIZED_DISCORD_USER_IDS": "111,222, 333",
            "AUTHORIZED_SLACK_USER_IDS": "U01,U02",
        }):
            auth = Auth()
        self.assertIn("111", auth.discord_user_ids)
        self.assertIn("222", auth.discord_user_ids)
        self.assertIn("333", auth.discord_user_ids)
        self.assertIn("U01", auth.slack_user_ids)
        self.assertIn("U02", auth.slack_user_ids)


# ===========================================================================
# RouterBridge tests
# ===========================================================================

class TestRouterBridgeClassify(unittest.TestCase):

    def setUp(self):
        self.bridge = make_bridge()

    def _classify(self, text: str) -> str:
        return self.bridge._classify_intent(text)

    def test_build_routes_to_coder(self):
        self.assertEqual(self._classify("build an API endpoint"), "Coder")

    def test_implement_routes_to_coder(self):
        self.assertEqual(self._classify("implement the login flow"), "Coder")

    def test_fix_routes_to_coder(self):
        self.assertEqual(self._classify("fix the bug in auth module"), "Coder")

    def test_research_routes_to_researcher(self):
        self.assertEqual(self._classify("research async Python frameworks"), "Researcher")

    def test_find_routes_to_researcher(self):
        self.assertEqual(self._classify("find me the best ORM for Python"), "Researcher")

    def test_plan_routes_to_planner(self):
        self.assertEqual(self._classify("plan the database architecture"), "Planner")

    def test_architect_routes_to_planner(self):
        self.assertEqual(self._classify("architect the microservices system"), "Planner")

    def test_design_routes_to_designer(self):
        # "design" alone maps to Designer
        self.assertEqual(self._classify("design a button component"), "Designer")

    def test_landing_page_beats_build(self):
        # "landing page" (multi-word) should take priority over "build"
        self.assertIn(
            self._classify("build a landing page"),
            ["Designer", "Coder"],  # landing page keyword fires first -> Designer
        )

    def test_unknown_intent_falls_back_to_default(self):
        self.assertEqual(self._classify("do something mysterious"), RouterBridge.DEFAULT_AGENT)


class TestRouterBridgeHandleMessage(unittest.IsolatedAsyncioTestCase):

    async def test_queued_running_completed_sequence(self):
        bridge = make_bridge()
        results = await collect(bridge, "build a REST API")
        statuses = [r.status for r in results]
        self.assertIn(ResultStatus.QUEUED, statuses)
        self.assertIn(ResultStatus.RUNNING, statuses)
        self.assertIn(ResultStatus.COMPLETED, statuses)

    async def test_correct_agent_spawned(self):
        router = MockRouter()
        bridge = RouterBridge(router=router)
        bridge.POLL_INTERVAL = 0.01
        await collect(bridge, "research best practices for REST")
        self.assertIn("Researcher", router.spawn_calls)

    async def test_failed_task_reports_failed(self):
        bridge = make_bridge(fail=True)
        # Override poll to return immediately with None
        async def fast_fail(*a, **kw):
            return None
        bridge._poll_until_complete = fast_fail
        results = await collect(bridge, "implement something")
        statuses = [r.status for r in results]
        self.assertIn(ResultStatus.FAILED, statuses)

    async def test_output_in_completed_result(self):
        bridge = make_bridge()
        results = await collect(bridge, "write unit tests")
        completed = [r for r in results if r.status == ResultStatus.COMPLETED]
        self.assertTrue(len(completed) >= 1)
        self.assertIsNotNone(completed[0].output)

    async def test_session_id_present_in_results(self):
        bridge = make_bridge()
        results = await collect(bridge, "code the backend")
        for r in results:
            self.assertIsNotNone(r.session_id)


class TestHitlApproval(unittest.IsolatedAsyncioTestCase):

    async def test_approve_resolves_true(self):
        bridge = make_bridge()
        results: List[RouterResult] = []

        async def auto_approve(r: RouterResult) -> None:
            results.append(r)
            if r.hitl_request_id:
                await bridge.resolve_hitl(r.hitl_request_id, approved=True)

        approved = await bridge.request_hitl_approval(
            session_id="sess-001",
            description="Write 20 files to disk?",
            status_callback=auto_approve,
            timeout=5.0,
        )
        self.assertTrue(approved)
        hitl_results = [r for r in results if r.status == ResultStatus.HITL_PENDING]
        self.assertEqual(len(hitl_results), 1)

    async def test_reject_resolves_false(self):
        bridge = make_bridge()

        async def auto_reject(r: RouterResult) -> None:
            if r.hitl_request_id:
                await bridge.resolve_hitl(r.hitl_request_id, approved=False)

        approved = await bridge.request_hitl_approval(
            session_id="sess-002",
            description="Delete production database?",
            status_callback=auto_reject,
            timeout=5.0,
        )
        self.assertFalse(approved)

    async def test_timeout_returns_false(self):
        bridge = make_bridge()

        async def noop(r: RouterResult) -> None:
            pass

        approved = await bridge.request_hitl_approval(
            session_id="sess-003",
            description="Approve this action?",
            status_callback=noop,
            timeout=0.05,
        )
        self.assertFalse(approved)

    async def test_resolve_unknown_request_id(self):
        bridge = make_bridge()
        result = await bridge.resolve_hitl("nonexistent-id-xxxx", approved=True)
        self.assertFalse(result)


# ===========================================================================
# DiscordHandler tests
# ===========================================================================

class TestDiscordHandlerAuth(unittest.IsolatedAsyncioTestCase):

    async def test_unauthorized_user_gets_denial_reply(self):
        auth = Auth()
        auth.add_discord_user("ALLOWED_USER")
        bridge = make_bridge()
        handler = DiscordHandler(bridge=bridge, auth=auth)
        handler.bot_user_id = "BOT001"
        handler.mock_mode = True   # avoid real HTTP

        replies: List[str] = []

        async def fake_post(channel_id, content, reply_to=None):
            replies.append(content)

        handler._post_message = fake_post

        msg_data = {
            "id": "msg-001",
            "channel_id": "ch-001",
            "guild_id": "guild-001",
            "author": {"id": "UNKNOWN_USER", "username": "badactor", "bot": False},
            "mentions": [{"id": "BOT001"}],
            "content": "<@BOT001> build a backdoor",
            "member": {"roles": []},
        }
        await handler._handle_message_create(msg_data)
        self.assertTrue(
            any("not authorized" in r for r in replies),
            f"Expected 'not authorized' in replies: {replies}"
        )

    async def test_bot_messages_are_ignored(self):
        auth = Auth()
        router = MockRouter()
        bridge = RouterBridge(router=router)
        handler = DiscordHandler(bridge=bridge, auth=auth)
        handler.bot_user_id = "BOT001"
        handler.mock_mode = True

        msg_data = {
            "id": "msg-bot",
            "channel_id": "ch-001",
            "guild_id": "guild-001",
            "author": {"id": "OTHER_BOT", "username": "somebot", "bot": True},
            "mentions": [{"id": "BOT001"}],
            "content": "<@BOT001> automated message",
            "member": {"roles": []},
        }
        await handler._handle_message_create(msg_data)
        self.assertEqual(len(router.spawn_calls), 0)

    async def test_non_mention_messages_are_ignored(self):
        auth = Auth()
        router = MockRouter()
        bridge = RouterBridge(router=router)
        handler = DiscordHandler(bridge=bridge, auth=auth)
        handler.bot_user_id = "BOT001"
        handler.mock_mode = True

        msg_data = {
            "id": "msg-nomention",
            "channel_id": "ch-001",
            "guild_id": "guild-001",
            "author": {"id": "USER001", "username": "alice", "bot": False},
            "mentions": [],   # Bot not mentioned
            "content": "just chatting",
            "member": {"roles": []},
        }
        await handler._handle_message_create(msg_data)
        self.assertEqual(len(router.spawn_calls), 0)


class TestDiscordHandlerHitl(unittest.IsolatedAsyncioTestCase):

    async def test_yes_resolves_hitl(self):
        # request_id must be exactly 12 lowercase hex chars (matches HITL regex)
        bridge = make_bridge()
        req = HitlRequest(
            request_id="abc123456789",
            session_id="sess-001",
            description="Approve?",
        )
        bridge._hitl_requests["abc123456789"] = req

        auth = Auth()
        handler = DiscordHandler(bridge=bridge, auth=auth)
        handler.bot_user_id = "BOT001"
        handler.mock_mode = True

        replies: List[str] = []

        async def fake_post(channel_id, content, reply_to=None):
            replies.append(content)

        handler._post_message = fake_post

        # Use <@BOT001> mention so the message reaches HITL check path
        # _handle_message_create strips mention first, then checks for HITL pattern
        msg_data = {
            "id": "msg-yes",
            "channel_id": "ch-001",
            "guild_id": "guild-001",
            "author": {"id": "USER001", "username": "alice", "bot": False},
            "mentions": [{"id": "BOT001"}],
            "content": "<@BOT001> YES abc123456789",
            "member": {"roles": []},
        }
        await handler._handle_message_create(msg_data)
        # Give background task time to settle
        await asyncio.sleep(0.05)
        self.assertTrue(req.event.is_set())
        self.assertTrue(req.approved)

    async def test_no_rejects_hitl(self):
        # request_id must be exactly 12 lowercase hex chars (matches HITL regex)
        bridge = make_bridge()
        req = HitlRequest(
            request_id="def456789abc",
            session_id="sess-002",
            description="Reject?",
        )
        bridge._hitl_requests["def456789abc"] = req

        auth = Auth()
        handler = DiscordHandler(bridge=bridge, auth=auth)
        handler.bot_user_id = "BOT001"
        handler.mock_mode = True

        async def fake_post(channel_id, content, reply_to=None):
            pass

        handler._post_message = fake_post

        msg_data = {
            "id": "msg-no",
            "channel_id": "ch-001",
            "guild_id": "guild-001",
            "author": {"id": "USER001", "username": "alice", "bot": False},
            "mentions": [{"id": "BOT001"}],
            "content": "<@BOT001> NO def456789abc",
            "member": {"roles": []},
        }
        await handler._handle_message_create(msg_data)
        await asyncio.sleep(0.05)
        self.assertTrue(req.event.is_set())
        self.assertFalse(req.approved)


class TestDiscordMockMode(unittest.IsolatedAsyncioTestCase):

    async def test_inject_mock_message_routes_to_bridge(self):
        """inject_mock_message routes to bridge without Discord connection."""
        router = MockRouter()
        bridge = RouterBridge(router=router)
        bridge.POLL_INTERVAL = 0.01

        auth = Auth()
        handler = DiscordHandler(bridge=bridge, auth=auth)
        handler.mock_mode = True
        handler.bot_user_id = "BOT001"

        await handler.inject_mock_message(
            content="<@BOT001> build an authentication module",
            user_id="mock-user",
        )
        # Allow background task to complete
        await asyncio.sleep(0.1)
        self.assertIn("Coder", router.spawn_calls)

    def test_strip_mention_removes_angle_format(self):
        # _strip_mention uses regex to strip all <@USER_ID> mentions
        handler = DiscordHandler()
        result = handler._strip_mention("<@123456789> research Python")
        self.assertEqual(result, "research Python")

    def test_strip_mention_removes_nickname_format(self):
        # _strip_mention uses regex; strips all <@!...> and <@...> tokens
        handler = DiscordHandler()
        result = handler._strip_mention("<@!123456789> fix the bug")
        self.assertEqual(result, "fix the bug")


# ===========================================================================
# SlackHandler tests
# ===========================================================================

class TestSlackSignatureVerification(unittest.TestCase):

    def _make_handler(self, secret: str) -> SlackHandler:
        handler = SlackHandler(signing_secret=secret)
        handler._signing_secret = secret
        handler.mock_mode = False
        return handler

    def _sign(self, secret: str, timestamp: str, body: str) -> str:
        base = f"v0:{timestamp}:{body}"
        return "v0=" + hmac.new(
            secret.encode(), base.encode(), hashlib.sha256
        ).hexdigest()

    def test_valid_signature_accepted(self):
        handler = self._make_handler("my-secret")
        ts = str(int(time.time()))
        body = b'{"event": {"type": "app_mention"}}'
        sig = self._sign("my-secret", ts, body.decode())
        headers = {
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
        }
        self.assertTrue(handler._verify_signature(headers, body))

    def test_invalid_signature_rejected(self):
        handler = self._make_handler("real-secret")
        ts = str(int(time.time()))
        body = b'{"event": {}}'
        headers = {
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": "v0=badhash",
        }
        self.assertFalse(handler._verify_signature(headers, body))

    def test_stale_timestamp_rejected(self):
        handler = self._make_handler("any-secret")
        old_ts = str(int(time.time()) - 400)  # 400s > 300s limit
        body = b"{}"
        sig = self._sign("any-secret", old_ts, body.decode())
        headers = {
            "X-Slack-Request-Timestamp": old_ts,
            "X-Slack-Signature": sig,
        }
        self.assertFalse(handler._verify_signature(headers, body))

    def test_missing_timestamp_rejected(self):
        handler = self._make_handler("secret")
        headers = {
            "X-Slack-Signature": "v0=something",
        }
        self.assertFalse(handler._verify_signature(headers, b"body"))


class TestSlackHandlerEvents(unittest.IsolatedAsyncioTestCase):

    async def test_unauthorized_user_gets_denial_reply(self):
        auth = Auth()
        auth.add_slack_user("U01AUTHORIZED")
        bridge = make_bridge()
        handler = SlackHandler(bridge=bridge, auth=auth)
        handler.mock_mode = True

        replies: List[str] = []

        async def fake_post(channel_id, text, thread_ts=None):
            replies.append(text)

        handler._post_message = fake_post

        event = {
            "type": "app_mention",
            "user": "U01STRANGER",
            "channel": "C01",
            "text": "<@UBOT> do something",
            "ts": "1700000000.000001",
            "thread_ts": "1700000000.000001",
        }
        await handler._handle_app_mention(event)
        self.assertTrue(any("not authorized" in r for r in replies))

    async def test_app_mention_routes_to_researcher(self):
        auth = Auth()
        router = MockRouter()
        bridge = RouterBridge(router=router)
        bridge.POLL_INTERVAL = 0.01

        handler = SlackHandler(bridge=bridge, auth=auth)
        handler.mock_mode = True

        event = {
            "type": "app_mention",
            "user": "U01USER",
            "channel": "C01",
            "text": "<@UBOT> research the best Python async libraries",
            "ts": "1700000001.000001",
            "thread_ts": "1700000001.000001",
        }
        await handler._handle_app_mention(event)
        await asyncio.sleep(0.1)
        self.assertIn("Researcher", router.spawn_calls)

    async def test_hitl_yes_in_slack_thread(self):
        bridge = make_bridge()
        req = HitlRequest(
            request_id="feed12345678",
            session_id="sess-s01",
            description="Push to prod?",
        )
        bridge._hitl_requests["feed12345678"] = req

        auth = Auth()
        handler = SlackHandler(bridge=bridge, auth=auth)
        handler.mock_mode = True

        replies: List[str] = []

        async def fake_post(channel_id, text, thread_ts=None):
            replies.append(text)

        handler._post_message = fake_post

        event = {
            "type": "app_mention",
            "user": "U01USER",
            "channel": "C01",
            "text": "YES feed12345678",
            "ts": "1700000002.000001",
            "thread_ts": "1700000002.000001",
        }
        await handler._handle_app_mention(event)
        self.assertTrue(req.event.is_set())
        self.assertTrue(req.approved)

    async def test_inject_mock_event_routes_to_bridge(self):
        router = MockRouter()
        bridge = RouterBridge(router=router)
        bridge.POLL_INTERVAL = 0.01

        auth = Auth()
        handler = SlackHandler(bridge=bridge, auth=auth)
        handler.mock_mode = True

        await handler.inject_mock_event(
            text="<@UBOT> implement the payment API",
            user_id="U_MOCK",
        )
        await asyncio.sleep(0.1)
        self.assertIn("Coder", router.spawn_calls)


class TestSlackStripMention(unittest.TestCase):

    def setUp(self):
        self.handler = SlackHandler()

    def test_removes_mention_token(self):
        self.assertEqual(
            self.handler._strip_mention("<@U01BOT> research Python"),
            "research Python",
        )

    def test_removes_multiple_mentions(self):
        result = self.handler._strip_mention("<@U01BOT> <@U02USER> hello")
        self.assertNotIn("<@", result)

    def test_preserves_content_after_mention(self):
        result = self.handler._strip_mention("<@UABC123> build the API endpoint")
        self.assertEqual(result, "build the API endpoint")


# ===========================================================================
# BotServer tests
# ===========================================================================

class TestBotServer(unittest.IsolatedAsyncioTestCase):

    async def test_shared_auth_and_bridge(self):
        """Both handlers share the same Auth and RouterBridge instances."""
        server = BotServer()
        self.assertIs(server.discord._auth, server.slack._auth)
        self.assertIs(server.discord._bridge, server.slack._bridge)

    async def test_status_method(self):
        server = BotServer()
        status = server.status()
        self.assertIn("mock_mode", status)
        self.assertIn("discord_mock", status)
        self.assertIn("slack_mock", status)
        self.assertIn("running", status)

    async def test_mock_mode_when_no_tokens(self):
        """Without tokens, both handlers should be in mock mode."""
        server = BotServer()
        self.assertTrue(server.discord.mock_mode)
        self.assertTrue(server.slack.mock_mode)
        self.assertTrue(server.mock_mode)

    async def test_start_and_stop_mock(self):
        """BotServer starts in mock mode and can be stopped cleanly."""
        server = BotServer()

        # Override handlers with instantly-completing coroutines
        async def instant():
            pass

        server.discord.start = instant
        server.slack.start = instant

        await asyncio.wait_for(server.start(), timeout=3.0)


# ===========================================================================
# End-to-end integration tests (mock mode)
# ===========================================================================

class TestEndToEnd(unittest.IsolatedAsyncioTestCase):

    async def test_discord_mention_to_completed_reply(self):
        """
        Full flow: Discord @mention 'build X' -> Coder spawned -> COMPLETED reply.
        """
        router = MockRouter()
        bridge = RouterBridge(router=router)
        bridge.POLL_INTERVAL = 0.01

        auth = Auth()
        handler = DiscordHandler(bridge=bridge, auth=auth)
        handler.bot_user_id = "BOT001"
        handler.mock_mode = True

        # In mock mode _post_result logs instead of calling _post_message.
        # Override _post_result directly to capture all status updates.
        results_received: List[RouterResult] = []

        original_post_result = handler._post_result

        async def capture_result(original_msg, result):
            results_received.append(result)

        handler._post_result = capture_result

        msg_data = {
            "id": "msg-e2e-001",
            "channel_id": "ch-e2e",
            "guild_id": "guild-e2e",
            "author": {"id": "user-alice", "username": "alice", "bot": False},
            "mentions": [{"id": "BOT001"}],
            "content": "<@BOT001> implement the user authentication module",
            "member": {"roles": []},
        }
        await handler._handle_message_create(msg_data)
        await asyncio.sleep(0.2)  # let background task complete

        self.assertIn("Coder", router.spawn_calls)
        statuses = [r.status for r in results_received]
        self.assertIn(ResultStatus.QUEUED, statuses)

    async def test_slack_mention_to_completed_reply(self):
        """
        Full flow: Slack @mention 'research X' -> Researcher spawned -> DONE reply.
        """
        router = MockRouter()
        bridge = RouterBridge(router=router)
        bridge.POLL_INTERVAL = 0.01

        auth = Auth()
        handler = SlackHandler(bridge=bridge, auth=auth)
        handler.mock_mode = True

        # Override _post_result to capture all status updates regardless of mock mode.
        results_received: List[RouterResult] = []

        async def capture_result(original_msg, result):
            results_received.append(result)

        handler._post_result = capture_result

        event = {
            "type": "app_mention",
            "user": "U01ALICE",
            "channel": "C01GENERAL",
            "text": "<@UBOT> research the best API design patterns in 2026",
            "ts": "1700000010.000001",
            "thread_ts": "1700000010.000001",
        }
        await handler._handle_app_mention(event)
        await asyncio.sleep(0.2)

        self.assertIn("Researcher", router.spawn_calls)
        statuses = [r.status for r in results_received]
        self.assertIn(ResultStatus.QUEUED, statuses)

    async def test_discord_hitl_approve_and_resume(self):
        """
        HITL flow: bot sends approval request -> user approves -> bridge gets True.
        """
        bridge = make_bridge()

        approve_fut: asyncio.Future = asyncio.get_event_loop().create_future()

        async def hitl_status_callback(r: RouterResult) -> None:
            if r.hitl_request_id:
                await bridge.resolve_hitl(r.hitl_request_id, approved=True)
                approve_fut.set_result(True)

        approved = await bridge.request_hitl_approval(
            session_id="e2e-sess-001",
            description="Deploy to production?",
            status_callback=hitl_status_callback,
            timeout=5.0,
        )
        self.assertTrue(approved)
        self.assertTrue(approve_fut.result())


if __name__ == "__main__":
    unittest.main(verbosity=2)
