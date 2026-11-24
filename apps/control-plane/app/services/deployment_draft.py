"""A deployment the operator can leave and reopen. Provision stays on launch (#343)."""

import asyncio
import json
import uuid
from datetime import UTC, datetime

from cryptography.fernet import InvalidToken
from pydantic import ValidationError

from starlette.concurrency import run_in_threadpool

from app.core.errors import AppError, ConflictError, NotFoundError, QueueUnavailableError, UnprocessableError
from app.core.logging import get_logger
from app.models.deployment import Deployment, DeploymentStatus, DeploymentTrigger
from app.schemas.deployment import DeploymentCreate, deployment_to_read
from app.schemas.draft_security import DraftSecurityRead, DraftSecurityRecord, SecurityAcknowledgement
from app.schemas.deployment_draft import (
    DeploymentDraftCreate,
    DeploymentDraftLaunch,
    DeploymentDraftPlan,
    DeploymentDraftRead,
    DeploymentDraftWrite,
    DraftVariable,
)
from app.services.advisory_source import AdvisorySource
from app.services.deploy_approval import DeployApprovalService
from app.services.deployments import DeploymentService
from app.services.environment_vars import decrypt_env_value, encrypt_env_value
from app.services.manifest_packages import PackageSource
from app.services.environments import EnvironmentService

logger = get_logger("services.deployment_draft")

_PLAN_TOO_LARGE = "Draft plan is too large."
_MAX_PLAN_CHARS = 64_000
# Covers the batch query and the detail fan-out. A stall becomes an incomplete scan.
_ADVISORY_DEADLINE_SECONDS = 30
# Launch reuses this scan. A later visit runs the scan again instead of cloning inside Deploy.
_SCAN_TTL_SECONDS = 15 * 60
_SCAN_MISSING = "Run the dependency scan before deploying."
_SCAN_STALE = "The dependency scan is out of date. Run it again before deploying."


def mask_plan(plan: DeploymentDraftPlan) -> DeploymentDraftPlan:
    """Drop secret values. ``redacted`` tells the client to keep the stored secret.

    ``extra_env`` is free-form and may hold secrets, so a read never returns it.
    An empty write keeps the sealed copy.
    """
    masked = plan.model_copy(deep=True)
    variables: list[DraftVariable] = []
    for item in masked.variables:
        if item.secret:
            variables.append(item.model_copy(update={"value": "", "redacted": True}))
        else:
            variables.append(item.model_copy(update={"redacted": False}))
    masked.variables = variables
    masked.extra_env = ""
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
    extra_env = incoming.extra_env if incoming.extra_env.strip() else stored.extra_env
    return incoming.model_copy(update={"variables": merged, "extra_env": extra_env})


def security_read(record: DraftSecurityRecord) -> DraftSecurityRead:
    return DraftSecurityRead(
        outcome=record.outcome,
        findings=record.findings,
        acknowledgement=record.acknowledgement,
    )


def _load_security(deployment: Deployment) -> DraftSecurityRecord | None:
    if not deployment.draft_security:
        return None
    try:
        return DraftSecurityRecord.model_validate_json(deployment.draft_security)
    except ValidationError:
        return None


def _launch_scan(deployment: Deployment) -> DraftSecurityRecord:
    """The scan Deploy is allowed to trust. Clone and OSV stay on the scan route."""
    record = _load_security(deployment)
    if record is None or record.scanned_at is None:
        raise UnprocessableError(_SCAN_MISSING)
    scanned_at = record.scanned_at
    if scanned_at.tzinfo is None:
        scanned_at = scanned_at.replace(tzinfo=UTC)
    if (datetime.now(UTC) - scanned_at).total_seconds() > _SCAN_TTL_SECONDS:
        raise UnprocessableError(_SCAN_STALE)
    return record


def _keep_acknowledgement(
    previous: DraftSecurityRecord | None, record: DraftSecurityRecord
) -> DraftSecurityRecord:
    if previous is None or previous.acknowledgement is None:
        return record
    same_findings = previous.outcome == "findings" and record.outcome == "findings" and {
        item.advisory_id for item in previous.findings
    } == {item.advisory_id for item in record.findings}
    same_incomplete = previous.acknowledgement == "incomplete" and record.outcome == "incomplete"
    if (previous.acknowledgement == "findings" and same_findings) or same_incomplete:
        return record.model_copy(
            update={
                "acknowledgement": previous.acknowledgement,
                "acknowledged_at": previous.acknowledged_at,
                "acknowledged_by": previous.acknowledged_by,
            }
        )
    return record


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
        packages: PackageSource,
        advisories: AdvisorySource,
    ) -> None:
        self._signing_key = signing_key
        self._deployments = deployments
        self._environments = environments
        self._approval = approval
        self._packages = packages
        self._advisories = advisories

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
        stored.draft_security = None
        return stored

    async def read_security(
        self, deployment_id: uuid.UUID, actor_user_id: uuid.UUID | None
    ) -> DraftSecurityRecord:
        stored = await self._deployments.get(deployment_id)
        self.require_owner(stored, actor_user_id)
        record = _load_security(stored)
        if record is None:
            raise NotFoundError("Dependency scan has not been run.")
        return record

    async def scan_security(
        self, deployment_id: uuid.UUID, actor_user_id: uuid.UUID | None
    ) -> DraftSecurityRecord:
        _stored, record = await self._rescan(deployment_id, actor_user_id)
        return record

    async def _rescan(
        self, deployment_id: uuid.UUID, actor_user_id: uuid.UUID | None
    ) -> tuple[Deployment, DraftSecurityRecord]:
        """Read, release the connection, scan, then write. The pool stays free during clone and OSV."""
        stored = await self._deployments.get(deployment_id)
        self.require_owner(stored, actor_user_id)
        repo_url = stored.repo_url
        previous = _load_security(stored)
        await self._deployments.commit()
        record = _keep_acknowledgement(previous, await self._scan_record(repo_url))
        record = record.model_copy(update={"scanned_at": datetime.now(UTC)})
        stored = await self._deployments.get(deployment_id)
        self.require_owner(stored, actor_user_id)
        stored.draft_security = record.model_dump_json()
        await self._deployments.commit()
        return stored, record

    async def acknowledge_security(
        self,
        deployment_id: uuid.UUID,
        acknowledgement: SecurityAcknowledgement,
        actor_user_id: uuid.UUID | None,
    ) -> DraftSecurityRecord:
        stored = await self._deployments.get(deployment_id)
        self.require_owner(stored, actor_user_id)
        record = _load_security(stored)
        if record is None:
            raise UnprocessableError("Run the dependency scan before acknowledging it.")
        if acknowledgement == "findings" and record.outcome != "findings":
            raise UnprocessableError("There are no dependency findings to acknowledge.")
        if acknowledgement == "incomplete" and record.outcome != "incomplete":
            raise UnprocessableError("The dependency scan is not incomplete.")
        updated = record.model_copy(
            update={
                "acknowledgement": acknowledgement,
                "acknowledged_at": datetime.now(UTC),
                "acknowledged_by": actor_user_id,
            }
        )
        stored.draft_security = updated.model_dump_json()
        return updated

    async def _scan_record(self, repo_url: str) -> DraftSecurityRecord:
        try:
            read = await run_in_threadpool(self._packages.read, repo_url)
        except Exception as exc:
            logger.warning(
                "event=dependency_scan_incomplete stage=manifest error_type=%s",
                type(exc).__name__,
                exc_info=exc,
            )
            return DraftSecurityRecord(outcome="incomplete")
        if not read.found:
            return DraftSecurityRecord(outcome="no_manifest")
        if not read.packages:
            return DraftSecurityRecord(outcome="incomplete" if read.unpinned else "no_advisories")
        try:
            findings = await asyncio.wait_for(
                self._advisories.query(list(read.packages)),
                timeout=_ADVISORY_DEADLINE_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                "event=dependency_scan_incomplete stage=advisory error_type=%s",
                type(exc).__name__,
                exc_info=exc,
            )
            return DraftSecurityRecord(outcome="incomplete")
        if findings:
            return DraftSecurityRecord(outcome="findings", findings=findings)
        if read.unpinned:
            return DraftSecurityRecord(outcome="incomplete")
        return DraftSecurityRecord(outcome="no_advisories")

    async def launch(
        self, deployment_id: uuid.UUID, body: DeploymentDraftLaunch, actor_user_id: uuid.UUID | None
    ) -> Deployment:
        stored = await self._deployments.get(deployment_id)
        self.require_owner(stored, actor_user_id)
        merged = merge_plan(self.open(stored.draft_plan), body.plan)
        if merged.step != "launch":
            raise UnprocessableError("Confirm the launch step before deploying.")
        record = _launch_scan(stored)
        if record.outcome == "findings" and record.acknowledgement != "findings":
            raise UnprocessableError("Acknowledge the dependency findings before deploying.")
        if record.outcome == "incomplete" and record.acknowledgement != "incomplete":
            raise UnprocessableError("The dependency scan did not complete.")
        environment = await self._environments.get(body.project_id, body.environment_id)
        stored.project_id = body.project_id
        stored.environment_id = body.environment_id
        try:
            await self._approval.enqueue_or_defer(
                environment,
                actor_user_id,
                stored.id,
                env_vars=plain_values(merged) or None,
                addons=list(merged.selected_addons) or None,
                port=merged.blueprint.build.port,
                service=merged.blueprint.build.service or None,
            )
        except AppError:
            raise
        except Exception as exc:
            raise QueueUnavailableError("Provision could not be queued.") from exc
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
        """Leave draft only after provision was queued or held for approval."""
        if deployment.status is DeploymentStatus.draft:
            deployment.status = DeploymentStatus.running
        deployment.draft_step = None
        deployment.draft_plan = None
        deployment.draft_security = None
