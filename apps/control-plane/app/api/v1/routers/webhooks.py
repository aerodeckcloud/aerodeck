"""Public GitHub webhook receiver verified via X-Hub-Signature-256 (#249)."""

from __future__ import annotations

from fastapi import APIRouter, Header, Request, Response, status

from app.api.deps import DeploymentServiceDep, JobServiceDep, WebhookServiceDep
from app.schemas.auth import ErrorResponse
from app.services.webhooks import WebhookDeliveryResult

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post(
    "/github",
    response_model=WebhookDeliveryResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Receive GitHub push webhooks for automatic blue/green redeploys (#249)",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    },
)
async def receive_github_webhook(
    request: Request,
    response: Response,
    webhooks: WebhookServiceDep,
    deployments: DeploymentServiceDep,
    jobs: JobServiceDep,
    x_github_event: str = Header(default="push", alias="X-GitHub-Event"),
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    x_github_delivery: str | None = Header(default=None, alias="X-GitHub-Delivery"),
) -> WebhookDeliveryResult:
    raw_body = await request.body()
    result = await webhooks.handle_github_event(
        raw_body=raw_body,
        event=x_github_event.strip().lower(),
        signature=x_hub_signature_256,
        delivery_id=x_github_delivery.strip() if x_github_delivery else None,
        deployments=deployments,
        jobs=jobs,
    )
    if result.status != "triggered":
        response.status_code = status.HTTP_200_OK
    return result
