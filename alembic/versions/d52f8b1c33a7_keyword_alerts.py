"""keyword alerts over incoming Discord messages

Everything tracked so far is a shop page. Plenty of drops only ever surface as
somebody's message in a cook group, and those need the same alarm.

Revision ID: d52f8b1c33a7
Revises: c41e7a09b2d5
Create Date: 2026-08-13

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d52f8b1c33a7"
down_revision = "c41e7a09b2d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "keyword_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("positive", sa.Text(), nullable=False),
        sa.Column("negative", sa.Text(), nullable=False, server_default=""),
        sa.Column("ranges", sa.Text(), nullable=False, server_default=""),
        sa.Column("delay_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("channels", sa.JSON(), nullable=False),
        sa.Column("priority", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_hit_at", sa.DateTime(), nullable=True),
        sa.Column("hits", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("keyword_alerts")
