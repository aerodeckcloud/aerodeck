"""Auto-discover runtime environment defaults from .env.example / docker-compose and fast-fail health checks (#231)."""

from __future__ import annotations

import json
import os
import re
import socket
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from app.services.docker_policy import CommandRunner

LogSink = Callable[[str], None]
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_MAX_DISCOVER_BYTES = 128 * 1024
_FORBIDDEN_ENV_KEYS = frozenset({
    "PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME",
    "NODE_OPTIONS", "DOCKER_HOST", "DOCKER_CONFIG", "HOME", "USER",
})


def _parse_dotenv_lines(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        k, v = [p.strip() for p in line.split("=", 1)]
        val = v.strip("'\"")
        if _ENV_KEY_RE.fullmatch(k) and k.upper() not in _FORBIDDEN_ENV_KEYS and "\n" not in val and "\x00" not in val:
            result[k] = val
    return result


def _parse_compose_build_service_env(text: str) -> dict[str, str]:
    """Extract ``environment:`` key/values from a ``docker-compose`` service that declares ``build:``."""
    services_env: list[tuple[bool, dict[str, str]]] = []
    has_build = False
    in_env = False
    env_indent = 0
    service_indent: int | None = None
    current: dict[str, str] = {}

    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        ind = len(raw) - len(raw.lstrip(" "))
        if ind == 2 and s.endswith(":") and not s.startswith("-"):
            if current or has_build:
                services_env.append((has_build, current))
            has_build, in_env, current, service_indent = False, False, {}, ind
            continue
        if service_indent is None:
            continue
        if ind == 4 and s.startswith("build:"):
            has_build, in_env = True, False
            continue
        if ind == 4 and s == "environment:":
            in_env, env_indent = True, ind
            continue
        if in_env and ind <= env_indent:
            in_env = False
        if in_env and ind > env_indent:
            item = s[1:].strip() if s.startswith("-") else s
            sep = "=" if "=" in item and (":" not in item or item.find("=") < item.find(":")) else (":" if ":" in item else "")
            if sep:
                k, v = item.split(sep, 1)
                key, val = k.strip(), v.strip().strip("'\"")
                if _ENV_KEY_RE.fullmatch(key) and key.upper() not in _FORBIDDEN_ENV_KEYS and "\x00" not in val:
                    current[key] = val

    if current or has_build:
        services_env.append((has_build, current))
    merged: dict[str, str] = {}
    for built, env_map in services_env:
        if built:
            merged.update(env_map)
    return merged


def _discover_mock_secret_files(checkout: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    root = checkout.resolve()
    for folder_name in ("mock-secrets", "secrets"):
        folder = checkout / folder_name
        if not folder.is_dir() or folder.is_symlink() or folder.resolve().parent != root:
            continue
        for entry in sorted(folder.iterdir()):
            if not entry.is_file() or entry.is_symlink() or entry.stat().st_size > 4096:
                continue
            env_key = re.sub(r"[^A-Za-z0-9]+", "_", entry.stem).strip("_").upper()
            val = entry.read_text(encoding="utf-8", errors="replace").strip()
            if _ENV_KEY_RE.fullmatch(env_key) and env_key not in _FORBIDDEN_ENV_KEYS and val and "\n" not in val and "\x00" not in val:
                out.setdefault(env_key, val)
    return out


def discover_repo_env_defaults(checkout: Path) -> dict[str, str]:
    defaults = _discover_mock_secret_files(checkout)
    for name in (".env.example", ".env.sample", ".env.template", ".env.defaults"):
        c = checkout / name
        if c.is_file() and not c.is_symlink() and c.stat().st_size <= _MAX_DISCOVER_BYTES:
            defaults.update(_parse_dotenv_lines(c.read_text(encoding="utf-8", errors="replace")))
    for name in ("docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml"):
        c = checkout / name
        if c.is_file() and not c.is_symlink() and c.stat().st_size <= _MAX_DISCOVER_BYTES:
            defaults.update(_parse_compose_build_service_env(c.read_text(encoding="utf-8", errors="replace")))
    return defaults


def parse_env_vars_json(raw: str | None) -> dict[str, str] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return {str(k): str(v) for k, v in parsed.items() if isinstance(k, str)} if isinstance(parsed, dict) else None


def build_runtime_env_file(
    checkout: Path, target_port: int, public_url: str | None = None,
    on_log: LogSink | None = None, user_env: dict[str, str] | str | None = None,
    addon_env: dict[str, str] | None = None,
) -> Path:
    """Discover repo env defaults, merge platform + addon + operator variables, and write a ``0600`` ``--env-file``."""
    env_vars = discover_repo_env_defaults(checkout)
    if env_vars and on_log is not None:
        keys = ", ".join(sorted(env_vars.keys()))
        on_log(f"Auto-loaded {len(env_vars)} runtime env default(s) from repository ({keys}).")
    env_vars["PORT"] = str(target_port)
    env_vars.setdefault("HOST", "0.0.0.0")
    if public_url:
        env_vars["AERODECK_PUBLIC_URL"] = public_url
        env_vars.setdefault("PUBLIC_URL", public_url)
        env_vars["PORTAL_PUBLIC_URL"] = public_url
    if addon_env:
        env_vars.update(addon_env)
    resolved_user_env = parse_env_vars_json(user_env) if isinstance(user_env, str) else user_env
    if resolved_user_env:
        applied: list[str] = []
        for raw_k, raw_v in resolved_user_env.items():
            k = raw_k.strip()
            v = str(raw_v).strip()
            if _ENV_KEY_RE.fullmatch(k) and k.upper() not in _FORBIDDEN_ENV_KEYS and "\n" not in v and "\r" not in v and "\x00" not in v:
                env_vars[k] = v
                applied.append(k)
        if applied and on_log is not None:
            on_log(f"Applied {len(applied)} operator-supplied env variable(s) ({', '.join(sorted(applied))}).")
    return write_secure_env_file(env_vars)


def write_secure_env_file(env_vars: dict[str, str]) -> Path:
    """Write ``KEY=VALUE`` lines to a ``0600`` temporary file for ``docker run --env-file``."""
    fd, raw_path = tempfile.mkstemp(prefix="aerodeck-env-", suffix=".env")
    path = Path(raw_path)
    lines = [f"{k}={v}" for k, v in sorted(env_vars.items()) if _ENV_KEY_RE.fullmatch(k) and "\n" not in v and "\r" not in v]
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + ("\n" if lines else ""))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def build_docker_run_command(
    *, docker_bin: str, candidate: str, network: str, memory_limit: str, cpus_limit: str, pids_limit: int,
    project_id: object, environment_id: object, env_file: Path, bind_host: str, host_port: int, target_port: int,
    runtime: str | None, tag: str,
) -> list[str]:
    """Assemble hardened ``docker run`` arguments for a tenant candidate container."""
    cmd = [
        docker_bin, "run", "-d", "--name", candidate, "--restart", "unless-stopped",
        "--network", network, "--cap-drop=ALL", "--security-opt", "no-new-privileges:true",
        "--memory", memory_limit, "--cpus", cpus_limit, "--pids-limit", str(pids_limit),
        "--log-driver", "json-file", "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
        "--label", "aerodeck.managed=true", "--label", f"aerodeck.project_id={project_id}",
        "--label", f"aerodeck.environment_id={environment_id}", "--env-file", str(env_file),
        "-p", f"{bind_host}:{host_port}:{target_port}",
    ]
    if runtime is not None:
        cmd.extend(["--runtime", runtime])
    cmd.append(tag)
    return cmd


def ensure_default_docker_config(env: dict[str, str], docker_host: str | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Ensure ``DOCKER_CONFIG`` points to a writable directory and apply ``DOCKER_HOST``."""
    out = dict(env)
    if not out.get("DOCKER_CONFIG"):
        cfg_dir = Path(tempfile.gettempdir()) / "aerodeck-docker-config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        out["DOCKER_CONFIG"] = str(cfg_dir)
    if docker_host:
        out["DOCKER_HOST"] = docker_host
    if extra:
        out.update(extra)
    return out


def wait_for_container_or_fast_fail(
    bind_host: str, port: int, timeout_seconds: int, *,
    runner: CommandRunner, docker_bin: str, container_name: str, docker_env: dict[str, str],
) -> bool:
    """Poll TCP connectivity while checking ``docker inspect`` so crashed containers fail immediately (#231)."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((bind_host, port), timeout=1.0):
                return True
        except OSError:
            pass
        inspect_res = runner.run(
            [docker_bin, "inspect", "-f", "{{.State.Running}} {{.RestartCount}}", container_name],
            env=docker_env,
            timeout=10,
        )
        if inspect_res.returncode == 0:
            parts = inspect_res.stdout.strip().split()
            running = parts[0].lower() == "true" if parts else True
            restarts = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            if not running or restarts > 0:
                return False
        time.sleep(0.5)
    return False
