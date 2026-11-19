"""Build and run tenant containers directly via Docker (#209, #210).

Dokploy-style deployment pipeline behind the ``Provisioner`` protocol:
1. Validate and shallow-clone the deployment's canonical GitHub ``repo_url``.
2. Verify a ``Dockerfile`` is present and build with BuildKit, streaming output
   lines to an optional ``on_log`` sink (Redis SSE deployment terminal).
3. Start an isolated container with memory, CPU, PID, and privilege limits
   bound to the configured host interface (e.g. private VPC IP).

No shell interpolation is ever used; every external command runs with an
explicit argument list via an injectable ``CommandRunner`` protocol.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.config import Settings
from app.core.github import canonical_github_repo_url, parse_github_repo
from app.services.provisioning import JobKind, JobOutcome, PermanentJobError, TransientJobError

LogSink = Callable[[str], None]

_SAFE_SEGMENT_RE = re.compile(r"[^a-z0-9-]+")
_MAX_LOG_LINE_LEN = 400
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


def _sanitize_log_line(line: str) -> str:
    cleaned = line.replace("\x00", "").strip()
    if len(cleaned) > _MAX_LOG_LINE_LEN:
        return f"{cleaned[:_MAX_LOG_LINE_LEN]}..."
    return cleaned


def _slug_prefix(value: str, fallback: str) -> str:
    lowered = _SAFE_SEGMENT_RE.sub("-", value.lower()).strip("-")
    return (lowered or fallback)[:16].strip("-") or fallback


def container_name_for(project_id: str, environment_id: str) -> str:
    p = _SAFE_SEGMENT_RE.sub("", project_id.lower())[:8]
    e = _SAFE_SEGMENT_RE.sub("", environment_id.lower())[:8]
    if not p or not e:
        raise PermanentJobError("invalid project_id or environment_id for container name")
    return f"aerodeck-app-{p}-{e}"


def image_tag_for(project_slug: str, environment_slug: str, project_id: str, environment_id: str) -> str:
    p_slug = _slug_prefix(project_slug, project_id[:8].lower())
    e_slug = _slug_prefix(environment_slug, environment_id[:8].lower())
    return f"aerodeck/{p_slug}-{e_slug}:latest"


def _classify_failure(step: str, result: CommandResult) -> Exception:
    blob = f"{result.stdout}\n{result.stderr}".strip()
    lower = blob.lower()
    if any(marker in lower for marker in _TRANSIENT_MARKERS):
        return TransientJobError(f"{step} failed: {blob[-1000:]}")
    return PermanentJobError(f"{step} failed (exit {result.returncode}): {blob[-1000:]}")


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
    port_allocator: Callable[[str, int, int], int] = find_available_port

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        runner: CommandRunner | None = None,
        port_allocator: Callable[[str, int, int], int] | None = None,
    ) -> DockerProvisioner:
        return cls(
            docker_bin=settings.docker_bin,
            git_bin=settings.git_bin,
            bind_host=settings.docker_bind_host,
            port_range_start=settings.docker_port_range_start,
            port_range_end=settings.docker_port_range_end,
            container_port=settings.docker_container_port,
            memory_limit=settings.docker_memory_limit,
            cpus_limit=settings.docker_cpus_limit,
            pids_limit=settings.docker_pids_limit,
            build_timeout_seconds=settings.docker_build_timeout_seconds,
            runner=runner or SubprocessCommandRunner(),
            port_allocator=port_allocator or find_available_port,
        )

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
        parsed = parse_github_repo(repo_url)
        if parsed is None:
            raise PermanentJobError("A valid GitHub repo_url is required for Docker provisioning")
        owner, repo = parsed
        clone_url = f"{canonical_github_repo_url(owner, repo)}.git"
        cname = container_name_for(project_id, environment_id)
        tag = image_tag_for(project_slug, environment_slug, project_id, environment_id)

        def emit(msg: str) -> None:
            if on_log is not None:
                on_log(msg)

        with tempfile.TemporaryDirectory(prefix="aerodeck-build-") as tmp:
            checkout = Path(tmp) / "repo"
            emit(f"Cloning {owner}/{repo} (shallow)...")
            clone_res = self.runner.run(
                [self.git_bin, "clone", "--depth=1", "--single-branch", "--", clone_url, str(checkout)],
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
                [self.docker_bin, "build", "--progress=plain", "--tag", tag, str(checkout)],
                cwd=checkout,
                env={**os.environ, "DOCKER_BUILDKIT": "1"},
                timeout=self.build_timeout_seconds,
                on_line=on_log,
            )
            if build_res.returncode != 0:
                raise _classify_failure("docker build", build_res)

        # Remove previous instance (if any) before binding the new container.
        self.runner.run([self.docker_bin, "rm", "-f", cname], timeout=30)
        host_port = self.port_allocator(self.bind_host, self.port_range_start, self.port_range_end)

        emit(f"Starting container {cname} on {self.bind_host}:{host_port}...")
        run_res = self.runner.run(
            [
                self.docker_bin,
                "run",
                "-d",
                "--name",
                cname,
                "--restart",
                "unless-stopped",
                "--memory",
                self.memory_limit,
                "--cpus",
                self.cpus_limit,
                "--pids-limit",
                str(self.pids_limit),
                "--security-opt",
                "no-new-privileges:true",
                "--label",
                "aerodeck.managed=true",
                "--label",
                f"aerodeck.project_id={project_id}",
                "--label",
                f"aerodeck.environment_id={environment_id}",
                "-p",
                f"{self.bind_host}:{host_port}:{self.container_port}",
                tag,
            ],
            timeout=60,
        )
        if run_res.returncode != 0:
            raise _classify_failure("docker run", run_res)

        emit(f"Container {cname} running on {self.bind_host}:{host_port}.")
        return JobOutcome(
            kind=JobKind.provision,
            project_id=project_id,
            environment_id=environment_id,
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
        cname = container_name_for(project_id, environment_id)
        inspect_res = self.runner.run(
            [self.docker_bin, "inspect", "-f", "{{.State.Running}}", cname],
            timeout=30,
        )
        if inspect_res.returncode != 0:
            raise PermanentJobError(f"container {cname} does not exist; deploy first")
        if inspect_res.stdout.strip().lower() == "true":
            return JobOutcome(
                kind=JobKind.reconcile,
                project_id=project_id,
                environment_id=environment_id,
                changed=False,
                detail=f"container {cname} is already running",
            )
        start_res = self.runner.run([self.docker_bin, "start", cname], timeout=60)
        if start_res.returncode != 0:
            raise _classify_failure("docker start", start_res)
        return JobOutcome(
            kind=JobKind.reconcile,
            project_id=project_id,
            environment_id=environment_id,
            changed=True,
            detail=f"restarted stopped container {cname}",
        )
