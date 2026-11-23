"""A deployment the operator can leave and reopen. Provision stays on launch (#343)."""

import json
import uuid

from cryptography.fernet import InvalidToken
from pydantic import ValidationError

from app.core.errors import ConflictError, NotFoundError, UnprocessableError
from app.models.deployment import Deployment, DeploymentStatus
from app.schemas.deployment_draft import DeploymentDraftPlan, DraftVariable
from app.services.environment_vars import decrypt_env_value, encrypt_env_value

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
    def __init__(self, signing_key: str) -> None:
        self._signing_key = signing_key

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
        except (InvalidToken, json.JSONDecodeError, ValidationError, UnicodeError, ValueError) as exc:
            raise ConflictError("Deployment draft could not be read.") from exc
        return DeploymentDraftPlan.model_validate(payload)

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
