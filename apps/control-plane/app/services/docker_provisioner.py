"""Build and run tenant containers directly via Docker (#209, #210, #215).

Dokploy-style deployment pipeline behind the ``Provisioner`` protocol:
1. Validate UUID identifiers, optional GitHub owner allowlist, and canonical ``repo_url``.
2. Shallow-clone the repository with ``GIT_TERMINAL_PROMPT=0`` and verify a root ``Dockerfile``.
3. Build with BuildKit, streaming sanitized output lines to ``on_log`` (Redis SSE).
4. Start an isolated container with ``--cap-drop=ALL``, ``no-new-privileges:true``,
   memory/CPU/PID caps, optional OCI ``--runtime`` (e.g. ``runsc`` / ``sysbox-runc``),
   and optional ``DOCKER_HOST`` (e.g. rootless Docker or dedicated build host).

No shell interpolation is ever used; every external command runs with an
explicit argument list via an injectable ``CommandRunner`` protocol.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.config import Settings
from app.core.github import canonical_github_repo_url, parse_github_repo
from app.services.provisioning import JobKind, JobOutcome, PermanentJobError, TransientJobError

LogSink = Callable[[str], None]
PortAllocator = Callable[[str, int, int], int]

_SAFE_SEGMENT_RE = re.compile(r"[^a-z0-9-]+")
_SAFE_BIN_RE = re.compile(r"^[A-Za-z0-9_./:-]+$")
_SAFE_RUNTIME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_MAX_LOG_LINE_LEN = 400
_MAX_ERROR_TAIL_LEN = 2000
_TRANSIENT_MARKERS = (
    "could not resolve host",
    "temporary failure",
    "connection reset",
    "timed out",
    "toomanyrequests",
    "429 too many requests",
    "cannot connect to the docker daemon",
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 600,
        on_line: LogSink | None = None,
    ) -> CommandResult: ...


class SubprocessCommandRunner:
    """Execute commands without a shell, optionally streaming merged output lines."""

    def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 600,
        on_line: LogSink | None = None,
    ) -> CommandResult:
        try:
            if on_line is None:
                completed = subprocess.run(
                    args,
                    cwd=cwd,
                    env=env,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                return CommandResult(completed.returncode, completed.stdout, completed.stderr)

            collected: list[str] = []
            proc = subprocess.Popen(
                args,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            try:
                assert proc.stdout is not None
                for raw_line in proc.stdout:
                    line = raw_line.rstrip("\r\n")
                    collected.append(line)
                    if line:
                        on_line(_sanitize_log_line(line))
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                proc.kill()
                proc.wait()
                raise TransientJobError(f"command timed out after {timeout}s: {args[0]}") from exc
            return CommandResult(proc.returncode, "\n".join(collected), "")
        except subprocess.TimeoutExpired as exc:
            raise TransientJobError(f"command timed out after {timeout}s: {args[0]}") from exc
        except OSError as exc:
            raise PermanentJobError(f"failed to execute {args[0]}: {exc}") from exc


def _sanitize_log_line(line: str) -> str:
    cleaned = line.replace("\x00", "").strip()
    if len(cleaned) > _MAX_LOG_LINE_LEN:
        return f"{cleaned[:_MAX_LOG_LINE_LEN]}..."
    return cleaned


def _validate_uuid(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise PermanentJobError(f"invalid UUID for {label}") from exc


def _validate_binary(binary: str, label: str) -> str:
    if not binary or not _SAFE_BIN_RE.fullmatch(binary):
        raise PermanentJobError(f"unsafe executable path for {label}")
    return binary


def _validate_runtime(runtime: str | None) -> str | None:
    if runtime is None:
        return None
    stripped = runtime.strip()
    if not stripped:
        return None
    if not _SAFE_RUNTIME_RE.fullmatch(stripped):
        raise PermanentJobError("invalid docker_runtime name")
    return stripped


def _slug_prefix(value: str, fallback: str) -> str:
    lowered = _SAFE_SEGMENT_RE.sub("-", value.lower()).strip("-")
    return (lowered or fallback)[:16].strip("-") or fallback


def container_name_for(project_id: str, environment_id: str) -> str:
    p_uuid = _validate_uuid(project_id, "project_id")
    e_uuid = _validate_uuid(environment_id, "environment_id")
    return f"aerodeck-app-{p_uuid.hex[:8]}-{e_uuid.hex[:8]}"


def image_tag_for(project_slug: str, environment_slug: str, project_id: str, environment_id: str) -> str:
    p_uuid = _validate_uuid(project_id, "project_id")
    e_uuid = _validate_uuid(environment_id, "environment_id")
    p_slug = _slug_prefix(project_slug, p_uuid.hex[:8])
    e_slug = _slug_prefix(environment_slug, e_uuid.hex[:8])
    return f"aerodeck/{p_slug}-{e_slug}:latest"


def _classify_failure(step: str, result: CommandResult) -> PermanentJobError | TransientJobError:
    blob = f"{result.stdout}\n{result.stderr}".strip()
    tail = blob[-_MAX_ERROR_TAIL_LEN:] if len(blob) > _MAX_ERROR_TAIL_LEN else blob
    lower = tail.lower()
    if any(marker in lower for marker in _TRANSIENT_MARKERS):
        return TransientJobError(f"{step} failed (exit {result.returncode}): {tail}")
    return PermanentJobError(f"{step} failed (exit {result.returncode}): {tail}")


def find_available_port(bind_host: str, start: int, end: int) -> int:
    """Return the first free TCP port on ``bind_host`` within ``[start, end]``."""
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((bind_host, port))
            except OSError:
                continue
            return port
    raise TransientJobError(f"no free host port available in range {start}-{end}")


@dataclass(frozen=True, slots=True)
class DockerProvisioner:
    """Clone a GitHub repo, build its Dockerfile, and run it as an isolated container."""

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
    allowed_owners: tuple[str, ...] = ()
    port_allocator: PortAllocator = find_available_port

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        runner: CommandRunner | None = None,
        port_allocator: PortAllocator | None = None,
    ) -> DockerProvisioner:
        return cls(
            docker_bin=_validate_binary(settings.docker_bin, "docker_bin"),
            git_bin=_validate_binary(settings.git_bin, "git_bin"),
            bind_host=settings.docker_bind_host,
            port_range_start=settings.docker_port_range_start,
            port_range_end=settings.docker_port_range_end,
            container_port=settings.docker_container_port,
            memory_limit=settings.docker_memory_limit,
            cpus_limit=settings.docker_cpus_limit,
            pids_limit=settings.docker_pids_limit,
            build_timeout_seconds=settings.docker_build_timeout_seconds,
            runner=runner or SubprocessCommandRunner(),
            docker_host=settings.docker_host,
            docker_runtime=_validate_runtime(settings.docker_runtime),
            allowed_owners=tuple(o.strip().lower() for o in settings.docker_allowed_owners if o.strip()),
            port_allocator=port_allocator or find_available_port,
        )

    def _docker_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        if self.docker_host:
            env["DOCKER_HOST"] = self.docker_host
        if extra:
            env.update(extra)
        return env

    def provision(
        self,
        *,
        project_id: str,
        environment_id: str,
        project_slug: str = "",
        environment_slug: str = "",
        environment_kind: str = "dev",
        region: str = "ams3",
        repo_url: str = "",
        on_log: LogSink | None = None,
    ) -> JobOutcome:
        _ = (environment_kind, region)
        p_uuid = _validate_uuid(project_id, "project_id")
        e_uuid = _validate_uuid(environment_id, "environment_id")
        parsed = parse_github_repo(repo_url)
        if parsed is None:
            raise PermanentJobError("A valid GitHub repo_url is required for Docker provisioning")
        owner, repo = parsed
        if self.allowed_owners and owner.lower() not in self.allowed_owners:
            raise PermanentJobError(
                f"Repository owner {owner!r} is not in AERODECK_DOCKER_ALLOWED_OWNERS"
            )
        clone_url = f"{canonical_github_repo_url(owner, repo)}.git"
        cname = container_name_for(str(p_uuid), str(e_uuid))
        tag = image_tag_for(project_slug, environment_slug, str(p_uuid), str(e_uuid))
        docker_bin = _validate_binary(self.docker_bin, "docker_bin")
        git_bin = _validate_binary(self.git_bin, "git_bin")
        runtime = _validate_runtime(self.docker_runtime)

        def emit(msg: str) -> None:
            if on_log is not None:
                on_log(msg)

        with tempfile.TemporaryDirectory(prefix="aerodeck-build-") as tmp:
            checkout = Path(tmp) / "repo"
            emit(f"Cloning {owner}/{repo} (shallow)...")
            clone_res = self.runner.run(
                [git_bin, "clone", "--depth=1", "--single-branch", "--", clone_url, str(checkout)],
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                timeout=120,
                on_line=on_log,
            )
            if clone_res.returncode != 0:
                raise _classify_failure("git clone", clone_res)

            dockerfile = checkout / "Dockerfile"
            if not dockerfile.is_file():
                raise PermanentJobError(
                    f"Repository {owner}/{repo} does not contain a root Dockerfile"
                )

            emit(f"Building Docker image {tag}...")
            build_res = self.runner.run(
                [docker_bin, "build", "--progress=plain", "--tag", tag, str(checkout)],
                cwd=checkout,
                env=self._docker_env({"DOCKER_BUILDKIT": "1"}),
                timeout=self.build_timeout_seconds,
                on_line=on_log,
            )
            if build_res.returncode != 0:
                raise _classify_failure("docker build", build_res)

        docker_env = self._docker_env()
        self.runner.run([docker_bin, "rm", "-f", cname], env=docker_env, timeout=30)
        host_port = self.port_allocator(self.bind_host, self.port_range_start, self.port_range_end)

        run_cmd = [
            docker_bin,
            "run",
            "-d",
            "--name",
            cname,
            "--restart",
            "unless-stopped",
            "--cap-drop=ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            self.memory_limit,
            "--cpus",
            self.cpus_limit,
            "--pids-limit",
            str(self.pids_limit),
            "--label",
            "aerodeck.managed=true",
            "--label",
            f"aerodeck.project_id={p_uuid}",
            "--label",
            f"aerodeck.environment_id={e_uuid}",
            "-p",
            f"{self.bind_host}:{host_port}:{self.container_port}",
        ]
        if runtime is not None:
            run_cmd.extend(["--runtime", runtime])
        run_cmd.append(tag)

        emit(f"Starting container {cname} on {self.bind_host}:{host_port}...")
        run_res = self.runner.run(run_cmd, env=docker_env, timeout=60)
        if run_res.returncode != 0:
            raise _classify_failure("docker run", run_res)

        emit(f"Container {cname} running on {self.bind_host}:{host_port}.")
        return JobOutcome(
            kind=JobKind.provision,
            project_id=str(p_uuid),
            environment_id=str(e_uuid),
            changed=True,
            detail=f"deployed {tag} as {cname} on {self.bind_host}:{host_port}",
        )

    def reconcile(
        self,
        *,
        project_id: str,
        environment_id: str,
        project_slug: str = "",
        environment_slug: str = "",
        environment_kind: str = "dev",
        region: str = "ams3",
        repo_url: str = "",
        on_log: LogSink | None = None,
    ) -> JobOutcome:
        _ = (project_slug, environment_slug, environment_kind, region, repo_url, on_log)
        p_uuid = _validate_uuid(project_id, "project_id")
        e_uuid = _validate_uuid(environment_id, "environment_id")
        cname = container_name_for(str(p_uuid), str(e_uuid))
        docker_bin = _validate_binary(self.docker_bin, "docker_bin")
        docker_env = self._docker_env()
        inspect_res = self.runner.run(
            [docker_bin, "inspect", "-f", "{{.State.Running}}", cname],
            env=docker_env,
            timeout=30,
        )
        if inspect_res.returncode != 0:
            raise PermanentJobError(f"container {cname} does not exist; deploy first")
        if inspect_res.stdout.strip().lower() == "true":
            return JobOutcome(
                kind=JobKind.reconcile,
                project_id=str(p_uuid),
                environment_id=str(e_uuid),
                changed=False,
                detail=f"container {cname} is already running",
            )
        start_res = self.runner.run([docker_bin, "start", cname], env=docker_env, timeout=60)
        if start_res.returncode != 0:
            raise _classify_failure("docker start", start_res)
        return JobOutcome(
            kind=JobKind.reconcile,
            project_id=str(p_uuid),
            environment_id=str(e_uuid),
            changed=True,
            detail=f"restarted stopped container {cname}",
        )
