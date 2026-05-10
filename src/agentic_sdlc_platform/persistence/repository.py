from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from agentic_sdlc_platform.glue.dag_decomposer import Subtask
from agentic_sdlc_platform.persistence.models import (
    AgentSession,
    AuditEvent,
    DagNodeExecution,
    InboundEvent,
    RepoIndexJob,
    RepositoryRecord,
    SessionEvent,
    Task,
    TaskArtifact,
    TaskDag,
    TaskDagNode,
    WorkspaceGitHubInstallation,
    utc_now,
)

DEPENDENCY_COMPLETE_STATUSES = {"completed", "skipped"}
ACTIVE_EXECUTION_STATUSES = {"queued", "running", "needs_input"}


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

    async def list_audit_events_for_targets(
        self,
        target_ids: list[str],
    ) -> list[AuditEvent]:
        if not target_ids:
            return []
        async with self._session_factory() as session:
            result = await session.execute(
                select(AuditEvent)
                .where(AuditEvent.target_id.in_(target_ids))
                .order_by(AuditEvent.created_at, AuditEvent.id)
            )
            return list(result.scalars().all())

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
            result = await session.execute(
                select(Task)
                .where(Task.external_id == external_id)
                .options(
                    selectinload(Task.dags).selectinload(TaskDag.nodes),
                    selectinload(Task.dags)
                    .selectinload(TaskDag.nodes)
                    .selectinload(TaskDagNode.executions),
                    selectinload(Task.sessions).selectinload(AgentSession.events),
                    selectinload(Task.artifacts),
                )
            )
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

    async def update_task_repo_and_status(
        self,
        task_id: str,
        repo: str,
        status: str,
    ) -> Task:
        async with self._session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise LookupError(f"task {task_id} not found")
            task.repo = repo
            task.status = status
            task.updated_at = utc_now()
            await session.commit()
            await session.refresh(task)
            return task

    async def upsert_repo(
        self,
        name: str,
        provider: str,
        clone_url: str | None,
        default_branch: str,
        metadata: dict[str, object] | None,
        status: str = "active",
    ) -> RepositoryRecord:
        async with self._session_factory() as session:
            result = await session.execute(
                select(RepositoryRecord).where(RepositoryRecord.name == name)
            )
            repo = result.scalars().first()
            if repo is None:
                repo = RepositoryRecord(
                    name=name,
                    provider=provider,
                    clone_url=clone_url,
                    default_branch=default_branch,
                    metadata_json=metadata or {},
                    status=status,
                )
                session.add(repo)
            else:
                repo.provider = provider
                repo.clone_url = clone_url
                repo.default_branch = default_branch
                repo.metadata_json = metadata or {}
                repo.status = status
                repo.updated_at = utc_now()
            await session.commit()
            await session.refresh(repo)
            return repo

    async def list_repos(
        self,
        provider: str | None = None,
        status: str | None = None,
    ) -> list[RepositoryRecord]:
        async with self._session_factory() as session:
            statement = select(RepositoryRecord).order_by(RepositoryRecord.name)
            if provider:
                statement = statement.where(RepositoryRecord.provider == provider)
            if status:
                statement = statement.where(RepositoryRecord.status == status)
            result = await session.execute(statement)
            return list(result.scalars().all())

    async def get_repo_by_name(self, name: str) -> RepositoryRecord | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(RepositoryRecord).where(RepositoryRecord.name == name)
            )
            return result.scalars().first()

    async def upsert_github_installation(
        self,
        workspace_id: str,
        installation_id: str,
        account: str | None,
        repository_selection: str,
        permissions: dict[str, object] | None,
        status: str = "active",
        metadata: dict[str, object] | None = None,
    ) -> WorkspaceGitHubInstallation:
        async with self._session_factory() as session:
            result = await session.execute(
                select(WorkspaceGitHubInstallation).where(
                    WorkspaceGitHubInstallation.workspace_id == workspace_id,
                    WorkspaceGitHubInstallation.installation_id == installation_id,
                )
            )
            installation = result.scalars().first()
            if installation is None:
                installation = WorkspaceGitHubInstallation(
                    workspace_id=workspace_id,
                    provider="github",
                    installation_id=installation_id,
                    account=account,
                    repository_selection=repository_selection,
                    permissions_json=permissions or {},
                    status=status,
                    metadata_json=metadata or {},
                )
                session.add(installation)
            else:
                installation.account = account
                installation.repository_selection = repository_selection
                installation.permissions_json = permissions or {}
                installation.status = status
                installation.metadata_json = metadata or {}
                installation.updated_at = utc_now()
            await session.commit()
            await session.refresh(installation)
            return installation

    async def list_github_installations(
        self,
        workspace_id: str | None = None,
        status: str | None = None,
    ) -> list[WorkspaceGitHubInstallation]:
        async with self._session_factory() as session:
            statement = select(WorkspaceGitHubInstallation).order_by(
                WorkspaceGitHubInstallation.workspace_id,
                WorkspaceGitHubInstallation.installation_id,
            )
            if workspace_id is not None:
                statement = statement.where(
                    WorkspaceGitHubInstallation.workspace_id == workspace_id
                )
            if status is not None:
                statement = statement.where(WorkspaceGitHubInstallation.status == status)
            result = await session.execute(statement)
            return list(result.scalars().all())

    async def create_repo_index_job(
        self,
        repo_name: str,
        provider: str,
        metadata: dict[str, object] | None = None,
    ) -> RepoIndexJob:
        async with self._session_factory() as session:
            job = RepoIndexJob(
                repo_name=repo_name,
                provider=provider,
                metadata_json=metadata or {},
            )
            session.add(job)
            await session.commit()
            await session.refresh(job)
            return job

    async def mark_repo_index_job_completed(
        self,
        job_id: str,
        external_index_id: str,
        status: str,
    ) -> RepoIndexJob:
        async with self._session_factory() as session:
            job = await session.get(RepoIndexJob, job_id)
            if job is None:
                raise LookupError(f"repo index job {job_id} not found")
            job.external_index_id = external_index_id
            job.status = status
            job.updated_at = utc_now()
            await session.commit()
            await session.refresh(job)
            return job

    async def mark_repo_index_job_failed(self, job_id: str, error: str) -> RepoIndexJob:
        async with self._session_factory() as session:
            job = await session.get(RepoIndexJob, job_id)
            if job is None:
                raise LookupError(f"repo index job {job_id} not found")
            job.status = "failed"
            job.error = error
            job.updated_at = utc_now()
            await session.commit()
            await session.refresh(job)
            return job

    async def list_repo_index_jobs(self, repo_name: str | None = None) -> list[RepoIndexJob]:
        async with self._session_factory() as session:
            statement = select(RepoIndexJob).order_by(RepoIndexJob.created_at.desc())
            if repo_name:
                statement = statement.where(RepoIndexJob.repo_name == repo_name)
            result = await session.execute(statement)
            return list(result.scalars().all())

    async def list_tasks(
        self,
        source: str | None = None,
        repo: str | None = None,
        status: str | None = None,
    ) -> list[Task]:
        async with self._session_factory() as session:
            statement = (
                select(Task)
                .options(
                    selectinload(Task.dags).selectinload(TaskDag.nodes),
                    selectinload(Task.dags)
                    .selectinload(TaskDag.nodes)
                    .selectinload(TaskDagNode.executions),
                    selectinload(Task.sessions).selectinload(AgentSession.events),
                    selectinload(Task.artifacts),
                )
                .order_by(Task.created_at.desc(), Task.id)
            )
            if source:
                statement = statement.where(Task.source == source)
            if repo:
                statement = statement.where(Task.repo == repo)
            if status:
                statement = statement.where(Task.status == status)
            result = await session.execute(statement)
            return list(result.scalars().all())

    async def get_task(self, task_id: str) -> Task | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(Task)
                .where(Task.id == task_id)
                .options(
                    selectinload(Task.dags).selectinload(TaskDag.nodes),
                    selectinload(Task.dags)
                    .selectinload(TaskDag.nodes)
                    .selectinload(TaskDagNode.executions),
                    selectinload(Task.sessions).selectinload(AgentSession.events),
                    selectinload(Task.artifacts),
                )
            )
            return result.scalars().first()

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
                        metadata_json={
                            **subtask.metadata,
                            "acceptance_criteria": list(subtask.acceptance_criteria),
                        },
                    )
                )
            session.add(dag)
            await session.commit()
            return await self._get_task_dag(session, dag.id)

    async def add_task_dag_node(
        self,
        dag_id: str,
        subtask: Subtask,
    ) -> TaskDagNode:
        async with self._session_factory() as session:
            dag = await session.get(TaskDag, dag_id)
            if dag is None:
                raise LookupError(f"dag {dag_id} not found")
            existing = await session.execute(
                select(TaskDagNode).where(
                    TaskDagNode.dag_id == dag_id,
                    TaskDagNode.node_key == subtask.id,
                )
            )
            node = existing.scalars().first()
            if node is not None:
                return node
            max_position_result = await session.execute(
                select(TaskDagNode.position)
                .where(TaskDagNode.dag_id == dag_id)
                .order_by(TaskDagNode.position.desc())
                .limit(1)
            )
            max_position = max_position_result.scalars().first()
            node = TaskDagNode(
                dag_id=dag_id,
                node_key=subtask.id,
                title=subtask.title,
                repo=subtask.repo,
                depends_on_json={"nodes": list(subtask.depends_on)},
                status="blocked" if subtask.depends_on else "ready",
                position=(max_position + 1) if isinstance(max_position, int) else 0,
                metadata_json={
                    **subtask.metadata,
                    "acceptance_criteria": list(subtask.acceptance_criteria),
                },
            )
            session.add(node)
            dag.status = "planned"
            dag.updated_at = utc_now()
            await session.commit()
            await session.refresh(node)
            return node

    async def get_task_dag(self, dag_id: str) -> TaskDag | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TaskDag)
                .where(TaskDag.id == dag_id)
                .options(
                    selectinload(TaskDag.nodes),
                    selectinload(TaskDag.nodes).selectinload(TaskDagNode.executions),
                    selectinload(TaskDag.task).selectinload(Task.sessions),
                )
            )
            return result.scalars().first()

    async def update_task_dag_status(self, dag_id: str, status: str) -> TaskDag:
        async with self._session_factory() as session:
            dag = await session.get(TaskDag, dag_id)
            if dag is None:
                raise LookupError(f"dag {dag_id} not found")
            dag.status = status
            dag.updated_at = utc_now()
            await session.commit()
            return await self._get_task_dag(session, dag_id)

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

            completed = _completed_dependency_keys(dag.nodes)
            ready_nodes = []
            for node in dag.nodes:
                if node.status not in {"ready", "blocked"}:
                    continue
                if all(dependency in completed for dependency in node.depends_on):
                    ready_nodes.append(node)
            return ready_nodes

    async def list_ready_dag_nodes_for_dag(self, dag_id: str) -> list[TaskDagNode]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TaskDag).where(TaskDag.id == dag_id).options(selectinload(TaskDag.nodes))
            )
            dag = result.scalars().first()
            if dag is None:
                return []

            completed = _completed_dependency_keys(dag.nodes)
            ready_nodes = []
            for node in dag.nodes:
                if node.status not in {"ready", "blocked"}:
                    continue
                if all(dependency in completed for dependency in node.depends_on):
                    ready_nodes.append(node)
            return ready_nodes

    async def mark_dag_node_completed(
        self,
        dag_id: str,
        node_key: str,
        orchestrator_status: str | None = None,
    ) -> TaskDagNode:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TaskDagNode).where(
                    TaskDagNode.dag_id == dag_id,
                    TaskDagNode.node_key == node_key,
                )
            )
            node = result.scalar_one()
            node.status = "completed"
            if orchestrator_status is not None:
                node.orchestrator_status = orchestrator_status
            node.updated_at = utc_now()
            await session.commit()
            await session.refresh(node)
            return node

    async def update_dag_node_status(
        self,
        dag_id: str,
        node_key: str,
        status: str,
        orchestrator_status: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> TaskDagNode:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TaskDagNode).where(
                    TaskDagNode.dag_id == dag_id,
                    TaskDagNode.node_key == node_key,
                )
            )
            node = result.scalar_one()
            node.status = status
            if orchestrator_status is not None:
                node.orchestrator_status = orchestrator_status
            if metadata is not None:
                node.metadata_json.update(metadata)
            node.updated_at = utc_now()
            await session.commit()
            await session.refresh(node)
            return node

    async def refresh_dag_completion_status(self, dag_id: str) -> TaskDag:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TaskDag)
                .where(TaskDag.id == dag_id)
                .options(selectinload(TaskDag.nodes), selectinload(TaskDag.task))
            )
            dag = result.scalar_one()
            node_statuses = {node.status for node in dag.nodes}
            if node_statuses and node_statuses <= {"completed", "skipped"}:
                dag.status = "completed"
                dag.task.status = "completed"
                dag.task.updated_at = utc_now()
            elif "failed" in node_statuses:
                dag.status = "failed"
                dag.task.status = "failed"
                dag.task.updated_at = utc_now()
            dag.updated_at = utc_now()
            await session.commit()
            return await self._get_task_dag(session, dag_id)

    async def update_dag_node_metadata(
        self,
        dag_id: str,
        node_key: str,
        metadata: dict[str, object],
    ) -> TaskDagNode:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TaskDagNode).where(
                    TaskDagNode.dag_id == dag_id,
                    TaskDagNode.node_key == node_key,
                )
            )
            node = result.scalar_one()
            node.metadata_json.update(metadata)
            node.updated_at = utc_now()
            await session.commit()
            await session.refresh(node)
            return node

    async def mark_dag_node_failed(
        self,
        dag_id: str,
        node_key: str,
        error: str,
    ) -> TaskDagNode:
        return await self.update_dag_node_status(
            dag_id=dag_id,
            node_key=node_key,
            status="failed",
            orchestrator_status="failed",
            metadata={"failure_error": error},
        )

    async def mark_dag_node_skipped(self, dag_id: str, node_key: str) -> TaskDagNode:
        return await self.update_dag_node_status(
            dag_id=dag_id,
            node_key=node_key,
            status="skipped",
            orchestrator_status="skipped",
        )

    async def retry_dag_node(self, dag_id: str, node_key: str) -> TaskDagNode:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TaskDagNode).where(
                    TaskDagNode.dag_id == dag_id,
                    TaskDagNode.node_key == node_key,
                )
            )
            node = result.scalar_one()
            retry_count = node.metadata_json.get("retry_count", 0)
            retry_count = retry_count if isinstance(retry_count, int) else 0
            node.status = "ready"
            node.orchestrator_status = None
            node.orchestrator_task_id = None
            node.metadata_json.update(
                {
                    "retry_count": retry_count + 1,
                    "failure_error": None,
                }
            )
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
        metadata: dict[str, object] | None = None,
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
            if metadata is not None:
                node.metadata_json.update(metadata)
            node.updated_at = utc_now()
            await session.commit()
            await session.refresh(node)
            return node

    async def create_dag_node_execution(
        self,
        dag_id: str,
        node_key: str,
        task_id: str,
        executor_provider: str,
        status: str,
        branch_name: str | None = None,
        workspace_path: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> DagNodeExecution:
        async with self._session_factory() as session:
            existing = await self._find_active_execution(
                session=session,
                dag_id=dag_id,
                node_key=node_key,
            )
            if existing is not None:
                return existing
            execution = DagNodeExecution(
                dag_id=dag_id,
                node_key=node_key,
                task_id=task_id,
                executor_provider=executor_provider,
                status=status,
                branch_name=branch_name,
                workspace_path=workspace_path,
                metadata_json=metadata or {},
            )
            session.add(execution)
            await session.commit()
            await session.refresh(execution)
            return execution

    async def update_dag_node_execution(
        self,
        execution_id: str,
        status: str,
        external_execution_id: str | None = None,
        branch_name: str | None = None,
        pr_url: str | None = None,
        pr_number: int | None = None,
        workspace_path: str | None = None,
        error: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> DagNodeExecution:
        async with self._session_factory() as session:
            execution = await session.get(DagNodeExecution, execution_id)
            if execution is None:
                raise LookupError(f"dag node execution {execution_id} not found")
            execution.status = status
            if external_execution_id is not None:
                execution.external_execution_id = external_execution_id
            if branch_name is not None:
                execution.branch_name = branch_name
            if pr_url is not None:
                execution.pr_url = pr_url
            if pr_number is not None:
                execution.pr_number = pr_number
            if workspace_path is not None:
                execution.workspace_path = workspace_path
            if error is not None:
                execution.error = error
            if metadata is not None:
                execution.metadata_json.update(metadata)
            execution.updated_at = utc_now()
            session.add(
                TaskArtifact(
                    task_id=execution.task_id,
                    dag_id=execution.dag_id,
                    node_key=execution.node_key,
                    execution_id=execution.id,
                    kind="dag_node_execution_result",
                    name=f"{execution.node_key}:{status}",
                    content_json=_execution_snapshot(execution),
                    metadata_json={
                        "status": status,
                        "executor_provider": execution.executor_provider,
                    },
                )
            )
            await session.commit()
            await session.refresh(execution)
            return execution

    async def create_task_artifact(
        self,
        task_id: str,
        kind: str,
        name: str,
        content: dict[str, object],
        metadata: dict[str, object] | None = None,
        dag_id: str | None = None,
        node_key: str | None = None,
        execution_id: str | None = None,
    ) -> TaskArtifact:
        async with self._session_factory() as session:
            artifact = TaskArtifact(
                task_id=task_id,
                dag_id=dag_id,
                node_key=node_key,
                execution_id=execution_id,
                kind=kind,
                name=name,
                content_json=content,
                metadata_json=metadata or {},
            )
            session.add(artifact)
            await session.commit()
            await session.refresh(artifact)
            return artifact

    async def list_task_artifacts(
        self,
        task_id: str,
        kind: str | None = None,
        dag_id: str | None = None,
        node_key: str | None = None,
        execution_id: str | None = None,
    ) -> list[TaskArtifact]:
        async with self._session_factory() as session:
            statement = (
                select(TaskArtifact)
                .where(TaskArtifact.task_id == task_id)
                .order_by(TaskArtifact.created_at.desc(), TaskArtifact.id)
            )
            if kind is not None:
                statement = statement.where(TaskArtifact.kind == kind)
            if dag_id is not None:
                statement = statement.where(TaskArtifact.dag_id == dag_id)
            if node_key is not None:
                statement = statement.where(TaskArtifact.node_key == node_key)
            if execution_id is not None:
                statement = statement.where(TaskArtifact.execution_id == execution_id)
            result = await session.execute(statement)
            return list(result.scalars().all())

    async def list_dag_node_executions(
        self,
        dag_id: str,
        node_key: str | None = None,
    ) -> list[DagNodeExecution]:
        async with self._session_factory() as session:
            statement = (
                select(DagNodeExecution)
                .where(DagNodeExecution.dag_id == dag_id)
                .order_by(DagNodeExecution.created_at.desc(), DagNodeExecution.id)
            )
            if node_key is not None:
                statement = statement.where(DagNodeExecution.node_key == node_key)
            result = await session.execute(statement)
            return list(result.scalars().all())

    async def list_active_dag_node_executions(
        self,
        task_id: str | None = None,
    ) -> list[DagNodeExecution]:
        async with self._session_factory() as session:
            statement = (
                select(DagNodeExecution)
                .where(DagNodeExecution.status.in_(ACTIVE_EXECUTION_STATUSES))
                .order_by(DagNodeExecution.created_at.desc(), DagNodeExecution.id)
            )
            if task_id is not None:
                statement = statement.where(DagNodeExecution.task_id == task_id)
            result = await session.execute(statement)
            return list(result.scalars().all())

    async def list_orchestrated_dag_nodes(
        self,
        *,
        statuses: tuple[str, ...] = ("queued", "running"),
        limit: int = 50,
    ) -> list[TaskDagNode]:
        async with self._session_factory() as session:
            statement = (
                select(TaskDagNode)
                .join(TaskDag, TaskDag.id == TaskDagNode.dag_id)
                .where(
                    TaskDag.status != "superseded",
                    TaskDagNode.orchestrator_task_id.is_not(None),
                    TaskDagNode.status.in_(statuses),
                )
                .options(
                    selectinload(TaskDagNode.dag).selectinload(TaskDag.nodes),
                    selectinload(TaskDagNode.dag)
                    .selectinload(TaskDag.task)
                    .selectinload(Task.sessions),
                )
                .order_by(TaskDagNode.updated_at, TaskDagNode.id)
                .limit(limit)
            )
            result = await session.execute(statement)
            return list(result.scalars().all())

    async def create_agent_session(
        self,
        task_id: str,
        provider: str,
        external_thread_id: str,
        hermes_session_id: str | None,
        repo: str | None,
        orchestrator_provider: str | None = None,
        orchestrator_issue_id: str | None = None,
        orchestrator_task_id: str | None = None,
    ) -> AgentSession:
        async with self._session_factory() as session:
            agent_session = AgentSession(
                task_id=task_id,
                provider=provider,
                external_thread_id=external_thread_id,
                hermes_session_id=hermes_session_id,
                orchestrator_provider=orchestrator_provider,
                orchestrator_issue_id=orchestrator_issue_id,
                orchestrator_task_id=orchestrator_task_id,
                repo=repo,
            )
            session.add(agent_session)
            try:
                await session.commit()
                await session.refresh(agent_session)
                return agent_session
            except IntegrityError:
                await session.rollback()
                existing = await self._find_agent_session(session, provider, external_thread_id)
                existing.hermes_session_id = hermes_session_id or existing.hermes_session_id
                existing.orchestrator_provider = (
                    orchestrator_provider or existing.orchestrator_provider
                )
                existing.orchestrator_issue_id = (
                    orchestrator_issue_id or existing.orchestrator_issue_id
                )
                existing.orchestrator_task_id = (
                    orchestrator_task_id or existing.orchestrator_task_id
                )
                existing.repo = repo or existing.repo
                existing.updated_at = utc_now()
                await session.commit()
                await session.refresh(existing)
                return existing

    async def find_agent_session(
        self,
        provider: str,
        external_thread_id: str,
    ) -> AgentSession | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(AgentSession)
                .where(
                    AgentSession.provider == provider,
                    AgentSession.external_thread_id == external_thread_id,
                )
                .options(selectinload(AgentSession.events))
            )
            return result.scalars().first()

    async def get_agent_session(self, session_id: str) -> AgentSession | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(AgentSession)
                .where(AgentSession.id == session_id)
                .options(
                    selectinload(AgentSession.events),
                    selectinload(AgentSession.task),
                )
            )
            return result.scalars().first()

    async def list_orchestrator_backed_agent_sessions(
        self,
        limit: int = 50,
    ) -> list[AgentSession]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(AgentSession)
                .where(
                    AgentSession.status == "active",
                    AgentSession.orchestrator_task_id.is_not(None),
                    AgentSession.orchestrator_issue_id.is_not(None),
                )
                .options(
                    selectinload(AgentSession.events),
                    selectinload(AgentSession.task),
                )
                .order_by(AgentSession.updated_at, AgentSession.created_at, AgentSession.id)
                .limit(limit)
            )
            return list(result.scalars().all())

    async def record_session_event(
        self,
        session_id: str,
        direction: str,
        event_type: str,
        actor: str,
        message: str | None,
        metadata: dict[str, object] | None = None,
    ) -> SessionEvent:
        async with self._session_factory() as session:
            event = SessionEvent(
                session_id=session_id,
                direction=direction,
                event_type=event_type,
                actor=actor,
                message=message,
                metadata_json=metadata or {},
            )
            session.add(event)
            await session.commit()
            await session.refresh(event)
            return event

    async def list_session_events(self, session_id: str) -> list[SessionEvent]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(SessionEvent)
                .where(SessionEvent.session_id == session_id)
                .order_by(SessionEvent.created_at, SessionEvent.id)
            )
            return list(result.scalars().all())

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
            select(TaskDag).where(TaskDag.id == dag_id).options(selectinload(TaskDag.nodes))
        )
        return result.scalar_one()

    async def _find_agent_session(
        self,
        session: AsyncSession,
        provider: str,
        external_thread_id: str,
    ) -> AgentSession:
        result = await session.execute(
            select(AgentSession).where(
                AgentSession.provider == provider,
                AgentSession.external_thread_id == external_thread_id,
            )
        )
        return result.scalar_one()

    async def _find_active_execution(
        self,
        session: AsyncSession,
        dag_id: str,
        node_key: str,
    ) -> DagNodeExecution | None:
        result = await session.execute(
            select(DagNodeExecution).where(
                DagNodeExecution.dag_id == dag_id,
                DagNodeExecution.node_key == node_key,
                DagNodeExecution.status.in_(ACTIVE_EXECUTION_STATUSES),
            )
        )
        return result.scalars().first()


def _completed_dependency_keys(nodes: list[TaskDagNode]) -> set[str]:
    return {node.node_key for node in nodes if node.status in DEPENDENCY_COMPLETE_STATUSES}


def _execution_snapshot(execution: DagNodeExecution) -> dict[str, object]:
    return {
        "execution_id": execution.id,
        "dag_id": execution.dag_id,
        "node_key": execution.node_key,
        "task_id": execution.task_id,
        "executor_provider": execution.executor_provider,
        "external_execution_id": execution.external_execution_id,
        "status": execution.status,
        "branch_name": execution.branch_name,
        "pr_url": execution.pr_url,
        "pr_number": execution.pr_number,
        "workspace_path": execution.workspace_path,
        "error": execution.error,
        "metadata": dict(execution.metadata_json),
    }
