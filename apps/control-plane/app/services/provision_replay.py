"""Replay payload for a provision that has to wait.

Stores the git ref, rollback source, and addon names. Environment values stay
in the encrypted variable table and are loaded again when the row starts.
"""

import json
import uuid


def provision_replay_json(
    *,
    git_ref: str | None,
    rollback_from_deployment_id: uuid.UUID | None,
    addons: list[str] | None,
) -> str | None:
    payload: dict[str, object] = {}
    if git_ref:
        payload["git_ref"] = git_ref
    if rollback_from_deployment_id is not None:
        payload["rollback_from_deployment_id"] = str(rollback_from_deployment_id)
    if addons:
        payload["addons"] = addons
    if not payload:
        return None
    return json.dumps(payload)


def parse_provision_replay(
    raw: str | None,
) -> tuple[str | None, uuid.UUID | None, list[str] | None]:
    if not raw:
        return None, None, None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, None, None
    if not isinstance(payload, dict):
        return None, None, None
    git_ref = payload.get("git_ref")
    if not isinstance(git_ref, str) or not git_ref:
        git_ref = None
    rollback_raw = payload.get("rollback_from_deployment_id")
    rollback_from: uuid.UUID | None = None
    if isinstance(rollback_raw, str):
        try:
            rollback_from = uuid.UUID(rollback_raw)
        except ValueError:
            rollback_from = None
    addons_raw = payload.get("addons")
    addons = (
        addons_raw
        if isinstance(addons_raw, list) and all(isinstance(item, str) for item in addons_raw)
        else None
    )
    return git_ref, rollback_from, addons
