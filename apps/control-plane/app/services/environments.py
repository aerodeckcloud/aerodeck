import uuid
from collections.abc import Sequence
from typing import Protocol

from sqlalchemy.exc import IntegrityError

from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models.project import Environment, EnvironmentKind
from app.repositories.environments import EnvironmentRepository
from app.schemas.environment import EnvironmentCreate, EnvironmentUpdate
from app.services.project_members import ForbiddenError, ProjectMemberService
from app.services.projects import ProjectService

_OWNER_ONLY_FIELDS = frozenset({"auto_deploy_paused", "prod_deploy_approval"})


class EnvironmentDeploymentStore(Protocol):
    async def clear_environment_link(self, environment_id: uuid.UUID) -> None: ...

    async def count_retained_images(
        self, environment_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, int]: ...


logger = get_logger("services.environments")


def _owner_required(current_kind: EnvironmentKind, data: EnvironmentUpdate) -> bool:
    """Pause, production approval, and moving into or out of prod are owner-only (#276)."""
    if _OWNER_ONLY_FIELDS & data.model_fields_set:
        return True
    if data.kind is None or data.kind == current_kind:
        return False
    return data.kind == EnvironmentKind.prod or current_kind == EnvironmentKind.prod


class EnvironmentService:
    def __init__(
        self,
        environments: EnvironmentRepository,
        projects: ProjectService,
        deployments: EnvironmentDeploymentStore,
    ) -> None:
        self._environments = environments
        self._projects = projects
        self._deployments = deployments

    async def create(self, project_id: uuid.UUID, data: EnvironmentCreate) -> Environment:
        project = await self._projects.get(project_id)
        if await self._environments.get_by_slug(project.id, data.slug) is not None:
            raise ConflictError(f"Environment slug {data.slug!r} already exists in project {project.slug!r}")
        try:
            environment = await self._environments.add(Environment(project_id=project.id, **data.model_dump()))
        except IntegrityError as exc:
            raise ConflictError(f"Environment slug {data.slug!r} already exists in project {project.slug!r}") from exc
        logger.info("created environment %s/%s (%s)", project.slug, environment.slug, environment.id)
        return environment

    async def get(self, project_id: uuid.UUID, environment_id: uuid.UUID) -> Environment:
        await self._projects.get(project_id)
        environment = await self._environments.get(project_id, environment_id)
        if environment is None:
            raise NotFoundError(f"Environment {environment_id} not found in project {project_id}")
        return environment

    async def lock_for_deploy(self, environment_id: uuid.UUID) -> None:
        """Serialise deploy overlap decisions for one environment (#271)."""
        await self._environments.lock_for_deploy(environment_id)

    async def list(self, project_id: uuid.UUID, *, limit: int, offset: int) -> tuple[Sequence[Environment], int]:
        await self._projects.get(project_id)
        return await self._environments.list(project_id, limit=limit, offset=offset)

    async def retained_rollback_image_counts(
        self, environment_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """How many succeeded images each environment has not pruned (#274)."""
        return await self._deployments.count_retained_images(environment_ids)

    async def update(
        self,
        project_id: uuid.UUID,
        environment_id: uuid.UUID,
        data: EnvironmentUpdate,
        *,
        actor_user_id: uuid.UUID | None,
        members: ProjectMemberService,
    ) -> Environment:
        environment = await self.get(project_id, environment_id)
        if _owner_required(environment.kind, data) and (
            actor_user_id is None or not await members.is_owner(project_id, actor_user_id)
        ):
            raise ForbiddenError(f"Requires owner role on project {project_id}.")
        changes = data.model_dump(exclude_unset=True)
        # Entering production turns approval on unless an owner set the flag in this request.
        entering_prod = data.kind == EnvironmentKind.prod and environment.kind != EnvironmentKind.prod
        if entering_prod and "prod_deploy_approval" not in data.model_fields_set:
            changes["prod_deploy_approval"] = True
        for field, value in changes.items():
            setattr(environment, field, value)
        return environment

    async def delete(self, project_id: uuid.UUID, environment_id: uuid.UUID) -> None:
        environment = await self.get(project_id, environment_id)
        # Clear both columns before the row goes away so the pair invariant holds
        # even when the DB composite SET NULL is incomplete (e.g. SQLite quirks).
        await self._deployments.clear_environment_link(environment.id)
        await self._environments.delete(environment)
        logger.info("deleted environment %s (%s)", environment.slug, environment.id)
