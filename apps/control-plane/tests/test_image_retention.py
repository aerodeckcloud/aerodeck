"""Keep a bounded set of succeeded images per environment (#274)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.models.deployment import Deployment, DeploymentStatus
from app.repositories.deployments import DeploymentRepository
from app.repositories.environments import EnvironmentRepository
from app.services.docker_policy import CommandResult, deployment_image_tag_for
from app.services.image_retention import (
    ImageRetentionService,
    RetentionCandidate,
    image_ids_to_drop,
    prune_succeeded_images,
    remove_image_tags,
)
from tests.conftest import make_settings

REPO = "https://github.com/mangatohjunior/dev-portal"
PROJECTS = "/api/v1/projects"


class FakeImageRm:
    def __init__(self, *, fail: set[str] | None = None, missing: set[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail or set()
        self.missing = missing or set()

    def run(
        self,
        args: list[str],
        *,
        cwd: object = None,
        env: dict[str, str] | None = None,
        timeout: int = 600,
        on_line: object = None,
    ) -> CommandResult:
        _ = (cwd, env, timeout, on_line)
        self.calls.append(args)
        tag = args[-1]
        if tag in self.fail:
            return CommandResult(1, "", "Error response from daemon")
        if tag in self.missing:
            return CommandResult(1, "", f"Error: No such image: {tag}")
        return CommandResult(0, "", "")


def _candidate(
    deployment_id: uuid.UUID,
    *,
    minutes: int,
    pruned: bool = False,
) -> RetentionCandidate:
    started = datetime(2026, 11, 23, tzinfo=UTC) + timedelta(minutes=minutes)
    return RetentionCandidate(
        deployment_id=deployment_id,
        finished_at=started,
        started_at=started,
        image_pruned_at=started if pruned else None,
    )


def test_image_ids_to_drop_keeps_the_newest_and_the_one_that_just_succeeded() -> None:
    oldest, middle, newest = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rows = [
        _candidate(oldest, minutes=1),
        _candidate(middle, minutes=2),
        _candidate(newest, minutes=3),
    ]

    dropped = image_ids_to_drop(rows, keep=1, protect_deployment_id=newest)

    assert set(dropped) == {middle, oldest}
    assert newest not in dropped


def test_image_ids_to_drop_still_protects_an_older_current_image() -> None:
    oldest, newest = uuid.uuid4(), uuid.uuid4()
    rows = [_candidate(oldest, minutes=1), _candidate(newest, minutes=2)]

    dropped = image_ids_to_drop(rows, keep=1, protect_deployment_id=oldest)

    assert oldest not in dropped
    assert newest not in dropped


def test_remove_image_tags_skips_a_refused_delete_and_accepts_a_missing_tag() -> None:
    gone = uuid.uuid4()
    refused = uuid.uuid4()
    missing = uuid.uuid4()
    runner = FakeImageRm(fail={"refused"}, missing={"missing"})

    removed = remove_image_tags(
        {gone: "gone", refused: "refused", missing: "missing"},
        runner=runner,
        docker_bin="docker",
        docker_host=None,
    )

    assert set(removed) == {gone, missing}
    assert refused not in removed


def test_schedule_does_not_raise_when_the_queue_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_settings: Settings) -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr("app.core.queue.create_celery_app", boom)
    service = ImageRetentionService(
        make_settings("sqlite+aiosqlite://", provisioner_backend="docker", redis_url="redis://localhost:9/0")
    )

    service.schedule(uuid.uuid4(), uuid.uuid4())


async def _project(client: AsyncClient, auth: dict[str, str]) -> dict:
    response = await client.post(PROJECTS, json={"slug": "portal", "name": "Portal"}, headers=auth)
    assert response.status_code == 201, response.text
    return response.json()


async def _environment(
    client: AsyncClient, auth: dict[str, str], project_id: str, slug: str, **extra: object
) -> dict:
    body = {"slug": slug, "name": slug, "kind": "dev", **extra}
    response = await client.post(f"{PROJECTS}/{project_id}/environments", json=body, headers=auth)
    assert response.status_code == 201, response.text
    return response.json()


async def _deployment(
    client: AsyncClient, auth: dict[str, str], project_id: str, environment_id: str
) -> dict:
    response = await client.post(
        "/api/v1/deployments",
        json={"repo_url": REPO, "project_id": project_id, "environment_id": environment_id},
        headers=auth,
    )
    assert response.status_code == 201, response.text
    done = await client.post(f"/api/v1/deployments/{response.json()['id']}/succeeded", headers=auth)
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "succeeded"
    return done.json()


@pytest.mark.asyncio
async def test_new_environment_keeps_five_rollback_images(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    project = await _project(client, auth)
    created = await _environment(client, auth, project["id"], "dev")

    assert created["rollback_images_to_keep"] == 5
    assert created["retained_rollback_images"] == 0

    low = await client.post(
        f"{PROJECTS}/{project['id']}/environments",
        json={"slug": "low", "name": "Low", "kind": "dev", "rollback_images_to_keep": 0},
        headers=auth,
    )
    high = await client.patch(
        f"{PROJECTS}/{project['id']}/environments/{created['id']}",
        json={"rollback_images_to_keep": 21},
        headers=auth,
    )
    kept = await client.patch(
        f"{PROJECTS}/{project['id']}/environments/{created['id']}",
        json={"rollback_images_to_keep": 1},
        headers=auth,
    )

    assert low.status_code == 422
    assert high.status_code == 422
    assert kept.status_code == 200
    assert kept.json()["rollback_images_to_keep"] == 1


@pytest.mark.asyncio
async def test_prune_drops_older_images_and_a_failure_leaves_the_deploy_succeeded(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    project = await _project(client, auth)
    environment = await _environment(client, auth, project["id"], "dev", rollback_images_to_keep=1)
    staging = await _environment(client, auth, project["id"], "staging")
    oldest = await _deployment(client, auth, project["id"], environment["id"])
    middle = await _deployment(client, auth, project["id"], environment["id"])
    newest = await _deployment(client, auth, project["id"], environment["id"])
    base = datetime(2026, 11, 23, tzinfo=UTC)
    sessions = async_sessionmaker(app.state.engine, expire_on_commit=False)
    async with sessions() as session:
        for index, deployment_id in enumerate((oldest["id"], middle["id"], newest["id"])):
            row = await session.get(Deployment, uuid.UUID(deployment_id))
            assert row is not None
            row.finished_at = base + timedelta(minutes=index)
        await session.commit()

    runner = FakeImageRm()
    async with sessions() as session:
        removed = await prune_succeeded_images(
            EnvironmentRepository(session),
            DeploymentRepository(session),
            environment_id=uuid.UUID(environment["id"]),
            protect_deployment_id=uuid.UUID(newest["id"]),
            runner=runner,
            docker_bin="docker",
            docker_host=None,
        )
        await session.commit()
    newest_tag = deployment_image_tag_for(
        "portal", "dev", project["id"], environment["id"], newest["id"]
    )
    assert all(call[-1] != newest_tag for call in runner.calls)

    async with sessions() as session:
        stored_newest = await session.get(Deployment, uuid.UUID(newest["id"]))
        stored_oldest = await session.get(Deployment, uuid.UUID(oldest["id"]))
        assert stored_newest is not None and stored_newest.status is DeploymentStatus.succeeded
        assert stored_newest.image_pruned_at is None
        assert stored_oldest is not None and stored_oldest.image_pruned_at is not None
        assert uuid.UUID(newest["id"]) not in removed
        assert uuid.UUID(oldest["id"]) in removed

    listed = await client.get(
        f"{PROJECTS}/{project['id']}/environments/{environment['id']}", headers=auth
    )
    assert listed.status_code == 200
    assert listed.json()["retained_rollback_images"] == 1

    refused = await client.post(f"/api/v1/deployments/{oldest['id']}/rollback", headers=auth)
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "image_not_retained"

    promoted = await client.post(
        f"/api/v1/deployments/{oldest['id']}/promote",
        json={"environment_id": staging["id"]},
        headers=auth,
    )
    assert promoted.status_code == 422, promoted.text
    assert promoted.json()["code"] == "image_not_retained"

    async with sessions() as session:
        row = await session.get(Deployment, uuid.UUID(middle["id"]))
        assert row is not None
        row.image_pruned_at = None
        await session.commit()
        refused_rm = await prune_succeeded_images(
            EnvironmentRepository(session),
            DeploymentRepository(session),
            environment_id=uuid.UUID(environment["id"]),
            protect_deployment_id=uuid.UUID(newest["id"]),
            runner=FakeImageRm(
            fail={
                deployment_image_tag_for(
                    "portal", "dev", project["id"], environment["id"], middle["id"]
                )
            }
        ),
            docker_bin="docker",
            docker_host=None,
        )
        await session.commit()
        again = await session.get(Deployment, uuid.UUID(middle["id"]))
        assert again is not None
        assert again.status is DeploymentStatus.succeeded
        assert again.image_pruned_at is None
        assert refused_rm == []
