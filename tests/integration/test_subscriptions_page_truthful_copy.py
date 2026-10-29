"""Customer pages say only what a subscription really gives (owner decisions, 2026-10-23).

A subscription gives NO discount (billing charges the regular price), delivery is free, AquaCoins are
earned at the user's tier rate, and the point of it is that the system places each order on schedule.
The /subscriptions page, the /services blurb, the shop FAQ and the "my subscriptions" tiles used to
promise "up to 20%", "2x AquaCoins", a savings readout and an always-zero "Monthly Savings" tile.

The render tests load the SHIPPED seed rows (not a local copy of the copy) for every key the page asks
for, so a key that is renamed in the template but not in the seed fails here.
"""

import json
import re
from html import unescape
from pathlib import Path

import pytest

from business_app.models.translation import Translation
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "business_app" / "templates" / "frontend"
STATIC_JS = REPO / "business_app" / "static" / "js" / "pages"

PAGE = TEMPLATES / "subscriptions.html"
LANGS = ("en", "uz", "ru")

AUTO_ORDER = {
    "en": "Your subscription places each order for you on schedule, so you never forget to order water "
    "and never run out when you need it most.",
    "uz": "Obunangiz har bir buyurtmani jadval bo'yicha o'zi beradi",
    "ru": "Подписка сама оформляет каждый заказ по расписанию",
}
FREE_DELIVERY = {
    "en": "Delivery is always free, on subscription deliveries as on every order.",
    "uz": "Yetkazib berish doim bepul",
    "ru": "Доставка всегда бесплатная",
}
COINS = {
    "en": "Earn AquaCoins on every subscription delivery at your tier's rate, and redeem them for rewards.",
    "uz": "darajangiz stavkasi bo'yicha AquaCoins",
    "ru": "AquaCoins по ставке вашего уровня",
}
REGULAR_PRICE_EN = "Subscription deliveries are billed at the regular price."

# Words the page may not carry in any language. "SMS": SMS is OTP-only. "2x"/"bonus": no multiplier.
FORBIDDEN = {
    "en": ["20%", "discount", "your savings", "savings", "2x", "bonus", "sms", "at checkout", "10,000"],
    "uz": ["20%", "chegirma", "tejash", "tejay", "sms", "bonus", "2x"],
    "ru": ["20%", "скидк", "эконом", "sms", "бонус", "2x"],
}


def _page_keys() -> set[str]:
    source = PAGE.read_text(encoding="utf-8")
    raw = re.findall(r"\{\{\s*'((?:[^'\\]|\\.)*)'\s*\|\s*(?:t|translate)\b", source)
    return {k.replace("\\'", "'") for k in raw}


def _seed_shipped_rows(db, keys: set[str]) -> None:
    for key in keys & set(BACKEND_TRANSLATIONS):
        for lang in LANGS:
            value = BACKEND_TRANSLATIONS[key].get(lang) or BACKEND_TRANSLATIONS[key]["en"]
            db.session.add(
                Translation(key=key, language=lang, value=value, category=_category_for(key), is_active=True)
            )
    db.session.commit()


def _render(client, db, path: str, lang: str, keys: set[str]) -> str:
    _seed_shipped_rows(db, keys)
    # `{{ 'literal'|t }}` is constant-folded into the cached template at first render.
    client.application.jinja_env.cache.clear()
    return _get(client, f"{path}?lang={lang}").get_data(as_text=True)


def _get(client, url: str, **kwargs):
    # `?lang=` stores the language in the session and redirects: follow it on ONE client.
    return client.application.test_client().get(url, follow_redirects=True, **kwargs)


def _visible_text(html: str) -> str:
    """What a visitor can read: no <style>/<script> bodies (`left: 20%` is CSS, not a promise), no tags."""
    html = re.sub(r"(?is)<(style|script)\b.*?</\1>", " ", html)
    return re.sub(r"(?s)<[^>]+>", " ", html)


@pytest.mark.integration
@pytest.mark.api
@pytest.mark.parametrize("lang", LANGS)
def test_subscriptions_page_carries_the_real_benefits_and_no_discount(client, db, lang):
    html = unescape(_render(client, db, "/subscriptions", lang, _page_keys()))

    assert AUTO_ORDER[lang] in html
    assert FREE_DELIVERY[lang] in html
    assert COINS[lang] in html
    lowered = _visible_text(html).lower()
    for word in FORBIDDEN[lang]:
        assert word.lower() not in lowered, f"/subscriptions?lang={lang} still says {word!r}"
    assert "preview-savings" not in html
    assert 'Subscribe" at checkout' not in html and "Subscribe&#34; at checkout" not in html


@pytest.mark.integration
@pytest.mark.api
def test_subscriptions_page_says_once_that_deliveries_are_billed_at_the_regular_price(client, db):
    html = unescape(_render(client, db, "/subscriptions", "en", _page_keys()))
    assert REGULAR_PRICE_EN in html


PAID_ORDER_STAYS = {
    "en": "An order you have already paid for, or one already picked up for delivery, cannot be skipped or paused: "
    "it stays and is delivered, and the change applies from the following delivery.",
    "uz": "Siz allaqachon to'lagan yoki kuryer yetkazib berish uchun olib ketgan buyurtmani o'tkazib yuborib ham, "
    "to'xtatib ham bo'lmaydi: u qoladi va yetkaziladi",
    "ru": "Уже оплаченный заказ или заказ, который курьер уже забрал для доставки, нельзя пропустить или "
    "приостановить: он остаётся и будет доставлен",
}
NO_INVOICE_BILLING = {"en": "invoice billing", "uz": "hisob-faktura", "ru": "оплата по счёту"}


@pytest.mark.integration
@pytest.mark.api
@pytest.mark.parametrize("lang", LANGS)
def test_subscriptions_page_matches_the_pause_skip_cancel_rule_and_promises_no_invoice(client, db, lang):
    """Spec 2026-10-23 §2.7, §4.6, §9: pause, skip and cancel cancel only an unpaid upcoming order; a paid one, or
    one the driver already picked up, cannot be skipped or paused and stays (final review M12). No monthly
    statement exists, so the offices card no longer lists "Invoice billing"."""
    html = unescape(_render(client, db, "/subscriptions", lang, _page_keys()))

    assert PAID_ORDER_STAYS[lang] in html
    assert "Invoice billing" not in html
    assert NO_INVOICE_BILLING[lang] not in _visible_text(html).lower()


@pytest.mark.integration
@pytest.mark.api
def test_subscriptions_page_points_to_the_real_ways_to_subscribe(client, db):
    """The home-page builder and the Telegram bot are the two real routes; checkout has no Subscribe option."""
    html = _render(client, db, "/subscriptions", "en", _page_keys())
    assert "/#subscription-constructor" in html
    assert "t.me/" in html
    checkout = (TEMPLATES / "checkout.html").read_text(encoding="utf-8").lower()
    assert "subscri" not in checkout, "checkout grew a Subscribe option: the page copy must be revisited"


@pytest.mark.integration
@pytest.mark.api
@pytest.mark.parametrize("lang", LANGS)
def test_services_page_subscription_blurb_is_the_auto_order_message(client, db, lang):
    key = "landing.services.subscription_plans.description"
    assert key in BACKEND_TRANSLATIONS, "the services blurb is DB-only no more: it must ship in the seed"
    html = unescape(_render(client, db, "/services", lang, {key}))
    value = BACKEND_TRANSLATIONS[key][lang]
    assert value in html
    assert "discount" not in value.lower() and "chegirma" not in value.lower() and "скидк" not in value.lower()


@pytest.mark.unit
def test_new_subscription_copy_is_trilingual_and_free_of_discount_words():
    keys = _page_keys() & set(BACKEND_TRANSLATIONS)
    # The seed must cover the new strings; the unseeded remainder is pre-existing.
    for must in (
        AUTO_ORDER["en"],
        FREE_DELIVERY["en"],
        COINS["en"],
        "What do I get with a subscription?",
        "landing.services.subscription_plans.description",
    ):
        assert must in BACKEND_TRANSLATIONS, f"{must[:50]!r} is not seeded"
    for key in keys | {"landing.services.subscription_plans.description"}:
        row = BACKEND_TRANSLATIONS[key]
        for lang in LANGS:
            assert row.get(lang), f"{key[:50]!r} has no {lang}"
        text = " ".join(row[lang] for lang in LANGS).lower()
        for word in ("20%", "2x", "bonus", "sms", "discount", "chegirma", "скидк", "бонус"):
            assert word not in text, f"{key[:50]!r} still says {word!r}"
    # The orphaned promises are gone from the seed.
    stale = [k for k in BACKEND_TRANSLATIONS if "up to 20%" in k or "2x multiplier" in k or k.startswith("Earn bonus")]
    assert stale == []


@pytest.mark.unit
def test_page_script_has_no_discount_table_or_savings_readout():
    source = (STATIC_JS / "subscriptions.js").read_text(encoding="utf-8")
    assert not re.search(r"\b(daily|weekly|biweekly|monthly)\s*:\s*\d+", source), "a per-frequency percentage table is back"
    for word in ("discount", "savings", "preview-savings"):
        assert word not in source.lower()


@pytest.mark.unit
def test_my_subscriptions_has_no_always_zero_savings_tile():
    html = (TEMPLATES / "my_subscriptions.html").read_text(encoding="utf-8")
    js = (STATIC_JS / "my-subscriptions.js").read_text(encoding="utf-8")
    routes = (REPO / "business_app" / "frontend" / "routes.py").read_text(encoding="utf-8")
    assert "monthlySavings" not in html + js
    assert "Monthly Savings" not in html
    assert "calculate_monthly_savings" not in routes and "monthly_savings" not in routes


@pytest.mark.integration
@pytest.mark.api
def test_my_subscriptions_page_renders_without_the_savings_tile(client, db, auth_headers):
    browser = client.application.test_client()
    browser.set_cookie("access_token_cookie", auth_headers["Authorization"].split(" ", 1)[1])
    response = browser.get("/my-subscriptions", follow_redirects=True)
    assert response.status_code == 200, response.headers.get("Location")
    html = response.get_data(as_text=True)
    assert "monthlySavings" not in html


@pytest.mark.integration
@pytest.mark.api
@pytest.mark.parametrize("lang", LANGS)
def test_shop_faq_does_not_promise_a_subscription_price_cut(client, db, lang):
    # The shop template %-formats this one translated string; the test DB starts without it.
    db.session.add(Translation(key="landing.shop.results_count", language=lang, value="%s-%s of %s",
                               category="landing", is_active=True))
    db.session.commit()
    client.application.jinja_env.cache.clear()
    html = _get(client, f"/shop?lang={lang}").get_data(as_text=True)
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, flags=re.S)
    faq = [json.loads(b) for b in blocks if '"FAQPage"' in b]
    assert faq, "the shop FAQPage JSON-LD disappeared"
    text = json.dumps(faq, ensure_ascii=False).lower()
    for word in ("lower unit", "reduce cost", "снизить стоимость", "narxini tushir", "foyda bormi"):
        assert word not in text, f"shop FAQ ({lang}) still promises a subscription saving: {word!r}"
    assert "subscri" in text or "подписк" in text or "obuna" in text
