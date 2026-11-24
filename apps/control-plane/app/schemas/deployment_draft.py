"""Saved deploy-deck answers. The ciphertext never leaves the server (#343)."""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.github import parse_github_repo
from app.schemas.blueprint import AddonType, DeploymentBlueprint
from app.schemas.deployment import DeploymentRead

DraftStep = Literal["build", "resources", "variables", "launch"]
_ENV_KEY = r"^[A-Za-z_][A-Za-z0-9_]{0,127}$"


class DraftVariable(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(pattern=_ENV_KEY)
    value: str = Field(default="", max_length=8192)
    secret: bool = False
    # True on a read, and on a write that means "keep the stored secret".
    redacted: bool = False

    @field_validator("value")
    @classmethod
    def no_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("variable value must not contain NUL")
        return value


class DeploymentDraftPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: DraftStep
    blueprint: DeploymentBlueprint
    detected_addons: list[AddonType] = Field(default_factory=list, max_length=3)
    selected_addons: list[AddonType] = Field(default_factory=list, max_length=3)
    variables: list[DraftVariable] = Field(default_factory=list, max_length=100)
    extra_env: str = Field(default="", max_length=16384)

    @field_validator("extra_env")
    @classmethod
    def extra_env_has_no_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("extra_env must not contain NUL")
        return value


class DeploymentDraftWrite(BaseModel):
    """Replace the saved plan. Secret fields marked redacted keep their stored value."""

    model_config = ConfigDict(extra="forbid")

    plan: DeploymentDraftPlan
    project_id: uuid.UUID | None = None
    environment_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def linked_pair(self) -> "DeploymentDraftWrite":
        if (self.project_id is None) ^ (self.environment_id is None):
            raise ValueError("project_id and environment_id must both be set or both omitted")
        return self


class DeploymentDraftCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    repo_url: str = Field(min_length=1, max_length=512)
    plan: DeploymentDraftPlan

    @field_validator("repo_url")
    @classmethod
    def github_repository(cls, value: str) -> str:
        if parse_github_repo(value) is None:
            raise ValueError("repo_url must be an http(s)://github.com/{owner}/{repo} URL")
        return value


class DeploymentDraftLaunch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: DeploymentDraftPlan
    project_id: uuid.UUID
    environment_id: uuid.UUID


class DeploymentDraftRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment: DeploymentRead
    plan: DeploymentDraftPlan
