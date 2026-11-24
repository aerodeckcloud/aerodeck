"""Pydantic models for the AeroDeck Application Blueprint Schema v1 (#236)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.github import parse_github_repo

AddonType = Literal["postgres", "redis", "mongodb"]
BuildStrategy = Literal["dockerfile", "compose", "auto"]
BlueprintRuntime = Literal["node", "python", "go", "java", "rust", "static"]
BlueprintSource = Literal["aerodeck.yaml", "docker-compose", "dockerfile", "auto"]

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){0,2}$")
_ADDON_DEFAULTS: dict[AddonType, tuple[str, str]] = {
    "postgres": ("DATABASE_URL", "16"),
    "redis": ("REDIS_URL", "7"),
    "mongodb": ("MONGODB_URI", "7"),
}


class BlueprintBuild(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    strategy: BuildStrategy = "dockerfile"
    dockerfile: str = "Dockerfile"
    compose_file: str | None = None
    service: str | None = None
    port: int = Field(default=8000, ge=1, le=65535)
    runtime: BlueprintRuntime | None = None
    marker: str | None = Field(default=None, max_length=64)


class BlueprintAddon(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    type: AddonType
    env_var: str = ""
    version: str = ""

    @model_validator(mode="after")
    def populate_defaults(self) -> BlueprintAddon:
        default_env, default_ver = _ADDON_DEFAULTS[self.type]
        if not self.env_var:
            self.env_var = default_env
        if not self.version:
            self.version = default_ver
        if not _ENV_KEY_RE.fullmatch(self.env_var):
            raise ValueError(f"invalid addon env_var {self.env_var!r}")
        if not _VERSION_RE.fullmatch(self.version):
            raise ValueError(f"invalid addon version {self.version!r}")
        return self


class BlueprintEnvVar(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    key: str
    description: str = ""
    required: bool = True
    secret: bool = False
    default: str | None = None
    generate: Literal["hex32"] | None = None

    @field_validator("key")
    @classmethod
    def valid_env_key(cls, value: str) -> str:
        if not _ENV_KEY_RE.fullmatch(value):
            raise ValueError(f"invalid environment variable key {value!r}")
        return value


class DeploymentBlueprint(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    version: int = 1
    name: str
    repo_url: str
    source: str = "auto"
    build: BlueprintBuild = Field(default_factory=BlueprintBuild)
    addons: list[BlueprintAddon] = Field(default_factory=list)
    env: list[BlueprintEnvVar] = Field(default_factory=list)


class DeploymentInspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    repo_url: str = Field(min_length=1, max_length=512)

    @field_validator("repo_url")
    @classmethod
    def github_repository(cls, value: str) -> str:
        if parse_github_repo(value) is None:
            raise ValueError("repo_url must be an http(s)://github.com/{owner}/{repo} URL")
        return value
