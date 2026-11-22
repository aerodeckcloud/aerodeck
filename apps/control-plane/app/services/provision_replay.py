"""What a waiting provision needs to start later (#271).

Stored on the deployment row while it waits and cleared when it stops
waiting. One-off variables from the request are kept only as an encrypted
token; saved environment variables are loaded again when the row starts.
"""

import json
import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProvisionReplay:
    git_ref: str | None = None
    rollback_from_deployment_id: uuid.UUID | None = None
    addons: list[str] | None = None
    sealed_env_vars: str | None = None

    def to_json(self) -> str | None:
        payload: dict[str, object] = {}
        if self.git_ref:
            payload["git_ref"] = self.git_ref
        if self.rollback_from_deployment_id is not None:
            payload["rollback_from_deployment_id"] = str(self.rollback_from_deployment_id)
        if self.addons:
            payload["addons"] = self.addons
        if self.sealed_env_vars:
            payload["sealed_env_vars"] = self.sealed_env_vars
        return json.dumps(payload) if payload else None

    @classmethod
    def from_json(cls, raw: str | None) -> "ProvisionReplay":
        """Read a stored payload. Anything malformed is treated as absent."""
        if not raw:
            return cls()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return cls()
        if not isinstance(payload, dict):
            return cls()
        addons = payload.get("addons")
        return cls(
            git_ref=_text(payload.get("git_ref")),
            rollback_from_deployment_id=_uuid(payload.get("rollback_from_deployment_id")),
            addons=addons
            if isinstance(addons, list) and addons and all(isinstance(item, str) for item in addons)
            else None,
            sealed_env_vars=_text(payload.get("sealed_env_vars")),
        )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None
