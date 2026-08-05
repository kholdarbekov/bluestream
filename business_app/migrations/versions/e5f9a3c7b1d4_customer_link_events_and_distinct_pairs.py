"""customer link events + distinct pairs

Revision ID: e5f9a3c7b1d4
Revises: c4d8e2f1a6b3
Create Date: 2026-07-20 11:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "e5f9a3c7b1d4"
down_revision = "c4d8e2f1a6b3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "customer_link_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column(
            "canonical_customer_id",
            sa.Integer(),
            sa.ForeignKey("canonical_customers.id", name="fk_customer_link_events_canonical"),
            nullable=True,
        ),
        sa.Column(
            "acting_admin_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="fk_customer_link_events_admin"),
            nullable=True,
        ),
        sa.Column("member_user_ids", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_customer_link_events_canonical", "customer_link_events", ["canonical_customer_id"])

    op.create_table(
        "customer_distinct_pairs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id_low",
            sa.Integer(),
            sa.ForeignKey("users.id", name="fk_customer_distinct_pairs_low"),
            nullable=False,
        ),
        sa.Column(
            "user_id_high",
            sa.Integer(),
            sa.ForeignKey("users.id", name="fk_customer_distinct_pairs_high"),
            nullable=False,
        ),
        sa.Column(
            "dismissed_by_admin_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="fk_customer_distinct_pairs_admin"),
            nullable=True,
        ),
        sa.Column("signal_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id_low", "user_id_high", name="uq_customer_distinct_pairs"),
    )


def downgrade():
    op.drop_table("customer_distinct_pairs")
    op.drop_index("idx_customer_link_events_canonical", table_name="customer_link_events")
    op.drop_table("customer_link_events")
