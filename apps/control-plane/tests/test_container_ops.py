"""Unit and API tests for container lifecycle actions and live resource metrics (#254)."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient

from app.services.container_ops import fetch_container_metrics_sync, run_container_action_sync
from app.services.docker_policy import CommandResult, LogSink


class FakeOpsRunner:
    def __init__(
        self,
        *,
        action_res: CommandResult | None = None,
        inspect_res: CommandResult | None = None,
        stats_res: CommandResult | None = None,
    ) -> None:
        self.action_res = action_res or CommandResult(0, "", "")
        self.inspect_res = inspect_res or CommandResult(0, "running\n", "")
        self.stats_res = stats_res or CommandResult(0, "1.42%|48.2MiB / 512MiB|9.41%\n", "")
        self.calls: list[list[str]] = []

    def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 600,
        on_line: LogSink | None = None,
    ) -> CommandResult:
        _ = (cwd, env, timeout, on_line)
        self.calls.append(args)
        if args[1] in {"start", "stop", "restart"}:
            return self.action_res
        if args[1] == "inspect":
            return self.inspect_res
        if args[1] == "stats":
            return self.stats_res
        return CommandResult(0, "", "")


def test_run_container_action_executes_and_reports_state() -> None:
    p_id = "f27ee337-a7d0-4d4d-8180-22579d6a2983"
    e_id = "bacbb040-41fa-4623-8e4b-02be2f7abf7b"

    runner = FakeOpsRunner(inspect_res=CommandResult(0, "exited\n", ""))
    res = run_container_action_sync(project_id=p_id, environment_id=e_id, action="stop", runner=runner)
    assert res == {
        "container": "aerodeck-app-f27ee337-bacbb040",
        "action": "stop",
        "status": "exited",
        "ok": True,
        "error": None,
    }
    assert runner.calls[0] == ["docker", "stop", "aerodeck-app-f27ee337-bacbb040"]

    missing_runner = FakeOpsRunner(action_res=CommandResult(1, "", "Error: No such container"))
    missing = run_container_action_sync(project_id=p_id, environment_id=e_id, action="restart", runner=missing_runner)
    assert missing["status"] == "not_found"
    assert missing["ok"] is False

    with pytest.raises(ValueError, match="Unsupported container action"):
        run_container_action_sync(project_id=p_id, environment_id=e_id, action="rm", runner=runner)


def test_fetch_container_metrics_parses_inspect_and_stats() -> None:
    p_id = "f27ee337-a7d0-4d4d-8180-22579d6a2983"
    e_id = "bacbb040-41fa-4623-8e4b-02be2f7abf7b"

    runner = FakeOpsRunner(
        inspect_res=CommandResult(0, "running|2026-11-22T15:00:00Z|2\n", ""),
        stats_res=CommandResult(0, "2.35%|64.5MiB / 512MiB|12.60%\n", ""),
    )
    metrics = fetch_container_metrics_sync(project_id=p_id, environment_id=e_id, runner=runner)
    assert metrics == {
        "container": "aerodeck-app-f27ee337-bacbb040",
        "status": "running",
        "cpu_percent": "2.35%",
        "memory_usage": "64.5MiB / 512MiB",
        "memory_percent": "12.60%",
        "started_at": "2026-11-22T15:00:00Z",
        "restart_count": 2,
        "error": None,
    }


@pytest.mark.asyncio
async def test_container_action_and_metrics_api_endpoints(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    project = (
        await client.post("/api/v1/projects", json={"slug": "ops-app", "name": "Ops App"}, headers=auth)
    ).json()
    env = (
        await client.post(
            f"/api/v1/projects/{project['id']}/environments",
            json={"slug": "dev", "name": "Dev", "kind": "dev", "region": "ams3"},
            headers=auth,
        )
    ).json()
    base = f"/api/v1/projects/{project['id']}/environments/{env['id']}"

    # Invalid action -> 422
    bad = await client.post(f"{base}/actions", json={"action": "kill"}, headers=auth)
    assert bad.status_code == 422

    # Valid action degrades gracefully when backend is noop
    ok = await client.post(f"{base}/actions", json={"action": "restart"}, headers=auth)
    assert ok.status_code == 200
    assert ok.json()["status"] == "unavailable"

    # Metrics degrades gracefully when backend is noop
    metrics = await client.get(f"{base}/metrics", headers=auth)
    assert metrics.status_code == 200
    assert metrics.json()["status"] == "unavailable"
