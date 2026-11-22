"""Unit and API tests for persistent encrypted environment variables (#248)."""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.environment_var import EnvironmentVariable
from app.repositories.jobs import JobState
from app.services.environment_vars import decrypt_env_value, encrypt_env_value
from app.services.jobs import DeploymentEnqueueHooks, JobService
from app.services.provisioning import JobKind


def test_encrypt_and_decrypt_env_value_round_trips() -> None:
    signing_key = "k" * 32
    secret = "glpat-super-secret-token-123"
    ciphertext = encrypt_env_value(secret, signing_key)
    assert ciphertext != secret
    assert secret not in ciphertext
    assert decrypt_env_value(ciphertext, signing_key) == secret


@pytest.mark.asyncio
async def test_env_vars_api_encrypts_at_rest_and_masks_responses(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    project = (await client.post("/api/v1/projects", json={"slug": "env-demo", "name": "Env Demo"}, headers=auth)).json()
    env = (
        await client.post(
            f"/api/v1/projects/{project['id']}/environments",
            json={"slug": "dev", "name": "Dev", "kind": "dev", "region": "ams3"},
            headers=auth,
        )
    ).json()
    base = f"/api/v1/projects/{project['id']}/environments/{env['id']}/env-vars"

    # Upsert two variables
    put_res = await client.put(
        base,
        json={
            "items": [
                {"key": "GITLAB_TOKEN", "value": "glpat-secret-value", "is_secret": True},
                {"key": "NODE_ENV", "value": "production", "is_secret": False},
            ]
        },
        headers=auth,
    )
    assert put_res.status_code == 200
    body = put_res.json()
    assert [row["key"] for row in body] == ["GITLAB_TOKEN", "NODE_ENV"]
    # Plaintext values are never returned in API responses.
    assert "glpat-secret-value" not in put_res.text
    assert "production" not in put_res.text

    # Verify DB stores ciphertext, not plaintext
    async with SessionLocal() as session:
        rows = (
            await session.scalars(
                select(EnvironmentVariable).where(
                    EnvironmentVariable.environment_id == uuid.UUID(env["id"])
                )
            )
        ).all()
        assert len(rows) == 2
        for row in rows:
            assert row.encrypted_value != "glpat-secret-value"
            assert row.encrypted_value != "production"

    # List returns masked keys
    list_res = await client.get(base, headers=auth)
    assert list_res.status_code == 200
    assert [row["key"] for row in list_res.json()] == ["GITLAB_TOKEN", "NODE_ENV"]

    # Delete one key
    del_res = await client.delete(f"{base}/NODE_ENV", headers=auth)
    assert del_res.status_code == 204
    after_del = (await client.get(base, headers=auth)).json()
    assert [row["key"] for row in after_del] == ["GITLAB_TOKEN"]

    # Deleting missing key returns 404
    missing = await client.delete(f"{base}/NO_SUCH_KEY", headers=auth)
    assert missing.status_code == 404

    # Clone encrypted vars to a second environment without exposing plaintext (#255)
    staging = (
        await client.post(
            f"/api/v1/projects/{project['id']}/environments",
            json={"slug": "staging", "name": "Staging", "kind": "staging", "region": "ams3"},
            headers=auth,
        )
    ).json()
    staging_base = f"/api/v1/projects/{project['id']}/environments/{staging['id']}/env-vars"

    # Pre-seed a conflicting key in staging
    await client.put(
        staging_base,
        json={"items": [{"key": "GITLAB_TOKEN", "value": "staging-token", "is_secret": True}]},
        headers=auth,
    )
    # Add a second key in dev
    await client.put(
        base,
        json={"items": [{"key": "API_BASE", "value": "https://api.example.com", "is_secret": False}]},
        headers=auth,
    )

    # Clone without overwrite skips GITLAB_TOKEN and copies API_BASE
    clone_res = await client.post(
        f"{staging_base}/clone",
        json={"source_environment_id": env["id"], "overwrite": False},
        headers=auth,
    )
    assert clone_res.status_code == 200
    assert clone_res.json()["copied"] == 1
    assert clone_res.json()["skipped"] == 1
    assert "glpat-secret-value" not in clone_res.text

    # Clone with overwrite copies both
    overwrite_res = await client.post(
        f"{staging_base}/clone",
        json={"source_environment_id": env["id"], "overwrite": True},
        headers=auth,
    )
    assert overwrite_res.status_code == 200
    assert overwrite_res.json()["copied"] == 2
    assert overwrite_res.json()["skipped"] == 0

    # Cloning from self -> 422
    self_clone = await client.post(
        f"{staging_base}/clone",
        json={"source_environment_id": staging["id"], "overwrite": False},
        headers=auth,
    )
    assert self_clone.status_code == 422


@pytest.mark.asyncio
async def test_job_service_merges_persisted_env_vars_with_deploy_overrides() -> None:
    captured_meta: list[dict[str, str]] = []

    class FakeDispatcher:
        def dispatch(self, kind: JobKind, *, project_id: str, environment_id: str, **meta: str) -> str:
            _ = (kind, project_id, environment_id)
            captured_meta.append(meta)
            return str(uuid.uuid4())

        def state(self, job_id: str) -> JobState:
            return JobState(job_id=job_id, status="queued")

    class FakeEnv:
        def __init__(self, project_id: uuid.UUID, environment_id: uuid.UUID) -> None:
            self.id = environment_id
            self.project_id = project_id
            self.slug = "dev"
            self.region = "ams3"
            self.kind = type("K", (), {"value": "dev"})()
            self.project = type("P", (), {"slug": "portal"})()

    class FakeEnvService:
        async def get(self, project_id: uuid.UUID, environment_id: uuid.UUID) -> FakeEnv:
            return FakeEnv(project_id, environment_id)

    class FakeDeployments:
        async def get(self, deployment_id: uuid.UUID) -> object:
            raise AssertionError(f"unexpected lookup {deployment_id}")

    class FakeVarLoader:
        async def load_decrypted_map(self, project_id: uuid.UUID, environment_id: uuid.UUID) -> dict[str, str]:
            _ = (project_id, environment_id)
            return {"GITLAB_TOKEN": "persisted-token", "KEYCLOAK_SECRET": "persisted-secret"}

    async def _noop_fail(_d: uuid.UUID) -> None:
        return None

    async def _noop_set(_d: uuid.UUID, _j: uuid.UUID) -> None:
        return None

    service = JobService(
        FakeDispatcher(),
        FakeEnvService(),  # type: ignore[arg-type]
        FakeDeployments(),  # type: ignore[arg-type]
        redis_url=None,
        deployment_hooks=DeploymentEnqueueHooks(mark_failed=_noop_fail, set_provision_job_id=_noop_set),
        env_var_loader=FakeVarLoader(),
    )

    await service.enqueue(
        JobKind.provision,
        uuid.uuid4(),
        uuid.uuid4(),
        env_vars={"GITLAB_TOKEN": "override-token", "EXTRA": "1"},
    )
    assert len(captured_meta) == 1
    merged = json.loads(captured_meta[0]["env_vars_json"])
    assert merged == {
        "GITLAB_TOKEN": "override-token",
        "KEYCLOAK_SECRET": "persisted-secret",
        "EXTRA": "1",
    }
