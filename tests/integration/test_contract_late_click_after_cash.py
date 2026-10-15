"""Case C for a contract customer (spec 2026-10-09 §5.4, owner ruling D7).

The driver converted an unpaid Click order to cash and collected the contract CASH price, so the
order now states the cash total. The customer's original Click payment then lands, for the original
CATALOGUE amount. It must still be accepted: the expected amount is the order quoted back on Click,
not the stored cash total.
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app import db as _db
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.services.cash_collection_service import CashCollectionService
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod, PaymentStatus, UserRole
from tests.integration.contract_pricing_factory import (
    CASH,
    CATALOGUE,
    QUANTITY,
    customer_headers,
    make_contract,
    make_staff,
    make_workplace,
)
from tests.integration.fake_gateways import TEST_CLICK_SHOP_SECRET_KEY, make_click_webhook_form
from tests.integration.tier_discount_factory import post_order

pytestmark = pytest.mark.integration

WEBHOOK_URL = "/api/v1/payments/webhook/click"


@pytest.fixture
def world(db, sample_product):
    user, address = make_workplace(db)
    make_contract(db, user, sample_product)
    return user, address


@pytest.fixture
def driver(db):
    user = make_staff(db, UserRole.DELIVERY_DRIVER, "+998901119914")
    db.session.add(
        DeliveryPerson(user_id=user.id, full_name="Late Click Driver", phone=user.phone, is_active=True, is_available=True)
    )
    db.session.commit()
    return user


def _reload(order_id):
    _db.session.expire_all()
    return Order.query.get(order_id)


def _settled_in_cash_after_click(app, user, address, product, driver):
    """A Click order (catalogue total), prepared by Click, then settled in cash at the door."""
    resp = post_order(
        app, customer_headers(app, user), product_id=product.id, address_id=address.id,
        payment_method="click", quantity=QUANTITY,
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    order = _reload(resp.get_json()["data"]["order"]["id"])
    click_total = Decimal(str(order.total_amount))
    # handle_prepare stamps these on every real order; Case C keys on them.
    order.payment.provider_data = {"click": {"click_trans_id": "940201", "click_paydoc_id": "5231140201"}}
    order.status = OrderStatus.OUT_FOR_DELIVERY
    delivery = Delivery(
        order_id=order.id, delivery_person_id=driver.id, status=DeliveryStatus.ARRIVED,
        scheduled_date=datetime.now(timezone.utc), scheduled_time_slot="09:00-12:00",
    )
    _db.session.add(delivery)
    _db.session.commit()

    cash_total = CASH * QUANTITY + Decimal(str(order.delivery_fee or 0))
    with patch("business_app.tasks.notification_tasks.send_delivery_update_task.delay"), patch(
        "business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"
    ):
        resp = app.test_client().put(
            f"/api/v1/staff/delivery/{delivery.id}/status",
            json={"status": "delivered", "metadata": {"cash_collected": str(cash_total)}},
            headers=customer_headers(app, driver),
        )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    order = _reload(order.id)
    assert (order.payment_method, Decimal(str(order.total_amount))) == (PaymentMethod.CASH, cash_total)
    return order, click_total, cash_total


def _late_complete(client, order, amount):
    form = make_click_webhook_form(
        action="1",
        click_trans_id="940201",
        merchant_trans_id=order.order_number,
        amount=str(int(amount)),
        secret_key=TEST_CLICK_SHOP_SECRET_KEY,
        merchant_prepare_id=str(order.payment.id),
        error=0,
        click_paydoc_id="5231140201",
    )
    return client.post(WEBHOOK_URL, data=form, content_type="application/x-www-form-urlencoded")


def test_the_original_click_amount_is_accepted_and_the_order_returns_to_the_click_price(
    app, matrix_app, matrix_client, db, world, driver, sample_product, no_fiscalization
):
    user, address = world
    order, click_total, cash_total = _settled_in_cash_after_click(app, user, address, sample_product, driver)

    resp = _late_complete(matrix_client, order, click_total)

    assert resp.get_json()["error"] == 0, resp.get_json()
    order = _reload(order.id)
    assert order.payment_method == PaymentMethod.CLICK
    assert order.payment.status == PaymentStatus.COMPLETED
    assert Decimal(str(order.order_items[0].unit_price)) == CATALOGUE
    assert Decimal(str(order.total_amount)) == click_total
    assert Decimal(str(order.payment.amount)) == click_total
    # The driver's banknotes become the customer's prepaid credit, as for every Case C.
    assert CashCollectionService().get_customer_prepaid_balance(user.id) == cash_total


def test_a_late_click_for_the_cash_amount_is_refused(
    app, matrix_app, matrix_client, db, world, driver, sample_product, no_fiscalization
):
    user, address = world
    order, _click_total, cash_total = _settled_in_cash_after_click(app, user, address, sample_product, driver)

    resp = _late_complete(matrix_client, order, cash_total)

    assert resp.get_json()["error"] == -2, resp.get_json()
    assert _reload(order.id).payment_method == PaymentMethod.CASH
