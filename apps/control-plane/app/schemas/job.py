import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.provisioning import JobKind

JobStatusValue = Literal["queued", "running", "retrying", "succeeded", "failed"]
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class ProvisionJobCreate(BaseModel):
    """Optional body for ``POST .../provision``. Links the job to a console deployment."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    deployment_id: uuid.UUID | None = Field(
        default=None,
        description="When set, the worker publishes progress lines for this deployment's log stream.",
    )
    env_vars: dict[str, str] | None = Field(
        default=None,
        description="Optional runtime KEY=VALUE environment overrides for the deployed container (#233).",
    )

    @field_validator("env_vars")
    @classmethod
    def validate_env_vars(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        if len(value) > 64:
            raise ValueError("at most 64 environment variables may be supplied per deployment")
        cleaned: dict[str, str] = {}
        for k, v in value.items():
            key = k.strip()
            if not _ENV_KEY_RE.fullmatch(key):
                raise ValueError(f"invalid environment variable key {key!r}")
            if len(v) > 4096 or "\n" in v or "\r" in v or "\x00" in v:
                raise ValueError(f"invalid value for environment variable {key!r}")
            cleaned[key] = v
        return cleaned


class JobAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    kind: JobKind
    project_id: uuid.UUID
    environment_id: uuid.UUID
    status: JobStatusValue = "queued"


class JobRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    status: JobStatusValue
    result: dict[str, str | bool] | None = None
    error: str | None = None
