"""Roll back to a succeeded deployment's image and its variable snapshot (#273)."""

from __future__ import annotations

import uuid

from app.core.errors import IMAGE_NOT_RETAINED_DETAIL, ImageNotRetainedError, UnprocessableError
from app.core.logging import get_logger
from app.models.deployment import Deployment, DeploymentStatus, DeploymentTrigger
from app.models.project_member import ProjectRole
from app.schemas.deployment import DeploymentCreate
from app.services.deployments import DeploymentService
from app.services.environments import EnvironmentService
from app.services.image_presence import ImageLookup, RetainedImage
from app.services.jobs import JobService
from app.services.project_members import ProjectMemberService
from app.services.provisioning import JobKind

logger = get_logger("services.rollbacks")


class RollbackService:
    def __init__(
        self,
        deployments: DeploymentService,
        members: ProjectMemberService,
        jobs: JobService,
        environments: EnvironmentService,
        images: ImageLookup,
    ) -> None:
        self._deployments = deployments
        self._members = members
        self._jobs = jobs
        self._environments = environments
        self._images = images

    async def rollback(
        self,
        deployment_id: uuid.UUID,
        *,
        restore_variables: bool,
        actor_user_id: uuid.UUID | None,
        recorded_actor_id: uuid.UUID | None,
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

        # Image-only rollback still retags the live environment, so the same role
        # applies whether or not variables are restored (#273).
        await self._members.require_role(source.project_id, actor_user_id, ProjectRole.developer)
        if await self._image_was_removed(source):
            raise ImageNotRetainedError(IMAGE_NOT_RETAINED_DETAIL)

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
            actor_user_id=recorded_actor_id,
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

    async def _image_was_removed(self, source: Deployment) -> bool:
        """True when retention removed the tag, or the host says it is gone."""
        if source.image_pruned_at is not None:
            return True
        if source.project_id is None or source.environment_id is None:
            return False
        environment = await self._environments.get(source.project_id, source.environment_id)
        present = await self._images.is_present(
            RetainedImage(
                project_id=source.project_id,
                project_slug=environment.project.slug,
                environment_id=environment.id,
                environment_slug=environment.slug,
                deployment_id=source.id,
            )
        )
        return present is False
