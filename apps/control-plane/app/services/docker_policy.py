"""Validation, naming, port allocation, and TCP health helpers for DockerProvisioner (#209, #212, #215)."""

from __future__ import annotations

import queue
import re
import shlex
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.services.provisioning import PermanentJobError, TransientJobError


class CommandTimeoutError(TransientJobError):
    """The process was killed because it passed its deadline."""

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
            # The deadline covers the read, not only the wait after stdout closes.
            # A hung docker exec that keeps the pipe open must still be killed (#275).
            _collect_stream(proc, on_line, collected, timeout)
            return CommandResult(proc.returncode, "\n".join(collected), "")
        except subprocess.TimeoutExpired as exc:
            raise CommandTimeoutError(f"command timed out after {timeout}s: {args[0]}") from exc
        except OSError as exc:
            raise PermanentJobError(f"failed to execute {args[0]}: {exc}") from exc


def _pump_stdout(proc: subprocess.Popen[str], lines: queue.Queue[str | None]) -> None:
    """Copy stdout onto a queue so the caller can stop waiting at a deadline."""
    try:
        stdout = proc.stdout
        if stdout is None:
            return
        for raw_line in stdout:
            lines.put(raw_line)
    except (OSError, ValueError):
        return
    finally:
        lines.put(None)


def _collect_stream(
    proc: subprocess.Popen[str], on_line: LogSink, collected: list[str], timeout: int
) -> None:
    """Read merged output until the process exits or the wall-clock deadline passes."""
    lines: queue.Queue[str | None] = queue.Queue()
    reader = threading.Thread(target=_pump_stdout, args=(proc, lines), daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(proc.args, timeout)
            try:
                raw_line = lines.get(timeout=remaining)
            except queue.Empty:
                raise subprocess.TimeoutExpired(proc.args, timeout) from None
            if raw_line is None:
                break
            line = raw_line.rstrip("\r\n")
            collected.append(line)
            if line:
                on_line(sanitize_log_line(line))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        proc.wait(timeout=remaining)
    except BaseException:
        _stop_process(proc)
        raise
    finally:
        reader.join(timeout=2)


def _stop_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    stdout = proc.stdout
    if stdout is not None:
        try:
            stdout.close()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.wait()


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


RELEASE_COMMAND_MAX_LENGTH = 500
_SHELL_CHARS = frozenset(";|&<>`")
_RELEASE_COMMAND_REJECTED = "release command must be an executable and arguments, not a shell script"


def _unquoted_shell_syntax(command: str) -> bool:
    """True when a character would be interpreted by a shell outside quotes."""
    quote: str | None = None
    escaped = False
    for char in command:
        if escaped:
            escaped = False
            continue
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char == "\\":
            escaped = True
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char in _SHELL_CHARS:
            return True
    return quote is not None


def parse_release_command(raw: str | None) -> list[str]:
    """Split an operator command into argv. An empty command runs nothing.

    The result is passed to ``docker exec`` as a list. A shell is never started,
    and tokens that only work in a shell are rejected (#275).
    """
    if raw is None:
        return []
    command = raw.strip()
    if not command:
        return []
    if len(command) > RELEASE_COMMAND_MAX_LENGTH:
        raise ValueError(
            f"release command must be at most {RELEASE_COMMAND_MAX_LENGTH} characters"
        )
    if any(char in command for char in "\n\r\x00"):
        raise ValueError(_RELEASE_COMMAND_REJECTED)
    if _unquoted_shell_syntax(command):
        raise ValueError(_RELEASE_COMMAND_REJECTED)
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ValueError(_RELEASE_COMMAND_REJECTED) from exc
    if not argv or any("\x00" in token for token in argv):
        raise ValueError(_RELEASE_COMMAND_REJECTED)
    return argv


def normalize_git_sha(raw: str | None) -> str | None:
    """Return a 40-char lowercase hex SHA, or None when the value is not one."""
    if raw is None:
        return None
    cleaned = raw.strip().lower()
    if len(cleaned) == 40 and all(character in "0123456789abcdef" for character in cleaned):
        return cleaned
    return None


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
