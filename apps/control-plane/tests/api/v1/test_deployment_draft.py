"""A draft is saved for its owner and does not queue provision (#343)."""

from uuid import uuid4

from httpx import AsyncClient

from app.schemas.deployment_draft import DeploymentDraftPlan
from app.services.deployment_draft import mask_plan, merge_plan

DEPLOYMENTS = "/api/v1/deployments"
REPO = "https://github.com/mangatohjunior/aerodeck"


def _plan(step: str = "build", *, secret: str = "super-secret", redacted: bool = False) -> dict:
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
    stored = DeploymentDraftPlan.model_validate(_plan())
    incoming = DeploymentDraftPlan.model_validate(_plan(secret="", redacted=True))
    merged = merge_plan(stored, incoming)
    assert merged.variables[0].value == "super-secret"
    assert mask_plan(merged).variables[0].value == ""
    assert mask_plan(merged).variables[0].redacted is True
    assert mask_plan(merged).variables[1].value == "portal"


async def test_draft_is_private_and_does_not_queue(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    owner = await _user(client, auth, "draft-owner@aerodeck.test")
    other = await _user(client, auth, "draft-other@aerodeck.test")

    created = await client.post(f"{DEPLOYMENTS}/draft", json={"repo_url": REPO, "plan": _plan()}, headers=owner)
    assert created.status_code == 201, created.text
    body = created.json()
    draft_id = body["deployment"]["id"]
    assert body["deployment"]["status"] == "draft"
    assert body["deployment"]["provision_job_id"] is None
    assert body["deployment"]["draft_step"] == "build"
    assert body["plan"]["variables"][0]["value"] == ""
    assert body["plan"]["variables"][0]["redacted"] is True

    listed = await client.get(DEPLOYMENTS, headers=owner)
    assert listed.status_code == 200, listed.text
    assert any(item["id"] == draft_id for item in listed.json()["items"])

    hidden = await client.get(DEPLOYMENTS, headers=other)
    assert all(item["id"] != draft_id for item in hidden.json()["items"])

    denied = await client.get(f"{DEPLOYMENTS}/{draft_id}/draft", headers=other)
    assert denied.status_code == 404

    started = await client.post(f"{DEPLOYMENTS}/{draft_id}/succeeded", headers=owner)
    assert started.status_code == 409

    discarded = await client.post(f"{DEPLOYMENTS}/{draft_id}/discard", headers=owner)
    assert discarded.status_code == 200, discarded.text
    assert discarded.json()["status"] == "cancelled"
    assert discarded.json()["provision_job_id"] is None


async def test_draft_requires_a_known_user(client: AsyncClient, auth: dict[str, str]) -> None:
    missing = await client.post(
        f"{DEPLOYMENTS}/draft",
        json={"repo_url": REPO, "plan": _plan()},
        headers={**auth, "X-AeroDeck-User-Id": str(uuid4())},
    )
    assert missing.status_code == 422
