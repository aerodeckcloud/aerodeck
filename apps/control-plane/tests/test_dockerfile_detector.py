"""Unit tests for zero-config Dockerfile detection (#217, #193) and PgBouncer DB settings (#214)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.db import requires_disabled_statement_cache
from app.services.docker_provisioner import CommandResult, DockerProvisioner, LogSink
from app.services.dockerfile_detector import (
    detect_dockerfile_template,
    detect_exposed_port,
    discover_repo_env_defaults,
    ensure_dockerfile,
    wait_for_container_or_fast_fail,
)
from app.services.provisioning import PermanentJobError


def test_detects_node_python_go_and_static_manifests(tmp_path: Path) -> None:
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    (node_dir / "package.json").write_text('{"name":"demo","scripts":{"start":"node index.js"}}', encoding="utf-8")
    detected_node = detect_dockerfile_template(node_dir, container_port=8000)
    assert detected_node is not None
    assert "Node.js" in detected_node[0]
    assert "FROM node:22-alpine" in detected_node[1]
    assert "USER node" in detected_node[1]

    py_dir = tmp_path / "py"
    py_dir.mkdir()
    (py_dir / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (py_dir / "main.py").write_text("print('hi')\n", encoding="utf-8")
    detected_py = detect_dockerfile_template(py_dir, container_port=8000)
    assert detected_py is not None
    assert "Python" in detected_py[0]
    assert 'CMD ["python", "main.py"]' in detected_py[1]
    assert "USER appuser" in detected_py[1]

    go_dir = tmp_path / "go"
    go_dir.mkdir()
    (go_dir / "go.mod").write_text("module example.com/demo\ngo 1.23\n", encoding="utf-8")
    detected_go = detect_dockerfile_template(go_dir, container_port=8000)
    assert detected_go is not None
    assert "Go" in detected_go[0]
    assert "go build" in detected_go[1]

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<h1>Hello</h1>\n", encoding="utf-8")
    detected_static = detect_dockerfile_template(static_dir, container_port=8000)
    assert detected_static is not None
    assert "Static HTML" in detected_static[0]
    assert "http.server" in detected_static[1]


def test_ensure_dockerfile_generates_and_validates_or_fails(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(PermanentJobError, match="does not contain a root Dockerfile"):
        ensure_dockerfile(empty_dir, "mangatohjunior", "empty-repo")

    node_dir = tmp_path / "node_app"
    node_dir.mkdir()
    (node_dir / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    logs: list[str] = []
    dockerfile = ensure_dockerfile(node_dir, "mangatohjunior", "node-app", on_log=logs.append)
    assert dockerfile.is_file()
    assert any("auto-generating Dockerfile for Node.js" in line for line in logs)


def test_provisioner_auto_generates_dockerfile_and_uses_buildkit_cache() -> None:
    calls: list[list[str]] = []

    class NodeRepoRunner:
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
                (target / "package.json").write_text('{"name":"auto-node"}', encoding="utf-8")
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
        runner=NodeRepoRunner(),
        port_allocator=lambda _h, _s, _e: 10444,
        health_checker=lambda _h, _p, _t: True,
        route_registrar=None,
    )
    streamed: list[str] = []
    provisioner.provision(
        project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
        environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
        project_slug="payments",
        environment_slug="dev",
        repo_url="https://github.com/mangatohjunior/node-only-app",
        on_log=streamed.append,
    )
    build_cmd = calls[1]
    assert "--cache-from" in build_cmd
    assert "BUILDKIT_INLINE_CACHE=1" in build_cmd
    assert any("auto-generating Dockerfile for Node.js" in line for line in streamed)


def test_requires_disabled_statement_cache_detects_pgbouncer_port_and_query() -> None:
    assert (
        requires_disabled_statement_cache(
            "postgresql+asyncpg://u:p@private-pg.db.ondigitalocean.com:25061/defaultdb?ssl=require"
        )
        is True
    )
    assert (
        requires_disabled_statement_cache(
            "postgresql+asyncpg://u:p@private-pg.db.ondigitalocean.com:25060/defaultdb?ssl=require&pgbouncer=true"
        )
        is True
    )
    assert (
        requires_disabled_statement_cache(
            "postgresql+asyncpg://u:p@private-pg.db.ondigitalocean.com:25060/defaultdb?ssl=require"
        )
        is False
    )


def test_detect_exposed_port_parses_dockerfile_or_falls_back(tmp_path: Path) -> None:
    df = tmp_path / "Dockerfile"
    df.write_text("FROM alpine:3.20\nEXPOSE 3000/tcp\n", encoding="utf-8")
    assert detect_exposed_port(df, default_port=8000) == 3000
    df.write_text("FROM alpine:3.20\n", encoding="utf-8")
    assert detect_exposed_port(df, default_port=8000) == 8000


def test_discover_repo_env_defaults_extracts_compose_and_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("FOO=bar\nPATH=/evil\n", encoding="utf-8")
    (tmp_path / "docker-compose.yaml").write_text(
        "services:\n"
        "  keycloak:\n"
        "    image: quay.io/keycloak/keycloak:25.0\n"
        "    environment:\n"
        "      KEYCLOAK_ADMIN: admin\n"
        "  portal:\n"
        "    build: .\n"
        "    environment:\n"
        "      KEYCLOAK_CLIENT_SECRET: dev-portal-secret\n"
        "      SESSION_SECRET_KEY: dev-session-secret-change-me\n",
        encoding="utf-8",
    )
    defaults = discover_repo_env_defaults(tmp_path)
    assert defaults["FOO"] == "bar"
    assert defaults["KEYCLOAK_CLIENT_SECRET"] == "dev-portal-secret"
    assert defaults["SESSION_SECRET_KEY"] == "dev-session-secret-change-me"
    assert "KEYCLOAK_ADMIN" not in defaults
    assert "PATH" not in defaults


def test_wait_for_container_or_fast_fail_aborts_when_container_exits() -> None:
    class ExitedContainerRunner:
        def run(self, args: list[str], **kwargs: object) -> CommandResult:
            _ = (args, kwargs)
            return CommandResult(0, "false 1\n", "")

    assert (
        wait_for_container_or_fast_fail(
            "127.0.0.1",
            19998,
            30,
            runner=ExitedContainerRunner(),
            docker_bin="docker",
            container_name="crashed-app",
            docker_env={},
        )
        is False
    )
