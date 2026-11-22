"""Unit and API tests for automatic Pull Request preview environments (#258)."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.api.deps import get_job_dispatcher
from app.repositories.jobs import JobState
from app.services.docker_policy import CommandResult, LogSink, validate_git_ref
from app.services.docker_provisioner import DockerProvisioner
from app.services.provisioning import JobKind, PermanentJobError


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


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


class FakeCloneRunner:
    def __init__(self) -> None:
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


def test_git_ref_validation_and_branch_clone() -> None:
    assert validate_git_ref("feature/pr-42") == "feature/pr-42"
    with pytest.raises(PermanentJobError, match="invalid git_ref"):
        validate_git_ref("--upload-pack=evil")
    with pytest.raises(PermanentJobError, match="invalid git_ref"):
        validate_git_ref("a..b")

    runner = FakeCloneRunner()
    prov = DockerProvisioner(
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
    prov.provision(
        project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
        environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
        project_slug="portal",
        environment_slug="pr-42",
        repo_url="https://github.com/mangatohjunior/dev-portal",
        git_ref="feature/login",
    )
    assert "--branch" in runner.calls[0] and "feature/login" in runner.calls[0]


@pytest.mark.asyncio
async def test_pull_request_preview_open_sync_fork_and_close_lifecycle(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    dispatcher = FakeDispatcher()
    app.dependency_overrides[get_job_dispatcher] = lambda: dispatcher
    try:
        project = (
            await client.post("/api/v1/projects", json={"slug": "pr-app", "name": "PR App"}, headers=auth)
        ).json()
        env = (
            await client.post(
                f"/api/v1/projects/{project['id']}/environments",
                json={"slug": "dev", "name": "Dev", "kind": "dev", "region": "ams3"},
                headers=auth,
            )
        ).json()

        # Seed an encrypted env var on base env so preview clones it
        await client.put(
            f"/api/v1/projects/{project['id']}/environments/{env['id']}/env-vars",
            json={"items": [{"key": "APP_SECRET", "value": "secret-123"}]},
            headers=auth,
        )

        # Register webhook
        wh = (
            await client.put(
                f"/api/v1/projects/{project['id']}/environments/{env['id']}/webhook",
                json={"repo_url": "https://github.com/mangatohjunior/dev-portal", "branch": "main"},
                headers=auth,
            )
        ).json()
        secret = wh["secret"]

        # 1. Fork PR is rejected safely
        fork_payload = json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": "mangatohjunior/dev-portal"},
                "pull_request": {
                    "number": 99,
                    "head": {"ref": "evil", "repo": {"full_name": "attacker/dev-portal"}},
                },
            }
        ).encode("utf-8")
        fork_res = await client.post(
            "/api/v1/webhooks/github",
            content=fork_payload,
            headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sign(fork_payload, secret)},
        )
        assert fork_res.status_code == 200
        assert fork_res.json()["reason"] == "fork pull request ignored"

        # 2. PR opened -> creates pr-42 env, clones env vars, enqueues provision with git_ref
        open_payload = json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": "mangatohjunior/dev-portal"},
                "pull_request": {
                    "number": 42,
                    "head": {"ref": "feature/login", "repo": {"full_name": "mangatohjunior/dev-portal"}},
                },
            }
        ).encode("utf-8")
        open_res = await client.post(
            "/api/v1/webhooks/github",
            content=open_payload,
            headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sign(open_payload, secret)},
        )
        assert open_res.status_code == 202
        assert open_res.json()["status"] == "triggered"
        assert len(dispatcher.calls) == 1
        assert dispatcher.calls[0]["meta"]["git_ref"] == "feature/login"
        assert "APP_SECRET" in dispatcher.calls[0]["meta"]["env_vars_json"]

        # 3. PR synchronize -> reuses existing pr-42 environment
        sync_payload = json.dumps(
            {
                "action": "synchronize",
                "repository": {"full_name": "mangatohjunior/dev-portal"},
                "pull_request": {
                    "number": 42,
                    "head": {"ref": "feature/login", "repo": {"full_name": "mangatohjunior/dev-portal"}},
                },
            }
        ).encode("utf-8")
        sync_res = await client.post(
            "/api/v1/webhooks/github",
            content=sync_payload,
            headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sign(sync_payload, secret)},
        )
        assert sync_res.status_code == 202
        assert len(dispatcher.calls) == 2

        # 4. PR closed -> cleans up pr-42 environment
        close_payload = json.dumps(
            {
                "action": "closed",
                "repository": {"full_name": "mangatohjunior/dev-portal"},
                "pull_request": {
                    "number": 42,
                    "head": {"ref": "feature/login", "repo": {"full_name": "mangatohjunior/dev-portal"}},
                },
            }
        ).encode("utf-8")
        close_res = await client.post(
            "/api/v1/webhooks/github",
            content=close_payload,
            headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sign(close_payload, secret)},
        )
        assert close_res.status_code == 200
        assert close_res.json()["status"] == "cleaned_up"

        envs = (await client.get(f"/api/v1/projects/{project['id']}/environments", headers=auth)).json()["items"]
        assert [e["slug"] for e in envs] == ["dev"]
    finally:
        app.dependency_overrides.pop(get_job_dispatcher, None)
