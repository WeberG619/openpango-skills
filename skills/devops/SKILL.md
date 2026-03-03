---
name: devops
description: "DevOps & cloud provisioning with Terraform, Vercel, and AWS/GCP wrappers"
version: "1.0.0"
user-invocable: true
metadata:
  capabilities:
    - devops/terraform-init
    - devops/terraform-plan
    - devops/terraform-apply
    - devops/vercel-deploy
    - devops/aws-lookup
    - devops/gcp-lookup
  author: "WeberG619"
  license: "MIT"
  openclaw:
    emoji: "🚀"
    skillKey: "openpango-devops"
---

# DevOps & Cloud Provisioning Skill

Provides infrastructure-as-code lifecycle management with a mandatory human-in-the-loop review gate before any `terraform apply`. Wraps Terraform, Vercel CLI, AWS CLI, and gcloud CLI — all using only Python stdlib (no pip packages required).

## Key Design Decision: Human Review Gate

The review gate is the core safety mechanism. When `terraform plan` runs, the plan output is written to a timestamped review file at:

```
~/.openclaw/workspace/devops/reviews/terraform-plan-{timestamp}.txt
```

The file contains instructions at the top and the plan output below. Before `terraform apply` can proceed, the review file **must** contain either `APPROVED` or `REJECTED` on a line by itself. This is enforced at the API level — `apply()` will raise if the gate has not been approved.

---

## Usage

```python
from skills.devops import TerraformProvisioner, ReviewGate, aws_lookup, vercel_deploy
from pathlib import Path

# --- Terraform lifecycle ---
tf = TerraformProvisioner(working_dir=Path("./infra"))

result = tf.init()
print(result)  # {"success": True, "stdout": "...", ...}

plan_result = tf.plan()
print(plan_result["review_file"])  # path to the review file

# Human opens the review file, reads the plan, adds "APPROVED"

apply_result = tf.apply()
print(apply_result)  # {"success": True, ...} or {"success": False, "error": "Not approved"}

# --- AWS lookup ---
buckets = aws_lookup(service="s3", resource_type="buckets")
print(buckets)

# --- Vercel deploy ---
deploy_result = vercel_deploy(project_dir=Path("./frontend"), prod=True)
print(deploy_result)
```

---

## CLI Usage

```bash
# Initialize Terraform
python -m skills.devops.cli init --dir ./infra

# Plan (writes review file, prints path)
python -m skills.devops.cli plan --dir ./infra

# Apply (blocks until review file is approved)
python -m skills.devops.cli apply --dir ./infra

# Deploy frontend to Vercel (production)
python -m skills.devops.cli deploy --dir ./frontend --prod

# AWS resource lookup
python -m skills.devops.cli lookup --provider aws --service s3 --type buckets

# GCP resource lookup
python -m skills.devops.cli lookup --provider gcp --service compute --type instances

# Check pending reviews
python -m skills.devops.cli status
```

---

## Security Model

- **No auto-approve by default** — `apply(auto_approve=True)` exists for CI pipelines but is explicitly opt-in.
- **Destroy requires double confirmation** — `destroy()` defaults to `auto_approve=False` and checks the review gate.
- **CLI availability checks** — every wrapper checks whether `terraform`, `vercel`, `aws`, or `gcloud` is on PATH before attempting execution; returns a structured error if missing.
- **Subprocess timeout** — all subprocess calls default to a 300-second timeout, configurable per call.
- **Stderr captured** — all errors from subprocesses are captured and returned in structured dicts, never swallowed.

---

## Review File Format

```
# Terraform Plan Review
# Generated: 2026-03-03T14:22:01
# Status: PENDING_REVIEW
#
# To approve: Add "APPROVED" on a new line at the end of this file
# To reject:  Add "REJECTED" on a new line at the end of this file
#
# --- Plan Output ---
<terraform plan output here>
```

---

## Module Layout

| File | Purpose |
|---|---|
| `provisioner.py` | `TerraformProvisioner` — full Terraform lifecycle |
| `review_gate.py` | `ReviewGate` — human approval enforcement |
| `cloud_lookup.py` | `aws_lookup` / `gcp_lookup` — read-only cloud queries |
| `vercel_deploy.py` | `deploy` / `list_deployments` / `inspect_deployment` |
| `cli.py` | `argparse` CLI entry point |
| `test_devops.py` | 30+ unit tests, all subprocess mocked |
