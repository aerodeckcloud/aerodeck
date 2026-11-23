import uuid
from collections.abc import Sequence

from fastapi import APIRouter, HTTPException, status
from starlette.concurrency import run_in_threadpool

from app.api.deps import (
    ActorUserIdDep,
    AuditLogServiceDep,
    DeployApprovalServiceDep,
    DeploymentServiceDep,
    EnvironmentServiceDep,
    EnvironmentVariableServiceDep,
    JobServiceDep,
    PaginationDep,
    ProjectMemberServiceDep,
    SettingsDep,
    WebhookServiceDep,
)
from app.models.project_member import ProjectRole
from app.schemas.auth import ErrorResponse
from app.schemas.common import Page
from app.schemas.environment import (
    EnvironmentCreate,
    EnvironmentRead,
    EnvironmentSource,
    EnvironmentUpdate,
    environment_to_read,
)
from app.schemas.job import JobAccepted, ProvisionJobCreate
from app.services.container_ops import (
    ContainerActionRequest,
    ContainerActionResult,
    ContainerMetricsRead,
    dispatch_container_action,
    dispatch_container_metrics,
)
from app.services.environment_vars import (
    EnvironmentVarCloneRequest,
    EnvironmentVarCloneResult,
    EnvironmentVarRead,
    EnvironmentVarUpsertRequest,
)
from app.services.environments import EnvironmentService
from app.services.jobs import JobTicket
from app.services.provisioning import JobKind
from app.services.webhooks import WebhookRead, WebhookUpsertRequest

router = APIRouter(prefix="/projects/{project_id}/environments", tags=["environments"])

_NOT_FOUND = {status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}}
_CONFLICT = {status.HTTP_409_CONFLICT: {"model": ErrorResponse}}
_UNAUTHENTICATED = {status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse}}
_FORBIDDEN = {status.HTTP_403_FORBIDDEN: {"model": ErrorResponse}}
_QUEUE_DOWN = {status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse}}


@router.get(
    "",
    response_model=Page[EnvironmentRead],
    status_code=status.HTTP_200_OK,
    summary="List a project's environments",
    responses=_NOT_FOUND,
)
async def list_environments(
    project_id: uuid.UUID, page: PaginationDep, environments: EnvironmentServiceDep
) -> Page[EnvironmentRead]:
    items, total = await environments.list(project_id, limit=page.limit, offset=page.offset)
    return Page(
        items=await _reads(environments, items), total=total, limit=page.limit, offset=page.offset
    )


@router.post(
    "",
    response_model=EnvironmentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an environment in a project",
    responses=_UNAUTHENTICATED | _NOT_FOUND | _CONFLICT,
)
async def create_environment(
    project_id: uuid.UUID, body: EnvironmentCreate, environments: EnvironmentServiceDep
) -> EnvironmentRead:
    return await _read(environments, await environments.create(project_id, body))


@router.get(
    "/{environment_id}",
    response_model=EnvironmentRead,
    status_code=status.HTTP_200_OK,
    summary="Get an environment",
    responses=_NOT_FOUND,
)
async def get_environment(
    project_id: uuid.UUID, environment_id: uuid.UUID, environments: EnvironmentServiceDep
) -> EnvironmentRead:
    return await _read(environments, await environments.get(project_id, environment_id))


@router.patch(
    "/{environment_id}",
    response_model=EnvironmentRead,
    status_code=status.HTTP_200_OK,
    summary="Update an environment",
    responses=_UNAUTHENTICATED | _NOT_FOUND | _FORBIDDEN,
)
async def update_environment(
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    body: EnvironmentUpdate,
    environments: EnvironmentServiceDep,
    members: ProjectMemberServiceDep,
    actor_user_id: ActorUserIdDep,
) -> EnvironmentRead:
    updated = await environments.update(
        project_id,
        environment_id,
        body,
        actor_user_id=actor_user_id,
        members=members,
    )
    return await _read(environments, updated)


@router.delete(
    "/{environment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an environment",
    responses=_UNAUTHENTICATED | _NOT_FOUND,
)
async def delete_environment(
    project_id: uuid.UUID, environment_id: uuid.UUID, environments: EnvironmentServiceDep
) -> None:
    await environments.delete(project_id, environment_id)


async def _reads(
    environments: EnvironmentService, items: Sequence[EnvironmentSource]
) -> list[EnvironmentRead]:
    counts = await environments.retained_rollback_image_counts([row.id for row in items])
    return [
        environment_to_read(row, retained_rollback_images=counts.get(row.id, 0)) for row in items
    ]


async def _read(environments: EnvironmentService, environment: EnvironmentSource) -> EnvironmentRead:
    reads = await _reads(environments, [environment])
    return reads[0]


def _accepted(ticket: JobTicket) -> JobAccepted:
    return JobAccepted(
        job_id=uuid.UUID(ticket.job_id) if ticket.job_id else None,
        active_deployment_id=ticket.active_deployment_id,
        kind=ticket.kind,
        project_id=ticket.project_id,
        environment_id=ticket.environment_id,
    )


async def _enqueue(
    kind: JobKind,
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    jobs: JobServiceDep,
    *,
    deployment_id: uuid.UUID | None = None,
    env_vars: dict[str, str] | None = None,
    addons: list[str] | None = None,
) -> JobAccepted:
    ticket = await jobs.enqueue(
        kind, project_id, environment_id, deployment_id=deployment_id, env_vars=env_vars, addons=addons
    )
    return _accepted(ticket)


def _approval_accepted(project_id: uuid.UUID, environment_id: uuid.UUID) -> JobAccepted:
    return JobAccepted(
        approval_pending=True,
        kind=JobKind.provision,
        project_id=project_id,
        environment_id=environment_id,
    )


@router.post(
    "/{environment_id}/provision",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a provisioning job for an environment",
    description=(
        "Returns at once with a job id. Poll `GET /api/v1/jobs/{job_id}` for the outcome. "
        "Optional body `deployment_id` ties the job to a console deployment so the worker "
        "publishes progress onto `GET /api/v1/deployments/{id}/logs`."
    ),
    responses=_UNAUTHENTICATED | _NOT_FOUND | _CONFLICT | _QUEUE_DOWN,
)
async def provision_environment(
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    jobs: JobServiceDep,
    deployments: DeploymentServiceDep,
    environments: EnvironmentServiceDep,
    approval: DeployApprovalServiceDep,
    body: ProvisionJobCreate | None = None,
) -> JobAccepted:
    deployment_id = body.deployment_id if body is not None else None
    env_vars = body.env_vars if body is not None else None
    addons = body.addons if body is not None else None
    if deployment_id is not None:
        stored = await deployments.get(deployment_id)
        environment = await environments.get(project_id, environment_id)
        ticket = await approval.enqueue_or_defer(
            environment, stored.actor_user_id, deployment_id, env_vars=env_vars, addons=addons
        )
        if ticket is None:
            return _approval_accepted(project_id, environment_id)
        return _accepted(ticket)
    return await _enqueue(
        JobKind.provision,
        project_id,
        environment_id,
        jobs,
        deployment_id=deployment_id,
        env_vars=env_vars,
        addons=addons,
    )


@router.post(
    "/{environment_id}/reconcile",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a reconciliation job for an environment",
    description="Returns at once with a job id. Poll `GET /api/v1/jobs/{job_id}` for the outcome.",
    responses=_UNAUTHENTICATED | _NOT_FOUND | _QUEUE_DOWN,
)
async def reconcile_environment(
    project_id: uuid.UUID, environment_id: uuid.UUID, jobs: JobServiceDep
) -> JobAccepted:
    return await _enqueue(JobKind.reconcile, project_id, environment_id, jobs)


@router.get(
    "/{environment_id}/env-vars",
    response_model=list[EnvironmentVarRead],
    status_code=status.HTTP_200_OK,
    summary="List saved environment variable keys (masked) (#248)",
    responses=_UNAUTHENTICATED | _NOT_FOUND,
)
async def list_environment_vars(
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    env_vars: EnvironmentVariableServiceDep,
) -> list[EnvironmentVarRead]:
    return await env_vars.list_masked(project_id, environment_id)


@router.put(
    "/{environment_id}/env-vars",
    response_model=list[EnvironmentVarRead],
    status_code=status.HTTP_200_OK,
    summary="Upsert encrypted environment variables (#248)",
    responses=_UNAUTHENTICATED | _NOT_FOUND,
)
async def upsert_environment_vars(
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    body: EnvironmentVarUpsertRequest,
    env_vars: EnvironmentVariableServiceDep,
    members: ProjectMemberServiceDep,
    audit: AuditLogServiceDep,
    actor_user_id: ActorUserIdDep,
) -> list[EnvironmentVarRead]:
    await members.require_role(project_id, actor_user_id, ProjectRole.developer)
    result = await env_vars.upsert_many(project_id, environment_id, body.items)
    keys = ", ".join(item.key for item in body.items[:5])
    suffix = f" (+{len(body.items) - 5} more)" if len(body.items) > 5 else ""
    await audit.record(
        project_id=project_id,
        environment_id=environment_id,
        action="env_vars.upserted",
        resource_type="env_var",
        summary=f"Saved {len(body.items)} encrypted env var(s): {keys}{suffix}",
        actor_user_id=actor_user_id,
    )
    return result


@router.delete(
    "/{environment_id}/env-vars/{key}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a saved environment variable (#248)",
    responses=_UNAUTHENTICATED | _NOT_FOUND,
)
async def delete_environment_var(
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    key: str,
    env_vars: EnvironmentVariableServiceDep,
    members: ProjectMemberServiceDep,
    audit: AuditLogServiceDep,
    actor_user_id: ActorUserIdDep,
) -> None:
    await members.require_role(project_id, actor_user_id, ProjectRole.developer)
    await env_vars.delete_key(project_id, environment_id, key)
    await audit.record(
        project_id=project_id,
        environment_id=environment_id,
        action="env_vars.deleted",
        resource_type="env_var",
        resource_id=key,
        summary=f"Deleted environment variable {key}",
        actor_user_id=actor_user_id,
    )


@router.post(
    "/{environment_id}/env-vars/clone",
    response_model=EnvironmentVarCloneResult,
    status_code=status.HTTP_200_OK,
    summary="Clone encrypted environment variables from another environment in the same project (#255)",
    responses=_UNAUTHENTICATED | _NOT_FOUND,
)
async def clone_environment_vars(
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    body: EnvironmentVarCloneRequest,
    env_vars: EnvironmentVariableServiceDep,
) -> EnvironmentVarCloneResult:
    try:
        return await env_vars.clone_from_environment(
            project_id,
            environment_id,
            body.source_environment_id,
            overwrite=body.overwrite,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@router.get(
    "/{environment_id}/webhook",
    response_model=WebhookRead | None,
    status_code=status.HTTP_200_OK,
    summary="Get GitHub push webhook configuration for an environment (#249)",
    responses=_UNAUTHENTICATED | _NOT_FOUND,
)
async def get_environment_webhook(
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    webhooks: WebhookServiceDep,
) -> WebhookRead | None:
    return await webhooks.get_for_environment(project_id, environment_id)


@router.put(
    "/{environment_id}/webhook",
    response_model=WebhookRead,
    status_code=status.HTTP_200_OK,
    summary="Create or update GitHub push webhook configuration (#249)",
    responses=_UNAUTHENTICATED | _NOT_FOUND,
)
async def upsert_environment_webhook(
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    body: WebhookUpsertRequest,
    webhooks: WebhookServiceDep,
) -> WebhookRead:
    return await webhooks.upsert_for_environment(project_id, environment_id, body)


@router.delete(
    "/{environment_id}/webhook",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete GitHub push webhook configuration (#249)",
    responses=_UNAUTHENTICATED | _NOT_FOUND,
)
async def delete_environment_webhook(
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    webhooks: WebhookServiceDep,
) -> None:
    await webhooks.delete_for_environment(project_id, environment_id)


@router.post(
    "/{environment_id}/actions",
    response_model=ContainerActionResult,
    status_code=status.HTTP_200_OK,
    summary="Start, stop, or restart an environment's active container (#254)",
    responses=_UNAUTHENTICATED | _NOT_FOUND,
)
async def run_environment_container_action(
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    body: ContainerActionRequest,
    environments: EnvironmentServiceDep,
    settings: SettingsDep,
) -> ContainerActionResult:
    await environments.get(project_id, environment_id)
    return await run_in_threadpool(
        dispatch_container_action,
        settings,
        project_id=project_id,
        environment_id=environment_id,
        action=body.action,
    )


@router.get(
    "/{environment_id}/metrics",
    response_model=ContainerMetricsRead,
    status_code=status.HTTP_200_OK,
    summary="Fetch live CPU, memory, and uptime metrics for an environment container (#254)",
    responses=_UNAUTHENTICATED | _NOT_FOUND,
)
async def get_environment_container_metrics(
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    environments: EnvironmentServiceDep,
    settings: SettingsDep,
) -> ContainerMetricsRead:
    await environments.get(project_id, environment_id)
    return await run_in_threadpool(
        dispatch_container_metrics,
        settings,
        project_id=project_id,
        environment_id=environment_id,
    )
