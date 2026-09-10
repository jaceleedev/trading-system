"""Disabled order management records, separate from broker observations."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_research.database import Base


class OrderIntentRow(Base):
    __tablename__ = "order_intents"
    __table_args__ = (
        CheckConstraint("mode IN ('prospective','synthetic')", name="ck_order_intent_mode"),
        CheckConstraint("status IN ('active','aborted')", name="ck_order_intent_status"),
        CheckConstraint("revision >= 1 AND event_sequence >= 0", name="ck_order_intent_revision"),
        CheckConstraint(
            "status <> 'aborted' OR NOT reservation_held", name="ck_order_intent_attachment"
        ),
        Index("ix_order_intents_workspace", "workspace_key", "created_at"),
        Index(
            "uq_order_reservation_attachment",
            "reservation_id",
            unique=True,
            postgresql_where=text("reservation_held"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(64))
    plan_id: Mapped[str] = mapped_column(String(64))
    alternative_id: Mapped[str] = mapped_column(String(64))
    reservation_id: Mapped[str] = mapped_column(ForeignKey("funding_reservations.id"))
    account_seq: Mapped[str] = mapped_column(String(19))
    mode: Mapped[str] = mapped_column(String(16))
    seed: Mapped[dict] = mapped_column(JSONB)
    legs: Mapped[list] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(Integer)
    event_sequence: Mapped[int] = mapped_column(Integer)
    reservation_held: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrderOperationRow(Base):
    __tablename__ = "order_operations"
    __table_args__ = (
        UniqueConstraint("intent_id", "sequence", name="uq_order_operation_sequence"),
        CheckConstraint("kind IN ('create','modify','cancel')", name="ck_order_operation_kind"),
        CheckConstraint(
            "state IN ('prepared','dispatching','acknowledged','rejected','ambiguous','aborted')",
            name="ck_order_operation_state",
        ),
        CheckConstraint(
            "sequence BETWEEN 1 AND 100 AND leg_index BETWEEN 0 AND 49",
            name="ck_order_operation_bounds",
        ),
        Index("ix_order_operations_intent", "workspace_key", "intent_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(64))
    intent_id: Mapped[str] = mapped_column(ForeignKey("order_intents.id", ondelete="CASCADE"))
    sequence: Mapped[int] = mapped_column(Integer)
    leg_index: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16))
    prepared: Mapped[dict] = mapped_column(JSONB)
    outcome: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    dispatch_token: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrderEventRow(Base):
    __tablename__ = "order_events"
    __table_args__ = (
        UniqueConstraint("intent_id", "sequence", name="uq_order_event_sequence"),
        UniqueConstraint("workspace_key", "request_key", name="uq_order_event_request"),
        CheckConstraint("sequence >= 1", name="ck_order_event_sequence"),
        Index("ix_order_events_intent", "workspace_key", "intent_id", "sequence"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(64))
    intent_id: Mapped[str] = mapped_column(ForeignKey("order_intents.id", ondelete="CASCADE"))
    operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("order_operations.id", ondelete="CASCADE")
    )
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    request_key: Mapped[str | None] = mapped_column(String(128))
    request_sha256: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
