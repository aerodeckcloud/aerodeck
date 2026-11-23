"""Deployment records and log streams.

``POST /api/v1/deployments`` stores a GitHub repository request. It does not
call DigitalOcean. ``GET /api/v1/deployments/{deployment_id}/logs`` streams
worker-published lines when present, otherwise the stub. ``POST .../succeeded``
records that the stream finished with ``Done``. ``POST .../failed`` records a
provision that ended without ``Done``. Neither call touches DigitalOcean.
"""

import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import (
    ActorUserIdDep,
    AuditLogServiceDep,
    ClientDep,
    ConsoleUserServiceDep,
    DeploymentServiceDep,
    JobServiceDep,
    ReleaseJobServiceDep,
    PaginationDep,
    PromotionServiceDep,
    SettingsDep,
)
from app.models.deployment import DeploymentStatus, DeploymentTrigger
from app.services.console_users import ConsoleUserService
from app.schemas.auth import ErrorResponse
from app.schemas.blueprint import DeploymentBlueprint, DeploymentInspectRequest
from app.schemas.common import Page
from app.schemas.deployment import DeploymentCreate, DeploymentPromote, DeploymentRead
from app.services.blueprint_inspector import inspect_github_repository_sync
from app.services.docker_policy import normalize_git_sha
from app.services.container_logs import ContainerLogsRead, dispatch_container_logs
from app.services.deployment_logs import ensure_log_store_ready, iter_deployment_log_lines
from app.services.provisioning import JobKind

router = APIRouter(prefix="/deployments", tags=["deployments"])

_NOT_FOUND = {status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}}
_UNAUTHENTICATED = {status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse}}
_UNAVAILABLE = {status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse}}
# A deployment held behind another one has no outcome yet (#271).
_NOT_STARTED = {status.HTTP_409_CONFLICT: {"model": ErrorResponse}}
# Promoting needs a developer, and an owner when the target is production (#272).
_FORBIDDEN = {status.HTTP_403_FORBIDDEN: {"model": ErrorResponse}}
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
async def list_deployments(page: PaginationDep, deployments: DeploymentServiceDep) -> Page[DeploymentRead]:
    items, total = await deployments.list(limit=page.limit, offset=page.offset)
    return Page(
        items=[DeploymentRead.model_validate(row) for row in items],
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
    payload = DeploymentRead.model_validate(created)
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


@router.get(
    "/{deployment_id}",
    response_model=DeploymentRead,
    summary="Get a deployment",
    responses=_NOT_FOUND,
)
async def get_deployment(deployment_id: uuid.UUID, deployments: DeploymentServiceDep) -> DeploymentRead:
    return DeploymentRead.model_validate(await deployments.get(deployment_id))


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
) -> DeploymentRead:
    stored = await deployments.mark_succeeded(deployment_id)
    if jobs is not None:
        await jobs.release_waiting(stored)
    return DeploymentRead.model_validate(stored)


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
    return DeploymentRead.model_validate(stored)


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
    jobs: JobServiceDep,
    users: ConsoleUserServiceDep,
    actor_user_id: uuid.UUID | None,
    *,
    git_ref: str | None,
) -> DeploymentRead:
    project_id, environment_id = _require_linked(source)
    created = await deployments.create(
        DeploymentCreate(
            repo_url=source.repo_url,
            project_id=project_id,
            environment_id=environment_id,
        ),
        trigger=DeploymentTrigger.manual,
        actor_user_id=await _known_actor(users, actor_user_id),
    )
    await jobs.enqueue(
        JobKind.provision,
        project_id,
        environment_id,
        deployment_id=created.id,
        git_ref=git_ref,
    )
    refreshed = await deployments.get(created.id)
    await deployments.refresh(refreshed)
    return DeploymentRead.model_validate(refreshed)


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
    jobs: JobServiceDep,
    users: ConsoleUserServiceDep,
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
        source, deployments, jobs, users, actor_user_id, git_ref=sha
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
    jobs: JobServiceDep,
    users: ConsoleUserServiceDep,
    actor_user_id: ActorUserIdDep,
) -> DeploymentRead:
    source = await deployments.get(deployment_id)
    return await _enqueue_copy(
        source, deployments, jobs, users, actor_user_id, git_ref=None
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
    return DeploymentRead.model_validate(stored)


@router.post(
    "/{deployment_id}/promote",
    response_model=DeploymentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Run a succeeded deployment's image in another environment of the project (#272)",
    responses=_NOT_FOUND | _UNAUTHENTICATED | _FORBIDDEN | _SWAP_STARTED | _UNAVAILABLE,
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
    return DeploymentRead.model_validate(promoted)


@router.post(
    "/{deployment_id}/rollback",
    response_model=DeploymentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Roll back an environment to a previously succeeded deployment image (#253)",
    responses=_NOT_FOUND | _UNAUTHENTICATED | _UNAVAILABLE,
)
async def rollback_deployment(
    deployment_id: uuid.UUID,
    deployments: DeploymentServiceDep,
    jobs: JobServiceDep,
    users: ConsoleUserServiceDep,
    actor_user_id: ActorUserIdDep,
) -> DeploymentRead:
    source = await deployments.get(deployment_id)
    if source.project_id is None or source.environment_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Cannot roll back a deployment that is not linked to an environment.",
        )
    if source.status is not DeploymentStatus.succeeded:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Only succeeded deployments can be rolled back to.",
        )
    created = await deployments.create(
        DeploymentCreate(
            repo_url=source.repo_url,
            project_id=source.project_id,
            environment_id=source.environment_id,
        ),
        trigger=DeploymentTrigger.rollback,
        actor_user_id=await _known_actor(users, actor_user_id),
    )
    await jobs.enqueue(
        JobKind.provision,
        source.project_id,
        source.environment_id,
        deployment_id=created.id,
        rollback_from_deployment_id=source.id,
    )
    refreshed = await deployments.get(created.id)
    await deployments.refresh(refreshed)
    return DeploymentRead.model_validate(refreshed)
