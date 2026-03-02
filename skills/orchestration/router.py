#!/usr/bin/env python3
"""
router.py — OpenClaw Manager Agent core router.

Manages the full lifecycle of sub-agent sessions: spawn, task dispatch,
status polling, output retrieval, session enumeration, and termination.
Execution is driven by the Gemini CLI and persisted to a local JSON store.

CLI usage:
    python router.py spawn <agent_type>
    python router.py append <session_id> <task_payload>
    python router.py status <session_id>
    python router.py output <session_id>
    python router.py list
    python router.py kill <session_id>
    python router.py wait <session_id> [--timeout 300]
"""

import argparse
import json
import os
import signal
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent.parent
SKILLS_DIR = BASE_DIR / "skills"
STORAGE_FILE = Path(__file__).resolve().parent / "openpango_storage.json"
OUTPUTS_DIR = Path(__file__).resolve().parent / "outputs"
PID_DIR = Path(__file__).resolve().parent / "pids"

VALID_AGENTS = {"Researcher", "Planner", "Coder", "Designer"}

# Default execution timeout for a single gemini CLI call (seconds)
DEFAULT_EXEC_TIMEOUT = 600

# How often the status poller sleeps between checks (seconds)
POLL_INTERVAL = 2.0

# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

_storage_lock = threading.Lock()


def load_storage() -> dict:
    """Load session storage from disk. Returns empty store on any error."""
    with _storage_lock:
        if STORAGE_FILE.exists():
            try:
                return json.loads(STORAGE_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"sessions": {}}


def save_storage(data: dict) -> None:
    """Atomically write session storage to disk."""
    with _storage_lock:
        tmp = STORAGE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=4), encoding="utf-8")
        tmp.replace(STORAGE_FILE)


def get_session(session_id: str) -> dict:
    """Return a session record or exit with an error message."""
    data = load_storage()
    if session_id not in data["sessions"]:
        _die(f"Session '{session_id}' not found.")
    return data["sessions"][session_id]


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _out(payload: dict) -> None:
    """Write a JSON payload to stdout."""
    print(json.dumps(payload))


def _die(message: str, code: int = 1) -> None:
    """Print an error to stderr and exit."""
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# spawn
# ---------------------------------------------------------------------------

def spawn_session(agent_type: str) -> None:
    """
    Initialise a new idle session for the given agent type.

    Prints JSON: {"session_id": "...", "agent_type": "...", "status": "idle"}
    """
    if agent_type not in VALID_AGENTS:
        _die(
            f"Invalid agent type '{agent_type}'. "
            f"Valid types: {', '.join(sorted(VALID_AGENTS))}."
        )

    session_id = str(uuid.uuid4())
    data = load_storage()
    data["sessions"][session_id] = {
        "agent_type": agent_type,
        "status": "idle",
        "task": None,
        "output_file": None,
        "pid": None,
        "error": None,
        "created_at": time.time(),
        "started_at": None,
        "completed_at": None,
    }
    save_storage(data)
    _out({"session_id": session_id, "agent_type": agent_type, "status": "idle"})


# ---------------------------------------------------------------------------
# Internal execution
# ---------------------------------------------------------------------------

def _build_prompt(agent_type: str, task_payload: str) -> str:
    """Compose the full prompt sent to the Gemini CLI."""
    agent_dir = SKILLS_DIR / agent_type.lower()
    identity_file = agent_dir / "workspace" / "IDENTITY.md"
    soul_file = agent_dir / "workspace" / "SOUL.md"

    identity = (
        identity_file.read_text(encoding="utf-8")
        if identity_file.exists()
        else f"You are the {agent_type} agent."
    )
    soul = (
        soul_file.read_text(encoding="utf-8")
        if soul_file.exists()
        else "Execute your assigned role with precision and conciseness."
    )

    return (
        f"{identity}\n\n"
        f"{soul}\n\n"
        f"=== TASK ===\n"
        f"{task_payload}\n"
        f"===========\n\n"
        "Execute this task strictly as your assigned role. "
        "You are running in a headless environment. "
        "Output your final response clearly."
    )


def _execute_agent_task(session_id: str, agent_type: str, task_payload: str) -> None:
    """
    Run the Gemini CLI for the given session.

    This is called from a daemon thread so that `append` returns immediately.
    State transitions: running → completed | error.
    """
    import subprocess  # local import keeps top-level clean for stdlib check

    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    PID_DIR.mkdir(parents=True, exist_ok=True)

    output_path = OUTPUTS_DIR / f"{agent_type}-{session_id[:8]}.txt"
    pid_path = PID_DIR / f"{session_id}.pid"

    prompt = _build_prompt(agent_type, task_payload)

    cmd = ["gemini", "--prompt", prompt]
    if agent_type == "Coder":
        # Coder writes files — enable auto-approval (yolo) mode
        cmd += ["--approval-mode", "yolo"]

    final_status = "completed"
    error_msg: Optional[str] = None

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Persist PID so `kill` can terminate the process
        pid_path.write_text(str(proc.pid))

        # Update session with PID
        data = load_storage()
        if session_id in data["sessions"]:
            data["sessions"][session_id]["pid"] = proc.pid
            save_storage(data)

        try:
            stdout, stderr = proc.communicate(timeout=DEFAULT_EXEC_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            final_status = "error"
            error_msg = f"Execution timed out after {DEFAULT_EXEC_TIMEOUT}s."

        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(stdout)
            if stderr:
                fh.write("\n\n--- STDERR ---\n")
                fh.write(stderr)

        if proc.returncode != 0 and final_status != "error":
            final_status = "error"
            error_msg = f"Gemini CLI exited with code {proc.returncode}."

    except FileNotFoundError:
        final_status = "error"
        error_msg = (
            "Gemini CLI not found. "
            "Ensure 'gemini' is installed and on your PATH."
        )
        output_path.write_text(error_msg, encoding="utf-8")

    except Exception as exc:  # pylint: disable=broad-except
        final_status = "error"
        error_msg = str(exc)
        output_path.write_text(
            f"Unexpected execution error: {exc}", encoding="utf-8"
        )

    finally:
        if pid_path.exists():
            pid_path.unlink(missing_ok=True)

    # Persist final state
    data = load_storage()
    if session_id in data["sessions"]:
        sess = data["sessions"][session_id]
        sess["status"] = final_status
        sess["completed_at"] = time.time()
        sess["output_file"] = str(output_path)
        sess["pid"] = None
        if error_msg:
            sess["error"] = error_msg
        save_storage(data)


# ---------------------------------------------------------------------------
# append
# ---------------------------------------------------------------------------

def append_task(session_id: str, task_payload: str) -> None:
    """
    Queue a task on an idle session and start execution in the background.

    Prints JSON: {"session_id": "...", "status": "running", "message": "..."}
    """
    data = load_storage()
    if session_id not in data["sessions"]:
        _die(f"Session '{session_id}' not found.")

    session = data["sessions"][session_id]

    if session["status"] == "running":
        _die(f"Session '{session_id}' is already running. Wait for completion or kill it first.")

    if session["status"] == "completed":
        _die(
            f"Session '{session_id}' already completed. "
            "Spawn a new session to run another task."
        )

    session["task"] = task_payload
    session["status"] = "running"
    session["started_at"] = time.time()
    session["error"] = None
    save_storage(data)

    agent_type = session["agent_type"]

    thread = threading.Thread(
        target=_execute_agent_task,
        args=(session_id, agent_type, task_payload),
        daemon=True,
        name=f"agent-{session_id[:8]}",
    )
    thread.start()

    _out({
        "session_id": session_id,
        "agent_type": agent_type,
        "status": "running",
        "message": "Task accepted. Execution started in background.",
    })


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def check_status(session_id: str) -> None:
    """
    Print the current status of a session.

    Prints JSON: {"session_id": "...", "agent_type": "...", "status": "...",
                  "task": "...", "created_at": ..., "started_at": ..., "completed_at": ...}
    """
    data = load_storage()
    if session_id not in data["sessions"]:
        _die(f"Session '{session_id}' not found.")

    sess = data["sessions"][session_id]
    _out({
        "session_id": session_id,
        "agent_type": sess["agent_type"],
        "status": sess["status"],
        "task": sess.get("task"),
        "error": sess.get("error"),
        "created_at": sess.get("created_at"),
        "started_at": sess.get("started_at"),
        "completed_at": sess.get("completed_at"),
    })


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------

def retrieve_output(session_id: str) -> None:
    """
    Retrieve the output of a completed session.

    Prints JSON: {"session_id": "...", "status": "completed",
                  "output_file": "...", "content": "..."}
    """
    data = load_storage()
    if session_id not in data["sessions"]:
        _die(f"Session '{session_id}' not found.")

    sess = data["sessions"][session_id]

    if sess["status"] not in ("completed", "error"):
        _die(
            f"Session '{session_id}' has not finished yet. "
            f"Current status: {sess['status']}."
        )

    output_file = sess.get("output_file")
    if not output_file:
        _die(f"Session '{session_id}' has no output file recorded.")

    output_path = Path(output_file)
    if not output_path.exists():
        _die(f"Output file '{output_path}' is missing from disk.")

    _out({
        "session_id": session_id,
        "agent_type": sess["agent_type"],
        "status": sess["status"],
        "error": sess.get("error"),
        "output_file": str(output_path),
        "content": output_path.read_text(encoding="utf-8"),
    })


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def list_sessions() -> None:
    """
    List all known sessions with their current state.

    Prints JSON: {"sessions": [...]}
    """
    data = load_storage()
    sessions = []
    for sid, sess in data["sessions"].items():
        sessions.append({
            "session_id": sid,
            "agent_type": sess["agent_type"],
            "status": sess["status"],
            "task_preview": (sess.get("task") or "")[:80] or None,
            "error": sess.get("error"),
            "created_at": sess.get("created_at"),
            "started_at": sess.get("started_at"),
            "completed_at": sess.get("completed_at"),
        })

    # Most-recently-created first
    sessions.sort(key=lambda s: s.get("created_at") or 0, reverse=True)
    _out({"sessions": sessions, "total": len(sessions)})


# ---------------------------------------------------------------------------
# kill
# ---------------------------------------------------------------------------

def kill_session(session_id: str) -> None:
    """
    Terminate a running session and mark it as 'killed'.

    If the underlying process is still running, sends SIGTERM (SIGKILL on
    platforms that don't support it). Idle or already-finished sessions are
    also marked 'killed' to prevent future task dispatch.

    Prints JSON: {"session_id": "...", "status": "killed", "message": "..."}
    """
    data = load_storage()
    if session_id not in data["sessions"]:
        _die(f"Session '{session_id}' not found.")

    sess = data["sessions"][session_id]
    prev_status = sess["status"]
    message_parts = []

    # Attempt to kill the OS process if we have a PID on record
    pid = sess.get("pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            message_parts.append(f"Sent SIGTERM to PID {pid}.")
        except ProcessLookupError:
            message_parts.append(f"PID {pid} no longer exists.")
        except PermissionError:
            message_parts.append(f"No permission to signal PID {pid}.")
        except AttributeError:
            # Windows does not have SIGTERM; use SIGKILL equivalent
            try:
                os.kill(pid, signal.SIGKILL)
                message_parts.append(f"Sent SIGKILL to PID {pid}.")
            except Exception:  # pylint: disable=broad-except
                message_parts.append(f"Could not signal PID {pid}.")

    # Also clean up the PID file if present
    pid_path = PID_DIR / f"{session_id}.pid"
    if pid_path.exists():
        pid_path.unlink(missing_ok=True)

    sess["status"] = "killed"
    sess["pid"] = None
    sess["completed_at"] = time.time()
    if not sess.get("error"):
        sess["error"] = f"Session killed (was: {prev_status})."
    save_storage(data)

    message_parts.append(f"Session status updated from '{prev_status}' to 'killed'.")
    _out({
        "session_id": session_id,
        "status": "killed",
        "message": " ".join(message_parts),
    })


# ---------------------------------------------------------------------------
# wait
# ---------------------------------------------------------------------------

def wait_for_completion(session_id: str, timeout: int) -> None:
    """
    Poll until the session reaches a terminal state then emit its output.

    Terminal states: completed, error, killed.
    Exits with code 1 on timeout.
    """
    deadline = time.monotonic() + timeout
    print(f"[router] Waiting for session {session_id} (timeout={timeout}s)…", file=sys.stderr)

    while time.monotonic() < deadline:
        data = load_storage()
        if session_id not in data["sessions"]:
            _die(f"Session '{session_id}' not found.")

        status = data["sessions"][session_id]["status"]
        if status in ("completed", "error", "killed"):
            print(f"[router] Session reached terminal state: {status}", file=sys.stderr)
            retrieve_output(session_id)
            return

        remaining = int(deadline - time.monotonic())
        print(
            f"[router] status={status}, {remaining}s remaining…",
            file=sys.stderr,
        )
        time.sleep(POLL_INTERVAL)

    _die(f"TIMEOUT: Session '{session_id}' did not complete within {timeout}s.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="router.py",
        description="OpenClaw Manager Agent — sub-agent session router.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python router.py spawn Researcher\n"
            "  python router.py append <session_id> 'Research quantum computing trends'\n"
            "  python router.py status <session_id>\n"
            "  python router.py output <session_id>\n"
            "  python router.py list\n"
            "  python router.py kill <session_id>\n"
            "  python router.py wait <session_id> --timeout 120\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # spawn
    p_spawn = sub.add_parser("spawn", help="Initialise a new agent session.")
    p_spawn.add_argument(
        "agent_type",
        choices=sorted(VALID_AGENTS),
        help="Type of sub-agent to spawn.",
    )

    # append
    p_append = sub.add_parser("append", help="Dispatch a task to an idle session.")
    p_append.add_argument("session_id", help="Target session UUID.")
    p_append.add_argument("task_payload", help="Task instructions sent to the agent.")

    # status
    p_status = sub.add_parser("status", help="Query the status of a session.")
    p_status.add_argument("session_id", help="Target session UUID.")

    # output
    p_output = sub.add_parser("output", help="Retrieve output of a completed session.")
    p_output.add_argument("session_id", help="Target session UUID.")

    # list
    sub.add_parser("list", help="List all sessions.")

    # kill
    p_kill = sub.add_parser("kill", help="Terminate a running session.")
    p_kill.add_argument("session_id", help="Target session UUID.")

    # wait
    p_wait = sub.add_parser(
        "wait",
        help="Block until a session completes, then emit its output.",
    )
    p_wait.add_argument("session_id", help="Target session UUID.")
    p_wait.add_argument(
        "--timeout",
        type=int,
        default=300,
        metavar="SECONDS",
        help="Maximum wait time in seconds (default: 300).",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "spawn": lambda: spawn_session(args.agent_type),
        "append": lambda: append_task(args.session_id, args.task_payload),
        "status": lambda: check_status(args.session_id),
        "output": lambda: retrieve_output(args.session_id),
        "list": list_sessions,
        "kill": lambda: kill_session(args.session_id),
        "wait": lambda: wait_for_completion(args.session_id, args.timeout),
    }

    dispatch[args.command]()


if __name__ == "__main__":
    main()
