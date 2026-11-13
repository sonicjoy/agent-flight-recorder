"""token counts are 64-bit

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-13 18:00:00.000000

A Claude Code session is one run, and each response's input tokens include its cache
reads, so a run's total passes 2,147,483,647 in a long session: every later event with
token usage was then refused. Spans are widened too, so a span holds whatever a run can.
"""

# pyright: basic
# Written to match `afr db revision` output, then reviewed; strict typing adds noise here.

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = [
    (table, column) for table in ("runs", "spans") for column in ("input_tokens", "output_tokens")
]


def upgrade() -> None:
    for table, column in COLUMNS:
        op.alter_column(table, column, type_=sa.BigInteger(), existing_type=sa.Integer())


def downgrade() -> None:
    # Fails, changing nothing, if a count no longer fits in 32 bits.
    for table, column in COLUMNS:
        op.alter_column(table, column, type_=sa.Integer(), existing_type=sa.BigInteger())
