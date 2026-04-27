from typing import Annotated

from fastapi import APIRouter, Header, Request, status

from agentic_sdlc_platform.glue.webhook_bridge import WebhookBridge
from agentic_sdlc_platform.models.webhooks import WebhookAcceptedResponse

router = APIRouter(tags=["webhooks"])


@router.post(
    "/linear",
    response_model=WebhookAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def linear_webhook(
    request: Request,
    linear_delivery: Annotated[str, Header(alias="Linear-Delivery", min_length=1)],
    linear_signature: str | None = Header(default=None, alias="Linear-Signature"),
) -> WebhookAcceptedResponse:
    bridge = WebhookBridge(
        settings=request.app.state.settings,
        repository=request.app.state.repository,
        task_orchestrator=request.app.state.task_orchestrator,
        issue_tracker=request.app.state.issue_tracker,
    )
    payload = await request.body()
    return await bridge.accept_linear(
        payload=payload,
        delivery_id=linear_delivery,
        signature=linear_signature,
    )


@router.post(
    "/github",
    response_model=WebhookAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def github_webhook(
    request: Request,
    github_event: Annotated[str, Header(alias="X-GitHub-Event", min_length=1)],
    github_delivery: Annotated[str, Header(alias="X-GitHub-Delivery", min_length=1)],
    github_signature: str | None = Header(default=None, alias="X-Hub-Signature-256"),
) -> WebhookAcceptedResponse:
    bridge = WebhookBridge(
        settings=request.app.state.settings,
        repository=request.app.state.repository,
        task_orchestrator=request.app.state.task_orchestrator,
        issue_tracker=request.app.state.issue_tracker,
    )
    payload = await request.body()
    return await bridge.accept_github(
        payload=payload,
        event=github_event,
        delivery_id=github_delivery,
        signature=github_signature,
    )
