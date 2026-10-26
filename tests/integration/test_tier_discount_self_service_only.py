"""The loyalty tier discount is a perk of the customer's own bot or website order (spec 2026-10-23).

`LoyaltyService.quote_tier_discount` decides it, and its first gate is the channel: an order the
customer places in the Telegram bot (`telegram`) or on the website (`web`) may carry it. An order
staff enter for them (admin panel, operator phone order, sales agent) never does, and neither does
any subscription order. Driven over the routes the clients call, with the bodies they send,
including every path that later moves an order onto cash, because each of those re-prices the tier.

Every rate is seeded here; no assertion reads a DB-resident number.
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from flask_jwt_extended import create_access_token

from business_app import db as _db
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.services.order_service import OrderService
from business_app.services.subscription_service import SubscriptionService
from business_app.utils.constants import ORDER_SOURCE_SALES_AGENT
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod, PaymentStatus, UserRole
from tests.integration import test_subscription_min_order_quantity as subscription_bodies
from tests.integration.contract_pricing_factory import (
    QUANTITY,
    customer_headers,
    make_contract,
    make_staff,
    make_workplace,
)
from tests.integration.test_contract_price_follows_method import _operator_estimate, _operator_order
from tests.integration.test_payment_method_change_reprices import CLICK_LINK, _change, _preview
from tests.integration.test_subscription_min_order_quantity import _no_external_io  # noqa: F401  (autouse)
from tests.integration.tier_discount_factory import post_order, seed_program, seed_tier, verify_phone

pytestmark = pytest.mark.integration

STAFF_ROLE_FOR_SOURCE = {
    "admin": UserRole.ADMIN,
    "phone": UserRole.OPERATOR,
    ORDER_SOURCE_SALES_AGENT: UserRole.SALES_AGENT,
}


def D(value) -> Decimal:
    return Decimal(str(value or 0))


@pytest.fixture
def tier(db):
    return seed_tier(db, seed_program(db), name="Base", rate=Decimal("5"))


@pytest.fixture
def customer(db, tier, sample_user):
    """A loyalty-eligible individual holding a tier whose live rate is above zero."""
    verify_phone(db, sample_user)
    return sample_user


@pytest.fixture
def workplace(db, tier, sample_product):
    """A legal entity whose contract makes it loyalty-eligible, so it holds the same tier.

    The admin payment-method edit reaches cash only from the business account (`ALLOWED_TRANSITIONS`
    has no click -> cash), and only an entity has a business account to move off.
    """
    user, address = make_workplace(db)
    contract, _row = make_contract(db, user, sample_product)
    contract.is_loyalty_points_eligible = True
    db.session.commit()
    return user, address


def _reload(order_id):
    _db.session.expire_all()
    return Order.query.get(order_id)


def _undiscounted(order) -> Decimal:
    return D(order.subtotal) + D(order.delivery_fee)


def _role_headers(app, staff):
    """`staff_or_higher_required` reads the ROLE CLAIM and cross-checks the DB row."""
    with app.app_context():
        token = create_access_token(identity=str(staff.id), additional_claims={"role": staff.role.value})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _admin_order(app, staff, customer, address, product, payment_method):
    """POST /api/v1/admin/orders with the body admin_ui/src/pages/Orders.js sends (no schedule picked)."""
    with patch(CLICK_LINK, return_value={"payment_url": "https://click.test/pay"}):
        resp = app.test_client().post(
            "/api/v1/admin/orders",
            json={
                "user_id": customer.id,
                "delivery_address_id": address.id,
                "payment_method": payment_method,
                "delivery_notes": "",
                "consume_marking_codes": False,
                "items": [{"product_id": product.id, "quantity": QUANTITY}],
            },
            headers=_role_headers(app, staff),
        )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return _reload(resp.get_json()["data"]["order"]["id"])


def _staff_click_order(app, db, source, customer, address, product):
    """An unpaid Click order staff entered for `customer`, through that source's own entry point.

    The admin panel and the operator phone order take Click at their own endpoints. A sales agent may
    take only cash or the business account (`AGENT_PAYMENT_METHODS`), so that row is built by
    `create_order`, the call `VisitService.place_order` makes, with the source and creator it stamps.
    """
    staff = make_staff(db, STAFF_ROLE_FOR_SOURCE[source], "+998901119931")
    if source == "admin":
        order = _admin_order(app, staff, customer, address, product, "click")
    elif source == "phone":
        order = _operator_order(app, staff, customer.id, product, address, "click")
    else:
        created = OrderService().create_order(
            customer.id,
            {
                "items": [{"product_id": product.id, "quantity": QUANTITY}],
                "delivery_address": {
                    "delivery_address_id": address.id,
                    "street": address.street_address,
                    "latitude": address.latitude,
                    "longitude": address.longitude,
                },
                "payment_method": "click",
                "order_source": source,
                "created_by_staff_id": staff.id,
            },
        )
        order = _reload(created.id)
    assert order.order_source == source
    assert order.payment_method == PaymentMethod.CLICK
    assert D(order.tier_discount) == Decimal("0.00")
    return order


def _billed_subscription_order(app, db, creator, customer, address, product, payment_method="cash"):
    """Created through the endpoint the bot (customer) or admin_ui Subscriptions.js (admin) posts to,
    then billed by the one function the daily billing task calls."""
    if creator == "customer":
        resp = app.test_client().post(
            "/api/v1/subscriptions/",
            headers=customer_headers(app, customer),
            json={
                **subscription_bodies._create_payload(product.id, QUANTITY, address.id),
                "payment_method": payment_method,
            },
        )
    else:
        admin = make_staff(db, UserRole.ADMIN, "+998901119932")
        resp = subscription_bodies.TestAdmin()._admin_create(
            app.test_client(), _role_headers(app, admin), customer, address, product.id, QUANTITY
        )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    subscription_id = resp.get_json()["data"]["subscription"]["id"]

    result = SubscriptionService().create_next_order(subscription_id)

    assert result["success"] is True, result
    return _reload(Order.query.filter_by(subscription_id=subscription_id).one().id)


# --------------------------------------------------------------------------- #
# New orders
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("source", ["web", "telegram"])
def test_a_customer_cash_order_from_the_bot_or_website_gets_the_tier(
    app, db, customer, sample_product, user_address, source
):
    resp = post_order(
        app,
        customer_headers(app, customer),
        product_id=sample_product.id,
        address_id=user_address.id,
        payment_method="cash",
        source=source,
    )

    assert resp.status_code == 201, resp.get_data(as_text=True)
    order = _reload(resp.get_json()["data"]["order"]["id"])
    assert order.order_source == source and D(order.tier_discount) > 0


@pytest.mark.parametrize("role", ["admin", "operator"])
def test_an_order_entered_in_the_admin_panel_gets_no_tier(app, db, customer, sample_product, user_address, role):
    staff = make_staff(db, UserRole(role), "+998901119933")

    order = _admin_order(app, staff, customer, user_address, sample_product, "cash")

    assert order.order_source == "admin"
    assert D(order.tier_discount) == Decimal("0.00")
    assert D(order.total_amount) == _undiscounted(order)
    assert D(order.payment.amount) == D(order.total_amount)


def test_the_operator_quote_and_phone_order_carry_no_tier(app, db, customer, sample_product, user_address):
    operator = make_staff(db, UserRole.OPERATOR, "+998901119934")

    estimate = _operator_estimate(app, operator, customer.id, sample_product, "cash")
    order = _operator_order(app, operator, customer.id, sample_product, user_address, "cash")

    assert estimate["tier_discount"] == 0.0 and D(order.tier_discount) == Decimal("0.00")
    assert D(estimate["total_amount"]) == D(order.total_amount)


@pytest.mark.parametrize("creator", ["customer", "admin"])
def test_no_subscription_order_gets_the_tier(app, db, customer, sample_product, user_address, creator):
    order = _billed_subscription_order(app, db, creator, customer, user_address, sample_product)

    assert order.payment_method == PaymentMethod.CASH
    assert order.is_subscription_order is True and D(order.tier_discount) == Decimal("0.00")


def test_the_subscription_preview_quotes_no_tier_for_a_user_who_would_get_one_on_a_web_cash_order(
    app, db, customer, sample_product
):
    """Positive control: this user's tier is real (non-zero on a web cash order), so the preview's 0
    is the subscription gate of `quote_tier_discount` speaking, not a hard-coded constant."""
    from business_app.services.loyalty_service import LoyaltyService

    basis = Decimal(str(sample_product.base_price)) * 2
    with app.app_context():
        web_cash = LoyaltyService().quote_tier_discount(
            customer, basis, PaymentMethod.CASH, order_source="web", is_subscription_order=False
        )
        assert web_cash.amount > 0

        preview = SubscriptionService().calculate_subscription_preview(
            customer.id, "monthly", [{"product_id": sample_product.id, "quantity": 2}]
        )

    # The preview exposes no tier field; the total is where a tier would show.
    assert D(preview["total_amount"]) == basis


# --------------------------------------------------------------------------- #
# Moves onto cash re-price the tier through the same decision
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("source", ["admin", "phone", ORDER_SOURCE_SALES_AGENT])
def test_a_staff_click_order_moved_onto_cash_by_the_customer_stays_without_the_tier(
    app, db, customer, sample_product, user_address, source
):
    order = _staff_click_order(app, db, source, customer, user_address, sample_product)

    resp = app.test_client().post(
        "/api/v1/payments/create",
        json={"order_id": order.id, "payment_method": "cash"},
        headers=customer_headers(app, customer),
    )

    assert resp.status_code in (200, 201), resp.get_data(as_text=True)
    order = _reload(order.id)
    assert order.payment_method == PaymentMethod.CASH
    assert D(order.tier_discount) == Decimal("0.00")
    assert D(order.total_amount) == _undiscounted(order)


def test_admin_payment_method_preview_and_apply_on_a_staff_order_show_no_tier(
    app, db, admin_user, workplace, sample_product
):
    user, address = workplace
    order = _admin_order(app, admin_user, user, address, sample_product, "business_account")

    preview = _preview(app, admin_user, order.id, "cash")
    resp = _change(app, admin_user, order.id, "cash")

    assert resp.status_code == 200, resp.get_data(as_text=True)
    order_after = _reload(order.id)
    assert order_after.payment_method == PaymentMethod.CASH
    assert D(order_after.tier_discount) == Decimal("0.00")
    assert D(preview["new_total"]) == _undiscounted(order_after) == D(order_after.total_amount)


def test_a_website_order_moved_onto_cash_by_an_admin_still_gains_the_tier(
    app, db, admin_user, workplace, sample_product
):
    """Control for the test above: the same admin move on the entity's own website order re-prices it
    onto the tier, so the zero there comes from the channel, not from a re-price that grants nothing."""
    user, address = workplace
    resp = post_order(
        app,
        customer_headers(app, user),
        product_id=sample_product.id,
        address_id=address.id,
        payment_method="business_account",
        source="web",
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    order = _reload(resp.get_json()["data"]["order"]["id"])

    preview = _preview(app, admin_user, order.id, "cash")
    changed = _change(app, admin_user, order.id, "cash")

    assert changed.status_code == 200, changed.get_data(as_text=True)
    order_after = _reload(order.id)
    assert D(order_after.tier_discount) > 0
    assert D(order_after.total_amount) == D(preview["new_total"]) < _undiscounted(order_after)


@pytest.fixture
def driver(db):
    user = make_staff(db, UserRole.DELIVERY_DRIVER, "+998901119935")
    db.session.add(
        DeliveryPerson(user_id=user.id, full_name="Tier Driver", phone=user.phone, is_active=True, is_available=True)
    )
    db.session.commit()
    return user


def test_a_driver_converting_a_staff_click_order_to_cash_charges_no_tier(
    app, db, admin_user, customer, driver, sample_product, user_address
):
    order = _admin_order(app, admin_user, customer, user_address, sample_product, "click")
    assert order.payment.status == PaymentStatus.PENDING
    order.status = OrderStatus.OUT_FOR_DELIVERY
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver.id,
        status=DeliveryStatus.ARRIVED,
        scheduled_date=datetime.now(timezone.utc),
        scheduled_time_slot="09:00-12:00",
    )
    _db.session.add(delivery)
    _db.session.commit()
    order = _reload(order.id)

    active = app.test_client().get("/api/v1/staff/delivery/active", headers=customer_headers(app, driver))
    assert active.status_code == 200, active.get_data(as_text=True)
    listed = next(row for row in active.get_json()["data"]["items"] if row["order_id"] == order.id)
    assert D(listed["cash_settlement_amount"]) == _undiscounted(order)  # the figure the driver collects

    with patch("business_app.tasks.notification_tasks.send_delivery_update_task.delay"), patch(
        "business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"
    ):
        resp = app.test_client().put(
            f"/api/v1/staff/delivery/{delivery.id}/status",
            json={"status": "delivered", "metadata": {"cash_collected": str(listed["cash_settlement_amount"])}},
            headers=customer_headers(app, driver),
        )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    converted = _reload(order.id)
    assert converted.payment_method == PaymentMethod.CASH
    assert D(converted.tier_discount) == Decimal("0.00")
    assert D(converted.total_amount) == D(listed["cash_settlement_amount"])


def test_a_billed_subscription_order_moved_onto_cash_by_an_admin_stays_without_the_tier(
    app, db, admin_user, workplace, sample_product
):
    """A subscription order carries the default `web` source, so the channel alone would let it
    through: only the subscription half of the gate keeps the re-price at zero."""
    user, address = workplace
    order = _billed_subscription_order(
        app, db, "customer", user, address, sample_product, payment_method="business_account"
    )
    assert order.order_source == "web" and order.payment_method == PaymentMethod.BUSINESS_ACCOUNT

    resp = _change(app, admin_user, order.id, "cash")

    assert resp.status_code == 200, resp.get_data(as_text=True)
    order_after = _reload(order.id)
    assert order_after.payment_method == PaymentMethod.CASH
    assert D(order_after.tier_discount) == Decimal("0.00")


def test_a_web_click_order_moved_onto_cash_still_gains_the_tier(app, db, customer, sample_product, user_address):
    """Control for the staff Click orders above: the customer's own website order, moved onto cash by
    the same `/payments/create` call, is re-priced onto the tier."""
    resp = post_order(
        app,
        customer_headers(app, customer),
        product_id=sample_product.id,
        address_id=user_address.id,
        payment_method="click",
        source="web",
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    order = _reload(resp.get_json()["data"]["order"]["id"])
    assert D(order.tier_discount) == Decimal("0.00")

    moved = app.test_client().post(
        "/api/v1/payments/create",
        json={"order_id": order.id, "payment_method": "cash"},
        headers=customer_headers(app, customer),
    )

    assert moved.status_code in (200, 201), moved.get_data(as_text=True)
    order_after = _reload(order.id)
    assert order_after.payment_method == PaymentMethod.CASH
    assert D(order_after.tier_discount) > 0
    assert D(order_after.total_amount) < _undiscounted(order_after)


# --------------------------------------------------------------------------- #
# The customer endpoints accept only the two self-service channels
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("source", ["api", "mobile", "admin", "kiosk"])
def test_the_customer_endpoint_refuses_any_source_but_web_or_telegram(
    app, db, customer, sample_product, user_address, source
):
    before = Order.query.count()

    resp = post_order(
        app,
        customer_headers(app, customer),
        product_id=sample_product.id,
        address_id=user_address.id,
        payment_method="cash",
        source=source,
    )

    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert Order.query.count() == before


def test_the_emergency_endpoint_refuses_an_unknown_source(app, db, customer, sample_product, user_address):
    before = Order.query.count()

    resp = app.test_client().post(
        "/api/v1/orders/emergency",
        json={
            "items": [{"product_id": sample_product.id, "quantity": 2}],
            "delivery_address_id": user_address.id,
            "payment_method": "cash",
            "source": "mobile",
        },
        headers=customer_headers(app, customer),
    )

    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert Order.query.count() == before


def test_the_bot_estimate_equals_the_charge_of_a_telegram_order(app, db, customer, sample_product, user_address):
    headers = customer_headers(app, customer)
    # The body telegram_bot/api_client.py sends: no `source`.
    estimate = app.test_client().post(
        "/api/v1/orders/cart/estimate",
        headers=headers,
        json={
            "items": [{"product_id": sample_product.id, "quantity": 2}],
            "payment_method": "cash",
            "delivery_address_id": user_address.id,
        },
    )
    assert estimate.status_code == 200, estimate.get_data(as_text=True)
    pricing = estimate.get_json()["data"]["pricing"]

    resp = post_order(
        app,
        headers,
        product_id=sample_product.id,
        address_id=user_address.id,
        payment_method="cash",
        source="telegram",
    )

    assert resp.status_code == 201, resp.get_data(as_text=True)
    order = _reload(resp.get_json()["data"]["order"]["id"])
    assert D(order.tier_discount) > 0
    assert D(pricing["tier_discount"]) == D(order.tier_discount)
    assert D(pricing["final_total"]) == D(order.total_amount)
