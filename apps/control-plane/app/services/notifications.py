"""Deployment notification channels, payload formatters, and non-blocking dispatch (#259)."""

from __future__ import annotations

import asyncio
import ipaddress
import uuid
from datetime import datetime
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

import httpx
from cryptography.fernet import InvalidToken
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.models.notification_channel import NotificationChannel, NotificationProvider
from app.models.project import Project
from app.services.environment_vars import decrypt_env_value, encrypt_env_value

logger = get_logger("services.notifications")

NotificationEvent = Literal["started", "succeeded", "failed", "test"]

_FORBIDDEN_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "169.254.169.254"}
_DISCORD_COLORS = {
    "started": 0x3B82F6,
    "succeeded": 0x22C55E,
    "failed": 0xEF4444,
    "test": 0x6366F1,
}


class ProjectLookup(Protocol):
    async def get(self, project_id: uuid.UUID) -> Project: ...


def validate_notification_url(raw_url: str) -> tuple[str, str]:
    """Validate an outbound notification webhook URL and return ``(url, host_hint)``."""
    cleaned = raw_url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Notification webhook URL must use https:// and include a hostname.")
    if parsed.username or parsed.password:
        raise ValueError("Notification webhook URL must not contain embedded credentials.")
    host = parsed.hostname.lower().rstrip(".")
    if host in _FORBIDDEN_HOSTS or host.endswith(".local") or host.endswith(".internal"):
        raise ValueError("Notification webhook URL cannot target local or internal hosts.")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("Notification webhook URL cannot target private IP ranges.")
    except ValueError as exc:
        if "private IP" in str(exc):
            raise
    return cleaned, host[:128]


def format_notification_payload(
    provider: NotificationProvider,
    *,
    event: NotificationEvent,
    project_slug: str,
    environment_slug: str,
    deployment_id: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Build a Slack, Discord, or generic JSON notification payload."""
    label = f"{project_slug}/{environment_slug}" if environment_slug else project_slug
    headline = f"AeroDeck · {label} · deployment {event}"
    body = detail or f"Deployment {event} for {label}."
    if deployment_id:
        body = f"{body} (deployment `{deployment_id[:8]}`)"

    if provider is NotificationProvider.slack:
        return {
            "text": f"{headline}: {body}",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*{headline}*\n{body}"}},
            ],
        }
    if provider is NotificationProvider.discord:
        return {
            "content": headline,
            "embeds": [
                {
                    "title": headline,
                    "description": body,
                    "color": _DISCORD_COLORS.get(event, 0x6366F1),
                }
            ],
        }
    return {
        "event": f"deployment.{event}",
        "project": project_slug,
        "environment": environment_slug,
        "deployment_id": deployment_id,
        "detail": detail,
    }


class NotificationChannelUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: NotificationProvider = NotificationProvider.slack
    webhook_url: str | None = Field(default=None, max_length=2048)
    notify_on_started: bool = False
    notify_on_succeeded: bool = True
    notify_on_failed: bool = True
    enabled: bool = True

    @field_validator("webhook_url")
    @classmethod
    def check_url(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        cleaned, _ = validate_notification_url(value)
        return cleaned


class NotificationChannelRead(BaseModel):
    """Masked view of a notification channel (never exposes the encrypted webhook URL)."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    provider: NotificationProvider
    url_hint: str
    notify_on_started: bool
    notify_on_succeeded: bool
    notify_on_failed: bool
    enabled: bool
    updated_at: datetime


class NotificationTestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    status_code: int | None = None
    error: str | None = None


class NotificationChannelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_project(self, project_id: uuid.UUID) -> NotificationChannel | None:
        result = await self._session.execute(
            select(NotificationChannel).where(NotificationChannel.project_id == project_id)
        )
        return result.scalar_one_or_none()

    async def add(self, row: NotificationChannel) -> NotificationChannel:
        self._session.add(row)
        await self._session.flush()
        return row

    async def delete(self, row: NotificationChannel) -> None:
        await self._session.delete(row)
        await self._session.flush()


class NotificationService:
    def __init__(
        self,
        repo: NotificationChannelRepository,
        projects: ProjectLookup,
        *,
        signing_key: str,
    ) -> None:
        self._repo = repo
        self._projects = projects
        self._signing_key = signing_key

    async def get_for_project(self, project_id: uuid.UUID) -> NotificationChannelRead | None:
        await self._projects.get(project_id)
        row = await self._repo.get_for_project(project_id)
        return NotificationChannelRead.model_validate(row) if row is not None else None

    async def upsert_for_project(
        self, project_id: uuid.UUID, data: NotificationChannelUpsertRequest
    ) -> NotificationChannelRead:
        await self._projects.get(project_id)
        existing = await self._repo.get_for_project(project_id)
        if existing is None:
            if not data.webhook_url:
                raise ValueError("webhook_url is required when creating a notification channel.")
            cleaned, hint = validate_notification_url(data.webhook_url)
            row = await self._repo.add(
                NotificationChannel(
                    project_id=project_id,
                    provider=data.provider,
                    encrypted_url=encrypt_env_value(cleaned, self._signing_key),
                    url_hint=hint,
                    notify_on_started=data.notify_on_started,
                    notify_on_succeeded=data.notify_on_succeeded,
                    notify_on_failed=data.notify_on_failed,
                    enabled=data.enabled,
                )
            )
            return NotificationChannelRead.model_validate(row)

        existing.provider = data.provider
        existing.notify_on_started = data.notify_on_started
        existing.notify_on_succeeded = data.notify_on_succeeded
        existing.notify_on_failed = data.notify_on_failed
        existing.enabled = data.enabled
        if data.webhook_url:
            cleaned, hint = validate_notification_url(data.webhook_url)
            existing.encrypted_url = encrypt_env_value(cleaned, self._signing_key)
            existing.url_hint = hint
        return NotificationChannelRead.model_validate(existing)

    async def delete_for_project(self, project_id: uuid.UUID) -> None:
        await self._projects.get(project_id)
        row = await self._repo.get_for_project(project_id)
        if row is None:
            raise NotFoundError(f"Notification channel for project {project_id} not found")
        await self._repo.delete(row)

    async def send_test(
        self, project_id: uuid.UUID, *, http_client: httpx.AsyncClient | None = None
    ) -> NotificationTestResult:
        project = await self._projects.get(project_id)
        row = await self._repo.get_for_project(project_id)
        if row is None:
            raise NotFoundError(f"Notification channel for project {project_id} not found")
        try:
            url = decrypt_env_value(row.encrypted_url, self._signing_key)
        except InvalidToken:
            return NotificationTestResult(ok=False, error="Stored webhook URL could not be decrypted.")
        payload = format_notification_payload(
            row.provider,
            event="test",
            project_slug=project.slug,
            environment_slug="console",
            detail="Test notification from AeroDeck Console.",
        )
        return await _post_notification(url, payload, http_client=http_client)


async def _post_notification(
    url: str,
    payload: dict[str, Any],
    *,
    http_client: httpx.AsyncClient | None = None,
) -> NotificationTestResult:
    try:
        if http_client is not None:
            resp = await http_client.post(url, json=payload)
        else:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, json=payload)
        ok = 200 <= resp.status_code < 300
        return NotificationTestResult(
            ok=ok,
            status_code=resp.status_code,
            error=None if ok else f"Webhook returned HTTP {resp.status_code}.",
        )
    except Exception as exc:
        logger.warning("notification delivery failed: %s", type(exc).__name__)
        return NotificationTestResult(ok=False, error="Failed to reach notification webhook.")


async def _notify_deployment_event(
    *,
    project_id: uuid.UUID,
    project_slug: str,
    environment_slug: str,
    event: Literal["started", "succeeded", "failed"],
    deployment_id: str | None = None,
    detail: str | None = None,
) -> None:
    if SessionLocal.kw.get("bind") is None:
        return
    settings = get_settings()
    async with SessionLocal() as session:
        row = await NotificationChannelRepository(session).get_for_project(project_id)
        if row is None or not row.enabled:
            return
        should_send = (
            (event == "started" and row.notify_on_started)
            or (event == "succeeded" and row.notify_on_succeeded)
            or (event == "failed" and row.notify_on_failed)
        )
        if not should_send:
            return
        try:
            url = decrypt_env_value(row.encrypted_url, settings.auth_signing_key.get_secret_value())
        except InvalidToken:
            return
        payload = format_notification_payload(
            row.provider,
            event=event,
            project_slug=project_slug,
            environment_slug=environment_slug,
            deployment_id=deployment_id,
            detail=detail,
        )
        await _post_notification(url, payload)


def notify_deployment_event_sync(
    *,
    project_id: str,
    project_slug: str,
    environment_slug: str,
    event: Literal["started", "succeeded", "failed"],
    deployment_id: str | None = None,
    detail: str | None = None,
) -> None:
    """Best-effort, non-blocking worker hook to send deployment notifications."""
    try:
        p_uuid = uuid.UUID(project_id)
        asyncio.run(
            _notify_deployment_event(
                project_id=p_uuid,
                project_slug=project_slug,
                environment_slug=environment_slug,
                event=event,
                deployment_id=deployment_id,
                detail=detail,
            )
        )
    except Exception as exc:
        logger.warning("deployment notification skipped: %s", type(exc).__name__)
