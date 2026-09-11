"""Durable workspace jobs with fenced attempts and recoverable leases."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "5d18a70bce42"
down_revision = "00d2be356055"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_key", sa.String(64), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("attempt_token", sa.String(64)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("result", postgresql.JSONB(none_as_null=True)),
        sa.Column("error_code", sa.String(64)),
        sa.UniqueConstraint("workspace_key", "request_key", name="uq_jobs_workspace_request"),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')", name="ck_jobs_status"
        ),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 10 AND attempt_count BETWEEN 0 AND max_attempts",
            name="ck_jobs_attempts",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND attempt_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'running' AND attempt_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_jobs_lease",
        ),
    )
    op.create_index(
        "ix_jobs_claim", "jobs", ["workspace_key", "status", "available_at", "created_at"]
    )
    op.create_index("ix_jobs_lease", "jobs", ["workspace_key", "status", "lease_expires_at"])
    op.create_table(
        "job_attempts",
        sa.Column(
            "job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("number", sa.Integer(), primary_key=True),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.UniqueConstraint("token", name="uq_job_attempts_token"),
        sa.CheckConstraint(
            "status IN ('running','succeeded','failed','cancelled','lease_expired')",
            name="ck_job_attempts_status",
        ),
        sa.CheckConstraint("number BETWEEN 1 AND 10", name="ck_job_attempts_number"),
    )


def downgrade():
    op.drop_table("job_attempts")
    op.drop_index("ix_jobs_lease", table_name="jobs")
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_table("jobs")
