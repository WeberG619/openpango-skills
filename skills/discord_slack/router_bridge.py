#!/usr/bin/env python3
"""
router_bridge.py - Bridge between chat messages and the OpenPango router.

Receives normalized ChatMessage objects from DiscordHandler / SlackHandler,
calls router.py (spawn_session / append_task / wait_for_completion), and
returns results + HITL events as RouterResult objects that the platform
handlers then post back into the chat thread.

Design decisions:
  - router.py is imported directly (not subprocess) for reliability.
  - Each command runs in a background asyncio thread so the bot stays live.
  - HITL approval is handled via an asyncio.Event + timeout.
  - All router calls are mockable by injecting a mock_router dependency.
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, Optional

logger = logging.getLogger("RouterBridge")

# ---------------------------------------------------------------------------
# Data types shared between bridge, Discord handler, and Slack handler
# ---------------------------------------------------------------------------

class Platform(str, Enum):
    DISCORD = "discord"
    SLACK = "slack"
    MOCK = "mock"


@dataclass
class ChatMessage:
    """Normalized inbound message from Discord or Slack."""
    platform: Platform
    user_id: str
    username: str
    channel_id: str
    thread_id: Optional[str]     # Discord: message_id for reply threading; Slack: thread_ts
    content: str                  # Raw text (bot mention already stripped by handler)
    guild_id: Optional[str] = None   # Discord only
    role_ids: Optional[list] = None  # Discord only (for auth)
    message_id: Optional[str] = None # Discord message snowflake


class ResultStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    HITL_PENDING = "hitl_pending"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"


@dataclass
class RouterResult:
    """Result or status update to post back into the chat thread."""
    status: ResultStatus
    message: str
    session_id: Optional[str] = None
    output: Optional[str] = None
    hitl_request_id: Optional[str] = None  # set when HITL approval is needed


# ---------------------------------------------------------------------------
# HITL approval tracker
# ---------------------------------------------------------------------------

@dataclass
class HitlRequest:
    """Pending HITL approval request waiting for a user reply."""
    request_id: str
    session_id: str
    description: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    approved: bool = False
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Router interface (replaceable for testing)
# ---------------------------------------------------------------------------

class RouterInterface:
    """
    Thin wrapper around router.py functions.

    Importing router.py injects its globals (STORAGE_FILE etc.) relative to
    the orchestration skill directory, so we do a direct import here.
    """

    def __init__(self):
        try:
            # Attempt real import; may fail in test environments
            import importlib.util
            router_path = (
                Path(__file__).parent.parent / "orchestration" / "router.py"
            )
            spec = importlib.util.spec_from_file_location("router", router_path)
            self._router = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self._router)
            self._mock = False
            logger.info(f"RouterInterface: Loaded router from {router_path}")
        except Exception as exc:
            logger.warning(
                f"RouterInterface: Could not load router.py ({exc}). "
                "Falling back to mock router."
            )
            self._mock = True
            self._router = None

    def spawn_session(self, agent_type: str) -> str:
        """Spawn a new agent session. Returns session_id."""
        if self._mock:
            sid = f"mock-{agent_type.lower()}-{uuid.uuid4().hex[:8]}"
            logger.info(f"[MOCK ROUTER] spawn_session({agent_type}) -> {sid}")
            return sid

        sid = str(uuid.uuid4())
        data = self._router.load_storage()
        data["sessions"][sid] = {
            "agent_type": agent_type,
            "status": "idle",
            "task": None,
            "output_file": None,
            "created_at": time.time(),
            "started_at": None,
            "completed_at": None,
        }
        self._router.save_storage(data)
        return sid

    def append_task(self, session_id: str, task_payload: str) -> None:
        """Append a task to an existing session (starts background execution)."""
        if self._mock:
            logger.info(
                f"[MOCK ROUTER] append_task({session_id}, {task_payload[:60]}...)"
            )
            return

        self._router.append_task(session_id, task_payload)

    def get_status(self, session_id: str) -> str:
        """Return the session status string."""
        if self._mock:
            return "completed"

        data = self._router.load_storage()
        return data["sessions"].get(session_id, {}).get("status", "unknown")

    def get_output(self, session_id: str) -> Optional[str]:
        """Return the session output text, or None if not ready."""
        if self._mock:
            return f"[MOCK OUTPUT] Task {session_id} completed successfully."

        data = self._router.load_storage()
        session = data["sessions"].get(session_id, {})
        if session.get("status") != "completed":
            return None
        output_path = session.get("output_file")
        if output_path and Path(output_path).exists():
            return Path(output_path).read_text()
        return None

    def wait_for_completion(
        self, session_id: str, timeout: float = 300.0, poll: float = 2.0
    ) -> Optional[str]:
        """
        Blocking poll until session completes or timeout.
        Returns output text on success, None on timeout/failure.
        """
        if self._mock:
            logger.info(f"[MOCK ROUTER] wait_for_completion({session_id}) -> done")
            return f"[MOCK OUTPUT] Task {session_id} completed successfully."

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.get_status(session_id)
            if status == "completed":
                return self.get_output(session_id)
            if status == "failed":
                return None
            time.sleep(poll)
        return None  # timeout


# ---------------------------------------------------------------------------
# RouterBridge — main orchestration bridge
# ---------------------------------------------------------------------------

# Callback type: async fn(result: RouterResult) -> None
StatusCallback = Callable[[RouterResult], Coroutine[Any, Any, None]]


class RouterBridge:
    """
    Bridge between normalized ChatMessage objects and router.py.

    Workflow per inbound message:
      1. Determine target agent type from message content.
      2. spawn_session() via RouterInterface.
      3. Fire status callback: QUEUED.
      4. append_task() in background thread (non-blocking).
      5. Poll for completion, firing RUNNING updates periodically.
      6. If HITL approval required: fire HITL_PENDING callback, wait for reply.
      7. Fire COMPLETED (or FAILED) callback with output.
    """

    # Keywords -> agent type routing table (matches router.py VALID_AGENTS).
    # ORDER MATTERS: longer / more specific phrases must come before shorter ones
    # so "landing page" is checked before "design", etc.
    ROUTING_TABLE: Dict[str, str] = {
        # Multi-word specifics first
        "landing page": "Designer",
        "look up": "Researcher",
        "what is": "Researcher",
        # Single-word keywords
        "research": "Researcher",
        "find": "Researcher",
        "lookup": "Researcher",
        "plan": "Planner",
        "architect": "Planner",
        "structure": "Planner",
        "build": "Coder",
        "code": "Coder",
        "implement": "Coder",
        "write": "Coder",
        "fix": "Coder",
        "create": "Coder",
        "ui": "Designer",
        "ux": "Designer",
        "frontend": "Designer",
        "style": "Designer",
        # "design" alone routes to Designer
        "design": "Designer",
    }

    DEFAULT_AGENT = "Researcher"

    POLL_INTERVAL = 5.0         # seconds between status polls
    STATUS_UPDATE_INTERVAL = 30.0  # seconds between "still running..." messages

    def __init__(self, router: Optional[RouterInterface] = None):
        self._router = router or RouterInterface()
        # hitl_requests maps request_id -> HitlRequest
        self._hitl_requests: Dict[str, HitlRequest] = {}
        # active_sessions maps session_id -> task info (for cancellation)
        self._active_sessions: Dict[str, dict] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    async def handle_message(
        self,
        message: ChatMessage,
        status_callback: StatusCallback,
    ) -> None:
        """
        Process an inbound chat message end-to-end.

        Runs the full spawn -> append -> poll -> reply cycle, calling
        `status_callback` at each significant state transition so the platform
        handler can post updates back into the chat thread.

        This coroutine is designed to be launched as a background task
        (asyncio.create_task) so the bot remains responsive.
        """
        agent_type = self._classify_intent(message.content)
        session_id = self._router.spawn_session(agent_type)
        self._active_sessions[session_id] = {
            "platform": message.platform,
            "channel_id": message.channel_id,
            "thread_id": message.thread_id,
            "user_id": message.user_id,
        }

        await status_callback(
            RouterResult(
                status=ResultStatus.QUEUED,
                session_id=session_id,
                message=(
                    f"Task queued for **{agent_type}** agent (session `{session_id[:8]}`).\n"
                    f"> {message.content[:200]}"
                ),
            )
        )

        # append_task is blocking (starts a background thread inside router.py)
        # Run in executor so we don't block the event loop
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, self._router.append_task, session_id, message.content
        )

        await status_callback(
            RouterResult(
                status=ResultStatus.RUNNING,
                session_id=session_id,
                message=f"Agent **{agent_type}** is working on your request...",
            )
        )

        # Poll for completion with periodic "still running" updates
        output = await self._poll_until_complete(
            session_id=session_id,
            agent_type=agent_type,
            status_callback=status_callback,
        )

        if output is None:
            await status_callback(
                RouterResult(
                    status=ResultStatus.FAILED,
                    session_id=session_id,
                    message=(
                        f"Agent **{agent_type}** did not complete within the "
                        f"timeout window. Session: `{session_id[:8]}`"
                    ),
                )
            )
        else:
            # Truncate very long outputs for chat readability
            display_output = output if len(output) <= 1800 else output[:1800] + "\n...[truncated]"
            await status_callback(
                RouterResult(
                    status=ResultStatus.COMPLETED,
                    session_id=session_id,
                    output=output,
                    message=(
                        f"**{agent_type}** completed (session `{session_id[:8]}`):\n\n"
                        f"{display_output}"
                    ),
                )
            )

        self._active_sessions.pop(session_id, None)

    async def resolve_hitl(self, request_id: str, approved: bool) -> bool:
        """
        Resolve a pending HITL approval request.

        Called when the user replies YES/NO to an approval prompt in-thread.
        Returns True if the request was found and resolved, False otherwise.
        """
        req = self._hitl_requests.get(request_id)
        if req is None:
            logger.warning(f"HITL: Unknown request_id {request_id}")
            return False
        req.approved = approved
        req.event.set()
        logger.info(f"HITL {request_id}: {'APPROVED' if approved else 'REJECTED'}")
        return True

    async def request_hitl_approval(
        self,
        session_id: str,
        description: str,
        status_callback: StatusCallback,
        timeout: float = 300.0,
    ) -> bool:
        """
        Pause execution and ask the user to approve/reject an action.

        Posts an approval prompt via status_callback, then waits up to
        `timeout` seconds for the user to call resolve_hitl().

        Returns True if approved, False if rejected or timed out.
        """
        request_id = uuid.uuid4().hex[:12]
        req = HitlRequest(
            request_id=request_id,
            session_id=session_id,
            description=description,
        )
        self._hitl_requests[request_id] = req

        await status_callback(
            RouterResult(
                status=ResultStatus.HITL_PENDING,
                session_id=session_id,
                hitl_request_id=request_id,
                message=(
                    f"**Approval required** (ID: `{request_id}`):\n"
                    f"{description}\n\n"
                    f"Reply `YES {request_id}` to approve or `NO {request_id}` to cancel."
                ),
            )
        )

        try:
            await asyncio.wait_for(req.event.wait(), timeout=timeout)
            return req.approved
        except asyncio.TimeoutError:
            logger.info(f"HITL {request_id}: timed out after {timeout}s")
            return False
        finally:
            self._hitl_requests.pop(request_id, None)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _classify_intent(self, text: str) -> str:
        """
        Map message text to a router agent type using the routing table.
        Returns DEFAULT_AGENT if no keyword matches.
        """
        lower = text.lower()
        for keyword, agent in self.ROUTING_TABLE.items():
            if keyword in lower:
                logger.debug(f"Routing '{text[:40]}...' -> {agent} (keyword: {keyword})")
                return agent
        logger.debug(f"Routing '{text[:40]}...' -> {self.DEFAULT_AGENT} (default)")
        return self.DEFAULT_AGENT

    async def _poll_until_complete(
        self,
        session_id: str,
        agent_type: str,
        status_callback: StatusCallback,
        timeout: float = 300.0,
    ) -> Optional[str]:
        """
        Async poll loop. Sends periodic "still running" updates via callback.
        Returns output text on completion, None on timeout/failure.
        """
        loop = asyncio.get_event_loop()
        deadline = time.monotonic() + timeout
        last_update = time.monotonic()

        while time.monotonic() < deadline:
            await asyncio.sleep(self.POLL_INTERVAL)

            status = await loop.run_in_executor(
                None, self._router.get_status, session_id
            )

            if status == "completed":
                output = await loop.run_in_executor(
                    None, self._router.get_output, session_id
                )
                return output

            if status == "failed":
                return None

            # Periodic "still running" message so the thread doesn't go silent
            if time.monotonic() - last_update >= self.STATUS_UPDATE_INTERVAL:
                elapsed = int(time.monotonic() - (deadline - timeout))
                await status_callback(
                    RouterResult(
                        status=ResultStatus.RUNNING,
                        session_id=session_id,
                        message=f"Agent **{agent_type}** still running... ({elapsed}s elapsed)",
                    )
                )
                last_update = time.monotonic()

        return None  # timeout
