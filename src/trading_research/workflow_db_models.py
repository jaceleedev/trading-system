"""Durable local orchestration checkpoints; no execution or balance ledger."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_research.database import Base


class WorkflowRow(Base):
    __tablename__ = "operation_workflows"
    __table_args__ = (
        CheckConstraint("mode IN ('prospective','synthetic')", name="ck_workflow_mode"),
        CheckConstraint(
            "status IN ('active','paused','attention','completed')", name="ck_workflow_status"
        ),
        CheckConstraint("revision >= 1 AND step_sequence >= 0", name="ck_workflow_revision"),
        Index("ix_workflows_workspace", "workspace_key", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(64))
    account_seq: Mapped[str] = mapped_column(String(19))
    mode: Mapped[str] = mapped_column(String(16))
    seed: Mapped[dict] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(Integer)
    step_sequence: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WorkflowStepRow(Base):
    __tablename__ = "operation_workflow_steps"
    __table_args__ = (
        UniqueConstraint("workspace_key", "request_key", name="uq_workflow_step_request"),
        UniqueConstraint("workflow_id", "sequence", name="uq_workflow_step_sequence"),
        CheckConstraint(
            "kind IN ('funding_refresh','capital_plan','reservation','order_intent',"
            "'order_observation','reconciliation','control')",
            name="ck_workflow_step_kind",
        ),
        CheckConstraint(
            "state IN ('prepared','running','succeeded','needs_check')",
            name="ck_workflow_step_state",
        ),
        CheckConstraint(
            "sequence >= 1 AND attempt_count BETWEEN 0 AND 100", name="ck_workflow_step_counts"
        ),
        CheckConstraint(
            "(state = 'running' AND token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (state <> 'running' AND token IS NULL AND lease_expires_at IS NULL)",
            name="ck_workflow_step_lease",
        ),
        Index("ix_workflow_steps_workflow", "workspace_key", "workflow_id", "sequence"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(64))
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("operation_workflows.id", ondelete="CASCADE")
    )
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    input: Mapped[dict] = mapped_column(JSONB)
    request_key: Mapped[str] = mapped_column(String(128))
    request_sha256: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    attempt_count: Mapped[int] = mapped_column(Integer)
    token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    receipt: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
