"""Drop succeeded Docker tags that fall outside an environment's keep count (#274).

The API has no Docker socket. After a deployment is marked succeeded it asks
the worker to remove the older tags. A failure is logged and the new
deployment stays succeeded. The current image is never one of the tags removed.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.db import create_engine
from app.core.logging import get_logger
from app.models.deployment import Deployment, DeploymentStatus
from app.models.project import ROLLBACK_IMAGES_TO_KEEP_MAX, ROLLBACK_IMAGES_TO_KEEP_MIN
from app.repositories.deployments import DeploymentRepository
from app.repositories.environments import EnvironmentRepository
from app.services.docker_env_discovery import ensure_default_docker_config
from app.services.docker_policy import (
    CommandRunner,
    SubprocessCommandRunner,
    deployment_image_tag_for,
    validate_binary,
)
from app.services.provisioning import PermanentJobError, TransientJobError

logger = get_logger("services.image_retention")

PRUNE_IMAGES_TASK = "aerodeck.environments.prune_images"
_RM_TIMEOUT_SECONDS = 30


@dataclass(frozen=True, slots=True)
class RetentionCandidate:
    """One succeeded deployment, enough to decide whether its tag stays."""

    deployment_id: uuid.UUID
    finished_at: datetime | None
    started_at: datetime
    image_pruned_at: datetime | None


def image_ids_to_drop(
    rows: list[RetentionCandidate],
    *,
    keep: int,
    protect_deployment_id: uuid.UUID,
) -> list[uuid.UUID]:
    """Ids whose tags are older than ``keep``. ``protect_deployment_id`` stays.

    Already-pruned rows still occupy a slot in the newest ``keep`` so a later
    success does not delete a newer image to fill a hole. They are not returned.
    """
    bounded = min(max(keep, ROLLBACK_IMAGES_TO_KEEP_MIN), ROLLBACK_IMAGES_TO_KEEP_MAX)
    ordered = sorted(rows, key=_recency, reverse=True)
    kept = {row.deployment_id for row in ordered[:bounded]}
    kept.add(protect_deployment_id)
    return [
        row.deployment_id
        for row in ordered
        if row.deployment_id not in kept and row.image_pruned_at is None
    ]


def _recency(row: RetentionCandidate) -> tuple[datetime, str]:
    when = row.finished_at or row.started_at
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    else:
        when = when.astimezone(UTC)
    return (when, row.deployment_id.hex)


def remove_image_tags(
    tags: dict[uuid.UUID, str],
    *,
    runner: CommandRunner,
    docker_bin: str,
    docker_host: str | None,
) -> list[uuid.UUID]:
    """Remove each tag. Return the ids that are gone. A refusal is logged and skipped."""
    if not tags:
        return []
    try:
        safe_bin = validate_binary(docker_bin, "docker_bin")
    except PermanentJobError as exc:
        logger.warning("event=rollback_image_prune_failed error_type=%s", type(exc).__name__)
        return []
    docker_env = ensure_default_docker_config(dict(os.environ), docker_host)
    removed: list[uuid.UUID] = []
    for deployment_id, tag in tags.items():
        if _tag_is_gone(runner, safe_bin, docker_env, deployment_id, tag):
            removed.append(deployment_id)
    return removed


def _tag_is_gone(
    runner: CommandRunner,
    docker_bin: str,
    docker_env: dict[str, str],
    deployment_id: uuid.UUID,
    tag: str,
) -> bool:
    try:
        result = runner.run(
            [docker_bin, "image", "rm", tag], env=docker_env, timeout=_RM_TIMEOUT_SECONDS
        )
    except (PermanentJobError, TransientJobError) as exc:
        logger.warning(
            "event=rollback_image_prune_failed deployment=%s error_type=%s",
            deployment_id,
            type(exc).__name__,
        )
        return False
    if result.returncode == 0 or "no such image" in result.stderr.lower():
        return True
    logger.warning(
        "event=rollback_image_prune_failed deployment=%s returncode=%s",
        deployment_id,
        result.returncode,
    )
    return False


async def load_prune_plan(
    environments: EnvironmentRepository,
    deployments: DeploymentRepository,
    *,
    environment_id: uuid.UUID,
    protect_deployment_id: uuid.UUID,
) -> dict[uuid.UUID, str]:
    """Tags to remove. Raises ``TransientJobError`` until the new row is succeeded."""
    protect = await deployments.get(protect_deployment_id)
    if (
        protect is None
        or protect.environment_id != environment_id
        or protect.status is not DeploymentStatus.succeeded
    ):
        raise TransientJobError("rollback image prune is waiting for the succeeded deployment")
    environment = await environments.get_with_project(environment_id)
    if environment is None:
        logger.info("event=rollback_image_prune_skipped environment=%s reason=missing", environment_id)
        return {}
    rows = await deployments.list_succeeded(environment_id)
    drop_ids = image_ids_to_drop(
        [_candidate(row) for row in rows],
        keep=environment.rollback_images_to_keep,
        protect_deployment_id=protect_deployment_id,
    )
    by_id = {row.id: row for row in rows}
    project = environment.project
    return {
        deployment_id: deployment_image_tag_for(
            project.slug,
            environment.slug,
            str(project.id),
            str(environment.id),
            str(deployment_id),
        )
        for deployment_id in drop_ids
        if (row := by_id.get(deployment_id)) is not None and row.image_pruned_at is None
    }


def _candidate(row: Deployment) -> RetentionCandidate:
    return RetentionCandidate(
        deployment_id=row.id,
        finished_at=row.finished_at,
        started_at=row.started_at,
        image_pruned_at=row.image_pruned_at,
    )


async def prune_succeeded_images(
    environments: EnvironmentRepository,
    deployments: DeploymentRepository,
    *,
    environment_id: uuid.UUID,
    protect_deployment_id: uuid.UUID,
    runner: CommandRunner,
    docker_bin: str,
    docker_host: str | None,
) -> list[uuid.UUID]:
    """Remove tags past the keep count and stamp the rows. Returns the stamped ids."""
    tags = await load_prune_plan(
        environments,
        deployments,
        environment_id=environment_id,
        protect_deployment_id=protect_deployment_id,
    )
    removed = remove_image_tags(tags, runner=runner, docker_bin=docker_bin, docker_host=docker_host)
    if removed:
        await deployments.mark_images_pruned(removed, datetime.now(UTC))
        logger.info(
            "event=rollback_images_pruned environment=%s removed=%d",
            environment_id,
            len(removed),
        )
    return removed


def prune_rollback_images_sync(
    *,
    environment_id: str,
    deployment_id: str,
    database_url: str | None,
    docker_bin: str,
    docker_host: str | None,
    runner: CommandRunner | None = None,
) -> None:
    """Worker entry. Logs a Docker or database failure and does not raise it.

    ``TransientJobError`` still propagates so the task can retry until the
    succeeded row is visible. That retry does not change the deployment status.
    """
    if not database_url:
        logger.info("event=rollback_image_prune_skipped reason=no_database")
        return
    try:
        asyncio.run(
            _prune_on_private_engine(
                database_url,
                environment_id=uuid.UUID(environment_id),
                protect_deployment_id=uuid.UUID(deployment_id),
                runner=runner or SubprocessCommandRunner(),
                docker_bin=docker_bin,
                docker_host=docker_host,
            )
        )
    except TransientJobError:
        logger.warning(
            "event=rollback_image_prune_waiting environment=%s deployment=%s",
            environment_id,
            deployment_id,
        )
        raise
    except Exception as exc:
        logger.warning(
            "event=rollback_image_prune_failed environment=%s deployment=%s error_type=%s",
            environment_id,
            deployment_id,
            type(exc).__name__,
        )


async def _prune_on_private_engine(
    database_url: str,
    *,
    environment_id: uuid.UUID,
    protect_deployment_id: uuid.UUID,
    runner: CommandRunner,
    docker_bin: str,
    docker_host: str | None,
) -> None:
    """Load, remove tags, then stamp. Docker work is not inside the write transaction."""
    engine = create_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with sessions() as session:
            tags = await load_prune_plan(
                EnvironmentRepository(session),
                DeploymentRepository(session),
                environment_id=environment_id,
                protect_deployment_id=protect_deployment_id,
            )
        removed = remove_image_tags(
            tags, runner=runner, docker_bin=docker_bin, docker_host=docker_host
        )
        if not removed:
            return
        async with sessions() as session:
            try:
                await DeploymentRepository(session).mark_images_pruned(removed, datetime.now(UTC))
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        logger.info(
            "event=rollback_images_pruned environment=%s removed=%d",
            environment_id,
            len(removed),
        )
    finally:
        await engine.dispose()


class ImageRetentionService:
    """Ask the worker to prune. Queue trouble is logged and does not propagate."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def schedule(self, environment_id: uuid.UUID, deployment_id: uuid.UUID) -> None:
        if self._settings.provisioner_backend != "docker":
            return
        if not self._settings.redis_url:
            logger.warning(
                "event=rollback_image_prune_skipped environment=%s reason=no_queue",
                environment_id,
            )
            return
        try:
            # Lazy import: queue → tasks.environments → this module.
            from app.core.queue import create_celery_app

            create_celery_app(self._settings).send_task(
                PRUNE_IMAGES_TASK,
                kwargs={
                    "environment_id": str(environment_id),
                    "deployment_id": str(deployment_id),
                },
                queue=self._settings.job_queue,
            )
        except Exception as exc:
            logger.warning(
                "event=rollback_image_prune_skipped environment=%s deployment=%s error_type=%s",
                environment_id,
                deployment_id,
                type(exc).__name__,
            )
