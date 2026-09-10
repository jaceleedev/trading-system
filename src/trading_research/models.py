from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_research import order_db_models as _order_db_models  # noqa: F401
from trading_research.database import Base


class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    manifest: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InstrumentRow(Base):
    __tablename__ = "instruments"
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB)


class BarRow(Base):
    __tablename__ = "daily_bars"
    __table_args__ = (
        ForeignKeyConstraint(
            ["dataset_id", "instrument_id"],
            ["instruments.dataset_id", "instruments.instrument_id"],
        ),
    )
    dataset_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    session_close_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    high: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    low: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    close: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    adjusted_close: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    volume: Mapped[Decimal] = mapped_column(Numeric(28, 10))


class FxRow(Base):
    __tablename__ = "fx_quotes"
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), primary_key=True)
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    krw_per_unit: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RecommendationRow(Base):
    __tablename__ = "recommendations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BacktestRow(Base):
    __tablename__ = "backtests"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvaluationRow(Base):
    __tablename__ = "evaluations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JobRow(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("workspace_key", "request_key", name="uq_jobs_workspace_request"),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')", name="ck_jobs_status"
        ),
        CheckConstraint(
            "max_attempts BETWEEN 1 AND 10 AND attempt_count BETWEEN 0 AND max_attempts",
            name="ck_jobs_attempts",
        ),
        CheckConstraint(
            "(status = 'running' AND attempt_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'running' AND attempt_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_jobs_lease",
        ),
        Index("ix_jobs_claim", "workspace_key", "status", "available_at", "created_at"),
        Index("ix_jobs_lease", "workspace_key", "status", "lease_expires_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(64))
    request_key: Mapped[str] = mapped_column(String(128))
    request_sha256: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(64))
    parameters: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_attempts: Mapped[int] = mapped_column(Integer)
    attempt_count: Mapped[int] = mapped_column(Integer)
    cancel_requested: Mapped[bool] = mapped_column(Boolean)
    attempt_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    error_code: Mapped[str | None] = mapped_column(String(64))


class JobAttemptRow(Base):
    __tablename__ = "job_attempts"
    __table_args__ = (
        UniqueConstraint("token", name="uq_job_attempts_token"),
        CheckConstraint(
            "status IN ('running','succeeded','failed','cancelled','lease_expired')",
            name="ck_job_attempts_status",
        ),
        CheckConstraint("number BETWEEN 1 AND 10", name="ck_job_attempts_number"),
    )
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    number: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(64))
    owner: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))


class InvestigationRow(Base):
    __tablename__ = "investigations"
    __table_args__ = (
        UniqueConstraint("workspace_key", "request_key", name="uq_investigations_request"),
        CheckConstraint("current_revision >= 1", name="ck_investigations_revision"),
        CheckConstraint("status IN ('active','paused')", name="ck_investigations_status"),
        CheckConstraint(
            "latest_completed_revision IS NULL OR "
            "latest_completed_revision BETWEEN 1 AND current_revision",
            name="ck_investigations_completed",
        ),
        Index("ix_investigations_due", "workspace_key", "next_review_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(64))
    request_key: Mapped[str] = mapped_column(String(128))
    request_sha256: Mapped[str] = mapped_column(String(64))
    current_revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    active_job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"))
    latest_completed_revision: Mapped[int | None] = mapped_column(Integer)
    latest_result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_conditions: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class InvestigationRevisionRow(Base):
    __tablename__ = "investigation_revisions"
    __table_args__ = (
        UniqueConstraint(
            "investigation_id", "request_key", name="uq_investigation_revision_request"
        ),
        UniqueConstraint("job_id", name="uq_investigation_revision_job"),
        CheckConstraint("number >= 1", name="ck_investigation_revision_number"),
    )
    investigation_id: Mapped[str] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), primary_key=True
    )
    number: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(64))
    request_key: Mapped[str] = mapped_column(String(128))
    request_sha256: Mapped[str] = mapped_column(String(64))
    trigger_kind: Mapped[str] = mapped_column(String(32))
    input_sha256: Mapped[str] = mapped_column(String(64))
    context_input: Mapped[dict] = mapped_column(JSONB)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FundingPoolRow(Base):
    __tablename__ = "funding_pools"
    __table_args__ = (
        CheckConstraint("kind IN ('cash','holding')", name="ck_funding_pool_kind"),
        CheckConstraint("mode IN ('prospective','synthetic')", name="ck_funding_pool_mode"),
        CheckConstraint("capacity IS NULL OR capacity >= 0", name="ck_funding_pool_capacity"),
        CheckConstraint("revision >= 1", name="ck_funding_pool_revision"),
        Index("ix_funding_pools_account", "workspace_key", "account_seq"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(16))
    account_seq: Mapped[str] = mapped_column(String(19))
    kind: Mapped[str] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(3))
    market: Mapped[str | None] = mapped_column(String(2))
    symbol: Mapped[str | None] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))
    snapshot_id: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    capacity: Mapped[Decimal | None] = mapped_column(Numeric())
    revision: Mapped[int] = mapped_column(Integer)
    basis: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FundingReservationRow(Base):
    __tablename__ = "funding_reservations"
    __table_args__ = (
        UniqueConstraint("workspace_key", "request_key", name="uq_funding_reservation_request"),
        CheckConstraint(
            "status IN ('active','released','replaced')", name="ck_funding_reservation_status"
        ),
        CheckConstraint("mode IN ('prospective','synthetic')", name="ck_funding_reservation_mode"),
        Index("ix_funding_reservations_account", "workspace_key", "account_seq", "status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(64))
    account_seq: Mapped[str] = mapped_column(String(19))
    plan_id: Mapped[str] = mapped_column(String(64))
    alternative_id: Mapped[str] = mapped_column(String(64))
    request_key: Mapped[str] = mapped_column(String(128))
    request_sha256: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    requirements: Mapped[dict] = mapped_column(JSONB)
    pool_revisions: Mapped[dict] = mapped_column(JSONB)
    snapshot_ids: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by: Mapped[str | None] = mapped_column(String(36))


class FundingReservationLineRow(Base):
    __tablename__ = "funding_reservation_lines"
    __table_args__ = (CheckConstraint("amount > 0", name="ck_funding_reservation_line_amount"),)
    reservation_id: Mapped[str] = mapped_column(
        ForeignKey("funding_reservations.id", ondelete="CASCADE"), primary_key=True
    )
    pool_id: Mapped[str] = mapped_column(ForeignKey("funding_pools.id"), primary_key=True)
    amount: Mapped[Decimal] = mapped_column(Numeric())


class CapitalPlanRegistrationRow(Base):
    __tablename__ = "capital_plan_registrations"
    __table_args__ = (
        PrimaryKeyConstraint("workspace_key", "request_key", name="uq_capital_plan_request"),
        Index("ix_capital_plan_registration_plan", "workspace_key", "plan_id"),
    )
    workspace_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_sha256: Mapped[str] = mapped_column(String(64))
    plan_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PaperBookRow(Base):
    __tablename__ = "paper_books"
    __table_args__ = (
        UniqueConstraint("workspace_key", "request_key", name="uq_paper_book_request"),
        CheckConstraint("mode IN ('prospective','synthetic')", name="ck_paper_book_mode"),
        CheckConstraint("revision >= 1", name="ck_paper_book_revision"),
        CheckConstraint("event_sequence >= 0", name="ck_paper_book_sequence"),
        Index("ix_paper_books_workspace", "workspace_key", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(64))
    request_key: Mapped[str] = mapped_column(String(128))
    request_sha256: Mapped[str] = mapped_column(String(64))
    account_seq: Mapped[str] = mapped_column(String(19))
    mode: Mapped[str] = mapped_column(String(16))
    snapshot_id: Mapped[str] = mapped_column(String(64))
    seed: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[dict] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(Integer)
    event_sequence: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PaperIntentRow(Base):
    __tablename__ = "paper_intents"
    __table_args__ = (
        UniqueConstraint("workspace_key", "request_key", name="uq_paper_intent_request"),
        CheckConstraint("mode IN ('prospective','synthetic')", name="ck_paper_intent_mode"),
        Index("ix_paper_intents_book", "workspace_key", "book_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(64))
    book_id: Mapped[str] = mapped_column(ForeignKey("paper_books.id", ondelete="CASCADE"))
    request_key: Mapped[str] = mapped_column(String(128))
    plan_id: Mapped[str] = mapped_column(String(64))
    alternative_id: Mapped[str] = mapped_column(String(64))
    account_seq: Mapped[str] = mapped_column(String(19))
    mode: Mapped[str] = mapped_column(String(16))
    alternative: Mapped[dict] = mapped_column(JSONB)
    profile: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PaperEventRow(Base):
    __tablename__ = "paper_events"
    __table_args__ = (
        UniqueConstraint("workspace_key", "request_key", name="uq_paper_event_request"),
        UniqueConstraint("book_id", "sequence", name="uq_paper_event_sequence"),
        UniqueConstraint("book_id", "capture_id", name="uq_paper_capture_receipt"),
        CheckConstraint("sequence >= 1", name="ck_paper_event_sequence"),
        Index("ix_paper_events_book", "workspace_key", "book_id", "sequence"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_key: Mapped[str] = mapped_column(String(64))
    book_id: Mapped[str] = mapped_column(ForeignKey("paper_books.id", ondelete="CASCADE"))
    sequence: Mapped[int] = mapped_column(Integer)
    request_key: Mapped[str | None] = mapped_column(String(128))
    request_sha256: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    intent_id: Mapped[str | None] = mapped_column(
        ForeignKey("paper_intents.id", ondelete="CASCADE")
    )
    capture_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
