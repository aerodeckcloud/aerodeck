"""Shared ``Depends()`` providers."""

import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Header, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.errors import InvalidTokenError, QueueUnavailableError
from app.core.queue import create_celery_app
from app.models.dns_record import DnsRecordType
from app.repositories.certificates import CertificateRepository
from app.repositories.console_sessions import ConsoleSessionRepository
from app.repositories.console_users import ConsoleUserRepository
from app.repositories.deployments import DeploymentRepository
from app.repositories.clients import ClientRepository, SettingsClientRepository
from app.repositories.dns_records import DnsRecordRepository
from app.repositories.environments import EnvironmentRepository
from app.repositories.jobs import CeleryJobDispatcher, JobState
from app.repositories.projects import ProjectRepository
from app.repositories.routes import RouteRepository
from app.repositories.sandbox_hosts import SandboxHostRepository
from app.repositories.sandboxes import SandboxRepository
from app.services.acme import AcmeIssuer, AcmeUnavailableError, FakeAcmeIssuer
from app.services.audit_logs import AuditLogRepository, AuditLogService
from app.services.auth import AuthenticatedClient, AuthService
from app.services.certificate_keys import FileCertificateKeyStore
from app.services.certificates import CertificateService
from app.services.console_sessions import ConsoleSessionService
from app.services.console_users import ConsoleUserService
from app.services.deployment_terminal import DeploymentTerminalService
from app.services.deployments import DeploymentService
from app.services.dns01 import DigitalOceanDns01, Dns01Provider, Dns01UnavailableError, MemoryDns01
from app.services.dns_records import (
    DigitalOceanDnsRecords,
    DnsRecordPublisher,
    DnsRecordService,
    DnsRecordUnavailableError,
    MemoryDnsRecords,
)
from app.services.environments import EnvironmentService
from app.services.environment_vars import EnvironmentVariableRepository, EnvironmentVariableService
from app.services.hypervisor import FirecrackerHypervisor, Hypervisor, MemoryHypervisor, SandboxUnavailableError
from app.services.jobs import DeploymentEnqueueHooks, JobDispatcher, JobService
from app.services.letsencrypt import LetsEncryptIssuer
from app.services.notifications import NotificationChannelRepository, NotificationService
from app.services.pr_previews import PrPreviewService
from app.services.projects import ProjectService
from app.services.project_members import ProjectMemberRepository, ProjectMemberService
from app.services.routes import RouteService
from app.services.sandbox_hosts import SandboxHostService
from app.services.sandboxes import SandboxService
from app.services.webhooks import WebhookRepository, WebhookService
from app.tasks.deployment_status import (
    try_mark_deployment_failed_isolated,
    try_set_provision_job_id_isolated,
)

SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


@dataclass(frozen=True, slots=True)
class PaginationParams:
    limit: int
    offset: int


def get_pagination(
    limit: Annotated[int, Query(ge=1, le=200, description="Page size")] = 50,
    offset: Annotated[int, Query(ge=0, description="Items to skip")] = 0,
) -> PaginationParams:
    return PaginationParams(limit=limit, offset=offset)


PaginationDep = Annotated[PaginationParams, Depends(get_pagination)]


def get_deployment_repository(session: SessionDep) -> DeploymentRepository:
    return DeploymentRepository(session)


DeploymentRepositoryDep = Annotated[DeploymentRepository, Depends(get_deployment_repository)]


def get_project_service(
    session: SessionDep, deployments: DeploymentRepositoryDep
) -> ProjectService:
    return ProjectService(ProjectRepository(session), deployments)


ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]


def get_project_member_service(
    session: SessionDep, projects: ProjectServiceDep
) -> ProjectMemberService:
    return ProjectMemberService(
        ProjectMemberRepository(session),
        ConsoleUserRepository(session),
        projects,
    )


ProjectMemberServiceDep = Annotated[ProjectMemberService, Depends(get_project_member_service)]


def get_audit_log_service(
    session: SessionDep, projects: ProjectServiceDep
) -> AuditLogService:
    return AuditLogService(
        AuditLogRepository(session),
        projects,
        ConsoleUserRepository(session),
    )


AuditLogServiceDep = Annotated[AuditLogService, Depends(get_audit_log_service)]


def get_notification_service(
    session: SessionDep, projects: ProjectServiceDep, settings: SettingsDep
) -> NotificationService:
    return NotificationService(
        NotificationChannelRepository(session),
        projects,
        signing_key=settings.auth_signing_key.get_secret_value(),
    )


NotificationServiceDep = Annotated[NotificationService, Depends(get_notification_service)]


def get_actor_user_id(
    x_aerodeck_user_id: str | None = Header(default=None, alias="X-AeroDeck-User-Id"),
) -> uuid.UUID | None:
    if not x_aerodeck_user_id:
        return None
    try:
        return uuid.UUID(x_aerodeck_user_id.strip())
    except ValueError:
        return None


ActorUserIdDep = Annotated[uuid.UUID | None, Depends(get_actor_user_id)]


def get_console_user_service(session: SessionDep) -> ConsoleUserService:
    return ConsoleUserService(ConsoleUserRepository(session))


ConsoleUserServiceDep = Annotated[ConsoleUserService, Depends(get_console_user_service)]


def get_console_session_service(session: SessionDep) -> ConsoleSessionService:
    return ConsoleSessionService(ConsoleSessionRepository(session), ConsoleUserRepository(session))


ConsoleSessionServiceDep = Annotated[ConsoleSessionService, Depends(get_console_session_service)]


def get_hypervisor(settings: SettingsDep) -> Hypervisor:
    if settings.sandbox_hypervisor == "firecracker":
        url = settings.firecracker_api_url.strip()
        if not url:
            raise SandboxUnavailableError("AERODECK_FIRECRACKER_API_URL is unset")
        return FirecrackerHypervisor(api_url=url, image_root=settings.sandbox_image_root)
    return MemoryHypervisor()


HypervisorDep = Annotated[Hypervisor, Depends(get_hypervisor)]


def get_sandbox_host_service(session: SessionDep) -> SandboxHostService:
    return SandboxHostService(SandboxHostRepository(session), SandboxRepository(session))


SandboxHostServiceDep = Annotated[SandboxHostService, Depends(get_sandbox_host_service)]


def get_sandbox_service(
    session: SessionDep,
    projects: ProjectServiceDep,
    hypervisor: HypervisorDep,
    hosts: SandboxHostServiceDep,
    settings: SettingsDep,
) -> SandboxService:
    return SandboxService(
        SandboxRepository(session),
        projects,
        hypervisor,
        hosts,
        require_host=settings.sandbox_hypervisor == "firecracker",
    )


SandboxServiceDep = Annotated[SandboxService, Depends(get_sandbox_service)]


def get_environment_service(
    session: SessionDep,
    projects: ProjectServiceDep,
    deployments: DeploymentRepositoryDep,
) -> EnvironmentService:
    return EnvironmentService(EnvironmentRepository(session), projects, deployments)


EnvironmentServiceDep = Annotated[EnvironmentService, Depends(get_environment_service)]


def get_environment_variable_service(
    session: SessionDep,
    environments: EnvironmentServiceDep,
    settings: SettingsDep,
) -> EnvironmentVariableService:
    return EnvironmentVariableService(
        EnvironmentVariableRepository(session),
        environments,
        signing_key=settings.auth_signing_key.get_secret_value(),
    )


EnvironmentVariableServiceDep = Annotated[
    EnvironmentVariableService, Depends(get_environment_variable_service)
]


def get_webhook_service(
    session: SessionDep,
    environments: EnvironmentServiceDep,
    env_vars: EnvironmentVariableServiceDep,
    settings: SettingsDep,
) -> WebhookService:
    pr_previews = PrPreviewService(
        EnvironmentRepository(session),
        environments,
        env_vars,
        settings,
    )
    return WebhookService(
        WebhookRepository(session),
        environments,
        signing_key=settings.auth_signing_key.get_secret_value(),
        pr_previews=pr_previews,
    )


WebhookServiceDep = Annotated[WebhookService, Depends(get_webhook_service)]


def get_deployment_terminal_service(
    deployments: DeploymentRepositoryDep,
) -> DeploymentTerminalService:
    return DeploymentTerminalService(deployments)


DeploymentTerminalServiceDep = Annotated[
    DeploymentTerminalService, Depends(get_deployment_terminal_service)
]


def get_deployment_service(
    deployments: DeploymentRepositoryDep,
    environments: EnvironmentServiceDep,
    terminal: DeploymentTerminalServiceDep,
) -> DeploymentService:
    return DeploymentService(deployments, environments, terminal)


DeploymentServiceDep = Annotated[DeploymentService, Depends(get_deployment_service)]


def get_dns_record_publisher(settings: SettingsDep) -> DnsRecordPublisher:
    if settings.dns_records_provider == "digitalocean":
        token = settings.digitalocean_token
        if token is None:
            raise DnsRecordUnavailableError("AERODECK_DIGITALOCEAN_TOKEN is unset")
        return DigitalOceanDnsRecords(token.get_secret_value())
    return MemoryDnsRecords()


DnsRecordPublisherDep = Annotated[DnsRecordPublisher, Depends(get_dns_record_publisher)]


def get_dns_record_service(
    session: SessionDep, publisher: DnsRecordPublisherDep, settings: SettingsDep
) -> DnsRecordService:
    return DnsRecordService(
        DnsRecordRepository(session),
        publisher,
        record_type=DnsRecordType(settings.dns_record_type),
        data=settings.dns_record_target,
        ttl=settings.dns_record_ttl,
    )


DnsRecordServiceDep = Annotated[DnsRecordService, Depends(get_dns_record_service)]


def get_certificate_key_store(settings: SettingsDep) -> FileCertificateKeyStore:
    return FileCertificateKeyStore(settings.certificate_key_dir)


CertificateKeyStoreDep = Annotated[FileCertificateKeyStore, Depends(get_certificate_key_store)]


def get_route_service(
    session: SessionDep,
    projects: ProjectServiceDep,
    environments: EnvironmentServiceDep,
    dns_records: DnsRecordServiceDep,
    keys: CertificateKeyStoreDep,
) -> RouteService:
    return RouteService(
        RouteRepository(session), projects, environments, CertificateRepository(session), dns_records, keys
    )


RouteServiceDep = Annotated[RouteService, Depends(get_route_service)]


def get_dns01_provider(settings: SettingsDep) -> Dns01Provider:
    if settings.dns01_provider == "digitalocean":
        token = settings.digitalocean_token
        if token is None:
            raise Dns01UnavailableError("AERODECK_DIGITALOCEAN_TOKEN is unset")
        return DigitalOceanDns01(token.get_secret_value())
    return MemoryDns01()


Dns01ProviderDep = Annotated[Dns01Provider, Depends(get_dns01_provider)]


def get_acme_issuer(dns: Dns01ProviderDep, settings: SettingsDep) -> AcmeIssuer:
    if settings.acme_issuer == "letsencrypt":
        email = (settings.acme_account_email or "").strip()
        key = settings.acme_account_key.get_secret_value() if settings.acme_account_key else ""
        if not email or not key:
            raise AcmeUnavailableError("AERODECK_ACME_ACCOUNT_EMAIL and AERODECK_ACME_ACCOUNT_KEY are required")
        return LetsEncryptIssuer(
            dns,
            directory_url=settings.acme_directory_url,
            account_email=email,
            account_key_pem=key,
        )
    return FakeAcmeIssuer(dns, lifetime=timedelta(days=settings.acme_lifetime_days))


AcmeIssuerDep = Annotated[AcmeIssuer, Depends(get_acme_issuer)]


def get_certificate_service(
    session: SessionDep,
    routes: RouteServiceDep,
    issuer: AcmeIssuerDep,
    keys: CertificateKeyStoreDep,
    settings: SettingsDep,
) -> CertificateService:
    return CertificateService(
        CertificateRepository(session),
        routes,
        issuer,
        keys,
        renew_before=timedelta(days=settings.certificate_renew_before_days),
    )


CertificateServiceDep = Annotated[CertificateService, Depends(get_certificate_service)]


_QUEUE_UNSET = "Job queue is not configured (AERODECK_REDIS_URL is unset)"


class UnconfiguredJobDispatcher:
    """Stands in for the broker when Redis is unset. Every call answers 503."""

    def dispatch(self, kind: object, *, project_id: str, environment_id: str, **meta: str) -> str:
        _ = (kind, project_id, environment_id, meta)
        raise QueueUnavailableError(_QUEUE_UNSET)

    def state(self, job_id: str) -> JobState:
        _ = job_id
        raise QueueUnavailableError(_QUEUE_UNSET)

    def revoke(self, job_id: str) -> None:
        _ = job_id
        raise QueueUnavailableError(_QUEUE_UNSET)


def get_job_dispatcher(settings: SettingsDep) -> JobDispatcher:
    """The broker, or a stand-in that raises 503 when it is used.

    Resolving this never fails: holding a deployment behind the active one
    writes only to the database, so it must work without a queue.
    """
    if settings.redis_url is None:
        return UnconfiguredJobDispatcher()
    return CeleryJobDispatcher(create_celery_app(settings))


JobDispatcherDep = Annotated[JobDispatcher, Depends(get_job_dispatcher)]


def get_job_service(
    dispatcher: JobDispatcherDep,
    environments: EnvironmentServiceDep,
    deployments: DeploymentServiceDep,
    env_vars: EnvironmentVariableServiceDep,
    settings: SettingsDep,
) -> JobService:
    return JobService(
        dispatcher,
        environments,
        deployments,
        redis_url=settings.redis_url,
        deployment_hooks=DeploymentEnqueueHooks(
            mark_failed=try_mark_deployment_failed_isolated,
            set_provision_job_id=try_set_provision_job_id_isolated,
        ),
        env_var_loader=env_vars,
    )


JobServiceDep = Annotated[JobService, Depends(get_job_service)]


def get_release_job_service(
    dispatcher: JobDispatcherDep, jobs: JobServiceDep
) -> JobService | None:
    """None without a queue, so a waiting deploy is not promoted with nowhere to run."""
    if isinstance(dispatcher, UnconfiguredJobDispatcher):
        return None
    return jobs


ReleaseJobServiceDep = Annotated[JobService | None, Depends(get_release_job_service)]

# auto_error=False so a missing header becomes our {"detail", "code"} 401, not FastAPI's 403.
_bearer = HTTPBearer(auto_error=False, description="Access token from POST /api/v1/auth/token")
BearerDep = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


def get_client_repository(settings: SettingsDep) -> ClientRepository:
    return SettingsClientRepository(settings.auth_clients)


ClientRepositoryDep = Annotated[ClientRepository, Depends(get_client_repository)]


def get_auth_service(clients: ClientRepositoryDep, settings: SettingsDep) -> AuthService:
    return AuthService(clients, settings)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


async def get_current_client(credentials: BearerDep, auth: AuthServiceDep) -> AuthenticatedClient:
    if credentials is None:
        raise InvalidTokenError("Missing bearer token")
    return await auth.authenticate(credentials.credentials)


ClientDep = Annotated[AuthenticatedClient, Depends(get_current_client)]


async def require_client(credentials: BearerDep, auth: AuthServiceDep) -> None:
    """Router-level guard: every method needs a valid enrolled-client token."""
    if credentials is None:
        raise InvalidTokenError("Missing bearer token")
    await auth.authenticate(credentials.credentials)


AUTH_GUARD = [Depends(require_client)]
# Kept so existing routers that imported MUTATION_GUARD stay equivalent (#36).
MUTATION_GUARD = AUTH_GUARD


def _bearer_matches(provided: str, expected: str) -> bool:
    if len(provided) != len(expected):
        return False
    return secrets.compare_digest(provided, expected)


async def require_edge_reader(credentials: BearerDep, auth: AuthServiceDep, settings: SettingsDep) -> None:
    """Traefik (static provider token) or any enrolled API client may read the snapshot."""
    if credentials is None:
        raise InvalidTokenError("Missing bearer token")
    provider = settings.traefik_provider_token
    if provider is not None and _bearer_matches(credentials.credentials, provider.get_secret_value()):
        return
    await auth.authenticate(credentials.credentials)


EdgeReaderDep = Annotated[None, Depends(require_edge_reader)]
