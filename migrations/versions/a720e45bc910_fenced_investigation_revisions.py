"""Frozen investigation revisions coordinated with durable jobs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a720e45bc910"
down_revision = "5d18a70bce42"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "investigations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_key", sa.String(64), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("active_job_id", sa.String(36), sa.ForeignKey("jobs.id")),
        sa.Column("latest_completed_revision", sa.Integer()),
        sa.Column("latest_result", postgresql.JSONB(none_as_null=True)),
        sa.Column("next_review_at", sa.DateTime(timezone=True)),
        sa.Column("event_conditions", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_key", "request_key", name="uq_investigations_request"),
        sa.CheckConstraint("current_revision >= 1", name="ck_investigations_revision"),
        sa.CheckConstraint("status IN ('active','paused')", name="ck_investigations_status"),
        sa.CheckConstraint(
            "latest_completed_revision IS NULL OR "
            "latest_completed_revision BETWEEN 1 AND current_revision",
            name="ck_investigations_completed",
        ),
    )
    op.create_index("ix_investigations_due", "investigations", ["workspace_key", "next_review_at"])
    op.create_table(
        "investigation_revisions",
        sa.Column(
            "investigation_id",
            sa.String(36),
            sa.ForeignKey("investigations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("number", sa.Integer(), primary_key=True),
        sa.Column("workspace_key", sa.String(64), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("trigger_kind", sa.String(32), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("context_input", postgresql.JSONB(), nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("result", postgresql.JSONB(none_as_null=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "investigation_id", "request_key", name="uq_investigation_revision_request"
        ),
        sa.UniqueConstraint("job_id", name="uq_investigation_revision_job"),
        sa.CheckConstraint("number >= 1", name="ck_investigation_revision_number"),
    )


def downgrade():
    op.drop_table("investigation_revisions")
    op.drop_index("ix_investigations_due", table_name="investigations")
    op.drop_table("investigations")
