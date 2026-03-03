#!/usr/bin/env python3
"""
discord_handler.py - Discord Gateway WebSocket handler for OpenPango bot integration.

Connects to the Discord Gateway via WebSocket (or runs in mock mode when no token
is configured), listens for message_create events where the bot is mentioned, strips
the mention, creates a ChatMessage, and passes it to RouterBridge for processing.

Protocol notes:
  - Opcode 10 Hello: server sends heartbeat_interval
  - Opcode 11 Heartbeat ACK: server acknowledges our heartbeat
  - Opcode 1  Heartbeat: we send this every heartbeat_interval ms
  - Opcode 2  Identify: we send this after Hello to authenticate
  - Opcode 0  Dispatch: incoming events (READY, MESSAGE_CREATE, etc.)
  - Opcode 7  Reconnect: server requests reconnect
  - Opcode 9  Invalid Session: re-identify required

PURE STDLIB — no discord.py or any third-party dependencies.
"""

import asyncio
import json
import logging
import os
import re
import ssl
import time
import urllib.request
from typing import Optional

from .router_bridge import ChatMessage, Platform, RouterBridge, RouterResult, ResultStatus
from .auth import Auth

logger = logging.getLogger("DiscordHandler")

# Discord Gateway and API constants
DISCORD_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
DISCORD_API_BASE = "https://discord.com/api/v10"

# Gateway opcodes
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11


class DiscordHandler:
    """
    Discord Gateway WebSocket handler.

    Maintains a persistent WebSocket connection to the Discord Gateway,
    handles heartbeating, identifies the bot, and routes MESSAGE_CREATE
    events that mention the bot to RouterBridge.

    Mock mode is activated automatically when DISCORD_BOT_TOKEN is not set.
    In mock mode, no network connections are made; all operations log to stdout.

    Usage:
        handler = DiscordHandler(bridge=RouterBridge(), auth=Auth())
        await handler.start()

    Attributes:
        mock_mode:   True when no DISCORD_BOT_TOKEN is configured.
        bot_user_id: Populated after a successful READY event.
    """

    def __init__(
        self,
        bridge: Optional[RouterBridge] = None,
        auth: Optional[Auth] = None,
        token: Optional[str] = None,
    ):
        self._token = token or os.getenv("DISCORD_BOT_TOKEN", "")
        self._bridge = bridge or RouterBridge()
        self._auth = auth or Auth()

        self.mock_mode = not bool(self._token)
        self.bot_user_id: Optional[str] = None
        self._running = False
        self._ws = None
        self._sequence: Optional[int] = None
        self._session_id: Optional[str] = None
        self._heartbeat_interval: float = 41.25  # seconds (Discord default ~41250ms)
        self._last_heartbeat_ack = True  # assume healthy on startup

        if self.mock_mode:
            logger.warning(
                "DiscordHandler: DISCORD_BOT_TOKEN not set. "
                "Running in MOCK mode — messages will be logged to stdout only."
            )
        else:
            logger.info("DiscordHandler: Token configured. Will connect to Discord Gateway.")

    # ── Public API ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Start the Discord handler.

        In mock mode: runs a simple stub loop that logs startup and then idles.
        In live mode: connects to the Discord Gateway and begins event processing.
        """
        self._running = True

        if self.mock_mode:
            await self._run_mock()
        else:
            await self._run_gateway()

    async def stop(self) -> None:
        """Gracefully stop the handler."""
        logger.info("DiscordHandler: Stopping.")
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def inject_mock_message(
        self,
        content: str,
        user_id: str = "mock-user-001",
        username: str = "MockUser",
        channel_id: str = "mock-channel-001",
        guild_id: str = "mock-guild-001",
    ) -> None:
        """
        Inject a mock message for testing without a real Discord connection.

        This method is safe to call in both mock mode and live mode; in live
        mode it bypasses the WebSocket entirely and routes directly to the bridge.

        Args:
            content:    Message text (should include a bot mention for realistic testing).
            user_id:    Simulated sender user ID.
            username:   Simulated sender username.
            channel_id: Simulated channel ID.
            guild_id:   Simulated guild (server) ID.
        """
        stripped = self._strip_mention(content)
        msg = ChatMessage(
            platform=Platform.DISCORD,
            user_id=user_id,
            username=username,
            channel_id=channel_id,
            thread_id=None,
            content=stripped,
            guild_id=guild_id,
            role_ids=[],
            message_id="mock-message-001",
        )
        logger.info(f"[MOCK DISCORD] Injected message from {username}: {stripped[:80]}")
        await self._handle_chat_message(msg)

    # ── Mock mode ──────────────────────────────────────────────────────────────

    async def _run_mock(self) -> None:
        """Mock event loop — idles and logs, no real connections."""
        logger.info("[MOCK DISCORD] Handler started. Waiting for injected messages.")
        while self._running:
            await asyncio.sleep(1.0)

    # ── Gateway connection ─────────────────────────────────────────────────────

    async def _run_gateway(self) -> None:
        """
        Main gateway connection loop with automatic reconnect.

        On Opcode 7 (Reconnect) or connection loss, waits briefly and reconnects.
        Tracks sequence numbers and session_id for proper session resumption.
        """
        backoff = 1.0
        while self._running:
            try:
                logger.info("DiscordHandler: Connecting to Discord Gateway...")
                await self._connect_and_run()
                backoff = 1.0  # reset on clean connection
            except Exception as exc:
                if not self._running:
                    break
                logger.warning(
                    f"DiscordHandler: Gateway connection error: {exc}. "
                    f"Reconnecting in {backoff:.1f}s..."
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _connect_and_run(self) -> None:
        """Open WebSocket, run heartbeat + receive loops concurrently."""
        import urllib.parse

        # Use asyncio streams via ssl for a pure-stdlib WebSocket handshake
        ws = await _WebSocket.connect(DISCORD_GATEWAY_URL)
        self._ws = ws

        try:
            # Run heartbeat sender and event receiver concurrently
            await asyncio.gather(
                self._heartbeat_loop(),
                self._receive_loop(),
            )
        finally:
            await ws.close()
            self._ws = None

    # ── Heartbeat loop ─────────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Send heartbeats at the interval specified by the Hello payload."""
        # Wait for Hello to set the interval
        await asyncio.sleep(0.5)

        while self._running and self._ws is not None:
            if not self._last_heartbeat_ack:
                logger.warning(
                    "DiscordHandler: No heartbeat ACK received. "
                    "Closing connection (zombie connection detected)."
                )
                await self._ws.close()
                return

            payload = {"op": OP_HEARTBEAT, "d": self._sequence}
            await self._ws.send(json.dumps(payload))
            self._last_heartbeat_ack = False
            logger.debug(f"DiscordHandler: Sent heartbeat (seq={self._sequence})")

            await asyncio.sleep(self._heartbeat_interval)

    # ── Receive loop ───────────────────────────────────────────────────────────

    async def _receive_loop(self) -> None:
        """Receive and dispatch Gateway messages."""
        while self._running and self._ws is not None:
            raw = await self._ws.recv()
            if raw is None:
                logger.info("DiscordHandler: WebSocket closed by server.")
                return

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.warning(f"DiscordHandler: Failed to parse Gateway message: {exc}")
                continue

            op = data.get("op")
            seq = data.get("s")
            if seq is not None:
                self._sequence = seq

            if op == OP_HELLO:
                await self._handle_hello(data)
            elif op == OP_HEARTBEAT_ACK:
                self._last_heartbeat_ack = True
                logger.debug("DiscordHandler: Heartbeat ACK received.")
            elif op == OP_HEARTBEAT:
                # Server requesting an immediate heartbeat
                payload = {"op": OP_HEARTBEAT, "d": self._sequence}
                await self._ws.send(json.dumps(payload))
            elif op == OP_DISPATCH:
                await self._handle_dispatch(data)
            elif op == OP_RECONNECT:
                logger.info("DiscordHandler: Server requested reconnect.")
                await self._ws.close()
                return
            elif op == OP_INVALID_SESSION:
                resumable = data.get("d", False)
                logger.warning(
                    f"DiscordHandler: Invalid session (resumable={resumable}). Re-identifying."
                )
                if not resumable:
                    self._session_id = None
                    self._sequence = None
                await asyncio.sleep(5.0)
                await self._identify()

    # ── Gateway event handlers ─────────────────────────────────────────────────

    async def _handle_hello(self, data: dict) -> None:
        """Handle Opcode 10 Hello — set heartbeat interval and identify."""
        interval_ms = data.get("d", {}).get("heartbeat_interval", 41250)
        self._heartbeat_interval = interval_ms / 1000.0
        logger.info(
            f"DiscordHandler: Received Hello. Heartbeat interval: {self._heartbeat_interval:.1f}s"
        )
        await self._identify()

    async def _identify(self) -> None:
        """Send Opcode 2 Identify to authenticate with the Gateway."""
        payload = {
            "op": OP_IDENTIFY,
            "d": {
                "token": self._token,
                "intents": 33280,  # GUILD_MESSAGES (512) + MESSAGE_CONTENT (32768)
                "properties": {
                    "os": "linux",
                    "browser": "openpango-bot",
                    "device": "openpango-bot",
                },
            },
        }
        await self._ws.send(json.dumps(payload))
        logger.info("DiscordHandler: Sent Identify.")

    async def _handle_dispatch(self, data: dict) -> None:
        """Route dispatch events (t field) to the appropriate handler."""
        event_type = data.get("t")

        if event_type == "READY":
            user = data.get("d", {}).get("user", {})
            self.bot_user_id = user.get("id")
            self._session_id = data.get("d", {}).get("session_id")
            logger.info(
                f"DiscordHandler: READY. Bot user_id={self.bot_user_id}, "
                f"username={user.get('username')}"
            )

        elif event_type == "MESSAGE_CREATE":
            await self._handle_message_create(data.get("d", {}))

    async def _handle_message_create(self, msg_data: dict) -> None:
        """
        Process a MESSAGE_CREATE event.

        HITL YES/NO replies are processed from any message in the channel
        (no bot mention required) so users can respond to approval prompts
        naturally. All other commands require a direct bot mention.

        Extracts the content, checks authorization, builds a ChatMessage,
        and passes to the bridge.
        """
        # Ignore messages from bots (including ourselves)
        author = msg_data.get("author", {})
        if author.get("bot", False):
            return

        user_id = author.get("id", "unknown")
        username = author.get("username", "unknown")
        channel_id = msg_data.get("channel_id", "unknown")
        guild_id = msg_data.get("guild_id")
        message_id = msg_data.get("id")

        # Extract role IDs from member data (present in guild messages)
        member = msg_data.get("member", {})
        role_ids = member.get("roles", [])

        raw_content = msg_data.get("content", "")

        # Check for HITL resolution FIRST — works with or without a bot mention.
        # Pattern: "YES <12-hex-char-id>" or "NO <12-hex-char-id>"
        stripped_for_hitl = self._strip_mention(raw_content)
        hitl_match = re.match(
            r"^(YES|NO)\s+([a-f0-9]{12})$", stripped_for_hitl.strip(), re.IGNORECASE
        )
        if hitl_match:
            approved = hitl_match.group(1).upper() == "YES"
            request_id = hitl_match.group(2)
            resolved = await self._bridge.resolve_hitl(request_id, approved)
            if resolved:
                word = "approved" if approved else "rejected"
                await self._post_message(
                    channel_id=channel_id,
                    content=f"<@{user_id}> Action {word}.",
                    reply_to=message_id,
                )
            return

        # All other commands require a direct bot mention
        mentions = msg_data.get("mentions", [])
        if self.bot_user_id:
            mentioned = any(m.get("id") == self.bot_user_id for m in mentions)
        else:
            # If we somehow don't know our ID yet, check for any mention
            mentioned = bool(mentions)

        if not mentioned:
            return

        # Authorization check
        if not self._auth.is_discord_authorized(user_id, role_ids):
            logger.info(
                f"DiscordHandler: Unauthorized message from {username} ({user_id}). Ignoring."
            )
            await self._post_message(
                channel_id=channel_id,
                content=f"Sorry <@{user_id}>, you are not authorized to use this bot.",
                reply_to=message_id,
            )
            return

        # Strip the mention(s) from the content
        stripped = self._strip_mention(raw_content)
        if not stripped:
            await self._post_message(
                channel_id=channel_id,
                content=f"Hi <@{user_id}>! Mention me with a command, e.g. `@Agent research Python async`",
                reply_to=message_id,
            )
            return

        chat_msg = ChatMessage(
            platform=Platform.DISCORD,
            user_id=user_id,
            username=username,
            channel_id=channel_id,
            thread_id=message_id,  # thread back to this message
            content=stripped,
            guild_id=guild_id,
            role_ids=role_ids,
            message_id=message_id,
        )

        logger.info(
            f"DiscordHandler: Message from {username} in {channel_id}: {stripped[:80]}"
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
        """Return an async callback that posts RouterResult updates back to Discord."""
        async def callback(result: RouterResult) -> None:
            await self._post_result(original_msg, result)
        return callback

    async def _post_result(self, original_msg: ChatMessage, result: RouterResult) -> None:
        """Convert a RouterResult to a Discord message and post it."""
        # Prepend status indicator
        prefix_map = {
            ResultStatus.QUEUED:       "[ QUEUED ]",
            ResultStatus.RUNNING:      "[ RUNNING ]",
            ResultStatus.HITL_PENDING: "[ APPROVAL NEEDED ]",
            ResultStatus.COMPLETED:    "[ DONE ]",
            ResultStatus.FAILED:       "[ FAILED ]",
            ResultStatus.DENIED:       "[ DENIED ]",
            ResultStatus.CANCELLED:    "[ CANCELLED ]",
        }
        prefix = prefix_map.get(result.status, f"[{result.status}]")
        content = f"{prefix} {result.message}"

        if self.mock_mode:
            logger.info(
                f"[MOCK DISCORD] -> #{original_msg.channel_id} "
                f"(reply to {original_msg.thread_id}): {content[:120]}"
            )
            return

        await self._post_message(
            channel_id=original_msg.channel_id,
            content=content,
            reply_to=original_msg.thread_id,
        )

    # ── Discord REST API helpers ───────────────────────────────────────────────

    async def _post_message(
        self,
        channel_id: str,
        content: str,
        reply_to: Optional[str] = None,
    ) -> None:
        """
        Post a message to a Discord channel via the REST API.

        Truncates content to Discord's 2000-character limit.
        Runs in an executor to avoid blocking the event loop.
        """
        if self.mock_mode:
            logger.info(f"[MOCK DISCORD] POST /{channel_id}: {content[:100]}")
            return

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            self._post_message_sync,
            channel_id,
            content[:2000],
            reply_to,
        )

    def _post_message_sync(
        self,
        channel_id: str,
        content: str,
        reply_to: Optional[str],
    ) -> None:
        """Synchronous REST POST to Discord channels endpoint."""
        url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
        payload: dict = {"content": content}
        if reply_to:
            payload["message_reference"] = {
                "message_id": reply_to,
                "fail_if_not_exists": False,
            }

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bot {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "OpenPangoBot/1.0",
            },
        )
        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                logger.debug(f"DiscordHandler: POST message -> {resp.status}")
        except urllib.error.HTTPError as exc:
            logger.error(f"DiscordHandler: HTTP error posting message: {exc.code} {exc.reason}")
        except Exception as exc:
            logger.error(f"DiscordHandler: Error posting message: {exc}")

    # ── Utility ────────────────────────────────────────────────────────────────

    def _strip_mention(self, text: str) -> str:
        """
        Remove all Discord user mentions from a string.

        Handles both real Discord snowflake IDs (<@!123456789>) and the
        alphanumeric IDs used in tests (<@BOT001>). The pattern matches any
        <@...> or <@!...> token so it is robust to both formats.

        Returns the remaining content stripped of leading/trailing whitespace.
        """
        cleaned = re.sub(r"<@!?[A-Za-z0-9]+>", "", text)
        return cleaned.strip()


# ---------------------------------------------------------------------------
# Minimal pure-stdlib WebSocket client
# ---------------------------------------------------------------------------

class _WebSocket:
    """
    Minimal WebSocket client using asyncio streams and stdlib only.

    Implements just enough of RFC 6455 for the Discord Gateway:
      - Client-side handshake (Upgrade request)
      - Sending text frames (with masking)
      - Receiving text frames (unmasked from server)
      - Close frame handling

    This is NOT a full RFC 6455 implementation. It handles the Discord
    Gateway's actual usage pattern which is exclusively text frames.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._closed = False

    @classmethod
    async def connect(cls, url: str) -> "_WebSocket":
        """Perform the WebSocket handshake and return a connected instance."""
        import base64
        import hashlib
        import secrets

        # Parse the WSS URL
        # wss://gateway.discord.gg/?v=10&encoding=json
        assert url.startswith("wss://"), "Only wss:// URLs are supported"
        rest = url[6:]  # strip "wss://"
        host_part, _, path_part = rest.partition("/")
        host = host_part
        path = "/" + path_part if path_part else "/"
        port = 443

        # Connect with SSL
        ctx = ssl.create_default_context()
        reader, writer = await asyncio.open_connection(host, port, ssl=ctx)

        # Generate a Sec-WebSocket-Key
        key_bytes = secrets.token_bytes(16)
        ws_key = base64.b64encode(key_bytes).decode()
        ws_accept_expected = base64.b64encode(
            hashlib.sha1(
                (ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()
        ).decode()

        # Send HTTP Upgrade request
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        writer.write(handshake.encode())
        await writer.drain()

        # Read the HTTP response
        response_lines = []
        while True:
            line = await reader.readline()
            decoded = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not decoded:
                break
            response_lines.append(decoded)

        if not response_lines or "101" not in response_lines[0]:
            raise ConnectionError(
                f"WebSocket handshake failed: {response_lines[0] if response_lines else 'no response'}"
            )

        logger.debug(f"_WebSocket: Handshake OK ({response_lines[0]})")
        return cls(reader, writer)

    async def send(self, text: str) -> None:
        """Send a masked text frame."""
        import os as _os
        if self._closed:
            raise ConnectionError("WebSocket is closed")

        payload = text.encode("utf-8")
        mask_key = _os.urandom(4)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        length = len(payload)
        header = bytearray()
        # FIN=1, opcode=1 (text)
        header.append(0x81)

        if length <= 125:
            header.append(0x80 | length)  # MASK bit set
        elif length <= 65535:
            header.append(0x80 | 126)
            header += length.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += length.to_bytes(8, "big")

        self._writer.write(bytes(header) + mask_key + masked)
        await self._writer.drain()

    async def recv(self) -> Optional[str]:
        """
        Receive the next WebSocket frame.

        Returns the text payload, or None if the connection was closed.
        Handles fragmented frames by reassembling them.
        """
        fragments = []
        while True:
            # Read the first two bytes (FIN/RSV/opcode + MASK/payload_len)
            header = await self._read_exactly(2)
            if header is None:
                return None

            byte1, byte2 = header[0], header[1]
            fin = bool(byte1 & 0x80)
            opcode = byte1 & 0x0F
            masked = bool(byte2 & 0x80)
            payload_len = byte2 & 0x7F

            if payload_len == 126:
                ext = await self._read_exactly(2)
                if ext is None:
                    return None
                payload_len = int.from_bytes(ext, "big")
            elif payload_len == 127:
                ext = await self._read_exactly(8)
                if ext is None:
                    return None
                payload_len = int.from_bytes(ext, "big")

            mask_key = None
            if masked:
                mask_key = await self._read_exactly(4)
                if mask_key is None:
                    return None

            payload_bytes = await self._read_exactly(payload_len)
            if payload_bytes is None:
                return None

            if masked and mask_key:
                payload_bytes = bytes(
                    b ^ mask_key[i % 4] for i, b in enumerate(payload_bytes)
                )

            # Handle control frames inline
            if opcode == 0x8:  # Close
                self._closed = True
                # Echo close frame
                try:
                    self._writer.write(b"\x88\x00")
                    await self._writer.drain()
                except Exception:
                    pass
                return None
            elif opcode == 0x9:  # Ping
                # Send Pong
                pong = bytearray([0x8A, len(payload_bytes)])
                pong += payload_bytes
                try:
                    self._writer.write(bytes(pong))
                    await self._writer.drain()
                except Exception:
                    pass
                continue
            elif opcode == 0xA:  # Pong
                continue

            fragments.append(payload_bytes)

            if fin:
                full_payload = b"".join(fragments)
                return full_payload.decode("utf-8", errors="replace")
            # else: continuation frame — keep reading

    async def close(self) -> None:
        """Send a close frame and close the underlying connection."""
        if not self._closed:
            self._closed = True
            try:
                self._writer.write(b"\x88\x00")
                await self._writer.drain()
            except Exception:
                pass
        try:
            self._writer.close()
        except Exception:
            pass

    async def _read_exactly(self, n: int) -> Optional[bytes]:
        """Read exactly n bytes; return None on EOF."""
        if n == 0:
            return b""
        try:
            data = await self._reader.readexactly(n)
            return data
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
