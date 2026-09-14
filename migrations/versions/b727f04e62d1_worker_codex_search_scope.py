"""Separate observed Codex web search from the Toss GET collection permission."""

import sqlalchemy as sa
from alembic import op

revision = "b727f04e62d1"
down_revision = "a727d91b30c4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("worker_sessions", sa.Column("codex_web_search_allowed", sa.Boolean()))


def downgrade():
    op.drop_column("worker_sessions", "codex_web_search_allowed")
