"""a run has each human failure mode once, whatever its spelling

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

# A mode's key: its words (letters and digits), lowercased, sorted, joined with '-'. So
# "Tool Timeout", "tool_timeout" and "timeout-tool" are one mode (#14). The app asks
# Postgres for keys rather than keeping a second copy of this rule.
MODE_KEY = """
CREATE FUNCTION afr_mode_key(mode text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
AS $$
    SELECT array_to_string(ARRAY(
        SELECT w FROM regexp_split_to_table(lower(mode), '[^[:alnum:]]+') AS w
        WHERE w <> '' ORDER BY w COLLATE "C"
    ), '-')
$$
"""

# The spelling kept for a key: the earliest label's words, lowercased, in their order.
SPELLING = """array_to_string(ARRAY(
    SELECT w FROM regexp_split_to_table(lower({mode}), '[^[:alnum:]]+') WITH ORDINALITY
        AS t(w, n)
    WHERE w <> '' ORDER BY n
), '-')"""


def _encoded(value: str) -> str:
    """Percent-encodes %, space, ;, [ and ] (% first), as store.labels.encoded does."""
    for char, code in (("%", "%25"), (" ", "%20"), (";", "%3B"), ("[", "%5B"), ("]", "%5D")):
        value = f"replace({value}, '{char}', '{code}')"
    return value


# A label's note as a fragment of another's, as store.labels.tag writes it.
_AUTHOR = "coalesce(author, '-')"
TAG = (
    "'[afr-note v1 at=' || to_char(created_at AT TIME ZONE 'UTC', "
    """'YYYY-MM-DD"T"HH24:MI:SS"Z"')"""
    f" || ' by=' || {_encoded(_AUTHOR)}"
    f" || ' mode=' || {_encoded('failure_mode')} || '] ' || note"
)

# Human labels with the same run, span and key; the earliest is the one kept.
RANKED = """
    SELECT id, note, author, failure_mode, created_at,
           row_number() OVER w AS rn,
           first_value(id) OVER w AS keep_id
    FROM labels
    WHERE source = 'human'
    WINDOW w AS (PARTITION BY run_id, span_id, mode_key ORDER BY created_at, id)
"""


def upgrade() -> None:
    # No labeller may add a row while labels are merged and the index is built.
    op.execute("LOCK TABLE labels IN SHARE ROW EXCLUSIVE MODE")
    op.execute(MODE_KEY)
    op.add_column(
        "labels",
        sa.Column(
            "mode_key",
            sa.Text(),
            sa.Computed("afr_mode_key(failure_mode)", persisted=True),
            nullable=True,
            comment="failure_mode's words, sorted: one mode whatever its spelling",
        ),
    )
    # allow-data-loss: human labels with the same run, span and mode become one. The others'
    # notes are appended to it as tagged fragments, sorted, so no text is lost and the result
    # doesn't depend on row order; empty notes are none.
    op.execute(f"""
        WITH ranked AS ({RANKED}),
        extra AS (
            SELECT keep_id, string_agg({TAG}, '; ' ORDER BY {TAG} COLLATE "C") AS notes
            FROM ranked
            WHERE rn > 1 AND nullif(note, '') IS NOT NULL
            GROUP BY keep_id
        )
        UPDATE labels SET note = concat_ws('; ', nullif(labels.note, ''), extra.notes)
        FROM extra
        WHERE labels.id = extra.keep_id
    """)
    op.execute(f"DELETE FROM labels WHERE id IN (SELECT id FROM ({RANKED}) r WHERE rn > 1)")
    # One spelling per mode: the earliest human label's, as the app writes new ones.
    op.execute(f"""
        UPDATE labels SET failure_mode = first.spelling
        FROM (
            SELECT DISTINCT ON (mode_key) mode_key, {SPELLING.format(mode="failure_mode")}
                AS spelling
            FROM labels
            WHERE source = 'human'
            ORDER BY mode_key, created_at, id
        ) AS first
        WHERE labels.source = 'human' AND labels.mode_key = first.mode_key
          AND labels.failure_mode <> first.spelling
    """)
    # Two labellers at once can't both add the mode: Postgres refuses the second. Model
    # labels may repeat; NULLS NOT DISTINCT makes "the whole run" (no span) one value.
    op.create_index(
        "uq_labels_human_mode",
        "labels",
        ["run_id", "span_id", "mode_key"],
        unique=True,
        postgresql_where=sa.text("source = 'human'"),
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_index("uq_labels_human_mode", table_name="labels")
    # allow-data-loss: the column is computed from failure_mode, so nothing is lost
    op.drop_column("labels", "mode_key")
    op.execute("DROP FUNCTION afr_mode_key(text)")
