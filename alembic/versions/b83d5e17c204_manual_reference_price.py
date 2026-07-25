"""manual reference price and link on watches

Cardmarket closed public API registrations, so the reference price needs a
path that works without API credentials: enter it once per watch by hand.

Revision ID: b83d5e17c204
Revises: a7f21c9b40de
Create Date: 2026-07-25

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b83d5e17c204"
down_revision = "a7f21c9b40de"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("watches", sa.Column("reference_price", sa.Float(), nullable=True))
    op.add_column("watches", sa.Column("reference_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("watches", "reference_url")
    op.drop_column("watches", "reference_price")
