"""Worker-delegated container lifecycle actions and live resource metrics (#254)."""

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
    validate_binary,
)
from app.services.provisioning import PermanentJobError, TransientJobError

logger = get_logger("services.container_ops")

CONTAINER_ACTION_TASK = "aerodeck.environments.container_action"
CONTAINER_METRICS_TASK = "aerodeck.environments.container_metrics"

ContainerAction = Literal["start", "stop", "restart"]
ContainerState = Literal["running", "exited", "not_found", "unavailable"]
_VALID_ACTIONS = {"start", "stop", "restart"}


class ContainerActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ContainerAction


class ContainerActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    container: str
    action: ContainerAction
    status: ContainerState
    ok: bool
    error: str | None = None


class ContainerMetricsRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    container: str
    status: ContainerState
    cpu_percent: str | None = None
    memory_usage: str | None = None
    memory_percent: str | None = None
    started_at: str | None = None
    restart_count: int = 0
    error: str | None = None


def run_container_action_sync(
    *,
    project_id: str,
    environment_id: str,
    action: str,
    docker_bin: str = "docker",
    docker_host: str | None = None,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Execute ``docker start|stop|restart`` for an environment's active container."""
    if action not in _VALID_ACTIONS:
        raise ValueError(f"Unsupported container action: {action!r}")
    cname = container_name_for(project_id, environment_id)
    safe_bin = validate_binary(docker_bin, "docker_bin")
    cmd_runner = runner or SubprocessCommandRunner()
    docker_env = ensure_default_docker_config(os.environ, docker_host)

    try:
        res = cmd_runner.run([safe_bin, action, cname], env=docker_env, timeout=30)
    except (PermanentJobError, TransientJobError) as exc:
        logger.warning("container %s failed for %s: %s", action, cname, type(exc).__name__)
        return {
            "container": cname,
            "action": action,
            "status": "unavailable",
            "ok": False,
            "error": f"Docker runtime timed out or unavailable during {action}.",
        }

    if res.returncode != 0:
        stderr_lower = res.stderr.lower()
        if "no such" in stderr_lower or "not found" in stderr_lower:
            return {
                "container": cname,
                "action": action,
                "status": "not_found",
                "ok": False,
                "error": "Container does not exist yet; deploy first.",
            }
        return {
            "container": cname,
            "action": action,
            "status": "unavailable",
            "ok": False,
            "error": f"Container {action} failed.",
        }

    inspect_res = cmd_runner.run(
        [safe_bin, "inspect", "-f", "{{.State.Status}}", cname], env=docker_env, timeout=5
    )
    raw_state = inspect_res.stdout.strip().lower() if inspect_res.returncode == 0 else ""
    state: ContainerState = "running" if raw_state == "running" else "exited"
    return {"container": cname, "action": action, "status": state, "ok": True, "error": None}


def fetch_container_metrics_sync(
    *,
    project_id: str,
    environment_id: str,
    docker_bin: str = "docker",
    docker_host: str | None = None,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Collect container state, uptime, restart count, and live CPU/RAM via Docker."""
    cname = container_name_for(project_id, environment_id)
    safe_bin = validate_binary(docker_bin, "docker_bin")
    cmd_runner = runner or SubprocessCommandRunner()
    docker_env = ensure_default_docker_config(os.environ, docker_host)

    try:
        inspect_res = cmd_runner.run(
            [safe_bin, "inspect", "-f", "{{.State.Status}}|{{.State.StartedAt}}|{{.RestartCount}}", cname],
            env=docker_env,
            timeout=5,
        )
    except (PermanentJobError, TransientJobError) as exc:
        logger.warning("container metrics inspect failed for %s: %s", cname, type(exc).__name__)
        return {
            "container": cname,
            "status": "unavailable",
            "error": "Docker runtime is unavailable on the worker.",
        }

    if inspect_res.returncode != 0:
        stderr_lower = inspect_res.stderr.lower()
        if "no such" in stderr_lower or "not found" in stderr_lower:
            return {"container": cname, "status": "not_found", "error": None}
        return {"container": cname, "status": "unavailable", "error": "Could not inspect container."}

    parts = inspect_res.stdout.strip().split("|")
    raw_state = parts[0].strip().lower() if len(parts) > 0 else ""
    started_at = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    restart_count = int(parts[2].strip()) if len(parts) > 2 and parts[2].strip().isdigit() else 0
    state: ContainerState = "running" if raw_state == "running" else "exited"

    cpu_pct: str | None = None
    mem_usage: str | None = None
    mem_pct: str | None = None
    if state == "running":
        try:
            stats_res = cmd_runner.run(
                [
                    safe_bin,
                    "stats",
                    "--no-stream",
                    "--format",
                    "{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}",
                    cname,
                ],
                env=docker_env,
                timeout=10,
            )
            if stats_res.returncode == 0:
                stat_parts = stats_res.stdout.strip().splitlines()[0].split("|") if stats_res.stdout.strip() else []
                if len(stat_parts) >= 3:
                    cpu_pct = stat_parts[0].strip() or None
                    mem_usage = stat_parts[1].strip() or None
                    mem_pct = stat_parts[2].strip() or None
        except (PermanentJobError, TransientJobError):
            logger.warning("docker stats timed out for %s", cname)

    return {
        "container": cname,
        "status": state,
        "cpu_percent": cpu_pct,
        "memory_usage": mem_usage,
        "memory_percent": mem_pct,
        "started_at": started_at,
        "restart_count": restart_count,
        "error": None,
    }


def dispatch_container_action(
    settings: Settings,
    *,
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    action: ContainerAction,
    timeout: float = 30.0,
) -> ContainerActionResult:
    cname = container_name_for(str(project_id), str(environment_id))
    if settings.provisioner_backend != "docker" or not settings.redis_url:
        return ContainerActionResult(
            container=cname,
            action=action,
            status="unavailable",
            ok=False,
            error="Container lifecycle controls require the Docker provisioner backend.",
        )
    from app.core.queue import create_celery_app

    celery_app = create_celery_app(settings)
    try:
        payload = celery_app.send_task(
            CONTAINER_ACTION_TASK,
            kwargs={
                "project_id": str(project_id),
                "environment_id": str(environment_id),
                "action": action,
            },
            queue=settings.job_queue,
        ).get(timeout=timeout)
    except Exception as exc:
        logger.warning("container action task failed for %s: %s", cname, type(exc).__name__)
        return ContainerActionResult(
            container=cname,
            action=action,
            status="unavailable",
            ok=False,
            error="Worker did not complete the container action in time.",
        )
    return ContainerActionResult.model_validate(payload)


def dispatch_container_metrics(
    settings: Settings,
    *,
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    timeout: float = 10.0,
) -> ContainerMetricsRead:
    cname = container_name_for(str(project_id), str(environment_id))
    if settings.provisioner_backend != "docker" or not settings.redis_url:
        return ContainerMetricsRead(
            container=cname,
            status="unavailable",
            error="Live container metrics require the Docker provisioner backend.",
        )
    from app.core.queue import create_celery_app

    celery_app = create_celery_app(settings)
    try:
        payload = celery_app.send_task(
            CONTAINER_METRICS_TASK,
            kwargs={"project_id": str(project_id), "environment_id": str(environment_id)},
            queue=settings.job_queue,
        ).get(timeout=timeout)
    except Exception as exc:
        logger.warning("container metrics task failed for %s: %s", cname, type(exc).__name__)
        return ContainerMetricsRead(
            container=cname,
            status="unavailable",
            error="Worker did not return container metrics in time.",
        )
    return ContainerMetricsRead.model_validate(payload)
