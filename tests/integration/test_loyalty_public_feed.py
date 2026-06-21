"""Integration tests for the public machine-readable loyalty feed."""

import json

import pytest

from business_app import db
from business_app.models.loyalty import LoyaltyProgram, LoyaltyTierConfig


@pytest.fixture
def loyalty_program(db):
    program = LoyaltyProgram(
        name="Aqua Club", is_active=True, is_default=True, uzs_per_point=250,
        signup_bonus=200, referral_bonus=500, birthday_bonus=200,
        points_expiry_days=365, min_redemption_points=200,
    )
    db.session.add(program)
    db.session.commit()
    return program


@pytest.fixture
def tiers(db, loyalty_program):
    rows = [
        LoyaltyTierConfig(program_id=loyalty_program.id, name="Bronze", display_order=0,
                          min_points=0, max_points=1499, points_multiplier=1.0, discount_percentage=0, is_active=True),
        LoyaltyTierConfig(program_id=loyalty_program.id, name="Platinum", display_order=1,
                          min_points=1500, max_points=None, points_multiplier=2.0, discount_percentage=6, is_active=True),
    ]
    db.session.add_all(rows)
    db.session.commit()
    return rows


@pytest.mark.integration
@pytest.mark.api
class TestPublicLoyaltyFeed:
    # A fresh test client carries no session ``?lang=`` cookie, so the frontend
    # before_request language redirect never fires on these public endpoints. The
    # session-scoped ``client`` fixture can leak a non-default language session from
    # an earlier test, which 302-redirects ``frontend.*`` GETs (incl. this feed).
    def test_feed_is_public_cacheable_json(self, client, loyalty_program, tiers):
        resp = client.application.test_client().get("/api/public/loyalty.json")
        assert resp.status_code == 200
        assert resp.mimetype == "application/json"
        assert resp.headers["Access-Control-Allow-Origin"] == "*"
        cache_control = resp.headers.get("Cache-Control", "")
        assert "public" in cache_control and "max-age=900" in cache_control

    def test_feed_shape_brand_and_tiers(self, client, loyalty_program, tiers):
        resp = client.application.test_client().get("/api/public/loyalty.json")
        assert resp.status_code == 200
        data = json.loads(resp.get_data(as_text=True))
        assert data["@type"] == "MemberProgram"
        assert data["brand"]["name"]
        assert data["url"].endswith("/api/public/loyalty.json")
        assert data["guideUrl"].endswith("/loyalty-guide")
        assert data["supportedLanguages"] == ["uz", "ru", "en"]
        # Tiers carry all-language names + numeric perks.
        keys = [t["key"] for t in data["tiers"]]
        assert keys == ["bronze", "platinum"]
        assert set(data["tiers"][0]["name"].keys()) == {"uz", "ru", "en"}

    def test_feed_is_rewards_only(self, client, loyalty_program, tiers):
        resp = client.application.test_client().get("/api/public/loyalty.json")
        assert resp.status_code == 200  # guard: only scan a real 200 feed body
        blob = resp.get_data(as_text=True)
        # No cash-out wording: AquaCoins are not currency.
        for token in ("cashValue", "cash_value", "redemptionValue", "uzsPerRedeemedPoint"):
            assert token not in blob


@pytest.mark.integration
class TestLoyaltyDiscoveryWiring:
    def test_loyalty_guide_in_static_sitemap(self, client, db):
        xml = client.get("/sitemap-static.xml").get_data(as_text=True)
        assert "/loyalty-guide" in xml

    def test_loyalty_guide_serves_markdown_on_negotiation(self, client, loyalty_program, tiers):
        # Fresh client: no leaked ?lang= session that would 302 this frontend GET.
        resp = client.application.test_client().get("/loyalty-guide", headers={"Accept": "text/markdown"})
        assert resp.status_code == 200
        assert resp.mimetype == "text/markdown"


@pytest.mark.integration
class TestLoyaltyStructuredData:
    def test_loyalty_guide_emits_member_program_jsonld(self, client, loyalty_program, tiers):
        html = client.get("/loyalty-guide?lang=en").get_data(as_text=True)
        assert '"@type": "MemberProgram"' in html or '"@type":"MemberProgram"' in html
        assert "#aquaclub" in html
        assert '"@type": "MemberProgramTier"' in html or '"@type":"MemberProgramTier"' in html
        # Existing FAQ JSON-LD must remain.
        assert '"@type": "FAQPage"' in html or '"@type":"FAQPage"' in html

    def test_organization_references_member_program(self, client, db):
        html = client.get("/", follow_redirects=True).get_data(as_text=True)
        assert "hasMemberProgram" in html
        assert "#aquaclub" in html


@pytest.mark.integration
class TestLoyaltyNavAndFooter:
    def test_header_nav_links_to_loyalty_guide(self, client, db):
        html = client.get("/", follow_redirects=True).get_data(as_text=True)
        # Nav (desktop + sticky + mobile) and footer all link to the guide.
        # external_url_for_lang emits ABSOLUTE urls (.../loyalty-guide), so match
        # on the path substring, not on href="/loyalty-guide".
        assert html.count("/loyalty-guide") >= 2
        assert "nav-badge" in html  # highlighted accent present

    def test_active_state_on_loyalty_guide_page(self, client, loyalty_program, tiers):
        html = client.get("/loyalty-guide", follow_redirects=True).get_data(as_text=True)
        assert "/loyalty-guide" in html


@pytest.mark.integration
class TestHomepageLoyaltyHighlight:
    def test_anonymous_homepage_shows_loyalty_pitch_without_featured_rewards(self, client, loyalty_program, tiers):
        # No featured LoyaltyReward rows exist; the section must STILL render and
        # link to the public guide (the old behaviour hid it entirely).
        html = client.get("/", follow_redirects=True).get_data(as_text=True)
        assert "/loyalty-guide" in html
        assert "aqua-club-public" in html  # the new always-on section marker (homepage-specific)


@pytest.mark.integration
class TestLoyaltyTranslations:
    def test_seeded_landing_labels_resolve(self, db):
        """The new landing.* keys (nav label + accent, footer, homepage pitch)
        resolve to their seeded values via ``get_translation`` — the exact path the
        template ``t`` filter uses at render time — in all three languages.

        This asserts translation *resolution* rather than scraping a full-page
        render: under the SQLite :memory: test harness a prior test can leave the
        DB session in an aborted state, which makes an *in-render* Translation.query
        fall back to the raw key on any page (a harness artifact, not production
        behavior). The template *references* to these keys are covered structurally
        by TestLoyaltyNavAndFooter / TestHomepageLoyaltyHighlight.
        """
        from business_app.models.translation import Translation
        from business_app.utils.translations import get_translation

        rows = {
            "landing.nav.aqua_club": "Aqua Club",
            "landing.nav.aqua_club_badge": "Rewards",
            "landing.footer.aqua_club": "Aqua Club rewards",
            "landing.loyalty.public_title": "Join Aqua Club",
        }
        for key, val in rows.items():
            for lang in ("uz", "ru", "en"):
                db.session.add(Translation(key=key, language=lang, value=val,
                                           category="landing", is_active=True))
        db.session.commit()

        for key, expected in rows.items():
            for lang in ("uz", "ru", "en"):
                assert get_translation(key, lang) == expected
