"""Project membership repository, schemas, and RBAC enforcement (#256)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, NotFoundError
from app.core.logging import get_logger
from app.models.console_user import ConsoleUser
from app.models.project import Project
from app.models.project_member import ProjectMember, ProjectRole
from app.repositories.console_users import ConsoleUserRepository

logger = get_logger("services.project_members")

_ROLE_RANK: dict[ProjectRole, int] = {
    ProjectRole.viewer: 1,
    ProjectRole.developer: 2,
    ProjectRole.owner: 3,
}


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class ProjectLookup(Protocol):
    async def get(self, project_id: uuid.UUID) -> Project: ...


class ProjectMemberUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: str = Field(min_length=3, max_length=254)
    role: ProjectRole = ProjectRole.developer

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        lowered = value.strip().lower()
        if "@" not in lowered or lowered.startswith("@") or lowered.endswith("@"):
            raise ValueError("Enter a valid email address")
        return lowered


class ProjectMemberRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    role: ProjectRole
    created_at: datetime
    updated_at: datetime


class ProjectMemberRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_with_users(
        self, project_id: uuid.UUID
    ) -> Sequence[tuple[ProjectMember, ConsoleUser]]:
        result = await self._session.execute(
            select(ProjectMember, ConsoleUser)
            .join(ConsoleUser, ConsoleUser.id == ProjectMember.user_id)
            .where(ProjectMember.project_id == project_id)
            .order_by(ConsoleUser.email)
        )
        return result.all()

    async def get(self, project_id: uuid.UUID, user_id: uuid.UUID) -> ProjectMember | None:
        result = await self._session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def count_for_project(self, project_id: uuid.UUID) -> int:
        count = await self._session.scalar(
            select(func.count()).select_from(ProjectMember).where(ProjectMember.project_id == project_id)
        )
        return int(count or 0)

    async def count_owners(self, project_id: uuid.UUID) -> int:
        count = await self._session.scalar(
            select(func.count())
            .select_from(ProjectMember)
            .where(ProjectMember.project_id == project_id, ProjectMember.role == ProjectRole.owner)
        )
        return int(count or 0)

    async def upsert(
        self, *, project_id: uuid.UUID, user_id: uuid.UUID, role: ProjectRole
    ) -> ProjectMember:
        existing = await self.get(project_id, user_id)
        if existing is not None:
            existing.role = role
            await self._session.flush()
            return existing
        row = ProjectMember(project_id=project_id, user_id=user_id, role=role)
        self._session.add(row)
        await self._session.flush()
        return row

    async def delete(self, row: ProjectMember) -> None:
        await self._session.delete(row)
        await self._session.flush()


class ProjectMemberService:
    def __init__(
        self,
        repo: ProjectMemberRepository,
        users: ConsoleUserRepository,
        projects: ProjectLookup,
    ) -> None:
        self._repo = repo
        self._users = users
        self._projects = projects

    async def require_role(
        self,
        project_id: uuid.UUID,
        actor_user_id: uuid.UUID | None,
        minimum_role: ProjectRole,
    ) -> None:
        """Enforce RBAC when an operator user id is supplied and project has members."""
        if actor_user_id is None:
            return
        total = await self._repo.count_for_project(project_id)
        if total == 0:
            # Legacy project created before #256 — allow until membership is configured.
            return
        member = await self._repo.get(project_id, actor_user_id)
        if member is None or _ROLE_RANK[member.role] < _ROLE_RANK[minimum_role]:
            raise ForbiddenError(
                f"Requires {minimum_role.value} role on project {project_id}."
            )

    async def has_members(self, project_id: uuid.UUID) -> bool:
        return await self._repo.count_for_project(project_id) > 0

    async def is_owner(self, project_id: uuid.UUID, actor_user_id: uuid.UUID | None) -> bool:
        """True when this user is an owner, or the project has no members yet.

        A project with no members has nobody to approve a deploy, so callers
        treat every actor as an owner and do not hold the row (#276).
        """
        if actor_user_id is None:
            return False
        if not await self.has_members(project_id):
            return True
        member = await self._repo.get(project_id, actor_user_id)
        return member is not None and _ROLE_RANK[member.role] >= _ROLE_RANK[ProjectRole.owner]

    async def assign_owner_if_user_exists(
        self, project_id: uuid.UUID, user_id: uuid.UUID | None
    ) -> None:
        if user_id is None:
            return
        user = await self._users.get(user_id)
        if user is None:
            return
        await self._repo.upsert(project_id=project_id, user_id=user.id, role=ProjectRole.owner)

    async def list_members(self, project_id: uuid.UUID) -> list[ProjectMemberRead]:
        await self._projects.get(project_id)
        rows = await self._repo.list_with_users(project_id)
        return [
            ProjectMemberRead(
                id=member.id,
                project_id=member.project_id,
                user_id=member.user_id,
                email=user.email,
                role=member.role,
                created_at=member.created_at,
                updated_at=member.updated_at,
            )
            for member, user in rows
        ]

    async def upsert_member(
        self,
        project_id: uuid.UUID,
        data: ProjectMemberUpsertRequest,
        *,
        actor_user_id: uuid.UUID | None = None,
    ) -> ProjectMemberRead:
        await self._projects.get(project_id)
        await self.require_role(project_id, actor_user_id, ProjectRole.owner)
        user = await self._users.get_by_email(data.email)
        if user is None:
            raise NotFoundError(f"Console user {data.email!r} not found")
        existing = await self._repo.get(project_id, user.id)
        if (
            existing is not None
            and existing.role is ProjectRole.owner
            and data.role is not ProjectRole.owner
            and await self._repo.count_owners(project_id) <= 1
        ):
            raise ValueError("Cannot demote the last owner of a project.")
        member = await self._repo.upsert(project_id=project_id, user_id=user.id, role=data.role)
        logger.info("set project %s member %s role=%s", project_id, user.id, data.role.value)
        return ProjectMemberRead(
            id=member.id,
            project_id=member.project_id,
            user_id=member.user_id,
            email=user.email,
            role=member.role,
            created_at=member.created_at,
            updated_at=member.updated_at,
        )

    async def remove_member(
        self,
        project_id: uuid.UUID,
        target_user_id: uuid.UUID,
        *,
        actor_user_id: uuid.UUID | None = None,
    ) -> None:
        await self._projects.get(project_id)
        await self.require_role(project_id, actor_user_id, ProjectRole.owner)
        member = await self._repo.get(project_id, target_user_id)
        if member is None:
            raise NotFoundError(f"Member {target_user_id} not found on project {project_id}")
        if member.role is ProjectRole.owner and await self._repo.count_owners(project_id) <= 1:
            raise ValueError("Cannot remove the last owner of a project.")
        await self._repo.delete(member)
        logger.info("removed project %s member %s", project_id, target_user_id)
