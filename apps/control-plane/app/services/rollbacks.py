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
        """Restore the release snapshot when asked, then enqueue the image retag.

        The environment deploy lock is held across restore and enqueue so the
        variable mutation and the job's env load cannot interleave with another
        provision. If the queue refuses the job after a restore, the prior
        variable map is written back and committed before the error propagates.
        """
        source = await self._deployments.get(deployment_id)
        if source.project_id is None or source.environment_id is None:
            raise UnprocessableError(
                "Cannot roll back a deployment that is not linked to an environment."
            )
        if source.status is not DeploymentStatus.succeeded:
            raise UnprocessableError("Only succeeded deployments can be rolled back to.")

        # Serialize restore with enqueue's meta load (same transaction lock).
        await self._environments.lock_for_deploy(source.environment_id)

        prior: dict[str, str] | None = None
        variables_restored = False
        if restore_variables:
            variables_restored, prior = await self._deployments.restore_variables_from_snapshot(
                source
            )

        created = await self._deployments.create(
            DeploymentCreate(
                repo_url=source.repo_url,
                project_id=source.project_id,
                environment_id=source.environment_id,
            ),
            trigger=DeploymentTrigger.rollback,
            actor_user_id=actor_user_id,
        )
        try:
            await self._jobs.enqueue(
                JobKind.provision,
                source.project_id,
                source.environment_id,
                deployment_id=created.id,
                rollback_from_deployment_id=source.id,
            )
        except Exception:
            if prior is not None:
                await self._deployments.reinstate_variables(
                    source.project_id, source.environment_id, prior
                )
            raise

        logger.info(
            "event=deployment_rollback source=%s deployment=%s variables_restored=%s",
            source.id,
            created.id,
            variables_restored,
        )
        stored = await self._deployments.get(created.id)
        return stored, variables_restored
