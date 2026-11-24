"""Public dependency findings stored on a draft. Advisory bodies are not kept (#345)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# no_advisories covers exact direct pins in root manifests only. Lockfiles and
# transitive packages are outside this result, so it is not a clear scan.
SecurityOutcome = Literal["no_advisories", "findings", "incomplete", "no_manifest"]
SecurityAcknowledgement = Literal["findings", "incomplete"]


class SecurityFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    severity: Literal["critical", "high", "medium", "low", "unknown"]
    package: str = Field(min_length=1, max_length=200)
    advisory_id: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=160)


class DraftSecurityRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: SecurityOutcome
    findings: list[SecurityFinding] = Field(default_factory=list, max_length=30)
    acknowledgement: SecurityAcknowledgement | None = None
    acknowledged_at: datetime | None = None
    acknowledged_by: uuid.UUID | None = None
    scanned_at: datetime | None = None


class DraftSecurityRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: SecurityOutcome
    findings: list[SecurityFinding] = Field(default_factory=list, max_length=30)
    acknowledgement: SecurityAcknowledgement | None = None


class DraftSecurityAcknowledge(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    acknowledgement: SecurityAcknowledgement
