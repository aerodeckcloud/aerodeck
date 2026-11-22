"""Enqueue long-running work for an environment and report on it.

The dispatcher is a protocol so routes never import Celery, and tests can
swap in an in-process implementation.

One deployment runs per environment at a time (#271). A provision that finds
another deploy active either waits behind it or replaces it, depending on the
environment's ``deploy_overlap``. Every path that changes the deployment rows
of an environment takes the environment lock first, so these decisions run one
at a time and always lock in the same order.
"""

import json
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.core.errors import ConflictError, UnprocessableError
from app.core.logging import get_logger
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import DeployOverlap, Environment
from app.repositories.jobs import JobState
from app.services.deployment_logs import (
    PROVISION_QUEUE_FAILED_LINE,
    PROVISION_QUEUED_LINE,
    publish_line,
)
from app.services.deployments import DeploymentService
from app.services.environments import EnvironmentService
from app.services.provision_replay import ProvisionReplay
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


class JobDispatcher(Protocol):
    def dispatch(self, kind: JobKind, *, project_id: str, environment_id: str, **meta: str) -> str: ...

    def state(self, job_id: str) -> JobState: ...

    def revoke(self, job_id: str) -> None: ...


class EnvironmentVarLoader(Protocol):
    async def load_decrypted_map(
        self, project_id: uuid.UUID, environment_id: uuid.UUID
    ) -> dict[str, str]: ...

    def seal(self, values: dict[str, str]) -> str: ...

    def unseal(self, token: str) -> dict[str, str]: ...


@dataclass(frozen=True, slots=True)
class JobTicket:
    """A queued job, or a deployment waiting behind ``active_deployment_id``."""

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


@dataclass(frozen=True, slots=True)
class _Admission:
    """Outcome of the overlap check for a new provision."""

    wait_behind: Deployment | None = None
    superseded_jobs: tuple[uuid.UUID, ...] = ()


def _has_finished(deployment: Deployment) -> bool:
    """True once the row is terminal or the worker stamped its finish time.

    A provision nobody watched in the console stays ``running`` with
    ``finished_at`` set. It no longer occupies the environment.
    """
    return deployment.status in _TERMINAL_STATUSES or deployment.finished_at is not None


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
        admission = _Admission()
        # Only provision publishes onto a deployment log stream (#121).
        if deployment_id is not None and kind is JobKind.provision:
            linked = await self._deployments.get(deployment_id)
            linked_deployment_id = linked.id
            meta["deployment_id"] = str(linked_deployment_id)
            meta["image_deployment_id"] = str(linked_deployment_id)
            meta["repo_url"] = linked.repo_url
            # Held until the commit that follows the dispatch (or the end of the
            # request when this provision waits).
            await self._environments.lock_for_deploy(environment.id)
            admission = await self._admit(environment, linked_deployment_id)
            if admission.wait_behind is not None:
                return await self._hold(
                    environment,
                    linked_deployment_id,
                    admission.wait_behind,
                    ProvisionReplay(
                        git_ref=git_ref,
                        rollback_from_deployment_id=rollback_from_deployment_id,
                        addons=addons,
                        sealed_env_vars=self._seal(env_vars),
                    ),
                )
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
                try:
                    await publish_line(
                        self._redis_url, linked_deployment_id, PROVISION_QUEUE_FAILED_LINE
                    )
                except Exception as publish_exc:
                    logger.warning(
                        "event=deployment_log_queue_failed_publish_skipped deployment=%s error_type=%s",
                        linked_deployment_id,
                        type(publish_exc).__name__,
                    )
                await self._fail_unstarted(linked_deployment_id)
                await self._revoke_all(admission.superseded_jobs)
            raise
        if linked_deployment_id is not None:
            # From here the row is the active deploy of its environment.
            await self._deployments.mark_dispatched(linked_deployment_id)
            await self._persist_progress()
            await self._revoke_all(admission.superseded_jobs)
            await self._remember_provision_job_id(linked_deployment_id, job_id)
        logger.info("queued %s job %s for environment %s", kind.value, job_id, environment.id)
        return JobTicket(job_id=job_id, kind=kind, project_id=environment.project_id, environment_id=environment.id)

    async def release_waiting(self, finished: Deployment) -> None:
        """Start the newest deployment that waited on ``finished``.

        The console outcome routes call this, and so does the worker when a
        provision ends. Whichever comes second finds nothing to claim. A waiter
        that cannot start is marked failed; the outcome that triggered the
        release stands either way.
        """
        if finished.environment_id is None or finished.project_id is None:
            return
        if not _has_finished(finished):
            return
        await self._environments.lock_for_deploy(finished.environment_id)
        claimed = await self._deployments.claim_newest_waiter(finished.environment_id, finished.id)
        if claimed is None:
            return
        waiter, replay_json = claimed
        try:
            replay = ProvisionReplay.from_json(replay_json)
            await self.enqueue(
                JobKind.provision,
                finished.project_id,
                finished.environment_id,
                deployment_id=waiter.id,
                env_vars=self._unseal(replay.sealed_env_vars),
                addons=replay.addons,
                rollback_from_deployment_id=replay.rollback_from_deployment_id,
                git_ref=replay.git_ref,
            )
        except ConflictError:
            # Override met a swap in progress. ``_override`` already cancelled the waiter.
            logger.info("event=deployment_wait_release_blocked deployment=%s", waiter.id)
        except SQLAlchemyError:
            # The session is unusable, so nothing below could be saved. Let the caller fail.
            raise
        except Exception as release_exc:
            logger.warning(
                "event=deployment_wait_release_failed deployment=%s error_type=%s",
                waiter.id,
                type(release_exc).__name__,
                exc_info=release_exc,
            )
            await self._fail_unstarted(waiter.id)

    async def _admit(self, environment: Environment, deployment_id: uuid.UUID) -> _Admission:
        """Decide what a new provision does. The caller holds the environment lock."""
        active = list(await self._deployments.list_active(environment.id, deployment_id))
        if not active:
            return _Admission()
        if environment.deploy_overlap != DeployOverlap.override:
            return _Admission(wait_behind=active[0])
        return _Admission(
            superseded_jobs=await self._override(environment, deployment_id, active)
        )

    async def _hold(
        self,
        environment: Environment,
        deployment_id: uuid.UUID,
        active: Deployment,
        replay: ProvisionReplay,
    ) -> JobTicket:
        """Keep the row queued behind ``active``. Only the newest waiter survives."""
        superseded = await self._deployments.cancel_waiting(environment.id, keep_id=deployment_id)
        await self._deployments.mark_waiting(deployment_id, active.id, replay.to_json())
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

    async def _override(
        self, environment: Environment, deployment_id: uuid.UUID, active: list[Deployment]
    ) -> tuple[uuid.UUID, ...]:
        """Cancel the active deploys, or refuse with 409 once one has begun its swap.

        Returns the job ids to revoke. The caller revokes them after its commit:
        a revoked job whose row still said running would hold the environment
        with nothing left to finish it.
        """
        jobs: list[uuid.UUID] = []
        try:
            if any(row.swap_started_at is not None for row in active):
                raise ConflictError(SWAP_STARTED_DETAIL)
            for row in active:
                try:
                    cancelled = await self._deployments.cancel(row.id)
                except UnprocessableError:
                    # Finished between the read and the cancel. Nothing left to stop.
                    continue
                if cancelled.provision_job_id is not None:
                    jobs.append(cancelled.provision_job_id)
        except ConflictError:
            # The swap is left alone. The new row never started, so close it.
            await self._deployments.cancel(deployment_id)
            await self._persist_progress()
            await self._revoke_all(jobs)
            raise
        superseded = await self._deployments.cancel_waiting(environment.id, keep_id=None)
        logger.info(
            "event=deployment_override deployment=%s cancelled=%d superseded=%d",
            deployment_id,
            len(jobs),
            superseded,
        )
        return tuple(jobs)

    async def _persist_progress(self) -> None:
        """Commit what this call has written so far.

        Used only where a write has to outlive the request:

        - The queue accepted or refused a job. That cannot be undone, so the
          database must say so even if the request fails afterwards.
        - A new deployment was cancelled because of a 409. The error rolls the
          request back, and the cancel must stay.

        It also ends this transaction before the isolated hooks update the same
        deployment row on another connection. PostgreSQL would otherwise make
        that update wait for a commit that is itself waiting on the update.
        """
        await self._deployments.commit()

    async def _fail_unstarted(self, deployment_id: uuid.UUID) -> None:
        """Mark a deployment that could not start as failed.

        The row was never dispatched, so nothing can be waiting behind it.
        """
        await self._persist_progress()
        await self._deployment_hooks.mark_failed(deployment_id)

    def _seal(self, env_vars: dict[str, str] | None) -> str | None:
        """Encrypt one-off variables so a waiting deployment starts with them."""
        if not env_vars:
            return None
        if self._env_var_loader is None:
            raise RuntimeError("One-off variables cannot be kept without the variable service.")
        return self._env_var_loader.seal(env_vars)

    def _unseal(self, token: str | None) -> dict[str, str] | None:
        if not token:
            return None
        if self._env_var_loader is None:
            raise RuntimeError("One-off variables cannot be read without the variable service.")
        return self._env_var_loader.unseal(token)

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

    async def _revoke_all(self, job_ids: Iterable[uuid.UUID]) -> None:
        for job_id in job_ids:
            await self.revoke_quietly(job_id)
