"""Public GitHub webhook receiver verified via X-Hub-Signature-256 (#249)."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request, Response, status

from app.api.deps import (
    DeploymentServiceDep,
    EnvironmentServiceDep,
    EnvironmentVariableServiceDep,
    JobServiceDep,
    SessionDep,
    SettingsDep,
    WebhookServiceDep,
)
from app.schemas.auth import ErrorResponse
from app.services.pr_previews import handle_pull_request_event
from app.services.webhook_rate_limit import enforce_webhook_rate_limit
from app.services.webhooks import _MAX_WEBHOOK_BODY_BYTES, WebhookDeliveryResult

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post(
    "/github",
    response_model=WebhookDeliveryResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Receive GitHub push webhooks for automatic blue/green redeploys (#249)",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse},
        status.HTTP_413_CONTENT_TOO_LARGE: {"model": ErrorResponse},
        status.HTTP_429_TOO_MANY_REQUESTS: {"model": ErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    },
)
async def receive_github_webhook(
    request: Request,
    response: Response,
    session: SessionDep,
    webhooks: WebhookServiceDep,
    environments: EnvironmentServiceDep,
    env_vars: EnvironmentVariableServiceDep,
    deployments: DeploymentServiceDep,
    jobs: JobServiceDep,
    settings: SettingsDep,
    x_github_event: str = Header(default="push", alias="X-GitHub-Event"),
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    x_github_delivery: str | None = Header(default=None, alias="X-GitHub-Delivery"),
) -> WebhookDeliveryResult:
    """Validate rate limit, payload size, and HMAC-SHA256 signature, then dispatch redeploys."""
    client_ip = request.client.host if request.client else "unknown"
    await enforce_webhook_rate_limit(client_ip, settings.redis_url)

    if not x_hub_signature_256 or not x_hub_signature_256.startswith("sha256="):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed X-Hub-Signature-256 header.",
        )

    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > _MAX_WEBHOOK_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Webhook payload exceeds 1 MiB limit.",
        )

    raw_body = await request.body()
    if len(raw_body) > _MAX_WEBHOOK_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Webhook payload exceeds 1 MiB limit.",
        )

    async def _handle_pr(
        *, payload: dict[str, object], verified_webhooks: object, parse_addons: object
    ) -> tuple[str, str | None, list[object]]:
        return await handle_pull_request_event(
            session=session,
            settings=settings,
            payload=payload,
            verified_webhooks=verified_webhooks,  # type: ignore[arg-type]
            environments=environments,
            env_vars=env_vars,
            deployments=deployments,
            jobs=jobs,
            parse_addons=parse_addons,
        )

    result = await webhooks.handle_github_event(
        raw_body=raw_body,
        event=x_github_event.strip().lower(),
        signature=x_hub_signature_256,
        delivery_id=x_github_delivery.strip() if x_github_delivery else None,
        deployments=deployments,
        jobs=jobs,
        pr_preview_handler=_handle_pr,
    )
    if result.status != "triggered":
        response.status_code = status.HTTP_200_OK
    return result
