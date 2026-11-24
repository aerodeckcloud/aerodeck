"""Upsert Traefik routes, publish DNS records, and issue TLS certificates for Docker-provisioned environments (#212, #240)."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.api.deps import (
    get_acme_issuer,
    get_certificate_key_store,
    get_dns01_provider,
    get_dns_record_publisher,
)
from app.core.config import get_settings
from app.core.db import SessionLocal, create_engine
from app.core.logging import get_logger
from app.models.certificate import Certificate, CertificateStatus
from app.models.dns_record import DnsRecordStatus, DnsRecordType
from app.models.route import Route
from app.repositories.certificates import CertificateRepository
from app.repositories.dns_records import DnsRecordRepository
from app.repositories.routes import RouteRepository
from app.services.dns_records import DnsRecordService, provider_record_data
from app.services.provisioning import PermanentJobError

logger = get_logger("services.docker_routes")


async def _ensure_dns_and_tls(session: AsyncSession, route: Route, cert: Certificate) -> None:
    """Best-effort publish DigitalOcean DNS record and issue ACME TLS certificate (#240, #247)."""
    settings = get_settings()
    dns_repo = DnsRecordRepository(session)
    desired_type = DnsRecordType(settings.dns_record_type)
    desired_data = provider_record_data(desired_type.value, settings.dns_record_target)
    existing_dns = await dns_repo.get_for_route(route.id)
    already_published = (
        existing_dns is not None
        and existing_dns.status is DnsRecordStatus.published
        and existing_dns.hostname == route.hostname
        and existing_dns.record_type is desired_type
        and existing_dns.data in {settings.dns_record_target, desired_data}
    )
    if not already_published:
        try:
            publisher = get_dns_record_publisher(settings)
            dns_service = DnsRecordService(
                dns_repo,
                publisher,
                record_type=desired_type,
                data=settings.dns_record_target,
                ttl=settings.dns_record_ttl,
            )
            await dns_service.publish(route)
        except Exception as exc:
            logger.warning("auto DNS publish skipped for %s: %s", route.hostname, type(exc).__name__)

    keys = get_certificate_key_store(settings)
    if cert.status is CertificateStatus.issued and keys.get(cert.id) is not None:
        return
    try:
        dns01 = get_dns01_provider(settings)
        issuer = get_acme_issuer(dns01, settings)
        material = await asyncio.to_thread(issuer.issue, route.hostname)
        cert.hostname = route.hostname
        cert.status = CertificateStatus.issued
        cert.not_after = material.not_after
        cert.fingerprint_sha256 = material.fingerprint_sha256
        cert.certificate_pem = material.certificate_pem
        cert.last_error = None
        keys.put(cert.id, material.private_key_pem)
        logger.info("auto-issued TLS certificate for %s until %s", route.hostname, material.not_after.isoformat())
    except Exception as exc:
        logger.warning("auto TLS issuance skipped for %s: %s", route.hostname, type(exc).__name__)


async def _select_environment_route(
    routes: RouteRepository,
    *,
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    hostname: str,
    backend_host: str,
    backend_port: int,
) -> Route:
    """Keep a hostname that already serves this environment and point every live route at the new container.

    A base-domain change must not open a second hostname or leave the previous
    backend port attached to a container that was just removed.
    """
    owned = list(await routes.list_for_environment(project_id, environment_id))
    enabled = [route for route in owned if route.enabled]
    matched = await routes.get_by_hostname(hostname)
    if matched is not None and (matched.project_id != project_id or matched.environment_id != environment_id):
        raise PermanentJobError(f"Hostname {hostname!r} is already routed by another environment")
    if matched is not None:
        primary = matched
        primary.enabled = True
    elif enabled:
        primary = enabled[0]
    else:
        primary = await routes.add(
            Route(
                project_id=project_id,
                environment_id=environment_id,
                hostname=hostname,
                path_prefix="/",
                backend_host=backend_host,
                backend_port=backend_port,
                entrypoint="web",
                enabled=True,
            )
        )
    primary.backend_host = backend_host
    primary.backend_port = backend_port
    for route in enabled:
        if route.id != primary.id:
            route.backend_host = backend_host
            route.backend_port = backend_port
    return primary


async def upsert_environment_route(
    *,
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    hostname: str,
    backend_host: str,
    backend_port: int,
) -> Route:
    """Point this environment's live route at the new container.

    The first publish uses ``hostname``. A later publish keeps the stored
    hostname, including one created under a previous base domain.
    """
    if SessionLocal.kw.get("bind") is None:
        raise PermanentJobError("Database is not configured; cannot register Traefik route")

    async with SessionLocal() as session:
        routes = RouteRepository(session)
        certs = CertificateRepository(session)
        route = await _select_environment_route(
            routes,
            project_id=project_id,
            environment_id=environment_id,
            hostname=hostname,
            backend_host=backend_host,
            backend_port=backend_port,
        )
        cert = await certs.get_for_route(route.id)
        if cert is None:
            cert = await certs.add(
                Certificate(route_id=route.id, hostname=route.hostname, status=CertificateStatus.pending)
            )
        await _ensure_dns_and_tls(session, route, cert)
        await session.commit()
        return route


async def _enabled_route_hostname(
    database_url: str, project_id: uuid.UUID, environment_id: uuid.UUID
) -> str | None:
    engine = create_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with sessions() as session:
            rows = await RouteRepository(session).list_for_environment(project_id, environment_id)
    finally:
        await engine.dispose()
    enabled = [row for row in rows if row.enabled]
    return enabled[0].hostname if enabled else None


def lookup_enabled_route_hostname(*, project_id: uuid.UUID, environment_id: uuid.UUID) -> str | None:
    """Return the hostname already serving this environment.

    Uses a private engine so the check does not share a loop with the later
    route write. An empty database URL means nothing is stored yet.
    """
    database_url = get_settings().database_url
    if not database_url:
        return None
    try:
        return asyncio.run(_enabled_route_hostname(database_url, project_id, environment_id))
    except Exception as exc:
        logger.warning(
            "event=route_hostname_lookup_failed project=%s environment=%s error_type=%s",
            project_id,
            environment_id,
            type(exc).__name__,
        )
        raise PermanentJobError("Could not read existing routes before choosing a hostname") from exc


def upsert_environment_route_sync(
    *,
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    hostname: str,
    backend_host: str,
    backend_port: int,
) -> None:
    """Synchronous bridge for Celery worker tasks."""
    asyncio.run(
        upsert_environment_route(
            project_id=project_id,
            environment_id=environment_id,
            hostname=hostname,
            backend_host=backend_host,
            backend_port=backend_port,
        )
    )
