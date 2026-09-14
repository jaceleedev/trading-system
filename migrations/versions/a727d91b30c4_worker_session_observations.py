"""Workspace worker-session observations independent of job attempt leases."""

import sqlalchemy as sa
from alembic import op

revision = "a727d91b30c4"
down_revision = "f726a13e940b"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "worker_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_key", sa.String(64), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("allow_network", sa.Boolean(), nullable=False),
        sa.Column("allow_codex", sa.Boolean(), nullable=False),
        sa.Column("current_job_id", sa.String(36)),
        sa.CheckConstraint(
            "state IN ('idle','running','stopped')", name="ck_worker_sessions_state"
        ),
        sa.CheckConstraint(
            "(state = 'running' AND current_job_id IS NOT NULL) OR "
            "(state <> 'running' AND current_job_id IS NULL)",
            name="ck_worker_sessions_job",
        ),
        sa.CheckConstraint(
            "(state = 'stopped') = (stopped_at IS NOT NULL)", name="ck_worker_sessions_stop"
        ),
    )
    op.create_index(
        "ix_worker_sessions_workspace",
        "worker_sessions",
        ["workspace_key", "expires_at", "started_at"],
    )


def downgrade():
    op.drop_table("worker_sessions")
