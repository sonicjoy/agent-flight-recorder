"""label right answer

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-15 12:00:00.000000

`afr label` can store the right answer with a failure mode, so `afr case` can turn the run
into an eval case for an agent with no truth dataset (#59). Existing labels have none.
"""

# pyright: basic
# Written to match `afr db revision` output, then reviewed; strict typing adds noise here.

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COMMENT = "The right answer, redacted: {fields: {key: value}} or {text: ...} (#59)"


def upgrade() -> None:
    op.add_column(
        "labels",
        sa.Column("expected", postgresql.JSONB(), nullable=True, comment=COMMENT),
    )


def downgrade() -> None:
    op.drop_column("labels", "expected")
