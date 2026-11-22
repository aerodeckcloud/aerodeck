"""The worker starts a waiting deployment when the active provision ends (#271)."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.db import create_engine
from app.models import Base
from app.models.deployment import Deployment, DeploymentStatus
from app.models.environment_var import EnvironmentVariable
from app.models.project import Environment, EnvironmentKind, Project
from app.repositories.jobs import JobState
from app.services.environment_vars import encrypt_env_value
from app.services.provision_replay import ProvisionReplay
from app.services.provisioning import JobKind
from app.tasks import waiting_release
from tests.conftest import SIGNING_KEY, make_settings

REPO = "https://github.com/mangatohjunior/dev-portal"
SHA = "abcdefabcdefabcdefabcdefabcdefabcdefabcd"


class RecordingDispatcher:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail = fail

    def dispatch(self, kind: JobKind, *, project_id: str, environment_id: str, **meta: str) -> str:
        if self._fail:
            raise ConnectionError("broker is gone")
        job_id = str(uuid.uuid4())
        self.calls.append({"job_id": job_id, "kind": kind, "environment_id": environment_id, "meta": meta})
        return job_id

    def state(self, job_id: str) -> JobState:
        return JobState(job_id=job_id, status="queued", result=None, error=None)

    def revoke(self, job_id: str) -> None:
        _ = job_id


def _row(project_id: uuid.UUID, environment_id: uuid.UUID, **fields: Any) -> Deployment:
    return Deployment(
        repo_url=REPO,
        owner="mangatohjunior",
        repo="dev-portal",
        project_id=project_id,
        environment_id=environment_id,
        **fields,
    )


def _seed(database_url: str, *, active: dict[str, Any]) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """One environment with an active deploy, an older waiter, and the newest waiter."""

    async def _run() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        engine = create_engine(database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        started = datetime.now(UTC) - timedelta(minutes=5)
        async with sessions() as session:
            project = Project(slug="worker-release", name="Worker release")
            session.add(project)
            await session.flush()
            environment = Environment(
                project_id=project.id, slug="dev", name="Dev", kind=EnvironmentKind.dev, region="ams3"
            )
            session.add(environment)
            await session.flush()
            session.add(
                EnvironmentVariable(
                    project_id=project.id,
                    environment_id=environment.id,
                    key="API_TOKEN",
                    encrypted_value=encrypt_env_value("stored-value", SIGNING_KEY),
                    is_secret=True,
                )
            )
            finished = _row(project.id, environment.id, started_at=started, **active)
            session.add(finished)
            await session.flush()
            older = _row(
                project.id,
                environment.id,
                status=DeploymentStatus.queued,
                started_at=started + timedelta(minutes=1),
                held_for_deployment_id=finished.id,
            )
            newest = _row(
                project.id,
                environment.id,
                status=DeploymentStatus.queued,
                started_at=started + timedelta(minutes=2),
                held_for_deployment_id=finished.id,
                provision_replay_json=ProvisionReplay(git_ref=SHA, addons=["redis"]).to_json(),
            )
            session.add_all([older, newest])
            await session.commit()
            ids = (finished.id, older.id, newest.id)
        await engine.dispose()
        return ids

    return asyncio.run(_run())


def _read(database_url: str, deployment_id: uuid.UUID) -> Deployment:
    async def _run() -> Deployment:
        engine = create_engine(database_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            stored = await session.get(Deployment, deployment_id)
            assert stored is not None
        await engine.dispose()
        return stored

    return asyncio.run(_run())


@pytest.fixture
def worker_database(tmp_path, monkeypatch: pytest.MonkeyPatch) -> str:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'worker-release.db').as_posix()}"
    monkeypatch.setattr(waiting_release, "get_settings", lambda: make_settings(database_url))
    return database_url


def test_worker_starts_the_newest_waiter_after_a_deploy_nobody_watched(
    worker_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful provision leaves the row running with finished_at set."""
    finished_id, older_id, newest_id = _seed(
        worker_database,
        active={"status": DeploymentStatus.running, "finished_at": datetime.now(UTC)},
    )
    dispatcher = RecordingDispatcher()
    monkeypatch.setattr(waiting_release, "_dispatcher", lambda _settings: dispatcher)

    waiting_release.try_release_waiting_sync(finished_id)

    assert len(dispatcher.calls) == 1
    meta = dispatcher.calls[0]["meta"]
    assert meta["deployment_id"] == str(newest_id)
    assert meta["git_ref"] == SHA
    assert json.loads(meta["addons_json"]) == ["redis"]
    assert json.loads(meta["env_vars_json"]) == {"API_TOKEN": "stored-value"}
    assert meta["project_slug"] == "worker-release"
    assert meta["environment_slug"] == "dev"

    newest = _read(worker_database, newest_id)
    assert newest.status is DeploymentStatus.running
    assert newest.held_for_deployment_id is None
    assert str(newest.provision_job_id) == dispatcher.calls[0]["job_id"]
    assert _read(worker_database, older_id).status is DeploymentStatus.cancelled

    # A second call (the console reporting Done later) finds nothing to start.
    waiting_release.try_release_waiting_sync(finished_id)
    assert len(dispatcher.calls) == 1


def test_worker_starts_the_waiter_after_a_failed_deploy(
    worker_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    finished_id, _older_id, newest_id = _seed(
        worker_database,
        active={"status": DeploymentStatus.failed, "finished_at": datetime.now(UTC)},
    )
    dispatcher = RecordingDispatcher()
    monkeypatch.setattr(waiting_release, "_dispatcher", lambda _settings: dispatcher)

    waiting_release.try_release_waiting_sync(finished_id)

    assert [call["meta"]["deployment_id"] for call in dispatcher.calls] == [str(newest_id)]


def test_worker_leaves_the_waiter_alone_while_the_active_deploy_runs(
    worker_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    finished_id, older_id, newest_id = _seed(
        worker_database, active={"status": DeploymentStatus.running}
    )
    dispatcher = RecordingDispatcher()
    monkeypatch.setattr(waiting_release, "_dispatcher", lambda _settings: dispatcher)

    waiting_release.try_release_waiting_sync(finished_id)

    assert dispatcher.calls == []
    assert _read(worker_database, newest_id).held_for_deployment_id == finished_id
    assert _read(worker_database, older_id).status is DeploymentStatus.queued


def test_worker_marks_the_waiter_failed_when_the_broker_refuses_it(
    worker_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    finished_id, _older_id, newest_id = _seed(
        worker_database,
        active={"status": DeploymentStatus.failed, "finished_at": datetime.now(UTC)},
    )
    monkeypatch.setattr(waiting_release, "_dispatcher", lambda _settings: RecordingDispatcher(fail=True))

    waiting_release.try_release_waiting_sync(finished_id)

    newest = _read(worker_database, newest_id)
    assert newest.status is DeploymentStatus.failed
    assert newest.held_for_deployment_id is None


def test_worker_does_not_promote_a_waiter_without_a_broker(worker_database: str) -> None:
    finished_id, _older_id, newest_id = _seed(
        worker_database,
        active={"status": DeploymentStatus.failed, "finished_at": datetime.now(UTC)},
    )

    waiting_release.try_release_waiting_sync(finished_id)

    newest = _read(worker_database, newest_id)
    assert newest.status is DeploymentStatus.queued
    assert newest.held_for_deployment_id == finished_id
