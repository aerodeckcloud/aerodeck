"""Zero-config language detection and fallback Dockerfile synthesis (#217, #193).

When a repository cloned by ``DockerProvisioner`` does not contain a root
``Dockerfile``, ``ensure_dockerfile`` inspects top-level project manifests
(``package.json``, ``requirements.txt`` / ``pyproject.toml``, ``go.mod``, or
``index.html``) and writes a hardened, non-root ``Dockerfile`` listening on
``container_port`` before running ``validate_dockerfile``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from app.services.docker_env_discovery import (
    build_runtime_env_file,
    discover_repo_env_defaults,
    wait_for_container_or_fast_fail,
    write_secure_env_file,
)
from app.services.docker_policy import CommandRunner, sanitize_log_line, validate_dockerfile
from app.services.provisioning import PermanentJobError

__all__ = [
    "build_runtime_env_file",
    "detect_dockerfile_template",
    "detect_exposed_port",
    "discover_repo_env_defaults",
    "ensure_dockerfile",
    "stream_container_crash_logs",
    "wait_for_container_or_fast_fail",
    "write_secure_env_file",
]

LogSink = Callable[[str], None]
_EXPOSE_RE = re.compile(r"^\s*EXPOSE\s+(\d+)(?:/tcp)?\b", re.IGNORECASE | re.MULTILINE)
_PACKAGE_JSON_BYTES = 512 * 1024
_PNPM_VERSION = "9.15.9"
_YARN_CLASSIC_VERSION = "1.22.22"
_YARN_BERRY_VERSION = "4.6.0"
_MISSING_START = "package.json is missing scripts.start"


class DockerfileTemplate(NamedTuple):
    label: str
    contents: str
    runtime: str | None = None
    marker: str | None = None
    error: str | None = None


def _package_json(checkout: Path) -> dict[str, Any] | None:
    path = checkout / "package.json"
    if not path.is_file() or path.is_symlink() or path.stat().st_size > _PACKAGE_JSON_BYTES:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _script(package: dict[str, Any], name: str) -> str | None:
    scripts = package.get("scripts")
    if not isinstance(scripts, dict):
        return None
    value = scripts.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _regular_file(checkout: Path, name: str) -> bool:
    path = checkout / name
    return path.is_file() and not path.is_symlink()


def _yarn_classic(checkout: Path) -> bool:
    text = (checkout / "yarn.lock").read_text(encoding="utf-8", errors="replace")[:4096]
    return "yarn lockfile v1" in text


def _node_dockerfile(checkout: Path, package: dict[str, Any], port: int) -> str:
    """Install with the lockfile's package manager, then run start as the node user."""
    build = _script(package, "build") is not None
    lines = ["FROM node:22-alpine", "WORKDIR /app"]
    if _regular_file(checkout, "pnpm-lock.yaml"):
        lines.append(f"RUN npm install -g pnpm@{_PNPM_VERSION}")
        lines.append("COPY package.json pnpm-lock.yaml ./")
        install = "pnpm install --frozen-lockfile" if build else "pnpm install --frozen-lockfile --prod"
        lines.append(f"RUN {install}")
        build_line = "RUN pnpm run build"
        start = 'CMD ["pnpm", "start"]'
    elif _regular_file(checkout, "yarn.lock"):
        if _yarn_classic(checkout):
            lines.append(f"RUN npm install -g yarn@{_YARN_CLASSIC_VERSION}")
            install = "yarn install --frozen-lockfile" if build else "yarn install --frozen-lockfile --production"
        else:
            lines.append(f"RUN corepack enable && corepack prepare yarn@{_YARN_BERRY_VERSION} --activate")
            install = "yarn install --immutable"
        lines.append("COPY package.json yarn.lock ./")
        lines.append(f"RUN {install}")
        build_line = "RUN yarn run build"
        start = 'CMD ["yarn", "start"]'
    else:
        lines.append("COPY package*.json ./")
        install = "npm install --no-audit --no-fund" if build else "npm install --omit=dev --no-audit --no-fund"
        lines.append(f"RUN {install}")
        build_line = "RUN npm run build"
        start = 'CMD ["npm", "start"]'
    lines.append("COPY . .")
    if build:
        lines.append(build_line)
    lines.extend(
        [
            "RUN chown -R node:node /app",
            f"ENV PORT={port} HOST=0.0.0.0",
            f"EXPOSE {port}",
            "USER node",
            start,
        ]
    )
    return "\n".join(lines) + "\n"


def detect_exposed_port(dockerfile: Path, default_port: int = 8000) -> int:
    """Return the first valid TCP ``EXPOSE`` port in ``dockerfile``, or ``default_port``."""
    try:
        content = dockerfile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default_port
    match = _EXPOSE_RE.search(content)
    if match:
        port = int(match.group(1))
        if 1 <= port <= 65535:
            return port
    return default_port


def stream_container_crash_logs(
    runner: CommandRunner,
    docker_bin: str,
    container_name: str,
    docker_env: dict[str, str],
    on_log: LogSink | None,
) -> str:
    """Fetch last 30 log lines from a failed candidate container and stream them to ``on_log``."""
    res = runner.run([docker_bin, "logs", "--tail", "30", container_name], env=docker_env, timeout=15)
    combined = f"{res.stdout}\n{res.stderr}".strip()
    if not combined:
        return ""
    lines = [sanitize_log_line(line) for line in combined.splitlines() if line.strip()]
    if on_log is not None and lines:
        on_log(f"--- Container {container_name} output before failure ---")
        for line in lines[-25:]:
            on_log(f"  {line}")
    return lines[-1] if lines else ""


def detect_dockerfile_template(checkout: Path, container_port: int = 8000) -> DockerfileTemplate | None:
    """Return a generated Dockerfile when a supported root manifest is present."""
    port = int(container_port)
    package = _package_json(checkout)
    if package is not None:
        if _script(package, "start") is None:
            return DockerfileTemplate(
                "Node.js (package.json)",
                "",
                "node",
                "package.json",
                _MISSING_START,
            )
        return DockerfileTemplate(
            "Node.js (package.json)",
            _node_dockerfile(checkout, package, port),
            "node",
            "package.json",
        )
    has_reqs = (checkout / "requirements.txt").is_file() and not (checkout / "requirements.txt").is_symlink()
    has_pyproject = (checkout / "pyproject.toml").is_file() and not (checkout / "pyproject.toml").is_symlink()
    if has_reqs or has_pyproject:
        install_step = (
            "COPY requirements.txt ./\nRUN pip install --no-cache-dir -r requirements.txt\n"
            if has_reqs
            else "COPY pyproject.toml ./\nRUN pip install --no-cache-dir .\n"
        )
        entry_cmd = (
            'CMD ["python", "app.py"]\n'
            if (checkout / "app.py").is_file()
            else 'CMD ["python", "main.py"]\n'
            if (checkout / "main.py").is_file()
            else f'CMD ["python", "-m", "http.server", "{port}"]\n'
        )
        return DockerfileTemplate(
            "Python (requirements.txt / pyproject.toml)",
            (
                "FROM python:3.12-slim\n"
                "WORKDIR /app\n"
                f"{install_step}"
                "COPY . .\n"
                "RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app\n"
                f"ENV PORT={port} PYTHONUNBUFFERED=1\n"
                f"EXPOSE {port}\n"
                "USER appuser\n"
                f"{entry_cmd}"
            ),
        )
    if (checkout / "go.mod").is_file() and not (checkout / "go.mod").is_symlink():
        return DockerfileTemplate(
            "Go (go.mod)",
            (
                "FROM golang:1.23-alpine AS build\n"
                "WORKDIR /src\n"
                "COPY go.* ./\n"
                "RUN go mod download\n"
                "COPY . .\n"
                "RUN CGO_ENABLED=0 go build -o /out/server .\n"
                "FROM alpine:3.20\n"
                "RUN adduser -D -u 10001 appuser\n"
                "COPY --from=build /out/server /usr/local/bin/server\n"
                f"ENV PORT={port}\n"
                f"EXPOSE {port}\n"
                "USER appuser\n"
                'CMD ["/usr/local/bin/server"]\n'
            ),
        )
    if (checkout / "index.html").is_file() and not (checkout / "index.html").is_symlink():
        return DockerfileTemplate(
            "Static HTML (index.html)",
            (
                "FROM python:3.12-alpine\n"
                "WORKDIR /srv\n"
                "COPY . /srv\n"
                "RUN adduser -D -u 10001 appuser\n"
                f"EXPOSE {port}\n"
                "USER appuser\n"
                f'CMD ["python3", "-m", "http.server", "{port}", "--bind", "0.0.0.0"]\n'
            ),
        )
    return None


def ensure_dockerfile(
    checkout: Path,
    owner: str,
    repo: str,
    *,
    container_port: int = 8000,
    on_log: LogSink | None = None,
) -> Path:
    """Validate an existing root Dockerfile or synthesize one from detected language manifests."""
    dockerfile = checkout / "Dockerfile"
    if not dockerfile.exists() and not dockerfile.is_symlink():
        detected = detect_dockerfile_template(checkout, container_port=container_port)
        if detected is not None:
            if detected.error:
                raise PermanentJobError(f"Repository {owner}/{repo} {detected.error}")
            if on_log is not None:
                on_log(
                    f"No root Dockerfile found in {owner}/{repo}; auto-generating Dockerfile for {detected.label}..."
                )
            dockerfile.write_text(detected.contents, encoding="utf-8")
    return validate_dockerfile(checkout, owner, repo)
