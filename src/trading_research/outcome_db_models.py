"""Idempotent registration of immutable local outcome reports."""

from datetime import datetime

from sqlalchemy import DateTime, Index, PrimaryKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from trading_research.database import Base


class OutcomeRegistrationRow(Base):
    __tablename__ = "outcome_reports"
    __table_args__ = (
        PrimaryKeyConstraint("workspace_key", "request_key", name="uq_outcome_report_request"),
        Index("ix_outcome_reports_workspace", "workspace_key", "created_at"),
    )
    workspace_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    request_sha256: Mapped[str] = mapped_column(String(64))
    report_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
