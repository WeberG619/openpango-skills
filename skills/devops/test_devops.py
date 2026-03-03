"""
skills/devops/test_devops.py

Comprehensive test suite for the DevOps & Cloud Provisioning skill.

- 30+ tests using unittest
- ALL subprocess calls are mocked — no real terraform/aws/gcloud/vercel runs
- Every test uses tempfile.TemporaryDirectory for isolation
- Tests cover: TerraformProvisioner, ReviewGate, cloud lookups, Vercel, CLI
"""

from __future__ import annotations

import json
import sys
import time
import unittest
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Path setup — allows running directly or via pytest from any cwd
# ---------------------------------------------------------------------------
_SKILLS_ROOT = Path(__file__).parent.parent.parent
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

from skills.devops.review_gate import ReviewGate, ReviewStatus, APPROVED_MARKER, REJECTED_MARKER
from skills.devops.provisioner import TerraformProvisioner, CommandResult
from skills.devops.cloud_lookup import aws_lookup, gcp_lookup, cloud_lookup, check_cli_available
from skills.devops.vercel_deploy import deploy as vercel_deploy, list_deployments, inspect_deployment, check_vercel_available
from skills.devops.cli import build_parser, main as cli_main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    """Return a mock subprocess.CompletedProcess-like object."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


# ===========================================================================
# ReviewGate tests
# ===========================================================================

class TestReviewGateCreate(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.review_dir = Path(self._tmp.name)
        self.gate = ReviewGate(review_dir=self.review_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_create_review_writes_file(self) -> None:
        plan_output = "Plan: 2 to add, 0 to change, 0 to destroy."
        result = self.gate.create_review(plan_output=plan_output)
        review_file = Path(result["review_file"])
        self.assertTrue(review_file.exists(), "Review file was not created")

    def test_create_review_contains_plan_output(self) -> None:
        plan_output = "Plan: 1 to add."
        result = self.gate.create_review(plan_output=plan_output)
        content = Path(result["review_file"]).read_text()
        self.assertIn(plan_output, content)

    def test_create_review_contains_header(self) -> None:
        result = self.gate.create_review(plan_output="output")
        content = Path(result["review_file"]).read_text()
        self.assertIn("Terraform Plan Review", content)
        self.assertIn("PENDING_REVIEW", content)
        self.assertIn(APPROVED_MARKER, content)

    def test_create_review_returns_pending_status(self) -> None:
        result = self.gate.create_review(plan_output="output")
        self.assertEqual(result["status"], ReviewStatus.PENDING.value)

    def test_create_review_with_explicit_plan_file(self) -> None:
        explicit = self.review_dir / "explicit-plan.txt"
        result = self.gate.create_review(plan_output="output", plan_file=explicit)
        self.assertEqual(result["review_file"], str(explicit))
        self.assertTrue(explicit.exists())

    def test_create_review_returns_message_with_path(self) -> None:
        result = self.gate.create_review(plan_output="output")
        # Message should reference the plan file path or give instructions.
        has_path_or_instruction = (
            "Plan saved" in result["message"]
            or "APPROVED" in result["message"]
            or ".txt" in result["message"]
        )
        self.assertTrue(has_path_or_instruction, f"Unexpected message: {result['message']}")


class TestReviewGateCheckApproval(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.review_dir = Path(self._tmp.name)
        self.gate = ReviewGate(review_dir=self.review_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_check_pending_when_no_marker(self) -> None:
        f = self.review_dir / "plan.txt"
        f.write_text("# Terraform Plan Review\nsome output\n")
        self.assertEqual(self.gate.check_approval(f), ReviewStatus.PENDING)

    def test_check_approved_when_marker_present(self) -> None:
        f = self.review_dir / "plan.txt"
        f.write_text("# header\nsome output\nAPPROVED\n")
        self.assertEqual(self.gate.check_approval(f), ReviewStatus.APPROVED)

    def test_check_rejected_when_marker_present(self) -> None:
        f = self.review_dir / "plan.txt"
        f.write_text("# header\nsome output\nREJECTED\n")
        self.assertEqual(self.gate.check_approval(f), ReviewStatus.REJECTED)

    def test_check_unknown_when_file_missing(self) -> None:
        f = self.review_dir / "nonexistent.txt"
        self.assertEqual(self.gate.check_approval(f), ReviewStatus.UNKNOWN)

    def test_check_case_insensitive_approved(self) -> None:
        f = self.review_dir / "plan.txt"
        f.write_text("# header\napproved\n")
        self.assertEqual(self.gate.check_approval(f), ReviewStatus.APPROVED)

    def test_check_case_insensitive_rejected(self) -> None:
        f = self.review_dir / "plan.txt"
        f.write_text("# header\nrejected\n")
        self.assertEqual(self.gate.check_approval(f), ReviewStatus.REJECTED)


class TestReviewGateApproveReject(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.review_dir = Path(self._tmp.name)
        self.gate = ReviewGate(review_dir=self.review_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_approve_appends_marker(self) -> None:
        f = self.review_dir / "plan.txt"
        f.write_text("# header\noutput\n")
        ok = self.gate.approve(f)
        self.assertTrue(ok)
        self.assertEqual(self.gate.check_approval(f), ReviewStatus.APPROVED)

    def test_reject_appends_marker(self) -> None:
        f = self.review_dir / "plan.txt"
        f.write_text("# header\noutput\n")
        ok = self.gate.reject(f)
        self.assertTrue(ok)
        self.assertEqual(self.gate.check_approval(f), ReviewStatus.REJECTED)

    def test_approve_returns_false_for_missing_file(self) -> None:
        f = self.review_dir / "missing.txt"
        self.assertFalse(self.gate.approve(f))

    def test_reject_returns_false_for_missing_file(self) -> None:
        f = self.review_dir / "missing.txt"
        self.assertFalse(self.gate.reject(f))


class TestReviewGateWaitForApproval(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.review_dir = Path(self._tmp.name)
        self.gate = ReviewGate(review_dir=self.review_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_wait_returns_approved_immediately_if_file_already_approved(self) -> None:
        f = self.review_dir / "plan.txt"
        f.write_text("# header\nAPPROVED\n")
        result = self.gate.wait_for_approval(f, timeout=10, poll_interval=1)
        self.assertTrue(result.approved)
        self.assertEqual(result.status, ReviewStatus.APPROVED)

    def test_wait_returns_rejected_immediately_if_file_already_rejected(self) -> None:
        f = self.review_dir / "plan.txt"
        f.write_text("# header\nREJECTED\n")
        result = self.gate.wait_for_approval(f, timeout=10, poll_interval=1)
        self.assertFalse(result.approved)
        self.assertEqual(result.status, ReviewStatus.REJECTED)

    def test_wait_times_out_when_no_decision(self) -> None:
        f = self.review_dir / "plan.txt"
        f.write_text("# header\nno decision here\n")
        result = self.gate.wait_for_approval(f, timeout=1, poll_interval=1)
        self.assertFalse(result.approved)
        self.assertEqual(result.status, ReviewStatus.TIMEOUT)

    def test_wait_detects_approval_written_by_background_thread(self) -> None:
        f = self.review_dir / "plan.txt"
        f.write_text("# header\nplan output\n")

        def approve_after_delay() -> None:
            time.sleep(0.3)
            self.gate.approve(f)

        t = threading.Thread(target=approve_after_delay)
        t.start()
        result = self.gate.wait_for_approval(f, timeout=5, poll_interval=0.1)
        t.join()
        self.assertTrue(result.approved)


class TestReviewGateListPending(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.review_dir = Path(self._tmp.name)
        self.gate = ReviewGate(review_dir=self.review_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_list_pending_returns_only_pending_files(self) -> None:
        pending = self.review_dir / "terraform-plan-20260301T120000.txt"
        approved = self.review_dir / "terraform-plan-20260301T130000.txt"
        pending.write_text("# header\noutput\n")
        approved.write_text("# header\noutput\nAPPROVED\n")

        results = self.gate.list_pending()
        review_files = [r["review_file"] for r in results]
        self.assertIn(str(pending), review_files)
        self.assertNotIn(str(approved), review_files)

    def test_list_pending_empty_when_no_files(self) -> None:
        results = self.gate.list_pending()
        self.assertEqual(results, [])


# ===========================================================================
# TerraformProvisioner tests
# ===========================================================================

class TestTerraformProvisionerInit(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)
        self.review_dir = self.work_dir / "reviews"
        self.review_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @patch("subprocess.run")
    def test_init_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_proc(0, "Terraform has been successfully initialized!")
        tf = TerraformProvisioner(self.work_dir, review_dir=self.review_dir)
        result = tf.init()
        self.assertTrue(result["success"])
        self.assertEqual(result["returncode"], 0)

    @patch("subprocess.run")
    def test_init_passes_no_color_flag(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_proc(0, "ok")
        tf = TerraformProvisioner(self.work_dir, review_dir=self.review_dir)
        tf.init()
        cmd = mock_run.call_args[0][0]
        self.assertIn("-no-color", cmd)

    @patch("subprocess.run")
    def test_init_upgrade_flag(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_proc(0, "ok")
        tf = TerraformProvisioner(self.work_dir, review_dir=self.review_dir)
        tf.init(upgrade=True)
        cmd = mock_run.call_args[0][0]
        self.assertIn("-upgrade", cmd)

    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_init_returns_error_when_terraform_missing(self, mock_run: MagicMock) -> None:
        tf = TerraformProvisioner(self.work_dir, review_dir=self.review_dir)
        result = tf.init()
        self.assertFalse(result["success"])
        self.assertIn("not found", result["error"])

    @patch("subprocess.run")
    def test_init_failure_nonzero_exit(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_proc(1, "", "Error: no configuration files found")
        tf = TerraformProvisioner(self.work_dir, review_dir=self.review_dir)
        result = tf.init()
        self.assertFalse(result["success"])
        self.assertNotEqual(result["error"], "")


class TestTerraformProvisionerPlan(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)
        self.review_dir = self.work_dir / "reviews"
        self.review_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @patch("subprocess.run")
    def test_plan_saves_review_file(self, mock_run: MagicMock) -> None:
        plan_out = "Plan: 2 to add, 0 to change, 0 to destroy."
        mock_run.return_value = _make_proc(0, plan_out)
        tf = TerraformProvisioner(self.work_dir, review_dir=self.review_dir)
        result = tf.plan()
        self.assertIn("review_file", result)
        review_file = Path(result["review_file"])
        self.assertTrue(review_file.exists(), "Review file not created after plan")

    @patch("subprocess.run")
    def test_plan_review_file_contains_plan_output(self, mock_run: MagicMock) -> None:
        plan_out = "Plan: 3 to add."
        mock_run.return_value = _make_proc(0, plan_out)
        tf = TerraformProvisioner(self.work_dir, review_dir=self.review_dir)
        result = tf.plan()
        content = Path(result["review_file"]).read_text()
        self.assertIn(plan_out, content)

    @patch("subprocess.run")
    def test_plan_returns_review_status_pending(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_proc(0, "plan output")
        tf = TerraformProvisioner(self.work_dir, review_dir=self.review_dir)
        result = tf.plan()
        self.assertEqual(result["review_status"], ReviewStatus.PENDING.value)

    @patch("subprocess.run")
    def test_plan_with_var_file(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_proc(0, "ok")
        tf = TerraformProvisioner(self.work_dir, review_dir=self.review_dir)
        tf.plan(var_file="prod.tfvars")
        cmd = mock_run.call_args[0][0]
        self.assertIn("-var-file", cmd)
        self.assertIn("prod.tfvars", cmd)

    @patch("subprocess.run")
    def test_plan_with_vars(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_proc(0, "ok")
        tf = TerraformProvisioner(self.work_dir, review_dir=self.review_dir)
        tf.plan(vars={"env": "prod", "region": "us-east-1"})
        cmd = mock_run.call_args[0][0]
        self.assertIn("-var", cmd)

    @patch("subprocess.run")
    def test_plan_still_saves_review_on_failure(self, mock_run: MagicMock) -> None:
        """Even on non-zero exit, plan should save a review file."""
        mock_run.return_value = _make_proc(1, "", "Error: config error")
        tf = TerraformProvisioner(self.work_dir, review_dir=self.review_dir)
        result = tf.plan()
        self.assertIn("review_file", result)
        self.assertTrue(Path(result["review_file"]).exists())


class TestTerraformProvisionerApply(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)
        self.review_dir = self.work_dir / "reviews"
        self.review_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @patch("subprocess.run")
    def test_apply_blocked_without_review(self, mock_run: MagicMock) -> None:
        """apply() with no prior plan() and no review file must fail."""
        tf = TerraformProvisioner(self.work_dir, review_dir=self.review_dir)
        result = tf.apply()
        self.assertFalse(result["success"])
        self.assertIn("review", result["error"].lower())
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_apply_blocked_when_review_pending(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_proc(0, "plan output")
        tf = TerraformProvisioner(self.work_dir, review_dir=self.review_dir)
        tf.plan()
        # Not approved yet — apply should block and timeout immediately with timeout=0
        result = tf.apply(wait_timeout=0)
        self.assertFalse(result["success"])

    @patch("subprocess.run")
    def test_apply_succeeds_after_approval(self, mock_run: MagicMock) -> None:
        plan_proc = _make_proc(0, "Plan: 1 to add.")
        apply_proc = _make_proc(0, "Apply complete! Resources: 1 added.")
        mock_run.side_effect = [plan_proc, apply_proc]

        tf = TerraformProvisioner(self.work_dir, review_dir=self.review_dir)
        tf.plan()
        tf._gate.approve(tf._last_review_file)

        result = tf.apply()
        self.assertTrue(result["success"])

    @patch("subprocess.run")
    def test_apply_auto_approve_skips_gate(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_proc(0, "Apply complete!")
        tf = TerraformProvisioner(self.work_dir, review_dir=self.review_dir)
        result = tf.apply(auto_approve=True)
        self.assertTrue(result["success"])
        cmd = mock_run.call_args[0][0]
        self.assertIn("-auto-approve", cmd)

    @patch("subprocess.run")
    def test_apply_rejected_review_blocks(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_proc(0, "Plan: 1 to add.")
        tf = TerraformProvisioner(self.work_dir, review_dir=self.review_dir)
        tf.plan()
        tf._gate.reject(tf._last_review_file)

        apply_mock = _make_proc(0, "Apply complete!")
        mock_run.return_value = apply_mock
        result = tf.apply()
        self.assertFalse(result["success"])
        self.assertIn("rejected", result["error"].lower())


class TestTerraformProvisionerDestroy(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)
        self.review_dir = self.work_dir / "reviews"
        self.review_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @patch("subprocess.run")
    def test_destroy_blocked_without_review(self, mock_run: MagicMock) -> None:
        tf = TerraformProvisioner(self.work_dir, review_dir=self.review_dir)
        result = tf.destroy()
        self.assertFalse(result["success"])
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_destroy_auto_approve_runs(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_proc(0, "Destroy complete!")
        tf = TerraformProvisioner(self.work_dir, review_dir=self.review_dir)
        result = tf.destroy(auto_approve=True)
        self.assertTrue(result["success"])
        cmd = mock_run.call_args[0][0]
        self.assertIn("-auto-approve", cmd)
        self.assertIn("destroy", cmd)


class TestTerraformProvisionerValidateOutput(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)
        self.review_dir = self.work_dir / "reviews"
        self.review_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @patch("subprocess.run")
    def test_validate_success_parses_json(self, mock_run: MagicMock) -> None:
        valid_json = json.dumps({"valid": True, "error_count": 0, "warning_count": 0})
        mock_run.return_value = _make_proc(0, valid_json)
        tf = TerraformProvisioner(self.work_dir, review_dir=self.review_dir)
        result = tf.validate()
        self.assertTrue(result["success"])
        self.assertIsNotNone(result.get("parsed"))
        self.assertTrue(result["parsed"]["valid"])

    @patch("subprocess.run")
    def test_output_parses_json(self, mock_run: MagicMock) -> None:
        outputs = {"instance_ip": {"value": "1.2.3.4", "type": "string"}}
        mock_run.return_value = _make_proc(0, json.dumps(outputs))
        tf = TerraformProvisioner(self.work_dir, review_dir=self.review_dir)
        result = tf.output()
        self.assertTrue(result["success"])
        self.assertEqual(result["outputs"]["instance_ip"]["value"], "1.2.3.4")

    @patch("subprocess.run")
    def test_timeout_returns_error(self, mock_run: MagicMock) -> None:
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired(cmd=["terraform", "init"], timeout=1)
        tf = TerraformProvisioner(self.work_dir, review_dir=self.review_dir, timeout=1)
        result = tf.init()
        self.assertFalse(result["success"])
        self.assertIn("timed out", result["error"])


# ===========================================================================
# cloud_lookup tests
# ===========================================================================

class TestAWSLookup(unittest.TestCase):

    @patch("shutil.which", return_value=None)
    def test_aws_lookup_fails_when_cli_missing(self, mock_which: MagicMock) -> None:
        result = aws_lookup(service="s3", resource_type="buckets")
        self.assertFalse(result["success"])
        self.assertIn("aws", result["error"].lower())

    @patch("shutil.which", return_value="/usr/bin/aws")
    @patch("subprocess.run")
    def test_aws_lookup_s3_buckets_success(self, mock_run: MagicMock, mock_which: MagicMock) -> None:
        payload = {"Buckets": [{"Name": "my-bucket"}], "Owner": {"ID": "abc"}}
        mock_run.return_value = _make_proc(0, json.dumps(payload))
        result = aws_lookup(service="s3", resource_type="buckets")
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["Buckets"][0]["Name"], "my-bucket")

    @patch("shutil.which", return_value="/usr/bin/aws")
    @patch("subprocess.run")
    def test_aws_lookup_passes_region(self, mock_run: MagicMock, mock_which: MagicMock) -> None:
        mock_run.return_value = _make_proc(0, "{}")
        aws_lookup(service="ec2", resource_type="instances", region="us-west-2")
        cmd = mock_run.call_args[0][0]
        self.assertIn("us-west-2", cmd)

    @patch("shutil.which", return_value="/usr/bin/aws")
    @patch("subprocess.run")
    def test_aws_lookup_returns_error_on_nonzero_exit(self, mock_run: MagicMock, mock_which: MagicMock) -> None:
        mock_run.return_value = _make_proc(1, "", "An error occurred")
        result = aws_lookup(service="ec2", resource_type="instances")
        self.assertFalse(result["success"])
        self.assertIn("error", result)

    @patch("shutil.which", return_value="/usr/bin/aws")
    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_aws_lookup_handles_file_not_found(self, mock_run: MagicMock, mock_which: MagicMock) -> None:
        result = aws_lookup(service="s3", resource_type="buckets")
        self.assertFalse(result["success"])


class TestGCPLookup(unittest.TestCase):

    @patch("shutil.which", return_value=None)
    def test_gcp_lookup_fails_when_cli_missing(self, mock_which: MagicMock) -> None:
        result = gcp_lookup(service="compute", resource_type="instances")
        self.assertFalse(result["success"])
        self.assertIn("gcloud", result["error"].lower())

    @patch("shutil.which", return_value="/usr/bin/gcloud")
    @patch("subprocess.run")
    def test_gcp_lookup_compute_instances_success(self, mock_run: MagicMock, mock_which: MagicMock) -> None:
        instances = [{"name": "my-vm", "zone": "us-central1-a"}]
        mock_run.return_value = _make_proc(0, json.dumps(instances))
        result = gcp_lookup(service="compute", resource_type="instances", project="my-project")
        self.assertTrue(result["success"])
        self.assertEqual(result["data"][0]["name"], "my-vm")

    @patch("shutil.which", return_value="/usr/bin/gcloud")
    @patch("subprocess.run")
    def test_gcp_lookup_passes_project(self, mock_run: MagicMock, mock_which: MagicMock) -> None:
        mock_run.return_value = _make_proc(0, "[]")
        gcp_lookup(service="compute", resource_type="instances", project="my-proj")
        cmd = mock_run.call_args[0][0]
        self.assertIn("my-proj", cmd)

    @patch("shutil.which", return_value="/usr/bin/gcloud")
    @patch("subprocess.run")
    def test_gcp_lookup_applies_filters(self, mock_run: MagicMock, mock_which: MagicMock) -> None:
        mock_run.return_value = _make_proc(0, "[]")
        gcp_lookup(service="compute", resource_type="instances", status="RUNNING")
        cmd = mock_run.call_args[0][0]
        self.assertIn("--filter", cmd)


class TestCloudLookupUnified(unittest.TestCase):

    def test_unknown_provider_returns_error(self) -> None:
        result = cloud_lookup(provider="azure", service="storage", resource_type="blobs")
        self.assertFalse(result["success"])
        self.assertIn("azure", result["error"].lower())

    def test_check_cli_available_false_when_missing(self) -> None:
        with patch("shutil.which", return_value=None):
            self.assertFalse(check_cli_available("aws"))

    def test_check_cli_available_true_when_present(self) -> None:
        with patch("shutil.which", return_value="/usr/bin/aws"):
            self.assertTrue(check_cli_available("aws"))


# ===========================================================================
# Vercel tests
# ===========================================================================

class TestVercelDeploy(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @patch("shutil.which", return_value=None)
    def test_deploy_fails_when_vercel_missing(self, mock_which: MagicMock) -> None:
        result = vercel_deploy(project_dir=self.project_dir)
        self.assertFalse(result["success"])
        self.assertIn("vercel", result["error"].lower())

    @patch("subprocess.run")
    def test_deploy_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_proc(0, "https://my-app.vercel.app")
        result = vercel_deploy(project_dir=self.project_dir)
        self.assertTrue(result["success"])

    @patch("subprocess.run")
    def test_deploy_prod_flag(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_proc(0, "https://my-app.vercel.app")
        vercel_deploy(project_dir=self.project_dir, prod=True)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--prod", cmd)

    @patch("subprocess.run")
    def test_deploy_extracts_url(self, mock_run: MagicMock) -> None:
        url = "https://my-feature-branch.vercel.app"
        mock_run.return_value = _make_proc(0, f"Deploying...\nReady!\n{url}")
        result = vercel_deploy(project_dir=self.project_dir)
        self.assertEqual(result["url"], url)

    @patch("subprocess.run")
    def test_deploy_nonzero_exit_returns_error(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_proc(1, "", "Error: not logged in")
        result = vercel_deploy(project_dir=self.project_dir)
        self.assertFalse(result["success"])

    @patch("subprocess.run")
    def test_list_deployments_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_proc(0, "my-app  https://my-app.vercel.app  1d ago")
        result = list_deployments(project="my-app")
        self.assertTrue(result["success"])

    @patch("subprocess.run")
    def test_inspect_deployment_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_proc(0, "Deployment info...")
        result = inspect_deployment(url="https://my-app.vercel.app")
        self.assertTrue(result["success"])

    def test_check_vercel_available_false_when_missing(self) -> None:
        with patch("shutil.which", return_value=None):
            self.assertFalse(check_vercel_available())

    def test_check_vercel_available_true_when_present(self) -> None:
        with patch("shutil.which", return_value="/usr/local/bin/vercel"):
            self.assertTrue(check_vercel_available())

    @patch("subprocess.run")
    def test_deploy_timeout_returns_error(self, mock_run: MagicMock) -> None:
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired(cmd=["vercel", "deploy"], timeout=1)
        result = vercel_deploy(project_dir=self.project_dir, timeout=1)
        self.assertFalse(result["success"])
        self.assertIn("timed out", result["error"])


# ===========================================================================
# CLI argument parsing tests
# ===========================================================================

class TestCLIParsing(unittest.TestCase):

    def setUp(self) -> None:
        self.parser = build_parser()

    def test_init_command_parsed(self) -> None:
        args = self.parser.parse_args(["init"])
        self.assertEqual(args.command, "init")

    def test_init_with_dir(self) -> None:
        args = self.parser.parse_args(["init", "--dir", "/tmp/tf"])
        self.assertEqual(args.dir, "/tmp/tf")

    def test_init_upgrade_flag(self) -> None:
        args = self.parser.parse_args(["init", "--upgrade"])
        self.assertTrue(args.upgrade)

    def test_plan_command_parsed(self) -> None:
        args = self.parser.parse_args(["plan"])
        self.assertEqual(args.command, "plan")

    def test_plan_with_var_file(self) -> None:
        args = self.parser.parse_args(["plan", "--var-file", "prod.tfvars"])
        self.assertEqual(args.var_file, "prod.tfvars")

    def test_apply_auto_approve_flag(self) -> None:
        args = self.parser.parse_args(["apply", "--auto-approve"])
        self.assertTrue(args.auto_approve)

    def test_apply_wait_timeout(self) -> None:
        args = self.parser.parse_args(["apply", "--wait-timeout", "60"])
        self.assertEqual(args.wait_timeout, 60)

    def test_deploy_prod_flag(self) -> None:
        args = self.parser.parse_args(["deploy", "--prod"])
        self.assertTrue(args.prod)

    def test_lookup_provider_aws(self) -> None:
        args = self.parser.parse_args(["lookup", "--provider", "aws", "--service", "s3", "--type", "buckets"])
        self.assertEqual(args.provider, "aws")

    def test_lookup_provider_gcp(self) -> None:
        args = self.parser.parse_args(["lookup", "--provider", "gcp", "--service", "compute", "--type", "instances"])
        self.assertEqual(args.provider, "gcp")

    def test_lookup_invalid_provider(self) -> None:
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["lookup", "--provider", "azure", "--service", "s", "--type", "t"])

    def test_status_command_parsed(self) -> None:
        args = self.parser.parse_args(["status"])
        self.assertEqual(args.command, "status")

    def test_approve_command_parsed(self) -> None:
        args = self.parser.parse_args(["approve", "/tmp/plan.txt"])
        self.assertEqual(args.review_file, "/tmp/plan.txt")


class TestCLIExecution(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @patch("subprocess.run")
    def test_cli_init_exits_zero_on_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_proc(0, "Initialized!")
        exit_code = cli_main(["init", "--dir", str(self.work_dir)])
        self.assertEqual(exit_code, 0)

    @patch("subprocess.run")
    def test_cli_init_exits_nonzero_on_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_proc(1, "", "Error")
        exit_code = cli_main(["init", "--dir", str(self.work_dir)])
        self.assertNotEqual(exit_code, 0)

    def test_cli_status_exits_zero(self) -> None:
        exit_code = cli_main(["status"])
        self.assertEqual(exit_code, 0)

    @patch("subprocess.run")
    def test_cli_plan_exits_zero_on_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _make_proc(0, "Plan: 1 to add.")
        exit_code = cli_main(["plan", "--dir", str(self.work_dir)])
        self.assertEqual(exit_code, 0)


# ===========================================================================
# CommandResult dataclass
# ===========================================================================

class TestCommandResult(unittest.TestCase):

    def test_to_dict_structure(self) -> None:
        r = CommandResult(
            success=True,
            returncode=0,
            stdout="ok",
            stderr="",
            command=["terraform", "init"],
        )
        d = r.to_dict()
        self.assertIn("success", d)
        self.assertIn("returncode", d)
        self.assertIn("stdout", d)
        self.assertIn("stderr", d)
        self.assertIn("command", d)

    def test_failed_result_has_error(self) -> None:
        r = CommandResult(
            success=False,
            returncode=1,
            stdout="",
            stderr="error text",
            command=["terraform", "apply"],
            error="error text",
        )
        self.assertFalse(r.success)
        self.assertNotEqual(r.error, "")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
