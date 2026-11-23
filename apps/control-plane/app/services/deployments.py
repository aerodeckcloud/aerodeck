import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.exc import IntegrityError

from app.core.errors import ConflictError, NotFoundError
from app.core.github import canonical_github_repo_url, parse_github_repo
from app.core.logging import get_logger
from app.models.deployment import (
    DEPLOYMENT_ENVIRONMENT_FK,
    Deployment,
    DeploymentStatus,
    DeploymentTrigger,
)
from app.models.project import Environment
from app.repositories.deployments import DeploymentRepository
from app.schemas.deployment import DeploymentCreate
from app.services.deployment_terminal import DeploymentTerminalService

logger = get_logger("services.deployments")

NOT_STARTED_DETAIL = "Deployment is waiting for another deployment and has not started."


def is_waiting(deployment: Deployment) -> bool:
    """True while the row is held behind another deployment (#271)."""
    return (
        deployment.status == DeploymentStatus.queued
        and deployment.held_for_deployment_id is not None
    )


def _is_deployment_environment_fk_violation(exc: IntegrityError) -> bool:
    """True when the failing constraint is the deployment↔environment composite FK.

    Postgres includes ``DEPLOYMENT_ENVIRONMENT_FK`` in the error. SQLite only says
    ``FOREIGN KEY constraint failed``; that fallback is safe only while
    ``deployments`` has no other FKs (see model). Callers must already require both
    link ids so unlinked creates never hit this path.
    """
    detail = str(exc.orig if exc.orig is not None else exc).lower()
    if DEPLOYMENT_ENVIRONMENT_FK.lower() in detail:
        return True
    orig = exc.orig
    if orig is not None and type(orig).__module__.startswith("sqlite3"):
        return "foreign key" in detail
    return False


class EnvironmentLookup(Protocol):
    """Create-time check that the environment exists, plus its deploy lock."""

    async def get(self, project_id: uuid.UUID, environment_id: uuid.UUID) -> Environment: ...

    async def lock_for_deploy(self, environment_id: uuid.UUID) -> None: ...


class DeploymentService:
    def __init__(
        self,
        deployments: DeploymentRepository,
        environments: EnvironmentLookup,
        terminal: DeploymentTerminalService,
    ) -> None:
        self._deployments = deployments
        self._environments = environments
        self._terminal = terminal

    async def create(
        self,
        data: DeploymentCreate,
        *,
        trigger: DeploymentTrigger | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> Deployment:
        parsed = parse_github_repo(data.repo_url)
        if parsed is None:
            raise ValueError("repo_url must be a GitHub repository URL")
        owner, repo = parsed
        project_id = data.project_id
        environment_id = data.environment_id
        if project_id is not None and environment_id is not None:
            await self._environments.get(project_id, environment_id)
        try:
            stored = await self._deployments.add(
                Deployment(
                    repo_url=canonical_github_repo_url(owner, repo),
                    owner=owner,
                    repo=repo,
                    project_id=project_id,
                    environment_id=environment_id,
                    status=DeploymentStatus.running,
                    started_at=datetime.now(UTC),
                    trigger=trigger.value if trigger is not None else None,
                    actor_user_id=actor_user_id,
                )
            )
        except IntegrityError as exc:
            # Env/project removed between lookup and insert (composite FK only).
            if (
                project_id is not None
                and environment_id is not None
                and _is_deployment_environment_fk_violation(exc)
            ):
                raise NotFoundError(
                    f"Environment {environment_id} not found in project {project_id}"
                ) from exc
            raise
        logger.info("created deployment %s", stored.id)
        return stored

    async def get(self, deployment_id: uuid.UUID) -> Deployment:
        deployment = await self._deployments.get(deployment_id)
        if deployment is None:
            raise NotFoundError(f"Deployment {deployment_id} not found")
        return deployment

    async def list(self, *, limit: int, offset: int) -> tuple[Sequence[Deployment], int]:
        return await self._deployments.list(limit=limit, offset=offset)

    async def list_for_project(
        self,
        project_id: uuid.UUID,
        *,
        environment_id: uuid.UUID | None = None,
        limit: int,
        offset: int,
    ) -> tuple[Sequence[Deployment], int]:
        return await self._deployments.list_for_project(
            project_id, environment_id=environment_id, limit=limit, offset=offset
        )

    async def mark_succeeded(self, deployment_id: uuid.UUID) -> Deployment:
        return await self._mark_terminal(deployment_id, DeploymentStatus.succeeded)

    async def mark_failed(self, deployment_id: uuid.UUID) -> Deployment:
        return await self._mark_terminal(deployment_id, DeploymentStatus.failed)

    async def cancel(self, deployment_id: uuid.UUID) -> Deployment:
        await self._lock_environment_of(deployment_id)
        return await self._terminal.cancel(deployment_id)

    # One deployment at a time (#271). JobService decides; these keep it off the repository.

    async def list_active(
        self, environment_id: uuid.UUID, exclude_id: uuid.UUID
    ) -> Sequence[Deployment]:
        return await self._deployments.list_active(environment_id, exclude_id)

    async def mark_dispatched(self, deployment_id: uuid.UUID) -> None:
        await self._deployments.mark_dispatched(deployment_id, datetime.now(UTC))

    async def mark_waiting(
        self, deployment_id: uuid.UUID, active_id: uuid.UUID, replay_json: str | None
    ) -> None:
        await self._deployments.mark_waiting(deployment_id, active_id, replay_json)

    async def cancel_waiting(
        self, environment_id: uuid.UUID, *, keep_id: uuid.UUID | None
    ) -> int:
        return await self._deployments.cancel_waiting(
            environment_id, datetime.now(UTC), keep_id=keep_id
        )

    async def claim_newest_waiter(
        self, environment_id: uuid.UUID, finished_id: uuid.UUID
    ) -> tuple[Deployment, str | None] | None:
        return await self._deployments.claim_newest_waiter(
            environment_id, finished_id, datetime.now(UTC)
        )

    async def commit(self) -> None:
        await self._deployments.commit()

    async def refresh(self, deployment: Deployment) -> None:
        await self._deployments.refresh(deployment)

    async def _lock_environment_of(self, deployment_id: uuid.UUID) -> Deployment | None:
        """Take the environment lock before writing the deployment row.

        Enqueue locks the environment and then writes deployment rows. A status
        change that wrote its row first and asked for the lock second could
        deadlock against it on PostgreSQL, so both use the same order.
        Returns the row as it is once the lock is held.
        """
        current = await self._deployments.get(deployment_id)
        if current is not None and current.environment_id is not None:
            await self._environments.lock_for_deploy(current.environment_id)
            # Another request may have changed the row while this one waited.
            await self._deployments.refresh(current)
        return current

    async def _mark_terminal(
        self, deployment_id: uuid.UUID, status: DeploymentStatus
    ) -> Deployment:
        current = await self._lock_environment_of(deployment_id)
        if current is not None and is_waiting(current):
            # Nothing has run yet, so there is no outcome to record. Without this
            # a console that reported one would close the row and it would never start.
            raise ConflictError(NOT_STARTED_DETAIL)
        stored = await self._terminal.mark(deployment_id, status)
        if stored is None:
            raise NotFoundError(f"Deployment {deployment_id} not found")
        return stored
