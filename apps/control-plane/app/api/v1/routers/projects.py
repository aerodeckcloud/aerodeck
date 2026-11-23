import uuid

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import (
    ActorUserIdDep,
    AuditLogServiceDep,
    DeploymentServiceDep,
    NotificationServiceDep,
    PaginationDep,
    ProjectMemberServiceDep,
    ProjectServiceDep,
)
from app.models.project_member import ProjectRole
from app.schemas.auth import ErrorResponse
from app.schemas.common import Page
from app.schemas.deployment import DeploymentRead, deployment_to_read
from app.schemas.project import ProjectCreate, ProjectRead, ProjectUpdate
from app.services.audit_logs import AuditEventRead
from app.services.notifications import (
    NotificationChannelRead,
    NotificationChannelUpsertRequest,
    NotificationTestResult,
)
from app.services.project_members import ProjectMemberRead, ProjectMemberUpsertRequest

router = APIRouter(prefix="/projects", tags=["projects"])

_NOT_FOUND = {status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}}
_CONFLICT = {status.HTTP_409_CONFLICT: {"model": ErrorResponse}}
_UNAUTHENTICATED = {status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse}}


@router.get("", response_model=Page[ProjectRead], status_code=status.HTTP_200_OK, summary="List projects")
async def list_projects(page: PaginationDep, projects: ProjectServiceDep) -> Page[ProjectRead]:
    items, total = await projects.list(limit=page.limit, offset=page.offset)
    return Page(
        items=[ProjectRead.model_validate(p) for p in items], total=total, limit=page.limit, offset=page.offset
    )


@router.post(
    "",
    response_model=ProjectRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a project",
    responses=_UNAUTHENTICATED | _CONFLICT,
)
async def create_project(
    body: ProjectCreate,
    projects: ProjectServiceDep,
    members: ProjectMemberServiceDep,
    actor_user_id: ActorUserIdDep,
) -> ProjectRead:
    created = await projects.create(body)
    await members.assign_owner_if_user_exists(created.id, actor_user_id)
    return ProjectRead.model_validate(created)


@router.get(
    "/{project_id}",
    response_model=ProjectRead,
    status_code=status.HTTP_200_OK,
    summary="Get a project",
    responses=_NOT_FOUND,
)
async def get_project(project_id: uuid.UUID, projects: ProjectServiceDep) -> ProjectRead:
    return ProjectRead.model_validate(await projects.get(project_id))


@router.patch(
    "/{project_id}",
    response_model=ProjectRead,
    status_code=status.HTTP_200_OK,
    summary="Update a project",
    responses=_UNAUTHENTICATED | _NOT_FOUND,
)
async def update_project(
    project_id: uuid.UUID,
    body: ProjectUpdate,
    projects: ProjectServiceDep,
    members: ProjectMemberServiceDep,
    audit: AuditLogServiceDep,
    actor_user_id: ActorUserIdDep,
) -> ProjectRead:
    await members.require_role(project_id, actor_user_id, ProjectRole.developer)
    updated = await projects.update(project_id, body)
    payload = ProjectRead.model_validate(updated)
    await audit.record(
        project_id=project_id,
        action="project.updated",
        resource_type="project",
        resource_id=str(project_id),
        summary=f"Updated project '{payload.name}'",
        actor_user_id=actor_user_id,
    )
    return payload


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a project and its environments",
    responses=_UNAUTHENTICATED | _NOT_FOUND,
)
async def delete_project(
    project_id: uuid.UUID,
    projects: ProjectServiceDep,
    members: ProjectMemberServiceDep,
    actor_user_id: ActorUserIdDep,
) -> None:
    await members.require_role(project_id, actor_user_id, ProjectRole.owner)
    await projects.delete(project_id)


@router.get(
    "/{project_id}/deployments",
    response_model=Page[DeploymentRead],
    status_code=status.HTTP_200_OK,
    summary="List deployments for a project (optionally filtered by environment) (#253)",
    responses=_NOT_FOUND,
)
async def list_project_deployments(
    project_id: uuid.UUID,
    page: PaginationDep,
    projects: ProjectServiceDep,
    deployments: DeploymentServiceDep,
    environment_id: uuid.UUID | None = Query(default=None),
) -> Page[DeploymentRead]:
    await projects.get(project_id)
    items, total = await deployments.list_for_project(
        project_id,
        environment_id=environment_id,
        limit=page.limit,
        offset=page.offset,
    )
    return Page(
        items=[deployment_to_read(row) for row in items],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get(
    "/{project_id}/members",
    response_model=list[ProjectMemberRead],
    status_code=status.HTTP_200_OK,
    summary="List members and roles on a project (#256)",
    responses=_UNAUTHENTICATED | _NOT_FOUND,
)
async def list_project_members(
    project_id: uuid.UUID, members: ProjectMemberServiceDep
) -> list[ProjectMemberRead]:
    return await members.list_members(project_id)


@router.put(
    "/{project_id}/members",
    response_model=ProjectMemberRead,
    status_code=status.HTTP_200_OK,
    summary="Invite or update a project member's role (#256)",
    responses=_UNAUTHENTICATED | _NOT_FOUND,
)
async def upsert_project_member(
    project_id: uuid.UUID,
    body: ProjectMemberUpsertRequest,
    members: ProjectMemberServiceDep,
    audit: AuditLogServiceDep,
    actor_user_id: ActorUserIdDep,
) -> ProjectMemberRead:
    try:
        saved = await members.upsert_member(project_id, body, actor_user_id=actor_user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    await audit.record(
        project_id=project_id,
        action="member.updated",
        resource_type="member",
        resource_id=str(saved.user_id),
        summary=f"Set {saved.email} role to {saved.role.value}",
        actor_user_id=actor_user_id,
    )
    return saved


@router.delete(
    "/{project_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member from a project (#256)",
    responses=_UNAUTHENTICATED | _NOT_FOUND,
)
async def remove_project_member(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    members: ProjectMemberServiceDep,
    audit: AuditLogServiceDep,
    actor_user_id: ActorUserIdDep,
) -> None:
    try:
        await members.remove_member(project_id, user_id, actor_user_id=actor_user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    await audit.record(
        project_id=project_id,
        action="member.removed",
        resource_type="member",
        resource_id=str(user_id),
        summary=f"Removed member {user_id} from project",
        actor_user_id=actor_user_id,
    )


@router.get(
    "/{project_id}/audit-logs",
    response_model=Page[AuditEventRead],
    status_code=status.HTTP_200_OK,
    summary="List activity and audit log events for a project (#257)",
    responses=_UNAUTHENTICATED | _NOT_FOUND,
)
async def list_project_audit_logs(
    project_id: uuid.UUID,
    page: PaginationDep,
    audit: AuditLogServiceDep,
) -> Page[AuditEventRead]:
    items, total = await audit.list_for_project(project_id, limit=page.limit, offset=page.offset)
    return Page(items=items, total=total, limit=page.limit, offset=page.offset)


@router.get(
    "/{project_id}/notifications",
    response_model=NotificationChannelRead | None,
    status_code=status.HTTP_200_OK,
    summary="Get project deployment notification channel (#259)",
    responses=_UNAUTHENTICATED | _NOT_FOUND,
)
async def get_project_notifications(
    project_id: uuid.UUID, notifications: NotificationServiceDep
) -> NotificationChannelRead | None:
    return await notifications.get_for_project(project_id)


@router.put(
    "/{project_id}/notifications",
    response_model=NotificationChannelRead,
    status_code=status.HTTP_200_OK,
    summary="Create or update project deployment notification channel (#259)",
    responses=_UNAUTHENTICATED | _NOT_FOUND,
)
async def upsert_project_notifications(
    project_id: uuid.UUID,
    body: NotificationChannelUpsertRequest,
    notifications: NotificationServiceDep,
    members: ProjectMemberServiceDep,
    actor_user_id: ActorUserIdDep,
) -> NotificationChannelRead:
    await members.require_role(project_id, actor_user_id, ProjectRole.developer)
    try:
        return await notifications.upsert_for_project(project_id, body)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@router.delete(
    "/{project_id}/notifications",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete project deployment notification channel (#259)",
    responses=_UNAUTHENTICATED | _NOT_FOUND,
)
async def delete_project_notifications(
    project_id: uuid.UUID,
    notifications: NotificationServiceDep,
    members: ProjectMemberServiceDep,
    actor_user_id: ActorUserIdDep,
) -> None:
    await members.require_role(project_id, actor_user_id, ProjectRole.developer)
    await notifications.delete_for_project(project_id)


@router.post(
    "/{project_id}/notifications/test",
    response_model=NotificationTestResult,
    status_code=status.HTTP_200_OK,
    summary="Send a test notification to the project's configured channel (#259)",
    responses=_UNAUTHENTICATED | _NOT_FOUND,
)
async def test_project_notifications(
    project_id: uuid.UUID,
    notifications: NotificationServiceDep,
    members: ProjectMemberServiceDep,
    actor_user_id: ActorUserIdDep,
) -> NotificationTestResult:
    await members.require_role(project_id, actor_user_id, ProjectRole.developer)
    return await notifications.send_test(project_id)
