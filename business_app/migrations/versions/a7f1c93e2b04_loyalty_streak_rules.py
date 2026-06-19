"""loyalty streak rules: table + seed + drop old streak counters"""

from alembic import op
import sqlalchemy as sa

revision = "a7f1c93e2b04"
down_revision = "d9e3b5a7c1f2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "loyalty_streak_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("program_id", sa.Integer(), sa.ForeignKey("loyalty_programs.id"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("required_orders", sa.Integer(), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column("min_order_amount", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("bonus_points", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_loyalty_streak_rules_program_id", "loyalty_streak_rules", ["program_id"])

    # Seed one default rule (preserves prior 3 orders / 30 days / +300 behaviour)
    conn = op.get_bind()
    prog = conn.execute(
        sa.text("SELECT id FROM loyalty_programs WHERE is_default = true ORDER BY id LIMIT 1")
    ).fetchone()
    if prog is None:
        prog = conn.execute(sa.text("SELECT id FROM loyalty_programs ORDER BY id LIMIT 1")).fetchone()
    if prog is not None:
        conn.execute(
            sa.text(
                "INSERT INTO loyalty_streak_rules "
                "(program_id, name, required_orders, window_days, bonus_points, is_active, display_order, created_at, updated_at) "
                "VALUES (:pid, :name, 3, 30, 300, true, 0, NOW(), NOW())"
            ),
            {"pid": prog[0], "name": "3 orders in 30 days"},
        )

    with op.batch_alter_table("loyalty_points") as batch_op:
        batch_op.drop_column("current_streak")
        batch_op.drop_column("last_streak_update")
        batch_op.drop_column("streak_orders_this_month")


def downgrade():
    with op.batch_alter_table("loyalty_points") as batch_op:
        batch_op.add_column(sa.Column("current_streak", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("last_streak_update", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("streak_orders_this_month", sa.Integer(), nullable=True))
    op.drop_index("ix_loyalty_streak_rules_program_id", table_name="loyalty_streak_rules")
    op.drop_table("loyalty_streak_rules")
