"""Isolated commits for deployment fields outside the request session.

Enqueue failure must mark the row even though the request then raises and rolls
back. Successful provision enqueue persists ``provision_job_id`` on its own
session so a flush failure cannot poison the request session and turn 202 into
500 (#139). Writers go through ``DeploymentTerminalService``.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.db import SessionLocal, bind_engine, create_engine, unbind_engine
from app.core.logging import get_logger
from app.models.deployment import Deployment, DeploymentStatus
from app.repositories.deployments import DeploymentRepository
from app.services.deployment_terminal import DeploymentTerminalService

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
