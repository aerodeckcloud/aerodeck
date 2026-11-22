import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.project import EnvironmentKind
from app.schemas.common import Slug

Name = Annotated[str, Field(min_length=1, max_length=120)]
# DigitalOcean region slug, e.g. nyc3, ams3, fra1. Validated against the live list in Epic 2.
Region = Annotated[str, Field(min_length=4, max_length=16, pattern=r"^[a-z]{3}[0-9]$")]
DeployOverlapSetting = Literal["wait", "override"]


class EnvironmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    slug: Slug
    name: Name
    kind: EnvironmentKind
    region: Region = "nyc3"
    deploy_overlap: DeployOverlapSetting = "wait"


class EnvironmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Name | None = None
    kind: EnvironmentKind | None = None
    region: Region | None = None
    deploy_overlap: DeployOverlapSetting | None = None


class EnvironmentRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    slug: str
    name: str
    kind: EnvironmentKind
    region: str
    deploy_overlap: DeployOverlapSetting
    created_at: datetime
    updated_at: datetime
