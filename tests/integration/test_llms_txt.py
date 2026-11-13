"""`/llms.txt` is rendered by Flask with live prices (plan 2026-11-12, "live price and same-day delivery visibility").

The `## Prices` section prints the offer facts in the three site languages; the document reads the same whatever
language the request names, and it is served with no redirect. It never writes the session: a fetcher with no cookie
gets none, and an existing session cookie is only re-sent by Flask's own refresh, its content unchanged."""

import re
from decimal import Decimal
from pathlib import Path

import pytest

import business_app
from business_app.services.public_offer_service import price_sentence, product_offer_facts
from tests.integration.public_page_builders import (  # noqa: F401 -- `offer_skus` is a fixture
    LANGS,
    offer_skus,
    seed_copy,
    set_price,
    shipped,
)

pytestmark = pytest.mark.integration

PRICE = Decimal("17345")


def fetch(app, path="/llms.txt", headers=None):
    app.jinja_env.cache.clear()
    return app.test_client().get(path, headers=headers or {})  # fresh client; redirects are not followed


def prices(body):
    return body.split("## Prices", 1)[1].split("\n## ", 1)[0]


def test_llms_txt_states_the_live_price_in_three_wordings(app, db, offer_skus):
    seed_copy(db)
    set_price(db, offer_skus.p19, PRICE)
    r = fetch(app)
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and r.mimetype == "text/markdown"
    assert r.headers["Cache-Control"] == "public, no-cache" and "Set-Cookie" not in r.headers
    assert body.index("## What we sell") < body.index("## Prices") < body.index("## Who we serve")
    section = prices(body)
    for lang in LANGS:
        assert price_sentence(product_offer_facts(offer_skus.p19), lang).replace("\u00a0", " ") in section
    assert "17 345 so'm" in section and "17 345 сум" in section and "UZS 17345" in section and "UZS 12340" in section
    assert "34 690" in section and "49 360" in section  # the two smallest orders
    assert section.count(shipped("landing.offer.no_deposit", "en")) == 1  # the returnable bottle only
    assert shipped("landing.offer.free_delivery", "en") in section
    assert shipped("landing.offer.as_of", "en").split("{date}")[0] in section
    assert "{" not in section and "landing.offer" not in section and "\n\n\n" not in body
    assert "\u00a0" not in body and "&#39;" not in body and "&amp;" not in body
    assert "23 450" not in body and "23450" not in body  # the fixture's first price


def test_a_discount_prints_the_payable_price(app, db, offer_skus):
    seed_copy(db)
    set_price(db, offer_skus.p19, base=Decimal("17345"), discount=Decimal("15000"))
    section = prices(fetch(app).get_data(as_text=True))
    for lang in LANGS:
        assert price_sentence(product_offer_facts(offer_skus.p19), lang).replace("\u00a0", " ") in section
    assert "15 000 so'm" in section and "15 000 сум" in section
    assert "UZS 15000" in section and "UZS 17345" not in section  # the numeric price is the payable one


@pytest.mark.parametrize(
    "path, headers",
    [
        ("/llms.txt", {"Accept-Language": "ru"}),
        ("/llms.txt?lang=uz", {}),
        ("/llms.txt?lang=ru", {}),
        ("/llms.txt?lang=en", {"Accept-Language": "ru"}),
    ],
)
def test_the_request_language_changes_nothing(app, db, offer_skus, path, headers):
    seed_copy(db)
    plain = fetch(app).get_data(as_text=True)
    r = fetch(app, path, headers)
    assert r.status_code == 200 and "Location" not in r.headers and "Set-Cookie" not in r.headers
    assert r.get_data(as_text=True) == plain


@pytest.mark.parametrize("path", ["/llms.txt", "/llms.txt?lang=en"])
def test_a_browser_with_a_russian_session_is_not_redirected(app, db, offer_skus, path):
    # What holds for a visitor who already has a session: the same document, no redirect, and the session is read,
    # never written. Flask's own refresh may re-send that permanent session's cookie; its content does not change.
    seed_copy(db)
    plain = fetch(app).get_data(as_text=True)
    browser = app.test_client()
    browser.get("/set-language/ru")
    with browser.session_transaction() as before:
        held = dict(before)
    assert held.get("language") == "ru"
    r = browser.get(path)
    assert r.status_code == 200 and "Location" not in r.headers
    assert r.get_data(as_text=True) == plain
    with browser.session_transaction() as after:
        assert dict(after) == held


def test_no_product_still_renders(app, db):
    r = fetch(app)
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "## Prices" not in body and "\n\n\n" not in body
    assert "many homes mix both sizes.\n\n## Delivery\n" in body  # the document closes up around the gap


def test_a_fee_above_zero_drops_the_word_free(app, db, offer_skus, monkeypatch):
    seed_copy(db)
    monkeypatch.setitem(app.config, "DEFAULT_DELIVERY_FEE", 5000)
    section = prices(fetch(app).get_data(as_text=True))
    assert "free" not in section.lower() and "5 000" in section


def test_llms_txt_no_longer_misdescribes_payment_and_its_links(app, db):
    body = fetch(app).get_data(as_text=True)
    # A word match: "Payme" is a prefix of the "Payment" line below.
    assert not re.search(r"\bPayme\b", body)
    assert "current pricing structure" not in body and "full descriptions" not in body
    assert "- **Payment**: cash on delivery, Click, bank transfer for legal entities" in body
    assert "## Our 11-stage filtration & mineralisation process" in body and "Sulfate (SO₄²⁻)" in body


def test_the_static_copy_is_gone():
    assert not (Path(business_app.__file__).parent / "static" / "llms.txt").exists()
