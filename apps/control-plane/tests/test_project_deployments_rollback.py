"""Unit and API tests for project deployment history, redeploy, and image rollback (#253)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.api.deps import get_job_dispatcher
from app.repositories.jobs import JobState
from app.services.docker_policy import CommandResult, LogSink, deployment_image_tag_for
from app.services.docker_provisioner import DockerProvisioner
from app.services.provisioning import JobKind, PermanentJobError


class FakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def dispatch(self, kind: JobKind, *, project_id: str, environment_id: str, **meta: str) -> str:
        self.calls.append(
            {"kind": kind, "project_id": project_id, "environment_id": environment_id, "meta": meta}
        )
        return str(uuid.uuid4())

    def state(self, job_id: str) -> JobState:
        return JobState(job_id=job_id, status="queued")


class FakeRollbackRunner:
    def __init__(self, *, image_exists: bool = True) -> None:
        self.image_exists = image_exists
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
        if args[:2] == ["git", "clone"]:
            target = Path(args[-1])
            target.mkdir(parents=True, exist_ok=True)
            (target / "Dockerfile").write_text("FROM alpine:3.20\n", encoding="utf-8")
            return CommandResult(0, "", "")
        if args[:3] == ["docker", "image", "inspect"]:
            return CommandResult(0, "[]", "") if self.image_exists else CommandResult(1, "", "No such image")
        return CommandResult(0, "", "")


def _make_provisioner(runner: FakeRollbackRunner) -> DockerProvisioner:
    return DockerProvisioner(
        docker_bin="docker",
        git_bin="git",
        bind_host="10.41.0.2",
        port_range_start=10000,
        port_range_end=19999,
        container_port=8000,
        memory_limit="512m",
        cpus_limit="0.5",
        pids_limit=256,
        build_timeout_seconds=300,
        runner=runner,
        health_timeout_seconds=10,
        port_allocator=lambda _h, _s, _e: 10420,
        health_checker=lambda _h, _p, _t: True,
        route_registrar=None,
    )


def test_provision_tags_immutable_deployment_image_and_supports_rollback() -> None:
    p_id = "f27ee337-a7d0-4d4d-8180-22579d6a2983"
    e_id = "bacbb040-41fa-4623-8e4b-02be2f7abf7b"
    d_id = "11111111-2222-4333-8444-555555555555"

    expected_tag = deployment_image_tag_for("portal", "dev", p_id, e_id, d_id)
    assert expected_tag == "aerodeck/portal-dev:111111112222"

    # Normal build tags both :latest and :<deploy_id>
    build_runner = FakeRollbackRunner()
    _make_provisioner(build_runner).provision(
        project_id=p_id,
        environment_id=e_id,
        project_slug="portal",
        environment_slug="dev",
        repo_url="https://github.com/mangatohjunior/dev-portal",
        image_deployment_id=d_id,
    )
    build_cmd = build_runner.calls[2]
    assert build_cmd[:2] == ["docker", "build"]
    assert expected_tag in build_cmd

    # Rollback skips git clone and docker build completely
    rb_runner = FakeRollbackRunner(image_exists=True)
    _make_provisioner(rb_runner).provision(
        project_id=p_id,
        environment_id=e_id,
        project_slug="portal",
        environment_slug="dev",
        repo_url="https://github.com/mangatohjunior/dev-portal",
        rollback_from_deployment_id=d_id,
    )
    assert [c[:2] for c in rb_runner.calls] == [
        ["docker", "image"],
        ["docker", "run"],
        ["docker", "rm"],
        ["docker", "rename"],
    ]
    assert rb_runner.calls[1][-1] == expected_tag

    # Missing rollback image fails cleanly
    missing_runner = FakeRollbackRunner(image_exists=False)
    with pytest.raises(PermanentJobError, match="no longer available"):
        _make_provisioner(missing_runner).provision(
            project_id=p_id,
            environment_id=e_id,
            project_slug="portal",
            environment_slug="dev",
            repo_url="https://github.com/mangatohjunior/dev-portal",
            rollback_from_deployment_id=d_id,
        )


@pytest.mark.asyncio
async def test_project_deployments_redeploy_and_rollback_endpoints(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    dispatcher = FakeDispatcher()
    app.dependency_overrides[get_job_dispatcher] = lambda: dispatcher
    try:
        project = (
            await client.post("/api/v1/projects", json={"slug": "rb-app", "name": "RB App"}, headers=auth)
        ).json()
        env = (
            await client.post(
                f"/api/v1/projects/{project['id']}/environments",
                json={"slug": "dev", "name": "Dev", "kind": "dev", "region": "ams3"},
                headers=auth,
            )
        ).json()
        dep = (
            await client.post(
                "/api/v1/deployments",
                json={
                    "repo_url": "https://github.com/mangatohjunior/dev-portal",
                    "project_id": project["id"],
                    "environment_id": env["id"],
                },
                headers=auth,
            )
        ).json()

        # Project-filtered list returns the deployment
        listed = await client.get(
            f"/api/v1/projects/{project['id']}/deployments?environment_id={env['id']}",
            headers=auth,
        )
        assert listed.status_code == 200
        assert listed.json()["total"] == 1
        assert listed.json()["items"][0]["id"] == dep["id"]

        # Rollback before succeeded -> 422
        bad_rb = await client.post(f"/api/v1/deployments/{dep['id']}/rollback", headers=auth)
        assert bad_rb.status_code == 422

        # Mark succeeded, then redeploy and rollback both return 201
        assert (await client.post(f"/api/v1/deployments/{dep['id']}/succeeded", headers=auth)).status_code == 200

        redeployed = await client.post(f"/api/v1/deployments/{dep['id']}/redeploy", headers=auth)
        assert redeployed.status_code == 201
        assert redeployed.json()["id"] != dep["id"]
        assert "rollback_from_deployment_id" not in dispatcher.calls[0]["meta"]

        rolled_back = await client.post(f"/api/v1/deployments/{dep['id']}/rollback", headers=auth)
        assert rolled_back.status_code == 201
        assert dispatcher.calls[1]["meta"]["rollback_from_deployment_id"] == dep["id"]
    finally:
        app.dependency_overrides.pop(get_job_dispatcher, None)
