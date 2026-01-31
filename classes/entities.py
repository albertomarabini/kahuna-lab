# classes/entities.py
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import create_engine, Column, Integer, String, Text, ForeignKey
from sqlalchemy.orm import Mapped, declarative_base, mapped_column
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    text,
    func,
    Index,
    Numeric,
    JSON
)

from typing import TypeAlias
DecimalString: TypeAlias = str
UUID: TypeAlias = str
Base = declarative_base()

Timestamp: TypeAlias = str

class TimestampMixin:
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

class User(Base):
    __tablename__ = "user"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=False), primary_key=True)
    created_at: Mapped[Timestamp] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[Timestamp] = mapped_column(DateTime(timezone=False), nullable=False)

class QueueMessage(Base):
    __tablename__ = "queue_messages"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    sender_id = Column(String, nullable=False)      # UUID string
    receiver_id = Column(String, nullable=False)    # UUID string
    type = Column(String, nullable=False)
    payload = Column(JSON, nullable=False)

    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

class Project(Base, TimestampMixin):
    __tablename__ = "project"

    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid4()),
    )

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=False),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )

    # NEW: required name + optional description
    name: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default=text("''"),
    )
    description: Mapped[str | None] = mapped_column(Text)

    prompt: Mapped[str | None] = mapped_column(Text)

    pre_flight: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    running: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    completed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    current_step: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )

    pre_flight_cost: Mapped[DecimalString] = mapped_column(
        Numeric(precision=20, scale=6),
        nullable=False,
        server_default=text("0"),
    )
    dev_cost: Mapped[DecimalString] = mapped_column(
        Numeric(precision=20, scale=6),
        nullable=False,
        server_default=text("0"),
    )

    state_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String)
    completion_report: Mapped[str | None] = mapped_column(Text)
    completion_zip_path: Mapped[str | None] = mapped_column(Text)

    deleted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )

    # NEW: structured JSON blobs
    preliminary_schema: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    bss_schema: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    pre_flight_data: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )

    # freeform JSON metadata (NOTE: name is metadata_json, not metadata)
    metadata_json: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    preflight_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))

    __table_args__ = (
        Index("ix_project_user_id", "user_id"),
        Index(
            "ix_project_user_id_not_deleted",
            "user_id",
            postgresql_where=text("deleted IS FALSE"),
        ),
    )


class Job(Base):
    __tablename__ = "job"

    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid4()),
    )

    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=False),
        ForeignKey("project.project_id", ondelete="CASCADE"),
        nullable=False,
    )

    prompt: Mapped[str | None] = mapped_column(Text)

    # Arbitrary structured input/output/whatever
    payload: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )

    # Free-form state vs status (you can specialize later if needed)
    state: Mapped[str | None] = mapped_column(String)
    status: Mapped[str | None] = mapped_column(String)

    # Serialized model configuration (kept as text per your request)
    model_config: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[Timestamp] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[Timestamp] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    heartbeat_at: Mapped[Timestamp | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[Timestamp | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Timestamp | None] = mapped_column(DateTime(timezone=True))

    runner_pid: Mapped[int | None] = mapped_column(Integer)


class PendingCharge(Base, TimestampMixin):
    __tablename__ = "pending_charge"

    id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid4()),
    )

    # External idempotency key (what your subsystem will call us with)
    idempotency_key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
    )

    # Who to charge
    user_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False),
        nullable=False,
    )

    # Optional context
    project_id: Mapped[str | None] = mapped_column(
        PGUUID(as_uuid=False),
        ForeignKey("project.project_id", ondelete="SET NULL"),
        nullable=True,
    )

    job_id: Mapped[str | None] = mapped_column(
        PGUUID(as_uuid=False),
        ForeignKey("job.job_id", ondelete="SET NULL"),
        nullable=True,
    )

    # What to charge (amount in accounting currency)
    amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2),
        nullable=False,
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    # State of this pending charge
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="PENDING",  # PENDING, CHARGED, FAILED
    )

    # Result / audit
    ledger_entry_id: Mapped[str | None] = mapped_column(
        PGUUID(as_uuid=False),
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    charged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_pending_charge_status", "status"),
    )
