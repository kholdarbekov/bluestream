"""Regression test for the loyalty-guide "English shows Uzbek" bug.

Root cause: loyalty rule names are entered into the canonical ``name`` column
(in English), with explicit ``uz``/``ru`` Translation rows but NO ``en`` row.
Because ``DEFAULT_LANGUAGE == "uz"``, ``get_translated("name", "en")`` cannot
return the column value (that branch fires only for the default language); it
falls through the uz→ru fallback chain and returns the **Uzbek** translation.
So the English /loyalty-guide page rendered Uzbek rule + strike names.

The data fix is an idempotent backfill that creates the missing ``en``
Translation row from the canonical ``name`` column for loyalty entities.
This test reproduces the buggy fallback, runs the backfill, and asserts that
English now resolves to the English column value while uz/ru are untouched.
"""

import pytest
from sqlalchemy import text

from business_app import db as _db
from business_app.models.loyalty import (
    LoyaltyConsecutiveStrikeRule,
    LoyaltyProgram,
    LoyaltyStreakRule,
)
from business_app.models.translation import Translation

# Local mirror of the idempotent backfill performed by Alembic migration
# d3f7a1c9e5b2 (kept self-contained in the migration). This re-implements the
# same dialect-portable INSERT so the test can exercise the backfill directly
# on SQLite and guard the expected behavior.
_LOYALTY_NAME_ENTITIES = (
    ("LoyaltyConsecutiveStrikeRule", "loyalty_consecutive_strike_rules"),
    ("LoyaltyStreakRule", "loyalty_streak_rules"),
    ("LoyaltyTierConfig", "loyalty_tier_configs"),
    ("LoyaltyProgram", "loyalty_programs"),
)


def backfill_loyalty_name_en_translations(connection) -> int:
    """Create a missing `en` Translation row (= canonical `name` column) for
    each loyalty entity that lacks one. Returns the number of rows inserted."""
    inserted = 0
    for entity_type, table in _LOYALTY_NAME_ENTITIES:
        result = connection.execute(
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
        inserted += result.rowcount or 0
    return inserted


def _seed_translation(key: str, value: str, language: str) -> None:
    existing = Translation.query.filter_by(key=key, language=language).first()
    if existing:
        existing.value = value
        existing.is_active = True
        return
    _db.session.add(
        Translation(
            key=key,
            language=language,
            value=value,
            category=key.split(".")[0] if "." in key else "general",
            is_active=True,
        )
    )


def _get_or_create_program() -> LoyaltyProgram:
    program = LoyaltyProgram.query.filter_by(is_default=True, is_active=True).first()
    if not program:
        program = LoyaltyProgram(name="Aqua Club", is_active=True, is_default=True)
        _db.session.add(program)
        _db.session.commit()
    return program


@pytest.mark.integration
class TestLoyaltyNameEnBackfill:
    def test_backfill_creates_en_translation_from_canonical_column(self, app, db, client):
        program = _get_or_create_program()

        # Canonical `name` column holds the ENGLISH text (as the admin enters it).
        streak = LoyaltyStreakRule(
            program_id=program.id,
            name="3 orders in 30 days",
            required_orders=3,
            window_days=30,
            bonus_points=300,
            is_active=True,
            display_order=0,
        )
        consec = LoyaltyConsecutiveStrikeRule(
            program_id=program.id,
            name="6 consecutive months",
            required_consecutive=6,
            combine_mode="all",
            bonus_points=600,
            is_active=True,
            display_order=0,
        )
        db.session.add_all([streak, consec])
        db.session.flush()
        consec.strikes = [streak]
        db.session.flush()

        streak_id, consec_id = streak.id, consec.id

        # Only uz + ru translation rows exist — NO `en` row (the bug precondition).
        _seed_translation(f"LoyaltyStreakRule.name.{streak_id}", "30 kun ichida 3 ta buyurtma", "uz")
        _seed_translation(f"LoyaltyStreakRule.name.{streak_id}", "3 заказа за 30 дней", "ru")
        _seed_translation(f"LoyaltyConsecutiveStrikeRule.name.{consec_id}", "Ketma-ket 6 oy", "uz")
        _seed_translation(f"LoyaltyConsecutiveStrikeRule.name.{consec_id}", "6 месяцев подряд", "ru")
        db.session.commit()

        try:
            # RED: with no `en` row, English falls back to the Uzbek translation.
            assert consec.get_translated("name", "en") == "Ketma-ket 6 oy"
            assert streak.get_translated("name", "en") == "30 kun ichida 3 ta buyurtma"

            # Run the backfill against the live connection (same call the migration makes).
            inserted = backfill_loyalty_name_en_translations(db.session.connection())
            db.session.commit()
            assert inserted >= 2  # at least the streak + consec rows above

            # Fresh instances → empty per-instance translation cache + identity map.
            db.session.expunge_all()
            consec2 = LoyaltyConsecutiveStrikeRule.query.get(consec_id)
            streak2 = LoyaltyStreakRule.query.get(streak_id)

            # GREEN: English now resolves to the canonical column value …
            assert consec2.get_translated("name", "en") == "6 consecutive months"
            assert streak2.get_translated("name", "en") == "3 orders in 30 days"
            # … and uz / ru are unchanged.
            assert consec2.get_translated("name", "uz") == "Ketma-ket 6 oy"
            assert consec2.get_translated("name", "ru") == "6 месяцев подряд"
            assert streak2.get_translated("name", "uz") == "30 kun ichida 3 ta buyurtma"

            # Idempotent: a second run inserts nothing and does not duplicate rows.
            again = backfill_loyalty_name_en_translations(db.session.connection())
            db.session.commit()
            assert again == 0
            en_rows = Translation.query.filter_by(
                key=f"LoyaltyConsecutiveStrikeRule.name.{consec_id}", language="en"
            ).count()
            assert en_rows == 1
        finally:
            Translation.query.filter(
                Translation.key.in_(
                    [
                        f"LoyaltyStreakRule.name.{streak_id}",
                        f"LoyaltyConsecutiveStrikeRule.name.{consec_id}",
                    ]
                )
            ).delete(synchronize_session=False)
            consec_row = LoyaltyConsecutiveStrikeRule.query.get(consec_id)
            if consec_row:
                consec_row.strikes = []
                db.session.delete(consec_row)
            streak_row = LoyaltyStreakRule.query.get(streak_id)
            if streak_row:
                db.session.delete(streak_row)
            db.session.commit()
