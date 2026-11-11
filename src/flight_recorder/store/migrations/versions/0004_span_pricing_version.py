"""span pricing version

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-11 18:00:00.000000
"""

# pyright: basic
# Written to match `afr db revision` output, then reviewed; strict typing adds noise here.

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Spans priced before this migration keep NULL: their version is not known.
    op.add_column(
        "spans",
        sa.Column(
            "pricing_version",
            sa.Text(),
            nullable=True,
            comment="Version of pricing.yaml that priced cost_usd; null if not priced",
        ),
    )


def downgrade() -> None:
    op.drop_column("spans", "pricing_version")
