"""embedding keyword index

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-12 14:00:00.000000
"""

# pyright: basic
# Written to match `afr db revision` output, then reviewed; strict typing adds noise here.

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Keyword search (#11) next to vector search: Postgres keeps the words of each embedded
    # text in a generated column, so existing rows get it too and nothing has to fill it.
    op.add_column(
        "embeddings",
        sa.Column(
            "content_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english'::regconfig, content)", persisted=True),
            nullable=True,
            comment="content's words, for keyword search; computed by Postgres",
        ),
    )
    op.create_index(
        "ix_embeddings_content_tsv",
        "embeddings",
        ["content_tsv"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_embeddings_content_tsv", table_name="embeddings", postgresql_using="gin")
    # allow-data-loss: the column is computed from content, so nothing is lost
    op.drop_column("embeddings", "content_tsv")
