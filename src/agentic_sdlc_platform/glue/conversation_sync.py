import asyncio
from dataclasses import dataclass

from agentic_sdlc_platform.persistence.models import AgentSession
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.issue_tracker import IssueTrackerPort, IssueTrackerReply
from agentic_sdlc_platform.ports.task_orchestrator import TaskOrchestratorPort


@dataclass(frozen=True)
class ConversationSyncResult:
    session_id: str
    task_id: str
    provider: str
    external_thread_id: str
    new_messages: int


class ConversationSyncService:
    def __init__(
        self,
        *,
        repository: PersistenceRepository,
        task_orchestrator: TaskOrchestratorPort | None,
        issue_tracker: IssueTrackerPort | None = None,
        slack_client=None,
        telegram_client=None,
    ) -> None:
        self._repository = repository
        self._task_orchestrator = task_orchestrator
        self._issue_tracker = issue_tracker
        self._slack_client = slack_client
        self._telegram_client = telegram_client

    async def sync_session(self, session_id: str) -> ConversationSyncResult:
        session = await self._repository.get_agent_session(session_id)
        if session is None:
            raise LookupError(f"agent session {session_id} not found")
        return await self.sync_loaded_session(session)

    async def sync_loaded_session(self, session: AgentSession) -> ConversationSyncResult:
        if not session.orchestrator_task_id or not session.orchestrator_issue_id:
            raise ValueError("session is not orchestrator-backed")

        list_comments = getattr(self._task_orchestrator, "list_comments", None)
        if list_comments is None:
            raise ValueError("task orchestrator does not support conversation sync")

        seen_comment_ids = {
            value
            for event in session.events
            if isinstance((value := event.metadata_json.get("multica_comment_id")), str)
        }
        comments = await list_comments(
            session.orchestrator_task_id,
            {
                "multica_issue_id": session.orchestrator_issue_id,
                "orchestrator_issue_id": session.orchestrator_issue_id,
            },
        )
        new_messages = 0
        for comment in comments:
            if comment.id in seen_comment_ids:
                continue
            await self._mirror_comment(session, comment.body)
            await self._repository.record_session_event(
                session_id=session.id,
                direction="outbound",
                event_type="orchestrator_reply",
                actor=comment.actor or "agent",
                message=comment.body,
                metadata={
                    "multica_comment_id": comment.id,
                    **(comment.metadata or {}),
                },
            )
            seen_comment_ids.add(comment.id)
            new_messages += 1

        if new_messages:
            await self._repository.record_audit_event(
                action="agent_session.conversation_synced",
                actor="system",
                target_type="agent_session",
                target_id=session.id,
                metadata={
                    "provider": session.provider,
                    "external_thread_id": session.external_thread_id,
                    "orchestrator_provider": session.orchestrator_provider,
                    "orchestrator_issue_id": session.orchestrator_issue_id,
                    "orchestrator_task_id": session.orchestrator_task_id,
                    "new_messages": new_messages,
                },
            )

        return ConversationSyncResult(
            session_id=session.id,
            task_id=session.task_id,
            provider=session.provider,
            external_thread_id=session.external_thread_id,
            new_messages=new_messages,
        )

    async def _mirror_comment(self, session: AgentSession, body: str) -> None:
        if session.provider == "linear" and self._issue_tracker is not None:
            await self._issue_tracker.reply(
                IssueTrackerReply(issue_id=session.external_thread_id, body=body)
            )
        elif session.provider == "slack" and self._slack_client is not None:
            channel, thread_ts = _split_slack_thread_id(session.external_thread_id)
            if channel and thread_ts:
                await self._slack_client.post_thread_reply(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=body,
                )
        elif session.provider == "telegram" and self._telegram_client is not None:
            await self._telegram_client.send_message(
                chat_id=session.external_thread_id,
                text=body,
            )

    async def sync_active_sessions(self, limit: int = 50) -> list[ConversationSyncResult]:
        sessions = await self._repository.list_orchestrator_backed_agent_sessions(limit=limit)
        results = []
        for session in sessions:
            try:
                results.append(await self.sync_loaded_session(session))
            except Exception as exc:  # pragma: no cover - defensive background isolation
                await self._repository.record_audit_event(
                    action="agent_session.conversation_sync_failed",
                    actor="system",
                    target_type="agent_session",
                    target_id=session.id,
                    metadata={
                        "provider": session.provider,
                        "external_thread_id": session.external_thread_id,
                        "error": str(exc),
                    },
                )
        return results


def _split_slack_thread_id(external_thread_id: str) -> tuple[str | None, str | None]:
    if ":" not in external_thread_id:
        return None, None
    channel, thread_ts = external_thread_id.split(":", 1)
    return (channel or None, thread_ts or None)


async def run_conversation_sync_loop(
    *,
    service: ConversationSyncService,
    interval_seconds: float,
    batch_size: int,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        await service.sync_active_sessions(limit=batch_size)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
