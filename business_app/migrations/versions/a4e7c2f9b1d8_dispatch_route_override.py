"""dispatch: manual route override columns on delivery_routes

Revision ID: a4e7c2f9b1d8
Revises: d6b3f8a2c5e9
Create Date: 2026-08-06

Adds the four columns that let an admin's manual stop sequence survive the
auto-optimiser. `optimized_order` remains the sequence SSOT.
"""

import sqlalchemy as sa
from alembic import op

revision = "a4e7c2f9b1d8"
down_revision = "d6b3f8a2c5e9"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "delivery_routes",
        sa.Column("manual_override", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("delivery_routes", sa.Column("pinned_stops", sa.JSON(), nullable=True))
    op.add_column("delivery_routes", sa.Column("overridden_by", sa.Integer(), nullable=True))
    op.add_column("delivery_routes", sa.Column("overridden_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_delivery_routes_overridden_by_users",
        "delivery_routes",
        "users",
        ["overridden_by"],
        ["id"],
    )
    # Existing rows predate the feature: no override, no pins.
    op.execute("UPDATE delivery_routes SET pinned_stops = '{}'::json WHERE pinned_stops IS NULL")


def downgrade():
    op.drop_constraint("fk_delivery_routes_overridden_by_users", "delivery_routes", type_="foreignkey")
    op.drop_column("delivery_routes", "overridden_at")
    op.drop_column("delivery_routes", "overridden_by")
    op.drop_column("delivery_routes", "pinned_stops")
    op.drop_column("delivery_routes", "manual_override")
