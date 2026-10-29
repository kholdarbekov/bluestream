"""The website's order pages show the three discounts the server publishes (spec 2026-10-23, section 6).

Before, the confirmation page and the two order-detail renderers listed Subtotal, Delivery fee and
Total with no discount row, so a discounted order did not add up. Each row is drawn only from the
order's own `tier_discount`, `discount_amount` (subscription) or `loyalty_discount` (reward) when it
is above zero; no page decides eligibility.

The storefront has no JS runner, so the two renderers' half of the contract is pinned through their
source and the PAGE_DATA labels they read. The confirmation page is a real GET as the customer.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from business_app.models.order import Order
from shared.enums import UserRole
from tests.integration.contract_pricing_factory import customer_headers, make_staff
from tests.integration.test_tier_discount_self_service_only import _admin_order
from tests.integration.test_subscription_min_order_quantity import _no_external_io  # noqa: F401  (autouse)
from tests.integration.tier_discount_factory import post_order, seed_program, seed_tier, verify_phone

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
TIER_LABEL_START = "Tier discount (bot"
CONFIRMATION_URL = "/order-confirmation?order_id={order_id}&lang=en"


@pytest.fixture
def customer(db, sample_user):
    seed_tier(db, seed_program(db), name="Base", rate=Decimal("5"))
    verify_phone(db, sample_user)
    return sample_user


def _confirmation_html(app, user, order_id):
    response = app.test_client().get(CONFIRMATION_URL.format(order_id=order_id), headers=customer_headers(app, user))
    assert response.status_code == 200, response.get_data(as_text=True)[:500]
    return BeautifulSoup(response.get_data(as_text=True), "html.parser").select_one(".totals-section").get_text(" ", strip=True)


def test_order_confirmation_shows_the_tier_saving(app, db, customer, sample_product, user_address):
    resp = post_order(
        app, customer_headers(app, customer), product_id=sample_product.id, address_id=user_address.id,
        payment_method="cash", source="web",
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    order = Order.query.get(resp.get_json()["data"]["order"]["id"])
    assert order.tier_discount and Decimal(str(order.tier_discount)) > 0

    totals = _confirmation_html(app, customer, order.id)

    assert TIER_LABEL_START in totals
    assert "−{:,.0f}".format(order.tier_discount) in totals


def test_order_confirmation_shows_no_discount_rows_when_none_apply(app, db, customer, sample_product, user_address):
    admin = make_staff(db, UserRole.ADMIN, "+998901119941")
    order = _admin_order(app, admin, customer, user_address, sample_product, "cash")

    totals = _confirmation_html(app, customer, order.id)

    assert "Tier discount" not in totals and "Subscription discount" not in totals and "Reward discount" not in totals
    assert "−0" not in totals


def test_order_confirmation_shows_subscription_and_reward_rows_from_the_order(app, db, customer, sample_product, user_address):
    admin = make_staff(db, UserRole.ADMIN, "+998901119942")
    order = _admin_order(app, admin, customer, user_address, sample_product, "cash")
    order.discount_amount = Decimal("1234.00")
    order.loyalty_discount = Decimal("5678.00")
    db.session.commit()

    totals = _confirmation_html(app, customer, order.id)

    assert "Subscription discount" in totals and "−1,234" in totals
    assert "Reward discount" in totals and "−5,678" in totals


def test_web_order_renderers_use_only_server_discount_fields():
    """The order details are the shared modal's (orders.js opens it, spec 2026-10-26 §12), and it draws each
    discount row from the label the server publishes for that order, only when there is one."""
    modal = (ROOT / "business_app/static/js/pages/order-detail-modal.js").read_text()
    for field in ("labels.tier_discount", "labels.subscription_discount", "labels.reward_discount"):
        assert field in modal, field
    for path in ("business_app/static/js/pages/orders.js", "business_app/static/js/pages/order-detail-modal.js"):
        src = (ROOT / path).read_text()
        for forbidden in ("order_source", "is_subscription_order", "payment_method === 'cash'"):
            assert forbidden not in src, (path, forbidden)
    assert "function displayOrderDetail" not in (ROOT / "business_app/static/js/pages/orders.js").read_text()


def test_the_order_modal_labels_carry_a_discount_row_only_when_there_is_one(app, db, customer, sample_product, user_address):
    """`GET /orders/<id>` publishes the modal's labels: a subscription or reward row only for a non-zero amount."""
    admin = make_staff(db, UserRole.ADMIN, "+998901119943")
    order = _admin_order(app, admin, customer, user_address, sample_product, "cash")
    plain = app.test_client().get(f"/api/v1/orders/{order.id}", headers=customer_headers(app, customer)).get_json()
    assert (plain["data"]["labels"]["subscription_discount"], plain["data"]["labels"]["reward_discount"]) == (None, None)

    order.discount_amount = Decimal("1234.00")
    db.session.commit()
    labels = app.test_client().get(f"/api/v1/orders/{order.id}", headers=customer_headers(app, customer)).get_json()
    assert labels["data"]["labels"]["subscription_discount"] is not None
    assert labels["data"]["labels"]["reward_discount"] is None


def test_order_page_i18n_carries_the_discount_labels():
    for path in ("business_app/templates/frontend/partials/order_detail_modal.html",):
        src = (ROOT / path).read_text()
        assert "'tier_discount': 'Tier discount (bot & website perk)' | t" in src, path
        assert "'subscription_discount': 'Subscription discount' | t" in src, path
        assert "'reward_discount': 'Reward discount' | t" in src, path


def test_the_labels_are_seeded_trilingual():
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

    for key in ("Tier discount (bot & website perk)", "Subscription discount", "Reward discount"):
        assert set(BACKEND_TRANSLATIONS[key]) >= {"en", "uz", "ru"}, key
