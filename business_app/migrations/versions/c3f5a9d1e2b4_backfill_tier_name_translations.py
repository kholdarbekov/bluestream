"""backfill LoyaltyTierConfig.name entity translations for the canonical tiers

The public loyalty-guide page renders tier names via the model's translatable
``name`` field (entity translations keyed ``LoyaltyTierConfig.name.<id>``) instead
of page-specific static keys. This seeds the entity translations for the canonical
default tiers (Bronze/Silver/Gold/Platinum, created by f8a9b2c3d4e5) so their
localized names survive the switch.

Self-contained on purpose: the localized names are hardcoded here rather than read
from the legacy ``loyalty_guide.tier.<slug>.name`` keys, so this works identically
on a fresh DB (where those keys don't exist at migration time and are no longer
seeded) and on an upgraded-in-place DB. Idempotent: only inserts when a row is
absent, so it never clobbers names later edited in the admin tier modal, and a
renamed/custom tier is simply skipped (admin sets its translations via the modal).
"""

from alembic import op
import sqlalchemy as sa

revision = "c3f5a9d1e2b4"
down_revision = "a7f1c93e2b04"
branch_labels = None
depends_on = None

# Canonical (English column) name -> localized display names.
CANONICAL_TIER_NAMES = {
    "Bronze": {"uz": "Bronza", "en": "Bronze", "ru": "Бронза"},
    "Silver": {"uz": "Kumush", "en": "Silver", "ru": "Серебро"},
    "Gold": {"uz": "Oltin", "en": "Gold", "ru": "Золото"},
    "Platinum": {"uz": "Platina", "en": "Platinum", "ru": "Платина"},
}


def upgrade():
    conn = op.get_bind()
    tiers = conn.execute(sa.text("SELECT id, name FROM loyalty_tier_configs")).fetchall()
    for tier_id, name in tiers:
        localized = CANONICAL_TIER_NAMES.get((name or "").strip())
        if not localized:
            continue  # renamed/custom tier — admin sets its translations in the tier modal
        entity_key = f"LoyaltyTierConfig.name.{tier_id}"
        for lang, value in localized.items():
            exists = conn.execute(
                sa.text("SELECT 1 FROM translations WHERE key = :k AND language = :l"),
                {"k": entity_key, "l": lang},
            ).fetchone()
            if exists:
                continue  # never clobber an existing (e.g. admin-entered) translation
            conn.execute(
                sa.text(
                    "INSERT INTO translations "
                    "(key, language, value, category, is_active, created_at, updated_at) "
                    "VALUES (:k, :l, :v, 'entity_loyaltytierconfig', true, NOW(), NOW())"
                ),
                {"k": entity_key, "l": lang, "v": value},
            )


def downgrade():
    # Pure data backfill — reverse by removing the entity-translation rows it owns.
    op.get_bind().execute(sa.text("DELETE FROM translations WHERE key LIKE 'LoyaltyTierConfig.name.%'"))
