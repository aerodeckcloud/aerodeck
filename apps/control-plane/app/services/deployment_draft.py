"""A deployment the operator can leave and reopen. Provision stays on launch (#343)."""

import json
import uuid
from datetime import UTC, datetime

from cryptography.fernet import InvalidToken
from pydantic import ValidationError

from app.core.errors import ConflictError, NotFoundError, UnprocessableError
from app.models.deployment import Deployment, DeploymentStatus, DeploymentTrigger
from app.schemas.deployment import DeploymentCreate, deployment_to_read
from app.schemas.deployment_draft import (
    DeploymentDraftCreate,
    DeploymentDraftLaunch,
    DeploymentDraftPlan,
    DeploymentDraftRead,
    DeploymentDraftWrite,
    DraftVariable,
)
from app.services.deploy_approval import DeployApprovalService
from app.services.deployments import DeploymentService
from app.services.environment_vars import decrypt_env_value, encrypt_env_value
from app.services.environments import EnvironmentService

_PLAN_TOO_LARGE = "Draft plan is too large."
_MAX_PLAN_CHARS = 64_000


def mask_plan(plan: DeploymentDraftPlan) -> DeploymentDraftPlan:
    """Drop secret values. ``redacted`` tells the client to keep the stored secret."""
    masked = plan.model_copy(deep=True)
    variables: list[DraftVariable] = []
    for item in masked.variables:
        if item.secret:
            variables.append(item.model_copy(update={"value": "", "redacted": True}))
        else:
            variables.append(item.model_copy(update={"redacted": False}))
    masked.variables = variables
    return masked


def merge_plan(stored: DeploymentDraftPlan, incoming: DeploymentDraftPlan) -> DeploymentDraftPlan:
    """Keep a stored secret when the write says it is still redacted and sends no new value."""
    previous = {item.key: item for item in stored.variables}
    merged: list[DraftVariable] = []
    for item in incoming.variables:
        kept = previous.get(item.key)
        if item.secret and item.redacted and item.value == "" and kept is not None and kept.secret:
            merged.append(kept.model_copy(update={"redacted": False}))
            continue
        merged.append(item.model_copy(update={"redacted": False}))
    return incoming.model_copy(update={"variables": merged})


def draft_read(deployment: Deployment, plan: DeploymentDraftPlan) -> DeploymentDraftRead:
    return DeploymentDraftRead(deployment=deployment_to_read(deployment), plan=mask_plan(plan))


def plain_values(plan: DeploymentDraftPlan) -> dict[str, str]:
    """Values provision should receive, including secrets the client no longer holds."""
    values: dict[str, str] = {}
    for item in plan.variables:
        if item.value != "":
            values[item.key] = item.value
    extra = plan.extra_env.strip()
    if extra:
        for line in extra.splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key:
                values[key] = value
    return values


class DeploymentDraftService:
    def __init__(
        self,
        signing_key: str,
        deployments: DeploymentService,
        environments: EnvironmentService,
        approval: DeployApprovalService,
    ) -> None:
        self._signing_key = signing_key
        self._deployments = deployments
        self._environments = environments
        self._approval = approval

    async def create(
        self, body: DeploymentDraftCreate, actor_user_id: uuid.UUID | None
    ) -> tuple[Deployment, DeploymentDraftPlan]:
        stored = await self._deployments.create(
            DeploymentCreate(repo_url=body.repo_url),
            trigger=DeploymentTrigger.manual,
            actor_user_id=actor_user_id,
            draft=True,
        )
        self.apply(stored, body.plan)
        return stored, body.plan

    async def read(
        self, deployment_id: uuid.UUID, actor_user_id: uuid.UUID | None
    ) -> tuple[Deployment, DeploymentDraftPlan]:
        stored = await self._deployments.get(deployment_id)
        self.require_owner(stored, actor_user_id)
        return stored, self.open(stored.draft_plan)

    async def visible(self, deployment_id: uuid.UUID, actor_user_id: uuid.UUID | None) -> Deployment:
        """Hide another operator's draft on the ordinary deployment read."""
        stored = await self._deployments.get(deployment_id)
        if stored.status is DeploymentStatus.draft and (
            actor_user_id is None or stored.actor_user_id != actor_user_id
        ):
            raise NotFoundError(f"Deployment {deployment_id} not found")
        return stored

    async def update(
        self, deployment_id: uuid.UUID, body: DeploymentDraftWrite, actor_user_id: uuid.UUID | None
    ) -> tuple[Deployment, DeploymentDraftPlan]:
        stored = await self._deployments.get(deployment_id)
        self.require_owner(stored, actor_user_id)
        merged = merge_plan(self.open(stored.draft_plan), body.plan)
        if body.project_id is not None and body.environment_id is not None:
            await self._environments.get(body.project_id, body.environment_id)
            stored.project_id = body.project_id
            stored.environment_id = body.environment_id
        self.apply(stored, merged)
        return stored, merged

    async def discard(self, deployment_id: uuid.UUID, actor_user_id: uuid.UUID | None) -> Deployment:
        stored = await self._deployments.get(deployment_id)
        self.require_owner(stored, actor_user_id)
        stored.status = DeploymentStatus.cancelled
        stored.finished_at = datetime.now(UTC)
        stored.draft_step = None
        stored.draft_plan = None
        return stored

    async def launch(
        self, deployment_id: uuid.UUID, body: DeploymentDraftLaunch, actor_user_id: uuid.UUID | None
    ) -> Deployment:
        stored = await self._deployments.get(deployment_id)
        self.require_owner(stored, actor_user_id)
        merged = merge_plan(self.open(stored.draft_plan), body.plan)
        if merged.step != "launch":
            raise UnprocessableError("Confirm the launch step before deploying.")
        environment = await self._environments.get(body.project_id, body.environment_id)
        stored.project_id = body.project_id
        stored.environment_id = body.environment_id
        self.apply(stored, merged)
        await self._approval.enqueue_or_defer(
            environment,
            actor_user_id,
            stored.id,
            env_vars=plain_values(merged) or None,
            addons=list(merged.selected_addons) or None,
            port=merged.blueprint.build.port,
            service=merged.blueprint.build.service or None,
        )
        self.finish(stored)
        return stored

    def seal(self, plan: DeploymentDraftPlan) -> str:
        raw = plan.model_dump_json()
        if len(raw) > _MAX_PLAN_CHARS:
            raise UnprocessableError(_PLAN_TOO_LARGE)
        return encrypt_env_value(raw, self._signing_key)

    def open(self, token: str | None) -> DeploymentDraftPlan:
        if not token:
            raise ConflictError("Deployment draft has no saved plan.")
        try:
            raw = decrypt_env_value(token, self._signing_key)
            payload = json.loads(raw)
            return DeploymentDraftPlan.model_validate(payload)
        except (InvalidToken, json.JSONDecodeError, ValidationError, UnicodeError, ValueError) as exc:
            raise ConflictError("Deployment draft could not be read.") from exc

    def require_owner(self, deployment: Deployment, actor_user_id: uuid.UUID | None) -> None:
        if deployment.status is not DeploymentStatus.draft:
            raise ConflictError("Deployment is not a draft.")
        if actor_user_id is None or deployment.actor_user_id != actor_user_id:
            raise NotFoundError(f"Deployment {deployment.id} not found")

    def apply(self, deployment: Deployment, plan: DeploymentDraftPlan) -> None:
        deployment.draft_step = plan.step
        deployment.draft_plan = self.seal(plan)

    def finish(self, deployment: Deployment) -> None:
        """Leave draft after the existing provision path accepts the row."""
        if deployment.status is DeploymentStatus.draft:
            deployment.status = DeploymentStatus.running
        deployment.draft_step = None
        deployment.draft_plan = None
