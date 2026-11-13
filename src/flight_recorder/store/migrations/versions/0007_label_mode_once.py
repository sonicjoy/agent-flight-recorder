"""a run has each human failure mode once

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-13 12:00:00.000000
"""

# pyright: basic
# Written to match `afr db revision` output, then reviewed; strict typing adds noise here.

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The same run (or span) and mode, human labels only; the earliest is the one kept.
_RANKED = """
    SELECT id, note, author, created_at,
           row_number() OVER w AS rn,
           first_value(id) OVER w AS keep_id
    FROM labels
    WHERE source = 'human'
    WINDOW w AS (PARTITION BY run_id, span_id, failure_mode ORDER BY created_at, id)
"""


def upgrade() -> None:
    # No labeller may add a row between merging and building the index.
    op.execute("LOCK TABLE labels IN SHARE ROW EXCLUSIVE MODE")
    # allow-data-loss: duplicate human labels (same run, span and mode) become one; their
    # notes are appended to the one kept, marked [duplicate, by <author>], so no text is
    # lost; empty notes are none.
    op.execute(f"""
        WITH ranked AS ({_RANKED}),
        extra AS (
            SELECT keep_id,
                   string_agg(
                       '[duplicate' || coalesce(', by ' || author, '') || '] ' || note,
                       '; ' ORDER BY created_at, id
                   ) AS notes
            FROM ranked
            WHERE rn > 1 AND nullif(note, '') IS NOT NULL
            GROUP BY keep_id
        )
        UPDATE labels SET note = concat_ws('; ', nullif(labels.note, ''), extra.notes)
        FROM extra
        WHERE labels.id = extra.keep_id
    """)
    op.execute(f"DELETE FROM labels WHERE id IN (SELECT id FROM ({_RANKED}) r WHERE rn > 1)")
    # Two labellers at once can't both add the mode (#14): Postgres refuses the second.
    # Model labels may repeat; NULLS NOT DISTINCT makes "the whole run" (no span) one value.
    op.create_index(
        "uq_labels_human_mode",
        "labels",
        ["run_id", "span_id", "failure_mode"],
        unique=True,
        postgresql_where=sa.text("source = 'human'"),
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_index("uq_labels_human_mode", table_name="labels")
