"""Start a waiting deployment from the worker when a provision ends (#271).

The API releases a waiting deployment when the console reports an outcome. A
deploy that nobody watches, a push webhook for example, ends only in the
worker, so the worker releases too. Both go through
``JobService.release_waiting``.

Like the other worker writers this runs on a private NullPool engine created
inside ``asyncio.run``. Reusing ``SessionLocal`` from that loop poisons asyncpg
connections that belong to the API loop.

Import this module lazily from ``app.tasks.environments``. It imports
``services.jobs``, which imports ``repositories.jobs``, which imports that
module.
"""

import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.core.db import create_engine
from app.core.logging import get_logger
from app.core.queue import create_celery_app
from app.models.deployment import DeploymentStatus
from app.repositories.deployments import DeploymentRepository
from app.repositories.environments import EnvironmentRepository
from app.repositories.jobs import CeleryJobDispatcher
from app.repositories.projects import ProjectRepository
from app.services.deployment_terminal import DeploymentTerminalService
from app.services.deployments import DeploymentService
from app.services.environment_vars import EnvironmentVariableRepository, EnvironmentVariableService
from app.services.environments import EnvironmentService
from app.services.jobs import DeploymentEnqueueHooks, JobDispatcher, JobService
from app.services.projects import ProjectService

logger = get_logger("tasks.waiting_release")


def _dispatcher(settings: Settings) -> JobDispatcher | None:
    """None without a broker, so a waiting row is not promoted with nowhere to run."""
    if not settings.redis_url:
        return None
    return CeleryJobDispatcher(create_celery_app(settings))


def _same_session_hooks(
    session: AsyncSession, terminal: DeploymentTerminalService
) -> DeploymentEnqueueHooks:
    """Hooks that write on the release session and never raise.

    The API runs these on a second session so a request rollback cannot undo
    them. Here ``JobService`` commits before each hook and nothing rolls back
    afterwards, so one session is enough.
    """

    async def mark_failed(deployment_id: uuid.UUID) -> None:
        try:
            await terminal.mark(deployment_id, DeploymentStatus.failed)
            await session.commit()
        except Exception as status_exc:
            await session.rollback()
            logger.warning(
                "event=deployment_failed_status_skipped deployment=%s error_type=%s",
                deployment_id,
                type(status_exc).__name__,
                exc_info=status_exc,
            )

    async def set_provision_job_id(deployment_id: uuid.UUID, job_id: uuid.UUID) -> None:
        try:
            await terminal.set_provision_job_id(deployment_id, job_id)
            await session.commit()
        except Exception as persist_exc:
            await session.rollback()
            logger.warning(
                "event=deployment_provision_job_id_skipped deployment=%s job_id=%s error_type=%s",
                deployment_id,
                job_id,
                type(persist_exc).__name__,
                exc_info=persist_exc,
            )

    return DeploymentEnqueueHooks(mark_failed=mark_failed, set_provision_job_id=set_provision_job_id)


def _job_service(session: AsyncSession, settings: Settings, dispatcher: JobDispatcher) -> JobService:
    deployments = DeploymentRepository(session)
    terminal = DeploymentTerminalService(deployments)
    environments = EnvironmentService(
        EnvironmentRepository(session),
        ProjectService(ProjectRepository(session), deployments),
        deployments,
    )
    # One service: the loader unseals the replay, and the deployment service
    # writes that map when the waiting rollback is dispatched (#273).
    env_vars = EnvironmentVariableService(
        EnvironmentVariableRepository(session),
        environments,
        signing_key=settings.auth_signing_key.get_secret_value(),
    )
    return JobService(
        dispatcher,
        environments,
        DeploymentService(deployments, environments, terminal, env_vars),
        redis_url=settings.redis_url,
        deployment_hooks=_same_session_hooks(session, terminal),
        env_var_loader=env_vars,
    )


async def release_waiting_once(
    settings: Settings, dispatcher: JobDispatcher, finished_id: uuid.UUID
) -> None:
    """Release what waited on ``finished_id`` using an engine owned by this loop."""
    if not settings.database_url:
        return
    engine = create_engine(settings.database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with sessions() as session:
            try:
                finished = await DeploymentRepository(session).get(finished_id)
                if finished is not None:
                    await _job_service(session, settings, dispatcher).release_waiting(finished)
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    finally:
        await engine.dispose()


def try_release_waiting_sync(finished_id: uuid.UUID) -> None:
    """Worker-side bridge. Logs and continues on failure so the task result stands."""
    try:
        settings = get_settings()
        if not settings.database_url:
            return
        dispatcher = _dispatcher(settings)
        if dispatcher is None:
            return
        asyncio.run(release_waiting_once(settings, dispatcher, finished_id))
    except Exception as release_exc:
        logger.warning(
            "event=deployment_wait_release_skipped deployment=%s error_type=%s",
            finished_id,
            type(release_exc).__name__,
            exc_info=release_exc,
        )
