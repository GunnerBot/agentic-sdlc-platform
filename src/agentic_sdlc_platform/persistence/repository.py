from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentic_sdlc_platform.persistence.models import AuditEvent, InboundEvent, Task, utc_now


@dataclass(frozen=True)
class InboundEventWriteResult:
    event: InboundEvent
    created: bool


class PersistenceRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_inbound_event(
        self,
        source: str,
        delivery_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> InboundEventWriteResult:
        async with self._session_factory() as session:
            event = InboundEvent(
                source=source,
                delivery_id=delivery_id,
                event_type=event_type,
                payload_json=payload,
            )
            session.add(event)
            try:
                await session.commit()
                await session.refresh(event)
                return InboundEventWriteResult(event=event, created=True)
            except IntegrityError:
                await session.rollback()
                existing = await self._find_inbound_event(session, source, delivery_id)
                return InboundEventWriteResult(event=existing, created=False)

    async def create_task_from_event(
        self,
        event_id: str,
        source: str,
        external_id: str,
        title: str,
        repo: str | None,
    ) -> Task:
        async with self._session_factory() as session:
            task = Task(
                inbound_event_id=event_id,
                source=source,
                external_id=external_id,
                title=title,
                repo=repo,
            )
            session.add(task)
            try:
                await session.commit()
                await session.refresh(task)
                return task
            except IntegrityError:
                await session.rollback()
                return await self._find_task(session, source, external_id)

    async def record_audit_event(
        self,
        action: str,
        actor: str,
        target_type: str,
        target_id: str,
        metadata: dict[str, object] | None = None,
    ) -> AuditEvent:
        async with self._session_factory() as session:
            audit_event = AuditEvent(
                action=action,
                actor=actor,
                target_type=target_type,
                target_id=target_id,
                metadata_json=metadata or {},
            )
            session.add(audit_event)
            await session.commit()
            await session.refresh(audit_event)
            return audit_event

    async def mark_task_orchestrated(
        self,
        task_id: str,
        orchestrator_task_id: str,
        orchestrator_status: str,
    ) -> Task:
        async with self._session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise LookupError(f"task {task_id} not found")
            task.orchestrator_task_id = orchestrator_task_id
            task.orchestrator_status = orchestrator_status
            task.updated_at = utc_now()
            await session.commit()
            await session.refresh(task)
            return task

    async def find_task_by_external_id(self, external_id: str) -> Task | None:
        async with self._session_factory() as session:
            result = await session.execute(select(Task).where(Task.external_id == external_id))
            return result.scalars().first()

    async def update_task_status(self, task_id: str, status: str) -> Task:
        async with self._session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise LookupError(f"task {task_id} not found")
            task.status = status
            task.updated_at = utc_now()
            await session.commit()
            await session.refresh(task)
            return task

    async def _find_inbound_event(
        self,
        session: AsyncSession,
        source: str,
        delivery_id: str,
    ) -> InboundEvent:
        result = await session.execute(
            select(InboundEvent).where(
                InboundEvent.source == source,
                InboundEvent.delivery_id == delivery_id,
            )
        )
        return result.scalar_one()

    async def _find_task(self, session: AsyncSession, source: str, external_id: str) -> Task:
        result = await session.execute(
            select(Task).where(Task.source == source, Task.external_id == external_id)
        )
        return result.scalar_one()
