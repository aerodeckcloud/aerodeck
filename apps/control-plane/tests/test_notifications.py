"""Unit and API tests for deployment notification channels (#259)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.models.notification_channel import NotificationProvider
from app.services.notifications import (
    format_notification_payload,
    validate_notification_url,
)


def test_notification_url_validation_and_payload_formatters() -> None:
    url, hint = validate_notification_url("https://hooks.slack.com/services/T000/B000/SECRET")
    assert url == "https://hooks.slack.com/services/T000/B000/SECRET"
    assert hint == "hooks.slack.com"

    for bad in (
        "http://hooks.slack.com/services/1",
        "https://localhost/hook",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.5/webhook",
        "https://user:pass@hooks.slack.com/services/1",
    ):
        with pytest.raises(ValueError):
            validate_notification_url(bad)

    slack = format_notification_payload(
        NotificationProvider.slack,
        event="succeeded",
        project_slug="portal",
        environment_slug="dev",
        deployment_id="11111111-2222-4333-8444-555555555555",
    )
    assert "blocks" in slack and "portal/dev" in slack["text"]

    discord = format_notification_payload(
        NotificationProvider.discord,
        event="failed",
        project_slug="portal",
        environment_slug="prod",
    )
    assert discord["embeds"][0]["color"] == 0xEF4444

    generic = format_notification_payload(
        NotificationProvider.webhook,
        event="succeeded",
        project_slug="portal",
        environment_slug="dev",
    )
    assert generic["event"] == "deployment.succeeded"


@pytest.mark.asyncio
async def test_project_notifications_crud_masks_webhook_url(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    project = (
        await client.post("/api/v1/projects", json={"slug": "notif-app", "name": "Notif App"}, headers=auth)
    ).json()
    base = f"/api/v1/projects/{project['id']}/notifications"

    assert (await client.get(base, headers=auth)).json() is None

    secret_url = "https://hooks.slack.com/services/T123/B456/VERY_SECRET_TOKEN"
    created = await client.put(
        base,
        json={
            "provider": "slack",
            "webhook_url": secret_url,
            "notify_on_started": True,
            "notify_on_succeeded": True,
            "notify_on_failed": True,
        },
        headers=auth,
    )
    assert created.status_code == 200
    assert created.json()["url_hint"] == "hooks.slack.com"
    assert "VERY_SECRET_TOKEN" not in created.text

    # Update toggles without resending URL
    updated = await client.put(
        base,
        json={
            "provider": "discord",
            "notify_on_started": False,
            "notify_on_succeeded": True,
            "notify_on_failed": True,
        },
        headers=auth,
    )
    assert updated.status_code == 200
    assert updated.json()["provider"] == "discord"
    assert updated.json()["notify_on_started"] is False

    # Delete channel
    assert (await client.delete(base, headers=auth)).status_code == 204
    assert (await client.get(base, headers=auth)).json() is None
