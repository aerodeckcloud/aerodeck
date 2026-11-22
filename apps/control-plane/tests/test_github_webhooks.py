"""Unit and API tests for GitHub push webhooks (#249)."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.api.deps import get_job_dispatcher
from app.repositories.jobs import JobState
from app.services.provisioning import JobKind
from app.services.webhooks import verify_github_signature


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class FakeWebhookDispatcher:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def dispatch(self, kind: JobKind, *, project_id: str, environment_id: str, **meta: str) -> str:
        self.calls.append(
            {"kind": kind, "project_id": project_id, "environment_id": environment_id, "meta": meta}
        )
        return str(uuid.uuid4())

    def state(self, job_id: str) -> JobState:
        return JobState(job_id=job_id, status="queued")


def test_verify_github_signature_constant_time() -> None:
    body = b'{"ref":"refs/heads/main"}'
    sig = _sign(body, "super-secret")
    assert verify_github_signature(body, sig, "super-secret") is True
    assert verify_github_signature(body, sig, "wrong-secret") is False
    assert verify_github_signature(body, "md5=abc", "super-secret") is False
    assert verify_github_signature(body, None, "super-secret") is False


@pytest.mark.asyncio
async def test_github_webhook_crud_and_push_dispatch(
    app: FastAPI, client: AsyncClient, auth: dict[str, str]
) -> None:
    dispatcher = FakeWebhookDispatcher()
    app.dependency_overrides[get_job_dispatcher] = lambda: dispatcher
    try:
        project = (
            await client.post("/api/v1/projects", json={"slug": "wh-app", "name": "WH App"}, headers=auth)
        ).json()
        env = (
            await client.post(
                f"/api/v1/projects/{project['id']}/environments",
                json={"slug": "dev", "name": "Dev", "kind": "dev", "region": "ams3"},
                headers=auth,
            )
        ).json()
        wh_url = f"/api/v1/projects/{project['id']}/environments/{env['id']}/webhook"

        # Initially none
        assert (await client.get(wh_url, headers=auth)).json() is None

        # Create returns one-time secret
        created = await client.put(
            wh_url,
            json={
                "repo_url": "https://github.com/mangatohjunior/dev-portal",
                "branch": "main",
                "addons": ["mongodb"],
            },
            headers=auth,
        )
        assert created.status_code == 200
        created_body = created.json()
        secret = created_body["secret"]
        assert isinstance(secret, str) and len(secret) == 64
        assert created_body["addons"] == ["mongodb"]

        # Subsequent GET never exposes secret
        fetched = (await client.get(wh_url, headers=auth)).json()
        assert fetched["secret"] is None
        assert fetched["has_secret"] is True

        # Invalid signature -> 401
        bad_payload = json.dumps(
            {"ref": "refs/heads/main", "repository": {"full_name": "mangatohjunior/dev-portal"}}
        ).encode("utf-8")
        bad_res = await client.post(
            "/api/v1/webhooks/github",
            content=bad_payload,
            headers={
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": _sign(bad_payload, "wrong-secret"),
                "X-GitHub-Delivery": "del-bad",
            },
        )
        assert bad_res.status_code == 401

        # Ping -> 200 pong
        ping_res = await client.post(
            "/api/v1/webhooks/github",
            content=bad_payload,
            headers={
                "X-GitHub-Event": "ping",
                "X-Hub-Signature-256": _sign(bad_payload, secret),
                "X-GitHub-Delivery": "del-ping",
            },
        )
        assert ping_res.status_code == 200
        assert ping_res.json()["status"] == "pong"

        # Non-matching branch -> 200 ignored
        other_branch = json.dumps(
            {"ref": "refs/heads/feature", "repository": {"full_name": "mangatohjunior/dev-portal"}}
        ).encode("utf-8")
        other_res = await client.post(
            "/api/v1/webhooks/github",
            content=other_branch,
            headers={
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": _sign(other_branch, secret),
                "X-GitHub-Delivery": "del-branch",
            },
        )
        assert other_res.status_code == 200
        assert other_res.json()["status"] == "ignored"
        assert len(dispatcher.calls) == 0

        # Deleted branch -> 200 ignored
        deleted_branch = json.dumps(
            {
                "ref": "refs/heads/main",
                "deleted": True,
                "repository": {"full_name": "mangatohjunior/dev-portal"},
            }
        ).encode("utf-8")
        del_branch_res = await client.post(
            "/api/v1/webhooks/github",
            content=deleted_branch,
            headers={
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": _sign(deleted_branch, secret),
                "X-GitHub-Delivery": "del-deleted",
            },
        )
        assert del_branch_res.status_code == 200
        assert del_branch_res.json()["reason"] == "branch deleted"

        # Matching push -> 202 triggered
        push_res = await client.post(
            "/api/v1/webhooks/github",
            content=bad_payload,
            headers={
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": _sign(bad_payload, secret),
                "X-GitHub-Delivery": "del-1",
            },
        )
        assert push_res.status_code == 202
        assert push_res.json()["status"] == "triggered"
        assert len(push_res.json()["deployment_ids"]) == 1
        assert len(dispatcher.calls) == 1
        assert dispatcher.calls[0]["meta"]["addons_json"] == '["mongodb"]'

        # Duplicate delivery -> 200 ignored idempotently
        dup_res = await client.post(
            "/api/v1/webhooks/github",
            content=bad_payload,
            headers={
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": _sign(bad_payload, secret),
                "X-GitHub-Delivery": "del-1",
            },
        )
        assert dup_res.status_code == 200
        assert dup_res.json()["reason"] == "duplicate delivery"
        assert len(dispatcher.calls) == 1

        # A second push waits behind the first instead of starting another job (#271).
        second = await client.post(
            "/api/v1/webhooks/github",
            content=bad_payload,
            headers={
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": _sign(bad_payload, secret),
                "X-GitHub-Delivery": "del-2",
            },
        )
        assert second.status_code == 202
        assert second.json()["status"] == "triggered"
        assert len(dispatcher.calls) == 1
        held = await client.get(
            f"/api/v1/deployments/{second.json()['deployment_ids'][0]}", headers=auth
        )
        assert held.status_code == 200
        assert held.json()["status"] == "queued"
        assert held.json()["held_for_deployment_id"] == push_res.json()["deployment_ids"][0]

        # Delete webhook
        assert (await client.delete(wh_url, headers=auth)).status_code == 204
    finally:
        app.dependency_overrides.pop(get_job_dispatcher, None)
