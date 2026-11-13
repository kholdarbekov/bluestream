"""Public pages promise only what the business does (honest-ETA Phase 1, spec 2026-11-03).

The website promised delivery the business does not offer. The dev call to action (scripts/seed_data.py) said
"within 2 hours anywhere in the city"; prod's own second sentence promised timing differently ("fit your schedule",
"kafolatlaymiz", "без лишних ожиданий"; see below). The footer said "Delivery: 24/7", and the loyalty guide sold
Silver "faster delivery" and Platinum "free express delivery". There is no two-hour guarantee, no round-the-clock
delivery and no express lane.
What is true for every customer: they can follow their delivery in the Telegram bot (Track) or on the website
(/my-orders, the order tracking page). The call to action promises no message, because a phone-only website
customer has no linked bot and no real email, so nothing can be sent to them (SMS is OTP-only). It promises no
timing either: prod's own second sentence ("designed to fit your schedule" and its uz and ru twins) is gone. Its first
sentence, the owner's own wording about Tashkent and the region, stays.

Each test GETs the real page in every site language with the rows the seed ships. The two landing keys were
DB-only until now, so no deploy could ever replace the live promise; they ship from the seed from here on.
Rows come from `BACKEND_TRANSLATIONS`, never from a copy kept in this file. The expected sentences below pin the
approved wording, so an edit to the seed has to be an edit here too.
"""

import re

import pytest

from business_app.models.loyalty import LoyaltyProgram, LoyaltyTierConfig
from tests.integration.public_page_builders import flat as _flat, page as _page, shipped as _shipped

LANGS = ("en", "uz", "ru")

CTA_KEY = "landing.cta.description"
FOOTER_KEY = "landing.footer.delivery_hours"

# Any way of promising two hours, in the three site languages: "2 hours", "2-hour", "2 hrs", "two hours", "2 soat",
# "ikki soat", "2 часа", "2 часов", "два часа". The retired dev call to action said "within 2 hours", "2 soat ichida"
# and "в течение 2 часов". `\b` keeps "12 hours" out, and `час(?!т)` keeps "2 части" (two parts) out. Case-insensitive,
# and it reads through any hyphen or dash. Run it on `_flat` text, which folds NBSP and newlines into plain spaces.
TWO_HOUR_PROMISE = re.compile(
    r"\b(?:2|two|ikki|два|двух)[\s\-\u2010-\u2015]*(?:hours?|hrs?|soat|час(?!т))", re.IGNORECASE
)
# The first version of the call to action promised a message ("we'll message you when your water is on the way").
# A phone-only website customer has no channel for one: no linked bot, no real email, and SMS is OTP-only.
MESSAGE_PROMISES = ("message you", "xabar beramiz", "мы напишем вам")
# Prod's own second sentences promised timing: "designed to fit your schedule", uz "kafolatlaymiz" (we guarantee) and
# ru "в удобное для вас время, без лишних ожиданий". The honest sentence below replaced them on purpose, and these
# phrases keep them from coming back.
TIMING_PROMISES = ("fit your schedule", "kafolatlaymiz", "без лишних ожиданий")
# The call to action's first sentence is prod's live wording, the owner's own: Tashkent and the region, their own
# fleet. The dev sentence about "delivery partners across Uzbekistan" was less true, so the seed carries prod's.
CTA_FIRST_SENTENCE = {
    "en": "We proudly serve Tashkent and the surrounding region with our own dedicated delivery fleet.",
    "uz": "Biz Toshkent shahri va viloyati bo‘ylab o‘zimizning maxsus transport xizmatimizni taklif etamiz.",
    "ru": "Мы обслуживаем Ташкент и область, используя собственный профессиональный автопарк.",
}
# What the call to action says after it, instead of the promises: every customer can follow the delivery in the bot
# or on the website.
CTA_NEW_SENTENCE = {
    "en": "Order in a few taps and follow your delivery in our Telegram bot or on the website.",
    "uz": "Bir necha bosishda buyurtma bering va yetkazib berishni Telegram botimizda yoki saytda kuzatib boring.",
    "ru": "Закажите в несколько касаний и следите за доставкой в нашем Telegram-боте или на сайте.",
}
FOOTER_DELIVERY_LINE = {
    "en": "Delivery: every day",
    "uz": "Yetkazib berish: har kuni",
    "ru": "Доставка: каждый день",
}

# The delivery promises the tier perks used to carry. They are checked on the perk bullets only: the same page says
# AquaCoins add up "faster" (points, not delivery), which is true and stays.
DELIVERY_UPSELL_WORDS = ("faster", "express", "tezroq", "ekspress", "tezkor", "быстр", "экспресс", "срочн")
# The uz perk keeps the seed's typographic apostrophe (U+2018) in "qo‘llab".
SILVER_PERK = {
    "en": "Priority support",
    "uz": "Ustuvor qo‘llab-quvvatlash",
    "ru": "Приоритетная поддержка",
}
PLATINUM_PERK = {
    "en": "Dedicated manager",
    "uz": "Shaxsiy menejer",
    "ru": "Персональный менеджер",
}


@pytest.fixture
def loyalty_tiers(db):
    """The four tiers the guide prints. `display_order` (0-3) is the number the perk keys are looked up by."""
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
        surprise_enabled=False,
    )
    db.session.add(program)
    db.session.flush()
    for order, name, low, high, multiplier, discount in (
        (0, "Bronze", 0, 1499, 1.0, 0),
        (1, "Silver", 1500, 4999, 1.25, 2),
        (2, "Gold", 5000, 11999, 1.5, 4),
        (3, "Platinum", 12000, None, 2.0, 6),
    ):
        db.session.add(
            LoyaltyTierConfig(
                program_id=program.id,
                name=name,
                display_order=order,
                min_points=low,
                max_points=high,
                points_multiplier=multiplier,
                discount_percentage=discount,
                is_active=True,
            )
        )
    db.session.commit()


@pytest.mark.integration
@pytest.mark.api
@pytest.mark.parametrize("lang", LANGS)
def test_the_landing_call_to_action_makes_no_time_promise(app, db, lang):
    shipped = _shipped(CTA_KEY, lang)
    soup = _page(app, db, "/", lang, ("landing.",))

    paragraph = soup.select_one("section.cta-section .text p")
    assert paragraph is not None, "the call to action lost its paragraph"
    text = paragraph.get_text(" ", strip=True)
    assert text == shipped
    for promise in MESSAGE_PROMISES:
        assert promise not in _flat(text).lower(), f"/?lang={lang} call to action still promises a message: {promise!r}"
    for promise in TIMING_PROMISES:
        assert promise not in _flat(text).lower(), f"/?lang={lang} call to action still promises timing: {promise!r}"
    assert text == f"{CTA_FIRST_SENTENCE[lang]} {CTA_NEW_SENTENCE[lang]}"

    # The scan covers the whole section but sees only what the seed owns. The section's title and feature rows are
    # DB-only, so in this test they render as key names and their live text is not checked here.
    section = _flat(soup.select_one("section.cta-section").get_text(" "))
    found = TWO_HOUR_PROMISE.search(section)
    assert found is None, f"/?lang={lang} call to action still promises {found.group(0)!r}"


@pytest.mark.integration
@pytest.mark.api
@pytest.mark.parametrize("lang", LANGS)
def test_the_footer_says_every_day_not_24_7(app, db, lang):
    shipped = _shipped(FOOTER_KEY, lang)
    soup = _page(app, db, "/", lang, ("landing.",))

    lines = [_flat(item.get_text(" ")) for item in soup.select(".schedule-box li")]
    assert _flat(shipped) in lines, f"/?lang={lang} footer does not show the shipped delivery line: {lines}"
    assert FOOTER_DELIVERY_LINE[lang] in lines
    assert not any("24/7" in line for line in lines), f"/?lang={lang} footer still says 24/7: {lines}"


@pytest.mark.integration
@pytest.mark.api
@pytest.mark.parametrize("lang", LANGS)
def test_the_loyalty_guide_promises_no_faster_or_express_delivery(app, db, loyalty_tiers, lang):
    soup = _page(app, db, "/loyalty-guide", lang, ("loyalty_guide.",))

    perks = {
        card.select_one(".lg-tier__name").get_text(strip=True): [
            _flat(item.get_text(" ")) for item in card.select(".lg-tier__perks li")
        ]
        for card in soup.select("article.lg-tier")
    }
    assert set(perks) == {"Bronze", "Silver", "Gold", "Platinum"}, f"/loyalty-guide?lang={lang} lost a tier card"

    for tier, bullets in perks.items():
        for bullet in bullets:
            for word in DELIVERY_UPSELL_WORDS:
                assert word not in bullet.lower(), f"/loyalty-guide?lang={lang} {tier} still sells {word!r}: {bullet!r}"

    # Tier 1 and tier 3 keep the support and the manager, as bullets of their own.
    assert SILVER_PERK[lang] in perks["Silver"]
    assert PLATINUM_PERK[lang] in perks["Platinum"]


@pytest.mark.unit
def test_the_two_hour_detector_catches_every_spelling_and_ignores_other_numbers():
    caught = (
        # The retired DEV call to action (scripts/seed_data.py), in each language.
        "Experience fast, reliable delivery within 2 hours anywhere in the city.",
        "Shaharning istalgan joyida 2 soat ichida tez va ishonchli yetkazib berish.",
        "Быстрая и надежная доставка в течение 2 часов в любую точку города.",
        # Other ways to say it: hyphen, abbreviation, spelled out, capitals, NBSP, narrow NBSP, en dash, newline.
        "Delivery in 2-hour windows",
        "Arrives in 2 hrs",
        "2hr delivery",
        "Two hours or it is free",
        "TWO-HOUR DELIVERY",
        "within 2\u00a0hours",
        "within 2\u202fhours",
        "2\u2013hour slots",
        "within 2\nhours",
        "ikki soat ichida",
        "Доставим за 2 часа",
        "2-часовая доставка",
        "два часа",
        "в течение двух часов",
    )
    ignored = (
        "12 hours",
        "22 hours",
        "20 soat",
        "22 часа",
        "2 parts",
        "between two houseboats",
        "2 части",
        "2 частных клиента",
        "Mon - Sat: 9AM - 6PM.",
        *CTA_FIRST_SENTENCE.values(),
        *CTA_NEW_SENTENCE.values(),
    )
    for text in caught:
        assert TWO_HOUR_PROMISE.search(_flat(text)), f"the detector missed {text!r}"
    for text in ignored:
        assert TWO_HOUR_PROMISE.search(_flat(text)) is None, f"the detector fired on {text!r}"
