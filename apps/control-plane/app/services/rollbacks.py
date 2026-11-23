"""Roll back to a succeeded deployment's image and its variable snapshot (#273)."""

from __future__ import annotations

import uuid

from app.core.errors import UnprocessableError
from app.core.logging import get_logger
from app.models.deployment import Deployment, DeploymentStatus, DeploymentTrigger
from app.schemas.deployment import DeploymentCreate
from app.services.deployments import DeploymentService
from app.services.environments import EnvironmentService
from app.services.jobs import JobService
from app.services.provisioning import JobKind

logger = get_logger("services.rollbacks")


class RollbackService:
    def __init__(
        self,
        deployments: DeploymentService,
        environments: EnvironmentService,
        jobs: JobService,
    ) -> None:
        self._deployments = deployments
        self._environments = environments
        self._jobs = jobs

    async def rollback(
        self,
        deployment_id: uuid.UUID,
        *,
        restore_variables: bool,
        actor_user_id: uuid.UUID | None,
    ) -> tuple[Deployment, bool]:
        """Enqueue the image retag. Apply the snapshot only when that job is dispatched.

        The snapshot is passed as ``replace_env_vars`` so the worker gets the release
        map without mutating the live environment first. A rollback that only waits
        stores the sealed map on the replay; cancel leaves variables unchanged (#273).
        """
        source = await self._deployments.get(deployment_id)
        if source.project_id is None or source.environment_id is None:
            raise UnprocessableError(
                "Cannot roll back a deployment that is not linked to an environment."
            )
        if source.status is not DeploymentStatus.succeeded:
            raise UnprocessableError("Only succeeded deployments can be rolled back to.")

        await self._environments.lock_for_deploy(source.environment_id)

        entries = None
        if restore_variables:
            entries = self._deployments.read_snapshot_entries(source)

        created = await self._deployments.create(
            DeploymentCreate(
                repo_url=source.repo_url,
                project_id=source.project_id,
                environment_id=source.environment_id,
            ),
            trigger=DeploymentTrigger.rollback,
            actor_user_id=actor_user_id,
        )
        replace_env_vars = entries if entries is not None else None
        ticket = await self._jobs.enqueue(
            JobKind.provision,
            source.project_id,
            source.environment_id,
            deployment_id=created.id,
            rollback_from_deployment_id=source.id,
            replace_env_vars=replace_env_vars,
        )
        # A held rollback keeps the sealed map on the replay and does not touch
        # the live variables until that job is actually dispatched.
        variables_restored = ticket.job_id is not None and entries is not None

        logger.info(
            "event=deployment_rollback source=%s deployment=%s variables_restored=%s",
            source.id,
            created.id,
            variables_restored,
        )
        stored = await self._deployments.get(created.id)
        return stored, variables_restored
