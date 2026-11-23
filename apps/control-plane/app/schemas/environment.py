import uuid
from datetime import datetime
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.models.project import (
    ROLLBACK_IMAGES_TO_KEEP_MAX,
    ROLLBACK_IMAGES_TO_KEEP_MIN,
    DeployOverlap,
    EnvironmentKind,
)
from app.schemas.common import Slug

Name = Annotated[str, Field(min_length=1, max_length=120)]
# DigitalOcean region slug, e.g. nyc3, ams3, fra1. Validated against the live list in Epic 2.
Region = Annotated[str, Field(min_length=4, max_length=16, pattern=r"^[a-z]{3}[0-9]$")]
# Succeeded images kept for rollback. The column default matches this (#274).
RollbackImagesToKeep = Annotated[
    int, Field(ge=ROLLBACK_IMAGES_TO_KEEP_MIN, le=ROLLBACK_IMAGES_TO_KEEP_MAX)
]


class EnvironmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    slug: Slug
    name: Name
    kind: EnvironmentKind
    region: Region = "nyc3"
    deploy_overlap: DeployOverlap = DeployOverlap.wait
    rollback_images_to_keep: RollbackImagesToKeep = 5


class EnvironmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Name | None = None
    kind: EnvironmentKind | None = None
    region: Region | None = None
    deploy_overlap: DeployOverlap | None = None
    rollback_images_to_keep: RollbackImagesToKeep | None = None


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
    created_at: datetime
    updated_at: datetime


def environment_to_read(
    environment: EnvironmentSource, *, retained_rollback_images: int
) -> EnvironmentRead:
    """Build the read model. The retained count is not a column (#274)."""
    read = EnvironmentRead.model_validate(environment)
    return read.model_copy(update={"retained_rollback_images": retained_rollback_images})
