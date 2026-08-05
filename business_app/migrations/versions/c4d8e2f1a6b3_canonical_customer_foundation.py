"""canonical customer foundation: canonical_customers + address_groups + FK pointers

Revision ID: c4d8e2f1a6b3
Revises: b8f4d2a1c9e6
Create Date: 2026-07-20 10:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "c4d8e2f1a6b3"
down_revision = "b8f4d2a1c9e6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "canonical_customers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "primary_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="fk_canonical_customers_primary_user"),
            nullable=True,
        ),
        sa.Column(
            "created_by_admin_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="fk_canonical_customers_created_by_admin"),
            nullable=True,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_canonical_customers_primary_user", "canonical_customers", ["primary_user_id"])

    op.create_table(
        "address_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "canonical_customer_id",
            sa.Integer(),
            sa.ForeignKey("canonical_customers.id", name="fk_address_groups_canonical_customer"),
            nullable=False,
        ),
        sa.Column("label", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_address_groups_canonical_customer", "address_groups", ["canonical_customer_id"])

    op.add_column("users", sa.Column("canonical_customer_id", sa.Integer(), nullable=True))
    op.create_index("ix_users_canonical_customer_id", "users", ["canonical_customer_id"])
    op.create_foreign_key(
        "fk_users_canonical_customer_id", "users", "canonical_customers", ["canonical_customer_id"], ["id"]
    )

    op.add_column("addresses", sa.Column("address_group_id", sa.Integer(), nullable=True))
    op.create_index("ix_addresses_address_group_id", "addresses", ["address_group_id"])
    op.create_foreign_key("fk_addresses_address_group_id", "addresses", "address_groups", ["address_group_id"], ["id"])


def downgrade():
    op.drop_constraint("fk_addresses_address_group_id", "addresses", type_="foreignkey")
    op.drop_index("ix_addresses_address_group_id", table_name="addresses")
    op.drop_column("addresses", "address_group_id")

    op.drop_constraint("fk_users_canonical_customer_id", "users", type_="foreignkey")
    op.drop_index("ix_users_canonical_customer_id", table_name="users")
    op.drop_column("users", "canonical_customer_id")

    op.drop_index("idx_address_groups_canonical_customer", table_name="address_groups")
    op.drop_table("address_groups")
    op.drop_index("idx_canonical_customers_primary_user", table_name="canonical_customers")
    op.drop_table("canonical_customers")
