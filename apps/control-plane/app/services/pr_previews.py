"""Automatic Pull Request preview environment lifecycle (#258)."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.deployment import DeploymentTrigger
from app.models.project import Environment, EnvironmentKind
from app.models.webhook import EnvironmentWebhook
from app.repositories.environments import EnvironmentRepository
from app.schemas.deployment import DeploymentCreate
from app.schemas.environment import EnvironmentCreate
from app.services.container_ops import dispatch_container_remove
from app.services.environment_vars import EnvironmentVariableService
from app.services.environments import EnvironmentService
from app.services.provisioning import JobKind

logger = get_logger("services.pr_previews")

_OPEN_ACTIONS = {"opened", "reopened", "synchronize"}


def preview_slug_for(pr_number: int) -> str:
    return f"pr-{pr_number}"


class PrPreviewService:
    """Orchestrate ephemeral PR preview environments on GitHub pull_request webhooks."""

    def __init__(
        self,
        env_repo: EnvironmentRepository,
        environments: EnvironmentService,
        env_vars: EnvironmentVariableService,
        settings: Settings,
    ) -> None:
        self._env_repo = env_repo
        self._environments = environments
        self._env_vars = env_vars
        self._settings = settings

    async def handle_event(
        self,
        *,
        payload: dict[str, Any],
        verified_webhooks: Sequence[EnvironmentWebhook],
        deployments: Any,
        jobs: Any,
        parse_addons: Callable[[str | None], list[str]],
    ) -> tuple[str, str | None, list[uuid.UUID]]:
        action = str(payload.get("action") or "").strip().lower()
        pr = payload.get("pull_request")
        if not isinstance(pr, dict):
            return ("ignored", "missing pull_request payload", [])

        pr_number = pr.get("number")
        if not isinstance(pr_number, int) or pr_number <= 0:
            return ("ignored", "invalid pull request number", [])

        # Security: never run untrusted fork PRs with project secrets.
        base_repo = (
            (payload.get("repository") or {}).get("full_name")
            if isinstance(payload.get("repository"), dict)
            else None
        )
        head_obj = pr.get("head") if isinstance(pr.get("head"), dict) else {}
        head_repo = (
            (head_obj.get("repo") or {}).get("full_name")
            if isinstance(head_obj.get("repo"), dict)
            else None
        )
        if base_repo and head_repo and str(base_repo).lower() != str(head_repo).lower():
            return ("ignored", "fork pull request ignored", [])

        slug = preview_slug_for(pr_number)
        primary = verified_webhooks[0]

        if action == "closed":
            existing = await self._env_repo.get_by_slug(primary.project_id, slug)
            if existing is None:
                return ("ignored", f"no preview environment {slug} found", [])
            preview_env_id = existing.id
            await self._environments.delete(primary.project_id, preview_env_id)
            dispatch_container_remove(
                self._settings, project_id=primary.project_id, environment_id=preview_env_id
            )
            logger.info("cleaned up PR preview environment %s (%s)", slug, preview_env_id)
            return ("cleaned_up", f"preview {slug} removed", [])

        if action not in _OPEN_ACTIONS:
            return ("ignored", f"unsupported pull_request action: {action}", [])

        head_ref = str(head_obj.get("ref") or "").strip()
        if not head_ref:
            return ("ignored", "missing pull request head branch", [])

        base_env = await self._environments.get(primary.project_id, primary.environment_id)
        preview_env: Environment | None = await self._env_repo.get_by_slug(primary.project_id, slug)
        if preview_env is None:
            preview_env = await self._environments.create(
                primary.project_id,
                EnvironmentCreate(
                    slug=slug,
                    name=f"PR #{pr_number} Preview",
                    kind=EnvironmentKind.dev,
                    region=base_env.region,
                ),
            )
            await self._env_vars.clone_from_environment(
                primary.project_id,
                preview_env.id,
                primary.environment_id,
                overwrite=False,
            )

        deployment = await deployments.create(
            DeploymentCreate(
                repo_url=primary.repo_url,
                project_id=primary.project_id,
                environment_id=preview_env.id,
            ),
            trigger=DeploymentTrigger.webhook,
        )
        addons = parse_addons(primary.addons_json) or None
        await jobs.enqueue(
            JobKind.provision,
            primary.project_id,
            preview_env.id,
            deployment_id=deployment.id,
            addons=addons,
            git_ref=head_ref,
        )
        logger.info(
            "triggered PR #%d preview deployment %s for branch %s",
            pr_number,
            deployment.id,
            head_ref,
        )
        return ("triggered", None, [deployment.id])
