import uuid
from datetime import datetime
from typing import Annotated, Protocol, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from app.models.project import (
    ROLLBACK_IMAGES_TO_KEEP_MAX,
    ROLLBACK_IMAGES_TO_KEEP_MIN,
    DeployOverlap,
    EnvironmentKind,
)
from app.schemas.common import Slug
from app.services.docker_policy import parse_release_command
from app.services.provisioning import PermanentJobError

Name = Annotated[str, Field(min_length=1, max_length=120)]
# DigitalOcean region slug, e.g. nyc3, ams3, fra1. Validated against the live list in Epic 2.
Region = Annotated[str, Field(min_length=4, max_length=16, pattern=r"^[a-z]{3}[0-9]$")]
# Succeeded images kept for rollback. The column default matches this (#274).
RollbackImagesToKeep = Annotated[
    int, Field(ge=ROLLBACK_IMAGES_TO_KEEP_MIN, le=ROLLBACK_IMAGES_TO_KEEP_MAX)
]


def _release_command_or_none(value: object) -> str | None:
    """Blank clears the column. Anything else must be an executable plus arguments."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("release command must be a string")
    if not value.strip():
        return None
    try:
        parse_release_command(value)
    except PermanentJobError as exc:
        raise ValueError(str(exc)) from exc
    return value.strip()


# Executable plus arguments. Blank clears the column. A shell script is rejected (#275).
ReleaseCommand = Annotated[str | None, BeforeValidator(_release_command_or_none)]


class EnvironmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    slug: Slug
    name: Name
    kind: EnvironmentKind
    region: Region = "nyc3"
    deploy_overlap: DeployOverlap = DeployOverlap.wait
    rollback_images_to_keep: RollbackImagesToKeep = 5
    release_command: ReleaseCommand = None
    # Accepted and ignored. Create never starts paused; an owner pauses later (#276).
    auto_deploy_paused: bool = False
    # Accepted and ignored. A new prod environment always requires approval (#276).
    prod_deploy_approval: bool | None = None

    @model_validator(mode="after")
    def default_release_controls(self) -> Self:
        self.auto_deploy_paused = False
        self.prod_deploy_approval = self.kind is EnvironmentKind.prod
        return self


class EnvironmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Name | None = None
    kind: EnvironmentKind | None = None
    region: Region | None = None
    deploy_overlap: DeployOverlap | None = None
    rollback_images_to_keep: RollbackImagesToKeep | None = None
    release_command: ReleaseCommand = None
    auto_deploy_paused: bool | None = None
    prod_deploy_approval: bool | None = None


class EnvironmentRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    slug: str
    name: str
    kind: EnvironmentKind
    region: str
    deploy_overlap: DeployOverlap
    rollback_images_to_keep: int
    retained_rollback_images: int = 0
    release_command: str | None = None
    auto_deploy_paused: bool
    prod_deploy_approval: bool
    created_at: datetime
    updated_at: datetime


class EnvironmentSource(Protocol):
    """Fields ``EnvironmentRead`` copies off the environment row."""

    id: uuid.UUID
    project_id: uuid.UUID
    slug: str
    name: str
    kind: EnvironmentKind
    region: str
    deploy_overlap: str
    rollback_images_to_keep: int
    release_command: str | None
    auto_deploy_paused: bool
    prod_deploy_approval: bool
    created_at: datetime
    updated_at: datetime


def environment_to_read(
    environment: EnvironmentSource, *, retained_rollback_images: int
) -> EnvironmentRead:
    """Build the read model. The retained count is not a column (#274)."""
    read = EnvironmentRead.model_validate(environment)
    return read.model_copy(update={"retained_rollback_images": retained_rollback_images})
