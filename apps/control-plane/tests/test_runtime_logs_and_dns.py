"""Unit tests for idempotent DNS publish skipping and runtime container logs (#247)."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.db import SessionLocal, create_engine
from app.models.certificate import Certificate, CertificateStatus
from app.models.dns_record import DnsRecord, DnsRecordStatus, DnsRecordType
from app.models.project import Environment, EnvironmentKind, Project
from app.models.route import Route
from app.services.container_logs import (
    clamp_tail,
    dispatch_container_logs,
    fetch_container_logs_sync,
)
from app.services.docker_policy import CommandResult, LogSink
from app.repositories.routes import RouteRepository
from app.services.dns_records import MemoryDnsRecords
from app.services.docker_routes import (
    _ensure_dns_and_tls,
    lookup_enabled_route_hostname,
    primary_enabled_route,
    upsert_environment_route,
)
from app.services.provisioning import PermanentJobError


class FakeLogsRunner:
    def __init__(self, *, inspect_res: CommandResult, logs_res: CommandResult | None = None) -> None:
        self.inspect_res = inspect_res
        self.logs_res = logs_res or CommandResult(0, "", "")
        self.calls: list[list[str]] = []

    def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 600,
        on_line: LogSink | None = None,
    ) -> CommandResult:
        _ = (cwd, env, timeout, on_line)
        self.calls.append(args)
        if args[:2] == ["docker", "inspect"]:
            return self.inspect_res
        if args[:2] == ["docker", "logs"]:
            return self.logs_res
        return CommandResult(0, "", "")


def test_fetch_container_logs_returns_sanitized_lines() -> None:
    runner = FakeLogsRunner(
        inspect_res=CommandResult(0, "running\n", ""),
        logs_res=CommandResult(
            0,
            "2026-11-22T15:00:00Z server started\n",
            "2026-11-22T15:00:01Z \x00warning line\n",
        ),
    )
    result = fetch_container_logs_sync(
        project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
        environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
        tail=9999,
        runner=runner,
    )

    assert result["container"] == "aerodeck-app-f27ee337-bacbb040"
    assert result["status"] == "running"
    assert result["error"] is None
    assert result["lines"] == [
        "2026-11-22T15:00:00Z server started",
        "2026-11-22T15:00:01Z warning line",
    ]
    assert runner.calls[1] == [
        "docker",
        "logs",
        "--tail",
        "500",
        "--timestamps",
        "aerodeck-app-f27ee337-bacbb040",
    ]
    assert clamp_tail(0) == 1
    assert clamp_tail(1000) == 500


def test_fetch_container_logs_handles_missing_container() -> None:
    runner = FakeLogsRunner(
        inspect_res=CommandResult(1, "", "Error: No such object: aerodeck-app-f27ee337-bacbb040"),
    )
    result = fetch_container_logs_sync(
        project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
        environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
        runner=runner,
    )
    assert result == {
        "container": "aerodeck-app-f27ee337-bacbb040",
        "status": "not_found",
        "lines": [],
        "error": None,
    }
    assert len(runner.calls) == 1


def test_dispatch_container_logs_degrades_when_backend_not_docker() -> None:
    settings = Settings(auth_signing_key="a" * 32, provisioner_backend="noop")
    result = dispatch_container_logs(
        settings,
        project_id=uuid.UUID("f27ee337-a7d0-4d4d-8180-22579d6a2983"),
        environment_id=uuid.UUID("bacbb040-41fa-4623-8e4b-02be2f7abf7b"),
    )
    assert result.status == "unavailable"
    assert result.lines == []
    assert result.error is not None


@pytest.mark.asyncio
async def test_ensure_dns_skips_publish_when_record_already_published(
    app: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _ = app
    async with SessionLocal() as session:
        project = Project(slug="portal", name="Portal")
        session.add(project)
        await session.flush()
        env = Environment(project_id=project.id, slug="dev", name="Dev", kind=EnvironmentKind.dev, region="ams3")
        session.add(env)
        await session.flush()
        route = Route(
            project_id=project.id,
            environment_id=env.id,
            hostname="portal-dev.dev.app.aero-deck.app",
            path_prefix="/",
            backend_host="10.41.0.2",
            backend_port=10000,
            entrypoint="web",
            enabled=True,
        )
        session.add(route)
        await session.flush()
        cert = Certificate(route_id=route.id, hostname=route.hostname, status=CertificateStatus.issued)
        dns = DnsRecord(
            route_id=route.id,
            hostname=route.hostname,
            record_type=DnsRecordType.CNAME,
            data="edge.aero-deck.app.",
            ttl=300,
            provider_record_id="12345",
            status=DnsRecordStatus.published,
        )
        session.add_all([cert, dns])
        await session.flush()

        settings = Settings(
            auth_signing_key="a" * 32,
            dns_record_type="CNAME",
            dns_record_target="edge.aero-deck.app",
            certificate_key_dir=str(tmp_path),
        )
        (tmp_path / f"{cert.id}.key").write_text("dummy-key", encoding="utf-8")

        publisher = MagicMock()
        monkeypatch.setattr("app.services.docker_routes.get_settings", lambda: settings)
        monkeypatch.setattr("app.services.docker_routes.get_dns_record_publisher", lambda _s: publisher)

        await _ensure_dns_and_tls(session, route, cert)

        publisher.upsert.assert_not_called()


@pytest.mark.asyncio
async def test_redeploy_keeps_the_old_hostname_and_refreshes_its_backend(
    app: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _ = app
    old = "portal-dev.dev.app.aero-deck.app"
    new = "portal-dev.dev.aerodeckcloud.com"
    async with SessionLocal() as session:
        project = Project(slug="portal", name="Portal")
        session.add(project)
        await session.flush()
        env = Environment(project_id=project.id, slug="dev", name="Dev", kind=EnvironmentKind.dev, region="ams3")
        session.add(env)
        await session.flush()
        route = Route(
            project_id=project.id,
            environment_id=env.id,
            hostname=old,
            path_prefix="/",
            backend_host="10.41.0.2",
            backend_port=10000,
            entrypoint="web",
            enabled=True,
        )
        session.add(route)
        await session.flush()
        cert = Certificate(route_id=route.id, hostname=route.hostname, status=CertificateStatus.issued)
        session.add(cert)
        await session.flush()
        (tmp_path / f"{cert.id}.key").write_text("dummy-key", encoding="utf-8")
        project_id, environment_id = project.id, env.id
        await session.commit()

    settings = Settings(
        auth_signing_key="a" * 32,
        dns_record_type="CNAME",
        dns_record_target="edge.aerodeckcloud.com",
        certificate_key_dir=str(tmp_path),
    )
    publisher = MemoryDnsRecords()
    monkeypatch.setattr("app.services.docker_routes.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.docker_routes.get_dns_record_publisher", lambda _s: publisher)

    saved = await upsert_environment_route(
        project_id=project_id,
        environment_id=environment_id,
        hostname=new,
        backend_host="10.41.0.2",
        backend_port=10555,
    )

    assert saved.hostname == old
    assert saved.backend_port == 10555
    async with SessionLocal() as session:
        rows = list(await RouteRepository(session).list_for_environment(project_id, environment_id))
    assert [row.hostname for row in rows] == [old]
    assert rows[0].backend_port == 10555
    assert rows[0].enabled is True
    assert set(publisher.published) == {old}
    assert publisher.published[old].data == "edge.aerodeckcloud.com"


def test_primary_enabled_route_keeps_the_oldest_enabled_hostname() -> None:
    older = Route(
        project_id=uuid.uuid4(),
        environment_id=uuid.uuid4(),
        hostname="older.dev.app.aero-deck.app",
        path_prefix="/",
        backend_host="10.41.0.2",
        backend_port=10000,
        entrypoint="web",
        enabled=True,
    )
    newer = Route(
        project_id=older.project_id,
        environment_id=older.environment_id,
        hostname="newer.dev.aerodeckcloud.com",
        path_prefix="/",
        backend_host="10.41.0.2",
        backend_port=10001,
        entrypoint="web",
        enabled=True,
    )
    disabled = Route(
        project_id=older.project_id,
        environment_id=older.environment_id,
        hostname="disabled.dev.aerodeckcloud.com",
        path_prefix="/",
        backend_host="10.41.0.2",
        backend_port=10002,
        entrypoint="web",
        enabled=False,
    )
    assert primary_enabled_route([disabled, older, newer]) is older
    assert primary_enabled_route([disabled]) is None


def test_lookup_enabled_route_hostname_returns_the_live_hostname(
    app: object, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = app
    hostname = "portal-dev.dev.app.aero-deck.app"

    async def seed() -> tuple[uuid.UUID, uuid.UUID]:
        assert settings.database_url is not None
        engine = create_engine(settings.database_url, poolclass=NullPool)
        sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        try:
            async with sessions() as session:
                project = Project(slug="portal-lookup", name="Portal")
                session.add(project)
                await session.flush()
                env = Environment(
                    project_id=project.id, slug="dev", name="Dev", kind=EnvironmentKind.dev, region="ams3"
                )
                session.add(env)
                await session.flush()
                session.add(
                    Route(
                        project_id=project.id,
                        environment_id=env.id,
                        hostname=hostname,
                        path_prefix="/",
                        backend_host="10.41.0.2",
                        backend_port=10000,
                        entrypoint="web",
                        enabled=True,
                    )
                )
                await session.commit()
                return project.id, env.id
        finally:
            await engine.dispose()

    project_id, environment_id = asyncio.run(seed())
    monkeypatch.setattr("app.services.docker_routes.get_settings", lambda: settings)

    assert lookup_enabled_route_hostname(project_id=project_id, environment_id=environment_id) == hostname


def test_lookup_enabled_route_hostname_fails_closed_when_the_engine_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.docker_routes.get_settings",
        lambda: Settings(
            _env_file=None,
            auth_signing_key="a" * 32,
            database_url="postgresql+asyncpg://unused",
        ),
    )

    def broken(_url: str, *, poolclass: object = None) -> object:
        _ = poolclass
        raise RuntimeError("engine down")

    monkeypatch.setattr("app.services.docker_routes.create_engine", broken)
    with pytest.raises(PermanentJobError, match="Could not read existing routes"):
        lookup_enabled_route_hostname(project_id=uuid.uuid4(), environment_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_runtime_logs_endpoint_returns_not_found_for_unlinked_deployment(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    created = await client.post(
        "/api/v1/deployments",
        json={"repo_url": "https://github.com/mangatohjunior/dev-portal"},
        headers=auth,
    )
    assert created.status_code == 201
    deployment_id = created.json()["id"]

    resp = await client.get(f"/api/v1/deployments/{deployment_id}/runtime-logs?tail=100", headers=auth)
    assert resp.status_code == 200
    assert resp.json() == {
        "container": "",
        "status": "not_found",
        "lines": [],
        "error": "Deployment is not linked to an environment.",
    }
