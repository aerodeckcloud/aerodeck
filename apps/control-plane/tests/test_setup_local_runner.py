"""Unit tests for setup-local-runner.sh and Windows Git Bash path handling."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "setup-local-runner.sh"
)
OPS_WORKFLOW_PATH = REPO / ".github" / "workflows" / "droplet-ops.yml"


def test_setup_local_runner_script_contract() -> None:
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text
    assert 'gh variable set CONTROL_DROPLET_HOST --repo "${REPO}"' in text
    assert 'gh variable set DEPLOY_DASHBOARD_SELF_HOSTED --repo "${REPO}" --body "true"' in text
    assert "actions/runners/registration-token" in text
    assert "--unattended" in text
    assert "--replace" in text


def test_droplet_ops_workflow_disables_msys_pathconv_on_self_hosted() -> None:
    workflow = OPS_WORKFLOW_PATH.read_text(encoding="utf-8")
    assert 'MSYS_NO_PATHCONV: "1"' in workflow
