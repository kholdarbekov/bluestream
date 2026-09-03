"""Regression test: consecutive-strike joiner key must resolve without space padding.

Bug: the Jinja2 template used `(' loyalty_guide.earn.consec_and ' | t)` — the
space-padded string was passed as the translation key, which is never seeded, so
the raw key string leaked onto the public page for any rule with 2+ strikes.

Fix: the key is looked up WITHOUT padding and spaces are concatenated outside:
    `' ' ~ ('loyalty_guide.earn.consec_and' | t) ~ ' '`

RED condition (before fix): seeding 'loyalty_guide.earn.consec_and' = 'ANDJOINER'
  - the joiner renders as the literal key string ' loyalty_guide.earn.consec_and '
  - 'ANDJOINER' is NOT in the HTML
  - the raw substring 'loyalty_guide.earn.consec_and' IS in the HTML
GREEN condition (after fix): the unpadded key resolves to the seeded sentinel:
  - 'ANDJOINER' IS in the HTML
  - the raw substring 'loyalty_guide.earn.consec_and' is NOT in the HTML
"""

import pytest

from business_app import db as _db
from business_app.models.loyalty import (
    LoyaltyConsecutiveStrikeRule,
    LoyaltyProgram,
    LoyaltyStreakRule,
)
from business_app.models.translation import Translation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_translation(key: str, value: str, language: str = "uz") -> Translation:
    """Insert or replace a Translation row. Returns the new row."""
    existing = Translation.query.filter_by(key=key, language=language).first()
    if existing:
        existing.value = value
        existing.is_active = True
        return existing
    row = Translation(
        key=key,
        language=language,
        value=value,
        category=key.split(".")[0] if "." in key else "general",
        is_active=True,
    )
    _db.session.add(row)
    return row


def _get_or_create_program() -> LoyaltyProgram:
    program = LoyaltyProgram.query.filter_by(is_default=True, is_active=True).first()
    if not program:
        program = LoyaltyProgram(name="Aqua Club", is_active=True, is_default=True)
        _db.session.add(program)
        _db.session.commit()
    return program


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.api
class TestConsecutiveStrikeJoinerRender:
    """The joiner translation key must not be padded with spaces."""

    def test_joiner_key_resolves_to_sentinel_not_raw_key(self, app, db, client):
        """RED before template fix, GREEN after.

        Two LoyaltyStreakRule strikes are attached to a
        LoyaltyConsecutiveStrikeRule (combine_mode='all').  The join() call in
        the template uses the joiner variable, which must resolve to the seeded
        'ANDJOINER' sentinel rather than leaking the raw key text.
        """
        program = _get_or_create_program()

        # Create two streak (strike) rules.
        alpha = LoyaltyStreakRule(
            program_id=program.id,
            name="Alpha30",
            required_orders=3,
            window_days=30,
            bonus_points=100,
            is_active=True,
            display_order=0,
        )
        beta = LoyaltyStreakRule(
            program_id=program.id,
            name="Beta40",
            required_orders=3,
            window_days=40,
            bonus_points=100,
            is_active=True,
            display_order=1,
        )
        db.session.add_all([alpha, beta])
        db.session.flush()

        # Create the consecutive-strike rule that combines both strikes (all).
        consec = LoyaltyConsecutiveStrikeRule(
            program_id=program.id,
            name="Double Champion",
            required_consecutive=6,
            combine_mode="all",
            bonus_points=1000,
            is_active=True,
            display_order=0,
        )
        consec.strikes = [alpha, beta]
        db.session.add(consec)
        db.session.commit()

        # Seed the minimal translation keys needed to render the consec card.
        # Using language='uz' (default request language for /loyalty-guide).
        _seed_translation("loyalty_guide.earn.consec_and", "ANDJOINER", "uz")
        _seed_translation(
            "loyalty_guide.earn.consec_line_all",
            "DOlist {strikes} END",
            "uz",
        )
        _seed_translation("loyalty_guide.unit.points", "AC", "uz")
        _seed_translation("loyalty_guide.earn.consec_title", "Consecutive Streaks", "uz")
        _seed_translation("loyalty_guide.earn.consec_repeat", "Repeats every {n}", "uz")
        db.session.commit()

        try:
            # Jinja compiles+caches templates by filename on an app that is
            # session-scoped across the whole suite (tests/conftest.py's `app`
            # fixture). A PLAIN `{{ 'key' | t }}` call (no dynamic kwargs) is a
            # compile-time constant to Jinja's optimizer -- the `t` filter is
            # registered without `@pass_context` -- so whichever test renders
            # loyalty_guide.html FIRST on this worker bakes that render's
            # translation values into the compiled template for the rest of the
            # worker's life. This test passes alone but fails inside the full
            # suite for exactly that reason. Clearing the cache forces a fresh
            # compile against the sentinel rows this test just seeded, mirroring
            # tests/integration/test_loyalty_guide_cod_section.py's identical fix.
            client.application.jinja_env.cache.clear()

            resp = client.get("/loyalty-guide?lang=uz", follow_redirects=True)
            assert resp.status_code == 200, resp.get_data(as_text=True)[:500]
            html = resp.get_data(as_text=True)

            # GREEN: sentinel value is present (key resolved correctly).
            assert "ANDJOINER" in html, (
                "Expected 'ANDJOINER' in HTML — the unpadded key "
                "'loyalty_guide.earn.consec_and' was not resolved. "
                "This means the template still uses the space-padded key."
            )

            # GREEN: raw key string must not appear on the page.
            assert "loyalty_guide.earn.consec_and" not in html, (
                "Raw key 'loyalty_guide.earn.consec_and' leaked into the HTML. "
                "The template must look up the key without surrounding spaces."
            )
        finally:
            # Clean up: remove the consecutive rule and strike rules (leave program).
            db.session.delete(consec)
            db.session.delete(alpha)
            db.session.delete(beta)
            # Also remove the sentinel translation rows this test seeded, so they
            # cannot bleed into any other /loyalty-guide render test.
            Translation.query.filter(
                Translation.language == "uz",
                Translation.key.in_(
                    [
                        "loyalty_guide.earn.consec_and",
                        "loyalty_guide.earn.consec_line_all",
                        "loyalty_guide.unit.points",
                        "loyalty_guide.earn.consec_title",
                        "loyalty_guide.earn.consec_repeat",
                    ]
                ),
            ).delete(synchronize_session=False)
            db.session.commit()
