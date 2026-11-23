"""Deployment field updates without ``EnvironmentLookup``.

Covers sticky terminal status (succeeded/failed) and last ``provision_job_id``.
Used by ``DeploymentService`` for API succeed/fail and by Celery/isolated
bridges so repositories stay persistence-only. Class name is historical;
prefer this module for any deployment mutation that must not pull environments.
"""

import uuid
from datetime import UTC, datetime
from typing import Protocol

from app.core.errors import ConflictError, NotFoundError, UnprocessableError
from app.core.logging import get_logger
from app.models.deployment import Deployment, DeploymentStatus
from app.services.docker_policy import normalize_git_sha, validate_git_ref
from app.services.provisioning import PermanentJobError

logger = get_logger("services.deployment_terminal")


class DeploymentStore(Protocol):
    async def get(self, deployment_id: uuid.UUID) -> Deployment | None: ...

    async def save(self, deployment: Deployment) -> Deployment: ...

    async def cancel_before_swap(
        self, deployment_id: uuid.UUID, finished_at: datetime
    ) -> Deployment | None: ...

    async def begin_swap(self, deployment_id: uuid.UUID, started_at: datetime) -> bool: ...


class DeploymentTerminalService:
    """Mutate deployment rows (status, provision job id) via a narrow store protocol."""

    def __init__(self, deployments: DeploymentStore) -> None:
        self._deployments = deployments

    async def mark(
        self, deployment_id: uuid.UUID, status: DeploymentStatus
    ) -> Deployment | None:
        """Set succeeded/failed if not already terminal. ``None`` if the row is missing."""
        deployment = await self._deployments.get(deployment_id)
        if deployment is None:
            return None
        if deployment.status in (
            DeploymentStatus.succeeded,
            DeploymentStatus.failed,
            DeploymentStatus.cancelled,
        ):
            logger.info(
                "deployment %s already %s; leave unchanged",
                deployment_id,
                deployment.status.value,
            )
            return deployment
        deployment.status = status
        # A finished row never starts, so what it kept for a later start goes too (#271).
        deployment.provision_replay_json = None
        if deployment.finished_at is None:
            deployment.finished_at = datetime.now(UTC)
        stored = await self._deployments.save(deployment)
        logger.info("deployment %s %s", stored.id, status.value)
        return stored

    async def record_release(
        self,
        deployment_id: uuid.UUID,
        *,
        git_sha: str | None = None,
        git_ref: str | None = None,
        finished: bool = False,
    ) -> Deployment | None:
        """Store checkout identity and finish time. Ignores a SHA that is not 40 hex chars."""
        deployment = await self._deployments.get(deployment_id)
        if deployment is None:
            return None
        sha = normalize_git_sha(git_sha)
        if sha is not None and deployment.git_sha is None:
            deployment.git_sha = sha
        if git_ref and deployment.git_ref is None:
            try:
                cleaned_ref = validate_git_ref(git_ref)
            except PermanentJobError:
                cleaned_ref = None
            if cleaned_ref is not None:
                deployment.git_ref = cleaned_ref
        if finished and deployment.finished_at is None:
            deployment.finished_at = datetime.now(UTC)
        stored = await self._deployments.save(deployment)
        logger.info("deployment %s release recorded", stored.id)
        return stored

    async def cancel(self, deployment_id: uuid.UUID) -> Deployment:
        """Cancel before cutover. 409 once swap has started, 422 when already terminal."""
        finished_at = datetime.now(UTC)
        stored = await self._deployments.cancel_before_swap(deployment_id, finished_at)
        if stored is not None:
            logger.info("deployment %s cancelled", stored.id)
            return stored
        current = await self._deployments.get(deployment_id)
        if current is None:
            raise NotFoundError(f"Deployment {deployment_id} not found")
        if current.swap_started_at is not None and current.status in (
            DeploymentStatus.queued,
            DeploymentStatus.running,
        ):
            raise ConflictError("Container swap already started.")
        raise UnprocessableError("Only a queued or running deployment can be cancelled.")

    async def begin_swap(self, deployment_id: uuid.UUID) -> bool:
        """Return False when a later job must not touch the live container."""
        allowed = await self._deployments.begin_swap(deployment_id, datetime.now(UTC))
        if not allowed:
            logger.info("deployment %s swap skipped", deployment_id)
        return allowed

    async def set_provision_job_id(
        self, deployment_id: uuid.UUID, job_id: uuid.UUID
    ) -> Deployment | None:
        """Remember the last successful provision enqueue. ``None`` if the row is missing."""
        deployment = await self._deployments.get(deployment_id)
        if deployment is None:
            return None
        deployment.provision_job_id = job_id
        stored = await self._deployments.save(deployment)
        logger.info("deployment %s provision_job_id=%s", stored.id, job_id)
        return stored
