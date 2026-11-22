"""Secret-free project activity and audit log service (#257)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_event import AuditEvent
from app.models.project import Project
from app.repositories.console_users import ConsoleUserRepository


class ProjectLookup(Protocol):
    async def get(self, project_id: uuid.UUID) -> Project: ...


class AuditEventRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    environment_id: uuid.UUID | None
    actor: str
    action: str
    resource_type: str
    resource_id: str | None
    summary: str
    created_at: datetime


class AuditLogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: AuditEvent) -> AuditEvent:
        self._session.add(event)
        await self._session.flush()
        return event

    async def list_for_project(
        self, project_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[Sequence[AuditEvent], int]:
        base = select(AuditEvent).where(AuditEvent.project_id == project_id)
        total = await self._session.scalar(select(func.count()).select_from(base.subquery()))
        rows = await self._session.scalars(
            base.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return rows.all(), int(total or 0)


class AuditLogService:
    def __init__(
        self,
        repo: AuditLogRepository,
        projects: ProjectLookup,
        users: ConsoleUserRepository,
    ) -> None:
        self._repo = repo
        self._projects = projects
        self._users = users

    async def record(
        self,
        *,
        project_id: uuid.UUID,
        action: str,
        resource_type: str,
        summary: str,
        environment_id: uuid.UUID | None = None,
        resource_id: str | None = None,
        actor_user_id: uuid.UUID | None = None,
        actor_label: str | None = None,
    ) -> AuditEvent:
        actor = actor_label or "operator"
        if actor_user_id is not None:
            user = await self._users.get(actor_user_id)
            actor = user.email if user is not None else str(actor_user_id)
        return await self._repo.add(
            AuditEvent(
                project_id=project_id,
                environment_id=environment_id,
                actor=actor[:254],
                action=action[:64],
                resource_type=resource_type[:64],
                resource_id=resource_id[:128] if resource_id else None,
                summary=summary[:500],
            )
        )

    async def list_for_project(
        self, project_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[AuditEventRead], int]:
        await self._projects.get(project_id)
        rows, total = await self._repo.list_for_project(project_id, limit=limit, offset=offset)
        return [AuditEventRead.model_validate(row) for row in rows], total
