"""
skills/devops/cloud_lookup.py

Read-only AWS and GCP resource lookup wrappers.

All calls go through the respective CLIs (``aws`` and ``gcloud``).  No pip
packages are used — only stdlib subprocess.  Every function checks CLI
availability before running and returns a structured dict with either results
or an error payload.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_AWS_REGION = "us-east-1"
DEFAULT_TIMEOUT = 60  # seconds


# ---------------------------------------------------------------------------
# CLI availability
# ---------------------------------------------------------------------------

def check_cli_available(cli_name: str) -> bool:
    """
    Return True if ``cli_name`` is found on PATH, False otherwise.

    Parameters
    ----------
    cli_name:
        Name of the CLI binary, e.g. "aws" or "gcloud".
    """
    return shutil.which(cli_name) is not None


# ---------------------------------------------------------------------------
# Internal subprocess helper
# ---------------------------------------------------------------------------

def _run_cli(
    cmd: list[str],
    timeout: int = DEFAULT_TIMEOUT,
    parse_json: bool = True,
) -> dict:
    """
    Execute a CLI command, capture output, and return a structured dict.

    Parameters
    ----------
    cmd:
        Argument list for subprocess.
    timeout:
        Subprocess timeout in seconds.
    parse_json:
        If True, attempt to JSON-parse stdout.

    Returns
    -------
    dict with success, data (parsed or raw), error.
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        if proc.returncode != 0:
            return {
                "success": False,
                "data": None,
                "error": proc.stderr.strip() or f"Command exited with code {proc.returncode}",
                "command": " ".join(cmd),
                "returncode": proc.returncode,
            }

        raw = proc.stdout.strip()
        data: Any = raw

        if parse_json and raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # Not valid JSON — return raw text.
                data = raw

        return {
            "success": True,
            "data": data,
            "error": "",
            "command": " ".join(cmd),
            "returncode": 0,
        }

    except FileNotFoundError:
        binary = cmd[0] if cmd else "unknown"
        return {
            "success": False,
            "data": None,
            "error": f"'{binary}' not found on PATH. Install the CLI and ensure it is accessible.",
            "command": " ".join(cmd),
            "returncode": -1,
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "data": None,
            "error": f"Command timed out after {timeout}s: {' '.join(cmd)}",
            "command": " ".join(cmd),
            "returncode": -1,
        }

    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "data": None,
            "error": f"Unexpected error: {exc}",
            "command": " ".join(cmd),
            "returncode": -1,
        }


# ---------------------------------------------------------------------------
# AWS lookup
# ---------------------------------------------------------------------------

# Map of (service, resource_type) -> aws CLI command builder.
_AWS_COMMANDS: dict[tuple[str, str], list[str]] = {
    ("s3", "buckets"): ["aws", "s3api", "list-buckets"],
    ("ec2", "instances"): ["aws", "ec2", "describe-instances"],
    ("ec2", "vpcs"): ["aws", "ec2", "describe-vpcs"],
    ("ec2", "subnets"): ["aws", "ec2", "describe-subnets"],
    ("ec2", "security-groups"): ["aws", "ec2", "describe-security-groups"],
    ("ec2", "key-pairs"): ["aws", "ec2", "describe-key-pairs"],
    ("iam", "users"): ["aws", "iam", "list-users"],
    ("iam", "roles"): ["aws", "iam", "list-roles"],
    ("rds", "instances"): ["aws", "rds", "describe-db-instances"],
    ("lambda", "functions"): ["aws", "lambda", "list-functions"],
    ("ecs", "clusters"): ["aws", "ecs", "list-clusters"],
    ("eks", "clusters"): ["aws", "eks", "list-clusters"],
    ("ecr", "repositories"): ["aws", "ecr", "describe-repositories"],
    ("cloudformation", "stacks"): ["aws", "cloudformation", "describe-stacks"],
    ("route53", "hosted-zones"): ["aws", "route53", "list-hosted-zones"],
    ("acm", "certificates"): ["aws", "acm", "list-certificates"],
    ("ssm", "parameters"): ["aws", "ssm", "describe-parameters"],
}


def aws_lookup(
    service: str,
    resource_type: str,
    region: str = DEFAULT_AWS_REGION,
    timeout: int = DEFAULT_TIMEOUT,
    **filters: str,
) -> dict:
    """
    Look up AWS resources using the AWS CLI.

    Parameters
    ----------
    service:
        AWS service name, e.g. "s3", "ec2", "iam", "rds", "lambda".
    resource_type:
        Resource type within the service, e.g. "buckets", "instances",
        "users", "roles".
    region:
        AWS region (default "us-east-1").
    timeout:
        Subprocess timeout in seconds.
    **filters:
        Additional key=value pairs appended as ``--filters`` (ec2-style).
        Example: aws_lookup("ec2", "instances", Name="my-vpc")

    Returns
    -------
    dict with success, data, error, command.
    """
    if not check_cli_available("aws"):
        return {
            "success": False,
            "data": None,
            "error": "'aws' CLI not found on PATH. Install the AWS CLI v2.",
            "command": "",
            "returncode": -1,
        }

    key = (service.lower(), resource_type.lower())
    if key in _AWS_COMMANDS:
        cmd = list(_AWS_COMMANDS[key]) + ["--region", region, "--output", "json"]
    else:
        # Generic fallback: aws <service> <list-resource_type> --region ...
        normalized = resource_type.replace("_", "-").replace(" ", "-")
        cmd = ["aws", service, f"list-{normalized}", "--region", region, "--output", "json"]

    # Append ec2-style filters if any.
    if filters:
        filter_parts = [f"Name={k},Values={v}" for k, v in filters.items()]
        cmd += ["--filters"] + filter_parts

    return _run_cli(cmd, timeout=timeout)


# ---------------------------------------------------------------------------
# GCP lookup
# ---------------------------------------------------------------------------

_GCP_COMMANDS: dict[tuple[str, str], list[str]] = {
    ("compute", "instances"): ["gcloud", "compute", "instances", "list", "--format=json"],
    ("compute", "networks"): ["gcloud", "compute", "networks", "list", "--format=json"],
    ("compute", "firewalls"): ["gcloud", "compute", "firewall-rules", "list", "--format=json"],
    ("compute", "disks"): ["gcloud", "compute", "disks", "list", "--format=json"],
    ("storage", "buckets"): ["gcloud", "storage", "buckets", "list", "--format=json"],
    ("iam", "service-accounts"): ["gcloud", "iam", "service-accounts", "list", "--format=json"],
    ("iam", "roles"): ["gcloud", "iam", "roles", "list", "--format=json"],
    ("container", "clusters"): ["gcloud", "container", "clusters", "list", "--format=json"],
    ("sql", "instances"): ["gcloud", "sql", "instances", "list", "--format=json"],
    ("run", "services"): ["gcloud", "run", "services", "list", "--format=json"],
    ("functions", "functions"): ["gcloud", "functions", "list", "--format=json"],
    ("pubsub", "topics"): ["gcloud", "pubsub", "topics", "list", "--format=json"],
    ("pubsub", "subscriptions"): ["gcloud", "pubsub", "subscriptions", "list", "--format=json"],
    ("dns", "managed-zones"): ["gcloud", "dns", "managed-zones", "list", "--format=json"],
    ("projects", "list"): ["gcloud", "projects", "list", "--format=json"],
}


def gcp_lookup(
    service: str,
    resource_type: str,
    project: Optional[str] = None,
    region: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    **filters: str,
) -> dict:
    """
    Look up GCP resources using gcloud CLI.

    Parameters
    ----------
    service:
        GCP service name, e.g. "compute", "storage", "iam", "container".
    resource_type:
        Resource type, e.g. "instances", "buckets", "clusters".
    project:
        GCP project ID.  If None, uses the gcloud active project.
    region:
        GCP region or zone for region-scoped resources.
    timeout:
        Subprocess timeout in seconds.
    **filters:
        Additional ``--filter`` key=value expressions.

    Returns
    -------
    dict with success, data, error, command.
    """
    if not check_cli_available("gcloud"):
        return {
            "success": False,
            "data": None,
            "error": "'gcloud' CLI not found on PATH. Install the Google Cloud SDK.",
            "command": "",
            "returncode": -1,
        }

    key = (service.lower(), resource_type.lower())
    if key in _GCP_COMMANDS:
        cmd = list(_GCP_COMMANDS[key])
    else:
        # Generic fallback: gcloud <service> <resource_type> list --format=json
        cmd = ["gcloud", service, resource_type, "list", "--format=json"]

    if project:
        cmd += ["--project", project]
    if region:
        cmd += ["--region", region]
    if filters:
        filter_expr = " AND ".join(f"{k}={v}" for k, v in filters.items())
        cmd += ["--filter", filter_expr]

    return _run_cli(cmd, timeout=timeout)


# ---------------------------------------------------------------------------
# Generic multi-cloud helper
# ---------------------------------------------------------------------------

def cloud_lookup(
    provider: str,
    service: str,
    resource_type: str,
    region: Optional[str] = None,
    project: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    **filters: str,
) -> dict:
    """
    Unified entry point for cloud resource lookups.

    Parameters
    ----------
    provider:
        "aws" or "gcp".
    service:
        Cloud service name.
    resource_type:
        Resource type to list.
    region:
        Cloud region.
    project:
        GCP project ID (ignored for AWS).
    timeout:
        Subprocess timeout.
    **filters:
        Provider-specific filter key=value pairs.

    Returns
    -------
    dict with success, data, error.
    """
    provider = provider.lower()

    if provider == "aws":
        return aws_lookup(
            service=service,
            resource_type=resource_type,
            region=region or DEFAULT_AWS_REGION,
            timeout=timeout,
            **filters,
        )
    elif provider == "gcp":
        return gcp_lookup(
            service=service,
            resource_type=resource_type,
            project=project,
            region=region,
            timeout=timeout,
            **filters,
        )
    else:
        return {
            "success": False,
            "data": None,
            "error": f"Unknown provider '{provider}'. Supported: 'aws', 'gcp'.",
            "command": "",
            "returncode": -1,
        }
