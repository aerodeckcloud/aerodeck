"""Enqueue long-running work for an environment and report on it.

The dispatcher is a protocol so routes never import Celery, and tests can
swap in an in-process implementation.
"""

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.core.errors import ConflictError, UnprocessableError
from app.core.logging import get_logger
from app.repositories.jobs import JobState
from app.services.deployment_logs import (
    PROVISION_QUEUE_FAILED_LINE,
    PROVISION_QUEUED_LINE,
    publish_line,
)
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import DeployOverlap, Environment
from app.services.deployments import DeploymentService
from app.services.environments import EnvironmentService
from app.services.provision_replay import parse_provision_replay, provision_replay_json
from app.services.provisioning import JobKind

logger = get_logger("services.jobs")

MarkDeploymentFailed = Callable[[uuid.UUID], Awaitable[None]]
SetProvisionJobId = Callable[[uuid.UUID, uuid.UUID], Awaitable[None]]

SWAP_STARTED_DETAIL = "Container swap already started."

_TERMINAL_STATUSES = (
    DeploymentStatus.succeeded,
    DeploymentStatus.failed,
    DeploymentStatus.cancelled,
)


def _has_finished(deployment: Deployment) -> bool:
    """True once the row is terminal or the worker stamped its finish time.

    A provision nobody watched in the console stays ``running`` with
    ``finished_at`` set. It no longer occupies the environment.
    """
    return deployment.status in _TERMINAL_STATUSES or deployment.finished_at is not None


class JobDispatcher(Protocol):
    def dispatch(self, kind: JobKind, *, project_id: str, environment_id: str, **meta: str) -> str: ...

    def state(self, job_id: str) -> JobState: ...

    def revoke(self, job_id: str) -> None: ...


class EnvironmentVarLoader(Protocol):
    async def load_decrypted_map(
        self, project_id: uuid.UUID, environment_id: uuid.UUID
    ) -> dict[str, str]: ...


@dataclass(frozen=True, slots=True)
class JobTicket:
    job_id: str | None
    kind: JobKind
    project_id: uuid.UUID
    environment_id: uuid.UUID
    active_deployment_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class DeploymentEnqueueHooks:
    """Isolated deployment side-effects for provision enqueue (fail mark + job id)."""

    mark_failed: MarkDeploymentFailed
    set_provision_job_id: SetProvisionJobId


class JobService:
    def __init__(
        self,
        dispatcher: JobDispatcher,
        environments: EnvironmentService,
        deployments: DeploymentService,
        *,
        redis_url: str | None,
        deployment_hooks: DeploymentEnqueueHooks,
        env_var_loader: EnvironmentVarLoader | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._environments = environments
        self._deployments = deployments
        self._redis_url = redis_url
        self._deployment_hooks = deployment_hooks
        self._env_var_loader = env_var_loader
        self._releasing = False

    async def enqueue(
        self,
        kind: JobKind,
        project_id: uuid.UUID,
        environment_id: uuid.UUID,
        *,
        deployment_id: uuid.UUID | None = None,
        env_vars: dict[str, str] | None = None,
        addons: list[str] | None = None,
        rollback_from_deployment_id: uuid.UUID | None = None,
        git_ref: str | None = None,
    ) -> JobTicket:
        environment = await self._environments.get(project_id, environment_id)
        meta: dict[str, str] = {
            "project_slug": environment.project.slug,
            "environment_slug": environment.slug,
            "environment_kind": environment.kind.value,
            "region": environment.region,
        }
        merged_env: dict[str, str] = {}
        if kind is JobKind.provision and self._env_var_loader is not None:
            merged_env.update(await self._env_var_loader.load_decrypted_map(project_id, environment_id))
        if env_vars:
            merged_env.update(env_vars)
        if merged_env:
            meta["env_vars_json"] = json.dumps(merged_env)
        if addons:
            meta["addons_json"] = json.dumps(addons)
        if rollback_from_deployment_id is not None and kind is JobKind.provision:
            meta["rollback_from_deployment_id"] = str(rollback_from_deployment_id)
        if git_ref and kind is JobKind.provision:
            meta["git_ref"] = git_ref
        linked_deployment_id: uuid.UUID | None = None
        # Only provision publishes onto a deployment log stream (#121).
        if deployment_id is not None and kind is JobKind.provision:
            linked = await self._deployments.get(deployment_id)
            linked_deployment_id = linked.id
            meta["deployment_id"] = str(linked_deployment_id)
            meta["image_deployment_id"] = str(linked_deployment_id)
            meta["repo_url"] = linked.repo_url
            # Lock first, write second: every path that changes deployment rows of
            # this environment takes the lock before the rows.
            await self._environments.lock_for_deploy(environment.id)
            replay_json = provision_replay_json(
                git_ref=git_ref,
                rollback_from_deployment_id=rollback_from_deployment_id,
                addons=addons,
            )
            await self._deployments.record_provision_replay(
                linked_deployment_id, git_ref=git_ref, replay_json=replay_json
            )
            waiting = await self._wait_or_override(environment, linked_deployment_id)
            if waiting is not None:
                return waiting
            # Seed a non-Done line before the worker runs so SSE cannot stub Done
            # while the job is still queued (Terminal would mark succeeded).
            await publish_line(self._redis_url, linked_deployment_id, PROVISION_QUEUED_LINE)
        try:
            # Publishing to the broker is blocking I/O; keep it off the event loop.
            job_id = await run_in_threadpool(
                self._dispatcher.dispatch,
                kind,
                project_id=str(environment.project_id),
                environment_id=str(environment.id),
                **meta,
            )
        except Exception:
            if linked_deployment_id is not None:
                await self._record_dispatch_failure(linked_deployment_id)
            raise
        if linked_deployment_id is not None:
            await self._persist_progress()
            await self._remember_provision_job_id(linked_deployment_id, job_id)
        logger.info("queued %s job %s for environment %s", kind.value, job_id, environment.id)
        return JobTicket(job_id=job_id, kind=kind, project_id=environment.project_id, environment_id=environment.id)

    async def _persist_progress(self) -> None:
        """Commit what this call has written so far.

        Used only where the write has to outlive the request:

        - The broker accepted, refused, or revoked a job. That cannot be undone,
          so the database must record it even if the request fails afterwards.
        - A new deployment was cancelled because of a 409. The error rolls the
          request back, and the cancel must stay.

        It also ends this transaction before the isolated hooks update the same
        deployment row on another connection. PostgreSQL would otherwise make
        that update wait for a commit that is itself waiting on the update.
        """
        await self._deployments.commit()

    async def _record_dispatch_failure(self, deployment_id: uuid.UUID) -> None:
        """Mark a deployment the broker refused as failed, then start what waited on it."""
        try:
            await publish_line(self._redis_url, deployment_id, PROVISION_QUEUE_FAILED_LINE)
        except Exception as publish_exc:
            logger.warning(
                "event=deployment_log_queue_failed_publish_skipped deployment=%s error_type=%s",
                deployment_id,
                type(publish_exc).__name__,
            )
        await self._persist_progress()
        await self._deployment_hooks.mark_failed(deployment_id)
        if self._releasing:
            # release_waiting is already running and continues with this row.
            return
        failed = await self._deployments.get(deployment_id)
        await self._deployments.refresh(failed)
        await self.release_waiting(failed)

    async def _remember_provision_job_id(self, deployment_id: uuid.UUID, job_id: str) -> None:
        """Parse job id then best-effort persist (hook is try_* and must not raise)."""
        try:
            job_uuid = uuid.UUID(job_id)
        except ValueError:
            logger.warning(
                "event=deployment_provision_job_id_skipped deployment=%s job_id=%s error_type=ValueError",
                deployment_id,
                job_id,
            )
            return
        await self._deployment_hooks.set_provision_job_id(deployment_id, job_uuid)

    async def state(self, job_id: uuid.UUID) -> JobState:
        return await run_in_threadpool(self._dispatcher.state, str(job_id))

    async def revoke(self, job_id: uuid.UUID) -> None:
        await run_in_threadpool(self._dispatcher.revoke, str(job_id))

    async def revoke_quietly(self, job_id: uuid.UUID | None) -> None:
        """Revoke when a job id exists. A broker error is logged and does not undo cancel."""
        if job_id is None:
            return
        try:
            await self.revoke(job_id)
        except Exception as revoke_exc:
            logger.warning(
                "event=deployment_revoke_skipped job_id=%s error_type=%s",
                job_id,
                type(revoke_exc).__name__,
            )

    async def release_waiting(self, finished: Deployment) -> None:
        """Start the newest deployment that was waiting on ``finished``.

        Older waiting rows are cancelled. A second caller finds nothing to claim.
        When the claimed deployment cannot start it is marked failed, and
        whatever waited on it is released in turn.
        """
        if self._releasing:
            return
        self._releasing = True
        try:
            current: Deployment | None = finished
            while current is not None:
                current = await self._start_next_waiter(current)
        finally:
            self._releasing = False

    async def _start_next_waiter(self, finished: Deployment) -> Deployment | None:
        """Start one waiter. Returns it only when it failed to start."""
        if finished.environment_id is None or finished.project_id is None:
            return None
        if not _has_finished(finished):
            return None
        await self._environments.lock_for_deploy(finished.environment_id)
        claimed = await self._deployments.claim_newest_waiter(finished.environment_id, finished.id)
        if claimed is None or claimed.project_id is None or claimed.environment_id is None:
            return None
        git_ref, rollback_from, replay_addons = parse_provision_replay(claimed.provision_replay_json)
        try:
            await self.enqueue(
                JobKind.provision,
                claimed.project_id,
                claimed.environment_id,
                deployment_id=claimed.id,
                git_ref=git_ref,
                rollback_from_deployment_id=rollback_from,
                addons=replay_addons,
            )
        except ConflictError:
            # Override met a swap in progress; enqueue already cancelled the claimed row.
            logger.info("event=deployment_wait_release_blocked deployment=%s", claimed.id)
            return None
        except SQLAlchemyError:
            # The session is unusable and nothing below could be saved. Let the caller fail.
            raise
        except Exception as release_exc:
            # The outcome that triggered this release is already decided, so it must
            # not turn into an error. The waiter did not start, so it must not stay
            # ``running`` and block the environment.
            logger.warning(
                "event=deployment_wait_release_failed deployment=%s error_type=%s",
                claimed.id,
                type(release_exc).__name__,
                exc_info=release_exc,
            )
            await self._persist_progress()
            await self._deployment_hooks.mark_failed(claimed.id)
            await self._deployments.refresh(claimed)
            return claimed
        return None

    async def _override(
        self, environment: Environment, deployment_id: uuid.UUID, blockers: list[Deployment]
    ) -> None:
        """Cancel the active deploys, or refuse with 409 once one has begun its swap."""
        revoke: list[uuid.UUID | None] = []
        try:
            if any(row.swap_started_at is not None for row in blockers):
                raise ConflictError(SWAP_STARTED_DETAIL)
            for row in blockers:
                try:
                    cancelled = await self._deployments.cancel(row.id)
                except UnprocessableError:
                    # Finished between the read and the cancel. Nothing left to stop.
                    continue
                revoke.append(cancelled.provision_job_id)
        except ConflictError:
            # The swap is left alone. The new row never started, so close it.
            await self._deployments.cancel(deployment_id)
            await self._persist_progress()
            raise
        superseded = await self._deployments.cancel_held(environment.id, keep_id=None)
        # Save the cancels before revoking: a revoked job with a row still marked
        # running would block the environment with nothing left to finish it.
        await self._persist_progress()
        for job_id in revoke:
            await self.revoke_quietly(job_id)
        logger.info(
            "event=deployment_override deployment=%s cancelled=%d superseded=%d",
            deployment_id,
            len(revoke),
            superseded,
        )

    async def _wait_or_override(
        self, environment: Environment, deployment_id: uuid.UUID
    ) -> JobTicket | None:
        """Hold, preempt, or return None so the caller dispatches.

        The caller holds the environment lock, so the rows read here cannot be
        claimed or held by another request until this transaction ends.
        """
        blockers = list(await self._deployments.list_blocking(environment.id, deployment_id))
        if not blockers:
            return None
        if environment.deploy_overlap == DeployOverlap.override:
            await self._override(environment, deployment_id, blockers)
            return None
        active = blockers[0]
        superseded = await self._deployments.cancel_held(environment.id, keep_id=deployment_id)
        await self._deployments.mark_waiting(deployment_id, active.id)
        logger.info(
            "event=deployment_waiting deployment=%s active=%s superseded=%d",
            deployment_id,
            active.id,
            superseded,
        )
        return JobTicket(
            job_id=None,
            kind=JobKind.provision,
            project_id=environment.project_id,
            environment_id=environment.id,
            active_deployment_id=active.id,
        )
