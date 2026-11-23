"""Encrypted release-variable snapshot and rollback restore (#273)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.deps import get_job_dispatcher
from app.models.deployment import Deployment
from app.models.environment_var import EnvironmentVariable
from app.repositories.jobs import JobState
from app.services.environment_vars import decrypt_env_value, encrypt_env_value
from app.services.provisioning import JobKind
from tests.conftest import SIGNING_KEY


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


async def _project_env(client: AsyncClient, auth: dict[str, str], slug: str) -> tuple[dict, dict]:
    project = (
        await client.post("/api/v1/projects", json={"slug": slug, "name": slug}, headers=auth)
    ).json()
    env = (
        await client.post(
            f"/api/v1/projects/{project['id']}/environments",
            json={"slug": "dev", "name": "Dev", "kind": "dev", "region": "ams3"},
            headers=auth,
        )
    ).json()
    return project, env


@pytest.mark.asyncio
async def test_succeed_stores_encrypted_env_snapshot_and_exposes_names_only(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    project, env = await _project_env(client, auth, "snap-capture")
    base = f"/api/v1/projects/{project['id']}/environments/{env['id']}/env-vars"
    put = await client.put(
        base,
        json={
            "items": [
                {"key": "API_TOKEN", "value": "super-secret-token", "is_secret": True},
                {"key": "NODE_ENV", "value": "production", "is_secret": False},
            ]
        },
        headers=auth,
    )
    assert put.status_code == 200

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
    succeeded = await client.post(f"/api/v1/deployments/{dep['id']}/succeeded", headers=auth)
    assert succeeded.status_code == 200
    body = succeeded.json()
    assert body["snapshot_variable_names"] == ["API_TOKEN", "NODE_ENV"]
    assert "super-secret-token" not in succeeded.text
    assert "production" not in succeeded.text
    assert "env_vars_snapshot" not in body

    sessions = async_sessionmaker(app.state.engine, expire_on_commit=False)
    async with sessions() as session:
        row = await session.get(Deployment, uuid.UUID(dep["id"]))
        assert row is not None
        assert row.env_vars_snapshot is not None
        assert row.snapshot_names_json == '["API_TOKEN", "NODE_ENV"]'
        assert "super-secret-token" not in row.env_vars_snapshot
        restored = json.loads(decrypt_env_value(row.env_vars_snapshot, SIGNING_KEY))
        assert restored == {
            "API_TOKEN": {"value": "super-secret-token", "is_secret": True},
            "NODE_ENV": {"value": "production", "is_secret": False},
        }

    # Sticky re-POST must not rewrite the snapshot after variables change.
    await client.put(
        base,
        json={"items": [{"key": "API_TOKEN", "value": "changed-after-success", "is_secret": True}]},
        headers=auth,
    )
    again = await client.post(f"/api/v1/deployments/{dep['id']}/succeeded", headers=auth)
    assert again.status_code == 200
    assert again.json()["snapshot_variable_names"] == ["API_TOKEN", "NODE_ENV"]
    async with sessions() as session:
        row = await session.get(Deployment, uuid.UUID(dep["id"]))
        assert row is not None
        restored = json.loads(decrypt_env_value(row.env_vars_snapshot or "", SIGNING_KEY))
        assert restored["API_TOKEN"]["value"] == "super-secret-token"


@pytest.mark.asyncio
async def test_rollback_restores_snapshot_then_enqueues_image(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    dispatcher = FakeDispatcher()
    app.dependency_overrides[get_job_dispatcher] = lambda: dispatcher
    try:
        project, env = await _project_env(client, auth, "snap-restore")
        base = f"/api/v1/projects/{project['id']}/environments/{env['id']}/env-vars"
        await client.put(
            base,
            json={
                "items": [
                    {"key": "KEEP", "value": "original", "is_secret": True},
                    {"key": "GONE", "value": "will-delete", "is_secret": False},
                ]
            },
            headers=auth,
        )
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
        assert (await client.post(f"/api/v1/deployments/{dep['id']}/succeeded", headers=auth)).status_code == 200

        # Drift the environment after the release shipped.
        await client.delete(f"{base}/GONE", headers=auth)
        await client.put(
            base,
            json={
                "items": [
                    {"key": "KEEP", "value": "drifted", "is_secret": True},
                    {"key": "NEW", "value": "added-later", "is_secret": True},
                ]
            },
            headers=auth,
        )

        rolled = await client.post(
            f"/api/v1/deployments/{dep['id']}/rollback",
            json={"restore_variables": True},
            headers=auth,
        )
        assert rolled.status_code == 201, rolled.text
        body = rolled.json()
        assert body["variables_restored"] is True
        assert body["trigger"] == "rollback"
        assert "original" not in rolled.text
        assert "drifted" not in rolled.text
        assert dispatcher.calls[-1]["meta"]["rollback_from_deployment_id"] == dep["id"]
        assert json.loads(dispatcher.calls[-1]["meta"]["env_vars_json"]) == {
            "KEEP": "original",
            "GONE": "will-delete",
        }

        listed = (await client.get(base, headers=auth)).json()
        assert [(row["key"], row["is_secret"]) for row in listed] == [
            ("GONE", False),
            ("KEEP", True),
        ]

        sessions = async_sessionmaker(app.state.engine, expire_on_commit=False)
        async with sessions() as session:
            rows = (
                await session.scalars(
                    select(EnvironmentVariable).where(
                        EnvironmentVariable.environment_id == uuid.UUID(env["id"])
                    )
                )
            ).all()
            values = {
                row.key: decrypt_env_value(row.encrypted_value, SIGNING_KEY) for row in rows
            }
            assert values == {"KEEP": "original", "GONE": "will-delete"}
    finally:
        app.dependency_overrides.pop(get_job_dispatcher, None)


@pytest.mark.asyncio
async def test_rollback_without_snapshot_leaves_variables(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    dispatcher = FakeDispatcher()
    app.dependency_overrides[get_job_dispatcher] = lambda: dispatcher
    try:
        project, env = await _project_env(client, auth, "snap-legacy")
        base = f"/api/v1/projects/{project['id']}/environments/{env['id']}/env-vars"
        await client.put(
            base,
            json={"items": [{"key": "STAY", "value": "current-value", "is_secret": True}]},
            headers=auth,
        )
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
        assert (await client.post(f"/api/v1/deployments/{dep['id']}/succeeded", headers=auth)).status_code == 200

        # Simulate a pre-story succeeded row: clear both snapshot columns.
        sessions = async_sessionmaker(app.state.engine, expire_on_commit=False)
        async with sessions() as session:
            row = await session.get(Deployment, uuid.UUID(dep["id"]))
            assert row is not None
            row.env_vars_snapshot = None
            row.snapshot_names_json = None
            await session.commit()

        got = await client.get(f"/api/v1/deployments/{dep['id']}", headers=auth)
        assert got.status_code == 200
        assert got.json()["snapshot_variable_names"] is None

        rolled = await client.post(f"/api/v1/deployments/{dep['id']}/rollback", headers=auth)
        assert rolled.status_code == 201
        assert rolled.json()["variables_restored"] is False
        assert dispatcher.calls[-1]["meta"]["rollback_from_deployment_id"] == dep["id"]

        listed = (await client.get(base, headers=auth)).json()
        assert [row["key"] for row in listed] == ["STAY"]
        async with sessions() as session:
            row = (
                await session.scalars(
                    select(EnvironmentVariable).where(
                        EnvironmentVariable.environment_id == uuid.UUID(env["id"]),
                        EnvironmentVariable.key == "STAY",
                    )
                )
            ).one()
            assert decrypt_env_value(row.encrypted_value, SIGNING_KEY) == "current-value"
    finally:
        app.dependency_overrides.pop(get_job_dispatcher, None)


@pytest.mark.asyncio
async def test_rollback_with_garbage_snapshot_continues_image_only(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    """Corrupt ciphertext must not 500; leave vars alone and still enqueue (#273)."""
    dispatcher = FakeDispatcher()
    app.dependency_overrides[get_job_dispatcher] = lambda: dispatcher
    try:
        project, env = await _project_env(client, auth, "snap-garbage")
        base = f"/api/v1/projects/{project['id']}/environments/{env['id']}/env-vars"
        await client.put(
            base,
            json={"items": [{"key": "STAY", "value": "keep-me", "is_secret": True}]},
            headers=auth,
        )
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
        assert (
            await client.post(f"/api/v1/deployments/{dep['id']}/succeeded", headers=auth)
        ).status_code == 200

        sessions = async_sessionmaker(app.state.engine, expire_on_commit=False)
        async with sessions() as session:
            row = await session.get(Deployment, uuid.UUID(dep["id"]))
            assert row is not None
            row.env_vars_snapshot = "not-valid-fernet-ciphertext"
            await session.commit()

        got = await client.get(f"/api/v1/deployments/{dep['id']}", headers=auth)
        assert got.status_code == 200
        # Names stay readable from plaintext; restore still no-ops on bad ciphertext.
        assert got.json()["snapshot_variable_names"] == ["STAY"]

        rolled = await client.post(
            f"/api/v1/deployments/{dep['id']}/rollback",
            json={"restore_variables": True},
            headers=auth,
        )
        assert rolled.status_code == 201, rolled.text
        assert rolled.json()["variables_restored"] is False
        assert dispatcher.calls[-1]["meta"]["rollback_from_deployment_id"] == dep["id"]

        listed = (await client.get(base, headers=auth)).json()
        assert [row["key"] for row in listed] == ["STAY"]
        async with sessions() as session:
            row = (
                await session.scalars(
                    select(EnvironmentVariable).where(
                        EnvironmentVariable.environment_id == uuid.UUID(env["id"]),
                        EnvironmentVariable.key == "STAY",
                    )
                )
            ).one()
            assert decrypt_env_value(row.encrypted_value, SIGNING_KEY) == "keep-me"
    finally:
        app.dependency_overrides.pop(get_job_dispatcher, None)


@pytest.mark.asyncio
async def test_rollback_dispatch_failure_does_not_mutate_environment(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    """Dispatch failure must never write the snapshot; another session still sees drift (#273)."""

    class BrokenDispatcher:
        def dispatch(self, kind: JobKind, *, project_id: str, environment_id: str, **meta: str) -> str:
            raise ConnectionError("broker down")

        def state(self, job_id: str) -> JobState:  # pragma: no cover
            raise AssertionError("state should not be called")

    app.dependency_overrides[get_job_dispatcher] = lambda: BrokenDispatcher()
    try:
        project, env = await _project_env(client, auth, "snap-queue-fail")
        base = f"/api/v1/projects/{project['id']}/environments/{env['id']}/env-vars"
        await client.put(
            base,
            json={"items": [{"key": "KEEP", "value": "from-snapshot", "is_secret": True}]},
            headers=auth,
        )
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
        assert (
            await client.post(f"/api/v1/deployments/{dep['id']}/succeeded", headers=auth)
        ).status_code == 200
        await client.put(
            base,
            json={
                "items": [
                    {"key": "KEEP", "value": "drifted-value", "is_secret": True},
                    {"key": "EXTRA", "value": "only-after", "is_secret": False},
                ]
            },
            headers=auth,
        )

        with pytest.raises(ConnectionError, match="broker down"):
            await client.post(
                f"/api/v1/deployments/{dep['id']}/rollback",
                json={"restore_variables": True},
                headers=auth,
            )

        # A separate session must still see the drifted map — restore never committed.
        sessions = async_sessionmaker(app.state.engine, expire_on_commit=False)
        async with sessions() as session:
            rows = (
                await session.scalars(
                    select(EnvironmentVariable).where(
                        EnvironmentVariable.environment_id == uuid.UUID(env["id"])
                    )
                )
            ).all()
            values = {
                row.key: decrypt_env_value(row.encrypted_value, SIGNING_KEY) for row in rows
            }
            secrets = {row.key: row.is_secret for row in rows}
            assert values == {"KEEP": "drifted-value", "EXTRA": "only-after"}
            assert secrets == {"KEEP": True, "EXTRA": False}
            assert "from-snapshot" not in values.values()

        listed = (await client.get(base, headers=auth)).json()
        assert [(row["key"], row["is_secret"]) for row in listed] == [
            ("EXTRA", False),
            ("KEEP", True),
        ]
    finally:
        app.dependency_overrides.pop(get_job_dispatcher, None)


@pytest.mark.asyncio
async def test_waiting_rollback_cancel_leaves_variables_unchanged(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    """A rollback that only waits must not restore until it is dispatched (#273)."""
    dispatcher = FakeDispatcher()
    app.dependency_overrides[get_job_dispatcher] = lambda: dispatcher
    try:
        project, env = await _project_env(client, auth, "snap-wait")
        base = f"/api/v1/projects/{project['id']}/environments/{env['id']}/env-vars"
        await client.put(
            base,
            json={"items": [{"key": "KEEP", "value": "from-snapshot", "is_secret": False}]},
            headers=auth,
        )
        source = (
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
        assert (
            await client.post(f"/api/v1/deployments/{source['id']}/succeeded", headers=auth)
        ).status_code == 200
        await client.put(
            base,
            json={"items": [{"key": "KEEP", "value": "drifted-value", "is_secret": False}]},
            headers=auth,
        )

        active = (
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
        started = await client.post(
            f"/api/v1/projects/{project['id']}/environments/{env['id']}/provision",
            json={"deployment_id": active["id"]},
            headers=auth,
        )
        assert started.status_code == 202, started.text

        rolled = await client.post(f"/api/v1/deployments/{source['id']}/rollback", headers=auth)
        assert rolled.status_code == 201, rolled.text
        body = rolled.json()
        assert body["variables_restored"] is False
        assert body["held_for_deployment_id"] == active["id"]

        listed = (await client.get(base, headers=auth)).json()
        assert [(row["key"], row["is_secret"]) for row in listed] == [("KEEP", False)]

        cancelled = await client.post(f"/api/v1/deployments/{body['id']}/cancel", headers=auth)
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"

        sessions = async_sessionmaker(app.state.engine, expire_on_commit=False)
        async with sessions() as session:
            row = (
                await session.scalars(
                    select(EnvironmentVariable).where(
                        EnvironmentVariable.environment_id == uuid.UUID(env["id"]),
                        EnvironmentVariable.key == "KEEP",
                    )
                )
            ).one()
            assert decrypt_env_value(row.encrypted_value, SIGNING_KEY) == "drifted-value"
            assert row.is_secret is False
    finally:
        app.dependency_overrides.pop(get_job_dispatcher, None)


@pytest.mark.asyncio
async def test_rollback_can_skip_variable_restore(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    dispatcher = FakeDispatcher()
    app.dependency_overrides[get_job_dispatcher] = lambda: dispatcher
    try:
        project, env = await _project_env(client, auth, "snap-skip")
        base = f"/api/v1/projects/{project['id']}/environments/{env['id']}/env-vars"
        await client.put(
            base,
            json={"items": [{"key": "A", "value": "snap", "is_secret": True}]},
            headers=auth,
        )
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
        assert (await client.post(f"/api/v1/deployments/{dep['id']}/succeeded", headers=auth)).status_code == 200
        await client.put(
            base,
            json={"items": [{"key": "A", "value": "drifted", "is_secret": True}]},
            headers=auth,
        )

        rolled = await client.post(
            f"/api/v1/deployments/{dep['id']}/rollback",
            json={"restore_variables": False},
            headers=auth,
        )
        assert rolled.status_code == 201
        assert rolled.json()["variables_restored"] is False

        sessions = async_sessionmaker(app.state.engine, expire_on_commit=False)
        async with sessions() as session:
            row = (
                await session.scalars(
                    select(EnvironmentVariable).where(
                        EnvironmentVariable.environment_id == uuid.UUID(env["id"]),
                        EnvironmentVariable.key == "A",
                    )
                )
            ).one()
            assert decrypt_env_value(row.encrypted_value, SIGNING_KEY) == "drifted"
    finally:
        app.dependency_overrides.pop(get_job_dispatcher, None)


def test_snapshot_uses_same_fernet_helper_as_env_vars() -> None:
    sealed = encrypt_env_value(json.dumps({"X": "1"}), SIGNING_KEY)
    assert json.loads(decrypt_env_value(sealed, SIGNING_KEY)) == {"X": "1"}
