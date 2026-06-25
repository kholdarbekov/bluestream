"""Backfill missing `en` name translations for loyalty entities

Loyalty rule/program/tier names are entered in English into the canonical
`name` column with explicit uz/ru Translation rows but historically no `en`
row. Because DEFAULT_LANGUAGE is uz, get_translated('name','en') could not
return the column value and fell back to the Uzbek translation, so the English
/loyalty-guide page rendered Uzbek names. This data migration creates the
missing `en` Translation row (= the canonical `name` column) for any loyalty
entity that lacks one. Idempotent (NOT EXISTS guard); going forward the admin
UI mirrors `en := name` on save.

Revision ID: d3f7a1c9e5b2
Revises: e2c5a8f1b3d7
Create Date: 2026-06-25 14:05:00.000000

"""

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = "d3f7a1c9e5b2"
down_revision = "e2c5a8f1b3d7"
branch_labels = None
depends_on = None

# (entity_type as used in the Translation key "EntityType.name.<id>", source table).
# Tiers already carry `en` rows from seed data — the NOT EXISTS guard skips them.
_LOYALTY_NAME_ENTITIES = (
    ("LoyaltyConsecutiveStrikeRule", "loyalty_consecutive_strike_rules"),
    ("LoyaltyStreakRule", "loyalty_streak_rules"),
    ("LoyaltyTierConfig", "loyalty_tier_configs"),
    ("LoyaltyProgram", "loyalty_programs"),
)


def upgrade():
    bind = op.get_bind()
    for entity_type, table in _LOYALTY_NAME_ENTITIES:
        # Table names come from the hardcoded tuple above — never user input.
        bind.execute(
            text(
                f"""
                INSERT INTO translations (key, language, value, category, is_active, created_at, updated_at)
                SELECT :etype || '.name.' || CAST(r.id AS TEXT), 'en', r.name, :cat,
                       TRUE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                FROM {table} AS r
                WHERE r.name IS NOT NULL
                  AND TRIM(r.name) <> ''
                  AND NOT EXISTS (
                      SELECT 1 FROM translations AS t
                      WHERE t.key = :etype || '.name.' || CAST(r.id AS TEXT)
                        AND t.language = 'en'
                  )
                """
            ),
            {"etype": entity_type, "cat": f"entity_{entity_type.lower()}"},
        )


def downgrade():
    # Data-only backfill. We cannot distinguish rows created here from `en`
    # translations a user may have since entered for the same entities, so the
    # downgrade is intentionally a no-op (the inserted rows are harmless and
    # equal to the canonical `name` column).
    pass
