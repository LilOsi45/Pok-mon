"""count the reminders sent while a product stays in stock

A restock was announced exactly once. In a busy channel that single message
scrolls away within minutes and the drop is missed anyway, which is the failure
this tracker exists to prevent. Repeating it needs to know how many reminders
have already gone out for the current in-stock streak — last_in_stock_at cannot
answer that, it is refreshed by every check.

Revision ID: c41e7a09b2d5
Revises: b83d5e17c204
Create Date: 2026-08-12

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c41e7a09b2d5"
down_revision = "b83d5e17c204"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "watches",
        sa.Column("reminders_sent", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("watches", "reminders_sent")
