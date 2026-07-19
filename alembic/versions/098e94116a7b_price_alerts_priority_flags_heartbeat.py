"""price alerts, priority flags, heartbeat

Revision ID: 098e94116a7b
Revises: 62671faf0364
Create Date: 2026-07-19

"""

import sqlalchemy as sa

from alembic import op

revision = "098e94116a7b"
down_revision = "62671faf0364"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New EventType members exist only as a native enum on PostgreSQL;
    # SQLite stores plain VARCHAR and needs nothing.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE eventtype ADD VALUE IF NOT EXISTS 'PRICE_DROP'")
        op.execute("ALTER TYPE eventtype ADD VALUE IF NOT EXISTS 'HEARTBEAT'")

    with op.batch_alter_table("product_scans", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("priority", sa.Boolean(), nullable=False, server_default=sa.false())
        )

    with op.batch_alter_table("watches", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("priority", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("price_target", sa.Float(), nullable=True))
        batch_op.add_column(
            sa.Column("price_target_hit", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("watches", schema=None) as batch_op:
        batch_op.drop_column("price_target_hit")
        batch_op.drop_column("price_target")
        batch_op.drop_column("priority")

    with op.batch_alter_table("product_scans", schema=None) as batch_op:
        batch_op.drop_column("priority")
