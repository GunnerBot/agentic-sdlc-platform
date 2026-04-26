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
    return datetime.now(UTC)


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
