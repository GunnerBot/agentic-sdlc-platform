from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, TypeDecorator


class JsonDocument(TypeDecorator[dict[str, object]]):
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB)
        return dialect.type_descriptor(JSON)


class Base(DeclarativeBase):
    pass


def new_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class InboundEvent(Base):
    __tablename__ = "inbound_events"
    __table_args__ = (
        UniqueConstraint("source", "delivery_id", name="uq_inbound_events_source_delivery"),
        Index("ix_inbound_events_source_status", "source", "status"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(nullable=False)
    delivery_id: Mapped[str] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False, default="received")
    payload_json: Mapped[dict[str, object]] = mapped_column(
        MutableDict.as_mutable(JsonDocument),
        nullable=False,
        default=dict,
    )
    received_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)
    processed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    tasks: Mapped[list["Task"]] = relationship(back_populates="inbound_event")


class RepositoryRecord(Base):
    __tablename__ = "repositories"
    __table_args__ = (
        UniqueConstraint("name", name="uq_repositories_name"),
        Index("ix_repositories_provider_status", "provider", "status"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(nullable=False)
    provider: Mapped[str] = mapped_column(nullable=False)
    clone_url: Mapped[str | None] = mapped_column(nullable=True)
    default_branch: Mapped[str] = mapped_column(nullable=False, default="main")
    status: Mapped[str] = mapped_column(nullable=False, default="active")
    metadata_json: Mapped[dict[str, object]] = mapped_column(
        MutableDict.as_mutable(JsonDocument),
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)


class RepoIndexJob(Base):
    __tablename__ = "repo_index_jobs"
    __table_args__ = (
        Index("ix_repo_index_jobs_repo_status", "repo_name", "status"),
        Index("ix_repo_index_jobs_external_index_id", "external_index_id"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    repo_name: Mapped[str] = mapped_column(nullable=False)
    provider: Mapped[str] = mapped_column(nullable=False)
    external_index_id: Mapped[str | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(nullable=False, default="queued")
    error: Mapped[str | None] = mapped_column(nullable=True)
    metadata_json: Mapped[dict[str, object]] = mapped_column(
        MutableDict.as_mutable(JsonDocument),
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_tasks_source_external"),
        Index("ix_tasks_status", "status"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    inbound_event_id: Mapped[str] = mapped_column(
        ForeignKey("inbound_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(nullable=False)
    external_id: Mapped[str] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(nullable=False)
    repo: Mapped[str | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(nullable=False, default="queued")
    orchestrator_task_id: Mapped[str | None] = mapped_column(nullable=True)
    orchestrator_status: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)

    inbound_event: Mapped[InboundEvent] = relationship(back_populates="tasks")
    dags: Mapped[list["TaskDag"]] = relationship(back_populates="task")
    sessions: Mapped[list["AgentSession"]] = relationship(back_populates="task")


class TaskDag(Base):
    __tablename__ = "task_dags"
    __table_args__ = (Index("ix_task_dags_task_id", "task_id"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(nullable=False, default="planned")
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)

    task: Mapped[Task] = relationship(back_populates="dags")
    nodes: Mapped[list["TaskDagNode"]] = relationship(
        back_populates="dag",
        cascade="all, delete-orphan",
        order_by="TaskDagNode.position",
    )


class TaskDagNode(Base):
    __tablename__ = "task_dag_nodes"
    __table_args__ = (
        UniqueConstraint("dag_id", "node_key", name="uq_task_dag_nodes_dag_node_key"),
        Index("ix_task_dag_nodes_dag_status", "dag_id", "status"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    dag_id: Mapped[str] = mapped_column(
        ForeignKey("task_dags.id", ondelete="CASCADE"),
        nullable=False,
    )
    node_key: Mapped[str] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(nullable=False)
    repo: Mapped[str | None] = mapped_column(nullable=True)
    depends_on_json: Mapped[dict[str, object]] = mapped_column(
        MutableDict.as_mutable(JsonDocument),
        nullable=False,
        default=dict,
    )
    status: Mapped[str] = mapped_column(nullable=False, default="ready")
    orchestrator_task_id: Mapped[str | None] = mapped_column(nullable=True)
    orchestrator_status: Mapped[str | None] = mapped_column(nullable=True)
    position: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)

    dag: Mapped[TaskDag] = relationship(back_populates="nodes")

    @property
    def depends_on(self) -> tuple[str, ...]:
        values = self.depends_on_json.get("nodes", [])
        if not isinstance(values, list):
            return ()
        return tuple(value for value in values if isinstance(value, str))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_target", "target_type", "target_id"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    action: Mapped[str] = mapped_column(nullable=False)
    actor: Mapped[str] = mapped_column(nullable=False)
    target_type: Mapped[str] = mapped_column(nullable=False)
    target_id: Mapped[str] = mapped_column(nullable=False)
    metadata_json: Mapped[dict[str, object]] = mapped_column(
        MutableDict.as_mutable(JsonDocument),
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "external_thread_id",
            name="uq_agent_sessions_provider_thread",
        ),
        Index("ix_agent_sessions_task_id", "task_id"),
        Index("ix_agent_sessions_status", "status"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(nullable=False)
    external_thread_id: Mapped[str] = mapped_column(nullable=False)
    hermes_session_id: Mapped[str | None] = mapped_column(nullable=True)
    repo: Mapped[str | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(nullable=False, default="active")
    context_summary: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)

    task: Mapped[Task] = relationship(back_populates="sessions")
    events: Mapped[list["SessionEvent"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="SessionEvent.created_at",
    )


class SessionEvent(Base):
    __tablename__ = "session_events"
    __table_args__ = (
        Index("ix_session_events_session_id", "session_id"),
        Index("ix_session_events_type", "event_type"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    direction: Mapped[str] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(nullable=False)
    actor: Mapped[str] = mapped_column(nullable=False)
    message: Mapped[str | None] = mapped_column(nullable=True)
    metadata_json: Mapped[dict[str, object]] = mapped_column(
        MutableDict.as_mutable(JsonDocument),
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)

    session: Mapped[AgentSession] = relationship(back_populates="events")
