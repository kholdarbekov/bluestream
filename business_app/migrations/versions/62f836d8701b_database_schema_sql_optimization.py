"""database_schema_sql_optimization

Revision ID: 62f836d8701b
Revises: e7b1c2d3f4a5
Create Date: 2026-03-03 20:12:54.789550

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "62f836d8701b"
down_revision = "e7b1c2d3f4a5"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Consolidate duplicate payment rows before enforcing the canonical one-payment-per-order invariant.
    op.execute(
        """
        WITH ranked_payments AS (
            SELECT
                id,
                order_id,
                ROW_NUMBER() OVER (
                    PARTITION BY order_id
                    ORDER BY
                        CASE
                            WHEN status = 'completed' THEN 0
                            WHEN status = 'pending' THEN 1
                            ELSE 2
                        END,
                        COALESCE(paid_at, created_at, updated_at) DESC,
                        id DESC
                ) AS row_rank
            FROM payments
            WHERE order_id IS NOT NULL
        ),
        duplicate_payments AS (
            SELECT id, order_id
            FROM ranked_payments
            WHERE row_rank > 1
        ),
        keeper_payments AS (
            SELECT order_id, id AS keep_id
            FROM ranked_payments
            WHERE row_rank = 1
        )
        UPDATE payment_transactions AS payment_transactions
        SET payment_id = keeper_payments.keep_id
        FROM duplicate_payments
        JOIN keeper_payments ON keeper_payments.order_id = duplicate_payments.order_id
        WHERE payment_transactions.payment_id = duplicate_payments.id
        """
    )
    op.execute(
        """
        DELETE FROM payments
        USING (
            WITH ranked_payments AS (
                SELECT
                    id,
                    order_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY order_id
                        ORDER BY
                            CASE
                                WHEN status = 'completed' THEN 0
                                WHEN status = 'pending' THEN 1
                                ELSE 2
                            END,
                            COALESCE(paid_at, created_at, updated_at) DESC,
                            id DESC
                    ) AS row_rank
                FROM payments
                WHERE order_id IS NOT NULL
            )
            SELECT id
            FROM ranked_payments
            WHERE row_rank > 1
        ) AS duplicate_payments
        WHERE payments.id = duplicate_payments.id
        """
    )

    with op.batch_alter_table("campaign_usage", schema=None) as batch_op:
        batch_op.create_index("idx_campaign_usage_campaign_user", ["campaign_id", "user_id"], unique=False)
        batch_op.create_index("idx_campaign_usage_order_id", ["order_id"], unique=False)
        batch_op.create_foreign_key("fk_campaign_usage_campaign_id", "promotional_campaigns", ["campaign_id"], ["id"])
        batch_op.create_foreign_key("fk_campaign_usage_user_id", "users", ["user_id"], ["id"])
        batch_op.create_foreign_key("fk_campaign_usage_order_id", "orders", ["order_id"], ["id"])

    with op.batch_alter_table("loyalty_points", schema=None) as batch_op:
        batch_op.create_index(
            "idx_loyalty_points_program_tier_activity",
            ["program_id", "current_tier", "last_activity_date"],
            unique=False,
        )

    with op.batch_alter_table("loyalty_transactions", schema=None) as batch_op:
        batch_op.create_index("idx_loyalty_transactions_user_created", ["user_id", "created_at"], unique=False)

    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.create_index("idx_orders_delivery_slot_date", ["delivery_time_slot", "delivery_date"], unique=False)

    with op.batch_alter_table("payments", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("idx_payments_order_status"))
        batch_op.create_unique_constraint("uq_payments_order_id", ["order_id"])

    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.create_index("idx_products_active_base_price", ["is_active", "base_price"], unique=False)
        batch_op.create_index("idx_products_active_category", ["is_active", "category_id"], unique=False)
        batch_op.create_index("idx_products_active_featured", ["is_active", "is_featured"], unique=False)
        batch_op.create_index("idx_products_slug", ["slug"], unique=False)
    op.execute(
        """
        CREATE INDEX idx_products_search_trgm
        ON products
        USING gin ((coalesce(name, '') || ' ' || coalesce(description, '') || ' ' || coalesce(sku, '')) gin_trgm_ops)
        """
    )

    with op.batch_alter_table("subscriptions", schema=None) as batch_op:
        batch_op.create_index("idx_subscriptions_status_next_billing", ["status", "next_billing_date"], unique=False)
        batch_op.create_index("idx_subscriptions_status_next_delivery", ["status", "next_delivery_date"], unique=False)


def downgrade():
    with op.batch_alter_table("subscriptions", schema=None) as batch_op:
        batch_op.drop_index("idx_subscriptions_status_next_delivery")
        batch_op.drop_index("idx_subscriptions_status_next_billing")

    op.execute("DROP INDEX IF EXISTS idx_products_search_trgm")
    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.drop_index("idx_products_slug")
        batch_op.drop_index("idx_products_active_featured")
        batch_op.drop_index("idx_products_active_category")
        batch_op.drop_index("idx_products_active_base_price")

    with op.batch_alter_table("payments", schema=None) as batch_op:
        batch_op.drop_constraint("uq_payments_order_id", type_="unique")
        batch_op.create_index(batch_op.f("idx_payments_order_status"), ["order_id", "status"], unique=False)

    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_index("idx_orders_delivery_slot_date")

    with op.batch_alter_table("loyalty_transactions", schema=None) as batch_op:
        batch_op.drop_index("idx_loyalty_transactions_user_created")

    with op.batch_alter_table("loyalty_points", schema=None) as batch_op:
        batch_op.drop_index("idx_loyalty_points_program_tier_activity")

    with op.batch_alter_table("campaign_usage", schema=None) as batch_op:
        batch_op.drop_constraint("fk_campaign_usage_order_id", type_="foreignkey")
        batch_op.drop_constraint("fk_campaign_usage_user_id", type_="foreignkey")
        batch_op.drop_constraint("fk_campaign_usage_campaign_id", type_="foreignkey")
        batch_op.drop_index("idx_campaign_usage_order_id")
        batch_op.drop_index("idx_campaign_usage_campaign_user")
