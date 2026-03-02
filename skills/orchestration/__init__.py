"""OpenClaw Manager Agent — orchestration skill for sub-agent session management."""

from .router import (
    spawn_session,
    append_task,
    check_status,
    retrieve_output,
    list_sessions,
    kill_session,
    wait_for_completion,
)

__all__ = [
    "spawn_session",
    "append_task",
    "check_status",
    "retrieve_output",
    "list_sessions",
    "kill_session",
    "wait_for_completion",
]
