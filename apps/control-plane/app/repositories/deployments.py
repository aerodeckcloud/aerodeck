import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deployment import Deployment, DeploymentStatus


def _awaiting_approval(deployment_id: uuid.UUID) -> tuple[ColumnElement[bool], ...]:
    """Rows still waiting for an owner. Shared by approve and reject (#276)."""
    return (
        Deployment.id == deployment_id,
        Deployment.approval_required_at.is_not(None),
        Deployment.approved_at.is_(None),
        Deployment.status == DeploymentStatus.queued,
        Deployment.finished_at.is_(None),
        Deployment.dispatched_at.is_(None),
    )


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
            # A cancelled row never starts, so what it kept for a later start goes too.
            .values(
                status=DeploymentStatus.cancelled,
                finished_at=finished_at,
                provision_replay_json=None,
            )
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

    async def list(
        self, *, limit: int, offset: int, actor_user_id: uuid.UUID | None = None
    ) -> tuple[Sequence[Deployment], int]:
        visible = Deployment.status != DeploymentStatus.draft
        if actor_user_id is not None:
            visible = or_(visible, Deployment.actor_user_id == actor_user_id)
        total = await self._session.scalar(select(func.count()).select_from(Deployment).where(visible))
        rows = await self._session.scalars(
            select(Deployment)
            .where(visible)
            .order_by(Deployment.started_at.desc(), Deployment.id.desc())
            .limit(limit)
            .offset(offset)
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

    async def list_succeeded(self, environment_id: uuid.UUID) -> Sequence[Deployment]:
        """Succeeded rows for one environment, oldest first."""
        rows = await self._session.scalars(
            select(Deployment).where(
                Deployment.environment_id == environment_id,
                Deployment.status == DeploymentStatus.succeeded,
            )
        )
        return rows.all()

    async def count_retained_images(
        self, environment_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """Succeeded deployments whose image tag has not been pruned (#274)."""
        if not environment_ids:
            return {}
        rows = await self._session.execute(
            select(Deployment.environment_id, func.count())
            .where(
                Deployment.environment_id.in_(tuple(environment_ids)),
                Deployment.status == DeploymentStatus.succeeded,
                Deployment.image_pruned_at.is_(None),
            )
            .group_by(Deployment.environment_id)
        )
        return {
            environment_id: int(count)
            for environment_id, count in rows.all()
            if environment_id is not None
        }

    async def mark_images_pruned(
        self, deployment_ids: Sequence[uuid.UUID], pruned_at: datetime
    ) -> None:
        if not deployment_ids:
            return
        await self._session.execute(
            update(Deployment)
            .where(
                Deployment.id.in_(tuple(deployment_ids)),
                Deployment.image_pruned_at.is_(None),
            )
            .values(image_pruned_at=pruned_at)
        )

    async def list_active(
        self, environment_id: uuid.UUID, exclude_id: uuid.UUID
    ) -> Sequence[Deployment]:
        """Deploys that occupy the environment, oldest first.

        A row is active once the queue accepted its job (``dispatched_at``) and
        until it ends. The worker stamps ``finished_at`` when a provision ends
        while the row stays ``running`` until the console reports the outcome,
        so the finish time counts as the end as well.
        """
        rows = await self._session.scalars(
            select(Deployment)
            .where(
                Deployment.environment_id == environment_id,
                Deployment.status.in_((DeploymentStatus.queued, DeploymentStatus.running)),
                Deployment.dispatched_at.is_not(None),
                Deployment.finished_at.is_(None),
                Deployment.id != exclude_id,
            )
            .order_by(Deployment.started_at.asc(), Deployment.id.asc())
        )
        return rows.all()

    async def mark_dispatched(self, deployment_id: uuid.UUID, dispatched_at: datetime) -> None:
        stored = await self._session.get(Deployment, deployment_id)
        if stored is None:
            return
        stored.dispatched_at = dispatched_at
        await self._session.flush()
        await self._session.refresh(stored)

    async def mark_awaiting_approval(
        self, deployment_id: uuid.UUID, required_at: datetime, replay_json: str | None
    ) -> Deployment | None:
        """Queue the row until an owner approves. A dispatched row is left alone."""
        stored = await self._session.get(Deployment, deployment_id)
        if stored is None or stored.dispatched_at is not None or stored.approved_at is not None:
            return stored
        stored.status = DeploymentStatus.queued
        stored.approval_required_at = required_at
        stored.provision_replay_json = replay_json
        await self._session.flush()
        await self._session.refresh(stored)
        return stored

    async def mark_approved(self, deployment_id: uuid.UUID, approved_at: datetime) -> Deployment | None:
        """Stamp approval only while the row is still waiting for an owner.

        A second approve, or an approve after reject or dispatch, updates nothing.
        """
        updated = await self._session.scalar(
            update(Deployment)
            .where(*_awaiting_approval(deployment_id))
            .values(approved_at=approved_at)
            .returning(Deployment)
        )
        return updated

    async def cancel_awaiting_approval(
        self, deployment_id: uuid.UUID, finished_at: datetime
    ) -> Deployment | None:
        """Cancel only while the row is still waiting for an owner.

        An approved or dispatched row is left alone, so a late reject cannot
        stop a deploy that already won the race.
        """
        updated = await self._session.scalar(
            update(Deployment)
            .where(*_awaiting_approval(deployment_id))
            .values(
                status=DeploymentStatus.cancelled,
                finished_at=finished_at,
                provision_replay_json=None,
            )
            .returning(Deployment)
        )
        return updated

    async def mark_waiting(
        self, deployment_id: uuid.UUID, active_id: uuid.UUID, replay_json: str | None
    ) -> None:
        """Hold the row behind ``active_id`` and keep what it needs to start later."""
        stored = await self._session.get(Deployment, deployment_id)
        if stored is None:
            return
        stored.status = DeploymentStatus.queued
        stored.held_for_deployment_id = active_id
        stored.provision_replay_json = replay_json
        await self._session.flush()
        await self._session.refresh(stored)

    async def cancel_waiting(
        self, environment_id: uuid.UUID, finished_at: datetime, *, keep_id: uuid.UUID | None
    ) -> int:
        """Cancel the waiting rows of an environment, except ``keep_id``. Returns how many."""
        statement = update(Deployment).where(
            Deployment.environment_id == environment_id,
            Deployment.held_for_deployment_id.is_not(None),
            Deployment.status == DeploymentStatus.queued,
        )
        if keep_id is not None:
            statement = statement.where(Deployment.id != keep_id)
        result = await self._session.execute(
            statement.values(
                status=DeploymentStatus.cancelled,
                finished_at=finished_at,
                provision_replay_json=None,
            )
        )
        return int(result.rowcount or 0)

    async def claim_newest_waiter(
        self, environment_id: uuid.UUID, finished_id: uuid.UUID, finished_at: datetime
    ) -> tuple[Deployment, str | None] | None:
        """Take the newest row waiting on ``finished_id`` and cancel the older ones.

        Returns the row, no longer waiting, with the payload it had kept. The
        payload is removed from every row touched here.
        """
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
        replay_json = newest.provision_replay_json
        for row in rows[1:]:
            row.status = DeploymentStatus.cancelled
            row.finished_at = finished_at
            row.provision_replay_json = None
        newest.status = DeploymentStatus.running
        newest.held_for_deployment_id = None
        newest.provision_replay_json = None
        await self._session.flush()
        await self._session.refresh(newest)
        return newest, replay_json

    async def commit(self) -> None:
        await self._session.commit()

    async def refresh(self, deployment: Deployment) -> None:
        await self._session.refresh(deployment)
