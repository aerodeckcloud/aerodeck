"""Unit tests for Application Blueprint Schema inspection (#236) and Postgres/Redis addon provisioning (#194, #237)."""

from __future__ import annotations

import uuid
from pathlib import Path

from app.services.blueprint_inspector import inspect_checkout_blueprint
from app.services.docker_addons import parse_addons_json, provision_environment_addons, tenant_network_name
from app.services.docker_provisioner import CommandResult, DockerProvisioner, LogSink


def test_inspect_checkout_blueprint_parses_explicit_aerodeck_yaml(tmp_path: Path) -> None:
    (tmp_path / "aerodeck.yaml").write_text(
        "version: 1\n"
        "name: custom-portal\n"
        "build:\n"
        "  strategy: dockerfile\n"
        "  port: 3000\n"
        "addons:\n"
        "  - type: postgres\n"
        "  - type: redis\n"
        "env:\n"
        "  - key: KEYCLOAK_CLIENT_SECRET\n"
        "    description: OIDC secret\n"
        "    required: true\n"
        "    secret: true\n"
        "  - key: SESSION_SECRET_KEY\n"
        "    required: true\n"
        "    secret: true\n"
        "    generate: hex32\n",
        encoding="utf-8",
    )
    bp = inspect_checkout_blueprint(tmp_path, "https://github.com/mangatohjunior/dev-portal", "dev-portal")
    assert bp.name == "custom-portal"
    assert bp.source == "aerodeck.yaml"
    assert bp.build.port == 3000
    assert [a.type for a in bp.addons] == ["postgres", "redis"]
    assert [a.env_var for a in bp.addons] == ["DATABASE_URL", "REDIS_URL"]
    assert [e.key for e in bp.env] == ["KEYCLOAK_CLIENT_SECRET", "SESSION_SECRET_KEY"]
    assert bp.env[1].generate == "hex32"


def test_inspect_checkout_blueprint_synthesizes_from_compose_and_code(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\nEXPOSE 8000\n", encoding="utf-8")
    (tmp_path / "config.py").write_text(
        'import os\nTOKEN = os.getenv("GITLAB_TOKEN", "")\nSECRET = os.getenv("KEYCLOAK_CLIENT_SECRET", "")\n',
        encoding="utf-8",
    )
    (tmp_path / "docker-compose.yaml").write_text(
        "services:\n"
        "  db:\n"
        "    image: postgres:16-alpine\n"
        "  portal:\n"
        "    build: .\n"
        "    environment:\n"
        "      KEYCLOAK_CLIENT_SECRET: dev-portal-secret\n"
        "      SESSION_SECRET_KEY: dev-session-secret-change-me\n",
        encoding="utf-8",
    )
    bp = inspect_checkout_blueprint(tmp_path, "https://github.com/mangatohjunior/dev-portal", "dev-portal")
    assert bp.source == "docker-compose"
    assert bp.build.service == "portal"
    assert bp.build.port == 8000
    assert [a.type for a in bp.addons] == ["postgres"]
    keys = {e.key: e for e in bp.env}
    assert "GITLAB_TOKEN" in keys and keys["GITLAB_TOKEN"].secret is True
    assert "KEYCLOAK_CLIENT_SECRET" in keys and keys["KEYCLOAK_CLIENT_SECRET"].default == "dev-portal-secret"
    assert "SESSION_SECRET_KEY" in keys and keys["SESSION_SECRET_KEY"].generate == "hex32"


def test_provision_environment_addons_starts_postgres_and_redis_and_injects_urls() -> None:
    calls: list[list[str]] = []

    class AddonRunner:
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
            calls.append(args)
            if args[:3] == ["docker", "network", "inspect"]:
                return CommandResult(1, "", "no such network")
            if args[:2] == ["docker", "inspect"]:
                return CommandResult(1, "", "not running")
            return CommandResult(0, "", "")

    p_id = uuid.UUID("f27ee337-a7d0-4d4d-8180-22579d6a2983")
    e_id = uuid.UUID("bacbb040-41fa-4623-8e4b-02be2f7abf7b")
    logs: list[str] = []
    net_name, injected = provision_environment_addons(
        runner=AddonRunner(),
        docker_bin="docker",
        docker_env={},
        project_id=p_id,
        environment_id=e_id,
        addons=parse_addons_json('["postgres", "redis", "mongodb"]'),
        on_log=logs.append,
    )
    assert net_name == tenant_network_name(str(p_id), str(e_id))
    assert "DATABASE_URL" in injected and injected["DATABASE_URL"].startswith("postgresql://aerodeck:")
    assert "REDIS_URL" in injected and injected["REDIS_URL"].startswith("redis://:")
    assert "MONGODB_URI" in injected and injected["MONGODB_URI"].startswith("mongodb://aerodeck:")
    assert any(c[:3] == ["docker", "network", "create"] for c in calls)
    assert any("postgres:16-alpine" in c for c in calls)
    assert any("redis:7-alpine" in c for c in calls)
    assert any("mongo:7" in c for c in calls)
    assert any(c[:2] == ["docker", "exec"] and "pg_isready" in c for c in calls)
    assert any(c[:2] == ["docker", "exec"] and "redis-cli" in c for c in calls)
    assert any(c[:2] == ["docker", "exec"] and "mongosh" in c for c in calls)


def test_docker_provisioner_attaches_addons_to_tenant_network() -> None:
    calls: list[list[str]] = []

    class FullRunner:
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
            calls.append(args)
            if args[:2] == ["git", "clone"]:
                target = Path(args[-1])
                target.mkdir(parents=True, exist_ok=True)
                (target / "Dockerfile").write_text("FROM alpine:3.20\nEXPOSE 8000\n", encoding="utf-8")
            if args[:2] == ["docker", "inspect"]:
                return CommandResult(0, "true\n", "")
            return CommandResult(0, "", "")

    provisioner = DockerProvisioner(
        docker_bin="docker",
        git_bin="git",
        bind_host="10.41.0.2",
        port_range_start=10000,
        port_range_end=19999,
        container_port=8000,
        memory_limit="512m",
        cpus_limit="0.5",
        pids_limit=256,
        build_timeout_seconds=300,
        runner=FullRunner(),
        port_allocator=lambda _h, _s, _e: 10500,
        health_checker=lambda _h, _p, _t: True,
        route_registrar=None,
    )
    provisioner.provision(
        project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
        environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
        project_slug="payments",
        environment_slug="dev",
        repo_url="https://github.com/mangatohjunior/demo-app",
        addons_json='["postgres"]',
    )
    run_calls = [c for c in calls if c[:2] == ["docker", "run"]]
    assert len(run_calls) == 1
    assert "aerodeck-net-f27ee337-bacbb040" in run_calls[0]
