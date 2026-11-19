"""Celery worker entrypoint. The API process never imports this module.

Run as its own container from the same image:

    celery -A app.worker worker --beat --queues aerodeck --loglevel INFO

``--beat`` fires the sandbox reap on ``AERODECK_SANDBOX_REAP_INTERVAL_SECONDS``.
When ``AERODECK_IAC_WORKER=true``, install the tenant Terraform provisioner
(#34). Otherwise jobs stay no-ops.
"""

from celery.signals import worker_process_init
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.core.db import bind_engine, create_engine
from app.core.logging import configure_logging
from app.core.queue import get_celery_app
from app.services.provisioning import Provisioner
from app.tasks.environments import use_provisioner


def select_provisioner(cfg: Settings) -> Provisioner | None:
    """Resolve the active provisioner from settings (#34, #209)."""
    if cfg.provisioner_backend == "docker":
        from app.services.docker_provisioner import DockerProvisioner

        return DockerProvisioner.from_settings(cfg)
    if cfg.provisioner_backend == "terraform" or cfg.iac_worker:
        from app.services.terraform_provisioner import TerraformProvisioner

        return TerraformProvisioner.from_settings(cfg)
    return None


settings = get_settings()
configure_logging(settings.log_level)

_selected = select_provisioner(settings)
if _selected is not None:
    use_provisioner(_selected)


@worker_process_init.connect
def _bind_database(**_: object) -> None:
    url = get_settings().database_url
    if url:
        bind_engine(create_engine(url, poolclass=NullPool))


# `celery -A app.worker` picks up this attribute.
celery_app = get_celery_app()
