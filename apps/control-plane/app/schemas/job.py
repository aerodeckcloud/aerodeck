import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    addons: list[Literal["postgres", "redis", "mongodb"]] | None = Field(
        default=None,
        description="Optional containerized database addons to provision and attach (#194, #237, #242).",
    )
    port: int | None = Field(
        default=None,
        ge=1,
        le=65535,
        description="Container port confirmed after the repository scan. Overrides the detected EXPOSE.",
    )
    service: str | None = Field(
        default=None,
        max_length=128,
        description="Compose service confirmed after the repository scan.",
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

    @field_validator("service")
    @classmethod
    def validate_service(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
            raise ValueError("invalid compose service")
        return value


class JobAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID | None = None
    active_deployment_id: uuid.UUID | None = None
    kind: JobKind
    project_id: uuid.UUID
    environment_id: uuid.UUID
    status: JobStatusValue = "queued"
    # True when the row is stored and waiting for an owner, so no job was started (#276).
    approval_pending: bool = False

    @model_validator(mode="after")
    def job_or_active_deployment(self) -> "JobAccepted":
        if self.approval_pending:
            return self
        if self.job_id is None and self.active_deployment_id is None:
            raise ValueError("job_id or active_deployment_id is required")
        return self


class JobRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    status: JobStatusValue
    result: dict[str, str | bool] | None = None
    error: str | None = None
