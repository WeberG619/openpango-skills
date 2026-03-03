#!/usr/bin/env python3
"""
slack_handler.py - Slack Events API HTTP handler for OpenPango bot integration.

Runs a lightweight HTTP server (stdlib http.server) that receives Slack Events
API webhooks, verifies requests using HMAC-SHA256, and routes app_mention events
to RouterBridge for processing.

Slack Events API flow:
  1. Slack sends POST to /slack/events with a JSON body
  2. We verify the request signature using SLACK_SIGNING_SECRET
  3. For url_verification challenges: echo the challenge back immediately
  4. For app_mention events: strip the bot mention, build ChatMessage, route to bridge
  5. Post status updates and results back via Slack Web API (chat.postMessage)

PURE STDLIB — no slack_sdk or any third-party dependencies.
"""

import asyncio
import hashlib
import hmac
import http.server
import json
import logging
import os
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional

from .router_bridge import ChatMessage, Platform, RouterBridge, RouterResult, ResultStatus
from .auth import Auth

logger = logging.getLogger("SlackHandler")

SLACK_API_BASE = "https://slack.com/api"
SLACK_EVENTS_PATH = "/slack/events"


class SlackHandler:
    """
    Slack Events API HTTP handler.

    Starts a stdlib HTTP server on SLACK_PORT (default 3000) to receive
    incoming Slack event payloads.  Requests are signature-verified using
    SLACK_SIGNING_SECRET, and app_mention events are routed to RouterBridge.

    Mock mode is activated when either SLACK_BOT_TOKEN or SLACK_SIGNING_SECRET
    is not configured.  In mock mode no HTTP server is started; all operations
    log to stdout.

    Usage:
        handler = SlackHandler(bridge=RouterBridge(), auth=Auth())
        await handler.start()

    Attributes:
        mock_mode: True when Slack credentials are not configured.
        port:      The HTTP port the server listens on.
    """

    def __init__(
        self,
        bridge: Optional[RouterBridge] = None,
        auth: Optional[Auth] = None,
        token: Optional[str] = None,
        signing_secret: Optional[str] = None,
        port: Optional[int] = None,
    ):
        self._token = token or os.getenv("SLACK_BOT_TOKEN", "")
        self._signing_secret = signing_secret or os.getenv("SLACK_SIGNING_SECRET", "")
        self._bridge = bridge or RouterBridge()
        self._auth = auth or Auth()
        self.port = port or int(os.getenv("SLACK_PORT", "3000"))

        self.mock_mode = not (self._token and self._signing_secret)
        self._running = False
        self._server: Optional[http.server.HTTPServer] = None
        # asyncio event loop reference for calling async handlers from sync threads
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        if self.mock_mode:
            logger.warning(
                "SlackHandler: SLACK_BOT_TOKEN and/or SLACK_SIGNING_SECRET not set. "
                "Running in MOCK mode — events will be logged to stdout only."
            )
        else:
            logger.info(
                f"SlackHandler: Credentials configured. "
                f"Will listen on port {self.port}."
            )

    # ── Public API ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Start the Slack handler.

        In mock mode: idles and logs, no HTTP server.
        In live mode: starts the HTTP server in a background thread and idles
                      the asyncio coroutine so the event loop stays alive.
        """
        self._running = True
        self._loop = asyncio.get_event_loop()

        if self.mock_mode:
            await self._run_mock()
        else:
            await self._run_server()

    async def stop(self) -> None:
        """Gracefully stop the HTTP server."""
        logger.info("SlackHandler: Stopping.")
        self._running = False
        if self._server is not None:
            self._server.shutdown()
            self._server = None

    async def inject_mock_event(
        self,
        text: str,
        user_id: str = "U_MOCKUSER",
        username: str = "mockuser",
        channel_id: str = "C_MOCKCHAN",
        team_id: str = "T_MOCKTEAM",
        thread_ts: Optional[str] = None,
    ) -> None:
        """
        Inject a mock Slack app_mention event for testing.

        Safe to call without a real Slack connection; routes directly to bridge.

        Args:
            text:       Message text (may include a bot mention like <@BOTID>).
            user_id:    Slack user ID string.
            username:   Slack username.
            channel_id: Slack channel ID.
            team_id:    Slack team (workspace) ID.
            thread_ts:  Optional thread timestamp to reply within a thread.
        """
        stripped = self._strip_mention(text)
        msg = ChatMessage(
            platform=Platform.SLACK,
            user_id=user_id,
            username=username,
            channel_id=channel_id,
            thread_id=thread_ts,
            content=stripped,
        )
        logger.info(f"[MOCK SLACK] Injected message from {username}: {stripped[:80]}")
        await self._handle_chat_message(msg)

    # ── Mock mode ──────────────────────────────────────────────────────────────

    async def _run_mock(self) -> None:
        """Mock event loop — idles and logs, no HTTP server started."""
        logger.info(
            f"[MOCK SLACK] Handler started. "
            f"Would listen on port {self.port}. Waiting for injected events."
        )
        while self._running:
            await asyncio.sleep(1.0)

    # ── HTTP server ────────────────────────────────────────────────────────────

    async def _run_server(self) -> None:
        """Start the HTTP server in a daemon thread and idle the coroutine."""
        handler_factory = self._make_handler_factory()
        self._server = http.server.HTTPServer(("0.0.0.0", self.port), handler_factory)
        server_thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="SlackHTTPServer",
        )
        server_thread.start()
        logger.info(
            f"SlackHandler: HTTP server listening on 0.0.0.0:{self.port}{SLACK_EVENTS_PATH}"
        )

        # Keep the coroutine alive while the server is running
        while self._running:
            await asyncio.sleep(1.0)

    def _make_handler_factory(self):
        """
        Return an HTTPRequestHandler class with a reference to this SlackHandler.

        We use a closure so the request handler can call back into our async methods
        via the stored event loop reference.
        """
        outer = self  # closure reference

        class _RequestHandler(http.server.BaseHTTPRequestHandler):

            def log_message(self, fmt, *args):
                # Redirect http.server logs to our logger
                logger.debug(f"SlackHTTP: {fmt % args}")

            def do_POST(self):
                if self.path != SLACK_EVENTS_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return

                # Read the body
                content_length = int(self.headers.get("Content-Length", 0))
                body_bytes = self.rfile.read(content_length)

                # Verify Slack signature
                if not outer._verify_signature(self.headers, body_bytes):
                    logger.warning("SlackHandler: Signature verification failed. Rejecting.")
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b"Unauthorized")
                    return

                # Parse JSON
                try:
                    payload = json.loads(body_bytes.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    logger.warning(f"SlackHandler: Bad JSON payload: {exc}")
                    self.send_response(400)
                    self.end_headers()
                    return

                event_type = payload.get("type")

                # URL verification challenge (one-time during Slack app setup)
                if event_type == "url_verification":
                    challenge = payload.get("challenge", "")
                    response_body = json.dumps({"challenge": challenge}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response_body)))
                    self.end_headers()
                    self.wfile.write(response_body)
                    logger.info("SlackHandler: Responded to url_verification challenge.")
                    return

                # Event callback
                if event_type == "event_callback":
                    # Acknowledge immediately (Slack requires < 3s response)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"ok")

                    # Dispatch event processing to the asyncio event loop
                    if outer._loop is not None:
                        asyncio.run_coroutine_threadsafe(
                            outer._handle_event_callback(payload),
                            outer._loop,
                        )
                    return

                # Unknown type — ack anyway
                self.send_response(200)
                self.end_headers()

        return _RequestHandler

    # ── Event handlers ─────────────────────────────────────────────────────────

    async def _handle_event_callback(self, payload: dict) -> None:
        """Process an event_callback payload from Slack."""
        event = payload.get("event", {})
        event_type = event.get("type")

        if event_type == "app_mention":
            await self._handle_app_mention(event)

    async def _handle_app_mention(self, event: dict) -> None:
        """
        Handle a Slack app_mention event.

        Strips the bot mention from the text, checks authorization, and
        routes to RouterBridge. Also handles HITL YES/NO replies.
        """
        user_id = event.get("user", "unknown")
        channel_id = event.get("channel", "unknown")
        text = event.get("text", "")
        thread_ts = event.get("thread_ts") or event.get("ts")
        event_ts = event.get("ts")

        # Authorization check
        if not self._auth.is_slack_authorized(user_id):
            logger.info(
                f"SlackHandler: Unauthorized mention from {user_id} in {channel_id}. Ignoring."
            )
            await self._post_message(
                channel_id=channel_id,
                text=f"<@{user_id}> Sorry, you are not authorized to use this bot.",
                thread_ts=thread_ts,
            )
            return

        # Strip the mention(s) from the text
        stripped = self._strip_mention(text)
        if not stripped:
            await self._post_message(
                channel_id=channel_id,
                text=(
                    f"<@{user_id}> Hi! Mention me with a command, "
                    f"e.g. `@agent research Python async`"
                ),
                thread_ts=thread_ts,
            )
            return

        # HITL resolution: "YES <id>" or "NO <id>"
        hitl_match = re.match(
            r"^(YES|NO)\s+([a-f0-9]{12})$", stripped.strip(), re.IGNORECASE
        )
        if hitl_match:
            approved = hitl_match.group(1).upper() == "YES"
            request_id = hitl_match.group(2)
            resolved = await self._bridge.resolve_hitl(request_id, approved)
            if resolved:
                word = "approved" if approved else "rejected"
                await self._post_message(
                    channel_id=channel_id,
                    text=f"<@{user_id}> Action {word}.",
                    thread_ts=thread_ts,
                )
            return

        # Attempt to resolve the username from the user_id (best-effort)
        username = await self._get_username(user_id)

        chat_msg = ChatMessage(
            platform=Platform.SLACK,
            user_id=user_id,
            username=username,
            channel_id=channel_id,
            thread_id=thread_ts,
            content=stripped,
        )

        logger.info(
            f"SlackHandler: Mention from {username} ({user_id}) "
            f"in {channel_id}: {stripped[:80]}"
        )
        await self._handle_chat_message(chat_msg)

    # ── Chat message -> Bridge ─────────────────────────────────────────────────

    async def _handle_chat_message(self, msg: ChatMessage) -> None:
        """Pass a ChatMessage to the RouterBridge as a background task."""
        asyncio.create_task(
            self._bridge.handle_message(
                message=msg,
                status_callback=self._make_status_callback(msg),
            )
        )

    def _make_status_callback(self, original_msg: ChatMessage):
        """Return an async callback that posts RouterResult updates back to Slack."""
        async def callback(result: RouterResult) -> None:
            await self._post_result(original_msg, result)
        return callback

    async def _post_result(self, original_msg: ChatMessage, result: RouterResult) -> None:
        """Convert a RouterResult to a Slack message and post it."""
        emoji_map = {
            ResultStatus.QUEUED:       ":hourglass_flowing_sand:",
            ResultStatus.RUNNING:      ":gear:",
            ResultStatus.HITL_PENDING: ":warning:",
            ResultStatus.COMPLETED:    ":white_check_mark:",
            ResultStatus.FAILED:       ":x:",
            ResultStatus.DENIED:       ":no_entry:",
            ResultStatus.CANCELLED:    ":stop_sign:",
        }
        emoji = emoji_map.get(result.status, ":speech_balloon:")
        text = f"{emoji} {result.message}"

        if self.mock_mode:
            logger.info(
                f"[MOCK SLACK] -> #{original_msg.channel_id} "
                f"(thread {original_msg.thread_id}): {text[:120]}"
            )
            return

        await self._post_message(
            channel_id=original_msg.channel_id,
            text=text,
            thread_ts=original_msg.thread_id,
        )

    # ── Slack Web API helpers ──────────────────────────────────────────────────

    async def _post_message(
        self,
        channel_id: str,
        text: str,
        thread_ts: Optional[str] = None,
    ) -> None:
        """
        Post a message to a Slack channel via chat.postMessage.

        Runs in an executor to avoid blocking the event loop.
        Truncates text to Slack's ~40,000 character limit.
        """
        if self.mock_mode:
            logger.info(f"[MOCK SLACK] POST /{channel_id}: {text[:100]}")
            return

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            self._post_message_sync,
            channel_id,
            text[:40000],
            thread_ts,
        )

    def _post_message_sync(
        self,
        channel_id: str,
        text: str,
        thread_ts: Optional[str],
    ) -> None:
        """Synchronous Slack chat.postMessage call."""
        url = f"{SLACK_API_BASE}/chat.postMessage"
        payload: dict = {
            "channel": channel_id,
            "text": text,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                if not resp_data.get("ok"):
                    logger.error(
                        f"SlackHandler: chat.postMessage error: {resp_data.get('error')}"
                    )
                else:
                    logger.debug(f"SlackHandler: Message posted to {channel_id}")
        except urllib.error.HTTPError as exc:
            logger.error(f"SlackHandler: HTTP error posting message: {exc.code} {exc.reason}")
        except Exception as exc:
            logger.error(f"SlackHandler: Error posting message: {exc}")

    async def _get_username(self, user_id: str) -> str:
        """
        Resolve a Slack user ID to a display name via users.info.

        Returns user_id on error (non-fatal: username is informational only).
        """
        if self.mock_mode:
            return f"mock_{user_id}"

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._get_username_sync, user_id)

    def _get_username_sync(self, user_id: str) -> str:
        """Synchronous Slack users.info call."""
        url = f"{SLACK_API_BASE}/users.info?user={user_id}"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("ok"):
                    profile = data.get("user", {}).get("profile", {})
                    return (
                        profile.get("display_name")
                        or profile.get("real_name")
                        or user_id
                    )
        except Exception as exc:
            logger.debug(f"SlackHandler: Could not resolve username for {user_id}: {exc}")
        return user_id

    # ── Signature verification ─────────────────────────────────────────────────

    def _verify_signature(self, headers, body_bytes: bytes) -> bool:
        """
        Verify the Slack request signature using HMAC-SHA256.

        Slack signs each request with:
          X-Slack-Signature: v0=<hex_digest>
          X-Slack-Request-Timestamp: <unix_timestamp>

        The base string is: "v0:<timestamp>:<body>"

        We reject requests older than 5 minutes to prevent replay attacks.

        Args:
            headers:    The HTTP request headers (dict-like).
            body_bytes: The raw request body bytes.

        Returns:
            True if the signature is valid, False otherwise.
        """
        if not self._signing_secret:
            # No secret configured — skip verification (mock mode guard)
            return True

        timestamp = headers.get("X-Slack-Request-Timestamp", "")
        signature = headers.get("X-Slack-Signature", "")

        if not timestamp or not signature:
            logger.debug("SlackHandler: Missing Slack signature headers.")
            return False

        # Replay attack prevention: reject requests older than 5 minutes
        try:
            ts_int = int(timestamp)
        except ValueError:
            return False

        if abs(time.time() - ts_int) > 300:
            logger.warning(
                f"SlackHandler: Request timestamp {timestamp} is too old. "
                "Possible replay attack."
            )
            return False

        # Compute HMAC-SHA256
        base_string = f"v0:{timestamp}:{body_bytes.decode('utf-8', errors='replace')}"
        expected = (
            "v0="
            + hmac.new(
                self._signing_secret.encode("utf-8"),
                base_string.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        )

        # Constant-time comparison to prevent timing attacks
        return hmac.compare_digest(expected, signature)

    # ── Utility ────────────────────────────────────────────────────────────────

    def _strip_mention(self, text: str) -> str:
        """
        Remove Slack user mentions (<@U...>) from a string.

        Returns the remaining content stripped of leading/trailing whitespace.
        """
        cleaned = re.sub(r"<@[A-Z0-9]+>", "", text)
        return cleaned.strip()
