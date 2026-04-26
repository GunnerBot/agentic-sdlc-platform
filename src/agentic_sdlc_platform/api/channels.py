from fastapi import APIRouter, Request, status

from agentic_sdlc_platform.glue.channel_router import ChannelMessage, ChannelRouter, RouteTarget
from agentic_sdlc_platform.models.channels import ChannelAcceptedResponse, ChannelMessageRequest
from agentic_sdlc_platform.ports.hermes_session import HermesSessionRequest

router = APIRouter(tags=["channels"])


@router.post(
    "/messages",
    response_model=ChannelAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={status.HTTP_400_BAD_REQUEST: {"description": "Malformed request body"}},
)
async def accept_channel_message(
    request: Request,
    message: ChannelMessageRequest,
) -> ChannelAcceptedResponse:
    mapping = request.app.state.channel_authorizer.authorize(
        provider=message.provider.value,
        channel=message.channel,
        sender_id=message.sender_id,
    )
    route = ChannelRouter().route(
        ChannelMessage(
            channel=message.channel,
            text=message.text,
            sender_id=message.sender_id,
        )
    )
    session_id = None
    message_id = None
    if route == RouteTarget.HERMES_DIRECT and request.app.state.hermes_session is not None:
        hermes_response = await request.app.state.hermes_session.ask(
            HermesSessionRequest(
                provider=message.provider.value,
                channel=message.channel,
                sender_id=message.sender_id,
                text=message.text,
                repo=message.repo or (mapping.repo if mapping else None),
            )
        )
        session_id = hermes_response.session_id
        message_id = hermes_response.message_id

    return ChannelAcceptedResponse(
        accepted=True,
        provider=message.provider,
        channel=message.channel,
        route=route.value,
        session_id=session_id,
        message_id=message_id,
    )
