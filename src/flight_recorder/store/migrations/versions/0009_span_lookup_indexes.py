"""span lookup indexes

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-14 12:00:00.000000

A PermissionRequest carries no tool_use_id, so Claude Code ingest finds its tool as the
latest call still running, and a tool's end finds its permission wait by parent. Both
scanned the whole session: 50 requests took 2.5 s in a 50,000-span run, near the hook's
3 s send timeout. A tool ends once, so few rows sit at or after a given end.
"""

# pyright: basic
# Written to match `afr db revision` output, then reviewed; strict typing adds noise here.

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_spans_tool_ends",
        "spans",
        ["run_id", "ended_at"],
        unique=False,
        postgresql_where=sa.text("kind = 'tool'"),
    )
    op.create_index(
        "ix_spans_permission_waits",
        "spans",
        ["run_id", "parent_external_id"],
        unique=False,
        postgresql_where=sa.text("external_id LIKE 'permission:%'"),
    )


def downgrade() -> None:
    op.drop_index("ix_spans_permission_waits", table_name="spans")
    op.drop_index("ix_spans_tool_ends", table_name="spans")
