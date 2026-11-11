"""span rejected status

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-11 19:00:00.000000
"""

# pyright: basic
# Written to match `afr db revision` output, then reviewed; strict typing adds noise here.

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "status IN ('unset', 'ok', 'error')"
NEW = "status IN ('unset', 'ok', 'error', 'rejected')"


def upgrade() -> None:
    # A span can now be `rejected`: a tool call (or its permission wait) that was refused.
    # Replaces the check constraint with a wider one; no rows change.
    op.drop_constraint(op.f("ck_spans_status"), "spans", type_="check")
    op.create_check_constraint("status", "spans", NEW)


def downgrade() -> None:
    # The old constraint has no `rejected`; a refused call was an error before.
    op.execute("UPDATE spans SET status = 'error' WHERE status = 'rejected'")
    op.drop_constraint(op.f("ck_spans_status"), "spans", type_="check")
    op.create_check_constraint("status", "spans", OLD)
