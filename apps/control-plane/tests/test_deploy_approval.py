"""Pause automatic redeploys and hold production deploys for an owner (#276)."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.deps import get_job_dispatcher
from app.repositories.deployments import DeploymentRepository
from app.repositories.jobs import JobState
from app.services.provisioning import JobKind

REPO = "https://github.com/acme/payments"


class FakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

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
        return JobState(job_id=job_id, status="queued")

    def revoke(self, job_id: str) -> None:
        return None


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


async def _user(client: AsyncClient, auth: dict[str, str], email: str) -> dict[str, str]:
    created = await client.post(
        "/api/v1/console-users",
        json={"email": email, "password": "correct-horse-battery-staple"},
        headers=auth,
    )
    assert created.status_code == 201, created.text
    return {**auth, "X-AeroDeck-User-Id": created.json()["id"]}


async def _project(client: AsyncClient, headers: dict[str, str], slug: str) -> dict[str, Any]:
    created = await client.post("/api/v1/projects", json={"slug": slug, "name": slug}, headers=headers)
    assert created.status_code == 201, created.text
    return created.json()


async def _environment(
    client: AsyncClient,
    headers: dict[str, str],
    project_id: str,
    *,
    slug: str,
    kind: str,
) -> dict[str, Any]:
    created = await client.post(
        f"/api/v1/projects/{project_id}/environments",
        json={"slug": slug, "name": slug, "kind": kind, "region": "ams3"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    return created.json()


def _provision(project_id: str, environment_id: str) -> str:
    return f"/api/v1/projects/{project_id}/environments/{environment_id}/provision"


async def _deploy(
    client: AsyncClient, headers: dict[str, str], project_id: str, environment_id: str
) -> dict[str, Any]:
    created = await client.post(
        "/api/v1/deployments",
        json={"repo_url": REPO, "project_id": project_id, "environment_id": environment_id},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    return created.json()


@pytest.mark.asyncio
async def test_new_prod_requires_approval_and_existing_flags_stay_off(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    project = await _project(client, auth, "approval-defaults")
    prod = await _environment(client, auth, project["id"], slug="prod", kind="prod")
    forced_response = await client.post(
        f"/api/v1/projects/{project['id']}/environments",
        json={
            "slug": "prod-off",
            "name": "Prod off",
            "kind": "prod",
            "region": "ams3",
            "prod_deploy_approval": False,
            "auto_deploy_paused": True,
        },
        headers=auth,
    )
    assert forced_response.status_code == 201, forced_response.text
    forced = forced_response.json()
    dev = await _environment(client, auth, project["id"], slug="dev", kind="dev")

    assert prod["prod_deploy_approval"] is True
    assert forced["prod_deploy_approval"] is True
    assert forced["auto_deploy_paused"] is False
    assert prod["auto_deploy_paused"] is False
    assert dev["prod_deploy_approval"] is False
    assert dev["auto_deploy_paused"] is False

    # No members yet, so any real user counts as the owner. A missing user does not.
    owner = await _user(client, auth, "owner-defaults@aerodeck.test")
    updated = await client.patch(
        f"/api/v1/projects/{project['id']}/environments/{prod['id']}",
        json={"auto_deploy_paused": True, "prod_deploy_approval": False},
        headers=owner,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["auto_deploy_paused"] is True
    assert updated.json()["prod_deploy_approval"] is False


@pytest.mark.asyncio
async def test_memberless_prod_still_enqueues(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    dispatcher = FakeDispatcher()
    app.dependency_overrides[get_job_dispatcher] = lambda: dispatcher
    project = await _project(client, auth, "approval-legacy")
    prod = await _environment(client, auth, project["id"], slug="prod", kind="prod")
    deployment = await _deploy(client, auth, project["id"], prod["id"])

    accepted = await client.post(
        _provision(project["id"], prod["id"]),
        json={"deployment_id": deployment["id"]},
        headers=auth,
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["approval_pending"] is False
    assert accepted.json()["job_id"] is not None
    assert len(dispatcher.calls) == 1


@pytest.mark.asyncio
async def test_developer_prod_deploy_waits_until_an_owner_approves_or_rejects(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    dispatcher = FakeDispatcher()
    app.dependency_overrides[get_job_dispatcher] = lambda: dispatcher
    owner = await _user(client, auth, "owner-approval@aerodeck.test")
    developer = await _user(client, auth, "dev-approval@aerodeck.test")
    project = await _project(client, owner, "approval-members")
    await client.put(
        f"/api/v1/projects/{project['id']}/members",
        json={"email": "dev-approval@aerodeck.test", "role": "developer"},
        headers=owner,
    )
    prod = await _environment(client, owner, project["id"], slug="prod", kind="prod")

    held = await _deploy(client, developer, project["id"], prod["id"])
    pending = await client.post(
        _provision(project["id"], prod["id"]),
        json={"deployment_id": held["id"]},
        headers=developer,
    )
    assert pending.status_code == 202, pending.text
    assert pending.json()["approval_pending"] is True
    assert pending.json()["job_id"] is None
    assert dispatcher.calls == []

    stored = await client.get(f"/api/v1/deployments/{held['id']}", headers=owner)
    assert stored.status_code == 200, stored.text
    body = stored.json()
    assert body["status"] == "queued"
    assert body["approval_required_at"] is not None
    assert body["approved_at"] is None

    refused = await client.post(f"/api/v1/deployments/{held['id']}/succeeded", headers=auth)
    assert refused.status_code == 409

    missing_user = await client.post(f"/api/v1/deployments/{held['id']}/approve", headers=auth)
    assert missing_user.status_code == 403
    missing_reject = await client.post(f"/api/v1/deployments/{held['id']}/reject", headers=auth)
    assert missing_reject.status_code == 403
    still_held = await client.get(f"/api/v1/deployments/{held['id']}", headers=owner)
    assert still_held.json()["status"] == "queued"
    assert still_held.json()["approved_at"] is None

    forbidden = await client.post(f"/api/v1/deployments/{held['id']}/approve", headers=developer)
    assert forbidden.status_code == 403
    assert (await client.post(f"/api/v1/deployments/{held['id']}/reject", headers=developer)).status_code == 403

    approved = await client.post(f"/api/v1/deployments/{held['id']}/approve", headers=owner)
    assert approved.status_code == 200, approved.text
    assert approved.json()["approved_at"] is not None
    assert len(dispatcher.calls) == 1
    late_reject = await client.post(f"/api/v1/deployments/{held['id']}/reject", headers=owner)
    assert late_reject.status_code == 409
    still_running = await client.get(f"/api/v1/deployments/{held['id']}", headers=owner)
    assert still_running.json()["status"] != "cancelled"
    assert len(dispatcher.calls) == 1
    second_approve = await client.post(f"/api/v1/deployments/{held['id']}/approve", headers=owner)
    assert second_approve.status_code == 409
    assert len(dispatcher.calls) == 1
    sessions = async_sessionmaker(app.state.engine, expire_on_commit=False)
    async with sessions() as session:
        repo = DeploymentRepository(session)
        lost = await repo.mark_approved(uuid.UUID(held["id"]), datetime.now(UTC))
        not_cancelled = await repo.cancel_awaiting_approval(uuid.UUID(held["id"]), datetime.now(UTC))
        assert lost is None
        assert not_cancelled is None
    finished = await client.post(f"/api/v1/deployments/{held['id']}/succeeded", headers=auth)
    assert finished.status_code == 200, finished.text

    again = await _deploy(client, developer, project["id"], prod["id"])
    second = await client.post(
        _provision(project["id"], prod["id"]),
        json={"deployment_id": again["id"]},
        headers=developer,
    )
    assert second.status_code == 202, second.text
    assert second.json()["approval_pending"] is True

    rejected = await client.post(f"/api/v1/deployments/{again['id']}/reject", headers=owner)
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "cancelled"
    after_reject = await client.post(f"/api/v1/deployments/{again['id']}/approve", headers=owner)
    assert after_reject.status_code == 409
    assert len(dispatcher.calls) == 1

    owner_deploy = await _deploy(client, owner, project["id"], prod["id"])
    started = await client.post(
        _provision(project["id"], prod["id"]),
        json={"deployment_id": owner_deploy["id"]},
        headers=owner,
    )
    assert started.status_code == 202, started.text
    assert started.json()["approval_pending"] is False
    assert len(dispatcher.calls) == 2


@pytest.mark.asyncio
async def test_developer_cannot_provision_an_owner_created_deployment(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    dispatcher = FakeDispatcher()
    app.dependency_overrides[get_job_dispatcher] = lambda: dispatcher
    owner = await _user(client, auth, "owner-caller@aerodeck.test")
    developer = await _user(client, auth, "dev-caller@aerodeck.test")
    project = await _project(client, owner, "approval-caller")
    await client.put(
        f"/api/v1/projects/{project['id']}/members",
        json={"email": "dev-caller@aerodeck.test", "role": "developer"},
        headers=owner,
    )
    prod = await _environment(client, owner, project["id"], slug="prod", kind="prod")
    created = await _deploy(client, owner, project["id"], prod["id"])

    pending = await client.post(
        _provision(project["id"], prod["id"]),
        json={"deployment_id": created["id"]},
        headers=developer,
    )
    assert pending.status_code == 202, pending.text
    assert pending.json()["approval_pending"] is True
    assert pending.json()["job_id"] is None
    assert dispatcher.calls == []

    stored = await client.get(f"/api/v1/deployments/{created['id']}", headers=owner)
    assert stored.status_code == 200, stored.text
    assert stored.json()["actor_user_id"] == owner["X-AeroDeck-User-Id"]
    assert stored.json()["approval_required_at"] is not None
    assert stored.json()["status"] == "queued"


@pytest.mark.asyncio
async def test_paused_webhook_is_skipped_and_owner_can_still_deploy(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    dispatcher = FakeDispatcher()
    app.dependency_overrides[get_job_dispatcher] = lambda: dispatcher
    owner = await _user(client, auth, "owner-pause@aerodeck.test")
    project = await _project(client, owner, "approval-pause")
    dev = await _environment(client, owner, project["id"], slug="dev", kind="dev")
    paused = await client.patch(
        f"/api/v1/projects/{project['id']}/environments/{dev['id']}",
        json={"auto_deploy_paused": True},
        headers=owner,
    )
    assert paused.status_code == 200, paused.text

    webhook = await client.put(
        f"/api/v1/projects/{project['id']}/environments/{dev['id']}/webhook",
        json={"repo_url": REPO, "branch": "main"},
        headers=owner,
    )
    assert webhook.status_code == 200, webhook.text
    secret = webhook.json()["secret"]
    payload = json.dumps(
        {"ref": "refs/heads/main", "repository": {"full_name": "acme/payments"}}
    ).encode("utf-8")
    pushed = await client.post(
        "/api/v1/webhooks/github",
        content=payload,
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": _sign(payload, secret),
            "X-GitHub-Delivery": "pause-1",
        },
    )
    assert pushed.status_code == 200, pushed.text
    assert pushed.json()["status"] == "ignored"
    assert pushed.json()["reason"] == "auto-deploy paused"
    assert pushed.json()["deployment_ids"] == []
    assert dispatcher.calls == []

    listed = await client.get(f"/api/v1/projects/{project['id']}/deployments", headers=owner)
    assert listed.json()["total"] == 0

    logs = await client.get(f"/api/v1/projects/{project['id']}/audit-logs", headers=owner)
    skipped = [item for item in logs.json()["items"] if item["action"] == "deployment.auto_deploy_skipped"]
    assert len(skipped) == 1
    assert secret not in json.dumps(skipped)

    duplicate = await client.post(
        "/api/v1/webhooks/github",
        content=payload,
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": _sign(payload, secret),
            "X-GitHub-Delivery": "pause-1",
        },
    )
    assert duplicate.status_code == 200
    logs_again = await client.get(f"/api/v1/projects/{project['id']}/audit-logs", headers=owner)
    skipped_again = [
        item for item in logs_again.json()["items"] if item["action"] == "deployment.auto_deploy_skipped"
    ]
    assert len(skipped_again) == 1

    deployment = await _deploy(client, owner, project["id"], dev["id"])
    started = await client.post(
        _provision(project["id"], dev["id"]),
        json={"deployment_id": deployment["id"]},
        headers=owner,
    )
    assert started.status_code == 202, started.text
    assert started.json()["job_id"] is not None
    latest = await client.post(f"/api/v1/deployments/{deployment['id']}/deploy-latest", headers=owner)
    assert latest.status_code == 201, latest.text
    # Pause does not block the owner. Overlap may hold this copy behind the one just started.
    latest_body = latest.json()
    assert latest_body["approval_required_at"] is None
    assert latest_body["status"] in ("queued", "running")
    if latest_body["held_for_deployment_id"] is None:
        assert len(dispatcher.calls) == 2
    else:
        assert latest_body["held_for_deployment_id"] == deployment["id"]
        assert len(dispatcher.calls) == 1


@pytest.mark.asyncio
async def test_webhook_to_prod_waits_for_an_owner(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    dispatcher = FakeDispatcher()
    app.dependency_overrides[get_job_dispatcher] = lambda: dispatcher
    owner = await _user(client, auth, "owner-hook@aerodeck.test")
    project = await _project(client, owner, "approval-hook")
    prod = await _environment(client, owner, project["id"], slug="prod", kind="prod")
    webhook = await client.put(
        f"/api/v1/projects/{project['id']}/environments/{prod['id']}/webhook",
        json={"repo_url": REPO, "branch": "main"},
        headers=owner,
    )
    secret = webhook.json()["secret"]
    payload = json.dumps(
        {"ref": "refs/heads/main", "repository": {"full_name": "acme/payments"}}
    ).encode("utf-8")
    pushed = await client.post(
        "/api/v1/webhooks/github",
        content=payload,
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": _sign(payload, secret),
            "X-GitHub-Delivery": "hook-1",
        },
    )
    assert pushed.status_code == 200, pushed.text
    assert pushed.json()["status"] == "pending_approval"
    assert len(pushed.json()["deployment_ids"]) == 1
    assert dispatcher.calls == []
    assert secret not in pushed.text

    approved = await client.post(
        f"/api/v1/deployments/{pushed.json()['deployment_ids'][0]}/approve",
        headers=owner,
    )
    assert approved.status_code == 200, approved.text
    assert str(owner["X-AeroDeck-User-Id"]) in json.dumps(
        (await client.get(f"/api/v1/projects/{project['id']}/audit-logs", headers=owner)).json()
    )
    assert len(dispatcher.calls) == 1


@pytest.mark.asyncio
async def test_developer_rollback_to_prod_waits_for_an_owner(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    dispatcher = FakeDispatcher()
    app.dependency_overrides[get_job_dispatcher] = lambda: dispatcher
    owner = await _user(client, auth, "owner-rollback@aerodeck.test")
    developer = await _user(client, auth, "dev-rollback@aerodeck.test")
    project = await _project(client, owner, "approval-rollback")
    await client.put(
        f"/api/v1/projects/{project['id']}/members",
        json={"email": "dev-rollback@aerodeck.test", "role": "developer"},
        headers=owner,
    )
    prod = await _environment(client, owner, project["id"], slug="prod", kind="prod")
    source = await _deploy(client, owner, project["id"], prod["id"])
    started = await client.post(
        _provision(project["id"], prod["id"]),
        json={"deployment_id": source["id"]},
        headers=owner,
    )
    assert started.status_code == 202, started.text
    finished = await client.post(f"/api/v1/deployments/{source['id']}/succeeded", headers=owner)
    assert finished.status_code == 200, finished.text

    rolled = await client.post(
        f"/api/v1/deployments/{source['id']}/rollback",
        json={"restore_variables": False},
        headers=developer,
    )
    assert rolled.status_code == 201, rolled.text
    body = rolled.json()
    assert body["status"] == "queued"
    assert body["approval_required_at"] is not None
    assert body["trigger"] == "rollback"
    assert len(dispatcher.calls) == 1

    approved = await client.post(f"/api/v1/deployments/{body['id']}/approve", headers=owner)
    assert approved.status_code == 200, approved.text
    assert len(dispatcher.calls) == 2
    assert dispatcher.calls[-1]["meta"]["rollback_from_deployment_id"] == source["id"]


@pytest.mark.asyncio
async def test_only_an_owner_can_change_pause_or_production_approval(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    owner = await _user(client, auth, "owner-flags@aerodeck.test")
    developer = await _user(client, auth, "dev-flags@aerodeck.test")
    project = await _project(client, owner, "approval-flags")
    await client.put(
        f"/api/v1/projects/{project['id']}/members",
        json={"email": "dev-flags@aerodeck.test", "role": "developer"},
        headers=owner,
    )
    prod = await _environment(client, owner, project["id"], slug="prod", kind="prod")
    env_url = f"/api/v1/projects/{project['id']}/environments/{prod['id']}"

    renamed = await client.patch(env_url, json={"region": "fra1"}, headers=developer)
    assert renamed.status_code == 200, renamed.text

    missing_user = await client.patch(env_url, json={"auto_deploy_paused": True}, headers=auth)
    assert missing_user.status_code == 403
    paused = await client.patch(env_url, json={"auto_deploy_paused": True}, headers=developer)
    assert paused.status_code == 403
    approval = await client.patch(env_url, json={"prod_deploy_approval": False}, headers=developer)
    assert approval.status_code == 403

    unchanged = await client.get(env_url, headers=owner)
    assert unchanged.json()["auto_deploy_paused"] is False
    assert unchanged.json()["prod_deploy_approval"] is True

    updated = await client.patch(
        env_url,
        json={"auto_deploy_paused": True, "prod_deploy_approval": False},
        headers=owner,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["auto_deploy_paused"] is True
    assert updated.json()["prod_deploy_approval"] is False

    staging = await _environment(client, owner, project["id"], slug="staging", kind="staging")
    staging_url = f"/api/v1/projects/{project['id']}/environments/{staging['id']}"
    entered = await client.patch(staging_url, json={"kind": "prod"}, headers=developer)
    assert entered.status_code == 403
    assert (await client.get(staging_url, headers=owner)).json()["kind"] == "staging"
    became_prod = await client.patch(staging_url, json={"kind": "prod"}, headers=owner)
    assert became_prod.status_code == 200, became_prod.text
    assert became_prod.json()["kind"] == "prod"
    assert became_prod.json()["prod_deploy_approval"] is True
    left = await client.patch(staging_url, json={"kind": "staging"}, headers=developer)
    assert left.status_code == 403
