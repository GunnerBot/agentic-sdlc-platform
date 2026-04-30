import hmac
import json
import re
import time
from hashlib import sha256
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, status

from agentic_sdlc_platform.adapters.slack import SlackClient
from agentic_sdlc_platform.glue.channel_repo_query import answer_repo_query, answer_repo_question
from agentic_sdlc_platform.glue.channel_router import (
    ChannelMessage,
    ChannelRouter,
    RouteTarget,
    parse_repo_query,
)
from agentic_sdlc_platform.glue.human_override import (
    HumanOverrideHandler,
    NodeOverrideHandler,
    parse_human_override,
    parse_node_override,
    parse_task_info,
)
from agentic_sdlc_platform.glue.task_info import TaskInfoHandler
from agentic_sdlc_platform.glue.ticket_command import (
    build_issue_create_request,
    parse_create_ticket,
)
from agentic_sdlc_platform.ports.hermes_session import (
    HermesStartSessionRequest,
)
from agentic_sdlc_platform.ports.task_orchestrator import TaskCommentRequest

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
        Header(alias="X-Slack-Request-Timestamp", pattern=r"^(?:[0-9]{1,32}|null)$"),
    ] = None,
    slack_signature: Annotated[
        str | None,
        Header(alias="X-Slack-Signature", pattern=r"^[!-~]{1,512}$"),
    ] = None,
) -> dict[str, object]:
    body = await request.body()
    _verify_slack_signature(
        body=body,
        timestamp=slack_timestamp,
        signature=slack_signature,
        secret=request.app.state.settings.slack_signing_secret,
        tolerance_seconds=request.app.state.settings.slack_signature_tolerance_seconds,
        allow_unsigned=_allow_unsigned_webhooks(request.app.state.settings),
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
    ticket_command = parse_create_ticket(text)
    if ticket_command is not None and request.app.state.issue_tracker is not None:
        message_ts = _str_value(event.get("ts"))
        thread_ts = _str_value(event.get("thread_ts"))
        thread_context = None
        if thread_ts:
            thread_context = await SlackClient(request.app.state.settings).fetch_thread_context(
                channel=channel,
                thread_ts=thread_ts,
            )
        created_issue = await request.app.state.issue_tracker.create_issue(
            build_issue_create_request(
                ticket_command,
                provider="slack",
                channel=channel,
                sender_id=sender_id,
                message_ts=message_ts,
                thread_ts=thread_ts,
                thread_context=thread_context,
            )
        )
        return {
            "ok": True,
            "route": "create_ticket",
            "command": "create-ticket",
            "repo": ticket_command.repo,
            "issue_id": created_issue.issue_id,
            "external_id": created_issue.external_id,
            "url": created_issue.url,
            "session_id": None,
            "message_id": None,
        }

    info_command = parse_task_info(text)
    if info_command is not None:
        result = await TaskInfoHandler(request.app.state.repository).handle(info_command)
        return {
            "ok": True,
            "route": "task_info",
            "task_id": result.task_id,
            "external_id": result.external_id,
            "command": result.command,
            "answer": result.answer,
            "session_id": None,
            "message_id": None,
        }

    node_override = parse_node_override(text)
    if node_override is not None:
        result = await NodeOverrideHandler(request.app.state.repository).handle(
            command=node_override,
            actor=sender_id,
            channel=channel,
        )
        return {
            "ok": True,
            "route": "node_override",
            "task_id": result.task_id,
            "external_id": node_override.external_id,
            "command": result.command,
            "answer": (
                f"Node {result.node_key} on {node_override.external_id} "
                f"is now {result.status}."
            ),
            "session_id": None,
            "message_id": None,
        }

    override = parse_human_override(text)
    if override is not None:
        result = await HumanOverrideHandler(
            repository=request.app.state.repository,
            task_orchestrator=request.app.state.task_orchestrator,
        ).handle(command=override, actor=sender_id, channel=channel)
        return {
            "ok": True,
            "route": "human_override",
            "task_id": result.task_id,
            "command": result.command,
            "session_id": None,
            "message_id": None,
        }

    route = ChannelRouter().route(
        ChannelMessage(channel=channel, text=text, sender_id=sender_id)
    )
    session_id = None
    message_id = None
    if route == RouteTarget.GRAPH_REPO_QUERY:
        repo_answer = await answer_repo_query(
            parse_repo_query(text),
            repository=request.app.state.repository,
            graph_store=request.app.state.graph_store,
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
    if route == RouteTarget.HERMES_DIRECT:
        thread_id = _slack_thread_id(channel=channel, event=event)
        existing_session = await request.app.state.repository.find_agent_session(
            provider="slack",
            external_thread_id=thread_id,
        )
        repo_scope = mapping.repo if mapping else None
        if (
            existing_session is None
            and repo_scope
            and request.app.state.settings.vendor_http_enabled
        ):
            repo_answer = await answer_repo_question(
                repo=repo_scope,
                question=text,
                repository=request.app.state.repository,
                graph_store=request.app.state.graph_store,
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
        request.app.state.channel_budget_ledger.reserve(provider="slack", channel=channel)
        hermes_response = None
        if (
            existing_session
            and getattr(existing_session, "orchestrator_task_id", None)
            and getattr(existing_session, "orchestrator_issue_id", None)
            and request.app.state.task_orchestrator is not None
            and hasattr(request.app.state.task_orchestrator, "add_comment")
        ):
            await request.app.state.repository.record_session_event(
                session_id=existing_session.id,
                direction="inbound",
                event_type="comment",
                actor=f"slack:{sender_id}",
                message=text,
                metadata={"channel": channel, "thread_id": thread_id},
            )
            comment_response = await request.app.state.task_orchestrator.add_comment(
                TaskCommentRequest(
                    external_task_id=existing_session.orchestrator_task_id,
                    body=text,
                    actor=f"slack:{sender_id}",
                    metadata={
                        "multica_issue_id": existing_session.orchestrator_issue_id,
                        "provider": "slack",
                        "external_thread_id": thread_id,
                    },
                )
            )
            await request.app.state.repository.record_session_event(
                session_id=existing_session.id,
                direction="outbound",
                event_type="orchestrator_comment",
                actor="system",
                message=text,
                metadata={
                    "orchestrator_provider": existing_session.orchestrator_provider,
                    "orchestrator_task_id": existing_session.orchestrator_task_id,
                    **(comment_response.metadata or {}),
                },
            )
            session_id = existing_session.hermes_session_id
            message_id = comment_response.comment_id
        elif (
            existing_session
            and existing_session.hermes_session_id
            and request.app.state.hermes_session is not None
        ):
            await request.app.state.repository.record_session_event(
                session_id=existing_session.id,
                direction="inbound",
                event_type="comment",
                actor=f"slack:{sender_id}",
                message=text,
                metadata={"channel": channel, "thread_id": thread_id},
            )
            hermes_response = await request.app.state.hermes_session.resume_session(
                session_id=existing_session.hermes_session_id,
                text=text,
                actor=f"slack:{sender_id}",
            )
        elif request.app.state.hermes_session is not None:
            recorded = await request.app.state.repository.record_inbound_event(
                source="slack",
                delivery_id=thread_id,
                event_type="message",
                payload={"channel": channel, "thread_id": thread_id, "text": text},
            )
            task = await request.app.state.repository.create_task_from_event(
                event_id=recorded.event.id,
                source="slack",
                external_id=thread_id,
                title=text[:200],
                repo=repo_scope,
            )
            hermes_response = await request.app.state.hermes_session.start_session(
                HermesStartSessionRequest(
                    task_id=task.id,
                    provider="slack",
                    external_thread_id=thread_id,
                    text=text,
                    repo=repo_scope,
                )
            )
            existing_session = await request.app.state.repository.create_agent_session(
                task_id=task.id,
                provider="slack",
                external_thread_id=thread_id,
                hermes_session_id=hermes_response.session_id,
                repo=mapping.repo if mapping else None,
            )
            await request.app.state.repository.record_session_event(
                session_id=existing_session.id,
                direction="outbound",
                event_type="session_started",
                actor="system",
                message=text,
                metadata={"message_id": hermes_response.message_id},
            )
        if hermes_response and hermes_response.answer and existing_session is not None:
            await request.app.state.repository.record_session_event(
                session_id=existing_session.id,
                direction="outbound",
                event_type="reply",
                actor="agent",
                message=hermes_response.answer,
                metadata={"message_id": hermes_response.message_id},
            )
        if hermes_response:
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
    allow_unsigned: bool,
) -> None:
    if not secret:
        if not allow_unsigned:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Slack signing secret is not configured",
            )
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


def _allow_unsigned_webhooks(settings) -> bool:
    return bool(settings.allow_unsigned_webhooks) or settings.environment in {
        "local",
        "dev",
        "development",
        "test",
    }


def _parse_payload(body: bytes) -> dict[str, object]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
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


def _str_value(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _slack_thread_id(channel: str, event: dict[str, object]) -> str:
    thread_ts = _str_value(event.get("thread_ts")) or _str_value(event.get("ts"))
    return f"{channel}:{thread_ts or 'root'}"
