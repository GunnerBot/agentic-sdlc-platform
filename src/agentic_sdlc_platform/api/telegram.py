import hmac
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from agentic_sdlc_platform.glue.channel_repo_query import answer_repo_query
from agentic_sdlc_platform.glue.channel_router import (
    ChannelMessage,
    ChannelRouter,
    RouteTarget,
    parse_repo_query,
)
from agentic_sdlc_platform.glue.human_override import HumanOverrideHandler, parse_human_override
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.hermes_session import HermesSessionPort, HermesSessionRequest
from agentic_sdlc_platform.ports.task_orchestrator import TaskOrchestratorPort

router = APIRouter(tags=["telegram"])


class TelegramChatPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StrictInt | StrictStr


class TelegramUserPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StrictInt | StrictStr


class TelegramMessagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat: TelegramChatPayload
    from_: TelegramUserPayload = Field(alias="from")
    text: StrictStr


class TelegramUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: TelegramMessagePayload | None = None
    edited_message: TelegramMessagePayload | None = None


@router.post(
    "/webhook",
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Malformed Telegram update"},
        status.HTTP_401_UNAUTHORIZED: {"description": "Invalid Telegram secret token"},
    },
)
async def telegram_webhook(
    request: Request,
    payload: TelegramUpdatePayload,
    telegram_secret: Annotated[
        str | None,
        Header(
            alias="X-Telegram-Bot-Api-Secret-Token",
            pattern=r"^[ -~]{1,512}$",
        ),
    ] = None,
) -> dict[str, object]:
    return await handle_telegram_update(
        payload=payload.model_dump(by_alias=True, exclude_none=True),
        configured_secret=request.app.state.settings.telegram_secret_token,
        provided_secret=telegram_secret,
        hermes_session=request.app.state.hermes_session,
        channel_authorizer=request.app.state.channel_authorizer,
        repository=request.app.state.repository,
        graph_store=request.app.state.graph_store,
        task_orchestrator=request.app.state.task_orchestrator,
        channel_budget_ledger=request.app.state.channel_budget_ledger,
    )


async def handle_telegram_update(
    payload: dict[str, object],
    configured_secret: str | None,
    provided_secret: str | None,
    hermes_session: HermesSessionPort | None,
    channel_authorizer,
    repository: PersistenceRepository,
    graph_store,
    task_orchestrator: TaskOrchestratorPort | None,
    channel_budget_ledger,
) -> dict[str, object]:
    _verify_telegram_secret(configured_secret, provided_secret)
    message = _object(payload.get("message") or payload.get("edited_message"))
    if not message:
        return {"ok": True, "route": None, "session_id": None, "message_id": None}

    text = message.get("text")
    chat = _object(message.get("chat"))
    sender = _object(message.get("from"))
    chat_id = chat.get("id")
    sender_id = sender.get("id")
    if not isinstance(text, str) or chat_id is None or sender_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Telegram message fields",
        )

    channel = str(chat_id)
    sender_id_text = str(sender_id)
    mapping = channel_authorizer.authorize(
        provider="telegram",
        channel=channel,
        sender_id=sender_id_text,
    )
    override = parse_human_override(text)
    if override is not None:
        result = await HumanOverrideHandler(
            repository=repository,
            task_orchestrator=task_orchestrator,
        ).handle(command=override, actor=sender_id_text, channel=channel)
        return {
            "ok": True,
            "route": "human_override",
            "task_id": result.task_id,
            "command": result.command,
            "session_id": None,
            "message_id": None,
        }

    route = ChannelRouter().route(
        ChannelMessage(channel=channel, text=text, sender_id=sender_id_text)
    )
    session_id = None
    message_id = None
    if route == RouteTarget.GRAPH_REPO_QUERY:
        repo_answer = await answer_repo_query(
            parse_repo_query(text),
            repository=repository,
            graph_store=graph_store,
        )
        return {
            "ok": True,
            "route": repo_answer["route"],
            "repo": repo_answer["repo"],
            "answer": repo_answer["answer"],
            "references": repo_answer["references"],
            "session_id": None,
            "message_id": None,
        }
    if route == RouteTarget.HERMES_DIRECT and hermes_session is not None:
        channel_budget_ledger.reserve(provider="telegram", channel=channel)
        hermes_response = await hermes_session.ask(
            HermesSessionRequest(
                provider="telegram",
                channel=channel,
                sender_id=sender_id_text,
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


def _verify_telegram_secret(configured_secret: str | None, provided_secret: str | None) -> None:
    if not configured_secret:
        return
    if not provided_secret or not hmac.compare_digest(configured_secret, provided_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Telegram secret token",
        )


def _object(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}
