"""Isolated commits for deployment fields outside the request session.

Enqueue failure must mark the row even though the request then raises and rolls
back. Successful provision enqueue persists ``provision_job_id`` on its own
session so a flush failure cannot poison the request session and turn 202 into
500 (#139). Writers go through ``DeploymentTerminalService``.
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.db import SessionLocal, bind_engine, create_engine, unbind_engine
from app.core.logging import get_logger
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import Environment, Project
from app.repositories.deployments import DeploymentRepository
from app.services.deployment_terminal import DeploymentTerminalService
from app.services.environment_vars import EnvironmentVariableRepository, decrypt_env_value
from app.services.provision_replay import parse_provision_replay

logger = get_logger("tasks.deployment_status")

TerminalWrite = Callable[[DeploymentTerminalService], Awaitable[Deployment | None]]


async def _isolated_terminal_write(deployment_id: uuid.UUID, write: TerminalWrite) -> None:
    """Run a terminal-service mutation on a short-lived session (commit or rollback)."""
    async with SessionLocal() as session:
        try:
            stored = await write(DeploymentTerminalService(DeploymentRepository(session)))
            if stored is None:
                raise LookupError(f"Deployment {deployment_id} not found")
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def mark_deployment_failed_isolated(deployment_id: uuid.UUID) -> None:
    """Commit failed on its own session so a later request rollback cannot undo it."""

    async def write(terminal: DeploymentTerminalService) -> Deployment | None:
        return await terminal.mark(deployment_id, DeploymentStatus.failed)

    await _isolated_terminal_write(deployment_id, write)


async def try_mark_deployment_failed_isolated(deployment_id: uuid.UUID) -> None:
    """Best-effort isolated mark; log and continue on failure."""
    try:
        await mark_deployment_failed_isolated(deployment_id)
    except Exception as status_exc:
        logger.warning(
            "event=deployment_failed_status_skipped deployment=%s error_type=%s",
            deployment_id,
            type(status_exc).__name__,
            exc_info=status_exc,
        )


async def set_provision_job_id_isolated(deployment_id: uuid.UUID, job_id: uuid.UUID) -> None:
    """Commit provision_job_id on its own session so request-session errors stay isolated."""

    async def write(terminal: DeploymentTerminalService) -> Deployment | None:
        return await terminal.set_provision_job_id(deployment_id, job_id)

    await _isolated_terminal_write(deployment_id, write)


async def try_set_provision_job_id_isolated(deployment_id: uuid.UUID, job_id: uuid.UUID) -> None:
    """Best-effort isolated persist; log and continue on failure."""
    try:
        await set_provision_job_id_isolated(deployment_id, job_id)
    except Exception as persist_exc:
        logger.warning(
            "event=deployment_provision_job_id_skipped deployment=%s job_id=%s error_type=%s",
            deployment_id,
            job_id,
            type(persist_exc).__name__,
            exc_info=persist_exc,
        )


def mark_deployment_failed_sync(deployment_id: uuid.UUID) -> None:
    """Worker-side bridge: sync Celery tasks mark a linked deployment failed."""
    settings = get_settings()
    if not settings.database_url:
        logger.info("deployment failed status skipped: AERODECK_DATABASE_URL is unset")
        return

    async def _run() -> None:
        owns_engine = False
        engine = None
        if SessionLocal.kw.get("bind") is None:
            engine = create_engine(settings.database_url, poolclass=NullPool)
            bind_engine(engine)
            owns_engine = True
        try:
            await mark_deployment_failed_isolated(deployment_id)
        finally:
            if owns_engine and engine is not None:
                unbind_engine()
                await engine.dispose()

    asyncio.run(_run())


async def _record_release_on_private_engine(
    database_url: str,
    deployment_id: uuid.UUID,
    *,
    git_sha: str | None,
    git_ref: str | None,
    finished: bool,
) -> None:
    """Write release fields on an engine owned by this loop.

    Celery calls this through ``asyncio.run`` on a worker thread. Reusing
    ``SessionLocal`` from that nested loop poisons asyncpg connections that
    still belong to the API loop.
    """
    engine = create_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with sessions() as session:
            try:
                stored = await DeploymentTerminalService(DeploymentRepository(session)).record_release(
                    deployment_id,
                    git_sha=git_sha,
                    git_ref=git_ref,
                    finished=finished,
                )
                if stored is None:
                    logger.info(
                        "event=deployment_release_record_skipped deployment=%s error_type=LookupError",
                        deployment_id,
                    )
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    finally:
        await engine.dispose()


def try_record_deployment_release_sync(
    deployment_id: uuid.UUID,
    *,
    git_sha: str | None = None,
    git_ref: str | None = None,
    finished: bool = False,
) -> None:
    """Best-effort worker write of checkout identity and finish time. Never raises."""
    settings = get_settings()
    if not settings.database_url:
        logger.info("deployment release record skipped: AERODECK_DATABASE_URL is unset")
        return
    try:
        asyncio.run(
            _record_release_on_private_engine(
                settings.database_url,
                deployment_id,
                git_sha=git_sha,
                git_ref=git_ref,
                finished=finished,
            )
        )
    except Exception as record_exc:
        logger.warning(
            "event=deployment_release_record_skipped deployment=%s error_type=%s",
            deployment_id,
            type(record_exc).__name__,
        )


async def _begin_swap_on_private_engine(database_url: str, deployment_id: uuid.UUID) -> bool:
    engine = create_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with sessions() as session:
            try:
                allowed = await DeploymentTerminalService(DeploymentRepository(session)).begin_swap(
                    deployment_id
                )
                await session.commit()
                return allowed
            except Exception:
                await session.rollback()
                raise
    finally:
        await engine.dispose()


def try_begin_deployment_swap_sync(deployment_id: uuid.UUID) -> bool:
    """Stamp swap start. True when the database is unset so tests without a URL still deploy."""
    settings = get_settings()
    if not settings.database_url:
        return True
    try:
        return asyncio.run(_begin_swap_on_private_engine(settings.database_url, deployment_id))
    except Exception as swap_exc:
        logger.warning(
            "event=deployment_swap_stamp_skipped deployment=%s error_type=%s",
            deployment_id,
            type(swap_exc).__name__,
        )
        return True


async def _deployment_is_cancelled(database_url: str, deployment_id: uuid.UUID) -> bool:
    engine = create_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with sessions() as session:
            stored = await DeploymentRepository(session).get(deployment_id)
            return stored is not None and stored.status is DeploymentStatus.cancelled
    finally:
        await engine.dispose()


def deployment_is_cancelled_sync(deployment_id: uuid.UUID) -> bool:
    """True only when the row is cancelled. False when the database URL is unset."""
    settings = get_settings()
    if not settings.database_url:
        return False
    try:
        return asyncio.run(_deployment_is_cancelled(settings.database_url, deployment_id))
    except Exception as status_exc:
        logger.warning(
            "event=deployment_cancel_check_skipped deployment=%s error_type=%s",
            deployment_id,
            type(status_exc).__name__,
        )
        return False


def try_mark_deployment_failed_sync(deployment_id: uuid.UUID) -> None:
    """Best-effort sync mark; log and continue on failure."""
    try:
        mark_deployment_failed_sync(deployment_id)
    except Exception as status_exc:
        logger.warning(
            "event=deployment_failed_status_skipped deployment=%s error_type=%s",
            deployment_id,
            type(status_exc).__name__,
            exc_info=status_exc,
        )
        return
    try_release_waiting_sync(deployment_id)


def try_release_waiting_sync(finished_id: uuid.UUID) -> None:
    """Start the newest deployment waiting on a failed row. Never raises.

    Uses a private engine, same as the other worker writers. No-ops when the
    database or Redis URL is unset so a test without a broker does not promote
    a row it cannot start.
    """
    settings = get_settings()
    if not settings.database_url or not settings.redis_url:
        return
    try:
        spec = asyncio.run(
            _claim_waiting_on_private_engine(
                settings.database_url, settings.auth_signing_key, finished_id
            )
        )
    except Exception as release_exc:
        logger.warning(
            "event=deployment_wait_release_skipped deployment=%s error_type=%s",
            finished_id,
            type(release_exc).__name__,
        )
        return
    if spec is None:
        return
    try:
        from app.core.queue import create_celery_app
        from app.repositories.jobs import CeleryJobDispatcher
        from app.services.provisioning import JobKind

        job_id = CeleryJobDispatcher(create_celery_app(settings)).dispatch(
            JobKind.provision,
            project_id=spec.pop("project_id"),
            environment_id=spec.pop("environment_id"),
            **spec,
        )
        asyncio.run(
            _remember_job_on_private_engine(
                settings.database_url, uuid.UUID(spec["deployment_id"]), uuid.UUID(job_id)
            )
        )
    except Exception as dispatch_exc:
        logger.warning(
            "event=deployment_wait_release_skipped deployment=%s error_type=%s",
            finished_id,
            type(dispatch_exc).__name__,
        )


async def _claim_waiting_on_private_engine(
    database_url: str, signing_key: str, finished_id: uuid.UUID
) -> dict[str, str] | None:
    from cryptography.fernet import InvalidToken

    engine = create_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with sessions() as session:
            try:
                finished = await DeploymentRepository(session).get(finished_id)
                if (
                    finished is None
                    or finished.environment_id is None
                    or finished.project_id is None
                    or finished.status is not DeploymentStatus.failed
                ):
                    return None
                claimed = await DeploymentRepository(session).claim_newest_waiter(
                    finished.environment_id, finished.id, finished.finished_at or finished.started_at
                )
                if claimed is None or claimed.environment_id is None or claimed.project_id is None:
                    await session.commit()
                    return None
                environment = await session.get(Environment, claimed.environment_id)
                project = await session.get(Project, claimed.project_id)
                if environment is None or project is None:
                    await session.commit()
                    return None
                decrypted: dict[str, str] = {}
                for row in await EnvironmentVariableRepository(session).list_for_environment(
                    claimed.project_id, claimed.environment_id
                ):
                    try:
                        decrypted[row.key] = decrypt_env_value(row.encrypted_value, signing_key)
                    except InvalidToken:
                        logger.warning(
                            "skipped unreadable encrypted env var %s for environment %s",
                            row.key,
                            claimed.environment_id,
                        )
                git_ref, rollback_from, addons = parse_provision_replay(claimed.provision_replay_json)
                kind = environment.kind.value if hasattr(environment.kind, "value") else str(environment.kind)
                spec: dict[str, str] = {
                    "project_id": str(claimed.project_id),
                    "environment_id": str(claimed.environment_id),
                    "project_slug": project.slug,
                    "environment_slug": environment.slug,
                    "environment_kind": kind,
                    "region": environment.region,
                    "deployment_id": str(claimed.id),
                    "image_deployment_id": str(claimed.id),
                    "repo_url": claimed.repo_url,
                }
                if git_ref:
                    spec["git_ref"] = git_ref
                if rollback_from is not None:
                    spec["rollback_from_deployment_id"] = str(rollback_from)
                if addons:
                    spec["addons_json"] = json.dumps(addons)
                if decrypted:
                    spec["env_vars_json"] = json.dumps(decrypted)
                await session.commit()
                return spec
            except Exception:
                await session.rollback()
                raise
    finally:
        await engine.dispose()


async def _remember_job_on_private_engine(
    database_url: str, deployment_id: uuid.UUID, job_id: uuid.UUID
) -> None:
    engine = create_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with sessions() as session:
            try:
                await DeploymentTerminalService(DeploymentRepository(session)).set_provision_job_id(
                    deployment_id, job_id
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    finally:
        await engine.dispose()
