"""One deployment at a time: wait, or override before the container swap (#271)."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.deps import get_job_dispatcher
from app.models.deployment import Deployment
from app.repositories.deployments import DeploymentRepository
from app.repositories.jobs import JobState
from app.services.provisioning import JobKind
from tests.conftest import TEST_DATABASE_URL

REPO = "https://github.com/mangatohjunior/dev-portal"


class FakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.revoked: list[str] = []

    def dispatch(self, kind: JobKind, *, project_id: str, environment_id: str, **meta: str) -> str:
        job_id = str(uuid.uuid4())
        self.calls.append(
            {
                "job_id": job_id,
                "kind": kind,
                "project_id": project_id,
                "environment_id": environment_id,
                "meta": meta,
            }
        )
        return job_id

    def state(self, job_id: str) -> JobState:
        return JobState(job_id=job_id, status="queued", result=None, error=None)

    def revoke(self, job_id: str) -> None:
        self.revoked.append(job_id)


async def _project_env(
    client: AsyncClient, auth: dict[str, str], *, slug: str, overlap: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    project = (
        await client.post("/api/v1/projects", json={"slug": slug, "name": slug}, headers=auth)
    ).json()
    body: dict[str, str] = {"slug": "dev", "name": "Dev", "kind": "dev", "region": "ams3"}
    if overlap is not None:
        body["deploy_overlap"] = overlap
    environment = (
        await client.post(
            f"/api/v1/projects/{project['id']}/environments",
            json=body,
            headers=auth,
        )
    ).json()
    return project, environment


async def _deployment(
    client: AsyncClient, auth: dict[str, str], project_id: str, environment_id: str
) -> dict[str, Any]:
    created = await client.post(
        "/api/v1/deployments",
        json={"repo_url": REPO, "project_id": project_id, "environment_id": environment_id},
        headers=auth,
    )
    assert created.status_code == 201, created.text
    return created.json()


def _provision(project_id: str, environment_id: str) -> str:
    return f"/api/v1/projects/{project_id}/environments/{environment_id}/provision"


@pytest.mark.asyncio
async def test_existing_environment_defaults_to_wait_and_can_be_changed(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    _project, environment = await _project_env(client, auth, slug="overlap-default")
    assert environment["deploy_overlap"] == "wait"

    updated = await client.patch(
        f"/api/v1/projects/{environment['project_id']}/environments/{environment['id']}",
        json={"deploy_overlap": "override"},
        headers=auth,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["deploy_overlap"] == "override"

    rejected = await client.patch(
        f"/api/v1/projects/{environment['project_id']}/environments/{environment['id']}",
        json={"deploy_overlap": "parallel"},
        headers=auth,
    )
    assert rejected.status_code == 422


@pytest.mark.asyncio
async def test_wait_holds_the_newest_and_starts_it_when_the_active_deploy_finishes(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    dispatcher = FakeDispatcher()
    app.dependency_overrides[get_job_dispatcher] = lambda: dispatcher
    try:
        project, environment = await _project_env(client, auth, slug="overlap-wait")
        first = await _deployment(client, auth, project["id"], environment["id"])
        started = await client.post(
            _provision(project["id"], environment["id"]),
            json={"deployment_id": first["id"]},
            headers=auth,
        )
        assert started.status_code == 202, started.text
        assert started.json()["job_id"]
        assert started.json()["active_deployment_id"] is None

        second = await _deployment(client, auth, project["id"], environment["id"])
        held = await client.post(
            _provision(project["id"], environment["id"]),
            json={"deployment_id": second["id"]},
            headers=auth,
        )
        assert held.status_code == 202, held.text
        assert held.json()["job_id"] is None
        assert held.json()["active_deployment_id"] == first["id"]
        assert len(dispatcher.calls) == 1

        third = await _deployment(client, auth, project["id"], environment["id"])
        newer = await client.post(
            _provision(project["id"], environment["id"]),
            json={"deployment_id": third["id"]},
            headers=auth,
        )
        assert newer.status_code == 202, newer.text
        assert newer.json()["active_deployment_id"] == first["id"]
        assert len(dispatcher.calls) == 1

        second_row = await client.get(f"/api/v1/deployments/{second['id']}", headers=auth)
        third_row = await client.get(f"/api/v1/deployments/{third['id']}", headers=auth)
        assert second_row.json()["status"] == "cancelled"
        assert third_row.json()["status"] == "queued"
        assert third_row.json()["held_for_deployment_id"] == first["id"]

        finished = await client.post(f"/api/v1/deployments/{first['id']}/succeeded", headers=auth)
        assert finished.status_code == 200, finished.text
        assert len(dispatcher.calls) == 2
        assert dispatcher.calls[1]["meta"]["deployment_id"] == third["id"]
        released = await client.get(f"/api/v1/deployments/{third['id']}", headers=auth)
        assert released.json()["status"] == "running"
        assert released.json()["held_for_deployment_id"] is None
    finally:
        app.dependency_overrides.pop(get_job_dispatcher, None)


@pytest.mark.asyncio
async def test_wait_replays_a_pinned_commit_when_the_held_redeploy_starts(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    dispatcher = FakeDispatcher()
    app.dependency_overrides[get_job_dispatcher] = lambda: dispatcher
    try:
        project, environment = await _project_env(client, auth, slug="overlap-pin")
        source = await _deployment(client, auth, project["id"], environment["id"])
        assert (await client.post(f"/api/v1/deployments/{source['id']}/succeeded", headers=auth)).status_code == 200
        sha = "abcdefabcdefabcdefabcdefabcdefabcdefabcd"
        sessions = async_sessionmaker(app.state.engine, expire_on_commit=False)
        async with sessions() as session:
            row = await session.get(Deployment, uuid.UUID(source["id"]))
            assert row is not None
            row.git_sha = sha
            await session.commit()

        first = await client.post(f"/api/v1/deployments/{source['id']}/redeploy", headers=auth)
        assert first.status_code == 201, first.text
        second = await client.post(f"/api/v1/deployments/{source['id']}/redeploy", headers=auth)
        assert second.status_code == 201, second.text
        assert second.json()["status"] == "queued"
        assert second.json()["held_for_deployment_id"] == first.json()["id"]
        assert len(dispatcher.calls) == 1

        finished = await client.post(
            f"/api/v1/deployments/{first.json()['id']}/succeeded", headers=auth
        )
        assert finished.status_code == 200, finished.text
        assert len(dispatcher.calls) == 2
        assert dispatcher.calls[1]["meta"]["git_ref"] == sha
        assert dispatcher.calls[1]["meta"]["deployment_id"] == second.json()["id"]
    finally:
        app.dependency_overrides.pop(get_job_dispatcher, None)


@pytest.mark.asyncio
async def test_a_deploy_the_worker_finished_does_not_hold_the_next_one(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    """The row stays running until the console reports Done; finished_at ends the wait."""
    dispatcher = FakeDispatcher()
    app.dependency_overrides[get_job_dispatcher] = lambda: dispatcher
    try:
        project, environment = await _project_env(client, auth, slug="overlap-unwatched")
        first = await _deployment(client, auth, project["id"], environment["id"])
        started = await client.post(
            _provision(project["id"], environment["id"]),
            json={"deployment_id": first["id"]},
            headers=auth,
        )
        assert started.status_code == 202, started.text

        sessions = async_sessionmaker(app.state.engine, expire_on_commit=False)
        async with sessions() as session:
            row = await session.get(Deployment, uuid.UUID(first["id"]))
            assert row is not None
            row.finished_at = datetime.now(UTC)
            await session.commit()

        second = await _deployment(client, auth, project["id"], environment["id"])
        accepted = await client.post(
            _provision(project["id"], environment["id"]),
            json={"deployment_id": second["id"]},
            headers=auth,
        )
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["job_id"]
        assert accepted.json()["active_deployment_id"] is None
        assert len(dispatcher.calls) == 2
        unwatched = await client.get(f"/api/v1/deployments/{first['id']}", headers=auth)
        assert unwatched.json()["status"] == "running"
    finally:
        app.dependency_overrides.pop(get_job_dispatcher, None)


@pytest.mark.asyncio
async def test_a_waiting_deploy_that_cannot_start_is_marked_failed(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    class RefusingSecondJob(FakeDispatcher):
        def dispatch(self, kind: JobKind, *, project_id: str, environment_id: str, **meta: str) -> str:
            if self.calls:
                raise ConnectionError("broker is gone")
            return super().dispatch(kind, project_id=project_id, environment_id=environment_id, **meta)

    dispatcher = RefusingSecondJob()
    app.dependency_overrides[get_job_dispatcher] = lambda: dispatcher
    try:
        project, environment = await _project_env(client, auth, slug="overlap-unstartable")
        first = await _deployment(client, auth, project["id"], environment["id"])
        started = await client.post(
            _provision(project["id"], environment["id"]),
            json={"deployment_id": first["id"]},
            headers=auth,
        )
        assert started.status_code == 202, started.text
        second = await _deployment(client, auth, project["id"], environment["id"])
        held = await client.post(
            _provision(project["id"], environment["id"]),
            json={"deployment_id": second["id"]},
            headers=auth,
        )
        assert held.json()["active_deployment_id"] == first["id"]

        # The outcome of the first deploy stands even though the release failed.
        finished = await client.post(f"/api/v1/deployments/{first['id']}/succeeded", headers=auth)
        assert finished.status_code == 200, finished.text
        assert finished.json()["status"] == "succeeded"

        stuck = await client.get(f"/api/v1/deployments/{second['id']}", headers=auth)
        assert stuck.json()["status"] == "failed"
        assert stuck.json()["held_for_deployment_id"] is None

        # A failed row does not occupy the environment.
        third = await _deployment(client, auth, project["id"], environment["id"])
        dispatcher.calls.clear()
        retried = await client.post(
            _provision(project["id"], environment["id"]),
            json={"deployment_id": third["id"]},
            headers=auth,
        )
        assert retried.status_code == 202, retried.text
        assert retried.json()["job_id"]
    finally:
        app.dependency_overrides.pop(get_job_dispatcher, None)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="SQLite shares one connection, so two requests cannot overlap in separate transactions",
)
async def test_two_provisions_at_once_start_exactly_one(
    app: FastAPI, client: AsyncClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the environment lock each request reads the other as active and both wait."""
    dispatcher = FakeDispatcher()
    app.dependency_overrides[get_job_dispatcher] = lambda: dispatcher
    readers = 0
    both_have_read = asyncio.Event()
    read_blocking = DeploymentRepository.list_blocking

    async def read_then_pause(self: DeploymentRepository, environment_id: uuid.UUID, exclude_id: uuid.UUID):
        # Give the other request the chance to read before this one writes. The lock
        # stops it from reading at all, so the pause simply runs out.
        nonlocal readers
        rows = await read_blocking(self, environment_id, exclude_id)
        readers += 1
        if readers == 2:
            both_have_read.set()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(1):
                await both_have_read.wait()
        return rows

    monkeypatch.setattr(DeploymentRepository, "list_blocking", read_then_pause)
    try:
        project, environment = await _project_env(client, auth, slug="overlap-race")
        first = await _deployment(client, auth, project["id"], environment["id"])
        second = await _deployment(client, auth, project["id"], environment["id"])

        responses = await asyncio.gather(
            *(
                client.post(
                    _provision(project["id"], environment["id"]),
                    json={"deployment_id": row["id"]},
                    headers=auth,
                )
                for row in (first, second)
            )
        )

        assert [response.status_code for response in responses] == [202, 202]
        bodies = [response.json() for response in responses]
        started = [body for body in bodies if body["job_id"]]
        waiting = [body for body in bodies if body["active_deployment_id"]]
        assert len(started) == 1, bodies
        assert len(waiting) == 1, bodies
        assert len(dispatcher.calls) == 1
        assert waiting[0]["active_deployment_id"] == dispatcher.calls[0]["meta"]["deployment_id"]
    finally:
        app.dependency_overrides.pop(get_job_dispatcher, None)


@pytest.mark.asyncio
async def test_override_cancels_before_swap_and_conflicts_after_it(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    dispatcher = FakeDispatcher()
    app.dependency_overrides[get_job_dispatcher] = lambda: dispatcher
    try:
        project, environment = await _project_env(
            client, auth, slug="overlap-override", overlap="override"
        )
        assert environment["deploy_overlap"] == "override"
        active = await _deployment(client, auth, project["id"], environment["id"])
        started = await client.post(
            _provision(project["id"], environment["id"]),
            json={"deployment_id": active["id"]},
            headers=auth,
        )
        assert started.status_code == 202, started.text
        active_job = started.json()["job_id"]

        replacement = await _deployment(client, auth, project["id"], environment["id"])
        replaced = await client.post(
            _provision(project["id"], environment["id"]),
            json={"deployment_id": replacement["id"]},
            headers=auth,
        )
        assert replaced.status_code == 202, replaced.text
        assert replaced.json()["job_id"]
        assert dispatcher.revoked == [active_job]
        cancelled = await client.get(f"/api/v1/deployments/{active['id']}", headers=auth)
        assert cancelled.json()["status"] == "cancelled"
        assert len(dispatcher.calls) == 2

        sessions = async_sessionmaker(app.state.engine, expire_on_commit=False)
        async with sessions() as session:
            row = await session.get(Deployment, uuid.UUID(replacement["id"]))
            assert row is not None
            row.swap_started_at = datetime.now(UTC)
            await session.commit()

        blocked = await _deployment(client, auth, project["id"], environment["id"])
        refused = await client.post(
            _provision(project["id"], environment["id"]),
            json={"deployment_id": blocked["id"]},
            headers=auth,
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "conflict"
        assert len(dispatcher.calls) == 2
        still_running = await client.get(f"/api/v1/deployments/{replacement['id']}", headers=auth)
        assert still_running.json()["status"] == "running"
        parked = await client.get(f"/api/v1/deployments/{blocked['id']}", headers=auth)
        assert parked.json()["status"] == "cancelled"
    finally:
        app.dependency_overrides.pop(get_job_dispatcher, None)
