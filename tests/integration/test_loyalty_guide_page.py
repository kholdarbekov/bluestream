"""Integration tests for the public loyalty handbook page (/loyalty-guide).

The page is config-driven via get_loyalty_handbook_context (live LoyaltyProgram +
LoyaltyTierConfig + business_config streak constants) and rendered through the
site's base.html. These tests assert it renders in all three site languages,
exposes the FAQ JSON-LD, and that the context helper reports DB-driven numbers.
"""

import pytest

from business_app import db
from business_app.models.loyalty import LoyaltyProgram, LoyaltyTierConfig
from business_app.frontend.routes import get_loyalty_handbook_context


@pytest.fixture
def loyalty_program(db):
    program = LoyaltyProgram(
        name="Aqua Club",
        is_active=True,
        is_default=True,
        uzs_per_point=250,
        signup_bonus=200,
        referral_bonus=500,
        birthday_bonus=200,
        points_expiry_days=365,
        min_redemption_points=200,
    )
    db.session.add(program)
    db.session.commit()
    return program


@pytest.fixture
def tiers(db, loyalty_program):
    rows = [
        LoyaltyTierConfig(program_id=loyalty_program.id, name="Bronze", display_order=0,
                          min_points=0, max_points=1499, points_multiplier=1.0, discount_percentage=0, is_active=True),
        LoyaltyTierConfig(program_id=loyalty_program.id, name="Silver", display_order=1,
                          min_points=1500, max_points=4999, points_multiplier=1.25, discount_percentage=2, is_active=True),
        LoyaltyTierConfig(program_id=loyalty_program.id, name="Gold", display_order=2,
                          min_points=5000, max_points=11999, points_multiplier=1.5, discount_percentage=4, is_active=True),
        LoyaltyTierConfig(program_id=loyalty_program.id, name="Platinum", display_order=3,
                          min_points=12000, max_points=None, points_multiplier=2.0, discount_percentage=6, is_active=True),
    ]
    db.session.add_all(rows)
    db.session.commit()
    return rows


@pytest.mark.integration
@pytest.mark.api
class TestLoyaltyGuidePage:
    def test_context_helper_reports_live_config(self, app, loyalty_program, tiers):
        ctx = get_loyalty_handbook_context()
        assert ctx["uzs_per_point"] == 250
        assert ctx["signup_bonus"] == 200
        assert ctx["referral_bonus"] == 500
        assert ctx["referee_bonus"] == 250  # half of referral
        assert ctx["birthday_bonus"] == 200
        assert ctx["expiry_days"] == 365
        assert len(ctx["tiers"]) == 4
        assert [t["key"] for t in ctx["tiers"]] == ["bronze", "silver", "gold", "platinum"]
        assert ctx["max_multiplier"] == 2.0
        # Worked example is derived from config (Gold tier 1.5×): 50000/250=200 → ×1.5 = 300.
        assert ctx["example"]["base"] == 200
        assert ctx["example"]["points"] == 300
        assert ctx["example"]["tier_name"] == "Gold"
        # Streak rules come from DB (LoyaltyStreakRule rows).
        assert "streak_rules" in ctx
        assert isinstance(ctx["streak_rules"], list)

    def test_context_helper_falls_back_without_program(self, app, db):
        """With no program configured, sane defaults keep the page renderable."""
        ctx = get_loyalty_handbook_context()
        assert ctx["uzs_per_point"] == 250  # bootstrap default
        assert ctx["expiry_days"] == 365
        assert ctx["tiers"] == []

    @pytest.mark.parametrize("path", ["/loyalty-guide", "/loyalty-guide?lang=ru", "/loyalty-guide?lang=en"])
    def test_page_renders_in_all_languages(self, client, loyalty_program, tiers, path):
        resp = client.get(path)
        assert resp.status_code == 200, resp.get_data(as_text=True)[:500]
        html = resp.get_data(as_text=True)
        # Config-driven content present: a tier name and the page CSS.
        assert "Platinum" in html
        assert "css/pages/loyalty-guide.css" in html
        # FAQ JSON-LD for SEO / AI grounding.
        assert 'application/ld+json' in html
        assert '"@type": "FAQPage"' in html or '"@type":"FAQPage"' in html

    def test_homepage_survives_featured_reward_with_null_description(self, client, db, loyalty_program):
        """Regression: a featured reward with a NULL description must not crash the
        homepage. index.html sliced reward.description[:50] without a None guard,
        500-ing '/' whenever such a reward was featured (e.g. '19 litrlik suv')."""
        from business_app.models.loyalty import LoyaltyReward

        db.session.add(LoyaltyReward(
            program_id=loyalty_program.id, name="Free 19L bottle", description=None,
            reward_type="free_product", points_cost=4000, is_active=True, is_featured=True,
        ))
        db.session.commit()

        resp = client.get("/", follow_redirects=True)
        assert resp.status_code == 200

    def test_page_renders_without_tiers(self, client, loyalty_program):
        """No tiers configured -> still a valid 200 (no tier cards).

        follow_redirects handles the language middleware adding an explicit
        ?lang= when a language cookie persists across the session-scoped client.
        """
        resp = client.get("/loyalty-guide", follow_redirects=True)
        assert resp.status_code == 200

    def test_tier_perk_bullets_reflect_live_config(self, client, db, loyalty_program, tiers):
        """Regression: tier perk bullets must derive their multiplier/discount
        numbers from live LoyaltyTierConfig, not from hardcoded prose.

        The multiplier/discount bullets are now SHARED, rename-proof keys
        (loyalty_guide.tier.perk_multiplier/perk_discount) rendered per tier from
        config — not per-tier keys named after the (renamable) tier name.
        """
        from business_app.models.translation import Translation

        for lang in ("en", "uz", "ru"):
            db.session.add(Translation(key="loyalty_guide.tier.perk_multiplier", language=lang,
                value="{mult}x AquaCoins on every order", category="loyalty_guide", is_active=True))
            db.session.add(Translation(key="loyalty_guide.tier.perk_discount", language=lang,
                value="{pct}% tier discount", category="loyalty_guide", is_active=True))

        # Admin edits the Gold tier away from the old hardcoded 1.5x / 4%.
        gold = next(t for t in tiers if t.name == "Gold")
        gold.points_multiplier = 1.3
        gold.discount_percentage = 3
        db.session.commit()

        resp = client.get("/loyalty-guide?lang=en")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)

        # Bullets show the live config values...
        assert "1.3x AquaCoins on every order" in html
        assert "3% tier discount" in html
        # ...and never the old hardcoded values or a literal placeholder.
        assert "4% tier discount" not in html
        assert "{pct}" not in html
        assert "{mult}" not in html
        # Bronze (0% discount) must NOT show a discount bullet.
        assert "0% tier discount" not in html

    def test_tier_name_is_driven_by_model_translation_not_static_keys(self, client, db, loyalty_program, tiers):
        """The page must render the tier name from the model's translatable field
        (LoyaltyTierConfig.name entity translation), NOT page-specific static keys
        named after the tier. So renaming a tier + setting its name translations in
        admin is reflected immediately, with no separate static key to keep in sync.
        """
        gold = next(t for t in tiers if t.name == "Gold")
        # Admin renames the tier and provides localized names (as the admin UI does
        # via tier.set_translations -> entity translations).
        gold.name = "Diamond"
        db.session.commit()
        gold.set_translated("name", "Diamond", "en")
        gold.set_translated("name", "Олмос", "ru")
        gold.set_translated("name", "Olmos", "uz")
        db.session.commit()

        ru = client.get("/loyalty-guide?lang=ru").get_data(as_text=True)
        assert "Олмос" in ru          # localized, model-driven name
        assert "Gold" not in ru       # old name must be gone everywhere
        assert "Золото" not in ru     # stale static-key translation must not leak

        en = client.get("/loyalty-guide?lang=en").get_data(as_text=True)
        assert "Diamond" in en

    def test_tagline_and_benefit_survive_a_tier_rename(self, client, db, loyalty_program, tiers):
        """Tagline + qualitative benefit bullet are keyed by display_order (a stable
        identity), so renaming a tier never drops its handbook copy."""
        from business_app.models.translation import Translation

        for lang in ("en", "uz", "ru"):
            db.session.add(Translation(key="loyalty_guide.tier.1.tagline", language=lang,
                value="For our regulars", category="loyalty_guide", is_active=True))
            db.session.add(Translation(key="loyalty_guide.tier.1.benefit1", language=lang,
                value="Priority support & faster delivery", category="loyalty_guide", is_active=True))
        # Rename the display_order=1 tier; its copy must still appear.
        silver = next(t for t in tiers if t.display_order == 1)
        silver.name = " Renamed!"
        db.session.commit()

        html = client.get("/loyalty-guide?lang=en").get_data(as_text=True)
        assert "For our regulars" in html
        assert "Priority support &amp; faster delivery" in html or "Priority support & faster delivery" in html

    def test_loyalty_guide_lists_streak_rules(self, client, db, loyalty_program):
        from business_app.models.loyalty import LoyaltyStreakRule
        db.session.add(LoyaltyStreakRule(
            program_id=loyalty_program.id, name="Frequent Buyer Bonus",
            required_orders=3, window_days=30, bonus_points=300,
            is_active=True, display_order=0,
        ))
        db.session.commit()
        resp = client.get("/loyalty-guide?lang=en")
        assert resp.status_code == 200
        assert b"Frequent Buyer Bonus" in resp.data
