"""Promote a succeeded deployment's image to another environment (#272)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.deps import get_image_lookup, get_job_dispatcher, get_settings
from app.core.config import Settings
from app.models.deployment import Deployment
from app.repositories.jobs import JobState
from app.services.docker_policy import CommandResult, LogSink
from app.services.docker_provisioner import DockerProvisioner
from app.services.image_presence import RetainedImage, WorkerImageLookup, image_present_sync
from app.services.provisioning import JobKind, NoopProvisioner, PermanentJobError
from app.tasks import environments as tasks
from tests.conftest import make_settings


@pytest.fixture
def settings(database_url: str, tmp_path: Path) -> Settings:
    """Promote only runs against the Docker backend."""
    return make_settings(
        database_url,
        certificate_key_dir=str(tmp_path / "cert-keys"),
        provisioner_backend="docker",
    )

REPO = "https://github.com/mangatohjunior/dev-portal"
SHA = "0123456789abcdef0123456789abcdef01234567"


class FakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def dispatch(self, kind: JobKind, *, project_id: str, environment_id: str, **meta: str) -> str:
        self.calls.append({"kind": kind, "environment_id": environment_id, "meta": meta})
        return str(uuid.uuid4())

    def state(self, job_id: str) -> JobState:
        return JobState(job_id=job_id, status="queued", result=None, error=None)

    def revoke(self, job_id: str) -> None:
        _ = job_id


class FakeImageLookup:
    def __init__(self, present: bool | None = True) -> None:
        self.present = present
        self.asked: list[RetainedImage] = []

    async def is_present(self, image: RetainedImage) -> bool | None:
        self.asked.append(image)
        return self.present


@pytest.fixture
def dispatcher(app: FastAPI):
    fake = FakeDispatcher()
    app.dependency_overrides[get_job_dispatcher] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_job_dispatcher, None)


@pytest.fixture
def images(app: FastAPI):
    fake = FakeImageLookup()
    app.dependency_overrides[get_image_lookup] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_image_lookup, None)


async def _project(client: AsyncClient, headers: dict[str, str], slug: str) -> dict[str, Any]:
    created = await client.post("/api/v1/projects", json={"slug": slug, "name": slug}, headers=headers)
    assert created.status_code == 201, created.text
    return created.json()


async def _environment(
    client: AsyncClient,
    headers: dict[str, str],
    project_id: str,
    kind: str,
    *,
    overlap: str | None = None,
) -> dict[str, Any]:
    body: dict[str, str] = {"slug": kind, "name": kind.title(), "kind": kind, "region": "ams3"}
    if overlap is not None:
        body["deploy_overlap"] = overlap
    created = await client.post(
        f"/api/v1/projects/{project_id}/environments", json=body, headers=headers
    )
    assert created.status_code == 201, created.text
    return created.json()


async def _deployment(
    client: AsyncClient, headers: dict[str, str], project_id: str, environment_id: str
) -> dict[str, Any]:
    created = await client.post(
        "/api/v1/deployments",
        json={"repo_url": REPO, "project_id": project_id, "environment_id": environment_id},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    return created.json()


async def _edit_row(app: FastAPI, deployment_id: str, **fields: Any) -> None:
    sessions = async_sessionmaker(app.state.engine, expire_on_commit=False)
    async with sessions() as session:
        row = await session.get(Deployment, uuid.UUID(deployment_id))
        assert row is not None
        for name, value in fields.items():
            setattr(row, name, value)
        await session.commit()


async def _succeeded_build(
    app: FastAPI, client: AsyncClient, headers: dict[str, str], project_id: str, environment_id: str
) -> dict[str, Any]:
    """A deployment that built its own image: succeeded, with a recorded commit."""
    built = await _deployment(client, headers, project_id, environment_id)
    done = await client.post(f"/api/v1/deployments/{built['id']}/succeeded", headers=headers)
    assert done.status_code == 200, done.text
    await _edit_row(app, built["id"], git_sha=SHA, git_ref="main")
    return built


def _promote(deployment_id: str) -> str:
    return f"/api/v1/deployments/{deployment_id}/promote"


async def _deployment_total(client: AsyncClient, headers: dict[str, str], project_id: str) -> int:
    listed = await client.get(f"/api/v1/projects/{project_id}/deployments", headers=headers)
    assert listed.status_code == 200, listed.text
    return listed.json()["total"]


@pytest.mark.asyncio
async def test_promote_runs_the_source_image_on_the_target(
    app: FastAPI,
    client: AsyncClient,
    auth: dict[str, str],
    dispatcher: FakeDispatcher,
    images: FakeImageLookup,
) -> None:
    project = await _project(client, auth, "promote-ok")
    dev = await _environment(client, auth, project["id"], "dev")
    staging = await _environment(client, auth, project["id"], "staging")
    source = await _succeeded_build(app, client, auth, project["id"], dev["id"])

    promoted = await client.post(
        _promote(source["id"]), json={"environment_id": staging["id"]}, headers=auth
    )

    assert promoted.status_code == 201, promoted.text
    body = promoted.json()
    assert body["id"] != source["id"]
    assert body["environment_id"] == staging["id"]
    assert body["trigger"] == "promote"
    assert body["git_sha"] == SHA
    assert body["git_ref"] == "main"
    assert body["status"] == "running"

    # The worker is told which environment built the image. It gets no ref to clone.
    assert len(dispatcher.calls) == 1
    call = dispatcher.calls[0]
    assert call["environment_id"] == staging["id"]
    assert call["meta"]["deployment_id"] == body["id"]
    assert call["meta"]["promote_from_deployment_id"] == source["id"]
    assert call["meta"]["promote_from_environment_id"] == dev["id"]
    assert call["meta"]["promote_from_environment_slug"] == "dev"
    assert call["meta"]["environment_slug"] == "staging"
    assert "git_ref" not in call["meta"]
    assert "rollback_from_deployment_id" not in call["meta"]

    # The image that was checked is the one the source environment built.
    assert [(asked.environment_slug, str(asked.deployment_id)) for asked in images.asked] == [
        ("dev", source["id"])
    ]


@pytest.mark.asyncio
async def test_a_promoted_deployment_can_be_promoted_again(
    app: FastAPI,
    client: AsyncClient,
    auth: dict[str, str],
    dispatcher: FakeDispatcher,
    images: FakeImageLookup,
) -> None:
    """dev → staging → prod: the staging row carries the commit, so it has an image too."""
    project = await _project(client, auth, "promote-chain")
    dev = await _environment(client, auth, project["id"], "dev")
    staging = await _environment(client, auth, project["id"], "staging")
    prod = await _environment(client, auth, project["id"], "prod")
    source = await _succeeded_build(app, client, auth, project["id"], dev["id"])

    first = await client.post(
        _promote(source["id"]), json={"environment_id": staging["id"]}, headers=auth
    )
    assert first.status_code == 201, first.text
    done = await client.post(f"/api/v1/deployments/{first.json()['id']}/succeeded", headers=auth)
    assert done.status_code == 200, done.text

    second = await client.post(
        _promote(first.json()["id"]), json={"environment_id": prod["id"]}, headers=auth
    )

    assert second.status_code == 201, second.text
    assert second.json()["git_sha"] == SHA
    assert dispatcher.calls[1]["meta"]["promote_from_deployment_id"] == first.json()["id"]
    assert dispatcher.calls[1]["meta"]["promote_from_environment_slug"] == "staging"


@pytest.mark.asyncio
async def test_promote_to_production_needs_an_owner(
    app: FastAPI,
    client: AsyncClient,
    auth: dict[str, str],
    dispatcher: FakeDispatcher,
    images: FakeImageLookup,
) -> None:
    async def user(email: str) -> dict[str, str]:
        created = await client.post(
            "/api/v1/console-users",
            json={"email": email, "password": "correct-horse-battery-staple"},
            headers=auth,
        )
        assert created.status_code == 201, created.text
        return {**auth, "X-AeroDeck-User-Id": created.json()["id"]}

    owner = await user("promote-owner@aerodeck.test")
    developer = await user("promote-dev@aerodeck.test")
    viewer = await user("promote-viewer@aerodeck.test")
    project = await _project(client, owner, "promote-roles")
    for email, role in (
        ("promote-dev@aerodeck.test", "developer"),
        ("promote-viewer@aerodeck.test", "viewer"),
    ):
        added = await client.put(
            f"/api/v1/projects/{project['id']}/members",
            json={"email": email, "role": role},
            headers=owner,
        )
        assert added.status_code == 200, added.text
    dev = await _environment(client, owner, project["id"], "dev")
    staging = await _environment(client, owner, project["id"], "staging")
    prod = await _environment(client, owner, project["id"], "prod")
    source = await _succeeded_build(app, client, owner, project["id"], dev["id"])
    before = await _deployment_total(client, owner, project["id"])

    refused = await client.post(
        _promote(source["id"]), json={"environment_id": prod["id"]}, headers=developer
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "forbidden"
    viewer_refused = await client.post(
        _promote(source["id"]), json={"environment_id": staging["id"]}, headers=viewer
    )
    assert viewer_refused.status_code == 403, viewer_refused.text
    assert await _deployment_total(client, owner, project["id"]) == before
    assert dispatcher.calls == []

    to_staging = await client.post(
        _promote(source["id"]), json={"environment_id": staging["id"]}, headers=developer
    )
    assert to_staging.status_code == 201, to_staging.text
    assert to_staging.json()["actor_user_id"] == developer["X-AeroDeck-User-Id"]
    to_prod = await client.post(
        _promote(source["id"]), json={"environment_id": prod["id"]}, headers=owner
    )
    assert to_prod.status_code == 201, to_prod.text
    assert [call["environment_id"] for call in dispatcher.calls] == [staging["id"], prod["id"]]


@pytest.mark.asyncio
async def test_promote_refuses_what_cannot_be_promoted(
    app: FastAPI,
    client: AsyncClient,
    auth: dict[str, str],
    dispatcher: FakeDispatcher,
    images: FakeImageLookup,
) -> None:
    project = await _project(client, auth, "promote-refuse")
    dev = await _environment(client, auth, project["id"], "dev")
    staging = await _environment(client, auth, project["id"], "staging")
    other = await _project(client, auth, "promote-elsewhere")
    elsewhere = await _environment(client, auth, other["id"], "staging")
    source = await _succeeded_build(app, client, auth, project["id"], dev["id"])
    before = await _deployment_total(client, auth, project["id"])

    async def refused(deployment_id: str, environment_id: str) -> str:
        response = await client.post(
            _promote(deployment_id), json={"environment_id": environment_id}, headers=auth
        )
        assert response.status_code == 422, response.text
        return response.json()["detail"]

    assert "not in this deployment's project" in await refused(source["id"], elsewhere["id"])
    assert "not in this deployment's project" in await refused(source["id"], str(uuid.uuid4()))
    assert "another environment" in await refused(source["id"], dev["id"])

    running = await _deployment(client, auth, project["id"], dev["id"])
    await _edit_row(app, running["id"], git_sha=SHA)
    assert "Only succeeded" in await refused(running["id"], staging["id"])

    # A rollback row reuses another deployment's image and records no commit of its own.
    no_image = await _deployment(client, auth, project["id"], dev["id"])
    await client.post(f"/api/v1/deployments/{no_image['id']}/succeeded", headers=auth)
    assert "no retained image" in await refused(no_image["id"], staging["id"])

    unlinked = (
        await client.post("/api/v1/deployments", json={"repo_url": REPO}, headers=auth)
    ).json()
    await client.post(f"/api/v1/deployments/{unlinked['id']}/succeeded", headers=auth)
    assert "not linked" in await refused(unlinked["id"], staging["id"])

    images.present = False
    assert "no longer on the host" in await refused(source["id"], staging["id"])

    assert dispatcher.calls == []
    # Only the two rows this test created itself were added.
    assert await _deployment_total(client, auth, project["id"]) == before + 2

    missing = await client.post(
        _promote(str(uuid.uuid4())), json={"environment_id": staging["id"]}, headers=auth
    )
    assert missing.status_code == 404, missing.text


@pytest.mark.asyncio
async def test_promote_requires_the_docker_backend(
    app: FastAPI,
    client: AsyncClient,
    auth: dict[str, str],
    dispatcher: FakeDispatcher,
    images: FakeImageLookup,
) -> None:
    """Terraform/noop would drop promote_from_* and still report success."""
    current = app.dependency_overrides[get_settings]()
    app.dependency_overrides[get_settings] = lambda: current.model_copy(
        update={"provisioner_backend": "noop"}
    )
    try:
        project = await _project(client, auth, "promote-noop")
        dev = await _environment(client, auth, project["id"], "dev")
        staging = await _environment(client, auth, project["id"], "staging")
        source = await _succeeded_build(app, client, auth, project["id"], dev["id"])
        before = await _deployment_total(client, auth, project["id"])

        refused = await client.post(
            _promote(source["id"]), json={"environment_id": staging["id"]}, headers=auth
        )

        assert refused.status_code == 422, refused.text
        assert "Docker provisioner" in refused.json()["detail"]
        assert dispatcher.calls == []
        assert await _deployment_total(client, auth, project["id"]) == before
    finally:
        app.dependency_overrides[get_settings] = lambda: current


@pytest.mark.asyncio
async def test_promote_goes_ahead_when_nobody_can_check_the_image(
    app: FastAPI,
    client: AsyncClient,
    auth: dict[str, str],
    dispatcher: FakeDispatcher,
    images: FakeImageLookup,
) -> None:
    """The provision job checks again, so an unreachable worker does not block a promotion."""
    images.present = None
    project = await _project(client, auth, "promote-unknown")
    dev = await _environment(client, auth, project["id"], "dev")
    staging = await _environment(client, auth, project["id"], "staging")
    source = await _succeeded_build(app, client, auth, project["id"], dev["id"])

    promoted = await client.post(
        _promote(source["id"]), json={"environment_id": staging["id"]}, headers=auth
    )

    assert promoted.status_code == 201, promoted.text
    assert len(dispatcher.calls) == 1


@pytest.mark.asyncio
async def test_promote_waits_behind_the_targets_active_deploy(
    app: FastAPI,
    client: AsyncClient,
    auth: dict[str, str],
    dispatcher: FakeDispatcher,
    images: FakeImageLookup,
) -> None:
    project = await _project(client, auth, "promote-wait")
    dev = await _environment(client, auth, project["id"], "dev")
    staging = await _environment(client, auth, project["id"], "staging")
    source = await _succeeded_build(app, client, auth, project["id"], dev["id"])
    active = await _deployment(client, auth, project["id"], staging["id"])
    started = await client.post(
        f"/api/v1/projects/{project['id']}/environments/{staging['id']}/provision",
        json={"deployment_id": active["id"]},
        headers=auth,
    )
    assert started.status_code == 202, started.text

    promoted = await client.post(
        _promote(source["id"]), json={"environment_id": staging["id"]}, headers=auth
    )

    assert promoted.status_code == 201, promoted.text
    assert promoted.json()["status"] == "queued"
    assert promoted.json()["held_for_deployment_id"] == active["id"]
    assert len(dispatcher.calls) == 1

    finished = await client.post(f"/api/v1/deployments/{active['id']}/succeeded", headers=auth)
    assert finished.status_code == 200, finished.text
    # It starts as a promotion, not as a build of the default branch.
    assert len(dispatcher.calls) == 2
    meta = dispatcher.calls[1]["meta"]
    assert meta["deployment_id"] == promoted.json()["id"]
    assert meta["promote_from_deployment_id"] == source["id"]
    assert meta["promote_from_environment_slug"] == "dev"


@pytest.mark.asyncio
async def test_a_waiting_promotion_that_lost_its_image_fails_instead_of_building(
    app: FastAPI,
    client: AsyncClient,
    auth: dict[str, str],
    dispatcher: FakeDispatcher,
    images: FakeImageLookup,
) -> None:
    project = await _project(client, auth, "promote-lost")
    dev = await _environment(client, auth, project["id"], "dev")
    staging = await _environment(client, auth, project["id"], "staging")
    source = await _succeeded_build(app, client, auth, project["id"], dev["id"])
    active = await _deployment(client, auth, project["id"], staging["id"])
    await client.post(
        f"/api/v1/projects/{project['id']}/environments/{staging['id']}/provision",
        json={"deployment_id": active["id"]},
        headers=auth,
    )
    promoted = await client.post(
        _promote(source["id"]), json={"environment_id": staging["id"]}, headers=auth
    )
    assert promoted.json()["held_for_deployment_id"] == active["id"]
    await _edit_row(app, promoted.json()["id"], provision_replay_json="{not json")

    finished = await client.post(f"/api/v1/deployments/{active['id']}/succeeded", headers=auth)

    assert finished.status_code == 200, finished.text
    assert len(dispatcher.calls) == 1
    lost = await client.get(f"/api/v1/deployments/{promoted.json()['id']}", headers=auth)
    assert lost.json()["status"] == "failed"


@pytest.mark.asyncio
async def test_promote_is_refused_while_the_target_is_swapping_containers(
    app: FastAPI,
    client: AsyncClient,
    auth: dict[str, str],
    dispatcher: FakeDispatcher,
    images: FakeImageLookup,
) -> None:
    project = await _project(client, auth, "promote-swap")
    dev = await _environment(client, auth, project["id"], "dev")
    staging = await _environment(client, auth, project["id"], "staging", overlap="override")
    source = await _succeeded_build(app, client, auth, project["id"], dev["id"])
    active = await _deployment(client, auth, project["id"], staging["id"])
    await client.post(
        f"/api/v1/projects/{project['id']}/environments/{staging['id']}/provision",
        json={"deployment_id": active["id"]},
        headers=auth,
    )
    await _edit_row(app, active["id"], swap_started_at=datetime.now(UTC))

    refused = await client.post(
        _promote(source["id"]), json={"environment_id": staging["id"]}, headers=auth
    )

    assert refused.status_code == 409, refused.text
    assert len(dispatcher.calls) == 1
    still_running = await client.get(f"/api/v1/deployments/{active['id']}", headers=auth)
    assert still_running.json()["status"] == "running"


class FakeDockerRunner:
    def __init__(self, *, image_stderr: str | None = None, tag_fails: bool = False) -> None:
        self.image_stderr = image_stderr
        self.tag_fails = tag_fails
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
        if args[:3] == ["docker", "image", "inspect"] and self.image_stderr is not None:
            return CommandResult(1, "", self.image_stderr)
        if args[:2] == ["docker", "tag"] and self.tag_fails:
            return CommandResult(1, "", "tag refused")
        return CommandResult(0, "", "")


def _provisioner(runner: FakeDockerRunner) -> DockerProvisioner:
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


P_ID = "f27ee337-a7d0-4d4d-8180-22579d6a2983"
DEV_ID = "bacbb040-41fa-4623-8e4b-02be2f7abf7b"
PROD_ID = "0c9c1d7e-5a53-4d38-9a43-4d9d3f0f6a11"
SOURCE_ID = "11111111-2222-4333-8444-555555555555"
NEW_ID = "99999999-8888-4777-8666-555555555555"


def _promote_on_worker(runner: FakeDockerRunner) -> None:
    _provisioner(runner).provision(
        project_id=P_ID,
        environment_id=PROD_ID,
        project_slug="portal",
        environment_slug="prod",
        repo_url=REPO,
        image_deployment_id=NEW_ID,
        promote_from_deployment_id=SOURCE_ID,
        promote_from_environment_id=DEV_ID,
        promote_from_environment_slug="dev",
    )


def test_worker_promotes_the_image_without_cloning_or_building() -> None:
    runner = FakeDockerRunner()

    _promote_on_worker(runner)

    assert [call[:2] for call in runner.calls] == [
        ["docker", "image"],
        ["docker", "tag"],
        ["docker", "tag"],
        ["docker", "run"],
        ["docker", "rm"],
        ["docker", "rename"],
    ]
    source_tag = "aerodeck/portal-dev:111111112222"
    assert runner.calls[0] == ["docker", "image", "inspect", source_tag]
    # Tagged for the target so it can be rolled back to and promoted again.
    assert runner.calls[1] == ["docker", "tag", source_tag, "aerodeck/portal-prod:latest"]
    assert runner.calls[2] == ["docker", "tag", source_tag, "aerodeck/portal-prod:999999998888"]
    assert runner.calls[3][-1] == "aerodeck/portal-prod:latest"


def test_worker_refuses_promote_without_a_docker_provisioner() -> None:
    tasks.use_provisioner(NoopProvisioner())
    try:
        with pytest.raises(PermanentJobError, match="Docker provisioner"):
            tasks._reject_promote_without_docker({"promote_from_deployment_id": SOURCE_ID})
        # Ordinary provisions are unchanged.
        tasks._reject_promote_without_docker({})
    finally:
        tasks.use_provisioner(NoopProvisioner())


def test_worker_fails_a_promotion_whose_image_is_gone() -> None:
    runner = FakeDockerRunner(image_stderr="Error response from daemon: No such image")

    with pytest.raises(PermanentJobError, match="no longer available"):
        _promote_on_worker(runner)

    assert [call[:2] for call in runner.calls] == [["docker", "image"]]


def test_worker_fails_a_promotion_it_cannot_tag() -> None:
    runner = FakeDockerRunner(tag_fails=True)

    with pytest.raises(Exception, match="docker tag"):
        _promote_on_worker(runner)

    assert ["docker", "run"] not in [call[:2] for call in runner.calls]


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (None, True),
        ("Error response from daemon: No such image: aerodeck/portal-dev:111111112222", False),
        ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock", None),
    ],
)
def test_image_presence_tells_missing_from_unreachable(
    stderr: str | None, expected: bool | None
) -> None:
    runner = FakeDockerRunner(image_stderr=stderr)

    present = image_present_sync(
        project_id=P_ID,
        project_slug="portal",
        environment_id=DEV_ID,
        environment_slug="dev",
        deployment_id=SOURCE_ID,
        runner=runner,
    )

    assert present is expected
    assert runner.calls == [["docker", "image", "inspect", "aerodeck/portal-dev:111111112222"]]


@pytest.mark.asyncio
async def test_image_presence_is_unknown_without_a_docker_worker() -> None:
    lookup = WorkerImageLookup(make_settings("sqlite+aiosqlite:///:memory:"))

    present = await lookup.is_present(
        RetainedImage(
            project_id=uuid.UUID(P_ID),
            project_slug="portal",
            environment_id=uuid.UUID(DEV_ID),
            environment_slug="dev",
            deployment_id=uuid.UUID(SOURCE_ID),
        )
    )

    assert present is None
