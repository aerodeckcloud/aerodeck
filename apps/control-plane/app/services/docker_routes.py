"""Upsert Traefik routes in Postgres for Docker-provisioned environments (#212).

Runs inside the Celery worker after a candidate container passes its health check,
using ``SessionLocal`` (bound with ``NullPool`` in ``worker.py``).
"""

from __future__ import annotations

import asyncio
import uuid

from app.core.db import SessionLocal
from app.models.certificate import Certificate, CertificateStatus
from app.models.route import Route
from app.repositories.certificates import CertificateRepository
from app.repositories.routes import RouteRepository
from app.services.provisioning import PermanentJobError


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
        existing = await routes.get_by_hostname(hostname)
        if existing is not None:
            if existing.project_id != project_id or existing.environment_id != environment_id:
                raise PermanentJobError(
                    f"Hostname {hostname!r} is already routed by another environment"
                )
            existing.backend_host = backend_host
            existing.backend_port = backend_port
            existing.enabled = True
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
        certs = CertificateRepository(session)
        await certs.add(
            Certificate(
                route_id=route.id,
                hostname=route.hostname,
                status=CertificateStatus.pending,
            )
        )
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
