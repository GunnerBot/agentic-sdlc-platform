from fastapi import APIRouter, status

from agentic_sdlc_platform.glue.channel_router import ChannelMessage, ChannelRouter
from agentic_sdlc_platform.models.channels import ChannelAcceptedResponse, ChannelMessageRequest

router = APIRouter(tags=["channels"])


@router.post(
    "/messages",
    response_model=ChannelAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={status.HTTP_400_BAD_REQUEST: {"description": "Malformed request body"}},
)
async def accept_channel_message(request: ChannelMessageRequest) -> ChannelAcceptedResponse:
    route = ChannelRouter().route(
        ChannelMessage(
            channel=request.channel,
            text=request.text,
            sender_id=request.sender_id,
        )
    )
    return ChannelAcceptedResponse(
        accepted=True,
        provider=request.provider,
        channel=request.channel,
        route=route.value,
    )
