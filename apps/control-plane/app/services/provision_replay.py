"""What a waiting provision needs to start later (#271).

Stored on the deployment row while it waits and cleared when it stops
waiting. One-off variables from the request are kept only as an encrypted
token; saved environment variables are loaded again when the row starts.
"""

import json
import uuid
from dataclasses import dataclass

from app.core.logging import get_logger

logger = get_logger("services.provision_replay")


@dataclass(frozen=True, slots=True)
class ProvisionReplay:
    git_ref: str | None = None
    rollback_from_deployment_id: uuid.UUID | None = None
    promote_from_deployment_id: uuid.UUID | None = None
    addons: list[str] | None = None
    sealed_env_vars: str | None = None
    # Full replace map for a rollback, distinct from one-off overlays (#273).
    sealed_replace_env_vars: str | None = None
    # Confirmed after the scan. Absent means the worker keeps its own detection.
    port: int | None = None
    service: str | None = None

    def to_json(self) -> str | None:
        payload: dict[str, object] = {}
        if self.git_ref:
            payload["git_ref"] = self.git_ref
        if self.rollback_from_deployment_id is not None:
            payload["rollback_from_deployment_id"] = str(self.rollback_from_deployment_id)
        if self.promote_from_deployment_id is not None:
            payload["promote_from_deployment_id"] = str(self.promote_from_deployment_id)
        if self.addons:
            payload["addons"] = self.addons
        if self.sealed_env_vars:
            payload["sealed_env_vars"] = self.sealed_env_vars
        if self.sealed_replace_env_vars:
            payload["sealed_replace_env_vars"] = self.sealed_replace_env_vars
        if self.port is not None:
            payload["port"] = self.port
        if self.service:
            payload["service"] = self.service
        return json.dumps(payload) if payload else None

    @classmethod
    def from_json(cls, raw: str | None) -> "ProvisionReplay":
        """Read a stored payload. Anything malformed is treated as absent.

        The payload itself is never logged: it can hold the encrypted variables.
        """
        if not raw:
            return cls()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("event=provision_replay_unreadable reason=invalid_json")
            return cls()
        if not isinstance(payload, dict):
            logger.warning("event=provision_replay_unreadable reason=not_an_object")
            return cls()
        addons = payload.get("addons")
        return cls(
            git_ref=_text(payload.get("git_ref")),
            rollback_from_deployment_id=_uuid(payload.get("rollback_from_deployment_id")),
            promote_from_deployment_id=_uuid(payload.get("promote_from_deployment_id")),
            addons=addons
            if isinstance(addons, list) and addons and all(isinstance(item, str) for item in addons)
            else None,
            sealed_env_vars=_text(payload.get("sealed_env_vars")),
            sealed_replace_env_vars=_text(payload.get("sealed_replace_env_vars")),
            port=_port(payload.get("port")),
            service=_text(payload.get("service")),
        )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _port(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if 1 <= value <= 65535:
        return value
    return None


def _uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None
