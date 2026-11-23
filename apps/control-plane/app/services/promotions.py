"""Promote a succeeded deployment's image to another environment (#272).

The target environment runs the image the source deployment built. Nothing is
cloned or rebuilt, and the target keeps its own variables. The target's
overlap setting decides whether the promotion starts, waits, or is refused,
exactly as for any other deploy.
"""

import uuid

from app.core.errors import NotFoundError, UnprocessableError
from app.core.logging import get_logger
from app.models.deployment import Deployment, DeploymentStatus, DeploymentTrigger
from app.models.project import Environment, EnvironmentKind
from app.models.project_member import ProjectRole
from app.schemas.deployment import DeploymentCreate
from app.services.deployments import DeploymentService
from app.services.docker_policy import normalize_git_sha
from app.services.environments import EnvironmentService
from app.services.image_presence import ImageLookup, RetainedImage
from app.services.jobs import JobService
from app.services.project_members import ProjectMemberService
from app.services.provisioning import JobKind

logger = get_logger("services.promotions")


class PromotionService:
    def __init__(
        self,
        deployments: DeploymentService,
        environments: EnvironmentService,
        members: ProjectMemberService,
        jobs: JobService,
        images: ImageLookup,
    ) -> None:
        self._deployments = deployments
        self._environments = environments
        self._members = members
        self._jobs = jobs
        self._images = images

    async def promote(
        self,
        deployment_id: uuid.UUID,
        target_environment_id: uuid.UUID,
        *,
        actor_user_id: uuid.UUID | None,
        recorded_actor_id: uuid.UUID | None,
    ) -> Deployment:
        """Create a deployment on the target that runs the source's image.

        ``actor_user_id`` is who asked, and is what the role check uses.
        ``recorded_actor_id`` is stored on the new row, and is set only when
        that user exists.
        """
        source = await self._deployments.get(deployment_id)
        if source.project_id is None or source.environment_id is None:
            raise UnprocessableError("Cannot promote a deployment that is not linked to an environment.")
        target = await self._target(source.project_id, source.environment_id, target_environment_id)
        # Production is the owner's call. Anything else needs a developer.
        minimum = ProjectRole.owner if target.kind is EnvironmentKind.prod else ProjectRole.developer
        await self._members.require_role(source.project_id, actor_user_id, minimum)

        if source.status is not DeploymentStatus.succeeded:
            raise UnprocessableError("Only succeeded deployments can be promoted.")
        sha = normalize_git_sha(source.git_sha)
        if sha is None:
            # No recorded commit means this row never built an image of its own.
            raise UnprocessableError("This deployment has no retained image to promote.")
        built_in = await self._environments.get(source.project_id, source.environment_id)
        present = await self._images.is_present(
            RetainedImage(
                project_id=source.project_id,
                project_slug=built_in.project.slug,
                environment_id=built_in.id,
                environment_slug=built_in.slug,
                deployment_id=source.id,
            )
        )
        if present is False:
            raise UnprocessableError(
                "The image of this deployment is no longer on the host. Deploy the commit instead."
            )

        created = await self._deployments.create(
            DeploymentCreate(
                repo_url=source.repo_url,
                project_id=source.project_id,
                environment_id=target.id,
            ),
            trigger=DeploymentTrigger.promote,
            actor_user_id=recorded_actor_id,
            git_sha=sha,
            git_ref=source.git_ref,
        )
        await self._jobs.enqueue(
            JobKind.provision,
            source.project_id,
            target.id,
            deployment_id=created.id,
            promote_from_deployment_id=source.id,
        )
        logger.info(
            "event=deployment_promoted source=%s deployment=%s environment=%s",
            source.id,
            created.id,
            target.id,
        )
        stored = await self._deployments.get(created.id)
        # Enqueue may have left the row waiting behind the target's active deploy.
        await self._deployments.refresh(stored)
        return stored

    async def _target(
        self, project_id: uuid.UUID, source_environment_id: uuid.UUID, target_environment_id: uuid.UUID
    ) -> Environment:
        if target_environment_id == source_environment_id:
            raise UnprocessableError(
                "Choose another environment. To run this release here again, use Rollback."
            )
        try:
            return await self._environments.get(project_id, target_environment_id)
        except NotFoundError as exc:
            raise UnprocessableError(
                "The target environment is not in this deployment's project."
            ) from exc
