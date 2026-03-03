"""
skills/devops/review_gate.py

Human-in-the-loop approval gate for Terraform plan review.

The gate enforces that a human reads and explicitly approves (or rejects)
a Terraform plan before apply() is allowed to proceed.  No AI auto-approve.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APPROVED_MARKER = "APPROVED"
REJECTED_MARKER = "REJECTED"
DEFAULT_TIMEOUT = 3600          # 1 hour
DEFAULT_POLL_INTERVAL = 5       # seconds

REVIEW_HEADER_TEMPLATE = """\
# Terraform Plan Review
# Generated: {timestamp}
# Status: PENDING_REVIEW
#
# To approve: Add "APPROVED" on a new line at the end of this file
# To reject:  Add "REJECTED" on a new line at the end of this file
#
# --- Plan Output ---
"""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class ReviewStatus(str, Enum):
    PENDING = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"


@dataclass
class ReviewResult:
    """Structured result from a review gate check."""

    status: ReviewStatus
    review_file: Path
    message: str = ""
    approved: bool = field(init=False)

    def __post_init__(self) -> None:
        self.approved = self.status == ReviewStatus.APPROVED

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "review_file": str(self.review_file),
            "message": self.message,
            "approved": self.approved,
        }


# ---------------------------------------------------------------------------
# ReviewGate
# ---------------------------------------------------------------------------

class ReviewGate:
    """
    Manages human approval of Terraform plans before apply.

    Usage
    -----
    gate = ReviewGate(review_dir=Path("~/.openclaw/workspace/devops/reviews"))

    # After terraform plan:
    result = gate.create_review(plan_output="<terraform plan text>", plan_file=my_path)

    # Blocking wait — returns True if approved, False if rejected or timed out:
    approved = gate.wait_for_approval(result["review_file"])

    # Programmatic approval (tests / CI with explicit opt-in):
    gate.approve(result["review_file"])
    """

    def __init__(self, review_dir: Optional[Path] = None) -> None:
        if review_dir is None:
            review_dir = Path.home() / ".openclaw" / "workspace" / "devops" / "reviews"
        self.review_dir = review_dir
        self.review_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_review(
        self,
        plan_output: str,
        plan_file: Optional[Path] = None,
    ) -> dict:
        """
        Write a plan review file and return metadata about it.

        Parameters
        ----------
        plan_output:
            Raw text output of `terraform plan`.
        plan_file:
            Optional explicit path for the review file.  Defaults to a
            timestamped file inside ``self.review_dir``.

        Returns
        -------
        dict with keys:
            review_file (str), status (str), message (str)
        """
        if plan_file is None:
            timestamp_str = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
            plan_file = self.review_dir / f"terraform-plan-{timestamp_str}.txt"

        timestamp = datetime.utcnow().isoformat(timespec="seconds")
        header = REVIEW_HEADER_TEMPLATE.format(timestamp=timestamp)
        content = header + plan_output

        with self._lock:
            plan_file.write_text(content, encoding="utf-8")

        return {
            "review_file": str(plan_file),
            "status": ReviewStatus.PENDING.value,
            "message": (
                f"Plan saved for review. Open {plan_file} and add "
                f"'{APPROVED_MARKER}' or '{REJECTED_MARKER}' on a new line "
                "at the end of the file."
            ),
        }

    def check_approval(self, review_file: Path) -> ReviewStatus:
        """
        Read the review file and return its current approval status.

        Returns
        -------
        ReviewStatus enum value.
        """
        if not review_file.exists():
            return ReviewStatus.UNKNOWN

        try:
            content = review_file.read_text(encoding="utf-8")
        except OSError:
            return ReviewStatus.UNKNOWN

        # Check the last non-empty, non-comment line for the marker.
        lines = [
            ln.strip()
            for ln in content.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if not lines:
            return ReviewStatus.PENDING

        # Also scan full content for markers (user may add them anywhere).
        content_upper = content.upper()
        # Prefer an explicit line-level marker to avoid false positives from
        # the word appearing inside the plan output itself.
        for line in reversed(lines):
            if line.upper() == APPROVED_MARKER:
                return ReviewStatus.APPROVED
            if line.upper() == REJECTED_MARKER:
                return ReviewStatus.REJECTED

        # Fallback: look for marker as a standalone word on any line.
        for raw_line in reversed(content.splitlines()):
            stripped = raw_line.strip().upper()
            if stripped == APPROVED_MARKER:
                return ReviewStatus.APPROVED
            if stripped == REJECTED_MARKER:
                return ReviewStatus.REJECTED

        return ReviewStatus.PENDING

    def wait_for_approval(
        self,
        review_file: Path,
        timeout: int = DEFAULT_TIMEOUT,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
    ) -> ReviewResult:
        """
        Block until the review file is approved, rejected, or timeout expires.

        Parameters
        ----------
        review_file:
            Path to the review file created by ``create_review()``.
        timeout:
            Maximum seconds to wait (default 3600 = 1 hour).
        poll_interval:
            How often to re-read the file (default 5 seconds).

        Returns
        -------
        ReviewResult — check ``.approved`` to determine whether to proceed.
        """
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            status = self.check_approval(review_file)

            if status == ReviewStatus.APPROVED:
                return ReviewResult(
                    status=ReviewStatus.APPROVED,
                    review_file=review_file,
                    message="Plan approved by human reviewer.",
                )
            if status == ReviewStatus.REJECTED:
                return ReviewResult(
                    status=ReviewStatus.REJECTED,
                    review_file=review_file,
                    message="Plan rejected by human reviewer. Apply aborted.",
                )

            time.sleep(poll_interval)

        return ReviewResult(
            status=ReviewStatus.TIMEOUT,
            review_file=review_file,
            message=(
                f"Review timed out after {timeout}s. "
                "Apply aborted. Re-run plan to start a new review."
            ),
        )

    def approve(self, review_file: Path) -> bool:
        """
        Programmatically approve a review file.

        Intended for CI pipelines that opt-in explicitly or for tests.
        Appends the APPROVED marker to the review file.

        Returns
        -------
        True if the file was found and updated, False otherwise.
        """
        return self._append_marker(review_file, APPROVED_MARKER)

    def reject(self, review_file: Path) -> bool:
        """
        Programmatically reject a review file.

        Returns
        -------
        True if the file was found and updated, False otherwise.
        """
        return self._append_marker(review_file, REJECTED_MARKER)

    def list_pending(self) -> list[dict]:
        """
        Return a list of all review files that are still PENDING_REVIEW.

        Returns
        -------
        List of dicts: {review_file, status, mtime}
        """
        results = []
        for review_file in sorted(self.review_dir.glob("terraform-plan-*.txt")):
            status = self.check_approval(review_file)
            if status == ReviewStatus.PENDING:
                mtime = datetime.utcfromtimestamp(
                    review_file.stat().st_mtime
                ).isoformat(timespec="seconds")
                results.append(
                    {
                        "review_file": str(review_file),
                        "status": status.value,
                        "mtime": mtime,
                    }
                )
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _append_marker(self, review_file: Path, marker: str) -> bool:
        if not review_file.exists():
            return False
        with self._lock:
            try:
                existing = review_file.read_text(encoding="utf-8")
                if not existing.endswith("\n"):
                    existing += "\n"
                review_file.write_text(existing + marker + "\n", encoding="utf-8")
                return True
            except OSError:
                return False
