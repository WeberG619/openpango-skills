"""
skills/devops/provisioner.py

TerraformProvisioner — full Terraform lifecycle management with a mandatory
human review gate before any `terraform apply`.

All external calls go through ``_run_command()``, which captures stdout/stderr,
enforces timeouts, and returns structured dicts.  No pip packages are used.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .review_gate import ReviewGate, ReviewStatus


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT = 300           # seconds per subprocess call
DEFAULT_REVIEW_DIR = Path.home() / ".openclaw" / "workspace" / "devops" / "reviews"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class CommandResult:
    """Structured result from a subprocess call."""

    success: bool
    returncode: int
    stdout: str
    stderr: str
    command: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "command": " ".join(self.command),
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# TerraformProvisioner
# ---------------------------------------------------------------------------

class TerraformProvisioner:
    """
    Manages the full Terraform lifecycle for a given working directory.

    The ``plan()`` method writes the plan output to a human-readable review
    file and returns the path.  ``apply()`` enforces the review gate — it will
    not proceed unless the review file contains an explicit APPROVED marker.

    Parameters
    ----------
    working_dir:
        Directory containing Terraform configuration (.tf files).
    review_dir:
        Directory where plan review files are stored.  Defaults to
        ``~/.openclaw/workspace/devops/reviews``.
    timeout:
        Subprocess timeout in seconds (default 300).
    terraform_bin:
        Path to the terraform binary (default "terraform", resolved via PATH).
    """

    def __init__(
        self,
        working_dir: Path,
        review_dir: Optional[Path] = None,
        timeout: int = DEFAULT_TIMEOUT,
        terraform_bin: str = "terraform",
    ) -> None:
        self.working_dir = Path(working_dir).expanduser().resolve()
        self.review_dir = Path(review_dir).expanduser().resolve() if review_dir else DEFAULT_REVIEW_DIR
        self.timeout = timeout
        self.terraform_bin = terraform_bin
        self._gate = ReviewGate(review_dir=self.review_dir)
        self._last_review_file: Optional[Path] = None

    # ------------------------------------------------------------------
    # Public lifecycle commands
    # ------------------------------------------------------------------

    def init(self, upgrade: bool = False) -> dict:
        """
        Run ``terraform init``.

        Parameters
        ----------
        upgrade:
            Pass ``-upgrade`` to upgrade provider plugins.

        Returns
        -------
        dict with success, stdout, stderr, error.
        """
        cmd = [self.terraform_bin, "init", "-no-color"]
        if upgrade:
            cmd.append("-upgrade")
        result = self._run_command(cmd)
        return result.to_dict()

    def validate(self) -> dict:
        """
        Run ``terraform validate``.

        Returns
        -------
        dict with success, stdout, stderr, and parsed JSON output when
        available.
        """
        cmd = [self.terraform_bin, "validate", "-json", "-no-color"]
        result = self._run_command(cmd)
        out = result.to_dict()
        # Attempt to parse the JSON output for structured error info.
        if result.stdout:
            try:
                out["parsed"] = json.loads(result.stdout)
            except json.JSONDecodeError:
                out["parsed"] = None
        return out

    def plan(
        self,
        out_file: Optional[str] = None,
        var_file: Optional[str] = None,
        vars: Optional[dict[str, str]] = None,
    ) -> dict:
        """
        Run ``terraform plan``, save output to a review file, and return
        the plan summary plus the review file path.

        The caller MUST wait for human approval before calling ``apply()``.

        Parameters
        ----------
        out_file:
            Optional path to save the binary plan file (``-out`` flag).
        var_file:
            Optional ``.tfvars`` file path (``-var-file`` flag).
        vars:
            Dict of ``-var key=value`` overrides.

        Returns
        -------
        dict with success, stdout, stderr, review_file, review_status.
        """
        cmd = [self.terraform_bin, "plan", "-no-color"]
        if out_file:
            cmd += ["-out", out_file]
        if var_file:
            cmd += ["-var-file", var_file]
        if vars:
            for k, v in vars.items():
                cmd += ["-var", f"{k}={v}"]

        result = self._run_command(cmd)
        out = result.to_dict()

        # Always save the plan to a review file, even on failure, so the
        # human can see what went wrong.
        plan_output = _combine_output(result)
        review_meta = self._gate.create_review(plan_output=plan_output)
        self._last_review_file = Path(review_meta["review_file"])

        out["review_file"] = review_meta["review_file"]
        out["review_status"] = review_meta["status"]
        out["review_message"] = review_meta["message"]
        return out

    def apply(
        self,
        auto_approve: bool = False,
        var_file: Optional[str] = None,
        vars: Optional[dict[str, str]] = None,
        review_file: Optional[Path] = None,
        wait_timeout: int = 3600,
    ) -> dict:
        """
        Apply the Terraform configuration.

        Enforces the human review gate unless ``auto_approve=True``.

        Parameters
        ----------
        auto_approve:
            Skip the review gate entirely (for CI pipelines).  Use with care.
        var_file:
            Optional ``.tfvars`` file.
        vars:
            Dict of ``-var`` overrides.
        review_file:
            Explicit review file to check.  Falls back to the file created by
            the most recent ``plan()`` call.
        wait_timeout:
            How long to wait for human approval (default 3600 s = 1 hour).

        Returns
        -------
        dict with success, stdout, stderr, error.
        """
        if not auto_approve:
            gate_result = self._enforce_review_gate(
                review_file=review_file,
                wait_timeout=wait_timeout,
            )
            if not gate_result["approved"]:
                return {
                    "success": False,
                    "returncode": -1,
                    "stdout": "",
                    "stderr": "",
                    "command": f"{self.terraform_bin} apply",
                    "error": gate_result["message"],
                    "review_status": gate_result["status"],
                }

        cmd = [self.terraform_bin, "apply", "-no-color"]
        if auto_approve:
            cmd.append("-auto-approve")
        if var_file:
            cmd += ["-var-file", var_file]
        if vars:
            for k, v in vars.items():
                cmd += ["-var", f"{k}={v}"]

        result = self._run_command(cmd)
        return result.to_dict()

    def destroy(
        self,
        auto_approve: bool = False,
        var_file: Optional[str] = None,
        vars: Optional[dict[str, str]] = None,
        review_file: Optional[Path] = None,
        wait_timeout: int = 3600,
    ) -> dict:
        """
        Destroy all Terraform-managed infrastructure.

        DANGEROUS — requires explicit review gate approval unless
        ``auto_approve=True``.

        Parameters
        ----------
        auto_approve:
            Skip the review gate.  Strongly discouraged for destroy.
        var_file, vars:
            Variable overrides.
        review_file:
            Override which review file to check.
        wait_timeout:
            Approval wait timeout.

        Returns
        -------
        dict with success, stdout, stderr, error.
        """
        if not auto_approve:
            gate_result = self._enforce_review_gate(
                review_file=review_file,
                wait_timeout=wait_timeout,
                operation="destroy",
            )
            if not gate_result["approved"]:
                return {
                    "success": False,
                    "returncode": -1,
                    "stdout": "",
                    "stderr": "",
                    "command": f"{self.terraform_bin} destroy",
                    "error": gate_result["message"],
                    "review_status": gate_result["status"],
                }

        cmd = [self.terraform_bin, "destroy", "-no-color"]
        if auto_approve:
            cmd.append("-auto-approve")
        if var_file:
            cmd += ["-var-file", var_file]
        if vars:
            for k, v in vars.items():
                cmd += ["-var", f"{k}={v}"]

        result = self._run_command(cmd)
        return result.to_dict()

    def output(self, name: Optional[str] = None) -> dict:
        """
        Run ``terraform output -json`` and return parsed outputs.

        Parameters
        ----------
        name:
            Optional specific output name.

        Returns
        -------
        dict with success, outputs (parsed dict), raw stdout, stderr.
        """
        cmd = [self.terraform_bin, "output", "-json", "-no-color"]
        if name:
            cmd.append(name)
        result = self._run_command(cmd)
        out = result.to_dict()
        if result.success and result.stdout:
            try:
                out["outputs"] = json.loads(result.stdout)
            except json.JSONDecodeError:
                out["outputs"] = None
                out["parse_error"] = "Failed to parse JSON output"
        else:
            out["outputs"] = None
        return out

    def show(self, plan_file: Optional[str] = None) -> dict:
        """
        Run ``terraform show`` on the current state or a plan file.

        Parameters
        ----------
        plan_file:
            Path to a binary plan file produced by ``terraform plan -out``.

        Returns
        -------
        dict with success, stdout, stderr.
        """
        cmd = [self.terraform_bin, "show", "-no-color"]
        if plan_file:
            cmd.append(plan_file)
        result = self._run_command(cmd)
        return result.to_dict()

    def workspace_list(self) -> dict:
        """
        List all Terraform workspaces.

        Returns
        -------
        dict with success, workspaces (list[str]), current (str).
        """
        cmd = [self.terraform_bin, "workspace", "list", "-no-color"]
        result = self._run_command(cmd)
        out = result.to_dict()
        if result.success:
            workspaces = []
            current = None
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("*"):
                    name = line.lstrip("* ").strip()
                    workspaces.append(name)
                    current = name
                else:
                    workspaces.append(line)
            out["workspaces"] = workspaces
            out["current"] = current
        return out

    def pending_reviews(self) -> list[dict]:
        """
        Return all review files that are still PENDING_REVIEW.

        Returns
        -------
        list of dicts: {review_file, status, mtime}
        """
        return self._gate.list_pending()

    # ------------------------------------------------------------------
    # Review gate helpers
    # ------------------------------------------------------------------

    def _enforce_review_gate(
        self,
        review_file: Optional[Path],
        wait_timeout: int,
        operation: str = "apply",
    ) -> dict:
        """
        Resolve which review file to use, then block until approved or timeout.

        Returns a dict with keys: approved (bool), status (str), message (str).
        """
        target = review_file or self._last_review_file

        if target is None:
            return {
                "approved": False,
                "status": ReviewStatus.UNKNOWN.value,
                "message": (
                    f"No review file found. Run plan() first to generate a "
                    f"review file, then approve it before calling {operation}()."
                ),
            }

        target = Path(target)

        # Check immediately first (avoids sleeping if already approved).
        immediate = self._gate.check_approval(target)
        if immediate == ReviewStatus.APPROVED:
            return {
                "approved": True,
                "status": ReviewStatus.APPROVED.value,
                "message": "Plan approved.",
            }
        if immediate == ReviewStatus.REJECTED:
            return {
                "approved": False,
                "status": ReviewStatus.REJECTED.value,
                "message": "Plan rejected by human reviewer.",
            }

        # Not yet decided — wait.
        result = self._gate.wait_for_approval(
            review_file=target,
            timeout=wait_timeout,
        )
        return result.to_dict()

    # ------------------------------------------------------------------
    # Subprocess wrapper
    # ------------------------------------------------------------------

    def _run_command(
        self,
        cmd: list[str],
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        input_text: Optional[str] = None,
    ) -> CommandResult:
        """
        Execute a shell command inside ``self.working_dir``.

        Parameters
        ----------
        cmd:
            Argument list for subprocess.
        timeout:
            Override the instance-level timeout for this call.
        env:
            Optional environment variable overrides.
        input_text:
            Optional stdin data.

        Returns
        -------
        CommandResult with all captured output.
        """
        effective_timeout = timeout if timeout is not None else self.timeout

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.working_dir),
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                env=env,
                input=input_text,
            )
            return CommandResult(
                success=proc.returncode == 0,
                returncode=proc.returncode,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
                command=cmd,
                error=proc.stderr.strip() if proc.returncode != 0 else "",
            )

        except FileNotFoundError:
            binary = cmd[0] if cmd else "unknown"
            return CommandResult(
                success=False,
                returncode=-1,
                stdout="",
                stderr="",
                command=cmd,
                error=(
                    f"'{binary}' not found on PATH. "
                    "Install Terraform and ensure it is accessible."
                ),
            )

        except subprocess.TimeoutExpired:
            return CommandResult(
                success=False,
                returncode=-1,
                stdout="",
                stderr="",
                command=cmd,
                error=f"Command timed out after {effective_timeout}s: {' '.join(cmd)}",
            )

        except Exception as exc:  # noqa: BLE001
            return CommandResult(
                success=False,
                returncode=-1,
                stdout="",
                stderr="",
                command=cmd,
                error=f"Unexpected error running command: {exc}",
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _combine_output(result: CommandResult) -> str:
    """Merge stdout and stderr into a single review-friendly string."""
    parts = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append("--- stderr ---\n" + result.stderr)
    return "\n".join(parts) if parts else "(no output)"
