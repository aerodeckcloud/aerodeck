"""Provisioning and reconciliation jobs for environments.

Tasks are thin: resolve the provisioner, run one operation, return a JSON-safe
outcome. ``TransientJobError`` (and plain connection trouble) is retried with
exponential backoff; anything else fails the job immediately.

When ``deployment_id`` is present in task meta (provision only), progress lines
are published for the deployment log SSE route (#121). ``Done`` is published only
on success so the console does not mark a failed deployment as succeeded.
"""

import inspect
import uuid
from typing import Any

from celery import Task, shared_task

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import record_iac_apply_failure
from app.services.container_logs import CONTAINER_LOGS_TASK, fetch_container_logs_sync
from app.services.container_ops import (
    CONTAINER_ACTION_TASK,
    CONTAINER_METRICS_TASK,
    CONTAINER_REMOVE_TASK,
    fetch_container_metrics_sync,
    remove_container_sync,
    run_container_action_sync,
)
from app.services.image_presence import IMAGE_PRESENT_TASK, image_present_sync
from app.services.notifications import notify_deployment_event_sync
from app.services.docker_policy import validate_git_ref
from app.services.provisioning import (
    DeploymentCancelled,
    JobKind,
    NoopProvisioner,
    PermanentJobError,
    Provisioner,
    TransientJobError,
)
from app.tasks.deployment_status import (
    deployment_is_cancelled_sync,
    try_begin_deployment_swap_sync,
    try_mark_deployment_failed_sync,
    try_record_deployment_release_sync,
)

logger = get_logger("tasks.environments")

PROVISION_TASK = "aerodeck.environments.provision"
RECONCILE_TASK = "aerodeck.environments.reconcile"
TASK_NAMES = (PROVISION_TASK, RECONCILE_TASK)

_RETRY_ON = (TransientJobError, ConnectionError, TimeoutError)

_provisioner: Provisioner = NoopProvisioner()


def use_provisioner(provisioner: Provisioner) -> None:
    """Worker bootstrap hook. ``worker.py`` installs ``TerraformProvisioner`` when enabled."""
    global _provisioner
    _provisioner = provisioner


def _deployment_id_from_meta(
    meta: dict[str, str],
    *,
    project_id: str,
    environment_id: str,
) -> uuid.UUID | None:
    raw = meta.pop("deployment_id", None)
    if raw is None:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        logger.warning(
            "event=deployment_id_invalid error_type=ValueError project=%s environment=%s",
            project_id,
            environment_id,
        )
        return None


def _release_waiting(deployment_id: uuid.UUID) -> None:
    """Start the deployment that waited on this one (#271). Never raises."""
    # Lazy import: waiting_release → services.jobs → repositories.jobs → this module.
    from app.tasks.waiting_release import try_release_waiting_sync

    try_release_waiting_sync(deployment_id)


def _will_retry(task: Task, exc: BaseException) -> bool:
    if not isinstance(exc, _RETRY_ON):
        return False
    return task.request.retries < task.max_retries


def _run(
    task: Task,
    kind: JobKind,
    project_id: str,
    environment_id: str,
    **meta: str,
) -> dict[str, str | bool]:
    # Lazy import: deployment_logs → queue → this module.
    from app.services.deployment_logs import (
        DONE_LINE,
        PROVISION_START_LINE,
        PROVISION_SUCCESS_LINE,
        publish_provision_lines_sync,
        provision_failure_line,
    )

    attempt = task.request.retries + 1
    # Always strip deployment_id so TerraformProvisioner never sees an unexpected kwarg.
    linked = _deployment_id_from_meta(meta, project_id=project_id, environment_id=environment_id)
    deployment_id = linked if kind is JobKind.provision else None
    redis_url = get_settings().redis_url if deployment_id is not None else None
    logger.info("%s environment=%s project=%s attempt=%d", kind.value, environment_id, project_id, attempt)
    if deployment_id is not None and deployment_is_cancelled_sync(deployment_id):
        logger.info("event=deployment_cancelled_noop deployment=%s", deployment_id)
        return {
            "kind": kind.value,
            "project_id": project_id,
            "environment_id": environment_id,
            "changed": False,
            "detail": "cancelled",
        }
    if deployment_id is not None and attempt == 1:
        publish_provision_lines_sync(redis_url, deployment_id, PROVISION_START_LINE)
    operation = _provisioner.provision if kind is JobKind.provision else _provisioner.reconcile
    call_kwargs: dict[str, Any] = dict(meta)
    params = inspect.signature(operation).parameters
    accepts_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    requested_ref: str | None = None
    raw_ref = meta.get("git_ref")
    if raw_ref:
        try:
            requested_ref = validate_git_ref(raw_ref)
        except PermanentJobError:
            requested_ref = None
    if deployment_id is not None and kind is JobKind.provision:
        def _remember_checkout(sha: str) -> None:
            try_record_deployment_release_sync(
                deployment_id, git_sha=sha, git_ref=requested_ref
            )

        call_kwargs["on_checkout"] = _remember_checkout

        def _begin_swap() -> None:
            if not try_begin_deployment_swap_sync(deployment_id):
                raise DeploymentCancelled("cancelled before container swap")

        call_kwargs["on_swap"] = _begin_swap
    if not accepts_var_kw:
        call_kwargs = {k: v for k, v in call_kwargs.items() if k in params}
    if deployment_id is not None and "on_log" in params:
        def _publish_line(line: str) -> None:
            publish_provision_lines_sync(redis_url, deployment_id, line)
        call_kwargs["on_log"] = _publish_line
    try:
        outcome = operation(project_id=project_id, environment_id=environment_id, **call_kwargs)
    except DeploymentCancelled:
        logger.info("event=deployment_cancelled_noop deployment=%s", deployment_id)
        return {
            "kind": kind.value,
            "project_id": project_id,
            "environment_id": environment_id,
            "changed": False,
            "detail": "cancelled",
        }
    except Exception as exc:
        if kind is JobKind.provision:
            logger.error("event=iac_apply_failed environment=%s project=%s", environment_id, project_id)
            record_iac_apply_failure()
        if deployment_id is not None and not _will_retry(task, exc):
            # No Done: Terminal treats Done as success. Stream ends on follow-idle.
            publish_provision_lines_sync(redis_url, deployment_id, provision_failure_line(exc))
            try_mark_deployment_failed_sync(deployment_id)
            try_record_deployment_release_sync(deployment_id, git_ref=requested_ref, finished=True)
            _release_waiting(deployment_id)
            notify_deployment_event_sync(
                project_id=project_id,
                project_slug=meta.get("project_slug", project_id[:8]),
                environment_slug=meta.get("environment_slug", environment_id[:8]),
                event="failed",
                deployment_id=str(deployment_id),
                detail=f"Deployment failed ({type(exc).__name__}).",
            )
        raise
    if deployment_id is not None:
        # Fixed success line only — never publish outcome.detail (may grow unsafe).
        publish_provision_lines_sync(redis_url, deployment_id, PROVISION_SUCCESS_LINE, DONE_LINE)
        try_record_deployment_release_sync(deployment_id, git_ref=requested_ref, finished=True)
        # The row stays ``running`` until the console reports Done. The finish time
        # is what tells a waiting deployment that this one is over.
        _release_waiting(deployment_id)
        notify_deployment_event_sync(
            project_id=project_id,
            project_slug=meta.get("project_slug", project_id[:8]),
            environment_slug=meta.get("environment_slug", environment_id[:8]),
            event="succeeded",
            deployment_id=str(deployment_id),
            detail="Deployment is live.",
        )
    logger.info("%s environment=%s changed=%s", kind.value, environment_id, outcome.changed)
    return outcome.as_dict()


@shared_task(
    name=PROVISION_TASK,
    bind=True,
    autoretry_for=_RETRY_ON,
    retry_backoff=True,
    retry_jitter=True,
    acks_late=True,
)
def provision_environment(self: Task, *, project_id: str, environment_id: str, **meta: str) -> dict[str, str | bool]:
    return _run(self, JobKind.provision, project_id, environment_id, **meta)


@shared_task(
    name=RECONCILE_TASK,
    bind=True,
    autoretry_for=_RETRY_ON,
    retry_backoff=True,
    retry_jitter=True,
    acks_late=True,
)
def reconcile_environment(self: Task, *, project_id: str, environment_id: str, **meta: str) -> dict[str, str | bool]:
    return _run(self, JobKind.reconcile, project_id, environment_id, **meta)


@shared_task(name=CONTAINER_LOGS_TASK)
def fetch_environment_container_logs(
    *, project_id: str, environment_id: str, tail: int = 200
) -> dict[str, Any]:
    settings = get_settings()
    return fetch_container_logs_sync(
        project_id=project_id,
        environment_id=environment_id,
        docker_bin=settings.docker_bin,
        docker_host=settings.docker_host,
        tail=tail,
    )


@shared_task(name=IMAGE_PRESENT_TASK)
def check_retained_image(
    *,
    project_id: str,
    project_slug: str,
    environment_id: str,
    environment_slug: str,
    deployment_id: str,
) -> bool | None:
    """Tell the API whether a deployment's image is still here to promote (#272)."""
    settings = get_settings()
    return image_present_sync(
        project_id=project_id,
        project_slug=project_slug,
        environment_id=environment_id,
        environment_slug=environment_slug,
        deployment_id=deployment_id,
        docker_bin=settings.docker_bin,
        docker_host=settings.docker_host,
    )


@shared_task(name=CONTAINER_ACTION_TASK)
def execute_environment_container_action(
    *, project_id: str, environment_id: str, action: str
) -> dict[str, Any]:
    settings = get_settings()
    return run_container_action_sync(
        project_id=project_id,
        environment_id=environment_id,
        action=action,
        docker_bin=settings.docker_bin,
        docker_host=settings.docker_host,
    )


@shared_task(name=CONTAINER_METRICS_TASK)
def collect_environment_container_metrics(
    *, project_id: str, environment_id: str
) -> dict[str, Any]:
    settings = get_settings()
    return fetch_container_metrics_sync(
        project_id=project_id,
        environment_id=environment_id,
        docker_bin=settings.docker_bin,
        docker_host=settings.docker_host,
    )


@shared_task(name=CONTAINER_REMOVE_TASK)
def remove_environment_container(*, project_id: str, environment_id: str) -> bool:
    settings = get_settings()
    return remove_container_sync(
        project_id=project_id,
        environment_id=environment_id,
        docker_bin=settings.docker_bin,
        docker_host=settings.docker_host,
    )
