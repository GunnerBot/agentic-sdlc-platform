import hmac
import json
import re
import time
from hashlib import sha256
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, status

from agentic_sdlc_platform.glue.channel_router import ChannelMessage, ChannelRouter, RouteTarget
from agentic_sdlc_platform.ports.hermes_session import HermesSessionRequest

router = APIRouter(tags=["slack"])


@router.post(
    "/events",
    status_code=status.HTTP_200_OK,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": True,
                    }
                }
            },
        }
    },
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Malformed Slack event"},
        status.HTTP_401_UNAUTHORIZED: {"description": "Invalid Slack signature"},
    },
)
async def slack_events(
    request: Request,
    slack_timestamp: Annotated[
        str | None,
        Header(alias="X-Slack-Request-Timestamp", pattern=r"^[ -~]{1,128}$"),
    ] = None,
    slack_signature: Annotated[
        str | None,
        Header(alias="X-Slack-Signature", pattern=r"^[ -~]{1,512}$"),
    ] = None,
) -> dict[str, object]:
    body = await request.body()
    _verify_slack_signature(
        body=body,
        timestamp=slack_timestamp,
        signature=slack_signature,
        secret=request.app.state.settings.slack_signing_secret,
        tolerance_seconds=request.app.state.settings.slack_signature_tolerance_seconds,
    )
    payload = _parse_payload(body)

    if payload.get("type") == "url_verification":
        challenge = payload.get("challenge")
        if not isinstance(challenge, str):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing Slack challenge",
            )
        return {"challenge": challenge}

    event = payload.get("event")
    if not isinstance(event, dict):
        return {"ok": True, "route": None, "session_id": None, "message_id": None}

    event_type = event.get("type")
    if event_type not in {"app_mention", "message"}:
        return {"ok": True, "route": None, "session_id": None, "message_id": None}

    text = _clean_slack_text(event.get("text"))
    channel = event.get("channel")
    sender_id = event.get("user")
    if not text or not isinstance(channel, str) or not isinstance(sender_id, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Slack event fields",
        )
    mapping = request.app.state.channel_authorizer.authorize(
        provider="slack",
        channel=channel,
        sender_id=sender_id,
    )

    route = ChannelRouter().route(
        ChannelMessage(channel=channel, text=text, sender_id=sender_id)
    )
    session_id = None
    message_id = None
    if route == RouteTarget.HERMES_DIRECT and request.app.state.hermes_session is not None:
        hermes_response = await request.app.state.hermes_session.ask(
            HermesSessionRequest(
                provider="slack",
                channel=channel,
                sender_id=sender_id,
                text=text,
                repo=mapping.repo if mapping else None,
            )
        )
        session_id = hermes_response.session_id
        message_id = hermes_response.message_id

    return {
        "ok": True,
        "route": route.value,
        "session_id": session_id,
        "message_id": message_id,
    }


def _verify_slack_signature(
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    secret: str | None,
    tolerance_seconds: int,
) -> None:
    if not secret:
        return
    if not timestamp or not signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Slack signature",
        )
    try:
        timestamp_int = int(timestamp)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Slack timestamp",
        ) from exc
    if abs(int(time.time()) - timestamp_int) > tolerance_seconds:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Expired Slack signature",
        )

    base = b"v0:" + timestamp.encode("utf-8") + b":" + body
    expected = "v0=" + hmac.new(secret.encode("utf-8"), base, sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Slack signature",
        )


def _parse_payload(body: bytes) -> dict[str, object]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed Slack event",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed Slack event",
        )
    return payload


def _clean_slack_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"<@[A-Z0-9]+>\s*", "", value).strip()
    return cleaned or None
