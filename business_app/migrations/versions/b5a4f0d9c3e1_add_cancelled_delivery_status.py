"""add cancelled delivery status enum value

Revision ID: b5a4f0d9c3e1
Revises: a7c4d9e2f1b6
Create Date: 2026-03-01 20:45:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "b5a4f0d9c3e1"
down_revision = "a7c4d9e2f1b6"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE delivery_status ADD VALUE IF NOT EXISTS 'cancelled'")


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("UPDATE deliveries SET status = 'failed' WHERE status = 'cancelled'")
    op.execute("UPDATE delivery_status_history SET old_status = 'failed' WHERE old_status = 'cancelled'")
    op.execute("UPDATE delivery_status_history SET new_status = 'failed' WHERE new_status = 'cancelled'")

    op.execute("ALTER TABLE delivery_status_history ALTER COLUMN old_status TYPE VARCHAR(20) USING old_status::text")
    op.execute("ALTER TABLE delivery_status_history ALTER COLUMN new_status TYPE VARCHAR(20) USING new_status::text")
    op.execute("ALTER TABLE deliveries ALTER COLUMN status TYPE VARCHAR(20) USING status::text")

    op.execute("DROP TYPE delivery_status")
    op.execute(
        "CREATE TYPE delivery_status AS ENUM "
        "('scheduled', 'pending', 'assigned', 'picked_up', 'in_transit', 'arrived', 'delivered', 'failed', 'returned')"
    )

    op.execute("ALTER TABLE deliveries ALTER COLUMN status TYPE delivery_status USING status::delivery_status")
    op.execute("ALTER TABLE delivery_status_history ALTER COLUMN old_status TYPE delivery_status USING old_status::delivery_status")
    op.execute("ALTER TABLE delivery_status_history ALTER COLUMN new_status TYPE delivery_status USING new_status::delivery_status")
