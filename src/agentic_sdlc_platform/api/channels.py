from fastapi import APIRouter, Request, status

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
    ticket_command = parse_create_ticket(message.text)
    if ticket_command is not None and request.app.state.issue_tracker is not None:
        created_issue = await request.app.state.issue_tracker.create_issue(
            build_issue_create_request(
                ticket_command,
                provider=message.provider.value,
                channel=message.channel,
                sender_id=message.sender_id,
            )
        )
        return ChannelAcceptedResponse(
            accepted=True,
            provider=message.provider,
            channel=message.channel,
            route="create_ticket",
            command="create-ticket",
            repo=ticket_command.repo,
            issue_id=created_issue.issue_id,
            external_id=created_issue.external_id,
            url=created_issue.url,
        )

    info_command = parse_task_info(message.text)
    if info_command is not None:
        result = await TaskInfoHandler(request.app.state.repository).handle(info_command)
        return ChannelAcceptedResponse(
            accepted=True,
            provider=message.provider,
            channel=message.channel,
            route="task_info",
            task_id=result.task_id,
            command=result.command,
            external_id=result.external_id,
            answer=result.answer,
        )

    node_override = parse_node_override(message.text)
    if node_override is not None:
        result = await NodeOverrideHandler(request.app.state.repository).handle(
            command=node_override,
            actor=message.sender_id,
            channel=message.channel,
        )
        return ChannelAcceptedResponse(
            accepted=True,
            provider=message.provider,
            channel=message.channel,
            route="node_override",
            task_id=result.task_id,
            command=result.command,
            external_id=node_override.external_id,
            answer=(
                f"Node {result.node_key} on {node_override.external_id} "
                f"is now {result.status}."
            ),
        )

    override = parse_human_override(message.text)
    if override is not None:
        result = await HumanOverrideHandler(
            repository=request.app.state.repository,
            task_orchestrator=request.app.state.task_orchestrator,
        ).handle(
            command=override,
            actor=message.sender_id,
            channel=message.channel,
        )
        return ChannelAcceptedResponse(
            accepted=True,
            provider=message.provider,
            channel=message.channel,
            route="human_override",
            task_id=result.task_id,
            command=result.command,
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
    if route == RouteTarget.GRAPH_REPO_QUERY:
        repo_query = parse_repo_query(message.text)
        repo_answer = await answer_repo_query(
            repo_query,
            repository=request.app.state.repository,
            graph_store=request.app.state.graph_store,
        )
        return ChannelAcceptedResponse(
            accepted=True,
            provider=message.provider,
            channel=message.channel,
            route=repo_answer["route"],
            repo=repo_answer["repo"],
            answer=repo_answer["answer"],
            references=repo_answer["references"],
        )
    repo_scope = message.repo or (mapping.repo if mapping else None)
    if (
        route == RouteTarget.HERMES_DIRECT
        and repo_scope
        and request.app.state.settings.vendor_http_enabled
    ):
        repo_answer = await answer_repo_question(
            repo=repo_scope,
            question=message.text,
            repository=request.app.state.repository,
            graph_store=request.app.state.graph_store,
        )
        return ChannelAcceptedResponse(
            accepted=True,
            provider=message.provider,
            channel=message.channel,
            route=repo_answer["route"],
            repo=repo_answer["repo"],
            answer=repo_answer["answer"],
            references=repo_answer["references"],
        )
    if route == RouteTarget.HERMES_DIRECT and request.app.state.hermes_session is not None:
        request.app.state.channel_budget_ledger.reserve(
            provider=message.provider.value,
            channel=message.channel,
        )
        hermes_response = await request.app.state.hermes_session.ask(
            HermesSessionRequest(
                provider=message.provider.value,
                channel=message.channel,
                sender_id=message.sender_id,
                text=message.text,
                repo=repo_scope,
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
