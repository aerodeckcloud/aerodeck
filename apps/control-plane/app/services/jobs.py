"""Enqueue long-running work for an environment and report on it.

The dispatcher is a protocol so routes never import Celery, and tests can
swap in an in-process implementation.
"""

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from starlette.concurrency import run_in_threadpool

from app.core.logging import get_logger
from app.repositories.jobs import JobState
from app.services.deployment_logs import (
    PROVISION_QUEUE_FAILED_LINE,
    PROVISION_QUEUED_LINE,
    publish_line,
)
from app.services.deployments import DeploymentService
from app.services.environments import EnvironmentService
from app.services.provisioning import JobKind

logger = get_logger("services.jobs")

MarkDeploymentFailed = Callable[[uuid.UUID], Awaitable[None]]
SetProvisionJobId = Callable[[uuid.UUID, uuid.UUID], Awaitable[None]]


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
    job_id: str
    kind: JobKind
    project_id: uuid.UUID
    environment_id: uuid.UUID


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
                # Own session+commit: the request that raised will roll back.
                await self._deployment_hooks.mark_failed(linked_deployment_id)
            raise
        if linked_deployment_id is not None:
            await self._remember_provision_job_id(linked_deployment_id, job_id)
        logger.info("queued %s job %s for environment %s", kind.value, job_id, environment.id)
        return JobTicket(job_id=job_id, kind=kind, project_id=environment.project_id, environment_id=environment.id)

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
