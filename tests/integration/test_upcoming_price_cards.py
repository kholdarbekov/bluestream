"""The product cards state the announced price (spec §4.3, plan Task 32).

Every test GETs the route the shop and the bots call (`/api/v1/products/`, `/products/<id>`, `POST /products/bulk`),
signed in or not, and compares with the published block in `price_change_builders.BLOCK`.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from business_app.models.price_change import PriceChangeStatus
from business_app.models.product import PriceRule
from business_app.utils.constants import PriceRuleType
from tests.integration.contract_pricing_factory import customer_headers, make_contract, make_workplace
from tests.integration.price_change_builders import APPLIED_AT, BLOCK, make_admin, make_price_change
from tests.integration.public_page_builders import offer_skus, set_price  # noqa: F401 (fixture)

pytestmark = pytest.mark.integration


@pytest.fixture
def announced(db, offer_skus):
    """p19 at 18,000 with a 20,000 change announced; p10 untouched."""
    set_price(db, offer_skus.p19, base=Decimal("18000"))
    make_price_change(db, [(offer_skus.p19, Decimal("20000"), 2, None)], created_by=make_admin(db))
    db.session.commit()
    return offer_skus


def _card(app, product, headers=None):
    resp = app.test_client().get(f"/api/v1/products/{product.id}", headers=headers or {})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["data"]["product"]["pricing"]


def test_a_customers_product_card_states_the_announced_price(app, db, announced, sample_user):
    pricing = _card(app, announced.p19, customer_headers(app, sample_user))
    assert pricing["upcoming_price"] == BLOCK
    assert pricing["current_price"] == 18000.0


def test_the_list_and_the_guest_cards_state_it_and_other_products_do_not(app, db, announced):
    client = app.test_client()
    listed = client.get("/api/v1/products/")
    assert listed.status_code == 200, listed.get_data(as_text=True)
    by_id = {p["id"]: p["pricing"]["upcoming_price"] for p in listed.get_json()["data"]["items"]}
    assert by_id[announced.p19.id] == BLOCK
    assert by_id[announced.p10.id] is None

    bulk = client.post("/api/v1/products/bulk", json={"product_ids": [announced.p19.id, announced.p10.id]})
    assert bulk.status_code == 200, bulk.get_data(as_text=True)
    served = {p["id"]: p["pricing"]["upcoming_price"] for p in bulk.get_json()["data"]["products"]}
    assert served == {announced.p19.id: BLOCK, announced.p10.id: None}

    assert _card(app, announced.p19)["upcoming_price"] == BLOCK
    assert _card(app, announced.p10)["upcoming_price"] is None


def test_nothing_is_stated_before_the_announcement_starts(app, db, offer_skus):
    set_price(db, offer_skus.p19, base=Decimal("18000"))
    make_price_change(
        db,
        [(offer_skus.p19, Decimal("20000"), 2, None)],
        created_by=make_admin(db),
        status=PriceChangeStatus.SCHEDULED,
        announcement_started_at=None,
    )
    db.session.commit()
    assert _card(app, offer_skus.p19)["upcoming_price"] is None


def test_nothing_is_stated_once_the_change_is_applied(app, db, offer_skus):
    set_price(db, offer_skus.p19, base=Decimal("18000"))
    make_price_change(
        db,
        [(offer_skus.p19, Decimal("20000"), 2, Decimal("18000"))],
        created_by=make_admin(db),
        status=PriceChangeStatus.APPLIED,
        applied_at=APPLIED_AT,
    )
    db.session.commit()
    assert _card(app, offer_skus.p19)["upcoming_price"] is None


def test_a_contract_priced_card_states_no_coming_price(app, db, announced):
    user, _address = make_workplace(db)
    make_contract(db, user, announced.p19)
    pricing = _card(app, announced.p19, customer_headers(app, user))
    assert pricing["contract_price_at_checkout"] is True
    assert pricing["upcoming_price"] is None
    assert _card(app, announced.p19)["upcoming_price"] == BLOCK


def test_an_active_price_rule_no_longer_changes_the_card(app, db, offer_skus):
    """(pf DE-5) A rule of 2,000 off leaves the card at the list price."""
    set_price(db, offer_skus.p19, base=Decimal("18000"))
    db.session.add(
        PriceRule(
            product_id=offer_skus.p19.id,
            rule_type=PriceRuleType.SEASONAL_DISCOUNT,
            name="Season",
            min_quantity=1,
            discount_type="fixed",
            discount_value=Decimal("2000"),
            is_active=True,
        )
    )
    db.session.commit()
    assert _card(app, offer_skus.p19)["current_price"] == 18000.0
