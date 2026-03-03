"""
skills/devops/cli.py

CLI entry point for the DevOps & Cloud Provisioning skill.

Usage
-----
    python -m skills.devops.cli <command> [options]

Commands
--------
    init     terraform init
    plan     terraform plan (saves review file)
    apply    terraform apply (waits for review approval)
    destroy  terraform destroy (waits for review approval)
    validate terraform validate
    output   terraform output -json
    deploy   vercel deploy
    lookup   aws/gcp resource lookup
    status   list pending review files

All commands print JSON to stdout for easy piping.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print_json(data: object) -> None:
    """Print data as pretty-formatted JSON to stdout."""
    print(json.dumps(data, indent=2, default=str))


def _resolve_dir(path_str: str | None) -> Path:
    """Resolve working directory, defaulting to cwd."""
    return Path(path_str).expanduser().resolve() if path_str else Path.cwd()


def _exit_code(result: dict) -> int:
    """Map a result dict's success flag to a process exit code."""
    return 0 if result.get("success") else 1


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    from skills.devops.provisioner import TerraformProvisioner

    tf = TerraformProvisioner(
        working_dir=_resolve_dir(args.dir),
        review_dir=_resolve_dir(args.review_dir) if args.review_dir else None,
    )
    result = tf.init(upgrade=args.upgrade)
    _print_json(result)
    return _exit_code(result)


def cmd_validate(args: argparse.Namespace) -> int:
    from skills.devops.provisioner import TerraformProvisioner

    tf = TerraformProvisioner(working_dir=_resolve_dir(args.dir))
    result = tf.validate()
    _print_json(result)
    return _exit_code(result)


def cmd_plan(args: argparse.Namespace) -> int:
    from skills.devops.provisioner import TerraformProvisioner

    tf = TerraformProvisioner(
        working_dir=_resolve_dir(args.dir),
        review_dir=_resolve_dir(args.review_dir) if args.review_dir else None,
    )

    vars_dict: dict[str, str] | None = None
    if args.var:
        vars_dict = {}
        for item in args.var:
            if "=" not in item:
                print(
                    json.dumps({"success": False, "error": f"Invalid --var format: '{item}'. Use KEY=VALUE."}),
                    file=sys.stderr,
                )
                return 1
            k, _, v = item.partition("=")
            vars_dict[k] = v

    result = tf.plan(
        out_file=args.out,
        var_file=args.var_file,
        vars=vars_dict,
    )
    _print_json(result)
    return _exit_code(result)


def cmd_apply(args: argparse.Namespace) -> int:
    from skills.devops.provisioner import TerraformProvisioner

    tf = TerraformProvisioner(
        working_dir=_resolve_dir(args.dir),
        review_dir=_resolve_dir(args.review_dir) if args.review_dir else None,
    )

    review_file = Path(args.review_file).expanduser().resolve() if args.review_file else None

    vars_dict: dict[str, str] | None = None
    if args.var:
        vars_dict = {}
        for item in args.var:
            if "=" not in item:
                print(
                    json.dumps({"success": False, "error": f"Invalid --var format: '{item}'. Use KEY=VALUE."}),
                    file=sys.stderr,
                )
                return 1
            k, _, v = item.partition("=")
            vars_dict[k] = v

    if args.auto_approve:
        print(
            json.dumps({
                "warning": "auto-approve enabled — skipping human review gate",
            }),
            file=sys.stderr,
        )

    result = tf.apply(
        auto_approve=args.auto_approve,
        var_file=args.var_file,
        vars=vars_dict,
        review_file=review_file,
        wait_timeout=args.wait_timeout,
    )
    _print_json(result)
    return _exit_code(result)


def cmd_destroy(args: argparse.Namespace) -> int:
    from skills.devops.provisioner import TerraformProvisioner

    tf = TerraformProvisioner(
        working_dir=_resolve_dir(args.dir),
        review_dir=_resolve_dir(args.review_dir) if args.review_dir else None,
    )

    review_file = Path(args.review_file).expanduser().resolve() if args.review_file else None

    result = tf.destroy(
        auto_approve=args.auto_approve,
        review_file=review_file,
        wait_timeout=args.wait_timeout,
    )
    _print_json(result)
    return _exit_code(result)


def cmd_output(args: argparse.Namespace) -> int:
    from skills.devops.provisioner import TerraformProvisioner

    tf = TerraformProvisioner(working_dir=_resolve_dir(args.dir))
    result = tf.output(name=args.name)
    _print_json(result)
    return _exit_code(result)


def cmd_deploy(args: argparse.Namespace) -> int:
    from skills.devops.vercel_deploy import deploy

    env_dict: dict[str, str] | None = None
    if args.env:
        env_dict = {}
        for item in args.env:
            if "=" not in item:
                print(
                    json.dumps({"success": False, "error": f"Invalid --env format: '{item}'. Use KEY=VALUE."}),
                    file=sys.stderr,
                )
                return 1
            k, _, v = item.partition("=")
            env_dict[k] = v

    result = deploy(
        project_dir=_resolve_dir(args.dir),
        prod=args.prod,
        token=args.token,
        scope=args.scope,
        env=env_dict,
        no_wait=args.no_wait,
    )
    _print_json(result)
    return _exit_code(result)


def cmd_lookup(args: argparse.Namespace) -> int:
    from skills.devops.cloud_lookup import cloud_lookup

    extra_filters: dict[str, str] = {}
    if args.filter:
        for item in args.filter:
            if "=" not in item:
                print(
                    json.dumps({"success": False, "error": f"Invalid --filter format: '{item}'. Use KEY=VALUE."}),
                    file=sys.stderr,
                )
                return 1
            k, _, v = item.partition("=")
            extra_filters[k] = v

    result = cloud_lookup(
        provider=args.provider,
        service=args.service,
        resource_type=args.type,
        region=args.region,
        project=args.project,
        **extra_filters,
    )
    _print_json(result)
    return _exit_code(result)


def cmd_status(args: argparse.Namespace) -> int:
    from skills.devops.review_gate import ReviewGate

    review_dir: Path | None = (
        Path(args.review_dir).expanduser().resolve() if args.review_dir else None
    )
    gate = ReviewGate(review_dir=review_dir)
    pending = gate.list_pending()

    _print_json({
        "success": True,
        "pending_count": len(pending),
        "pending_reviews": pending,
    })
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    """Programmatically approve a review file (for CI or testing)."""
    from skills.devops.review_gate import ReviewGate

    review_file = Path(args.review_file).expanduser().resolve()
    gate = ReviewGate()
    ok = gate.approve(review_file)
    result = {
        "success": ok,
        "review_file": str(review_file),
        "message": "Approved." if ok else "File not found or could not be written.",
    }
    _print_json(result)
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openpango devops",
        description="DevOps & Cloud Provisioning skill — Terraform, Vercel, AWS, GCP",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # ---- init ----
    p_init = sub.add_parser("init", help="terraform init")
    p_init.add_argument("--dir", metavar="PATH", help="Terraform working directory (default: cwd)")
    p_init.add_argument("--review-dir", metavar="PATH", help="Override review file directory")
    p_init.add_argument("--upgrade", action="store_true", help="Pass -upgrade to terraform init")
    p_init.set_defaults(func=cmd_init)

    # ---- validate ----
    p_val = sub.add_parser("validate", help="terraform validate")
    p_val.add_argument("--dir", metavar="PATH", help="Terraform working directory")
    p_val.set_defaults(func=cmd_validate)

    # ---- plan ----
    p_plan = sub.add_parser("plan", help="terraform plan (saves review file)")
    p_plan.add_argument("--dir", metavar="PATH", help="Terraform working directory")
    p_plan.add_argument("--review-dir", metavar="PATH", help="Override review file directory")
    p_plan.add_argument("--out", metavar="FILE", help="Save binary plan to FILE (-out flag)")
    p_plan.add_argument("--var-file", metavar="FILE", help="Variable file (.tfvars)")
    p_plan.add_argument("--var", metavar="KEY=VALUE", action="append", help="Variable override (repeatable)")
    p_plan.set_defaults(func=cmd_plan)

    # ---- apply ----
    p_apply = sub.add_parser("apply", help="terraform apply (requires review approval)")
    p_apply.add_argument("--dir", metavar="PATH", help="Terraform working directory")
    p_apply.add_argument("--review-dir", metavar="PATH", help="Override review file directory")
    p_apply.add_argument("--review-file", metavar="FILE", help="Explicit review file to check")
    p_apply.add_argument("--var-file", metavar="FILE", help="Variable file (.tfvars)")
    p_apply.add_argument("--var", metavar="KEY=VALUE", action="append", help="Variable override (repeatable)")
    p_apply.add_argument(
        "--auto-approve",
        action="store_true",
        help="Skip review gate (CI pipelines only — use with care)",
    )
    p_apply.add_argument(
        "--wait-timeout",
        type=int,
        default=3600,
        metavar="SECONDS",
        help="Max seconds to wait for human approval (default 3600)",
    )
    p_apply.set_defaults(func=cmd_apply)

    # ---- destroy ----
    p_destroy = sub.add_parser("destroy", help="terraform destroy (requires review approval)")
    p_destroy.add_argument("--dir", metavar="PATH", help="Terraform working directory")
    p_destroy.add_argument("--review-dir", metavar="PATH", help="Override review file directory")
    p_destroy.add_argument("--review-file", metavar="FILE", help="Explicit review file to check")
    p_destroy.add_argument("--auto-approve", action="store_true", help="Skip review gate")
    p_destroy.add_argument("--wait-timeout", type=int, default=3600, metavar="SECONDS")
    p_destroy.set_defaults(func=cmd_destroy)

    # ---- output ----
    p_out = sub.add_parser("output", help="terraform output -json")
    p_out.add_argument("--dir", metavar="PATH", help="Terraform working directory")
    p_out.add_argument("--name", metavar="NAME", help="Specific output name")
    p_out.set_defaults(func=cmd_output)

    # ---- deploy ----
    p_dep = sub.add_parser("deploy", help="vercel deploy")
    p_dep.add_argument("--dir", metavar="PATH", help="Frontend project directory (default: cwd)")
    p_dep.add_argument("--prod", action="store_true", help="Deploy to production URL")
    p_dep.add_argument("--token", metavar="TOKEN", help="Vercel auth token")
    p_dep.add_argument("--scope", metavar="SCOPE", help="Team scope")
    p_dep.add_argument("--env", metavar="KEY=VALUE", action="append", help="Runtime env var (repeatable)")
    p_dep.add_argument("--no-wait", action="store_true", help="Return immediately without waiting for build")
    p_dep.set_defaults(func=cmd_deploy)

    # ---- lookup ----
    p_look = sub.add_parser("lookup", help="AWS/GCP resource lookup")
    p_look.add_argument(
        "--provider",
        required=True,
        choices=["aws", "gcp"],
        help="Cloud provider",
    )
    p_look.add_argument("--service", required=True, metavar="SERVICE", help="Service name (e.g. ec2, s3)")
    p_look.add_argument("--type", required=True, metavar="TYPE", help="Resource type (e.g. instances, buckets)")
    p_look.add_argument("--region", metavar="REGION", help="Cloud region")
    p_look.add_argument("--project", metavar="PROJECT", help="GCP project ID")
    p_look.add_argument(
        "--filter",
        metavar="KEY=VALUE",
        action="append",
        help="Filter expression (repeatable)",
    )
    p_look.set_defaults(func=cmd_lookup)

    # ---- status ----
    p_status = sub.add_parser("status", help="List pending review files")
    p_status.add_argument("--review-dir", metavar="PATH", help="Override review directory")
    p_status.set_defaults(func=cmd_status)

    # ---- approve ----
    p_approve = sub.add_parser("approve", help="Programmatically approve a review file")
    p_approve.add_argument("review_file", metavar="FILE", help="Path to the review file")
    p_approve.set_defaults(func=cmd_approve)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "func"):
        parser.print_help()
        return 1

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n{\"success\": false, \"error\": \"Interrupted by user\"}")
        return 130
    except Exception as exc:  # noqa: BLE001
        _print_json({"success": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
