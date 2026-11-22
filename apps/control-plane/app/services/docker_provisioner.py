"""Build and run tenant containers directly via Docker (#209, #210, #212, #215)."""

from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.config import Settings
from app.core.github import canonical_github_repo_url, parse_github_repo
from app.services.docker_policy import (
    CommandResult, CommandRunner, LogSink, SubprocessCommandRunner, classify_failure, container_name_for,
    find_available_port, image_tag_for, route_hostname_for, validate_binary, validate_dockerfile,
    validate_network, validate_runtime, validate_uuid, wait_for_tcp_health,
)
from app.services.docker_routes import upsert_environment_route_sync
from app.services.dockerfile_detector import detect_exposed_port, ensure_dockerfile, stream_container_crash_logs
from app.services.provisioning import JobKind, JobOutcome, PermanentJobError

__all__ = [
    "CommandResult", "CommandRunner", "DockerProvisioner", "HealthChecker", "LogSink", "PortAllocator",
    "RouteRegistrar", "SubprocessCommandRunner", "container_name_for", "find_available_port", "image_tag_for",
    "route_hostname_for", "wait_for_tcp_health",
]

PortAllocator = Callable[[str, int, int], int]
HealthChecker = Callable[[str, int, int], bool]


class RouteRegistrar(Protocol):
    def __call__(
        self, *, project_id: uuid.UUID, environment_id: uuid.UUID, hostname: str,
        backend_host: str, backend_port: int,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class DockerProvisioner:
    docker_bin: str
    git_bin: str
    bind_host: str
    port_range_start: int
    port_range_end: int
    container_port: int
    memory_limit: str
    cpus_limit: str
    pids_limit: int
    build_timeout_seconds: int
    runner: CommandRunner
    docker_host: str | None = None
    docker_runtime: str | None = None
    docker_network: str = "bridge"
    allowed_owners: tuple[str, ...] = ()
    health_timeout_seconds: int = 30
    route_base_domain: str | None = None
    port_allocator: PortAllocator = find_available_port
    health_checker: HealthChecker = wait_for_tcp_health
    route_registrar: RouteRegistrar | None = upsert_environment_route_sync

    @classmethod
    def from_settings(
        cls, settings: Settings, runner: CommandRunner | None = None, port_allocator: PortAllocator | None = None,
        health_checker: HealthChecker | None = None, route_registrar: RouteRegistrar | None = upsert_environment_route_sync,
    ) -> DockerProvisioner:
        return cls(
            docker_bin=validate_binary(settings.docker_bin, "docker_bin"), git_bin=validate_binary(settings.git_bin, "git_bin"),
            bind_host=settings.docker_bind_host, port_range_start=settings.docker_port_range_start,
            port_range_end=settings.docker_port_range_end, container_port=settings.docker_container_port,
            memory_limit=settings.docker_memory_limit, cpus_limit=settings.docker_cpus_limit,
            pids_limit=settings.docker_pids_limit, build_timeout_seconds=settings.docker_build_timeout_seconds,
            runner=runner or SubprocessCommandRunner(), docker_host=settings.docker_host,
            docker_runtime=validate_runtime(settings.docker_runtime), docker_network=validate_network(settings.docker_network),
            allowed_owners=tuple(o.strip().lower() for o in settings.docker_allowed_owners if o.strip()),
            health_timeout_seconds=settings.docker_health_timeout_seconds, route_base_domain=settings.docker_route_base_domain,
            port_allocator=port_allocator or find_available_port, health_checker=health_checker or wait_for_tcp_health,
            route_registrar=route_registrar,
        )

    def _docker_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        if not env.get("DOCKER_CONFIG"):
            cfg_dir = Path(tempfile.gettempdir()) / "aerodeck-docker-config"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            env["DOCKER_CONFIG"] = str(cfg_dir)
        if self.docker_host:
            env["DOCKER_HOST"] = self.docker_host
        if extra:
            env.update(extra)
        return env

    def provision(
        self, *, project_id: str, environment_id: str, project_slug: str = "", environment_slug: str = "",
        environment_kind: str = "dev", region: str = "ams3", repo_url: str = "", on_log: LogSink | None = None,
    ) -> JobOutcome:
        _ = (environment_kind, region)
        p_uuid = validate_uuid(project_id, "project_id")
        e_uuid = validate_uuid(environment_id, "environment_id")
        parsed = parse_github_repo(repo_url)
        if parsed is None:
            raise PermanentJobError("A valid GitHub repo_url is required for Docker provisioning")
        owner, repo = parsed
        if self.allowed_owners and owner.lower() not in self.allowed_owners:
            raise PermanentJobError(f"Repository owner {owner!r} is not in AERODECK_DOCKER_ALLOWED_OWNERS")
        clone_url = f"{canonical_github_repo_url(owner, repo)}.git"
        cname = container_name_for(str(p_uuid), str(e_uuid))
        tag = image_tag_for(project_slug, environment_slug, str(p_uuid), str(e_uuid))
        docker_bin, git_bin = validate_binary(self.docker_bin, "docker_bin"), validate_binary(self.git_bin, "git_bin")
        runtime, network = validate_runtime(self.docker_runtime), validate_network(self.docker_network)

        def emit(msg: str) -> None:
            if on_log is not None:
                on_log(msg)

        with tempfile.TemporaryDirectory(prefix="aerodeck-build-") as tmp:
            checkout = Path(tmp) / "repo"
            emit(f"Cloning {owner}/{repo} (shallow)...")
            clone_res = self.runner.run(
                [git_bin, "clone", "--depth=1", "--single-branch", "--", clone_url, str(checkout)],
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout=120, on_line=on_log,
            )
            if clone_res.returncode != 0:
                raise classify_failure("git clone", clone_res)
            dockerfile = ensure_dockerfile(checkout, owner, repo, container_port=self.container_port, on_log=on_log)
            target_port = detect_exposed_port(dockerfile, default_port=self.container_port)
            emit(f"Building Docker image {tag}...")
            build_res = self.runner.run(
                [docker_bin, "build", "--progress=plain", "--build-arg", "BUILDKIT_INLINE_CACHE=1", "--cache-from", tag, "--tag", tag, str(checkout)],
                cwd=checkout, env=self._docker_env({"DOCKER_BUILDKIT": "1"}), timeout=self.build_timeout_seconds, on_line=on_log,
            )
            if build_res.returncode != 0:
                raise classify_failure("docker build", build_res)

        docker_env = self._docker_env()
        host_port = self.port_allocator(self.bind_host, self.port_range_start, self.port_range_end)
        candidate = f"{cname}-new-{uuid.uuid4().hex[:6]}"
        run_cmd = [
            docker_bin, "run", "-d", "--name", candidate, "--restart", "unless-stopped",
            "--network", network, "--cap-drop=ALL", "--security-opt", "no-new-privileges:true",
            "--memory", self.memory_limit, "--cpus", self.cpus_limit, "--pids-limit", str(self.pids_limit),
            "--log-driver", "json-file", "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
            "--label", "aerodeck.managed=true", "--label", f"aerodeck.project_id={p_uuid}",
            "--label", f"aerodeck.environment_id={e_uuid}", "-e", f"PORT={target_port}",
            "-p", f"{self.bind_host}:{host_port}:{target_port}",
        ]
        if runtime is not None:
            run_cmd.extend(["--runtime", runtime])
        run_cmd.append(tag)

        emit(f"Starting candidate container {candidate} on {self.bind_host}:{host_port} (-> :{target_port})...")
        run_res = self.runner.run(run_cmd, env=docker_env, timeout=60)
        if run_res.returncode != 0:
            raise classify_failure("docker run", run_res)

        emit(f"Waiting for health check on {self.bind_host}:{host_port} (timeout {self.health_timeout_seconds}s)...")
        if not self.health_checker(self.bind_host, host_port, self.health_timeout_seconds):
            last = stream_container_crash_logs(self.runner, docker_bin, candidate, docker_env, on_log)
            self.runner.run([docker_bin, "rm", "-f", candidate], env=docker_env, timeout=30)
            raise PermanentJobError(
                f"container {candidate} failed health check on {self.bind_host}:{host_port}"
                f"{f' (last log: {last})' if last else ''}; rolled back and kept previous container live"
            )

        route_hostname: str | None = None
        if self.route_base_domain and self.route_registrar is not None:
            route_hostname = route_hostname_for(
                project_slug, environment_slug, str(p_uuid), str(e_uuid), self.route_base_domain
            )
            emit(f"Registering Traefik route https://{route_hostname} -> {self.bind_host}:{host_port}...")
            try:
                self.route_registrar(
                    project_id=p_uuid, environment_id=e_uuid, hostname=route_hostname,
                    backend_host=self.bind_host, backend_port=host_port,
                )
            except Exception:
                self.runner.run([docker_bin, "rm", "-f", candidate], env=docker_env, timeout=30)
                raise

        self.runner.run([docker_bin, "rm", "-f", cname], env=docker_env, timeout=30)
        rename_res = self.runner.run([docker_bin, "rename", candidate, cname], env=docker_env, timeout=30)
        if rename_res.returncode != 0:
            raise classify_failure("docker rename", rename_res)

        if route_hostname:
            emit(f"Container {cname} live at https://{route_hostname} ({self.bind_host}:{host_port}).")
            detail = f"deployed {tag} as {cname} at https://{route_hostname} ({self.bind_host}:{host_port})"
        else:
            emit(f"Container {cname} running on {self.bind_host}:{host_port}.")
            detail = f"deployed {tag} as {cname} on {self.bind_host}:{host_port}"
        return JobOutcome(
            kind=JobKind.provision, project_id=str(p_uuid), environment_id=str(e_uuid),
            changed=True, detail=detail,
        )

    def reconcile(
        self, *, project_id: str, environment_id: str, project_slug: str = "", environment_slug: str = "",
        environment_kind: str = "dev", region: str = "ams3", repo_url: str = "", on_log: LogSink | None = None,
    ) -> JobOutcome:
        _ = (project_slug, environment_slug, environment_kind, region, repo_url, on_log)
        p_uuid = validate_uuid(project_id, "project_id")
        e_uuid = validate_uuid(environment_id, "environment_id")
        cname = container_name_for(str(p_uuid), str(e_uuid))
        docker_bin = validate_binary(self.docker_bin, "docker_bin")
        docker_env = self._docker_env()
        inspect_res = self.runner.run(
            [docker_bin, "inspect", "-f", "{{.State.Running}}", cname], env=docker_env, timeout=30
        )
        if inspect_res.returncode != 0:
            raise PermanentJobError(f"container {cname} does not exist; deploy first")
        if inspect_res.stdout.strip().lower() == "true":
            return JobOutcome(
                kind=JobKind.reconcile, project_id=str(p_uuid), environment_id=str(e_uuid),
                changed=False, detail=f"container {cname} is already running",
            )
        start_res = self.runner.run([docker_bin, "start", cname], env=docker_env, timeout=60)
        if start_res.returncode != 0:
            raise classify_failure("docker start", start_res)
        return JobOutcome(
            kind=JobKind.reconcile, project_id=str(p_uuid), environment_id=str(e_uuid),
            changed=True, detail=f"restarted stopped container {cname}",
        )
