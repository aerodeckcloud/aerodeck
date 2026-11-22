"""Upsert Traefik routes, publish DNS records, and issue TLS certificates for Docker-provisioned environments (#212, #240)."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_acme_issuer,
    get_certificate_key_store,
    get_dns01_provider,
    get_dns_record_publisher,
)
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.models.certificate import Certificate, CertificateStatus
from app.models.dns_record import DnsRecordType
from app.models.route import Route
from app.repositories.certificates import CertificateRepository
from app.repositories.dns_records import DnsRecordRepository
from app.repositories.routes import RouteRepository
from app.services.dns_records import DnsRecordService
from app.services.provisioning import PermanentJobError

logger = get_logger("services.docker_routes")


async def _ensure_dns_and_tls(session: AsyncSession, route: Route, cert: Certificate) -> None:
    """Best-effort publish DigitalOcean DNS record and issue ACME TLS certificate (#240)."""
    settings = get_settings()
    try:
        publisher = get_dns_record_publisher(settings)
        dns_service = DnsRecordService(
            DnsRecordRepository(session),
            publisher,
            record_type=DnsRecordType(settings.dns_record_type),
            data=settings.dns_record_target,
            ttl=settings.dns_record_ttl,
        )
        await dns_service.publish(route)
    except Exception as exc:
        logger.warning("auto DNS publish skipped for %s: %s", route.hostname, type(exc).__name__)

    if cert.status is CertificateStatus.issued:
        return
    try:
        dns01 = get_dns01_provider(settings)
        issuer = get_acme_issuer(dns01, settings)
        keys = get_certificate_key_store(settings)
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


async def upsert_environment_route(
    *,
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    hostname: str,
    backend_host: str,
    backend_port: int,
) -> Route:
    """Create or update the Traefik ``Route`` pointing ``hostname`` to the new container."""
    if SessionLocal.kw.get("bind") is None:
        raise PermanentJobError("Database is not configured; cannot register Traefik route")

    async with SessionLocal() as session:
        routes = RouteRepository(session)
        certs = CertificateRepository(session)
        existing = await routes.get_by_hostname(hostname)
        if existing is not None:
            if existing.project_id != project_id or existing.environment_id != environment_id:
                raise PermanentJobError(
                    f"Hostname {hostname!r} is already routed by another environment"
                )
            existing.backend_host = backend_host
            existing.backend_port = backend_port
            existing.enabled = True
            cert = await certs.get_for_route(existing.id)
            if cert is None:
                cert = await certs.add(
                    Certificate(route_id=existing.id, hostname=existing.hostname, status=CertificateStatus.pending)
                )
            await _ensure_dns_and_tls(session, existing, cert)
            await session.commit()
            return existing

        route = await routes.add(
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
        cert = await certs.add(
            Certificate(
                route_id=route.id,
                hostname=route.hostname,
                status=CertificateStatus.pending,
            )
        )
        await _ensure_dns_and_tls(session, route, cert)
        await session.commit()
        return route


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
