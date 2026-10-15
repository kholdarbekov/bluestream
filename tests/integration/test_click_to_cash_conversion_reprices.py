"""An unpaid Click order settled in cash at the door pays the contract CASH price (spec 2026-10-09 §5.2 #5, §6.3)."""

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
    admin_headers,
    customer_headers,
    make_contract,
    make_staff,
    make_workplace,
)
from tests.integration.tier_discount_factory import post_order

pytestmark = pytest.mark.integration


@pytest.fixture
def world(db, sample_product):
    user, address = make_workplace(db)
    make_contract(db, user, sample_product)
    return user, address


@pytest.fixture
def driver(db):
    user = make_staff(db, UserRole.DELIVERY_DRIVER, "+998901119913")
    db.session.add(
        DeliveryPerson(user_id=user.id, full_name="Dual Driver", phone=user.phone, is_active=True, is_available=True)
    )
    db.session.commit()
    return user


def _reload(order_id):
    _db.session.expire_all()
    return Order.query.get(order_id)


def _unpaid_click_order_at_the_door(app, user, address, product, driver):
    resp = post_order(
        app, customer_headers(app, user), product_id=product.id, address_id=address.id,
        payment_method="click", quantity=QUANTITY,
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    order = _reload(resp.get_json()["data"]["order"]["id"])
    assert order.payment.status == PaymentStatus.PENDING
    order.status = OrderStatus.OUT_FOR_DELIVERY
    delivery = Delivery(
        order_id=order.id, delivery_person_id=driver.id, status=DeliveryStatus.ARRIVED,
        scheduled_date=datetime.now(timezone.utc), scheduled_time_slot="09:00-12:00",
    )
    _db.session.add(delivery)
    _db.session.commit()
    return order, delivery


def _fee(order):
    return Decimal(str(order.delivery_fee or 0))


def test_the_driver_is_told_the_cash_price_for_an_unpaid_click_order(app, db, world, driver, sample_product):
    user, address = world
    order, _delivery = _unpaid_click_order_at_the_door(app, user, address, sample_product, driver)

    resp = app.test_client().get("/api/v1/staff/delivery/active", headers=customer_headers(app, driver))

    assert resp.status_code == 200, resp.get_data(as_text=True)
    item = next(row for row in resp.get_json()["data"]["items"] if row["order_id"] == order.id)
    assert item["expected_cash_to_collect"] == float(CATALOGUE * QUANTITY + _fee(order))  # the Click receivable
    assert item["cash_settlement_amount"] == float(CASH * QUANTITY + _fee(order))


def test_collecting_the_quoted_cash_settles_the_order_with_no_surplus(app, db, world, driver, sample_product):
    user, address = world
    order, delivery = _unpaid_click_order_at_the_door(app, user, address, sample_product, driver)
    cash_total = CASH * QUANTITY + _fee(order)

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
    assert order.payment_method == PaymentMethod.CASH
    assert Decimal(str(order.order_items[0].unit_price)) == CASH
    assert Decimal(str(order.total_amount)) == cash_total
    assert Decimal(str(order.payment.amount_collected)) == cash_total
    assert CashCollectionService().get_customer_prepaid_balance(user.id) == Decimal("0.00")


def test_the_card_transfer_preview_quotes_the_cash_total_for_a_click_order(
    app, db, admin_user, world, driver, sample_product
):
    user, address = world
    order, _delivery = _unpaid_click_order_at_the_door(app, user, address, sample_product, driver)
    cash_total = CASH * QUANTITY + _fee(order)

    plan = CashCollectionService().preview_personal_card_transfer(order_id=order.id, amount=cash_total)

    summary = plan.to_summary()
    assert "order_will_convert_to_cash" in summary["warnings"]
    assert Decimal(str(summary["applied_to_order"])) == cash_total
    assert Decimal(str(summary["order_outstanding_after"])) == Decimal("0.00")
    assert Decimal(str(summary["remaining_as_credit"])) == Decimal("0.00")


def test_the_admin_preview_endpoint_quotes_the_cash_total_for_a_click_order(
    app, db, admin_user, world, driver, sample_product
):
    """The route the admin Orders modal calls, with the typed amount it sends (a string)."""
    user, address = world
    order, _delivery = _unpaid_click_order_at_the_door(app, user, address, sample_product, driver)
    cash_total = CASH * QUANTITY + _fee(order)

    resp = app.test_client().post(
        f"/api/v1/admin/orders/{order.id}/personal-card-transfer/preview",
        json={"amount": str(cash_total)},
        headers=admin_headers(app, admin_user),
    )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    summary = resp.get_json()["data"]
    assert "order_will_convert_to_cash" in summary["warnings"]
    assert Decimal(str(summary["applied_to_order"])) == cash_total
    assert Decimal(str(summary["order_outstanding_after"])) == Decimal("0.00")
    assert Decimal(str(summary["remaining_as_credit"])) == Decimal("0.00")


def test_recording_the_previewed_card_transfer_settles_the_order_at_the_cash_price(
    app, db, admin_user, world, driver, sample_product
):
    """Preview and apply agree: the body the admin Orders modal posts converts and settles the cash total."""
    user, address = world
    order, _delivery = _unpaid_click_order_at_the_door(app, user, address, sample_product, driver)
    cash_total = CASH * QUANTITY + _fee(order)

    resp = app.test_client().post(
        "/api/v1/admin/staff/cash-reconciliation/collections",
        json={
            "customer_id": user.id,
            "order_id": order.id,
            "amount": str(cash_total),
            "notes": "Paid to the owner's card",  # the modal's Notes field is required
            "source": "personal_card_transfer",
            "proof_data": {"channel": "admin_ui_orders"},
        },
        headers=admin_headers(app, admin_user),
    )

    assert resp.status_code == 201, resp.get_data(as_text=True)
    order = _reload(order.id)
    assert order.payment_method == PaymentMethod.CASH
    assert Decimal(str(order.order_items[0].unit_price)) == CASH
    assert Decimal(str(order.total_amount)) == cash_total
    assert Decimal(str(order.payment.amount)) == cash_total
    assert Decimal(str(order.payment.amount_collected)) == cash_total
    assert Decimal(str(order.payment.outstanding_amount)) == Decimal("0.00")
    assert CashCollectionService().get_customer_prepaid_balance(user.id) == Decimal("0.00")


def _admin_order_detail(app, admin, order_id):
    resp = app.test_client().get(f"/api/v1/admin/orders/{order_id}", headers=admin_headers(app, admin))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["data"]["order"]


def test_the_admin_order_detail_publishes_the_cash_settlement_figure_for_an_unpaid_click_order(
    app, db, admin_user, world, driver, sample_product
):
    """The card-transfer screen reads the cash figure from the server (spec 2026-10-09 §6.3), never re-derives it."""
    user, address = world
    order, _delivery = _unpaid_click_order_at_the_door(app, user, address, sample_product, driver)

    detail = _admin_order_detail(app, admin_user, order.id)

    assert detail["net_outstanding_amount"] == float(CATALOGUE * QUANTITY + _fee(order))  # the Click receivable
    assert detail["cash_settlement_amount"] == float(CASH * QUANTITY + _fee(order))


def test_the_admin_order_detail_publishes_no_cash_settlement_figure_for_a_cash_order(
    app, db, admin_user, world, sample_product
):
    user, address = world
    resp = post_order(
        app, customer_headers(app, user), product_id=sample_product.id, address_id=address.id,
        payment_method="cash", quantity=QUANTITY,
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)

    detail = _admin_order_detail(app, admin_user, resp.get_json()["data"]["order"]["id"])

    assert "cash_settlement_amount" in detail
    assert detail["cash_settlement_amount"] is None


def test_ambiguous_contract_coverage_still_lets_the_driver_list_render(app, db, driver, sample_product):
    """Two active contracts covering one product (legacy data; contract writes refuse the overlap).

    The cash quote cannot be resolved, so that order publishes no cash figure and shows its
    `expected_cash_to_collect`; the driver's list must not fail as a whole.
    """
    user, address = make_workplace(db)
    order, _delivery = _unpaid_click_order_at_the_door(app, user, address, sample_product, driver)
    make_contract(db, user, sample_product)
    make_contract(db, user, sample_product)

    resp = app.test_client().get("/api/v1/staff/delivery/active", headers=customer_headers(app, driver))

    assert resp.status_code == 200, resp.get_data(as_text=True)
    item = next(row for row in resp.get_json()["data"]["items"] if row["order_id"] == order.id)
    assert item["cash_settlement_amount"] is None
    assert item["expected_cash_to_collect"] == float(CATALOGUE * QUANTITY + _fee(order))
