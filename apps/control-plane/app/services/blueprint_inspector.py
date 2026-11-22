"""Inspect a repository checkout or GitHub URL and produce a normalized DeploymentBlueprint (#236)."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

from app.core.github import canonical_github_repo_url, parse_github_repo
from app.schemas.blueprint import (
    BlueprintAddon,
    BlueprintBuild,
    BlueprintEnvVar,
    DeploymentBlueprint,
)
from app.services.docker_env_discovery import discover_repo_env_defaults
from app.services.docker_policy import CommandRunner, SubprocessCommandRunner, validate_binary
from app.services.dockerfile_detector import detect_dockerfile_template, detect_exposed_port

_SECRET_TOKENS = ("SECRET", "TOKEN", "PASSWORD", "PRIVATE_KEY", "API_KEY", "SIGNING_KEY")
_GENERATE_TOKENS = ("SECRET_KEY", "SIGNING_KEY", "SESSION_SECRET")
_PLATFORM_MANAGED_KEYS = frozenset({
    "PORT", "HOST", "AERODECK_PUBLIC_URL", "PUBLIC_URL", "PORTAL_PUBLIC_URL",
    "DATABASE_URL", "POSTGRES_URL", "REDIS_URL",
})
_PY_ENV_RE = re.compile(r"""os\.(?:getenv|environ\.get)\(\s*['"]([A-Za-z_][A-Za-z0-9_]{1,127})['"]""")


def _is_secret_key(key: str) -> bool:
    upper = key.upper()
    return any(tok in upper for tok in _SECRET_TOKENS)


def _needs_generator(key: str) -> bool:
    upper = key.upper()
    return any(tok in upper for tok in _GENERATE_TOKENS)


def _load_manifest_dict(checkout: Path) -> tuple[str, dict[str, Any]] | None:
    for name in ("aerodeck.yaml", "aerodeck.yml", "aerodeck.json"):
        path = checkout / name
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 128 * 1024:
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        try:
            data = json.loads(raw) if name.endswith(".json") else yaml.safe_load(raw)
        except Exception:
            continue
        if isinstance(data, dict):
            return name, data
    return None


def _detect_compose_addons_and_service(checkout: Path) -> tuple[str | None, str | None, list[BlueprintAddon]]:
    for name in ("docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml"):
        path = checkout / name
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 128 * 1024:
            continue
        try:
            parsed = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(parsed, dict) or not isinstance(parsed.get("services"), dict):
            continue
        services: dict[str, Any] = parsed["services"]
        app_service: str | None = None
        addons: list[BlueprintAddon] = []
        seen_types: set[str] = set()
        for svc_name, svc_cfg in services.items():
            if not isinstance(svc_cfg, dict):
                continue
            image = str(svc_cfg.get("image") or "").lower()
            if "postgres" in image and "postgres" not in seen_types:
                addons.append(BlueprintAddon(type="postgres", env_var="DATABASE_URL", version="16"))
                seen_types.add("postgres")
            elif "redis" in image and "redis" not in seen_types:
                addons.append(BlueprintAddon(type="redis", env_var="REDIS_URL", version="7"))
                seen_types.add("redis")
            elif svc_cfg.get("build") is not None and app_service is None:
                app_service = str(svc_name)
        return name, app_service, addons
    return None, None, []


def _scan_code_env_keys(checkout: Path) -> set[str]:
    found: set[str] = set()
    for name in ("config.py", "settings.py", "main.py", "app.py"):
        candidate = checkout / name
        if candidate.is_file() and not candidate.is_symlink() and candidate.stat().st_size <= 64 * 1024:
            text = candidate.read_text(encoding="utf-8", errors="replace")
            for match in _PY_ENV_RE.finditer(text):
                found.add(match.group(1))
    return found


def inspect_checkout_blueprint(checkout: Path, repo_url: str, repo_name: str) -> DeploymentBlueprint:
    """Build a normalized ``DeploymentBlueprint`` from a local repository checkout (#236)."""
    explicit = _load_manifest_dict(checkout)
    dockerfile = checkout / "Dockerfile"
    default_port = detect_exposed_port(dockerfile, 8000) if dockerfile.is_file() else 8000

    if explicit is not None:
        source_name, raw = explicit
        build_raw = raw.get("build") if isinstance(raw.get("build"), dict) else {}
        build = BlueprintBuild(
            strategy=build_raw.get("strategy", "dockerfile"),
            dockerfile=build_raw.get("dockerfile", "Dockerfile"),
            compose_file=build_raw.get("compose_file"),
            service=build_raw.get("service"),
            port=int(build_raw.get("port", default_port)),
        )
        addons = [
            BlueprintAddon.model_validate(item)
            for item in (raw.get("addons") or [])
            if isinstance(item, dict) and item.get("type") in ("postgres", "redis")
        ]
        env_items = [
            BlueprintEnvVar.model_validate(item)
            for item in (raw.get("env") or [])
            if isinstance(item, dict) and isinstance(item.get("key"), str)
        ]
        return DeploymentBlueprint(
            version=int(raw.get("version", 1)),
            name=str(raw.get("name") or repo_name),
            repo_url=repo_url,
            source=source_name,
            build=build,
            addons=addons,
            env=env_items,
        )

    compose_file, compose_service, addons = _detect_compose_addons_and_service(checkout)
    defaults = discover_repo_env_defaults(checkout)
    code_keys = _scan_code_env_keys(checkout)
    all_keys = sorted((set(defaults.keys()) | code_keys) - _PLATFORM_MANAGED_KEYS)

    if any("POSTGRES" in k.upper() or "DATABASE_URL" in k.upper() for k in (set(defaults.keys()) | code_keys)):
        if not any(a.type == "postgres" for a in addons):
            addons.append(BlueprintAddon(type="postgres", env_var="DATABASE_URL", version="16"))
    if any("REDIS" in k.upper() for k in (set(defaults.keys()) | code_keys)):
        if not any(a.type == "redis" for a in addons):
            addons.append(BlueprintAddon(type="redis", env_var="REDIS_URL", version="7"))

    env_vars: list[BlueprintEnvVar] = []
    for key in all_keys:
        default_val = defaults.get(key)
        is_secret = _is_secret_key(key)
        env_vars.append(
            BlueprintEnvVar(
                key=key,
                description="Auto-discovered secret" if is_secret else "Auto-discovered runtime variable",
                required=is_secret or default_val is None or default_val == "",
                secret=is_secret,
                default=default_val,
                generate="hex32" if _needs_generator(key) else None,
            )
        )

    has_dockerfile = dockerfile.is_file() and not dockerfile.is_symlink()
    detected_auto = None if has_dockerfile else detect_dockerfile_template(checkout, default_port)
    strategy = "dockerfile" if has_dockerfile else ("compose" if compose_file and not detected_auto else "auto")
    source = "docker-compose" if compose_file else ("dockerfile" if has_dockerfile else "auto")
    return DeploymentBlueprint(
        version=1,
        name=repo_name,
        repo_url=repo_url,
        source=source,
        build=BlueprintBuild(
            strategy=strategy,
            dockerfile="Dockerfile",
            compose_file=compose_file,
            service=compose_service,
            port=default_port,
        ),
        addons=addons,
        env=env_vars,
    )


def inspect_github_repository_sync(
    repo_url: str,
    *,
    git_bin: str = "git",
    runner: CommandRunner | None = None,
) -> DeploymentBlueprint:
    """Shallow-clone ``repo_url`` into a temporary directory and return its ``DeploymentBlueprint`` (#236)."""
    parsed = parse_github_repo(repo_url)
    if parsed is None:
        raise ValueError("repo_url must be a valid GitHub repository URL")
    owner, repo = parsed
    safe_git = validate_binary(git_bin, "git_bin")
    canonical = canonical_github_repo_url(owner, repo)
    clone_url = f"{canonical}.git"
    cmd_runner = runner or SubprocessCommandRunner()
    with tempfile.TemporaryDirectory(prefix="aerodeck-inspect-") as tmp:
        checkout = Path(tmp) / "repo"
        res = cmd_runner.run(
            [safe_git, "clone", "--depth=1", "--single-branch", "--", clone_url, str(checkout)],
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            timeout=45,
        )
        if res.returncode != 0:
            raise RuntimeError(f"Failed to inspect repository {owner}/{repo}")
        return inspect_checkout_blueprint(checkout, canonical, repo)
