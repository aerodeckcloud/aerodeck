"""Pydantic models for the AeroDeck Application Blueprint Schema v1 (#236)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.github import parse_github_repo

AddonType = Literal["postgres", "redis"]
BuildStrategy = Literal["dockerfile", "compose", "auto"]
BlueprintSource = Literal["aerodeck.yaml", "docker-compose", "dockerfile", "auto"]

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class BlueprintBuild(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    strategy: BuildStrategy = "dockerfile"
    dockerfile: str = "Dockerfile"
    compose_file: str | None = None
    service: str | None = None
    port: int = Field(default=8000, ge=1, le=65535)


class BlueprintAddon(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    type: AddonType
    env_var: str = ""
    version: str = ""

    def model_post_init(self, __context: object) -> None:
        if not self.env_var:
            object.__setattr__(self, "env_var", "DATABASE_URL" if self.type == "postgres" else "REDIS_URL")
        if not self.version:
            object.__setattr__(self, "version", "16" if self.type == "postgres" else "7")


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
