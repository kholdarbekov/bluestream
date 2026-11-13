"""Bottle terms: the Terms clause, the customer-bot charge labels and the no-deposit proof (plan 2026-11-12, Task 23)."""

import json
import re

import pytest
from flask import current_app
from flask_jwt_extended import create_access_token

from business_app.models.bottle import BottleFine, BottleLedger
from business_app.models.payment import Payment
from shared.enums import BottleLedgerEventType, PaymentMethod, UserType
from staff_bot.api_client import StaffAPIClient
from tests.integration.public_page_builders import (  # noqa: F401 -- `offer_skus` and `blog_post` are fixtures
    BLOCK_PAGES,
    LANGS,
    blog_post,
    flat,
    get_page,
    jsonld,
    offer_skus,
    page,
    shipped,
)
from tests.integration.retired_phrases import find_bottle_claims, find_retired
from tests.integration.shared_place_builders import (
    BASE_LAT,
    BASE_LNG,
    UNIT_PRICE,
    customer,
    driver,
    pinned_address,
    stop_for,
)

BANNED = re.compile(r"\bfine\b|jarima|штраф", re.I)
CUSTOMER_TYPES = (UserType.INDIVIDUAL, UserType.ENTITY)


def _bearer(user):
    token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _first_delivery_scene(db, user_type):
    u = customer(db, "Jasur", user_type=user_type)
    address = pinned_address(db, u, BASE_LAT, BASE_LNG)
    drv = driver(db, "Botir")
    order, delivery = stop_for(u, address, drv, quantity=2)
    return u, drv, order, delivery


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGS)
def test_the_terms_page_states_the_bottle_clause_without_an_amount(app, db, lang):
    heading = page(app, db, "/terms", lang).select_one("h2#returnable-bottles")
    assert heading is not None
    body = heading.find_next_sibling("p")
    assert flat(heading.get_text(" ")) == shipped("landing.terms.bottles_heading", lang)
    assert flat(body.get_text(" ")) == flat(shipped("landing.terms.bottles_body", lang))
    clause = f"{heading.get_text(' ')} {body.get_text(' ')}"
    assert not re.search(r"\d", clause) and not BANNED.search(clause)


@pytest.mark.unit
@pytest.mark.parametrize("lang", LANGS)
def test_the_clause_passes_both_wording_detectors(lang):
    text = flat(shipped("landing.terms.bottles_heading", lang) + ". " + shipped("landing.terms.bottles_body", lang))
    assert find_retired(text) == [] and find_bottle_claims(text) == []


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGS)
def test_the_charge_sentence_is_published_only_on_the_terms_page(app, db, offer_skus, blog_post, lang):
    charge = flat(shipped("landing.terms.bottles_body", lang)).split(". ")[-1]
    assert charge in flat(page(app, db, "/terms", lang).get_text(" "))
    paths = {p.format(id=offer_skus.p19.id, slug=blog_post.slug) for p in BLOCK_PAGES} | {
        "/water-delivery-faq",
        "/delivery-policy",
    }
    for path in sorted(paths):
        soup = get_page(app, path, lang)
        assert charge not in flat(soup.get_text(" ")), path
        assert charge not in json.dumps(jsonld(soup, "FAQPage"), ensure_ascii=False), path
    client = app.test_client()
    assert charge not in client.get("/llms.txt").get_data(as_text=True)
    assert charge not in json.dumps(client.get("/api/public/products.json").get_json(), ensure_ascii=False)


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGS)
def test_the_delivery_page_links_to_the_clause(app, db, lang):
    soup = page(app, db, "/delivery-policy", lang)
    assert soup.select_one('a[href*="/terms"][href$="#returnable-bottles"]') is not None


@pytest.mark.unit
def test_the_bot_labels_call_it_a_bottle_charge():
    from scripts.seed_bottle_ledger_translations import KEYS

    keys = [f"telegram.bottles.event.{name}" for name in ("fine_issued", "fine_reversed", "fine_paid")]
    assert [KEYS[key]["en"] for key in keys] == [
        "Bottle charge added",
        "Bottle charge cancelled",
        "Bottle charge paid",
    ]
    assert not any(BANNED.search(value) for key in keys for value in KEYS[key].values())


@pytest.mark.integration
@pytest.mark.parametrize("user_type", CUSTOMER_TYPES)
def test_a_new_customers_first_delivery_creates_no_bottle_charge(app, db, user_type):
    u, drv, order, delivery = _first_delivery_scene(db, user_type)
    body = StaffAPIClient.delivery_status_body("delivered", {"bottles_returned": 0})
    resp = current_app.test_client().put(
        f"/api/v1/staff/delivery/{delivery.id}/status", json=body, headers=_bearer(drv)
    )
    assert resp.status_code == 200, resp.get_json()
    assert BottleFine.query.count() == 0
    events = {row.event_type for row in BottleLedger.query.filter_by(user_id=u.id)}
    assert BottleLedgerEventType.DELIVERY in events
    assert not events & {
        BottleLedgerEventType.FINE_ISSUED,
        BottleLedgerEventType.FINE_PAID,
        BottleLedgerEventType.FINE_REVERSED,
    }
    assert [p.amount for p in Payment.query.filter_by(user_id=u.id)] == [order.total_amount] == [UNIT_PRICE * 2]


@pytest.mark.integration
@pytest.mark.parametrize("user_type", CUSTOMER_TYPES)
def test_the_driver_is_asked_for_the_price_of_the_water_only(app, db, user_type):
    u, drv, order, delivery = _first_delivery_scene(db, user_type)
    Payment.query.filter_by(order_id=order.id).delete()
    order.payment_method = PaymentMethod.CASH
    db.session.commit()
    resp = current_app.test_client().get("/api/v1/staff/delivery/active", headers=_bearer(drv))
    assert resp.status_code == 200, resp.get_json()
    row = next(item for item in resp.get_json()["data"]["items"] if item["order_id"] == order.id)
    assert row["expected_cash_to_collect"] == row["total_amount"] == float(UNIT_PRICE * 2)
    assert [(line["quantity"], line["unit_price"]) for line in row["items"]] == [(2, float(UNIT_PRICE))]
