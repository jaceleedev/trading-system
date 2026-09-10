"""Idempotent local outcome report registration, no account ledger changes."""

import sqlalchemy as sa
from alembic import op

revision = "f726a13e940b"
down_revision = "e725c01a48b9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "outcome_reports",
        sa.Column("workspace_key", sa.String(64), primary_key=True),
        sa.Column("request_key", sa.String(100), primary_key=True),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("report_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_key", "request_key", name="uq_outcome_report_request"),
    )
    op.create_index(
        "ix_outcome_reports_workspace", "outcome_reports", ["workspace_key", "created_at"]
    )


def downgrade():
    op.drop_table("outcome_reports")
