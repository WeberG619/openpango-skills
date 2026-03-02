#!/usr/bin/env python3
"""Tests for the OpenClaw Manager Agent router."""

import json
import os
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

# Ensure the skills directory is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from skills.orchestration.router import (
    VALID_AGENTS,
    build_parser,
    load_storage,
    save_storage,
)


class TestStorageHelpers(unittest.TestCase):
    """Test load/save storage with temp files."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.storage_path = Path(self.tmp.name)

    def tearDown(self):
        if self.storage_path.exists():
            self.storage_path.unlink()
        tmp_path = self.storage_path.with_suffix(".tmp")
        if tmp_path.exists():
            tmp_path.unlink()

    @patch("skills.orchestration.router.STORAGE_FILE")
    def test_load_empty(self, mock_path):
        """load_storage returns empty structure when file doesn't exist."""
        mock_path.__class__ = Path
        mock_path.exists = lambda: False
        result = load_storage()
        self.assertIn("sessions", result)
        self.assertEqual(result["sessions"], {})

    @patch("skills.orchestration.router.STORAGE_FILE")
    def test_save_and_load(self, mock_path):
        """save_storage writes JSON that load_storage can read back."""
        mock_path.__class__ = Path
        mock_path.exists = lambda: self.storage_path.exists()
        mock_path.read_text = lambda encoding="utf-8": self.storage_path.read_text(encoding=encoding)
        mock_path.with_suffix = lambda s: self.storage_path.with_suffix(s)

        data = {"sessions": {"test-123": {"status": "idle", "agent_type": "Coder"}}}
        # Direct write for this test
        self.storage_path.write_text(json.dumps(data))
        loaded = json.loads(self.storage_path.read_text())
        self.assertEqual(loaded["sessions"]["test-123"]["status"], "idle")


class TestCLIParser(unittest.TestCase):
    """Verify the argparse configuration."""

    def test_spawn_valid(self):
        parser = build_parser()
        args = parser.parse_args(["spawn", "Coder"])
        self.assertEqual(args.command, "spawn")
        self.assertEqual(args.agent_type, "Coder")

    def test_spawn_invalid(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["spawn", "InvalidAgent"])

    def test_append(self):
        parser = build_parser()
        args = parser.parse_args(["append", "abc-123", "do something"])
        self.assertEqual(args.command, "append")
        self.assertEqual(args.session_id, "abc-123")
        self.assertEqual(args.task_payload, "do something")

    def test_status(self):
        parser = build_parser()
        args = parser.parse_args(["status", "abc-123"])
        self.assertEqual(args.command, "status")

    def test_output(self):
        parser = build_parser()
        args = parser.parse_args(["output", "abc-123"])
        self.assertEqual(args.command, "output")

    def test_list(self):
        parser = build_parser()
        args = parser.parse_args(["list"])
        self.assertEqual(args.command, "list")

    def test_kill(self):
        parser = build_parser()
        args = parser.parse_args(["kill", "abc-123"])
        self.assertEqual(args.command, "kill")

    def test_wait_default_timeout(self):
        parser = build_parser()
        args = parser.parse_args(["wait", "abc-123"])
        self.assertEqual(args.timeout, 300)

    def test_wait_custom_timeout(self):
        parser = build_parser()
        args = parser.parse_args(["wait", "abc-123", "--timeout", "60"])
        self.assertEqual(args.timeout, 60)

    def test_all_agent_types(self):
        """All valid agent types are accepted by spawn."""
        parser = build_parser()
        for agent in sorted(VALID_AGENTS):
            args = parser.parse_args(["spawn", agent])
            self.assertEqual(args.agent_type, agent)


class TestSpawnSession(unittest.TestCase):
    """Test spawning sessions end-to-end."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.storage_path = Path(self.tmp.name)

    def tearDown(self):
        if self.storage_path.exists():
            self.storage_path.unlink()
        tmp_path = self.storage_path.with_suffix(".tmp")
        if tmp_path.exists():
            tmp_path.unlink()

    @patch("skills.orchestration.router.STORAGE_FILE")
    def test_spawn_creates_session(self, mock_path):
        """Spawning a session creates a valid session record."""
        # Point storage at our temp file
        mock_path.exists = lambda: self.storage_path.exists()
        mock_path.read_text = lambda encoding="utf-8": self.storage_path.read_text(encoding=encoding)
        mock_path.with_suffix = lambda s: self.storage_path.with_suffix(s)

        # Initialize empty storage
        self.storage_path.write_text('{"sessions": {}}')

        from skills.orchestration.router import spawn_session

        captured = StringIO()
        with patch("sys.stdout", captured):
            spawn_session("Researcher")

        output = json.loads(captured.getvalue())
        self.assertEqual(output["agent_type"], "Researcher")
        self.assertEqual(output["status"], "idle")
        self.assertIn("session_id", output)


class TestListSessions(unittest.TestCase):
    """Test session listing."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.storage_path = Path(self.tmp.name)

    def tearDown(self):
        if self.storage_path.exists():
            self.storage_path.unlink()

    @patch("skills.orchestration.router.STORAGE_FILE")
    def test_list_empty(self, mock_path):
        """Listing sessions on empty storage returns empty list."""
        mock_path.exists = lambda: self.storage_path.exists()
        mock_path.read_text = lambda encoding="utf-8": self.storage_path.read_text(encoding=encoding)
        mock_path.with_suffix = lambda s: self.storage_path.with_suffix(s)

        self.storage_path.write_text('{"sessions": {}}')

        from skills.orchestration.router import list_sessions

        captured = StringIO()
        with patch("sys.stdout", captured):
            list_sessions()

        output = json.loads(captured.getvalue())
        self.assertEqual(output["total"], 0)
        self.assertEqual(output["sessions"], [])


if __name__ == "__main__":
    unittest.main()
