"""A draft is saved for its owner and does not queue provision (#343)."""

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.deps import get_job_dispatcher
from app.core.errors import ConflictError
from app.core.queue import create_celery_app
from app.models.deployment import Deployment
from app.repositories.jobs import CeleryJobDispatcher
from app.schemas.deployment_draft import DeploymentDraftPlan
from app.services.deployment_draft import DeploymentDraftService, mask_plan, merge_plan
from app.services.environment_vars import decrypt_env_value, encrypt_env_value
from tests.conftest import SIGNING_KEY, make_settings, obtain_token

DEPLOYMENTS = "/api/v1/deployments"
PROJECTS = "/api/v1/projects"
REPO = "https://github.com/mangatohjunior/aerodeck"


def _plan(
    step: str = "build",
    *,
    secret: str = "super-secret",
    redacted: bool = False,
    extra_env: str = "",
) -> dict:
    return {
        "step": step,
        "blueprint": {
            "version": 1,
            "name": "demo",
            "repo_url": REPO,
            "build": {"strategy": "dockerfile", "port": 8000},
        },
        "variables": [
            {"key": "SESSION_SECRET_KEY", "value": secret, "secret": True, "redacted": redacted},
            {"key": "PUBLIC_NAME", "value": "portal", "secret": False},
        ],
        "extra_env": extra_env,
    }


async def _user(client: AsyncClient, auth: dict[str, str], email: str) -> dict[str, str]:
    created = await client.post(
        "/api/v1/console-users",
        json={"email": email, "password": "correct-horse-battery-staple"},
        headers=auth,
    )
    assert created.status_code == 201, created.text
    return {**auth, "X-AeroDeck-User-Id": created.json()["id"]}


def test_redacted_write_keeps_the_stored_secret() -> None:
    stored = DeploymentDraftPlan.model_validate(_plan(extra_env="TOKEN=sekrit"))
    incoming = DeploymentDraftPlan.model_validate(_plan(secret="", redacted=True))
    merged = merge_plan(stored, incoming)
    assert merged.variables[0].value == "super-secret"
    assert merged.extra_env == "TOKEN=sekrit"
    masked = mask_plan(merged)
    assert masked.variables[0].value == ""
    assert masked.variables[0].redacted is True
    assert masked.variables[1].value == "portal"
    assert masked.extra_env == ""
    replaced = merge_plan(stored, DeploymentDraftPlan.model_validate(_plan(extra_env="OTHER=1")))
    assert replaced.extra_env == "OTHER=1"


async def test_draft_is_private_and_does_not_queue(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    owner = await _user(client, auth, "draft-owner@aerodeck.test")
    other = await _user(client, auth, "draft-other@aerodeck.test")

    created = await client.post(
        f"{DEPLOYMENTS}/draft",
        json={"repo_url": REPO, "plan": _plan(extra_env="TOKEN=sekrit")},
        headers=owner,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    draft_id = body["deployment"]["id"]
    assert body["deployment"]["status"] == "draft"
    assert body["deployment"]["provision_job_id"] is None
    assert body["deployment"]["draft_step"] == "build"
    assert body["plan"]["variables"][0]["value"] == ""
    assert body["plan"]["variables"][0]["redacted"] is True
    assert body["plan"]["extra_env"] == ""

    listed = await client.get(DEPLOYMENTS, headers=owner)
    assert listed.status_code == 200, listed.text
    assert any(item["id"] == draft_id for item in listed.json()["items"])

    hidden = await client.get(DEPLOYMENTS, headers=other)
    assert all(item["id"] != draft_id for item in hidden.json()["items"])

    denied = await client.get(f"{DEPLOYMENTS}/{draft_id}/draft", headers=other)
    assert denied.status_code == 404
    denied_row = await client.get(f"{DEPLOYMENTS}/{draft_id}", headers=other)
    assert denied_row.status_code == 404
    denied_logs = await client.get(f"{DEPLOYMENTS}/{draft_id}/logs", headers=other)
    assert denied_logs.status_code == 404
    denied_runtime = await client.get(f"{DEPLOYMENTS}/{draft_id}/runtime-logs", headers=other)
    assert denied_runtime.status_code == 404
    for action in ("succeeded", "failed", "cancel", "redeploy", "deploy-latest", "rollback"):
        denied_action = await client.post(f"{DEPLOYMENTS}/{draft_id}/{action}", headers=other)
        assert denied_action.status_code == 404, action
    denied_promote = await client.post(
        f"{DEPLOYMENTS}/{draft_id}/promote",
        json={"environment_id": str(uuid4())},
        headers=other,
    )
    assert denied_promote.status_code == 404
    own_logs = await client.get(f"{DEPLOYMENTS}/{draft_id}/logs", headers=owner)
    assert own_logs.status_code == 200

    started = await client.post(f"{DEPLOYMENTS}/{draft_id}/succeeded", headers=owner)
    assert started.status_code == 409

    discarded = await client.post(f"{DEPLOYMENTS}/{draft_id}/discard", headers=owner)
    assert discarded.status_code == 200, discarded.text
    assert discarded.json()["status"] == "cancelled"
    assert discarded.json()["provision_job_id"] is None


def test_open_maps_a_schema_mismatch_to_conflict() -> None:
    service = DeploymentDraftService.__new__(DeploymentDraftService)
    service._signing_key = SIGNING_KEY
    token = encrypt_env_value('{"step": "build"}', SIGNING_KEY)
    with pytest.raises(ConflictError, match="could not be read"):
        service.open(token)


async def _stored_plan(app: FastAPI, deployment_id: str) -> DeploymentDraftPlan:
    sessions = async_sessionmaker(app.state.engine, expire_on_commit=False)
    async with sessions() as session:
        row = await session.get(Deployment, UUID(deployment_id))
    assert row is not None and row.draft_plan
    return DeploymentDraftPlan.model_validate_json(decrypt_env_value(row.draft_plan, SIGNING_KEY))


async def test_redacted_update_keeps_the_sealed_secret(
    client: AsyncClient, auth: dict[str, str], app: FastAPI
) -> None:
    owner = await _user(client, auth, "draft-update@aerodeck.test")
    other = await _user(client, auth, "draft-update-other@aerodeck.test")
    project = await client.post(PROJECTS, json={"slug": "drafts", "name": "Drafts"}, headers=auth)
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    environment = await client.post(
        f"{PROJECTS}/{project_id}/environments",
        json={"slug": "dev", "name": "Dev", "kind": "dev"},
        headers=auth,
    )
    assert environment.status_code == 201, environment.text
    environment_id = environment.json()["id"]

    created = await client.post(
        f"{DEPLOYMENTS}/draft",
        json={"repo_url": REPO, "plan": _plan(extra_env="TOKEN=sekrit")},
        headers=owner,
    )
    assert created.status_code == 201, created.text
    draft_id = created.json()["deployment"]["id"]

    updated = await client.put(
        f"{DEPLOYMENTS}/{draft_id}/draft",
        json={
            "plan": _plan(secret="", redacted=True) | {"variables": [
                {"key": "SESSION_SECRET_KEY", "value": "", "secret": True, "redacted": True},
                {"key": "PUBLIC_NAME", "value": "renamed", "secret": False},
            ]},
            "project_id": project_id,
            "environment_id": environment_id,
        },
        headers=owner,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["plan"]["variables"][1]["value"] == "renamed"
    assert updated.json()["plan"]["variables"][0]["value"] == ""
    assert updated.json()["plan"]["extra_env"] == ""
    sealed = await _stored_plan(app, draft_id)
    assert sealed.variables[0].value == "super-secret"
    assert sealed.variables[1].value == "renamed"
    assert sealed.extra_env == "TOKEN=sekrit"

    hidden = await client.get(f"{PROJECTS}/{project_id}/deployments", headers=other)
    assert hidden.status_code == 200, hidden.text
    assert all(item["id"] != draft_id for item in hidden.json()["items"])
    visible = await client.get(f"{PROJECTS}/{project_id}/deployments", headers=owner)
    assert any(item["id"] == draft_id for item in visible.json()["items"])


def _eager_dispatcher() -> CeleryJobDispatcher:
    worker = create_celery_app(make_settings(database_url=None, redis_url="memory://"))
    worker.conf.update(
        result_backend="cache+memory://",
        task_always_eager=True,
        task_eager_propagates=False,
        task_store_eager_result=True,
        result_extended=True,
    )
    return CeleryJobDispatcher(worker)


async def test_launch_queues_provision_and_leaves_draft(app: FastAPI) -> None:
    app.dependency_overrides[get_job_dispatcher] = _eager_dispatcher
    try:
        await _launch_and_assert_queued(app)
    finally:
        app.dependency_overrides.pop(get_job_dispatcher, None)


async def _launch_and_assert_queued(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await obtain_token(client)
        headers = {"Authorization": f"Bearer {token}"}
        owner = await _user(client, headers, "draft-launch@aerodeck.test")
        project = await client.post(PROJECTS, json={"slug": "launch", "name": "Launch"}, headers=headers)
        assert project.status_code == 201, project.text
        project_id = project.json()["id"]
        environment = await client.post(
            f"{PROJECTS}/{project_id}/environments",
            json={"slug": "dev", "name": "Dev", "kind": "dev"},
            headers=headers,
        )
        assert environment.status_code == 201, environment.text
        created = await client.post(
            f"{DEPLOYMENTS}/draft", json={"repo_url": REPO, "plan": _plan()}, headers=owner
        )
        assert created.status_code == 201, created.text
        draft_id = created.json()["deployment"]["id"]
        launched = await client.post(
            f"{DEPLOYMENTS}/{draft_id}/launch",
            json={
                "plan": _plan(step="launch", secret="", redacted=True),
                "project_id": project_id,
                "environment_id": environment.json()["id"],
            },
            headers=owner,
        )
        assert launched.status_code == 202, launched.text
        body = launched.json()
        assert body["status"] != "draft"
        assert body["draft_step"] is None
        stored = await client.get(f"{DEPLOYMENTS}/{draft_id}", headers=owner)
        assert stored.status_code == 200, stored.text
        assert stored.json()["status"] != "draft"
        assert stored.json()["draft_step"] is None
        assert stored.json()["provision_job_id"]


class _BrokenDispatcher:
    def dispatch(self, kind: object, *, project_id: str, environment_id: str, **meta: str) -> str:
        raise ConnectionError("broker down")

    def state(self, job_id: str) -> None:  # pragma: no cover
        raise AssertionError("state should not be called")

    def revoke(self, job_id: str) -> None:  # pragma: no cover
        raise AssertionError("revoke should not be called")


async def test_launch_broker_failure_leaves_the_draft(app: FastAPI) -> None:
    app.dependency_overrides[get_job_dispatcher] = lambda: _BrokenDispatcher()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = await obtain_token(client)
            headers = {"Authorization": f"Bearer {token}"}
            owner = await _user(client, headers, "draft-broker@aerodeck.test")
            project = await client.post(
                PROJECTS, json={"slug": "broker", "name": "Broker"}, headers=headers
            )
            assert project.status_code == 201, project.text
            environment = await client.post(
                f"{PROJECTS}/{project.json()['id']}/environments",
                json={"slug": "dev", "name": "Dev", "kind": "dev"},
                headers=headers,
            )
            assert environment.status_code == 201, environment.text
            created = await client.post(
                f"{DEPLOYMENTS}/draft", json={"repo_url": REPO, "plan": _plan()}, headers=owner
            )
            assert created.status_code == 201, created.text
            draft_id = created.json()["deployment"]["id"]
            launched = await client.post(
                f"{DEPLOYMENTS}/{draft_id}/launch",
                json={
                    "plan": _plan(step="launch"),
                    "project_id": project.json()["id"],
                    "environment_id": environment.json()["id"],
                },
                headers=owner,
            )
            assert launched.status_code == 503, launched.text
            assert launched.json()["code"] == "queue_unavailable"
            stored = await client.get(f"{DEPLOYMENTS}/{draft_id}", headers=owner)
            assert stored.status_code == 200, stored.text
            assert stored.json()["status"] == "draft"
            assert stored.json()["draft_step"] == "build"
            sealed = await _stored_plan(app, draft_id)
            assert sealed.variables[0].value == "super-secret"
            again = await client.get(f"{DEPLOYMENTS}/{draft_id}/draft", headers=owner)
            assert again.status_code == 200, again.text
            discarded = await client.post(f"{DEPLOYMENTS}/{draft_id}/discard", headers=owner)
            assert discarded.status_code == 200, discarded.text
            assert discarded.json()["status"] == "cancelled"
            assert discarded.json()["draft_step"] is None
    finally:
        app.dependency_overrides.pop(get_job_dispatcher, None)


async def test_draft_requires_a_known_user(client: AsyncClient, auth: dict[str, str]) -> None:
    missing = await client.post(
        f"{DEPLOYMENTS}/draft",
        json={"repo_url": REPO, "plan": _plan()},
        headers={**auth, "X-AeroDeck-User-Id": str(uuid4())},
    )
    assert missing.status_code == 422


async def test_variables_step_is_a_saved_draft_page(client: AsyncClient, auth: dict[str, str]) -> None:
    owner = await _user(client, auth, "draft-variables@aerodeck.test")
    created = await client.post(
        f"{DEPLOYMENTS}/draft", json={"repo_url": REPO, "plan": _plan()}, headers=owner
    )
    assert created.status_code == 201, created.text
    draft_id = created.json()["deployment"]["id"]
    updated = await client.put(
        f"{DEPLOYMENTS}/{draft_id}/draft", json={"plan": _plan(step="variables")}, headers=owner
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["deployment"]["draft_step"] == "variables"
    assert updated.json()["plan"]["step"] == "variables"
