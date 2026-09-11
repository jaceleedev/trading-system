"""Disabled order intents, single-dispatch operations and request receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "d724b39a610e"
down_revision = "c722a64e9013"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "order_intents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_key", sa.String(64), nullable=False),
        sa.Column("plan_id", sa.String(64), nullable=False),
        sa.Column("alternative_id", sa.String(64), nullable=False),
        sa.Column(
            "reservation_id",
            sa.String(36),
            sa.ForeignKey("funding_reservations.id"),
            nullable=False,
        ),
        sa.Column("account_seq", sa.String(19), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("seed", pg.JSONB(), nullable=False),
        sa.Column("legs", pg.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
        sa.Column("reservation_held", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("mode IN ('prospective','synthetic')", name="ck_order_intent_mode"),
        sa.CheckConstraint("status IN ('active','aborted')", name="ck_order_intent_status"),
        sa.CheckConstraint(
            "revision >= 1 AND event_sequence >= 0", name="ck_order_intent_revision"
        ),
        sa.CheckConstraint(
            "status <> 'aborted' OR NOT reservation_held", name="ck_order_intent_attachment"
        ),
    )
    op.create_index("ix_order_intents_workspace", "order_intents", ["workspace_key", "created_at"])
    op.create_index(
        "uq_order_reservation_attachment",
        "order_intents",
        ["reservation_id"],
        unique=True,
        postgresql_where=sa.text("reservation_held"),
    )
    op.create_table(
        "order_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_key", sa.String(64), nullable=False),
        sa.Column(
            "intent_id",
            sa.String(36),
            sa.ForeignKey("order_intents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("leg_index", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("prepared", pg.JSONB(), nullable=False),
        sa.Column("outcome", pg.JSONB(none_as_null=True)),
        sa.Column("dispatch_token", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("intent_id", "sequence", name="uq_order_operation_sequence"),
        sa.CheckConstraint("kind IN ('create','modify','cancel')", name="ck_order_operation_kind"),
        sa.CheckConstraint(
            "state IN ('prepared','dispatching','acknowledged','rejected','ambiguous','aborted')",
            name="ck_order_operation_state",
        ),
        sa.CheckConstraint(
            "sequence BETWEEN 1 AND 100 AND leg_index BETWEEN 0 AND 49",
            name="ck_order_operation_bounds",
        ),
    )
    op.create_index(
        "ix_order_operations_intent", "order_operations", ["workspace_key", "intent_id"]
    )
    op.create_table(
        "order_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_key", sa.String(64), nullable=False),
        sa.Column(
            "intent_id",
            sa.String(36),
            sa.ForeignKey("order_intents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "operation_id", sa.String(36), sa.ForeignKey("order_operations.id", ondelete="CASCADE")
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("request_key", sa.String(128)),
        sa.Column("request_sha256", sa.String(64)),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column("result", pg.JSONB(none_as_null=True)),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("intent_id", "sequence", name="uq_order_event_sequence"),
        sa.UniqueConstraint("workspace_key", "request_key", name="uq_order_event_request"),
        sa.CheckConstraint("sequence >= 1", name="ck_order_event_sequence"),
    )
    op.create_index(
        "ix_order_events_intent", "order_events", ["workspace_key", "intent_id", "sequence"]
    )


def downgrade():
    op.drop_index("ix_order_events_intent", table_name="order_events")
    op.drop_table("order_events")
    op.drop_index("ix_order_operations_intent", table_name="order_operations")
    op.drop_table("order_operations")
    op.drop_index("uq_order_reservation_attachment", table_name="order_intents")
    op.drop_index("ix_order_intents_workspace", table_name="order_intents")
    op.drop_table("order_intents")
