"""/loyalty-guide states the tier discount is a cash-on-delivery benefit via a
quiet asterisk + footnote, not a dedicated marketing section.

INTENT CHANGE (2026-08-27): the owner decided the earlier full-width COD
section (a "Cash benefit" kicker, an h2, a lede, a two-card "qualifies" vs
"online" grid, a worked example with a basis-minus-saved math strip, and a
reassurance callout, all gated on `handbook.cod_example`) was too loud for
what is a small, honest disclosure — "I don't want to scream about our
discounts being applied to only cash orders." This module used to assert
that section existed in every language; it now asserts its replacement: a
`*` on the tier card's discount stat (accessibly tied via `aria-describedby`
to a fine-print footnote rendered once beneath the tier grid). This is a
deliberate rewrite of the earlier tests' intent, not a regression being
papered over — the underlying business rule (the discount is COD-only) is
unchanged and still enforced here.

`cod_example` / `_uzs_display` were removed from get_loyalty_handbook_context
(business_app/frontend/routes.py) because nothing but the deleted section
consumed them (verified by grep before deletion). The `loyalty_guide.cod.*`
translation keys were deleted from the seeder for the same reason; existing
DB rows for those keys are harmless dead data, not migrated away.

Every number here comes from tier rows this file seeds itself. Production and
dev hold different percentages and neither may leak into an assertion.
"""

import re

import pytest

from business_app.models.loyalty import LoyaltyProgram, LoyaltyTierConfig
from business_app.models.translation import Translation


@pytest.fixture
def program(db):
    row = LoyaltyProgram(
        name="Aqua Club",
        is_active=True,
        is_default=True,
        uzs_per_point=250,
        signup_bonus=200,
        referral_bonus=500,
        birthday_bonus=200,
        points_expiry_days=365,
        min_redemption_points=200,
        surprise_enabled=False,
    )
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def tiers(db, program):
    """Deliberately unlike dev and unlike production: names and a rate that
    exist nowhere but this file."""
    rows = [
        LoyaltyTierConfig(
            program_id=program.id, name="Base", display_order=0, min_points=0, max_points=999,
            points_multiplier=1.0, discount_percentage=0, is_active=True,
        ),
        LoyaltyTierConfig(
            program_id=program.id, name="Summit", display_order=1, min_points=1000, max_points=None,
            points_multiplier=2.0, discount_percentage=20, is_active=True,
        ),
    ]
    db.session.add_all(rows)
    db.session.commit()
    return rows


def _seed(db, key: str, value: str, lang: str | None = None) -> None:
    langs = (lang,) if lang else ("en", "uz", "ru")
    for lg in langs:
        db.session.add(
            Translation(key=key, language=lg, value=value, category="loyalty_guide", is_active=True)
        )
    db.session.commit()


@pytest.mark.integration
@pytest.mark.api
def test_tier_card_states_the_discount_is_for_cash_orders(client, db, program, tiers):
    """The stat and the perk bullet are the two places the card publishes the
    rate. Both must carry the condition, and neither may embed the rate itself."""
    _seed(db, "loyalty_guide.tier.label_discount", "cash-order discount")
    _seed(db, "loyalty_guide.tier.perk_discount", "{pct}% off when you pay cash on delivery")

    # `{{ 'key' | t }}` with no arguments is a compile-time constant for
    # Jinja (the filter is not @pass_context), so the value is folded into the
    # cached template the FIRST time this worker renders the page — a seed
    # committed afterwards would never appear. Clearing the cache is what the
    # other tests in this file and test_loyalty_guide_consecutive_render.py do.
    client.application.jinja_env.cache.clear()

    # A fresh client, not the session-scoped `client` fixture: its cookie jar
    # can carry a leaked `?lang=` session from an earlier test in this worker
    # (see test_loyalty_public_feed.py's `client.application.test_client()`
    # convention), which would render this page in the wrong language.
    html = client.application.test_client().get("/loyalty-guide?lang=en").get_data(as_text=True)

    assert "20% off when you pay cash on delivery" in html
    assert "cash-order discount" in html
    # The 0% tier still shows no perk bullet. A plain substring check for
    # "0% off..." is a false negative here: it also matches inside the
    # Summit tier's "20% off..." bullet. Anchor on the digit boundary so the
    # standalone "0%" bullet is what's actually being ruled out.
    assert re.search(r"(?<!\d)0% off when you pay cash on delivery", html) is None
    assert "{pct}" not in html


@pytest.mark.integration
@pytest.mark.api
def test_zero_discount_tier_shows_no_discount_stat(client, db, program, tiers):
    """The tier card's two-stat list (multiplier, discount) must omit — not
    zero-render — the discount figure for a 0% tier (Base), with no dangling
    asterisk either. Summit (20%) still gets its stat, now carrying the `*`
    marker that ties it to the footnote via aria-describedby."""
    _seed(db, "loyalty_guide.tier.label_multiplier", "points multiplier")
    _seed(db, "loyalty_guide.tier.label_discount", "cash-order discount")

    # A plain `{{ 'key' | t }}` call (no dynamic kwargs) is a compile-time
    # constant to Jinja's optimizer, so whichever test renders this template
    # FIRST on this worker bakes that render's translation result into the
    # compiled template for the rest of the worker's life. Clearing the cache
    # forces a fresh compile against the rows this test just committed.
    client.application.jinja_env.cache.clear()
    html = client.application.test_client().get("/loyalty-guide?lang=en").get_data(as_text=True)

    # Summit (20%) gets its stat, with the accessible asterisk marker...
    assert (
        '<strong>20%<sup class="lg-stat__sup" aria-describedby="lg-tier-discount-note">*</sup></strong>'
        '<span>cash-order discount</span>' in html
    )
    # ...but Base (0%) must not render a "0%" discount stat, or an asterisk, at all.
    assert "<strong>0%<sup" not in html
    assert "<strong>0%</strong><span>cash-order discount</span>" not in html
    # The multiplier stat is unaffected either way — the card never ends up
    # with zero stats.
    assert html.count("points multiplier") == len(tiers)


@pytest.mark.integration
@pytest.mark.api
def test_discount_stat_does_not_truncate_a_fractional_rate(client, db, program, tiers):
    """Rates are admin-set Float columns, not integers — `| int` would floor
    2.5% down to "2%". The stat must format the same way the bot's
    `_format_rate` already does (trim trailing zeros, never invent precision),
    not truncate — and the new asterisk marker must not disturb that."""
    summit = next(t for t in tiers if t.name == "Summit")
    summit.discount_percentage = 2.5
    db.session.commit()

    _seed(db, "loyalty_guide.tier.label_discount", "cash-order discount")

    # `{{ 'key' | t }}` with no arguments is a compile-time constant for
    # Jinja (the filter is not @pass_context), so the value is folded into the
    # cached template the FIRST time this worker renders the page — a seed
    # committed afterwards would never appear. Clearing the cache is what the
    # other tests in this file and test_loyalty_guide_consecutive_render.py do.
    client.application.jinja_env.cache.clear()

    html = client.application.test_client().get("/loyalty-guide?lang=en").get_data(as_text=True)

    assert (
        '<strong>2.5%<sup class="lg-stat__sup" aria-describedby="lg-tier-discount-note">*</sup></strong>'
        '<span>cash-order discount</span>' in html
    )
    assert "<strong>2%<sup" not in html
    assert "<strong>2%</strong><span>cash-order discount</span>" not in html


@pytest.mark.integration
@pytest.mark.api
def test_discount_footnote_renders_in_every_language(client, db, program, tiers):
    """The footnote is new copy (loyalty_guide.tier.discount_footnote). It must
    render the SEEDED row for each language — never the raw dotted key, and
    never another language's row leaking through (DEFAULT_LANGUAGE=uz would
    otherwise silently render Uzbek to an English reader)."""
    markers = {
        "en": "EN-ONLY tier discount footnote marker",
        "uz": "UZ-ONLY daraja chegirmasi izohi",
        "ru": "RU-ONLY сноска о скидке уровня",
    }
    for lang, marker in markers.items():
        _seed(db, "loyalty_guide.tier.discount_footnote", marker, lang=lang)

    for lang, marker in markers.items():
        # The `| t` call here takes no dynamic kwargs, so Jinja constant-folds
        # it into the COMPILED TEMPLATE the first time any request renders
        # this file — and that compiled template is shared across languages
        # (Jinja caches by filename, not by request language). Left uncleared,
        # only the FIRST language rendered this run would ever appear; every
        # other language's request would silently replay that first render's
        # baked-in string. Clearing before each request forces a fresh
        # compile — and a fresh `t()` evaluation — against THIS request's
        # active language.
        client.application.jinja_env.cache.clear()
        # follow_redirects: ?lang=uz matches DEFAULT_LANGUAGE, which
        # before_request 301-redirects to the bare path (SEO canonicalization,
        # see test_loyalty_guide_page.py's identical convention) — the
        # footnote still renders once the redirect is followed.
        html = client.application.test_client().get(
            f"/loyalty-guide?lang={lang}", follow_redirects=True
        ).get_data(as_text=True)
        assert 'id="lg-tier-discount-note"' in html, f"footnote container missing in {lang}"
        assert marker in html, f"footnote did not render the {lang} row"
        for other_lang, other_marker in markers.items():
            if other_lang != lang:
                assert other_marker not in html, f"{lang} page leaked the {other_lang} footnote row"
        assert "loyalty_guide.tier.discount_footnote" not in html


@pytest.mark.integration
@pytest.mark.api
def test_no_active_tier_discount_shows_no_asterisk_or_footnote(client, db, program, tiers):
    """An operator who zeroes every rate must not leave a page pointing a
    reader at a discount nobody gets — no asterisk marker and no footnote
    line, mirroring the old section's disappearance at zero."""
    for tier in tiers:
        tier.discount_percentage = 0
    db.session.commit()

    html = client.application.test_client().get("/loyalty-guide?lang=en").get_data(as_text=True)

    assert "<sup" not in html
    assert 'id="lg-tier-discount-note"' not in html


@pytest.mark.integration
def test_shipped_tier_discount_copy_names_the_payment_condition():
    """The DEFAULT catalogue — what actually ships to customers — must carry
    the condition everywhere the rate is published: the stat label, the perk
    bullet, and the footnote the asterisk points to."""
    from scripts.seed_backend_translations import LOYALTY_GUIDE_TRANSLATIONS

    label = LOYALTY_GUIDE_TRANSLATIONS["loyalty_guide.tier.label_discount"]
    perk = LOYALTY_GUIDE_TRANSLATIONS["loyalty_guide.tier.perk_discount"]
    footnote = LOYALTY_GUIDE_TRANSLATIONS["loyalty_guide.tier.discount_footnote"]

    assert "cash" in label["en"].lower()
    assert "naqd" in label["uz"].lower()
    assert "налич" in label["ru"].lower()
    assert "cash" in perk["en"].lower()
    assert "naqd" in perk["uz"].lower()
    assert "налич" in perk["ru"].lower()
    assert "cash" in footnote["en"].lower()
    assert "naqd" in footnote["uz"].lower()
    assert "налич" in footnote["ru"].lower()
    for lang in ("en", "uz", "ru"):
        assert footnote.get(lang), f"discount_footnote has no {lang} value"


@pytest.mark.integration
def test_no_tier_discount_copy_states_a_literal_percentage():
    """Production tier rates differ from dev's. A rate baked into copy is a
    promise the pricing engine never made — it must arrive as the {pct} param."""
    from scripts.seed_backend_translations import LOYALTY_GUIDE_TRANSLATIONS

    offenders = []
    for key, row in LOYALTY_GUIDE_TRANSLATIONS.items():
        if "discount" not in key:
            continue
        for lang, value in row.items():
            if value and re.search(r"\d\s*%", value):
                offenders.append(f"{key}[{lang}]: {value}")

    assert offenders == [], f"literal percentage in tier-discount copy: {offenders}"
