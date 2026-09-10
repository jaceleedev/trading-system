"""Separate paper books, immutable intents and atomic event receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c722a64e9013"
down_revision = "b721fd9038a1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "paper_books",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_key", sa.String(64), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("account_seq", sa.String(19), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("snapshot_id", sa.String(64), nullable=False),
        sa.Column("seed", postgresql.JSONB(), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_key", "request_key", name="uq_paper_book_request"),
        sa.CheckConstraint("mode IN ('prospective','synthetic')", name="ck_paper_book_mode"),
        sa.CheckConstraint("revision >= 1", name="ck_paper_book_revision"),
        sa.CheckConstraint("event_sequence >= 0", name="ck_paper_book_sequence"),
    )
    op.create_index("ix_paper_books_workspace", "paper_books", ["workspace_key", "created_at"])
    op.create_table(
        "paper_intents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_key", sa.String(64), nullable=False),
        sa.Column(
            "book_id",
            sa.String(36),
            sa.ForeignKey("paper_books.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("plan_id", sa.String(64), nullable=False),
        sa.Column("alternative_id", sa.String(64), nullable=False),
        sa.Column("account_seq", sa.String(19), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("alternative", postgresql.JSONB(), nullable=False),
        sa.Column("profile", postgresql.JSONB(), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_key", "request_key", name="uq_paper_intent_request"),
        sa.CheckConstraint("mode IN ('prospective','synthetic')", name="ck_paper_intent_mode"),
    )
    op.create_index(
        "ix_paper_intents_book", "paper_intents", ["workspace_key", "book_id", "created_at"]
    )
    op.create_table(
        "paper_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_key", sa.String(64), nullable=False),
        sa.Column(
            "book_id",
            sa.String(36),
            sa.ForeignKey("paper_books.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("request_key", sa.String(128)),
        sa.Column("request_sha256", sa.String(64)),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column(
            "intent_id", sa.String(36), sa.ForeignKey("paper_intents.id", ondelete="CASCADE")
        ),
        sa.Column("capture_id", sa.String(64)),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(none_as_null=True)),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_key", "request_key", name="uq_paper_event_request"),
        sa.UniqueConstraint("book_id", "sequence", name="uq_paper_event_sequence"),
        sa.UniqueConstraint("book_id", "capture_id", name="uq_paper_capture_receipt"),
        sa.CheckConstraint("sequence >= 1", name="ck_paper_event_sequence"),
    )
    op.create_index(
        "ix_paper_events_book", "paper_events", ["workspace_key", "book_id", "sequence"]
    )


def downgrade():
    op.drop_index("ix_paper_events_book", table_name="paper_events")
    op.drop_table("paper_events")
    op.drop_index("ix_paper_intents_book", table_name="paper_intents")
    op.drop_table("paper_intents")
    op.drop_index("ix_paper_books_workspace", table_name="paper_books")
    op.drop_table("paper_books")
