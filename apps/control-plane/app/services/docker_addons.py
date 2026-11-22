"""Provision containerized PostgreSQL and Redis addons for tenant environments (#194, #237)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from collections.abc import Callable

from app.services.docker_policy import CommandRunner, classify_failure, validate_uuid
from app.services.provisioning import PermanentJobError, TransientJobError

LogSink = Callable[[str], None]
_ALLOWED_ADDONS = frozenset({"postgres", "redis"})
_ADDON_READY_TIMEOUT_SECONDS = 30


def _addon_password(project_id: uuid.UUID, environment_id: uuid.UUID, addon_type: str) -> str:
    """Derive a per-environment addon password via HMAC-SHA256 keyed by AERODECK_AUTH_SIGNING_KEY."""
    key_str = os.environ.get("AERODECK_AUTH_SIGNING_KEY", "").strip()
    if not key_str:
        raise PermanentJobError("AERODECK_AUTH_SIGNING_KEY is required to derive addon credentials")
    msg = f"{project_id}:{environment_id}:{addon_type}".encode("utf-8")
    return hmac.new(key_str.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]


def _wait_for_addon_ready(
    runner: CommandRunner,
    docker_bin: str,
    docker_env: dict[str, str],
    cname: str,
    probe_cmd: list[str],
    timeout_seconds: int = _ADDON_READY_TIMEOUT_SECONDS,
) -> None:
    """Wait until the addon container accepts connections before booting the app container (#237)."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        res = runner.run([docker_bin, "exec", cname, *probe_cmd], env=docker_env, timeout=10)
        if res.returncode == 0:
            return
        time.sleep(0.5)
    raise TransientJobError(f"addon container {cname} did not become ready within {timeout_seconds}s")


def parse_addons_json(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    seen: list[str] = []
    for item in parsed:
        name = str(item).strip().lower()
        if name in _ALLOWED_ADDONS and name not in seen:
            seen.append(name)
    return seen


def tenant_network_name(project_id: str, environment_id: str) -> str:
    p_uuid = validate_uuid(project_id, "project_id")
    e_uuid = validate_uuid(environment_id, "environment_id")
    return f"aerodeck-net-{p_uuid.hex[:8]}-{e_uuid.hex[:8]}"


def ensure_tenant_network(
    runner: CommandRunner,
    docker_bin: str,
    network_name: str,
    docker_env: dict[str, str],
) -> None:
    inspect_res = runner.run([docker_bin, "network", "inspect", network_name], env=docker_env, timeout=15)
    if inspect_res.returncode == 0:
        return
    create_res = runner.run([docker_bin, "network", "create", "--driver", "bridge", network_name], env=docker_env, timeout=30)
    if create_res.returncode == 0:
        return
    # Handle concurrent creation race: if another worker just created the network, inspect succeeds now.
    if runner.run([docker_bin, "network", "inspect", network_name], env=docker_env, timeout=15).returncode == 0:
        return
    raise classify_failure("docker network create", create_res)


def provision_environment_addons(
    *,
    runner: CommandRunner,
    docker_bin: str,
    docker_env: dict[str, str],
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    addons: list[str],
    on_log: LogSink | None = None,
) -> tuple[str | None, dict[str, str]]:
    """Start requested ``postgres`` / ``redis`` addon containers and return ``(network_name, injected_env)`` (#194, #237)."""
    valid = [a for a in addons if a in _ALLOWED_ADDONS]
    if not valid:
        return None, {}
    net_name = tenant_network_name(str(project_id), str(environment_id))
    ensure_tenant_network(runner, docker_bin, net_name, docker_env)
    injected: dict[str, str] = {}

    for addon in valid:
        if addon not in _ALLOWED_ADDONS:
            raise PermanentJobError(f"unsupported addon type {addon!r}")
        cname = f"aerodeck-addon-{project_id.hex[:8]}-{environment_id.hex[:8]}-{addon}"
        pwd = _addon_password(project_id, environment_id, addon)
        inspect_res = runner.run(
            [docker_bin, "inspect", "-f", "{{.State.Running}}", cname],
            env=docker_env,
            timeout=15,
        )
        is_running = inspect_res.returncode == 0 and inspect_res.stdout.strip().lower() == "true"

        if addon == "postgres":
            if not is_running:
                if on_log is not None:
                    on_log(f"Provisioning PostgreSQL 16 addon container ({cname})...")
                runner.run([docker_bin, "rm", "-f", cname], env=docker_env, timeout=15)
                run_res = runner.run(
                    [
                        docker_bin, "run", "-d", "--name", cname, "--restart", "unless-stopped",
                        "--network", net_name, "--network-alias", "postgres",
                        "--security-opt", "no-new-privileges:true",
                        "--memory", "256m", "--cpus", "0.5", "--pids-limit", "128",
                        "--log-driver", "json-file", "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
                        "--label", "aerodeck.managed=true",
                        "--label", "aerodeck.addon=postgres",
                        "--label", f"aerodeck.project_id={project_id}",
                        "--label", f"aerodeck.environment_id={environment_id}",
                        "-e", "POSTGRES_USER=aerodeck",
                        "-e", f"POSTGRES_PASSWORD={pwd}",
                        "-e", "POSTGRES_DB=app",
                        "postgres:16-alpine",
                    ],
                    env=docker_env,
                    timeout=90,
                )
                if run_res.returncode != 0:
                    raise classify_failure("docker run postgres addon", run_res)
                _wait_for_addon_ready(
                    runner, docker_bin, docker_env, cname,
                    ["pg_isready", "-U", "aerodeck", "-d", "app"],
                )
            db_url = f"postgresql://aerodeck:{pwd}@{cname}:5432/app"
            injected["DATABASE_URL"] = db_url
            injected["POSTGRES_URL"] = db_url
            if on_log is not None:
                on_log(f"Attached PostgreSQL addon ({cname}:5432) -> injected DATABASE_URL.")

        elif addon == "redis":
            if not is_running:
                if on_log is not None:
                    on_log(f"Provisioning Redis 7 addon container ({cname})...")
                runner.run([docker_bin, "rm", "-f", cname], env=docker_env, timeout=15)
                run_res = runner.run(
                    [
                        docker_bin, "run", "-d", "--name", cname, "--restart", "unless-stopped",
                        "--network", net_name, "--network-alias", "redis",
                        "--cap-drop=ALL", "--security-opt", "no-new-privileges:true",
                        "--memory", "128m", "--cpus", "0.25", "--pids-limit", "64",
                        "--log-driver", "json-file", "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
                        "--label", "aerodeck.managed=true",
                        "--label", "aerodeck.addon=redis",
                        "--label", f"aerodeck.project_id={project_id}",
                        "--label", f"aerodeck.environment_id={environment_id}",
                        "redis:7-alpine", "redis-server", "--requirepass", pwd,
                    ],
                    env=docker_env,
                    timeout=90,
                )
                if run_res.returncode != 0:
                    raise classify_failure("docker run redis addon", run_res)
                _wait_for_addon_ready(
                    runner, docker_bin, docker_env, cname,
                    ["redis-cli", "-a", pwd, "ping"],
                )
            redis_url = f"redis://:{pwd}@{cname}:6379/0"
            injected["REDIS_URL"] = redis_url
            if on_log is not None:
                on_log(f"Attached Redis addon ({cname}:6379) -> injected REDIS_URL.")

    return net_name, injected
