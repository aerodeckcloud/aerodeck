"""Unit tests for DockerProvisioner (#209, #210) and worker backend selection."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.services.docker_provisioner import (
    CommandResult,
    DockerProvisioner,
    LogSink,
    container_name_for,
    image_tag_for,
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


def _make_provisioner(runner: FakeCommandRunner, port: int = 10420) -> DockerProvisioner:
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
        port_allocator=lambda _h, _s, _e: port,
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

    # Verify commands executed in order with security flags.
    assert [c[:2] for c in runner.calls] == [
        ["git", "clone"],
        ["docker", "build"],
        ["docker", "rm"],
        ["docker", "run"],
    ]
    clone_cmd = runner.calls[0]
    assert "--depth=1" in clone_cmd
    assert "https://github.com/mangatohjunior/demo-app.git" in clone_cmd
    assert runner.envs[0] is not None and runner.envs[0]["GIT_TERMINAL_PROMPT"] == "0"

    run_cmd = runner.calls[3]
    assert "--memory" in run_cmd and "512m" in run_cmd
    assert "--cpus" in run_cmd and "0.5" in run_cmd
    assert "--pids-limit" in run_cmd and "256" in run_cmd
    assert "no-new-privileges:true" in run_cmd
    assert "10.41.0.2:10420:8000" in run_cmd

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
    assert container_name_for("F27EE337-a7d0", "BACBB040-41fa") == "aerodeck-app-f27ee337-bacbb040"
    assert image_tag_for("My_App!", "Prod Env", "11111111", "22222222") == "aerodeck/my-app-prod-env:latest"


def test_select_provisioner_respects_backend_setting() -> None:
    base_kwargs = {"auth_signing_key": "a" * 32}
    assert select_provisioner(Settings(**base_kwargs)) is None
    docker_prov = select_provisioner(Settings(**base_kwargs, provisioner_backend="docker"))
    assert isinstance(docker_prov, DockerProvisioner)
