"""Encrypted per-environment runtime variable repository, schemas, and service (#248)."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.models.environment_var import EnvironmentVariable
from app.models.project import Environment

logger = get_logger("services.environment_vars")

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_ENCRYPTION_DOMAIN = b"aerodeck:env-vars:v1:"


class EnvironmentLookup(Protocol):
    async def get(self, project_id: uuid.UUID, environment_id: uuid.UUID) -> Environment: ...


class EnvironmentVarUpsertItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=128)
    value: str = Field(max_length=8192)
    is_secret: bool = True

    @field_validator("key")
    @classmethod
    def valid_key(cls, value: str) -> str:
        cleaned = value.strip()
        if not _ENV_KEY_RE.fullmatch(cleaned):
            raise ValueError("Environment variable key must match ^[A-Za-z_][A-Za-z0-9_]{0,127}$")
        return cleaned


class EnvironmentVarUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[EnvironmentVarUpsertItem] = Field(min_length=1, max_length=100)


class EnvironmentVarRead(BaseModel):
    """Masked representation of a stored environment variable (never exposes plaintext)."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    key: str
    is_secret: bool
    updated_at: datetime


class EnvironmentVarCloneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_environment_id: uuid.UUID
    overwrite: bool = False


class EnvironmentVarCloneResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    copied: int
    skipped: int
    items: list[EnvironmentVarRead]


def _fernet_for(signing_key: str) -> Fernet:
    digest = hashlib.sha256(_ENCRYPTION_DOMAIN + signing_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_env_value(value: str, signing_key: str) -> str:
    return _fernet_for(signing_key).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_env_value(token: str, signing_key: str) -> str:
    return _fernet_for(signing_key).decrypt(token.encode("ascii")).decode("utf-8")


class EnvironmentVariableRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_environment(
        self, project_id: uuid.UUID, environment_id: uuid.UUID
    ) -> Sequence[EnvironmentVariable]:
        rows = await self._session.scalars(
            select(EnvironmentVariable)
            .where(
                EnvironmentVariable.project_id == project_id,
                EnvironmentVariable.environment_id == environment_id,
            )
            .order_by(EnvironmentVariable.key)
        )
        return rows.all()

    async def get_by_key(
        self, project_id: uuid.UUID, environment_id: uuid.UUID, key: str
    ) -> EnvironmentVariable | None:
        result = await self._session.execute(
            select(EnvironmentVariable).where(
                EnvironmentVariable.project_id == project_id,
                EnvironmentVariable.environment_id == environment_id,
                EnvironmentVariable.key == key,
            )
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        *,
        project_id: uuid.UUID,
        environment_id: uuid.UUID,
        key: str,
        encrypted_value: str,
        is_secret: bool,
    ) -> EnvironmentVariable:
        existing = await self.get_by_key(project_id, environment_id, key)
        if existing is not None:
            existing.encrypted_value = encrypted_value
            existing.is_secret = is_secret
            await self._session.flush()
            return existing
        row = EnvironmentVariable(
            project_id=project_id,
            environment_id=environment_id,
            key=key,
            encrypted_value=encrypted_value,
            is_secret=is_secret,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def delete(self, row: EnvironmentVariable) -> None:
        await self._session.delete(row)
        await self._session.flush()


class EnvironmentVariableService:
    def __init__(
        self,
        repo: EnvironmentVariableRepository,
        environments: EnvironmentLookup,
        *,
        signing_key: str,
    ) -> None:
        self._repo = repo
        self._environments = environments
        self._signing_key = signing_key

    async def list_masked(
        self, project_id: uuid.UUID, environment_id: uuid.UUID
    ) -> list[EnvironmentVarRead]:
        await self._environments.get(project_id, environment_id)
        rows = await self._repo.list_for_environment(project_id, environment_id)
        return [EnvironmentVarRead.model_validate(row) for row in rows]

    async def upsert_many(
        self,
        project_id: uuid.UUID,
        environment_id: uuid.UUID,
        items: Sequence[EnvironmentVarUpsertItem],
    ) -> list[EnvironmentVarRead]:
        await self._environments.get(project_id, environment_id)
        for item in items:
            encrypted = encrypt_env_value(item.value, self._signing_key)
            await self._repo.upsert(
                project_id=project_id,
                environment_id=environment_id,
                key=item.key,
                encrypted_value=encrypted,
                is_secret=item.is_secret,
            )
        logger.info("upserted %d env var(s) for environment %s", len(items), environment_id)
        rows = await self._repo.list_for_environment(project_id, environment_id)
        return [EnvironmentVarRead.model_validate(row) for row in rows]

    async def delete_key(self, project_id: uuid.UUID, environment_id: uuid.UUID, key: str) -> None:
        await self._environments.get(project_id, environment_id)
        row = await self._repo.get_by_key(project_id, environment_id, key)
        if row is None:
            raise NotFoundError(f"Environment variable {key!r} not found")
        await self._repo.delete(row)
        logger.info("deleted env var %s for environment %s", key, environment_id)

    async def clone_from_environment(
        self,
        project_id: uuid.UUID,
        target_environment_id: uuid.UUID,
        source_environment_id: uuid.UUID,
        *,
        overwrite: bool = False,
    ) -> EnvironmentVarCloneResult:
        if source_environment_id == target_environment_id:
            raise ValueError("Source and target environments must be different.")
        await self._environments.get(project_id, source_environment_id)
        await self._environments.get(project_id, target_environment_id)
        source_rows = await self._repo.list_for_environment(project_id, source_environment_id)
        target_rows = await self._repo.list_for_environment(project_id, target_environment_id)
        existing_keys = {row.key for row in target_rows}
        copied = 0
        skipped = 0
        for row in source_rows:
            if row.key in existing_keys and not overwrite:
                skipped += 1
                continue
            await self._repo.upsert(
                project_id=project_id,
                environment_id=target_environment_id,
                key=row.key,
                encrypted_value=row.encrypted_value,
                is_secret=row.is_secret,
            )
            copied += 1
        logger.info(
            "cloned env vars from %s to %s (copied=%d, skipped=%d)",
            source_environment_id,
            target_environment_id,
            copied,
            skipped,
        )
        updated = await self._repo.list_for_environment(project_id, target_environment_id)
        return EnvironmentVarCloneResult(
            copied=copied,
            skipped=skipped,
            items=[EnvironmentVarRead.model_validate(r) for r in updated],
        )

    def seal(self, values: dict[str, str]) -> str:
        """Encrypt one-off variables of a deployment that has to wait (#271).

        Same key and cipher as saved variables. The token lives on the waiting
        deployment row and is removed when the row starts or is cancelled.
        """
        return encrypt_env_value(json.dumps(values), self._signing_key)

    def unseal(self, token: str) -> dict[str, str]:
        """Reverse ``seal``. Raises ``InvalidToken`` when the signing key changed."""
        values = json.loads(decrypt_env_value(token, self._signing_key))
        return {str(key): str(value) for key, value in values.items()}

    async def load_decrypted_map(
        self, project_id: uuid.UUID, environment_id: uuid.UUID
    ) -> dict[str, str]:
        rows = await self._repo.list_for_environment(project_id, environment_id)
        decrypted: dict[str, str] = {}
        for row in rows:
            try:
                decrypted[row.key] = decrypt_env_value(row.encrypted_value, self._signing_key)
            except InvalidToken:
                logger.warning(
                    "skipped unreadable encrypted env var %s for environment %s",
                    row.key,
                    environment_id,
                )
        return decrypted
