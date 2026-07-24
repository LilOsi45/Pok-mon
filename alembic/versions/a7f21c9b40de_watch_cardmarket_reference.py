"""watch cardmarket reference price

Revision ID: a7f21c9b40de
Revises: 1cd5812c08c3
Create Date: 2026-07-25

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7f21c9b40de"
down_revision = "1cd5812c08c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("watches", sa.Column("cardmarket_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("watches", "cardmarket_id")
