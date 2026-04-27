from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from agentic_sdlc_platform.glue.dag_decomposer import Subtask
from agentic_sdlc_platform.persistence.models import (
    AuditEvent,
    InboundEvent,
    Task,
    TaskDag,
    TaskDagNode,
    utc_now,
)


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

    async def create_task_dag(self, task_id: str, subtasks: list[Subtask]) -> TaskDag:
        async with self._session_factory() as session:
            dag = TaskDag(task_id=task_id)
            for index, subtask in enumerate(subtasks):
                dag.nodes.append(
                    TaskDagNode(
                        node_key=subtask.id,
                        title=subtask.title,
                        repo=subtask.repo,
                        depends_on_json={"nodes": list(subtask.depends_on)},
                        status="blocked" if subtask.depends_on else "ready",
                        position=index,
                    )
                )
            session.add(dag)
            await session.commit()
            return await self._get_task_dag(session, dag.id)

    async def list_ready_dag_nodes(self, task_id: str) -> list[TaskDagNode]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TaskDag)
                .where(TaskDag.task_id == task_id)
                .options(selectinload(TaskDag.nodes))
                .order_by(TaskDag.created_at.desc())
            )
            dag = result.scalars().first()
            if dag is None:
                return []

            completed = {
                node.node_key for node in dag.nodes if node.status == "completed"
            }
            ready_nodes = []
            for node in dag.nodes:
                if node.status == "completed":
                    continue
                if all(dependency in completed for dependency in node.depends_on):
                    ready_nodes.append(node)
            return ready_nodes

    async def mark_dag_node_completed(self, dag_id: str, node_key: str) -> TaskDagNode:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TaskDagNode).where(
                    TaskDagNode.dag_id == dag_id,
                    TaskDagNode.node_key == node_key,
                )
            )
            node = result.scalar_one()
            node.status = "completed"
            node.updated_at = utc_now()
            await session.commit()
            await session.refresh(node)
            return node

    async def mark_dag_node_orchestrated(
        self,
        dag_id: str,
        node_key: str,
        orchestrator_task_id: str,
        orchestrator_status: str,
    ) -> TaskDagNode:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TaskDagNode).where(
                    TaskDagNode.dag_id == dag_id,
                    TaskDagNode.node_key == node_key,
                )
            )
            node = result.scalar_one()
            node.status = orchestrator_status
            node.orchestrator_task_id = orchestrator_task_id
            node.orchestrator_status = orchestrator_status
            node.updated_at = utc_now()
            await session.commit()
            await session.refresh(node)
            return node

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

    async def _get_task_dag(self, session: AsyncSession, dag_id: str) -> TaskDag:
        result = await session.execute(
            select(TaskDag)
            .where(TaskDag.id == dag_id)
            .options(selectinload(TaskDag.nodes))
        )
        return result.scalar_one()
