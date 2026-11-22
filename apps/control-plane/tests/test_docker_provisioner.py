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
from app.services.provisioning import JobKind, PermanentJobError, TransientJobError
from app.worker import select_provisioner


class FakeCommandRunner:
    def __init__(self, *, create_dockerfile: bool = True, fail_on: dict[str, CommandResult] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []
        self.create_dockerfile = create_dockerfile
        self.fail_on = fail_on or {}

    def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 600,
        on_line: LogSink | None = None,
    ) -> CommandResult:
        _ = (cwd, timeout)
        self.calls.append(args)
        self.envs.append(env)
        key = " ".join(args[:2])
        if key in self.fail_on:
            return self.fail_on[key]
        if args[:2] == ["git", "clone"]:
            target = Path(args[-1])
            target.mkdir(parents=True, exist_ok=True)
            if self.create_dockerfile:
                (target / "Dockerfile").write_text("FROM alpine:3.20\n", encoding="utf-8")
            if on_line:
                on_line("Cloning into 'repo'...")
            return CommandResult(0, "", "")
        if args[:2] == ["docker", "build"]:
            if on_line:
                on_line("#1 [internal] load build definition from Dockerfile")
                on_line("#2 exporting to image")
            return CommandResult(0, "built", "")
        if args[:2] == ["docker", "rm"]:
            return CommandResult(0, "", "")
        if args[:2] == ["docker", "run"]:
            return CommandResult(0, "abc123container\n", "")
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
        route_base_domain=route_base_domain,
        port_allocator=lambda _h, _s, _e: port,
        health_checker=health_checker or (lambda _h, _p, _t: True),
        route_registrar=route_registrar,
    )


def test_provision_clones_builds_runs_and_streams_logs() -> None:
    runner = FakeCommandRunner(create_dockerfile=True)
    provisioner = _make_provisioner(runner, port=10420)
    streamed: list[str] = []

    outcome = provisioner.provision(
        project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
        environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
        project_slug="payments",
        environment_slug="dev",
        repo_url="https://github.com/mangatohjunior/demo-app",
        on_log=streamed.append,
    )

    assert outcome.kind is JobKind.provision
    assert outcome.changed is True
    assert "aerodeck/payments-dev:latest" in outcome.detail
    assert "10.41.0.2:10420" in outcome.detail

    # Verify blue/green swap order: run candidate -> health check -> remove old -> rename candidate.
    assert [c[:2] for c in runner.calls] == [
        ["git", "clone"],
        ["docker", "build"],
        ["docker", "run"],
        ["docker", "rm"],
        ["docker", "rename"],
    ]
    clone_cmd = runner.calls[0]
    assert "--depth=1" in clone_cmd
    assert "https://github.com/mangatohjunior/demo-app.git" in clone_cmd
    assert runner.envs[0] is not None and runner.envs[0]["GIT_TERMINAL_PROMPT"] == "0"

    run_cmd = runner.calls[2]
    assert "--network" in run_cmd and "bridge" in run_cmd
    assert "--cap-drop=ALL" in run_cmd
    assert "--log-opt" in run_cmd and "max-size=10m" in run_cmd
    assert "--memory" in run_cmd and "512m" in run_cmd
    assert "--cpus" in run_cmd and "0.5" in run_cmd
    assert "--pids-limit" in run_cmd and "256" in run_cmd
    assert "no-new-privileges:true" in run_cmd
    assert "10.41.0.2:10420:8000" in run_cmd
    assert runner.calls[3] == ["docker", "rm", "-f", "aerodeck-app-f27ee337-bacbb040"]
    assert runner.calls[4][-1] == "aerodeck-app-f27ee337-bacbb040"

    # Verify streamed SSE lines include clone, build, and start steps.
    assert any("Cloning mangatohjunior/demo-app" in line for line in streamed)
    assert any("#1 [internal] load build definition" in line for line in streamed)
    assert any("Container aerodeck-app-f27ee337-bacbb040 running on 10.41.0.2:10420." in line for line in streamed)


def test_provision_rejects_missing_dockerfile() -> None:
    runner = FakeCommandRunner(create_dockerfile=False)
    provisioner = _make_provisioner(runner)

    with pytest.raises(PermanentJobError, match="does not contain a root Dockerfile"):
        provisioner.provision(
            project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
            environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
            repo_url="https://github.com/mangatohjunior/no-docker",
        )
    assert [c[:2] for c in runner.calls] == [["git", "clone"]]


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
    assert runner.envs[1] is not None and runner.envs[1]["DOCKER_HOST"] == "unix:///run/user/1000/docker.sock"
    run_cmd = runner.calls[2]
    assert "--runtime" in run_cmd and "runsc" in run_cmd


def test_provision_rolls_back_candidate_when_health_check_fails() -> None:
    runner = FakeCommandRunner(create_dockerfile=True)
    registered: list[tuple[str, str, int]] = []

    def fake_register(
        *,
        project_id: uuid.UUID,
        environment_id: uuid.UUID,
        hostname: str,
        backend_host: str,
        backend_port: int,
    ) -> None:
        _ = (project_id, environment_id)
        registered.append((hostname, backend_host, backend_port))

    provisioner = _make_provisioner(
        runner,
        route_base_domain="dev.app.aero-deck.app",
        health_checker=lambda _h, _p, _t: False,
        route_registrar=fake_register,
    )

    with pytest.raises(PermanentJobError, match="failed health check"):
        provisioner.provision(
            project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
            environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
            project_slug="payments",
            environment_slug="dev",
            repo_url="https://github.com/mangatohjunior/demo-app",
        )

    # Candidate was removed, old canonical container was NEVER touched, route was NEVER mutated.
    assert [c[:2] for c in runner.calls] == [
        ["git", "clone"],
        ["docker", "build"],
        ["docker", "run"],
        ["docker", "rm"],
    ]
    assert runner.calls[3][-1].startswith("aerodeck-app-f27ee337-bacbb040-new-")
    assert registered == []


def test_provision_registers_traefik_route_after_health_check() -> None:
    runner = FakeCommandRunner(create_dockerfile=True)
    registered: list[tuple[str, str, int]] = []

    def fake_register(
        *,
        project_id: uuid.UUID,
        environment_id: uuid.UUID,
        hostname: str,
        backend_host: str,
        backend_port: int,
    ) -> None:
        _ = (project_id, environment_id)
        registered.append((hostname, backend_host, backend_port))

    provisioner = _make_provisioner(
        runner,
        port=10555,
        route_base_domain="dev.app.aero-deck.app",
        route_registrar=fake_register,
    )

    outcome = provisioner.provision(
        project_id="f27ee337-a7d0-4d4d-8180-22579d6a2983",
        environment_id="bacbb040-41fa-4623-8e4b-02be2f7abf7b",
        project_slug="payments",
        environment_slug="dev",
        repo_url="https://github.com/mangatohjunior/demo-app",
    )

    assert registered == [("payments-dev.dev.app.aero-deck.app", "10.41.0.2", 10555)]
    assert "https://payments-dev.dev.app.aero-deck.app" in outcome.detail
    assert (
        route_hostname_for(
            "payments",
            "dev",
            "f27ee337-a7d0-4d4d-8180-22579d6a2983",
            "bacbb040-41fa-4623-8e4b-02be2f7abf7b",
            "dev.app.aero-deck.app",
        )
        == "payments-dev.dev.app.aero-deck.app"
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
    assert [c[:2] for c in runner.calls] == [["git", "clone"]]
