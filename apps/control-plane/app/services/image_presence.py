"""Ask whether a deployment's retained image is still on the Docker host (#272).

The API has no Docker socket. The worker does, so the question goes to it as a
short task, the same way runtime logs do. When nobody can answer, the caller
is told so (``None``) and decides; the provision job checks again before it
uses the image.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Protocol

from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.core.logging import get_logger
from app.services.docker_env_discovery import ensure_default_docker_config
from app.services.docker_policy import (
    CommandRunner,
    SubprocessCommandRunner,
    deployment_image_tag_for,
    validate_binary,
)
from app.services.provisioning import PermanentJobError, TransientJobError

logger = get_logger("services.image_presence")

IMAGE_PRESENT_TASK = "aerodeck.environments.image_present"
_INSPECT_TIMEOUT_SECONDS = 10
_WORKER_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class RetainedImage:
    """The image one deployment built, named by the environment that built it."""

    project_id: uuid.UUID
    project_slug: str
    environment_id: uuid.UUID
    environment_slug: str
    deployment_id: uuid.UUID

    def as_task_kwargs(self) -> dict[str, str]:
        return {
            "project_id": str(self.project_id),
            "project_slug": self.project_slug,
            "environment_id": str(self.environment_id),
            "environment_slug": self.environment_slug,
            "deployment_id": str(self.deployment_id),
        }


class ImageLookup(Protocol):
    async def is_present(self, image: RetainedImage) -> bool | None:
        """True or False when known. None when the Docker host could not be asked."""
        ...


def image_present_sync(
    *,
    project_id: str,
    project_slug: str,
    environment_id: str,
    environment_slug: str,
    deployment_id: str,
    docker_bin: str = "docker",
    docker_host: str | None = None,
    runner: CommandRunner | None = None,
) -> bool | None:
    """Run ``docker image inspect`` for the deployment's tag. Runs on the worker."""
    tag = deployment_image_tag_for(
        project_slug, environment_slug, project_id, environment_id, deployment_id
    )
    safe_bin = validate_binary(docker_bin, "docker_bin")
    cmd_runner = runner or SubprocessCommandRunner()
    docker_env = ensure_default_docker_config(os.environ, docker_host)
    try:
        inspected = cmd_runner.run(
            [safe_bin, "image", "inspect", tag], env=docker_env, timeout=_INSPECT_TIMEOUT_SECONDS
        )
    except (PermanentJobError, TransientJobError) as exc:
        logger.warning("event=image_inspect_unavailable error_type=%s", type(exc).__name__)
        return None
    if inspected.returncode == 0:
        return True
    if "no such image" in inspected.stderr.lower():
        return False
    # Docker is down or refused the call. That is not the same as a missing image.
    logger.warning("event=image_inspect_failed returncode=%d", inspected.returncode)
    return None


class WorkerImageLookup:
    """Asks the Celery worker, which holds the Docker socket."""

    def __init__(self, settings: Settings, *, timeout: float = _WORKER_TIMEOUT_SECONDS) -> None:
        self._settings = settings
        self._timeout = timeout

    async def is_present(self, image: RetainedImage) -> bool | None:
        if self._settings.provisioner_backend != "docker" or not self._settings.redis_url:
            return None
        return await run_in_threadpool(self._ask_worker, image)

    def _ask_worker(self, image: RetainedImage) -> bool | None:
        # Lazy import: queue → tasks.environments → this module.
        from app.core.queue import create_celery_app

        try:
            answer = (
                create_celery_app(self._settings)
                .send_task(
                    IMAGE_PRESENT_TASK,
                    kwargs=image.as_task_kwargs(),
                    queue=self._settings.job_queue,
                )
                .get(timeout=self._timeout)
            )
        except Exception as exc:
            # A busy or stopped worker must not block a promotion: the job checks again.
            logger.warning(
                "event=image_presence_unknown deployment=%s error_type=%s",
                image.deployment_id,
                type(exc).__name__,
            )
            return None
        return answer if isinstance(answer, bool) else None
