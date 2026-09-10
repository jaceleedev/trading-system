"""Durable operation workflow and frozen step request receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "e725c01a48b9"
down_revision = "d724b39a610e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "operation_workflows",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_key", sa.String(64), nullable=False),
        sa.Column("account_seq", sa.String(19), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("seed", pg.JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("step_sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("mode IN ('prospective','synthetic')", name="ck_workflow_mode"),
        sa.CheckConstraint(
            "status IN ('active','paused','attention','completed')", name="ck_workflow_status"
        ),
        sa.CheckConstraint("revision >= 1 AND step_sequence >= 0", name="ck_workflow_revision"),
    )
    op.create_index(
        "ix_workflows_workspace", "operation_workflows", ["workspace_key", "created_at"]
    )
    op.create_table(
        "operation_workflow_steps",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_key", sa.String(64), nullable=False),
        sa.Column(
            "workflow_id",
            sa.String(36),
            sa.ForeignKey("operation_workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("input", pg.JSONB(), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(64)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("result", pg.JSONB(none_as_null=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("receipt", pg.JSONB(none_as_null=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("workspace_key", "request_key", name="uq_workflow_step_request"),
        sa.UniqueConstraint("workflow_id", "sequence", name="uq_workflow_step_sequence"),
        sa.CheckConstraint(
            "kind IN ('funding_refresh','capital_plan','reservation','order_intent',"
            "'order_observation','reconciliation','control')",
            name="ck_workflow_step_kind",
        ),
        sa.CheckConstraint(
            "state IN ('prepared','running','succeeded','needs_check')",
            name="ck_workflow_step_state",
        ),
        sa.CheckConstraint(
            "sequence >= 1 AND attempt_count BETWEEN 0 AND 100", name="ck_workflow_step_counts"
        ),
        sa.CheckConstraint(
            "(state = 'running' AND token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (state <> 'running' AND token IS NULL AND lease_expires_at IS NULL)",
            name="ck_workflow_step_lease",
        ),
    )
    op.create_index(
        "ix_workflow_steps_workflow",
        "operation_workflow_steps",
        ["workspace_key", "workflow_id", "sequence"],
    )


def downgrade():
    op.drop_index("ix_workflow_steps_workflow", table_name="operation_workflow_steps")
    op.drop_table("operation_workflow_steps")
    op.drop_index("ix_workflows_workspace", table_name="operation_workflows")
    op.drop_table("operation_workflows")
