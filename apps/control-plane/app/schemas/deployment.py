import json
import uuid
from datetime import datetime
from typing import Annotated, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.github import parse_github_repo
from app.models.deployment import DeploymentStatus, DeploymentTrigger

RepoUrl = Annotated[str, Field(min_length=1, max_length=512)]


class DeploymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    repo_url: RepoUrl
    project_id: uuid.UUID | None = None
    environment_id: uuid.UUID | None = None

    @field_validator("repo_url")
    @classmethod
    def github_repository(cls, value: str) -> str:
        if parse_github_repo(value) is None:
            raise ValueError("repo_url must be an http(s)://github.com/{owner}/{repo} URL")
        return value

    @model_validator(mode="after")
    def linked_pair(self) -> Self:
        if (self.project_id is None) ^ (self.environment_id is None):
            raise ValueError("project_id and environment_id must both be set or both omitted")
        return self


class DeploymentPromote(BaseModel):
    """Where to run a succeeded deployment's image (#272)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    environment_id: uuid.UUID


class DeploymentRollback(BaseModel):
    """Whether to restore the variables that shipped with the release (#273)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # When the source has a snapshot, default True restores it once the image
    # rollback is dispatched. A row that only waits does not change variables.
    restore_variables: bool = True


class DeploymentRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    repo_url: str
    owner: str
    repo: str
    project_id: uuid.UUID | None
    environment_id: uuid.UUID | None
    provision_job_id: uuid.UUID | None
    git_sha: str | None
    git_ref: str | None
    trigger: DeploymentTrigger | None
    actor_user_id: uuid.UUID | None
    status: DeploymentStatus
    started_at: datetime
    finished_at: datetime | None
    swap_started_at: datetime | None
    held_for_deployment_id: uuid.UUID | None
    # Names only when a snapshot exists. Never values or ciphertext (#273).
    snapshot_variable_names: list[str] | None = None
    # Set on rollback responses: True when the snapshot was applied.
    variables_restored: bool | None = None
    created_at: datetime
    updated_at: datetime


class DeploymentSource(Protocol):
    """Fields the read model copies. Not the ORM entity (#273)."""

    snapshot_names_json: str | None


def deployment_to_read(
    deployment: DeploymentSource,
    *,
    variables_restored: bool | None = None,
) -> DeploymentRead:
    """API model for a deployment row. Names only — never values or ciphertext."""
    return DeploymentRead.model_validate(deployment).model_copy(
        update={
            "snapshot_variable_names": _snapshot_names(deployment.snapshot_names_json),
            "variables_restored": variables_restored,
        }
    )


def _snapshot_names(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    try:
        names = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        return None
    return list(names)
