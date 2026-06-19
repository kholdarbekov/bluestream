"""loyalty: reward_redemptions table (Phase 3 redemption SSOT)"""

from alembic import op
import sqlalchemy as sa

revision = "b7d4e9f1c2a3"
down_revision = "a1c2e3f4d5b6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "reward_redemptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("reward_id", sa.Integer(), sa.ForeignKey("loyalty_rewards.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=True),
        sa.Column("reward_type", sa.String(length=50), nullable=False),
        sa.Column("points_spent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("discount_amount", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("free_product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=True),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="applied"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("code", name="uq_reward_redemptions_code"),
    )
    op.create_index("ix_reward_redemptions_reward_id", "reward_redemptions", ["reward_id"])
    op.create_index("ix_reward_redemptions_user_id", "reward_redemptions", ["user_id"])
    op.create_index("ix_reward_redemptions_order_id", "reward_redemptions", ["order_id"])
    op.create_index("idx_reward_redemptions_reward_user", "reward_redemptions", ["reward_id", "user_id"])


def downgrade():
    op.drop_table("reward_redemptions")
