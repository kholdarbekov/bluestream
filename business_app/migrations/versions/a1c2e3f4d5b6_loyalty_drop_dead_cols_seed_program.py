"""loyalty: drop deprecated program columns + seed a default LoyaltyProgram

Revision ID: a1c2e3f4d5b6
Revises: f4b8c1d6a907
Create Date: 2026-06-14 01:00:00.000000

Phase 2 (P2-6) of the loyalty SSOT finalization.

1. Drops the now-dead ``loyalty_programs`` columns that the loyalty SSOT cleanup
   stopped reading/writing:
     * ``points_per_uzs``  — superseded by ``uzs_per_point``.
     * ``tier_thresholds`` / ``tier_multipliers`` — superseded by LoyaltyTierConfig.
2. Seeds a default LoyaltyProgram when the table is empty, so a fresh database
   has the program that accounts/tiers/rewards reference (previously missing).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision = "a1c2e3f4d5b6"
down_revision = "f4b8c1d6a907"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("loyalty_programs") as batch_op:
        batch_op.drop_column("points_per_uzs")
        batch_op.drop_column("tier_thresholds")
        batch_op.drop_column("tier_multipliers")

    bind = op.get_bind()
    existing = bind.execute(text("SELECT COUNT(*) FROM loyalty_programs")).scalar() or 0
    if existing == 0:
        program_id = bind.execute(
            text(
                """
                INSERT INTO loyalty_programs
                    (name, description, is_active, is_default, uzs_per_point,
                     signup_bonus, referral_bonus, birthday_bonus,
                     points_expiry_days, min_redemption_points, created_at, updated_at)
                VALUES
                    (:name, :description, true, true, 250,
                     100, 50, 25,
                     365, 100, now(), now())
                RETURNING id
                """
            ),
            {"name": "Default Program", "description": "Default loyalty program"},
        ).scalar()

        # Seed the default tiers for THIS program. The original tier-config
        # migration CROSS JOINs loyalty_programs, which was empty when it ran on a
        # fresh DB (the program is only created here), so without this a fresh DB
        # would have a program but ZERO tiers (every customer stuck Bronze, 1.0x).
        # Guarded so a partially-seeded DB is not double-seeded.
        has_tiers = (
            bind.execute(
                text("SELECT COUNT(*) FROM loyalty_tier_configs WHERE program_id = :pid"),
                {"pid": program_id},
            ).scalar()
            or 0
        )
        if has_tiers == 0:
            bind.execute(
                text(
                    """
                    INSERT INTO loyalty_tier_configs
                        (program_id, name, display_order, min_points, max_points,
                         points_multiplier, discount_percentage, benefits, color, icon,
                         is_active, created_at, updated_at)
                    VALUES
                        (:pid, 'Bronze',   0, 0,     2000, 1.0,  0,  '[]'::json, '#CD7F32', 'fa-medal', true, now(), now()),
                        (:pid, 'Silver',   1, 2000,  7000, 1.25, 5,  '[]'::json, '#C0C0C0', 'fa-medal', true, now(), now()),
                        (:pid, 'Gold',     2, 7000,  20000, 1.5, 10, '[]'::json, '#FFD700', 'fa-medal', true, now(), now()),
                        (:pid, 'Platinum', 3, 20000, NULL, 2.0,  15, '[]'::json, '#7C3AED', 'fa-crown', true, now(), now())
                    """
                ),
                {"pid": program_id},
            )


def downgrade():
    with op.batch_alter_table("loyalty_programs") as batch_op:
        batch_op.add_column(sa.Column("points_per_uzs", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("tier_thresholds", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("tier_multipliers", sa.JSON(), nullable=True))
