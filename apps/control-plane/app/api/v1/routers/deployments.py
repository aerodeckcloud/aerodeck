"""Deployment records and log streams.

``POST /api/v1/deployments`` stores a GitHub repository request. It does not
call DigitalOcean. ``GET /api/v1/deployments/{deployment_id}/logs`` streams
worker-published lines when present, otherwise the stub. ``POST .../succeeded``
records that the stream finished with ``Done``. ``POST .../failed`` records a
provision that ended without ``Done``. Neither call touches DigitalOcean.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import (
    ActorUserIdDep,
    AuditLogServiceDep,
    ClientDep,
    ConsoleUserServiceDep,
    DeployApprovalServiceDep,
    DeploymentDraftServiceDep,
    DeploymentServiceDep,
    EnvironmentServiceDep,
    ImageRetentionServiceDep,
    JobServiceDep,
    ReleaseJobServiceDep,
    PaginationDep,
    PromotionServiceDep,
    RollbackServiceDep,
    SettingsDep,
)
from app.core.errors import UnprocessableError
from app.models.deployment import Deployment, DeploymentStatus, DeploymentTrigger
from app.services.console_users import ConsoleUserService
from app.schemas.auth import ErrorResponse
from app.schemas.blueprint import DeploymentBlueprint, DeploymentInspectRequest
from app.schemas.common import Page
from app.schemas.deployment import (
    DeploymentCreate,
    DeploymentPromote,
    DeploymentRead,
    DeploymentRollback,
    deployment_to_read,
)
from app.schemas.deployment_draft import (
    DeploymentDraftCreate,
    DeploymentDraftLaunch,
    DeploymentDraftPlan,
    DeploymentDraftRead,
    DeploymentDraftWrite,
)
from app.services.deployment_draft import mask_plan, merge_plan, plain_values
from app.services.blueprint_inspector import inspect_github_repository_sync
from app.services.docker_policy import normalize_git_sha
from app.services.container_logs import ContainerLogsRead, dispatch_container_logs
from app.services.deployment_logs import ensure_log_store_ready, iter_deployment_log_lines

router = APIRouter(prefix="/deployments", tags=["deployments"])

_NOT_FOUND = {status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}}
_UNAUTHENTICATED = {status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse}}
_UNAVAILABLE = {status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse}}
# A deployment held behind another one has no outcome yet (#271).
_NOT_STARTED = {status.HTTP_409_CONFLICT: {"model": ErrorResponse}}
# Promoting needs a developer, and an owner when the target is production (#272).
_FORBIDDEN = {status.HTTP_403_FORBIDDEN: {"model": ErrorResponse}}
# Same environment, other project, non-succeeded row, or missing image (#272).
_UNPROCESSABLE = {status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse}}
# The target replaces its active deploy, and that deploy is already swapping containers.
_SWAP_STARTED = {status.HTTP_409_CONFLICT: {"model": ErrorResponse}}


async def _known_actor(users: ConsoleUserService, actor_user_id: uuid.UUID | None) -> uuid.UUID | None:
    """Keep the header only when it names a stored console user."""
    if actor_user_id is None:
        return None
    user = await users.get(actor_user_id)
    return user.id if user is not None else None


def _sse_frame(line: str) -> bytes:
    return f"data: {line}\n\n".encode()


async def _as_sse(lines: AsyncIterator[str]) -> AsyncIterator[bytes]:
    async for line in lines:
        yield _sse_frame(line)


@router.get("", response_model=Page[DeploymentRead], summary="List deployments")
async def list_deployments(
    page: PaginationDep,
    deployments: DeploymentServiceDep,
    users: ConsoleUserServiceDep,
    actor_user_id: ActorUserIdDep,
) -> Page[DeploymentRead]:
    # Another operator's draft is omitted. Their own draft stays so they can resume (#343).
    items, total = await deployments.list(
        limit=page.limit,
        offset=page.offset,
        actor_user_id=await _known_actor(users, actor_user_id),
    )
    return Page(
        items=[deployment_to_read(row) for row in items],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post(
    "/inspect",
    response_model=DeploymentBlueprint,
    status_code=status.HTTP_200_OK,
    summary="Inspect a GitHub repository and return its DeploymentBlueprint (#236)",
    responses=_UNAUTHENTICATED | _UNAVAILABLE,
)
async def inspect_deployment_repository(
    body: DeploymentInspectRequest,
    client: ClientDep,
    settings: SettingsDep,
) -> DeploymentBlueprint:
    # Authenticated via ClientDep (enrolled control-plane caller required).
    if not client:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    try:
        return await run_in_threadpool(
            inspect_github_repository_sync,
            body.repo_url,
            git_bin=settings.git_bin,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc


@router.post(
    "",
    response_model=DeploymentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a deployment",
    responses=_UNAUTHENTICATED | _NOT_FOUND,
)
async def create_deployment(
    body: DeploymentCreate,
    deployments: DeploymentServiceDep,
    audit: AuditLogServiceDep,
    users: ConsoleUserServiceDep,
    actor_user_id: ActorUserIdDep,
) -> DeploymentRead:
    created = await deployments.create(
        body,
        trigger=DeploymentTrigger.manual,
        actor_user_id=await _known_actor(users, actor_user_id),
    )
    payload = deployment_to_read(created)
    if payload.project_id is not None:
        await audit.record(
            project_id=payload.project_id,
            environment_id=payload.environment_id,
            action="deployment.created",
            resource_type="deployment",
            resource_id=str(payload.id),
            summary=f"Started deployment of {payload.owner}/{payload.repo}",
            actor_user_id=actor_user_id,
        )
    return payload


def _draft_read(deployment: Deployment, plan: DeploymentDraftPlan) -> DeploymentDraftRead:
    return DeploymentDraftRead(deployment=deployment_to_read(deployment), plan=mask_plan(plan))


@router.post(
    "/draft",
    response_model=DeploymentDraftRead,
    status_code=status.HTTP_201_CREATED,
    summary="Save a deployment draft without queueing provision (#343)",
    responses=_UNAUTHENTICATED | _UNPROCESSABLE,
)
async def create_deployment_draft(
    body: DeploymentDraftCreate,
    deployments: DeploymentServiceDep,
    drafts: DeploymentDraftServiceDep,
    users: ConsoleUserServiceDep,
    actor_user_id: ActorUserIdDep,
) -> DeploymentDraftRead:
    created = await deployments.create(
        DeploymentCreate(repo_url=body.repo_url),
        trigger=DeploymentTrigger.manual,
        actor_user_id=await _known_actor(users, actor_user_id),
        draft=True,
    )
    drafts.apply(created, body.plan)
    return _draft_read(created, body.plan)


@router.get(
    "/{deployment_id}/draft",
    response_model=DeploymentDraftRead,
    summary="Read the caller's deployment draft (#343)",
    responses=_NOT_FOUND | _UNAUTHENTICATED | _NOT_STARTED,
)
async def read_deployment_draft(
    deployment_id: uuid.UUID,
    deployments: DeploymentServiceDep,
    drafts: DeploymentDraftServiceDep,
    users: ConsoleUserServiceDep,
    actor_user_id: ActorUserIdDep,
) -> DeploymentDraftRead:
    stored = await deployments.get(deployment_id)
    drafts.require_owner(stored, await _known_actor(users, actor_user_id))
    plan = drafts.open(stored.draft_plan)
    return _draft_read(stored, plan)


@router.put(
    "/{deployment_id}/draft",
    response_model=DeploymentDraftRead,
    summary="Update the caller's deployment draft (#343)",
    responses=_NOT_FOUND | _UNAUTHENTICATED | _NOT_STARTED | _UNPROCESSABLE,
)
async def update_deployment_draft(
    deployment_id: uuid.UUID,
    body: DeploymentDraftWrite,
    deployments: DeploymentServiceDep,
    drafts: DeploymentDraftServiceDep,
    environments: EnvironmentServiceDep,
    users: ConsoleUserServiceDep,
    actor_user_id: ActorUserIdDep,
) -> DeploymentDraftRead:
    stored = await deployments.get(deployment_id)
    drafts.require_owner(stored, await _known_actor(users, actor_user_id))
    merged = merge_plan(drafts.open(stored.draft_plan), body.plan)
    if body.project_id is not None and body.environment_id is not None:
        await environments.get(body.project_id, body.environment_id)
        stored.project_id = body.project_id
        stored.environment_id = body.environment_id
    drafts.apply(stored, merged)
    return _draft_read(stored, merged)


@router.post(
    "/{deployment_id}/discard",
    response_model=DeploymentRead,
    summary="Discard a draft without queueing provision (#343)",
    responses=_NOT_FOUND | _UNAUTHENTICATED | _NOT_STARTED,
)
async def discard_deployment_draft(
    deployment_id: uuid.UUID,
    deployments: DeploymentServiceDep,
    drafts: DeploymentDraftServiceDep,
    users: ConsoleUserServiceDep,
    actor_user_id: ActorUserIdDep,
) -> DeploymentRead:
    stored = await deployments.get(deployment_id)
    drafts.require_owner(stored, await _known_actor(users, actor_user_id))
    stored.status = DeploymentStatus.cancelled
    stored.finished_at = datetime.now(UTC)
    stored.draft_step = None
    stored.draft_plan = None
    return deployment_to_read(stored)


@router.post(
    "/{deployment_id}/launch",
    response_model=DeploymentRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Leave draft and queue provision on the existing path (#343)",
    responses=_NOT_FOUND | _UNAUTHENTICATED | _NOT_STARTED | _UNPROCESSABLE | _UNAVAILABLE,
)
async def launch_deployment_draft(
    deployment_id: uuid.UUID,
    body: DeploymentDraftLaunch,
    deployments: DeploymentServiceDep,
    drafts: DeploymentDraftServiceDep,
    environments: EnvironmentServiceDep,
    approval: DeployApprovalServiceDep,
    users: ConsoleUserServiceDep,
    actor_user_id: ActorUserIdDep,
) -> DeploymentRead:
    known = await _known_actor(users, actor_user_id)
    stored = await deployments.get(deployment_id)
    drafts.require_owner(stored, known)
    merged = merge_plan(drafts.open(stored.draft_plan), body.plan)
    if merged.step != "launch":
        raise UnprocessableError("Confirm the launch step before deploying.")
    environment = await environments.get(body.project_id, body.environment_id)
    stored.project_id = body.project_id
    stored.environment_id = body.environment_id
    drafts.apply(stored, merged)
    service = merged.blueprint.build.service or None
    await approval.enqueue_or_defer(
        environment,
        known,
        stored.id,
        env_vars=plain_values(merged) or None,
        addons=list(merged.selected_addons) or None,
        port=merged.blueprint.build.port,
        service=service,
    )
    drafts.finish(stored)
    return deployment_to_read(stored)


@router.get(
    "/{deployment_id}",
    response_model=DeploymentRead,
    summary="Get a deployment",
    responses=_NOT_FOUND,
)
async def get_deployment(deployment_id: uuid.UUID, deployments: DeploymentServiceDep) -> DeploymentRead:
    return deployment_to_read(await deployments.get(deployment_id))


@router.post(
    "/{deployment_id}/succeeded",
    response_model=DeploymentRead,
    status_code=status.HTTP_200_OK,
    summary="Record that a deployment finished",
    responses=_NOT_FOUND | _UNAUTHENTICATED | _NOT_STARTED,
)
async def succeed_deployment(
    deployment_id: uuid.UUID,
    deployments: DeploymentServiceDep,
    jobs: ReleaseJobServiceDep,
    retention: ImageRetentionServiceDep,
) -> DeploymentRead:
    stored = await deployments.mark_succeeded(deployment_id)
    if jobs is not None:
        await jobs.release_waiting(stored)
    # Older tags are removed after this row is succeeded. A prune failure is
    # logged inside the scheduler and leaves this row succeeded (#274).
    if stored.environment_id is not None:
        retention.schedule(stored.environment_id, stored.id)
    return deployment_to_read(stored)


@router.post(
    "/{deployment_id}/failed",
    response_model=DeploymentRead,
    status_code=status.HTTP_200_OK,
    summary="Record that a deployment failed",
    responses=_NOT_FOUND | _UNAUTHENTICATED | _NOT_STARTED,
)
async def fail_deployment(
    deployment_id: uuid.UUID,
    deployments: DeploymentServiceDep,
    jobs: ReleaseJobServiceDep,
) -> DeploymentRead:
    stored = await deployments.mark_failed(deployment_id)
    if jobs is not None:
        await jobs.release_waiting(stored)
    return deployment_to_read(stored)


@router.get(
    "/{deployment_id}/logs",
    summary="Stream deployment logs (SSE)",
    response_class=StreamingResponse,
    status_code=status.HTTP_200_OK,
    responses=_NOT_FOUND | _UNAUTHENTICATED | _UNAVAILABLE,
)
async def stream_deployment_logs(
    deployment_id: uuid.UUID,
    deployments: DeploymentServiceDep,
    settings: SettingsDep,
) -> StreamingResponse:
    """Stream published lines when present; otherwise the stub for a stored deployment."""
    await deployments.get(deployment_id)
    await ensure_log_store_ready(settings.redis_url)
    return StreamingResponse(
        _as_sse(
            iter_deployment_log_lines(
                settings.redis_url,
                deployment_id,
                follow_idle_seconds=settings.deployment_log_follow_idle_seconds,
                first_publish_grace_seconds=settings.deployment_log_first_publish_grace_seconds,
            )
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/{deployment_id}/runtime-logs",
    response_model=ContainerLogsRead,
    status_code=status.HTTP_200_OK,
    summary="Fetch recent runtime container logs for a deployment's environment (#247)",
    responses=_NOT_FOUND | _UNAUTHENTICATED,
)
async def get_deployment_runtime_logs(
    deployment_id: uuid.UUID,
    deployments: DeploymentServiceDep,
    settings: SettingsDep,
    tail: int = Query(default=200, ge=1, le=500),
) -> ContainerLogsRead:
    deployment = await deployments.get(deployment_id)
    if deployment.project_id is None or deployment.environment_id is None:
        return ContainerLogsRead(
            container="",
            status="not_found",
            lines=[],
            error="Deployment is not linked to an environment.",
        )
    return await run_in_threadpool(
        dispatch_container_logs,
        settings,
        project_id=deployment.project_id,
        environment_id=deployment.environment_id,
        tail=tail,
    )


def _require_linked(source) -> tuple[uuid.UUID, uuid.UUID]:
    if source.project_id is None or source.environment_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Deployment is not linked to an environment.",
        )
    return source.project_id, source.environment_id


async def _enqueue_copy(
    source,
    deployments: DeploymentServiceDep,
    users: ConsoleUserServiceDep,
    environments: EnvironmentServiceDep,
    approval: DeployApprovalServiceDep,
    actor_user_id: uuid.UUID | None,
    *,
    git_ref: str | None,
) -> DeploymentRead:
    project_id, environment_id = _require_linked(source)
    known_actor = await _known_actor(users, actor_user_id)
    created = await deployments.create(
        DeploymentCreate(
            repo_url=source.repo_url,
            project_id=project_id,
            environment_id=environment_id,
        ),
        trigger=DeploymentTrigger.manual,
        actor_user_id=known_actor,
    )
    environment = await environments.get(project_id, environment_id)
    await approval.enqueue_or_defer(environment, known_actor, created.id, git_ref=git_ref)
    refreshed = await deployments.get(created.id)
    await deployments.refresh(refreshed)
    return deployment_to_read(refreshed)


@router.post(
    "/{deployment_id}/redeploy",
    response_model=DeploymentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Repeat a recorded release by provisioning its stored commit (#269)",
    responses=_NOT_FOUND | _UNAUTHENTICATED | _UNAVAILABLE,
)
async def redeploy_deployment(
    deployment_id: uuid.UUID,
    deployments: DeploymentServiceDep,
    users: ConsoleUserServiceDep,
    environments: EnvironmentServiceDep,
    approval: DeployApprovalServiceDep,
    actor_user_id: ActorUserIdDep,
) -> DeploymentRead:
    source = await deployments.get(deployment_id)
    _require_linked(source)
    sha = normalize_git_sha(source.git_sha)
    if sha is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Cannot repeat a deployment that has no recorded commit.",
        )
    return await _enqueue_copy(
        source,
        deployments,
        users,
        environments,
        approval,
        actor_user_id,
        git_ref=sha,
    )


@router.post(
    "/{deployment_id}/deploy-latest",
    response_model=DeploymentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Deploy the repository default branch instead of a recorded commit (#269)",
    responses=_NOT_FOUND | _UNAUTHENTICATED | _UNAVAILABLE,
)
async def deploy_latest_deployment(
    deployment_id: uuid.UUID,
    deployments: DeploymentServiceDep,
    users: ConsoleUserServiceDep,
    environments: EnvironmentServiceDep,
    approval: DeployApprovalServiceDep,
    actor_user_id: ActorUserIdDep,
) -> DeploymentRead:
    source = await deployments.get(deployment_id)
    return await _enqueue_copy(
        source,
        deployments,
        users,
        environments,
        approval,
        actor_user_id,
        git_ref=None,
    )


@router.post(
    "/{deployment_id}/cancel",
    response_model=DeploymentRead,
    status_code=status.HTTP_200_OK,
    summary="Cancel a deployment before the live container is swapped (#270)",
    responses=_NOT_FOUND | _UNAUTHENTICATED,
)
async def cancel_deployment(
    deployment_id: uuid.UUID,
    deployments: DeploymentServiceDep,
    jobs: JobServiceDep,
    release: ReleaseJobServiceDep,
) -> DeploymentRead:
    stored = await deployments.cancel(deployment_id)
    await jobs.revoke_quietly(stored.provision_job_id)
    # Same rule as the outcome routes: without a queue the waiter stays queued.
    if release is not None:
        await release.release_waiting(stored)
    return deployment_to_read(stored)


@router.post(
    "/{deployment_id}/approve",
    response_model=DeploymentRead,
    status_code=status.HTTP_200_OK,
    summary="Approve a production deploy that is waiting for an owner (#276)",
    responses=_NOT_FOUND | _UNAUTHENTICATED | _FORBIDDEN | _NOT_STARTED | _UNAVAILABLE,
)
async def approve_deployment(
    deployment_id: uuid.UUID,
    approval: DeployApprovalServiceDep,
    actor_user_id: ActorUserIdDep,
) -> DeploymentRead:
    stored = await approval.approve(deployment_id, actor_user_id)
    return deployment_to_read(stored)


@router.post(
    "/{deployment_id}/reject",
    response_model=DeploymentRead,
    status_code=status.HTTP_200_OK,
    summary="Reject a production deploy that is waiting for an owner (#276)",
    responses=_NOT_FOUND | _UNAUTHENTICATED | _FORBIDDEN | _NOT_STARTED,
)
async def reject_deployment(
    deployment_id: uuid.UUID,
    approval: DeployApprovalServiceDep,
    actor_user_id: ActorUserIdDep,
) -> DeploymentRead:
    stored = await approval.reject(deployment_id, actor_user_id)
    return deployment_to_read(stored)


@router.post(
    "/{deployment_id}/promote",
    response_model=DeploymentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Run a succeeded deployment's image in another environment of the project (#272)",
    responses=_NOT_FOUND
    | _UNAUTHENTICATED
    | _FORBIDDEN
    | _UNPROCESSABLE
    | _SWAP_STARTED
    | _UNAVAILABLE,
)
async def promote_deployment(
    deployment_id: uuid.UUID,
    body: DeploymentPromote,
    promotions: PromotionServiceDep,
    users: ConsoleUserServiceDep,
    actor_user_id: ActorUserIdDep,
) -> DeploymentRead:
    """The target's overlap setting applies: the new row may come back waiting."""
    promoted = await promotions.promote(
        deployment_id,
        body.environment_id,
        actor_user_id=actor_user_id,
        recorded_actor_id=await _known_actor(users, actor_user_id),
    )
    return deployment_to_read(promoted)


@router.post(
    "/{deployment_id}/rollback",
    response_model=DeploymentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Roll back to a succeeded deployment image and its variable snapshot (#273)",
    responses=_NOT_FOUND | _UNAUTHENTICATED | _FORBIDDEN | _UNPROCESSABLE | _UNAVAILABLE,
)
async def rollback_deployment(
    deployment_id: uuid.UUID,
    deployments: DeploymentServiceDep,
    rollbacks: RollbackServiceDep,
    users: ConsoleUserServiceDep,
    actor_user_id: ActorUserIdDep,
    body: DeploymentRollback | None = None,
) -> DeploymentRead:
    """Restore the release variables when a snapshot exists, then retag the image (#273)."""
    request = body or DeploymentRollback()
    stored, variables_restored = await rollbacks.rollback(
        deployment_id,
        restore_variables=request.restore_variables,
        actor_user_id=actor_user_id,
        recorded_actor_id=await _known_actor(users, actor_user_id),
    )
    await deployments.refresh(stored)
    return deployment_to_read(stored, variables_restored=variables_restored)
