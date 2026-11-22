"""Unit and API tests for deployment notification channels (#259)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import AsyncClient

from app.core.db import create_engine
from app.models import Base
from app.models.notification_channel import NotificationProvider
from app.services.notifications import (
    format_notification_payload,
    notify_deployment_event_sync,
    validate_notification_url,
)
from tests.conftest import make_settings


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


def test_notify_sync_reads_channels_on_a_private_engine(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dispatch opens its own engine. Sharing SessionLocal across asyncio.run poisons asyncpg."""
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'notifications.db').as_posix()}"

    async def _create_schema() -> None:
        engine = create_engine(database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_create_schema())
    monkeypatch.setattr(
        "app.services.notifications.get_settings",
        lambda: make_settings(database_url),
    )

    with caplog.at_level("WARNING", logger="aerodeck.services.notifications"):
        notify_deployment_event_sync(
            project_id=str(uuid.uuid4()),
            project_slug="portal",
            environment_slug="dev",
            event="succeeded",
            deployment_id=str(uuid.uuid4()),
            detail="Deployment is live.",
        )

    assert "deployment notification skipped" not in caplog.text


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
