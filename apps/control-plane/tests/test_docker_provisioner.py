"""Unit tests for DockerProvisioner (#209, #210) and worker backend selection."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services.docker_provisioner import (
    CommandResult,
    DockerProvisioner,
    HealthChecker,
    LogSink,
    RouteRegistrar,
    container_name_for,
    image_tag_for,
    route_hostname_for,
)
from app.services.docker_policy import CommandTimeoutError
from app.services.provisioning import DeploymentCancelled, JobKind, PermanentJobError, TransientJobError
from app.worker import select_provisioner


class FakeCommandRunner:
    def __init__(
        self,
        *,
        create_dockerfile: bool = True,
        fail_on: dict[str, CommandResult] | None = None,
        dockerfile_text: str = "FROM alpine:3.20\n",
        compose_text: str | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []
        self.timeouts: list[int] = []
        self.create_dockerfile = create_dockerfile
        self.fail_on = fail_on or {}
        self.dockerfile_text = dockerfile_text
        self.compose_text = compose_text
        self.env_files: list[str] = []

    def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 600,
        on_line: LogSink | None = None,
    ) -> CommandResult:
        _ = cwd
        self.calls.append(args)
        self.timeouts.append(timeout)
        self.envs.append(env)
        key = " ".join(args[:2])
        if key in self.fail_on:
            return self.fail_on[key]
        if args[:2] == ["git", "clone"]:
            target = Path(args[-1])
            target.mkdir(parents=True, exist_ok=True)
            if self.create_dockerfile:
                (target / "Dockerfile").write_text(self.dockerfile_text, encoding="utf-8")
            if self.compose_text is not None:
                (target / "docker-compose.yaml").write_text(self.compose_text, encoding="utf-8")
            if on_line:
                on_line("Cloning into 'repo'...")
            return CommandResult(0, "", "")
        if args[:2] == ["git", "-C"] and "rev-parse" in args:
            return CommandResult(0, "0123456789abcdef0123456789abcdef01234567\n", "")
        if args[:2] == ["docker", "build"]:
            if on_line:
                on_line("#1 [internal] load build definition from Dockerfile")
                on_line("#2 exporting to image")
            return CommandResult(0, "built", "")
        if args[:2] == ["docker", "rm"]:
            return CommandResult(0, "", "")
        if args[:2] == ["docker", "run"]:
            if "--env-file" in args:
                env_path = Path(args[args.index("--env-file") + 1])
                self.env_files.append(env_path.read_text(encoding="utf-8"))
            return CommandResult(0, "abc123container\n", "")
        if args[:2] == ["docker", "exec"]:
            if on_line:
                on_line("migrated")
            return CommandResult(0, "migrated", "")
        if args[:2] == ["docker", "inspect"]:
            return CommandResult(0, "true\n", "")
        if args[:2] == ["docker", "start"]:
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")


def _make_provisioner(
    runner: FakeCommandRunner,
    port: int = 10420,
    *,
    docker_host: str | None = None,
    docker_runtime: str | None = None,
    allowed_owners: tuple[str, ...] = (),
    route_base_domain: str | None = None,
    release_command_timeout_seconds: int = 300,
    health_checker: HealthChecker | None = None,
    route_registrar: RouteRegistrar | None = None,
) -> DockerProvisioner:
    return DockerProvisioner(
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
        runner=runner,
        docker_host=docker_host,
        docker_runtime=docker_runtime,
        allowed_owners=allowed_owners,
        health_timeout_seconds=10,
        release_command_timeout_seconds=release_command_timeout_seconds,
        route_base_domain=route_base_domain,
        port_allocator=lambda _h, _s, _e: port,
        health_checker=health_checker or (lambda _h, _p, _t: True),
        route_registrar=route_registrar,
    )


def test_provision_clones_builds_runs_and_streams_logs() -> None:
    runner = FakeCommandRunner(create_dockerfile=True)
    provisioner = _make_provisioner(runner, port=10420)
    streamed: list[str] = []
    checked_out: list[str] = []

    outcome = provisioner.provision(
        project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
        environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
        project_slug="payments",
        environment_slug="dev",
        repo_url="https://github.com/mangatohjunior/demo-app",
        on_log=streamed.append,
        on_checkout=checked_out.append,
    )

    assert outcome.kind is JobKind.provision
    assert outcome.changed is True
    assert "aerodeck/payments-dev:latest" in outcome.detail
    assert "10.41.0.2:10420" in outcome.detail

    # Verify blue/green swap order: run candidate -> health check -> remove old -> rename candidate.
    assert [c[:2] for c in runner.calls] == [
        ["git", "clone"],
        ["git", "-C"],
        ["docker", "build"],
        ["docker", "run"],
        ["docker", "rm"],
        ["docker", "rename"],
    ]
    clone_cmd = runner.calls[0]
    assert "--depth=1" in clone_cmd
    assert "https://github.com/mangatohjunior/demo-app.git" in clone_cmd
    assert runner.envs[0] is not None and runner.envs[0]["GIT_TERMINAL_PROMPT"] == "0"
    assert checked_out == ["0123456789abcdef0123456789abcdef01234567"]

    run_cmd = runner.calls[3]
    assert "--network" in run_cmd and "bridge" in run_cmd
    assert "--cap-drop=ALL" in run_cmd
    assert "--log-opt" in run_cmd and "max-size=10m" in run_cmd
    assert "--memory" in run_cmd and "512m" in run_cmd
    assert "--cpus" in run_cmd and "0.5" in run_cmd
    assert "--pids-limit" in run_cmd and "256" in run_cmd
    assert "no-new-privileges:true" in run_cmd
    assert "10.41.0.2:10420:8000" in run_cmd
    assert runner.calls[4] == ["docker", "rm", "-f", "aerodeck-app-f27ee337-bacbb040"]
    assert runner.calls[5][-1] == "aerodeck-app-f27ee337-bacbb040"

    # Verify streamed SSE lines include clone, build, and start steps.
    assert any("Checked out 0123456" in line for line in streamed)
    assert any("Cloning mangatohjunior/demo-app" in line for line in streamed)
    assert any("#1 [internal] load build definition" in line for line in streamed)
    assert any("Container aerodeck-app-f27ee337-bacbb040 running on 10.41.0.2:10420." in line for line in streamed)


def test_provision_uses_confirmed_port_instead_of_expose() -> None:
    runner = FakeCommandRunner(dockerfile_text="FROM alpine:3.20\nEXPOSE 8080\n")
    provisioner = _make_provisioner(runner, port=10420)
    streamed: list[str] = []

    provisioner.provision(
        project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
        environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
        project_slug="payments",
        environment_slug="dev",
        repo_url="https://github.com/mangatohjunior/demo-app",
        container_port="3000",
        on_log=streamed.append,
    )

    run_cmd = next(call for call in runner.calls if call[:2] == ["docker", "run"])
    assert "10.41.0.2:10420:3000" in run_cmd
    assert "10.41.0.2:10420:8080" not in run_cmd
    assert "PORT=3000" in runner.env_files[0]
    assert any("Using confirmed port 3000." in line for line in streamed)


def test_provision_scopes_compose_env_to_the_confirmed_service() -> None:
    runner = FakeCommandRunner(
        compose_text=(
            "services:\n"
            "  web:\n"
            "    build: .\n"
            "    environment:\n"
            "      WEB_ONLY: yes\n"
            "  db:\n"
            "    build: .\n"
            "    environment:\n"
            "      DB_ONLY: yes\n"
        ),
    )
    provisioner = _make_provisioner(runner)
    streamed: list[str] = []

    provisioner.provision(
        project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
        environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
        project_slug="payments",
        environment_slug="dev",
        repo_url="https://github.com/mangatohjunior/demo-app",
        compose_service="web",
        on_log=streamed.append,
    )

    assert "WEB_ONLY=yes" in runner.env_files[0]
    assert "DB_ONLY" not in runner.env_files[0]
    assert any("Using Compose service web." in line for line in streamed)


def test_provision_fetches_a_pinned_commit_instead_of_the_default_branch() -> None:
    runner = FakeCommandRunner()
    provisioner = _make_provisioner(runner)
    sha = "abcdef0123456789abcdef0123456789abcdef01"
    streamed: list[str] = []

    provisioner.provision(
        project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
        environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
        project_slug="payments",
        environment_slug="dev",
        repo_url="https://github.com/mangatohjunior/demo-app",
        git_ref=sha,
        on_log=streamed.append,
    )

    clone_cmd = runner.calls[0]
    assert "--branch" not in clone_cmd
    assert runner.calls[1][-1] == sha
    assert "fetch" in runner.calls[1]
    assert runner.calls[2][-2:] == ["--detach", "FETCH_HEAD"]
    assert any(f"commit {sha[:7]}" in line for line in streamed)


def test_provision_stops_before_the_live_container_swap_when_cancelled() -> None:
    runner = FakeCommandRunner()
    provisioner = _make_provisioner(runner)

    def _refuse() -> None:
        raise DeploymentCancelled("cancelled before container swap")

    with pytest.raises(DeploymentCancelled):
        provisioner.provision(
            project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
            environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
            project_slug="payments",
            environment_slug="dev",
            repo_url="https://github.com/mangatohjunior/demo-app",
            on_swap=_refuse,
        )

    assert not any(call[:2] == ["docker", "rename"] for call in runner.calls)
    removed = [call for call in runner.calls if call[:2] == ["docker", "rm"]]
    assert removed
    assert all("-new-" in call[-1] for call in removed)


def test_provision_rejects_missing_dockerfile() -> None:
    runner = FakeCommandRunner(create_dockerfile=False)
    provisioner = _make_provisioner(runner)

    with pytest.raises(PermanentJobError, match="does not contain a root Dockerfile"):
        provisioner.provision(
            project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
            environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
            repo_url="https://github.com/mangatohjunior/no-docker",
        )
    assert [c[:2] for c in runner.calls] == [["git", "clone"], ["git", "-C"]]


def test_provision_rejects_invalid_repo_url_without_running_commands() -> None:
    runner = FakeCommandRunner()
    provisioner = _make_provisioner(runner)

    with pytest.raises(PermanentJobError, match="valid GitHub repo_url"):
        provisioner.provision(
            project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
            environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
            repo_url="file:///etc/passwd",
        )
    assert runner.calls == []


def test_provision_classifies_transient_docker_error() -> None:
    runner = FakeCommandRunner(
        fail_on={"docker build": CommandResult(1, "", "Cannot connect to the Docker daemon")}
    )
    provisioner = _make_provisioner(runner)

    with pytest.raises(TransientJobError, match="docker build failed"):
        provisioner.provision(
            project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
            environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
            repo_url="https://github.com/mangatohjunior/demo-app",
        )


def test_reconcile_restarts_stopped_container() -> None:
    runner = FakeCommandRunner(
        fail_on={"docker inspect": CommandResult(0, "false\n", "")}
    )
    provisioner = _make_provisioner(runner)

    outcome = provisioner.reconcile(
        project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
        environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
    )
    assert outcome.kind is JobKind.reconcile
    assert outcome.changed is True
    assert [c[:2] for c in runner.calls] == [["docker", "inspect"], ["docker", "start"]]


def test_naming_helpers_sanitize_segments() -> None:
    p = "f27ee337-a7d0-4d4d-8180-22579d6a2983"
    e = "bacbb040-41fa-4623-8e4b-02be2f7abf7b"
    assert container_name_for(p, e) == "aerodeck-app-f27ee337-bacbb040"
    assert image_tag_for("My_App!", "Prod Env", p, e) == "aerodeck/my-app-prod-env:latest"
    with pytest.raises(PermanentJobError, match="invalid UUID"):
        container_name_for("not-a-uuid", e)


def test_provision_enforces_owner_allowlist_and_passes_runtime_and_docker_host() -> None:
    runner = FakeCommandRunner(create_dockerfile=True)
    provisioner = _make_provisioner(
        runner,
        docker_host="unix:///run/user/1000/docker.sock",
        docker_runtime="runsc",
        allowed_owners=("mangatohjunior",),
    )

    with pytest.raises(PermanentJobError, match="AERODECK_DOCKER_ALLOWED_OWNERS"):
        provisioner.provision(
            project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
            environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
            repo_url="https://github.com/untrusted-org/evil-repo",
        )
    assert runner.calls == []

    provisioner.provision(
        project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
        environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
        repo_url="https://github.com/mangatohjunior/demo-app",
    )
    assert runner.envs[2] is not None and runner.envs[2]["DOCKER_HOST"] == "unix:///run/user/1000/docker.sock"
    assert runner.envs[2].get("DOCKER_CONFIG")
    run_cmd = runner.calls[3]
    assert "--runtime" in run_cmd and "runsc" in run_cmd


def test_provision_rolls_back_candidate_when_health_check_fails() -> None:
    runner = FakeCommandRunner(create_dockerfile=True)
    registered: list[tuple[str, str, int]] = []
    provisioner = _make_provisioner(
        runner,
        route_base_domain="dev.aerodeckcloud.com",
        health_checker=lambda _h, _p, _t: False,
        route_registrar=lambda **kw: registered.append((kw["hostname"], kw["backend_host"], kw["backend_port"])),
    )

    with pytest.raises(PermanentJobError, match="failed health check"):
        provisioner.provision(
            project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
            environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
            project_slug="payments",
            environment_slug="dev",
            repo_url="https://github.com/mangatohjunior/demo-app",
        )

    assert [c[:2] for c in runner.calls] == [
        ["git", "clone"], ["git", "-C"], ["docker", "build"], ["docker", "run"], ["docker", "logs"], ["docker", "rm"],
    ]
    assert runner.calls[5][-1].startswith("aerodeck-app-f27ee337-bacbb040-new-")
    assert registered == []


def test_provision_registers_traefik_route_after_health_check() -> None:
    runner = FakeCommandRunner(create_dockerfile=True)
    registered: list[tuple[str, str, int]] = []
    provisioner = _make_provisioner(
        runner,
        port=10555,
        route_base_domain="dev.aerodeckcloud.com",
        route_registrar=lambda **kw: registered.append((kw["hostname"], kw["backend_host"], kw["backend_port"])),
    )

    outcome = provisioner.provision(
        project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
        environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
        project_slug="payments",
        environment_slug="dev",
        repo_url="https://github.com/mangatohjunior/demo-app",
    )

    assert registered == [("payments-dev.dev.aerodeckcloud.com", "10.41.0.2", 10555)]
    assert "https://payments-dev.dev.aerodeckcloud.com" in outcome.detail
    assert (
        route_hostname_for(
            "payments", "dev",
            "f27ee337-a7d0-4d4d-8180-22579d6a2983",
            "bacbb040-41fa-4623-8e4b-02be2f7abf7b",
            "dev.aerodeckcloud.com",
        )
        == "payments-dev.dev.aerodeckcloud.com"
    )


def test_select_provisioner_respects_backend_setting() -> None:
    base_kwargs = {"auth_signing_key": "a" * 32}
    assert select_provisioner(Settings(**base_kwargs)) is None
    docker_prov = select_provisioner(Settings(**base_kwargs, provisioner_backend="docker"))
    assert isinstance(docker_prov, DockerProvisioner)


def test_provision_rejects_host_network_and_forbidden_dockerfile_directives() -> None:
    base_kwargs = {"auth_signing_key": "a" * 32}
    with pytest.raises(PermanentJobError, match="host networking is forbidden"):
        DockerProvisioner.from_settings(
            Settings(**base_kwargs, provisioner_backend="docker", docker_network="host")
        )

    class EvilDockerfileRunner(FakeCommandRunner):
        def run(
            self,
            args: list[str],
            *,
            cwd: Path | None = None,
            env: dict[str, str] | None = None,
            timeout: int = 600,
            on_line: LogSink | None = None,
        ) -> CommandResult:
            res = super().run(args, cwd=cwd, env=env, timeout=timeout, on_line=on_line)
            if args[:2] == ["git", "clone"]:
                target = Path(args[-1])
                (target / "Dockerfile").write_text(
                    "FROM alpine:3.20\nRUN --mount=type=ssh cat /root/.ssh/id_rsa\n",
                    encoding="utf-8",
                )
            return res

    runner = EvilDockerfileRunner()
    provisioner = _make_provisioner(runner)
    with pytest.raises(PermanentJobError, match="forbidden directive"):
        provisioner.provision(
            project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
            environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
            repo_url="https://github.com/mangatohjunior/evil-dockerfile",
        )
    assert [c[:2] for c in runner.calls] == [["git", "clone"], ["git", "-C"]]


def test_release_command_runs_after_health_and_before_traffic() -> None:
    runner = FakeCommandRunner(create_dockerfile=True)
    registered: list[str] = []
    provisioner = _make_provisioner(
        runner,
        release_command_timeout_seconds=7,
        route_base_domain="dev.aerodeckcloud.com",
        route_registrar=lambda **_kw: registered.append(runner.calls[-1][1]),
    )
    streamed: list[str] = []
    secret = "postgres://secret-user:secret-pass@db/app"

    provisioner.provision(
        project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
        environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
        project_slug="payments",
        environment_slug="dev",
        repo_url="https://github.com/mangatohjunior/demo-app",
        env_vars_json='{"DATABASE_URL":"%s"}' % secret,
        release_command="python manage.py migrate",
        on_log=streamed.append,
    )

    assert [c[:2] for c in runner.calls] == [
        ["git", "clone"],
        ["git", "-C"],
        ["docker", "build"],
        ["docker", "run"],
        ["docker", "exec"],
        ["docker", "rm"],
        ["docker", "rename"],
    ]
    exec_cmd = runner.calls[4]
    assert exec_cmd[2].startswith("aerodeck-app-f27ee337-bacbb040-new-")
    assert exec_cmd[3:] == ["python", "manage.py", "migrate"]
    assert "sh" not in exec_cmd and "-c" not in exec_cmd and "-e" not in exec_cmd
    assert runner.timeouts[4] == 7
    assert registered == ["exec"]
    assert runner.calls[5] == ["docker", "rm", "-f", "aerodeck-app-f27ee337-bacbb040"]
    blob = " ".join(" ".join(call) for call in runner.calls)
    assert secret not in blob
    assert secret not in "\n".join(streamed)
    assert "migrated" in streamed
    assert any("Running release command before traffic moves" in line for line in streamed)


def test_empty_release_command_keeps_the_current_swap_order() -> None:
    runner = FakeCommandRunner(create_dockerfile=True)
    provisioner = _make_provisioner(runner)
    provisioner.provision(
        project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
        environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
        project_slug="payments",
        environment_slug="dev",
        repo_url="https://github.com/mangatohjunior/demo-app",
        release_command="   ",
    )
    assert [c[:2] for c in runner.calls] == [
        ["git", "clone"],
        ["git", "-C"],
        ["docker", "build"],
        ["docker", "run"],
        ["docker", "rm"],
        ["docker", "rename"],
    ]


def test_failed_release_command_removes_only_the_candidate() -> None:
    runner = FakeCommandRunner(fail_on={"docker exec": CommandResult(1, "nope", "")})
    provisioner = _make_provisioner(runner)
    with pytest.raises(PermanentJobError, match="release command failed \\(exit 1\\)"):
        provisioner.provision(
            project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
            environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
            project_slug="payments",
            environment_slug="dev",
            repo_url="https://github.com/mangatohjunior/demo-app",
            release_command="python manage.py migrate",
        )
    removed = [call[-1] for call in runner.calls if call[:2] == ["docker", "rm"]]
    assert len(removed) == 1
    assert "-new-" in removed[0]
    assert "aerodeck-app-f27ee337-bacbb040" not in removed
    assert not any(call[:2] == ["docker", "rename"] for call in runner.calls)


def test_release_command_timeout_fails_without_moving_traffic() -> None:
    class ExecTimeout(FakeCommandRunner):
        def run(
            self,
            args: list[str],
            *,
            cwd: Path | None = None,
            env: dict[str, str] | None = None,
            timeout: int = 600,
            on_line: LogSink | None = None,
        ) -> CommandResult:
            if args[:2] == ["docker", "exec"]:
                self.calls.append(args)
                raise CommandTimeoutError(f"command timed out after {timeout}s: docker")
            return super().run(args, cwd=cwd, env=env, timeout=timeout, on_line=on_line)

    runner = ExecTimeout()
    provisioner = _make_provisioner(runner, release_command_timeout_seconds=7)
    with pytest.raises(PermanentJobError, match="timed out after 7s"):
        provisioner.provision(
            project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
            environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
            project_slug="payments",
            environment_slug="dev",
            repo_url="https://github.com/mangatohjunior/demo-app",
            release_command="python manage.py migrate",
        )
    removed = [call[-1] for call in runner.calls if call[:2] == ["docker", "rm"]]
    assert len(removed) == 1 and "-new-" in removed[0]
    assert not any(call[:2] == ["docker", "rename"] for call in runner.calls)
