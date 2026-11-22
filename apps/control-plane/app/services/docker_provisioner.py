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
    deployment_image_tag_for, find_available_port, image_tag_for, route_hostname_for, validate_binary,
    validate_dockerfile, validate_git_ref, validate_network, validate_runtime, validate_uuid, wait_for_tcp_health,
)
from app.services.docker_addons import parse_addons_json, provision_environment_addons
from app.services.docker_env_discovery import build_docker_run_command, ensure_default_docker_config
from app.services.docker_routes import upsert_environment_route_sync
from app.services.dockerfile_detector import (
    build_runtime_env_file, detect_exposed_port, ensure_dockerfile,
    stream_container_crash_logs, wait_for_container_or_fast_fail,
)
from app.services.provisioning import JobKind, JobOutcome, PermanentJobError

__all__ = [
    "CommandResult", "CommandRunner", "DockerProvisioner", "HealthChecker", "LogSink", "PortAllocator",
    "RouteRegistrar", "SubprocessCommandRunner", "container_name_for", "find_available_port", "image_tag_for",
    "route_hostname_for", "wait_for_tcp_health",
]
PortAllocator = Callable[[str, int, int], int]
HealthChecker = Callable[[str, int, int], bool]


class RouteRegistrar(Protocol):
    def __call__(self, *, project_id: uuid.UUID, environment_id: uuid.UUID, hostname: str, backend_host: str, backend_port: int) -> None: ...


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
        return ensure_default_docker_config(os.environ, self.docker_host, extra)

    def provision(
        self, *, project_id: str, environment_id: str, project_slug: str = "", environment_slug: str = "",
        environment_kind: str = "dev", region: str = "ams3", repo_url: str = "", env_vars_json: str = "",
        addons_json: str = "", image_deployment_id: str = "", rollback_from_deployment_id: str = "",
        git_ref: str = "", on_log: LogSink | None = None,
    ) -> JobOutcome:
        _ = (environment_kind, region)
        p_uuid, e_uuid = validate_uuid(project_id, "project_id"), validate_uuid(environment_id, "environment_id")
        parsed = parse_github_repo(repo_url)
        if parsed is None:
            raise PermanentJobError("A valid GitHub repo_url is required for Docker provisioning")
        owner, repo = parsed
        if self.allowed_owners and owner.lower() not in self.allowed_owners:
            raise PermanentJobError(f"Repository owner {owner!r} is not in AERODECK_DOCKER_ALLOWED_OWNERS")
        clone_url, cname, tag = f"{canonical_github_repo_url(owner, repo)}.git", container_name_for(str(p_uuid), str(e_uuid)), image_tag_for(project_slug, environment_slug, str(p_uuid), str(e_uuid))
        docker_bin, git_bin = validate_binary(self.docker_bin, "docker_bin"), validate_binary(self.git_bin, "git_bin")
        runtime, network, docker_env = validate_runtime(self.docker_runtime), validate_network(self.docker_network), self._docker_env()
        emit = on_log or (lambda _m: None)
        route_hostname = route_hostname_for(project_slug, environment_slug, str(p_uuid), str(e_uuid), self.route_base_domain) if self.route_base_domain and self.route_registrar is not None else None
        addon_net, addon_env = provision_environment_addons(
            runner=self.runner, docker_bin=docker_bin, docker_env=docker_env,
            project_id=p_uuid, environment_id=e_uuid, addons=parse_addons_json(addons_json), on_log=on_log,
        )
        run_tag = tag
        if rollback_from_deployment_id:
            rollback_tag = deployment_image_tag_for(
                project_slug, environment_slug, str(p_uuid), str(e_uuid), rollback_from_deployment_id
            )
            emit(f"Rolling back to retained Docker image {rollback_tag}...")
            inspect_img = self.runner.run([docker_bin, "image", "inspect", rollback_tag], env=docker_env, timeout=15)
            if inspect_img.returncode != 0:
                raise PermanentJobError(
                    f"Rollback image {rollback_tag} is no longer available on this host; use Redeploy instead"
                )
            target_port = self.container_port
            with tempfile.TemporaryDirectory(prefix="aerodeck-rollback-") as tmp:
                env_file = build_runtime_env_file(
                    Path(tmp), target_port, f"https://{route_hostname}" if route_hostname else None,
                    on_log, env_vars_json, addon_env,
                )
            run_tag = rollback_tag
        else:
            deploy_tag = (
                deployment_image_tag_for(project_slug, environment_slug, str(p_uuid), str(e_uuid), image_deployment_id)
                if image_deployment_id
                else None
            )
            safe_ref = validate_git_ref(git_ref)
            with tempfile.TemporaryDirectory(prefix="aerodeck-build-") as tmp:
                checkout = Path(tmp) / "repo"
                emit(f"Cloning {owner}/{repo}{f' (branch {safe_ref})' if safe_ref else ' (shallow)'}...")
                clone_cmd = [git_bin, "clone", "--depth=1", "--single-branch"]
                if safe_ref:
                    clone_cmd.extend(["--branch", safe_ref])
                clone_cmd.extend(["--", clone_url, str(checkout)])
                clone_res = self.runner.run(
                    clone_cmd,
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout=120, on_line=on_log,
                )
                if clone_res.returncode != 0:
                    raise classify_failure("git clone", clone_res)
                df = ensure_dockerfile(checkout, owner, repo, container_port=self.container_port, on_log=on_log)
                target_port = detect_exposed_port(df, default_port=self.container_port)
                env_file = build_runtime_env_file(checkout, target_port, f"https://{route_hostname}" if route_hostname else None, on_log, env_vars_json, addon_env)
                emit(f"Building Docker image {tag}...")
                build_cmd = [docker_bin, "build", "--progress=plain", "--tag", tag]
                if deploy_tag:
                    build_cmd.extend(["--tag", deploy_tag])
                build_cmd.append(str(checkout))
                build_res = self.runner.run(
                    build_cmd,
                    cwd=checkout, env=self._docker_env({"DOCKER_BUILDKIT": "1"}), timeout=self.build_timeout_seconds, on_line=on_log,
                )
                if build_res.returncode != 0:
                    env_file.unlink(missing_ok=True)
                    raise classify_failure("docker build", build_res)

        host_port, candidate = self.port_allocator(self.bind_host, self.port_range_start, self.port_range_end), f"{cname}-new-{uuid.uuid4().hex[:6]}"
        run_cmd = build_docker_run_command(
            docker_bin=docker_bin, candidate=candidate, network=addon_net or network, memory_limit=self.memory_limit,
            cpus_limit=self.cpus_limit, pids_limit=self.pids_limit, project_id=p_uuid, environment_id=e_uuid,
            env_file=env_file, bind_host=self.bind_host, host_port=host_port, target_port=target_port, runtime=runtime, tag=run_tag,
        )
        emit(f"Starting candidate container {candidate} on {self.bind_host}:{host_port} (-> :{target_port})...")
        try:
            run_res = self.runner.run(run_cmd, env=docker_env, timeout=60)
        finally:
            env_file.unlink(missing_ok=True)
        if run_res.returncode != 0:
            raise classify_failure("docker run", run_res)

        emit(f"Waiting for health check on {self.bind_host}:{host_port} (timeout {self.health_timeout_seconds}s)...")
        healthy = (
            wait_for_container_or_fast_fail(
                self.bind_host, host_port, self.health_timeout_seconds,
                runner=self.runner, docker_bin=docker_bin, container_name=candidate, docker_env=docker_env,
            ) if self.health_checker is wait_for_tcp_health else self.health_checker(self.bind_host, host_port, self.health_timeout_seconds)
        )
        if not healthy:
            last = stream_container_crash_logs(self.runner, docker_bin, candidate, docker_env, on_log)
            self.runner.run([docker_bin, "rm", "-f", candidate], env=docker_env, timeout=30)
            raise PermanentJobError(
                f"container {candidate} failed health check on {self.bind_host}:{host_port}"
                f"{f' (last log: {last})' if last else ''}; rolled back and kept previous container live"
            )

        if route_hostname and self.route_registrar is not None:
            emit(f"Registering Traefik route https://{route_hostname} -> {self.bind_host}:{host_port}...")
            try:
                self.route_registrar(project_id=p_uuid, environment_id=e_uuid, hostname=route_hostname, backend_host=self.bind_host, backend_port=host_port)
            except Exception:
                self.runner.run([docker_bin, "rm", "-f", candidate], env=docker_env, timeout=30)
                raise

        self.runner.run([docker_bin, "rm", "-f", cname], env=docker_env, timeout=30)
        rename_res = self.runner.run([docker_bin, "rename", candidate, cname], env=docker_env, timeout=30)
        if rename_res.returncode != 0:
            raise classify_failure("docker rename", rename_res)
        loc = f"at https://{route_hostname} ({self.bind_host}:{host_port})" if route_hostname else f"on {self.bind_host}:{host_port}"
        emit(f"Container {cname} {'live ' if route_hostname else 'running '}{loc}.")
        return JobOutcome(kind=JobKind.provision, project_id=str(p_uuid), environment_id=str(e_uuid), changed=True, detail=f"deployed {tag} as {cname} {loc}")

    def reconcile(
        self, *, project_id: str, environment_id: str, project_slug: str = "", environment_slug: str = "",
        environment_kind: str = "dev", region: str = "ams3", repo_url: str = "", on_log: LogSink | None = None,
    ) -> JobOutcome:
        _ = (project_slug, environment_slug, environment_kind, region, repo_url, on_log)
        p_uuid, e_uuid = validate_uuid(project_id, "project_id"), validate_uuid(environment_id, "environment_id")
        cname, docker_bin, docker_env = container_name_for(str(p_uuid), str(e_uuid)), validate_binary(self.docker_bin, "docker_bin"), self._docker_env()
        inspect_res = self.runner.run([docker_bin, "inspect", "-f", "{{.State.Running}}", cname], env=docker_env, timeout=30)
        if inspect_res.returncode != 0:
            raise PermanentJobError(f"container {cname} does not exist; deploy first")
        if inspect_res.stdout.strip().lower() == "true":
            return JobOutcome(kind=JobKind.reconcile, project_id=str(p_uuid), environment_id=str(e_uuid), changed=False, detail=f"container {cname} is already running")
        start_res = self.runner.run([docker_bin, "start", cname], env=docker_env, timeout=60)
        if start_res.returncode != 0:
            raise classify_failure("docker start", start_res)
        return JobOutcome(kind=JobKind.reconcile, project_id=str(p_uuid), environment_id=str(e_uuid), changed=True, detail=f"restarted stopped container {cname}")
