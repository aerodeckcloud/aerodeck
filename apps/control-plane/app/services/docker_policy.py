"""Validation, naming, port allocation, and TCP health helpers for DockerProvisioner (#209, #212, #215)."""

from __future__ import annotations

import re
import socket
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.services.provisioning import PermanentJobError, TransientJobError

LogSink = Callable[[str], None]
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
                    args, cwd=cwd, env=env, check=False, capture_output=True, text=True, timeout=timeout
                )
                return CommandResult(completed.returncode, completed.stdout, completed.stderr)

            collected: list[str] = []
            proc = subprocess.Popen(
                args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
            )
            try:
                assert proc.stdout is not None
                for raw_line in proc.stdout:
                    line = raw_line.rstrip("\r\n")
                    collected.append(line)
                    if line:
                        on_line(sanitize_log_line(line))
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


def sanitize_log_line(line: str) -> str:
    cleaned = line.replace("\x00", "").strip()
    return f"{cleaned[:_MAX_LOG_LINE_LEN]}..." if len(cleaned) > _MAX_LOG_LINE_LEN else cleaned


def classify_failure(step: str, result: CommandResult) -> PermanentJobError | TransientJobError:
    blob = f"{result.stdout}\n{result.stderr}".strip()
    tail = blob[-_MAX_ERROR_TAIL_LEN:] if len(blob) > _MAX_ERROR_TAIL_LEN else blob
    if any(marker in tail.lower() for marker in _TRANSIENT_MARKERS):
        return TransientJobError(f"{step} failed (exit {result.returncode}): {tail}")
    return PermanentJobError(f"{step} failed (exit {result.returncode}): {tail}")

_SAFE_SEGMENT_RE = re.compile(r"[^a-z0-9-]+")
_SAFE_BIN_RE = re.compile(r"^[A-Za-z0-9_./:-]+$")
_SAFE_RUNTIME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SAFE_GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_SAFE_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_MAX_DOCKERFILE_BYTES = 64 * 1024
_FORBIDDEN_DOCKERFILE_PATTERNS = (
    "--mount=type=ssh",
    "--network=host",
    "--security-opt",
)


def validate_uuid(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise PermanentJobError(f"invalid UUID for {label}") from exc


def validate_binary(binary: str, label: str) -> str:
    if not binary or not _SAFE_BIN_RE.fullmatch(binary):
        raise PermanentJobError(f"unsafe executable path for {label}")
    return binary


def validate_git_ref(git_ref: str | None) -> str | None:
    if not git_ref:
        return None
    cleaned = git_ref.strip()
    if not cleaned:
        return None
    if ".." in cleaned or cleaned.endswith("/") or not _SAFE_GIT_REF_RE.fullmatch(cleaned):
        raise PermanentJobError("invalid git_ref branch name")
    return cleaned


def validate_runtime(runtime: str | None) -> str | None:
    if runtime is None:
        return None
    stripped = runtime.strip()
    if not stripped:
        return None
    if not _SAFE_RUNTIME_RE.fullmatch(stripped):
        raise PermanentJobError("invalid docker_runtime name")
    return stripped


def validate_network(network: str) -> str:
    stripped = network.strip()
    if (
        not stripped
        or not _SAFE_RUNTIME_RE.fullmatch(stripped)
        or stripped.lower() == "host"
        or stripped.lower().startswith("container:")
    ):
        raise PermanentJobError("unsafe docker_network; host networking is forbidden for tenant containers")
    return stripped


def validate_dockerfile(checkout: Path, owner: str, repo: str) -> Path:
    """Ensure root Dockerfile exists, is not a symlink escaping checkout, and uses no forbidden flags."""
    dockerfile = checkout / "Dockerfile"
    if dockerfile.is_symlink():
        raise PermanentJobError(f"Repository {owner}/{repo} Dockerfile must not be a symlink")
    if not dockerfile.is_file():
        raise PermanentJobError(f"Repository {owner}/{repo} does not contain a root Dockerfile")
    if dockerfile.resolve().parent != checkout.resolve():
        raise PermanentJobError(f"Repository {owner}/{repo} Dockerfile path escapes checkout directory")
    if dockerfile.stat().st_size > _MAX_DOCKERFILE_BYTES:
        raise PermanentJobError(f"Repository {owner}/{repo} Dockerfile exceeds 64 KiB limit")
    content = dockerfile.read_text(encoding="utf-8", errors="replace").lower()
    for forbidden in _FORBIDDEN_DOCKERFILE_PATTERNS:
        if forbidden in content:
            raise PermanentJobError(
                f"Repository {owner}/{repo} Dockerfile contains forbidden directive {forbidden!r}"
            )
    return dockerfile


def _slug_prefix(value: str, fallback: str) -> str:
    lowered = _SAFE_SEGMENT_RE.sub("-", value.lower()).strip("-")
    return (lowered or fallback)[:16].strip("-") or fallback


def container_name_for(project_id: str, environment_id: str) -> str:
    p_uuid = validate_uuid(project_id, "project_id")
    e_uuid = validate_uuid(environment_id, "environment_id")
    return f"aerodeck-app-{p_uuid.hex[:8]}-{e_uuid.hex[:8]}"


def image_tag_for(project_slug: str, environment_slug: str, project_id: str, environment_id: str) -> str:
    p_uuid = validate_uuid(project_id, "project_id")
    e_uuid = validate_uuid(environment_id, "environment_id")
    p_slug = _slug_prefix(project_slug, p_uuid.hex[:8])
    e_slug = _slug_prefix(environment_slug, e_uuid.hex[:8])
    return f"aerodeck/{p_slug}-{e_slug}:latest"


def deployment_image_tag_for(
    project_slug: str,
    environment_slug: str,
    project_id: str,
    environment_id: str,
    deployment_id: str,
) -> str:
    """Immutable per-deployment local image tag used for 1-click rollbacks (#253)."""
    d_uuid = validate_uuid(deployment_id, "deployment_id")
    base = image_tag_for(project_slug, environment_slug, project_id, environment_id)
    repo_name = base.rsplit(":", 1)[0]
    return f"{repo_name}:{d_uuid.hex[:12]}"


def route_hostname_for(
    project_slug: str,
    environment_slug: str,
    project_id: str,
    environment_id: str,
    base_domain: str,
) -> str:
    domain = base_domain.strip().lower().strip(".")
    if not _SAFE_DOMAIN_RE.fullmatch(domain):
        raise PermanentJobError("invalid AERODECK_DOCKER_ROUTE_BASE_DOMAIN")
    p_uuid = validate_uuid(project_id, "project_id")
    e_uuid = validate_uuid(environment_id, "environment_id")
    p_slug = _slug_prefix(project_slug, p_uuid.hex[:8])
    e_slug = _slug_prefix(environment_slug, e_uuid.hex[:8])
    label = f"{p_slug}-{e_slug}"[:63].strip("-")
    return f"{label}.{domain}"


def wait_for_tcp_health(bind_host: str, port: int, timeout_seconds: int) -> bool:
    """Poll TCP connectivity on ``bind_host:port`` until open or ``timeout_seconds`` elapses."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((bind_host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.5)
    return False


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
