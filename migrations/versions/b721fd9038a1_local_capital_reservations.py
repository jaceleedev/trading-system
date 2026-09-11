"""Stable account pools and atomic local plan reservations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b721fd9038a1"
down_revision = "a720e45bc910"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "funding_pools",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_key", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("account_seq", sa.String(19), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("market", sa.String(2)),
        sa.Column("symbol", sa.String(64)),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("snapshot_id", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capacity", sa.Numeric()),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("basis", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('cash','holding')", name="ck_funding_pool_kind"),
        sa.CheckConstraint("mode IN ('prospective','synthetic')", name="ck_funding_pool_mode"),
        sa.CheckConstraint("capacity IS NULL OR capacity >= 0", name="ck_funding_pool_capacity"),
        sa.CheckConstraint("revision >= 1", name="ck_funding_pool_revision"),
    )
    op.create_index("ix_funding_pools_account", "funding_pools", ["workspace_key", "account_seq"])
    op.create_table(
        "funding_reservations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_key", sa.String(64), nullable=False),
        sa.Column("account_seq", sa.String(19), nullable=False),
        sa.Column("plan_id", sa.String(64), nullable=False),
        sa.Column("alternative_id", sa.String(64), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("requirements", postgresql.JSONB(), nullable=False),
        sa.Column("pool_revisions", postgresql.JSONB(), nullable=False),
        sa.Column("snapshot_ids", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("replaced_by", sa.String(36)),
        sa.UniqueConstraint("workspace_key", "request_key", name="uq_funding_reservation_request"),
        sa.CheckConstraint(
            "status IN ('active','released','replaced')", name="ck_funding_reservation_status"
        ),
        sa.CheckConstraint(
            "mode IN ('prospective','synthetic')", name="ck_funding_reservation_mode"
        ),
    )
    op.create_index(
        "ix_funding_reservations_account",
        "funding_reservations",
        ["workspace_key", "account_seq", "status"],
    )
    op.create_table(
        "funding_reservation_lines",
        sa.Column(
            "reservation_id",
            sa.String(36),
            sa.ForeignKey("funding_reservations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("pool_id", sa.String(64), sa.ForeignKey("funding_pools.id"), primary_key=True),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_funding_reservation_line_amount"),
    )
    op.create_table(
        "capital_plan_registrations",
        sa.Column("workspace_key", sa.String(64), primary_key=True),
        sa.Column("request_key", sa.String(128), primary_key=True),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("plan_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_key", "request_key", name="uq_capital_plan_request"),
    )
    op.create_index(
        "ix_capital_plan_registration_plan",
        "capital_plan_registrations",
        ["workspace_key", "plan_id"],
    )


def downgrade():
    op.drop_index("ix_capital_plan_registration_plan", table_name="capital_plan_registrations")
    op.drop_table("capital_plan_registrations")
    op.drop_table("funding_reservation_lines")
    op.drop_index("ix_funding_reservations_account", table_name="funding_reservations")
    op.drop_table("funding_reservations")
    op.drop_index("ix_funding_pools_account", table_name="funding_pools")
    op.drop_table("funding_pools")
