"""API tests for project activity and secret-free audit logs (#257)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_audit_log_records_mutations_without_leaking_secrets(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    user = (
        await client.post(
            "/api/v1/console-users",
            json={"email": "auditor@aerodeck.test", "password": "correct-horse-battery-staple"},
            headers=auth,
        )
    ).json()
    actor_headers = {**auth, "X-AeroDeck-User-Id": user["id"]}

    project = (
        await client.post(
            "/api/v1/projects",
            json={"slug": "audit-proj", "name": "Audit Project"},
            headers=actor_headers,
        )
    ).json()
    env = (
        await client.post(
            f"/api/v1/projects/{project['id']}/environments",
            json={"slug": "dev", "name": "Dev", "kind": "dev", "region": "ams3"},
            headers=actor_headers,
        )
    ).json()

    # Trigger mutations: update project, upsert secret env var, create deployment
    await client.patch(
        f"/api/v1/projects/{project['id']}",
        json={"name": "Audit Project Renamed"},
        headers=actor_headers,
    )
    await client.put(
        f"/api/v1/projects/{project['id']}/environments/{env['id']}/env-vars",
        json={"items": [{"key": "SUPER_SECRET_KEY", "value": "top-secret-plaintext-999"}]},
        headers=actor_headers,
    )
    await client.post(
        "/api/v1/deployments",
        json={
            "repo_url": "https://github.com/mangatohjunior/dev-portal",
            "project_id": project["id"],
            "environment_id": env["id"],
        },
        headers=actor_headers,
    )

    res = await client.get(f"/api/v1/projects/{project['id']}/audit-logs", headers=actor_headers)
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 3
    actions = [item["action"] for item in body["items"]]
    assert set(actions) == {"project.updated", "env_vars.upserted", "deployment.created"}
    assert all(item["actor"] == "auditor@aerodeck.test" for item in body["items"])
    # Secret values must NEVER appear anywhere in audit logs.
    assert "top-secret-plaintext-999" not in res.text
    assert "SUPER_SECRET_KEY" in res.text
