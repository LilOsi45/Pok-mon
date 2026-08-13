"""direct-message target per alert, and a URL block list

The ping goes to a person, not a channel, and a monitor channel repeats the
same listing for days — so a hit needs a recipient and a way to silence one
product without touching the keyword that found it.

Revision ID: e63a9c2d4f18
Revises: d52f8b1c33a7
Create Date: 2026-08-13

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e63a9c2d4f18"
down_revision = "d52f8b1c33a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "keyword_alerts",
        sa.Column("dm_user_id", sa.String(length=32), nullable=False, server_default=""),
    )
    op.create_table(
        "blocked_urls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("url", sa.String(length=500), nullable=False, unique=True),
        sa.Column("label", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("blocked_urls")
    op.drop_column("keyword_alerts", "dm_user_id")
