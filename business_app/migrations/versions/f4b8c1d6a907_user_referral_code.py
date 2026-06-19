"""loyalty: persist referral code + referred_by on users (referral SSOT)

Revision ID: f4b8c1d6a907
Revises: e7d2a9c4b1f3
Create Date: 2026-06-14 00:30:00.000000

Phase 2 (P2-5) of the loyalty SSOT finalization. Adds the persisted referral
identity to ``users``:
  * ``referral_code``  — the user's own shareable code (unique), generated once
    on first use by LoyaltyService.get_user_referral_code.
  * ``referred_by_user_id`` — who referred this user (set once at signup), the
    guard against double-referral.

Both are nullable with no backfill: existing users get a code lazily the first
time it's requested, and have no referrer.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f4b8c1d6a907"
down_revision = "e7d2a9c4b1f3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("referral_code", sa.String(length=20), nullable=True))
    op.add_column("users", sa.Column("referred_by_user_id", sa.Integer(), nullable=True))
    op.create_index("ix_users_referral_code", "users", ["referral_code"], unique=True)
    op.create_index("ix_users_referred_by_user_id", "users", ["referred_by_user_id"], unique=False)
    op.create_foreign_key(
        "fk_users_referred_by_user_id_users",
        "users",
        "users",
        ["referred_by_user_id"],
        ["id"],
    )

    # Drop the UNIQUE constraint on referral_programs.referral_code: it records
    # which referrer code was used and is reused across all of a referrer's
    # referees — a unique constraint capped each referrer at a single referral.
    # Per-referee uniqueness is enforced via users.referred_by_user_id.
    op.drop_constraint("referral_programs_referral_code_key", "referral_programs", type_="unique")
    op.create_index("ix_referral_programs_referral_code", "referral_programs", ["referral_code"], unique=False)


def downgrade():
    op.drop_index("ix_referral_programs_referral_code", table_name="referral_programs")
    op.create_unique_constraint("referral_programs_referral_code_key", "referral_programs", ["referral_code"])
    op.drop_constraint("fk_users_referred_by_user_id_users", "users", type_="foreignkey")
    op.drop_index("ix_users_referred_by_user_id", table_name="users")
    op.drop_index("ix_users_referral_code", table_name="users")
    op.drop_column("users", "referred_by_user_id")
    op.drop_column("users", "referral_code")
