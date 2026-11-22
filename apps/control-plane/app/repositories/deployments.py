import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deployment import Deployment, DeploymentStatus


class DeploymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, deployment: Deployment) -> Deployment:
        self._session.add(deployment)
        await self._session.flush()
        return deployment

    async def get(self, deployment_id: uuid.UUID) -> Deployment | None:
        return await self._session.get(Deployment, deployment_id)

    async def cancel_before_swap(
        self, deployment_id: uuid.UUID, finished_at: datetime
    ) -> Deployment | None:
        """Mark cancelled only while the row is still queued or running and swap has not started."""
        updated = await self._session.scalar(
            update(Deployment)
            .where(
                Deployment.id == deployment_id,
                Deployment.status.in_((DeploymentStatus.queued, DeploymentStatus.running)),
                Deployment.swap_started_at.is_(None),
            )
            .values(status=DeploymentStatus.cancelled, finished_at=finished_at)
            .returning(Deployment)
        )
        return updated

    async def begin_swap(self, deployment_id: uuid.UUID, started_at: datetime) -> bool:
        """Stamp swap start. False when the row was cancelled or is already terminal."""
        updated = await self._session.scalar(
            update(Deployment)
            .where(
                Deployment.id == deployment_id,
                Deployment.status.in_((DeploymentStatus.queued, DeploymentStatus.running)),
                Deployment.swap_started_at.is_(None),
            )
            .values(swap_started_at=started_at)
            .returning(Deployment.id)
        )
        if updated is not None:
            return True
        current = await self.get(deployment_id)
        return (
            current is not None
            and current.swap_started_at is not None
            and current.status in (DeploymentStatus.queued, DeploymentStatus.running)
        )

    async def save(self, deployment: Deployment) -> Deployment:
        await self.add(deployment)
        await self._session.refresh(deployment)
        return deployment

    async def clear_environment_link(self, environment_id: uuid.UUID) -> None:
        """Null both link columns so the both-or-neither invariant survives env delete."""
        await self._clear_link(Deployment.environment_id, environment_id)

    async def clear_project_link(self, project_id: uuid.UUID) -> None:
        """Null both link columns so the both-or-neither invariant survives project delete."""
        await self._clear_link(Deployment.project_id, project_id)

    async def _clear_link(
        self, column: ColumnElement[uuid.UUID | None], value: uuid.UUID
    ) -> None:
        await self._session.execute(
            update(Deployment)
            .where(column == value)
            .values(project_id=None, environment_id=None)
        )

    async def list(self, *, limit: int, offset: int) -> tuple[Sequence[Deployment], int]:
        total = await self._session.scalar(select(func.count()).select_from(Deployment))
        rows = await self._session.scalars(
            select(Deployment).order_by(Deployment.started_at.desc(), Deployment.id.desc()).limit(limit).offset(offset)
        )
        return rows.all(), int(total or 0)

    async def list_for_project(
        self,
        project_id: uuid.UUID,
        *,
        environment_id: uuid.UUID | None = None,
        limit: int,
        offset: int,
    ) -> tuple[Sequence[Deployment], int]:
        base = select(Deployment).where(Deployment.project_id == project_id)
        if environment_id is not None:
            base = base.where(Deployment.environment_id == environment_id)
        total = await self._session.scalar(select(func.count()).select_from(base.subquery()))
        rows = await self._session.scalars(
            base.order_by(Deployment.started_at.desc(), Deployment.id.desc()).limit(limit).offset(offset)
        )
        return rows.all(), int(total or 0)

    async def list_blocking(
        self, environment_id: uuid.UUID, exclude_id: uuid.UUID
    ) -> Sequence[Deployment]:
        """Queued or running deploys that are not themselves waiting."""
        rows = await self._session.scalars(
            select(Deployment)
            .where(
                Deployment.environment_id == environment_id,
                Deployment.status.in_((DeploymentStatus.queued, DeploymentStatus.running)),
                Deployment.held_for_deployment_id.is_(None),
                Deployment.id != exclude_id,
            )
            .order_by(Deployment.started_at.asc(), Deployment.id.asc())
        )
        return rows.all()

    async def cancel_held(
        self, environment_id: uuid.UUID, finished_at: datetime, *, keep_id: uuid.UUID | None
    ) -> None:
        """Cancel queued rows that are waiting, except ``keep_id`` when it is set."""
        statement = update(Deployment).where(
            Deployment.environment_id == environment_id,
            Deployment.held_for_deployment_id.is_not(None),
            Deployment.status == DeploymentStatus.queued,
        )
        if keep_id is not None:
            statement = statement.where(Deployment.id != keep_id)
        await self._session.execute(
            statement.values(status=DeploymentStatus.cancelled, finished_at=finished_at)
        )

    async def mark_waiting(self, deployment_id: uuid.UUID, active_id: uuid.UUID) -> None:
        stored = await self._session.get(Deployment, deployment_id)
        if stored is None:
            return
        stored.status = DeploymentStatus.queued
        stored.held_for_deployment_id = active_id
        await self._session.flush()
        await self._session.refresh(stored)

    async def record_provision_replay(
        self, deployment_id: uuid.UUID, *, git_ref: str | None, replay_json: str | None
    ) -> None:
        if not git_ref and replay_json is None:
            return
        stored = await self._session.get(Deployment, deployment_id)
        if stored is None:
            return
        if git_ref:
            stored.git_ref = git_ref
        if replay_json is not None:
            stored.provision_replay_json = replay_json
        await self._session.flush()
        await self._session.refresh(stored)

    async def claim_newest_waiter(
        self, environment_id: uuid.UUID, finished_id: uuid.UUID, finished_at: datetime
    ) -> Deployment | None:
        """Start the newest row waiting on ``finished_id`` and cancel the older ones."""
        rows = list(
            (
                await self._session.scalars(
                    select(Deployment)
                    .where(
                        Deployment.environment_id == environment_id,
                        Deployment.held_for_deployment_id == finished_id,
                        Deployment.status == DeploymentStatus.queued,
                    )
                    .order_by(Deployment.started_at.desc(), Deployment.id.desc())
                )
            ).all()
        )
        if not rows:
            return None
        newest = rows[0]
        for row in rows[1:]:
            row.status = DeploymentStatus.cancelled
            row.finished_at = finished_at
        newest.status = DeploymentStatus.running
        newest.held_for_deployment_id = None
        await self._session.flush()
        await self._session.refresh(newest)
        return newest

    async def commit(self) -> None:
        await self._session.commit()

    async def refresh(self, deployment: Deployment) -> None:
        await self._session.refresh(deployment)
