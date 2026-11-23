"""Hold a production deploy until an owner approves it, and skip paused auto-deploys (#276).

A project with no members has nobody who can approve, so those deploys still
enqueue. That keeps a fresh project working and keeps member-less tests on the
old path. Once a project has members, a developer or a webhook waits; an owner
does not.
"""

import uuid

from app.core.errors import ConflictError, NotFoundError
from app.models.deployment import Deployment
from app.models.project import Environment, EnvironmentKind
from app.services.audit_logs import AuditLogService
from app.services.deployments import DeploymentService, is_awaiting_approval
from app.services.environment_vars import EnvVarPlain
from app.services.jobs import JobService, JobTicket
from app.services.project_members import ForbiddenError, ProjectMemberService
from app.services.provisioning import JobKind

AUTO_DEPLOY_SKIPPED = "deployment.auto_deploy_skipped"


async def needs_production_approval(
    members: ProjectMemberService,
    environment: Environment,
    actor_user_id: uuid.UUID | None,
) -> bool:
    if environment.kind is not EnvironmentKind.prod or not environment.prod_deploy_approval:
        return False
    if not await members.has_members(environment.project_id):
        return False
    return not await members.is_owner(environment.project_id, actor_user_id)


class DeployApprovalService:
    def __init__(
        self,
        members: ProjectMemberService,
        jobs: JobService,
        deployments: DeploymentService,
        audit: AuditLogService,
    ) -> None:
        self._members = members
        self._jobs = jobs
        self._deployments = deployments
        self._audit = audit

    async def needs(self, environment: Environment, actor_user_id: uuid.UUID | None) -> bool:
        return await needs_production_approval(self._members, environment, actor_user_id)

    async def enqueue_or_defer(
        self,
        environment: Environment,
        actor_user_id: uuid.UUID | None,
        deployment_id: uuid.UUID,
        *,
        git_ref: str | None = None,
        addons: list[str] | None = None,
        env_vars: dict[str, str] | None = None,
        rollback_from_deployment_id: uuid.UUID | None = None,
        promote_from_deployment_id: uuid.UUID | None = None,
        replace_env_vars: dict[str, EnvVarPlain] | None = None,
    ) -> JobTicket | None:
        """Start the provision, or hold it for an owner.

        ``None`` means the row is waiting for approval and nothing was dispatched.
        A row that is already waiting is left as it is.
        """
        stored = await self._deployments.get(deployment_id)
        if is_awaiting_approval(stored):
            return None
        if await self.needs(environment, actor_user_id):
            await self.defer(
                deployment_id,
                git_ref=git_ref,
                addons=addons,
                env_vars=env_vars,
                rollback_from_deployment_id=rollback_from_deployment_id,
                promote_from_deployment_id=promote_from_deployment_id,
                replace_env_vars=replace_env_vars,
            )
            return None
        return await self._jobs.enqueue(
            JobKind.provision,
            environment.project_id,
            environment.id,
            deployment_id=deployment_id,
            git_ref=git_ref,
            addons=addons,
            env_vars=env_vars,
            rollback_from_deployment_id=rollback_from_deployment_id,
            promote_from_deployment_id=promote_from_deployment_id,
            replace_env_vars=replace_env_vars,
        )

    async def defer(
        self,
        deployment_id: uuid.UUID,
        *,
        git_ref: str | None = None,
        addons: list[str] | None = None,
        env_vars: dict[str, str] | None = None,
        rollback_from_deployment_id: uuid.UUID | None = None,
        promote_from_deployment_id: uuid.UUID | None = None,
        replace_env_vars: dict[str, EnvVarPlain] | None = None,
    ) -> None:
        await self._jobs.defer_for_approval(
            deployment_id,
            git_ref=git_ref,
            addons=addons,
            env_vars=env_vars,
            rollback_from_deployment_id=rollback_from_deployment_id,
            promote_from_deployment_id=promote_from_deployment_id,
            replace_env_vars=replace_env_vars,
        )

    async def note_auto_deploy_skipped(self, environment: Environment) -> None:
        """One audit row for a paused webhook. The webhook secret is never written."""
        await self._audit.record(
            project_id=environment.project_id,
            environment_id=environment.id,
            action=AUTO_DEPLOY_SKIPPED,
            resource_type="environment",
            resource_id=str(environment.id),
            summary="Automatic redeploy skipped because auto-deploy is paused.",
            actor_label="github-webhook",
        )

    async def approve(self, deployment_id: uuid.UUID, actor_user_id: uuid.UUID | None) -> Deployment:
        stored = await self._pending_for_owner(deployment_id, actor_user_id)
        approved = await self._deployments.mark_approved(deployment_id)
        if approved is None:
            raise ConflictError("Deployment is not waiting for approval.")
        await self._jobs.start_approved(approved)
        await self._audit.record(
            project_id=_require_project(stored),
            environment_id=stored.environment_id,
            action="deployment.approved",
            resource_type="deployment",
            resource_id=str(deployment_id),
            summary=f"Owner {actor_user_id} approved production deploy {deployment_id}.",
            actor_user_id=actor_user_id,
        )
        refreshed = await self._deployments.get(deployment_id)
        await self._deployments.refresh(refreshed)
        return refreshed

    async def reject(self, deployment_id: uuid.UUID, actor_user_id: uuid.UUID | None) -> Deployment:
        stored = await self._pending_for_owner(deployment_id, actor_user_id)
        cancelled = await self._deployments.cancel(deployment_id)
        await self._audit.record(
            project_id=_require_project(stored),
            environment_id=stored.environment_id,
            action="deployment.rejected",
            resource_type="deployment",
            resource_id=str(deployment_id),
            summary=f"Owner {actor_user_id} rejected production deploy {deployment_id}.",
            actor_user_id=actor_user_id,
        )
        return cancelled

    async def _pending_for_owner(
        self, deployment_id: uuid.UUID, actor_user_id: uuid.UUID | None
    ) -> Deployment:
        stored = await self._deployments.get(deployment_id)
        project_id = _require_project(stored)
        # require_role ignores a missing user id. Approve and reject must not.
        if actor_user_id is None or not await self._members.is_owner(project_id, actor_user_id):
            raise ForbiddenError(f"Requires owner role on project {project_id}.")
        if not is_awaiting_approval(stored):
            raise ConflictError("Deployment is not waiting for approval.")
        return stored


def _require_project(deployment: Deployment) -> uuid.UUID:
    if deployment.project_id is None:
        raise NotFoundError(f"Deployment {deployment.id} is not linked to a project.")
    return deployment.project_id
