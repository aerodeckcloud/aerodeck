"""Unit tests for start-dashboard-on-droplet.sh and self-hosted deploy job."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "start-dashboard-on-droplet.sh"
)
WORKFLOW_PATH = REPO / ".github" / "workflows" / "dashboard-image.yml"


def test_start_dashboard_on_droplet_script_contract() -> None:
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text
    assert 'DASHBOARD_ENV="/etc/aerodeck/dashboard.env"' in text
    assert 'IMAGE_FILE="/etc/aerodeck/dashboard-image"' in text
    assert (
        'DEFAULT_IMAGE="registry.digitalocean.com/aerodeck/control-plane:dashboard-main"'
        in text
    )
    assert "docker login registry.digitalocean.com" in text
    assert 'docker pull "${DASHBOARD_IMAGE}"' in text
    assert 'docker tag "${DASHBOARD_IMAGE}" aerodeck/dashboard:dev' in text
    assert "--name aerodeck-dashboard --network host --restart unless-stopped" in text
    assert "http://127.0.0.1:3000/login" in text
    assert "publish_console_route_if_present" in text
    assert "publish-console-route.py" in text


def test_dashboard_workflow_uses_pull_based_cd_without_self_hosted_ssh() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "runs-on: self-hosted" not in workflow
    assert "runs-on: ubuntu-latest" in workflow
    reconciler = (
        Path(__file__).resolve().parents[1] / "scripts" / "reconcile-droplet-images.sh"
    ).read_text(encoding="utf-8")
    installer = (
        Path(__file__).resolve().parents[1] / "scripts" / "install-auto-updater.sh"
    ).read_text(encoding="utf-8")
    assert "flock -n" in reconciler
    assert "start-dashboard-on-droplet.sh" in reconciler
    assert "start-on-droplet.sh" in reconciler
    assert "aerodeck-auto-updater.timer" in installer
