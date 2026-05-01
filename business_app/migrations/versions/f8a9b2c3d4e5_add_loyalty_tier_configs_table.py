"""Add loyalty_tier_configs table for admin-managed tiers

Revision ID: f8a9b2c3d4e5
Revises: 53ff118bbf09
Create Date: 2026-01-20 20:30:00.000000

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "f8a9b2c3d4e5"
down_revision = "53ff118bbf09"
branch_labels = None
depends_on = None


def upgrade():
    # Create the loyalty_tier_configs table
    op.create_table(
        "loyalty_tier_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("program_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("display_order", sa.Integer(), default=0),
        sa.Column("min_points", sa.Integer(), nullable=False, default=0),
        sa.Column("max_points", sa.Integer(), nullable=True),
        sa.Column("points_multiplier", sa.Float(), default=1.0),
        sa.Column("discount_percentage", sa.Float(), default=0),
        sa.Column("benefits", sa.JSON(), default=[]),
        sa.Column("color", sa.String(20), default="#CD7F32"),
        sa.Column("icon", sa.String(50), default="fa-medal"),
        sa.Column("is_active", sa.Boolean(), default=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["program_id"],
            ["loyalty_programs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_loyalty_tier_configs_program_id", "loyalty_tier_configs", ["program_id"], unique=False)

    op.execute(
        """
        INSERT INTO loyalty_tier_configs (program_id, name, display_order, min_points, max_points, points_multiplier, discount_percentage, benefits, color, icon, is_active, created_at, updated_at)
        SELECT
            lp.id,
            tier.name,
            tier.display_order,
            tier.min_points,
            tier.max_points,
            tier.points_multiplier,
            tier.discount_percentage,
            tier.benefits::jsonb,
            tier.color,
            tier.icon,
            true,
            NOW(),
            NOW()
        FROM loyalty_programs lp
        CROSS JOIN (VALUES
            ('Bronze', 0, 0, 2000, 1.0, 0, '["Basic support", "Standard delivery", "Access to rewards catalog"]', '#CD7F32', 'fa-medal'),
            ('Silver', 1, 2000, 7000, 1.25, 5, '["Typical households reach Silver in one season", "Birthday bonus points"]', '#C0C0C0', 'fa-medal'),
            ('Gold', 2, 7000, 20000, 1.5, 10, '["For families and long-term customers", "Double birthday bonus"]', '#FFD700', 'fa-medal'),
            ('Platinum', 3, 20000, NULL, 2.0, 15, '["Designed for offices and high-volume users", "Triple birthday bonus"]', '#7C3AED', 'fa-crown')
        ) AS tier(name, display_order, min_points, max_points, points_multiplier, discount_percentage, benefits, color, icon)
        WHERE lp.is_default = true;
    """
    )


def downgrade():
    op.drop_index("ix_loyalty_tier_configs_program_id", table_name="loyalty_tier_configs")
    op.drop_table("loyalty_tier_configs")
