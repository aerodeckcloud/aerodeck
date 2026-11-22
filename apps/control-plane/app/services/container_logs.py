"""Fetch live runtime container logs for a Docker-provisioned environment (#247)."""

from __future__ import annotations

import os
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.core.config import Settings
from app.core.logging import get_logger
from app.services.docker_env_discovery import ensure_default_docker_config
from app.services.docker_policy import (
    CommandRunner,
    SubprocessCommandRunner,
    container_name_for,
    sanitize_log_line,
    validate_binary,
)
from app.services.provisioning import PermanentJobError, TransientJobError

logger = get_logger("services.container_logs")

CONTAINER_LOGS_TASK = "aerodeck.environments.container_logs"
_DEFAULT_TAIL = 200
_MAX_TAIL = 500

RuntimeLogsStatus = Literal["running", "exited", "not_found", "unavailable"]


class ContainerLogsRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    container: str
    status: RuntimeLogsStatus
    lines: list[str]
    error: str | None = None


def clamp_tail(tail: int) -> int:
    return max(1, min(int(tail), _MAX_TAIL))


def fetch_container_logs_sync(
    *,
    project_id: str,
    environment_id: str,
    docker_bin: str = "docker",
    docker_host: str | None = None,
    tail: int = _DEFAULT_TAIL,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Read recent ``docker logs`` for an environment's active container."""
    cname = container_name_for(project_id, environment_id)
    safe_bin = validate_binary(docker_bin, "docker_bin")
    safe_tail = clamp_tail(tail)
    cmd_runner = runner or SubprocessCommandRunner()
    docker_env = ensure_default_docker_config(os.environ, docker_host)

    try:
        inspect_res = cmd_runner.run(
            [safe_bin, "inspect", "-f", "{{.State.Status}}", cname],
            env=docker_env,
            timeout=5,
        )
    except (PermanentJobError, TransientJobError) as exc:
        logger.warning("container inspect unavailable for %s: %s", cname, type(exc).__name__)
        return {
            "container": cname,
            "status": "unavailable",
            "lines": [],
            "error": "Docker runtime is unavailable on the worker.",
        }

    if inspect_res.returncode != 0:
        stderr_lower = inspect_res.stderr.lower()
        if "no such" in stderr_lower or "not found" in stderr_lower:
            return {"container": cname, "status": "not_found", "lines": [], "error": None}
        return {
            "container": cname,
            "status": "unavailable",
            "lines": [],
            "error": "Could not inspect container status.",
        }

    raw_state = inspect_res.stdout.strip().lower()
    state: RuntimeLogsStatus = "running" if raw_state == "running" else "exited"

    try:
        logs_res = cmd_runner.run(
            [safe_bin, "logs", "--tail", str(safe_tail), "--timestamps", cname],
            env=docker_env,
            timeout=10,
        )
    except (PermanentJobError, TransientJobError) as exc:
        logger.warning("container logs unavailable for %s: %s", cname, type(exc).__name__)
        return {
            "container": cname,
            "status": state,
            "lines": [],
            "error": "Timed out reading container logs.",
        }

    if logs_res.returncode != 0:
        return {
            "container": cname,
            "status": state,
            "lines": [],
            "error": "Could not read container logs.",
        }

    combined = "\n".join(part for part in (logs_res.stdout, logs_res.stderr) if part)
    lines = [sanitize_log_line(line) for line in combined.splitlines() if line.strip()]
    return {"container": cname, "status": state, "lines": lines[-safe_tail:], "error": None}


def dispatch_container_logs(
    settings: Settings,
    *,
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    tail: int = _DEFAULT_TAIL,
    timeout: float = 10.0,
) -> ContainerLogsRead:
    """Ask the Celery worker (which holds the Docker socket) for container logs."""
    cname = container_name_for(str(project_id), str(environment_id))
    if settings.provisioner_backend != "docker":
        return ContainerLogsRead(
            container=cname,
            status="unavailable",
            lines=[],
            error="Runtime container logs require the Docker provisioner backend.",
        )
    if not settings.redis_url:
        return ContainerLogsRead(
            container=cname,
            status="unavailable",
            lines=[],
            error="Worker queue is not configured.",
        )

    from app.core.queue import create_celery_app

    celery_app = create_celery_app(settings)
    try:
        async_result = celery_app.send_task(
            CONTAINER_LOGS_TASK,
            kwargs={
                "project_id": str(project_id),
                "environment_id": str(environment_id),
                "tail": clamp_tail(tail),
            },
            queue=settings.job_queue,
        )
        payload = async_result.get(timeout=timeout)
    except Exception as exc:
        logger.warning("runtime logs task failed for %s: %s", cname, type(exc).__name__)
        return ContainerLogsRead(
            container=cname,
            status="unavailable",
            lines=[],
            error="Worker did not return runtime logs in time.",
        )
    return ContainerLogsRead.model_validate(payload)
