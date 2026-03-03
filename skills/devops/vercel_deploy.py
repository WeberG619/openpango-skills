"""
skills/devops/vercel_deploy.py

Vercel CLI wrapper for frontend deployments.

Provides thin, structured wrappers around ``vercel deploy``,
``vercel list``, and ``vercel inspect``.  No pip packages — only stdlib.

All functions check for Vercel CLI availability before running and return
structured dicts with results or error information.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT = 120  # seconds (deploys can be slow)


# ---------------------------------------------------------------------------
# CLI availability
# ---------------------------------------------------------------------------

def check_vercel_available() -> bool:
    """Return True if the Vercel CLI is installed and on PATH."""
    return shutil.which("vercel") is not None


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _run_vercel(
    args: list[str],
    cwd: Optional[Path] = None,
    timeout: int = DEFAULT_TIMEOUT,
    parse_json: bool = False,
) -> dict:
    """
    Execute a ``vercel`` CLI command and return a structured result.

    Parameters
    ----------
    args:
        Arguments to pass to ``vercel`` (not including the binary name).
    cwd:
        Working directory for the subprocess.
    timeout:
        Subprocess timeout in seconds.
    parse_json:
        If True, attempt to JSON-parse stdout.

    Returns
    -------
    dict with success, data (parsed or raw stdout), stderr, error, command.
    """
    cmd = ["vercel"] + args

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        raw_stdout = proc.stdout.strip()
        raw_stderr = proc.stderr.strip()

        if proc.returncode != 0:
            return {
                "success": False,
                "data": None,
                "stdout": raw_stdout,
                "stderr": raw_stderr,
                "error": raw_stderr or f"vercel exited with code {proc.returncode}",
                "command": " ".join(cmd),
                "returncode": proc.returncode,
            }

        data = raw_stdout
        if parse_json and raw_stdout:
            try:
                data = json.loads(raw_stdout)
            except json.JSONDecodeError:
                pass  # Keep as raw string.

        return {
            "success": True,
            "data": data,
            "stdout": raw_stdout,
            "stderr": raw_stderr,
            "error": "",
            "command": " ".join(cmd),
            "returncode": 0,
        }

    except FileNotFoundError:
        return {
            "success": False,
            "data": None,
            "stdout": "",
            "stderr": "",
            "error": (
                "'vercel' not found on PATH. "
                "Install it with: npm i -g vercel"
            ),
            "command": " ".join(cmd),
            "returncode": -1,
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "data": None,
            "stdout": "",
            "stderr": "",
            "error": f"vercel command timed out after {timeout}s: {' '.join(cmd)}",
            "command": " ".join(cmd),
            "returncode": -1,
        }

    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "data": None,
            "stdout": "",
            "stderr": "",
            "error": f"Unexpected error: {exc}",
            "command": " ".join(cmd),
            "returncode": -1,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def deploy(
    project_dir: Path,
    prod: bool = False,
    token: Optional[str] = None,
    scope: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    build_env: Optional[dict[str, str]] = None,
    no_wait: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
    **kwargs: str,
) -> dict:
    """
    Deploy a project using ``vercel deploy``.

    Parameters
    ----------
    project_dir:
        Path to the frontend project directory.
    prod:
        If True, pass ``--prod`` to deploy to the production URL.
    token:
        Optional Vercel authentication token (``--token``).
        If not provided, the CLI uses stored credentials.
    scope:
        Optional team scope (``--scope``).
    env:
        Key=value pairs passed as ``--env KEY=VALUE`` (runtime env vars).
    build_env:
        Key=value pairs passed as ``--build-env KEY=VALUE``.
    no_wait:
        If True, pass ``--no-wait`` to return immediately without waiting
        for the deployment to finish building.
    timeout:
        Subprocess timeout.
    **kwargs:
        Additional flags passed as ``--key value`` pairs.

    Returns
    -------
    dict with success, url (str), data, stderr, error, command.
    """
    project_dir = Path(project_dir).expanduser().resolve()

    args = ["deploy", "--yes"]  # --yes skips interactive confirmation
    if prod:
        args.append("--prod")
    if token:
        args += ["--token", token]
    if scope:
        args += ["--scope", scope]
    if no_wait:
        args.append("--no-wait")
    if env:
        for k, v in env.items():
            args += ["--env", f"{k}={v}"]
    if build_env:
        for k, v in build_env.items():
            args += ["--build-env", f"{k}={v}"]
    for k, v in kwargs.items():
        flag = k.replace("_", "-")
        args += [f"--{flag}", v]

    result = _run_vercel(args, cwd=project_dir, timeout=timeout)

    # Extract deployment URL from stdout (last non-empty line is typically the URL).
    url = ""
    if result["success"] and result["stdout"]:
        lines = [ln.strip() for ln in result["stdout"].splitlines() if ln.strip()]
        if lines:
            candidate = lines[-1]
            if candidate.startswith("https://"):
                url = candidate

    result["url"] = url
    return result


def list_deployments(
    project: Optional[str] = None,
    limit: int = 20,
    token: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """
    List recent Vercel deployments using ``vercel list``.

    Parameters
    ----------
    project:
        Optional project name to filter deployments.
    limit:
        Maximum number of deployments to return (``--limit``).
    token:
        Optional Vercel auth token.
    timeout:
        Subprocess timeout.

    Returns
    -------
    dict with success, data (raw list output), stderr, error.
    """
    args = ["list"]
    if project:
        args.append(project)
    args += ["--limit", str(limit)]
    if token:
        args += ["--token", token]

    return _run_vercel(args, timeout=timeout)


def inspect_deployment(
    url: str,
    token: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """
    Inspect a specific Vercel deployment using ``vercel inspect``.

    Parameters
    ----------
    url:
        The deployment URL or deployment ID to inspect.
    token:
        Optional Vercel auth token.
    timeout:
        Subprocess timeout.

    Returns
    -------
    dict with success, data (inspect output), stderr, error.
    """
    args = ["inspect", url]
    if token:
        args += ["--token", token]

    return _run_vercel(args, timeout=timeout)


def remove_deployment(
    url: str,
    safe: bool = True,
    token: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """
    Remove a Vercel deployment using ``vercel remove``.

    Parameters
    ----------
    url:
        The deployment URL or name to remove.
    safe:
        If True, pass ``--safe`` to protect the production deployment.
    token:
        Optional Vercel auth token.
    timeout:
        Subprocess timeout.

    Returns
    -------
    dict with success, data, stderr, error.
    """
    args = ["remove", url, "--yes"]  # --yes skips confirmation prompt
    if safe:
        args.append("--safe")
    if token:
        args += ["--token", token]

    return _run_vercel(args, timeout=timeout)


def get_env(
    project: str,
    environment: str = "production",
    token: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """
    List environment variables for a Vercel project.

    Parameters
    ----------
    project:
        Project name.
    environment:
        "production", "preview", or "development".
    token:
        Optional auth token.
    timeout:
        Subprocess timeout.

    Returns
    -------
    dict with success, data, stderr, error.
    """
    args = ["env", "ls", project, environment]
    if token:
        args += ["--token", token]

    return _run_vercel(args, timeout=timeout)
