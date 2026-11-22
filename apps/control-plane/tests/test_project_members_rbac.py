"""Unit and API tests for project members and RBAC enforcement (#256)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


async def _create_console_user(client: AsyncClient, auth: dict[str, str], email: str) -> dict[str, str]:
    res = await client.post(
        "/api/v1/console-users",
        json={"email": email, "password": "correct-horse-battery-staple"},
        headers=auth,
    )
    assert res.status_code == 201
    return res.json()


@pytest.mark.asyncio
async def test_project_members_rbac_owner_developer_viewer(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    owner = await _create_console_user(client, auth, "owner@aerodeck.test")
    dev = await _create_console_user(client, auth, "dev@aerodeck.test")
    viewer = await _create_console_user(client, auth, "viewer@aerodeck.test")

    owner_headers = {**auth, "X-AeroDeck-User-Id": owner["id"]}
    dev_headers = {**auth, "X-AeroDeck-User-Id": dev["id"]}
    viewer_headers = {**auth, "X-AeroDeck-User-Id": viewer["id"]}

    # Creating a project with X-AeroDeck-User-Id automatically assigns creator as owner.
    project = (
        await client.post(
            "/api/v1/projects",
            json={"slug": "rbac-proj", "name": "RBAC Project"},
            headers=owner_headers,
        )
    ).json()
    pid = project["id"]

    members = (await client.get(f"/api/v1/projects/{pid}/members", headers=owner_headers)).json()
    assert len(members) == 1
    assert members[0]["email"] == "owner@aerodeck.test"
    assert members[0]["role"] == "owner"

    # Owner invites developer and viewer
    add_dev = await client.put(
        f"/api/v1/projects/{pid}/members",
        json={"email": "dev@aerodeck.test", "role": "developer"},
        headers=owner_headers,
    )
    assert add_dev.status_code == 200
    add_viewer = await client.put(
        f"/api/v1/projects/{pid}/members",
        json={"email": "viewer@aerodeck.test", "role": "viewer"},
        headers=owner_headers,
    )
    assert add_viewer.status_code == 200

    # Developer cannot manage members or delete project (403)
    assert (
        await client.put(
            f"/api/v1/projects/{pid}/members",
            json={"email": "viewer@aerodeck.test", "role": "owner"},
            headers=dev_headers,
        )
    ).status_code == 403
    assert (await client.delete(f"/api/v1/projects/{pid}", headers=dev_headers)).status_code == 403

    # Viewer cannot update project or edit env vars (403), but developer can
    env = (
        await client.post(
            f"/api/v1/projects/{pid}/environments",
            json={"slug": "dev", "name": "Dev", "kind": "dev", "region": "ams3"},
            headers=owner_headers,
        )
    ).json()
    assert (
        await client.patch(
            f"/api/v1/projects/{pid}",
            json={"name": "Viewer Hack"},
            headers=viewer_headers,
        )
    ).status_code == 403
    assert (
        await client.put(
            f"/api/v1/projects/{pid}/environments/{env['id']}/env-vars",
            json={"items": [{"key": "FOO", "value": "bar"}]},
            headers=viewer_headers,
        )
    ).status_code == 403
    assert (
        await client.put(
            f"/api/v1/projects/{pid}/environments/{env['id']}/env-vars",
            json={"items": [{"key": "FOO", "value": "bar"}]},
            headers=dev_headers,
        )
    ).status_code == 200

    # Cannot demote or remove the last owner (422)
    assert (
        await client.put(
            f"/api/v1/projects/{pid}/members",
            json={"email": "owner@aerodeck.test", "role": "developer"},
            headers=owner_headers,
        )
    ).status_code == 422
    assert (
        await client.delete(f"/api/v1/projects/{pid}/members/{owner['id']}", headers=owner_headers)
    ).status_code == 422

    # Owner can remove viewer and delete project
    assert (
        await client.delete(f"/api/v1/projects/{pid}/members/{viewer['id']}", headers=owner_headers)
    ).status_code == 204
    assert (await client.delete(f"/api/v1/projects/{pid}", headers=owner_headers)).status_code == 204
