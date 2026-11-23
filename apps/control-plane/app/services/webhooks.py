"""GitHub push webhook verification, CRUD, and auto-redeploy dispatch (#249)."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from cryptography.fernet import InvalidToken
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ConflictError, NotFoundError
from app.core.github import canonical_github_repo_url, parse_github_repo
from app.core.logging import get_logger
from app.models.deployment import DeploymentTrigger
from app.models.project import Environment
from app.models.webhook import EnvironmentWebhook
from app.schemas.deployment import DeploymentCreate
from app.services.environment_vars import _fernet_for, encrypt_env_value
from app.services.provisioning import JobKind

logger = get_logger("services.webhooks")

_ALLOWED_ADDONS = {"postgres", "redis", "mongodb"}
_MAX_REPO_WEBHOOKS = 50
_MAX_WEBHOOK_BODY_BYTES = 1024 * 1024


class WebhookSignatureError(AppError):
    status_code = 401
    code = "invalid_webhook_signature"


class EnvironmentLookup(Protocol):
    async def get(self, project_id: uuid.UUID, environment_id: uuid.UUID) -> Environment: ...


class DeploymentCreator(Protocol):
    async def create(self, data: DeploymentCreate) -> Any: ...


class JobEnqueuer(Protocol):
    async def enqueue(
        self,
        kind: JobKind,
        project_id: uuid.UUID,
        environment_id: uuid.UUID,
        *,
        deployment_id: uuid.UUID | None = None,
        env_vars: dict[str, str] | None = None,
        addons: list[str] | None = None,
    ) -> Any: ...


class WebhookUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    repo_url: str = Field(min_length=1, max_length=512)
    branch: str = Field(default="main", min_length=1, max_length=128)
    addons: list[Literal["postgres", "redis", "mongodb"]] = Field(default_factory=list)
    enabled: bool = True
    rotate_secret: bool = False

    @field_validator("repo_url")
    @classmethod
    def valid_repo_url(cls, value: str) -> str:
        parsed = parse_github_repo(value)
        if parsed is None:
            raise ValueError("repo_url must be an http(s)://github.com/{owner}/{repo} URL")
        return canonical_github_repo_url(*parsed)

    @field_validator("branch")
    @classmethod
    def valid_branch(cls, value: str) -> str:
        if ".." in value or value.startswith("/") or value.endswith("/") or " " in value:
            raise ValueError("branch contains invalid characters")
        return value


class WebhookRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    project_id: uuid.UUID
    environment_id: uuid.UUID
    repo_url: str
    branch: str
    addons: list[str]
    enabled: bool
    has_secret: bool = True
    secret: str | None = None
    last_delivery_id: str | None = None
    last_triggered_at: datetime | None = None


class WebhookDeliveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["pong", "triggered", "cleaned_up", "ignored", "pending_approval"]
    reason: str | None = None
    deployment_ids: list[uuid.UUID] = Field(default_factory=list)


def verify_github_signature(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    """Verify GitHub's ``X-Hub-Signature-256`` header in constant time."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip())


def _parse_addons(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        logger.warning("ignored malformed addons_json on webhook record")
        return []
    if not isinstance(items, list):
        return []
    return [str(x) for x in items if str(x) in _ALLOWED_ADDONS]


def _to_read(row: EnvironmentWebhook, *, secret: str | None = None) -> WebhookRead:
    return WebhookRead(
        id=row.id,
        project_id=row.project_id,
        environment_id=row.environment_id,
        repo_url=row.repo_url,
        branch=row.branch,
        addons=_parse_addons(row.addons_json),
        enabled=row.enabled,
        has_secret=True,
        secret=secret,
        last_delivery_id=row.last_delivery_id,
        last_triggered_at=row.last_triggered_at,
    )


class WebhookRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_environment(
        self, project_id: uuid.UUID, environment_id: uuid.UUID
    ) -> EnvironmentWebhook | None:
        result = await self._session.execute(
            select(EnvironmentWebhook).where(
                EnvironmentWebhook.project_id == project_id,
                EnvironmentWebhook.environment_id == environment_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_enabled_for_repo(
        self, repo_url: str, *, limit: int = _MAX_REPO_WEBHOOKS
    ) -> Sequence[EnvironmentWebhook]:
        rows = await self._session.scalars(
            select(EnvironmentWebhook)
            .where(
                EnvironmentWebhook.repo_url == repo_url,
                EnvironmentWebhook.enabled.is_(True),
            )
            .order_by(EnvironmentWebhook.created_at, EnvironmentWebhook.id)
            .limit(limit)
        )
        return rows.all()

    async def add(self, row: EnvironmentWebhook) -> EnvironmentWebhook:
        self._session.add(row)
        await self._session.flush()
        return row

    async def delete(self, row: EnvironmentWebhook) -> None:
        await self._session.delete(row)
        await self._session.flush()


class WebhookService:
    def __init__(
        self,
        repo: WebhookRepository,
        environments: EnvironmentLookup,
        *,
        signing_key: str,
        pr_previews: Any = None,
        approval: Any = None,
    ) -> None:
        self._repo = repo
        self._environments = environments
        self._signing_key = signing_key
        self._pr_previews = pr_previews
        self._approval = approval

    async def get_for_environment(
        self, project_id: uuid.UUID, environment_id: uuid.UUID
    ) -> WebhookRead | None:
        await self._environments.get(project_id, environment_id)
        row = await self._repo.get_for_environment(project_id, environment_id)
        return _to_read(row) if row is not None else None

    async def upsert_for_environment(
        self, project_id: uuid.UUID, environment_id: uuid.UUID, data: WebhookUpsertRequest
    ) -> WebhookRead:
        await self._environments.get(project_id, environment_id)
        existing = await self._repo.get_for_environment(project_id, environment_id)
        addons_json = json.dumps(data.addons) if data.addons else None
        if existing is None:
            raw_secret = secrets.token_hex(32)
            row = await self._repo.add(
                EnvironmentWebhook(
                    project_id=project_id,
                    environment_id=environment_id,
                    repo_url=data.repo_url,
                    branch=data.branch,
                    addons_json=addons_json,
                    encrypted_secret=encrypt_env_value(raw_secret, self._signing_key),
                    enabled=data.enabled,
                )
            )
            logger.info("created github webhook for environment %s (%s)", environment_id, data.repo_url)
            return _to_read(row, secret=raw_secret)

        existing.repo_url = data.repo_url
        existing.branch = data.branch
        existing.addons_json = addons_json
        existing.enabled = data.enabled
        revealed: str | None = None
        if data.rotate_secret:
            revealed = secrets.token_hex(32)
            existing.encrypted_secret = encrypt_env_value(revealed, self._signing_key)
        logger.info("updated github webhook for environment %s", environment_id)
        return _to_read(existing, secret=revealed)

    async def delete_for_environment(
        self, project_id: uuid.UUID, environment_id: uuid.UUID
    ) -> None:
        await self._environments.get(project_id, environment_id)
        row = await self._repo.get_for_environment(project_id, environment_id)
        if row is None:
            raise NotFoundError(f"Webhook for environment {environment_id} not found")
        await self._repo.delete(row)

    async def handle_github_event(
        self,
        *,
        raw_body: bytes,
        event: str,
        signature: str | None,
        delivery_id: str | None,
        deployments: DeploymentCreator,
        jobs: JobEnqueuer,
    ) -> WebhookDeliveryResult:
        if len(raw_body) > _MAX_WEBHOOK_BODY_BYTES:
            raise WebhookSignatureError("Webhook payload exceeds maximum allowed size")
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise WebhookSignatureError("Invalid webhook JSON payload") from exc

        repo_obj = payload.get("repository") if isinstance(payload, dict) else None
        repo_url_raw = (
            repo_obj.get("html_url")
            or (f"https://github.com/{repo_obj['full_name']}" if repo_obj.get("full_name") else "")
            if isinstance(repo_obj, dict)
            else ""
        )
        parsed = parse_github_repo(str(repo_url_raw))
        if parsed is None:
            raise WebhookSignatureError("Webhook repository URL is missing or invalid")
        canonical_repo = canonical_github_repo_url(*parsed)

        candidates = await self._repo.list_enabled_for_repo(canonical_repo)
        fernet = _fernet_for(self._signing_key)
        verified: list[EnvironmentWebhook] = []
        for candidate in candidates:
            try:
                secret = fernet.decrypt(candidate.encrypted_secret.encode("ascii")).decode("utf-8")
            except InvalidToken:
                continue
            if verify_github_signature(raw_body, signature, secret):
                verified.append(candidate)

        if not verified:
            raise WebhookSignatureError("Invalid or unknown GitHub webhook signature")

        if event == "ping":
            return WebhookDeliveryResult(status="pong")
        if event == "pull_request" and self._pr_previews is not None:
            pr_status, pr_reason, pr_ids = await self._pr_previews.handle_event(
                payload=payload,
                verified_webhooks=verified,
                deployments=deployments,
                jobs=jobs,
                parse_addons=_parse_addons,
            )
            return WebhookDeliveryResult(
                status=pr_status,
                reason=pr_reason,
                deployment_ids=pr_ids,
            )
        if event != "push":
            return WebhookDeliveryResult(status="ignored", reason=f"unsupported event: {event}")
        if payload.get("deleted") is True:
            return WebhookDeliveryResult(status="ignored", reason="branch deleted")

        ref = str(payload.get("ref") or "")
        triggered_ids: list[uuid.UUID] = []
        pending_ids: list[uuid.UUID] = []
        skipped_reason: str | None = None

        for webhook in verified:
            if ref != f"refs/heads/{webhook.branch}":
                skipped_reason = skipped_reason or "branch mismatch"
                continue
            if delivery_id and webhook.last_delivery_id == delivery_id:
                skipped_reason = skipped_reason or "duplicate delivery"
                continue
            environment = await self._environments.get(webhook.project_id, webhook.environment_id)
            if self._approval is not None and environment.auto_deploy_paused:
                await self._approval.note_auto_deploy_skipped(environment)
                webhook.last_delivery_id = delivery_id
                webhook.last_triggered_at = datetime.now(UTC)
                skipped_reason = "auto-deploy paused"
                continue
            deployment = await deployments.create(
                DeploymentCreate(
                    repo_url=webhook.repo_url,
                    project_id=webhook.project_id,
                    environment_id=webhook.environment_id,
                ),
                trigger=DeploymentTrigger.webhook,
            )
            addons = _parse_addons(webhook.addons_json) or None
            if self._approval is not None and await self._approval.needs(environment, None):
                await self._approval.defer(deployment.id, addons=addons)
                webhook.last_delivery_id = delivery_id
                webhook.last_triggered_at = datetime.now(UTC)
                pending_ids.append(deployment.id)
                logger.info(
                    "github webhook stored deployment %s pending approval for %s (%s)",
                    deployment.id,
                    webhook.repo_url,
                    webhook.branch,
                )
                continue
            try:
                await jobs.enqueue(
                    JobKind.provision,
                    webhook.project_id,
                    webhook.environment_id,
                    deployment_id=deployment.id,
                    addons=addons,
                )
            except ConflictError:
                skipped_reason = "container swap already started"
                continue
            webhook.last_delivery_id = delivery_id
            webhook.last_triggered_at = datetime.now(UTC)
            triggered_ids.append(deployment.id)
            logger.info(
                "github webhook triggered deployment %s for %s (%s)",
                deployment.id,
                webhook.repo_url,
                webhook.branch,
            )

        if triggered_ids:
            return WebhookDeliveryResult(
                status="triggered", deployment_ids=[*triggered_ids, *pending_ids]
            )
        if pending_ids:
            return WebhookDeliveryResult(status="pending_approval", deployment_ids=pending_ids)
        return WebhookDeliveryResult(status="ignored", reason=skipped_reason or "no matching branch")
