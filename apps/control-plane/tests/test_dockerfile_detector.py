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

    missing = tmp_path / "missing_start"
    missing.mkdir()
    (missing / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    with pytest.raises(PermanentJobError, match="scripts.start"):
        ensure_dockerfile(missing, "mangatohjunior", "node-app")
    assert not (missing / "Dockerfile").exists()

    node_dir = tmp_path / "node_app"
    node_dir.mkdir()
    (node_dir / "package.json").write_text(
        '{"name":"app","scripts":{"start":"node index.js"}}', encoding="utf-8"
    )
    logs: list[str] = []
    dockerfile = ensure_dockerfile(node_dir, "mangatohjunior", "node-app", on_log=logs.append)
    assert dockerfile.is_file()
    text = dockerfile.read_text(encoding="utf-8")
    assert "USER node" in text
    assert "ENV PORT=8000 HOST=0.0.0.0" in text
    assert 'CMD ["npm", "start"]' in text
    assert "--omit=dev" in text
    assert "npm run build" not in text
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
                (target / "package.json").write_text(
                    '{"name":"auto-node","scripts":{"start":"node index.js"}}', encoding="utf-8"
                )
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
    build_cmd = calls[2]
    assert "--progress=plain" in build_cmd
    assert "--cache-from" not in build_cmd
    assert any("auto-generating Dockerfile for Node.js" in line for line in streamed)


def test_node_build_script_installs_dev_dependencies_and_runs_before_start(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"build":"tsc","start":"node dist/index.js"}}', encoding="utf-8"
    )
    detected = detect_dockerfile_template(tmp_path, container_port=3000)
    assert detected is not None and detected.error is None
    text = detected.contents
    assert "npm install --no-audit --no-fund" in text
    assert "npm install --omit=dev" not in text
    assert text.index("RUN npm run build") < text.index("RUN npm prune --omit=dev")
    assert text.index("RUN npm prune --omit=dev") < text.index('CMD ["npm", "start"]')
    assert "ENV PORT=3000 HOST=0.0.0.0" in text
    assert "USER node" in text


def test_pnpm_and_yarn_lockfiles_choose_the_package_manager(tmp_path: Path) -> None:
    pnpm_dir = tmp_path / "pnpm"
    pnpm_dir.mkdir()
    (pnpm_dir / "package.json").write_text('{"scripts":{"start":"node index.js"}}', encoding="utf-8")
    (pnpm_dir / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    pnpm = detect_dockerfile_template(pnpm_dir)
    assert pnpm is not None
    assert "pnpm install --frozen-lockfile --prod" in pnpm.contents
    assert "npm install --omit=dev" not in pnpm.contents
    assert "pnpm prune" not in pnpm.contents

    pnpm_build = tmp_path / "pnpm_build"
    pnpm_build.mkdir()
    (pnpm_build / "package.json").write_text(
        '{"scripts":{"build":"tsc","start":"node dist/index.js"}}', encoding="utf-8"
    )
    (pnpm_build / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    built_pnpm = detect_dockerfile_template(pnpm_build)
    assert built_pnpm is not None
    assert "pnpm install --frozen-lockfile --prod" not in built_pnpm.contents
    assert built_pnpm.contents.index("pnpm install --frozen-lockfile") < built_pnpm.contents.index("RUN pnpm run build")
    assert built_pnpm.contents.index("RUN pnpm run build") < built_pnpm.contents.index("RUN pnpm prune --prod")

    yarn_dir = tmp_path / "yarn"
    yarn_dir.mkdir()
    (yarn_dir / "package.json").write_text(
        '{"scripts":{"build":"tsc","start":"node dist/index.js"}}', encoding="utf-8"
    )
    (yarn_dir / "yarn.lock").write_text("# yarn lockfile v1\n", encoding="utf-8")
    yarn = detect_dockerfile_template(yarn_dir)
    assert yarn is not None
    assert yarn.contents.index("RUN yarn install --frozen-lockfile\n") < yarn.contents.index("RUN yarn run build")
    assert "--production" not in yarn.contents.split("RUN yarn run build", 1)[0]
    assert yarn.contents.index("RUN yarn run build") < yarn.contents.index(
        "RUN yarn install --frozen-lockfile --production"
    )

    berry_dir = tmp_path / "berry"
    berry_dir.mkdir()
    (berry_dir / "package.json").write_text('{"scripts":{"start":"node index.js"}}', encoding="utf-8")
    (berry_dir / "yarn.lock").write_text("__metadata:\n  version: 8\n", encoding="utf-8")
    berry = detect_dockerfile_template(berry_dir)
    assert berry is not None
    assert "yarn install --immutable" in berry.contents
    assert "workspaces focus" not in berry.contents

    berry_build = tmp_path / "berry_build"
    berry_build.mkdir()
    (berry_build / "package.json").write_text(
        '{"scripts":{"build":"tsc","start":"node dist/index.js"}}', encoding="utf-8"
    )
    (berry_build / "yarn.lock").write_text("__metadata:\n  version: 8\n", encoding="utf-8")
    built_berry = detect_dockerfile_template(berry_build)
    assert built_berry is not None
    assert built_berry.contents.index("yarn install --immutable") < built_berry.contents.index("RUN yarn run build")
    assert "NODE_ENV=production" not in built_berry.contents.split("RUN yarn run build", 1)[0]
    assert built_berry.contents.index("RUN yarn run build") < built_berry.contents.index(
        "yarn workspaces focus --all --production"
    )

    workspace_dir = tmp_path / "workspaces"
    workspace_dir.mkdir()
    (workspace_dir / "package.json").write_text(
        '{"workspaces":["packages/*"],"scripts":{"start":"node index.js"}}', encoding="utf-8"
    )
    (workspace_dir / "yarn.lock").write_text("__metadata:\n  version: 8\n", encoding="utf-8")
    workspace = detect_dockerfile_template(workspace_dir)
    assert workspace is not None
    assert workspace.contents.index("COPY . .") < workspace.contents.index("yarn workspaces focus --production")
    assert workspace.contents.count("COPY . .") == 1
    assert "yarn install --immutable" not in workspace.contents

    built_dir = tmp_path / "workspace_build"
    built_dir.mkdir()
    (built_dir / "package.json").write_text(
        '{"workspaces":["packages/*"],"scripts":{"build":"tsc","start":"node index.js"}}',
        encoding="utf-8",
    )
    (built_dir / "yarn.lock").write_text("__metadata:\n  version: 8\n", encoding="utf-8")
    built = detect_dockerfile_template(built_dir)
    assert built is not None
    assert built.contents.index("COPY . .") < built.contents.index("yarn install --immutable")
    assert built.contents.count("COPY . .") == 1
    assert built.contents.index("yarn install --immutable") < built.contents.index("RUN yarn run build")
    assert built.contents.index("RUN yarn run build") < built.contents.index(
        "yarn workspaces focus --all --production"
    )
    assert "workspaces focus --production" not in built.contents.split("RUN yarn run build", 1)[0]


def test_yarn_classic_probe_stops_after_four_kibibytes(tmp_path: Path) -> None:
    classic = tmp_path / "classic"
    classic.mkdir()
    (classic / "package.json").write_text('{"scripts":{"start":"node index.js"}}', encoding="utf-8")
    (classic / "yarn.lock").write_bytes(b"# yarn lockfile v1\n" + b"x" * 200_000)
    detected_classic = detect_dockerfile_template(classic)
    assert detected_classic is not None
    assert "yarn@1.22.22" in detected_classic.contents
    assert "corepack prepare" not in detected_classic.contents

    late = tmp_path / "late"
    late.mkdir()
    (late / "package.json").write_text('{"scripts":{"start":"node index.js"}}', encoding="utf-8")
    (late / "yarn.lock").write_bytes(b"x" * 5000 + b"yarn lockfile v1\n")
    detected_late = detect_dockerfile_template(late)
    assert detected_late is not None
    assert "corepack prepare yarn@4.6.0" in detected_late.contents
    assert "yarn@1.22.22" not in detected_late.contents


def test_root_dockerfile_is_not_replaced_by_a_node_generator(tmp_path: Path) -> None:
    original = "FROM node:22-alpine\nUSER node\n"
    (tmp_path / "Dockerfile").write_text(original, encoding="utf-8")
    (tmp_path / "package.json").write_text('{"scripts":{"start":"node index.js"}}', encoding="utf-8")
    logs: list[str] = []
    path = ensure_dockerfile(tmp_path, "mangatohjunior", "node-app", on_log=logs.append)
    assert path.read_text(encoding="utf-8") == original
    assert logs == []


def test_missing_start_fails_provision_before_docker_build() -> None:
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
    with pytest.raises(PermanentJobError, match="scripts.start"):
        provisioner.provision(
            project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
            environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
            project_slug="payments",
            environment_slug="dev",
            repo_url="https://github.com/mangatohjunior/node-only-app",
        )
    assert not any(args[:2] == ["docker", "build"] for args in calls)


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
    mock_secrets = tmp_path / "mock-secrets"
    mock_secrets.mkdir()
    (mock_secrets / "gitlab-token").write_text("glpat-mock-token-123\n", encoding="utf-8")
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
    assert defaults["GITLAB_TOKEN"] == "glpat-mock-token-123"
    assert defaults["KEYCLOAK_CLIENT_SECRET"] == "dev-portal-secret"
    assert defaults["SESSION_SECRET_KEY"] == "dev-session-secret-change-me"
    assert "KEYCLOAK_ADMIN" not in defaults
    assert "PATH" not in defaults
    named = discover_repo_env_defaults(tmp_path, compose_service="keycloak")
    assert named["KEYCLOAK_ADMIN"] == "admin"
    assert "KEYCLOAK_CLIENT_SECRET" not in named


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
