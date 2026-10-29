"""Changing an order's payment method re-prices it (spec 2026-10-09 §5, owner ruling D6)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app import db as _db
from business_app.models.order import Order
from business_app.models.payment import CashCollectionAllocation, CashCollectionEvent
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.corporate_contract_service import CorporateContractService
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import CashCollectionSource, OrderStatus, PaymentMethod, PaymentStatus
from tests.integration.contract_pricing_factory import (
    BANK,
    CASH,
    CATALOGUE,
    QUANTITY,
    admin_headers,
    customer_headers,
    make_contract,
    make_workplace,
)
from tests.integration.tier_discount_factory import post_order

pytestmark = pytest.mark.integration

CLICK_LINK = "business_app.services.payment_service.PaymentService.create_payment_link"


@pytest.fixture
def world(db, sample_product):
    user, address = make_workplace(db)
    _contract, row = make_contract(db, user, sample_product)
    return user, address, row


def _reload(order_id):
    _db.session.expire_all()
    return Order.query.get(order_id)


def _place(app, user, address, product, method):
    resp = post_order(
        app, customer_headers(app, user), product_id=product.id, address_id=address.id,
        payment_method=method, quantity=QUANTITY,
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return _reload(resp.get_json()["data"]["order"]["id"])


def _fee(order):
    return Decimal(str(order.delivery_fee or 0))


def _preview(app, admin, order_id, new_method):
    resp = app.test_client().post(
        f"/api/v1/admin/orders/{order_id}/payment-method/preview",
        json={"new_method": new_method},
        headers=admin_headers(app, admin),
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["data"]


def _change(app, admin, order_id, new_method):
    return app.test_client().post(
        f"/api/v1/admin/orders/{order_id}/payment-method",
        json={"new_method": new_method, "reason": "reclassified after a call with the customer"},
        headers=admin_headers(app, admin),
    )


def _deliver_with_collected_cash(order, driver):
    """Delivered COD order whose whole total was collected at the door (the order-627 shape)."""
    # Read the id BEFORE mutating: `driver` is expired by `_reload`'s expire_all, so a later
    # `driver.id` refresh autoflushes a COMPLETED cash payment with no collector (CHECK fails).
    driver_id = driver.id
    order.status = OrderStatus.DELIVERED
    payment = order.payment
    payment.amount_collected = payment.amount
    payment.outstanding_amount = Decimal("0.00")
    payment.status = PaymentStatus.COMPLETED
    payment.collected_by = driver_id
    event = CashCollectionEvent(
        customer_id=order.user_id, collector_user_id=driver_id, recorded_by_user_id=driver_id,
        order_id=order.id, amount=payment.amount, currency="UZS",
        source=CashCollectionSource.DELIVERY_COMPLETION, unapplied_amount=Decimal("0.00"),
        occurred_at=datetime.now(timezone.utc),
    )
    _db.session.add(event)
    _db.session.flush()
    _db.session.add(
        CashCollectionAllocation(
            cash_collection_event_id=event.id, payment_id=payment.id, order_id=order.id,
            allocated_amount=payment.amount, allocation_order=1, allocation_mode="auto",
            allocation_metadata={"affects_payment_projection": True},
        )
    )
    order.is_paid = True
    _db.session.commit()
    return Decimal(str(payment.amount))


def test_the_admin_preview_names_the_old_and_new_total(app, db, admin_user, world, sample_product):
    user, address, _row = world
    order = _place(app, user, address, sample_product, "cash")

    preview = _preview(app, admin_user, order.id, "business_account")

    assert preview["current_total"] == float(CASH * QUANTITY + _fee(order))
    assert preview["new_total"] == float(BANK * QUANTITY + _fee(order))


def test_cash_to_business_account_charges_the_bank_transfer_price(app, db, admin_user, world, sample_product):
    user, address, _row = world
    order = _place(app, user, address, sample_product, "cash")
    preview = _preview(app, admin_user, order.id, "business_account")

    resp = _change(app, admin_user, order.id, "business_account")

    assert resp.status_code == 200, resp.get_data(as_text=True)
    order = _reload(order.id)
    assert Decimal(str(order.order_items[0].unit_price)) == BANK
    assert Decimal(str(order.total_amount)) == Decimal(str(preview["new_total"]))
    assert Decimal(str(order.payment.amount)) == Decimal(str(order.total_amount))


def test_cash_to_business_account_on_a_delivered_order_credits_the_cash_collected(
    app, db, admin_user, delivery_driver, world, sample_product
):
    user, address, _row = world
    order = _place(app, user, address, sample_product, "cash")
    collected = _deliver_with_collected_cash(order, delivery_driver)
    assert collected == CASH * QUANTITY + _fee(order)

    resp = _change(app, admin_user, order.id, "business_account")

    assert resp.status_code == 200, resp.get_data(as_text=True)
    order = _reload(order.id)
    assert Decimal(str(order.total_amount)) == BANK * QUANTITY + _fee(order)
    assert CashCollectionService().get_customer_prepaid_balance(user.id) == collected


def test_business_account_to_cash_on_a_delivered_order_owes_the_cash_price(
    app, db, admin_user, world, sample_product
):
    user, address, _row = world
    order = _place(app, user, address, sample_product, "business_account")
    CorporateContractService().consume_for_order(order.id)
    order.status = OrderStatus.DELIVERED
    _db.session.commit()

    resp = _change(app, admin_user, order.id, "cash")

    assert resp.status_code == 200, resp.get_data(as_text=True)
    order = _reload(order.id)
    owed = CASH * QUANTITY + _fee(order)
    assert order.payment.payment_method == PaymentMethod.CASH
    assert Decimal(str(order.total_amount)) == owed
    assert Decimal(str(order.payment.amount)) == owed
    assert Decimal(str(order.payment.outstanding_amount)) == owed


def test_business_account_to_click_issues_a_link_for_the_catalogue_total(
    app, db, admin_user, world, sample_product
):
    user, address, _row = world
    order = _place(app, user, address, sample_product, "business_account")

    with patch(CLICK_LINK, return_value={"payment_url": "https://click.test/pay"}) as link:
        resp = _change(app, admin_user, order.id, "click")

    assert resp.status_code == 200, resp.get_data(as_text=True)
    order = _reload(order.id)
    catalogue_total = CATALOGUE * QUANTITY + _fee(order)
    assert Decimal(str(order.total_amount)) == catalogue_total
    assert Decimal(str(order.payment.amount)) == catalogue_total
    link.assert_called_once_with(order.payment.id)


def test_business_account_to_click_starts_the_click_window(app, db, admin_user, world, sample_product):
    """Spec 2026-10-27 §4.4, §17 Q3: an order staff move onto Click becomes payable on Click now, so its 24 hours
    count from the move, not from when it was placed."""
    user, address, _row = world
    order = _place(app, user, address, sample_product, "business_account")
    order.created_at = datetime.now(timezone.utc) - timedelta(days=3)
    _db.session.commit()

    before = datetime.now(timezone.utc)
    with patch(CLICK_LINK, return_value={"payment_url": "https://click.test/pay"}):
        resp = _change(app, admin_user, order.id, "click")
    after = datetime.now(timezone.utc)

    assert resp.status_code == 200, resp.get_data(as_text=True)
    order = _reload(order.id)
    assert before <= ensure_utc(order.online_payable_since) <= after


def test_ambiguous_contract_coverage_blocks_the_edit_instead_of_breaking_the_order_page(
    app, db, admin_user, sample_product
):
    """Two active contracts covering one product (legacy data; contract writes refuse the overlap).

    The admin order page must still load, and the preview must say why the edit is refused, because
    the re-price inside apply would raise on the same ambiguity.
    """
    user, address = make_workplace(db)
    order = _place(app, user, address, sample_product, "cash")
    make_contract(db, user, sample_product)
    make_contract(db, user, sample_product)

    detail = app.test_client().get(f"/api/v1/admin/orders/{order.id}", headers=admin_headers(app, admin_user))
    preview = app.test_client().post(
        f"/api/v1/admin/orders/{order.id}/payment-method/preview",
        json={"new_method": "business_account"},
        headers=admin_headers(app, admin_user),
    )

    assert detail.status_code == 200, detail.get_data(as_text=True)
    assert "business_account" not in detail.get_json()["data"]["order"]["allowed_target_methods"]
    assert preview.status_code == 200, preview.get_data(as_text=True)
    plan = preview.get_json()["data"]
    assert any(reason.startswith("ambiguous_contract_pricing") for reason in plan["blocking_reasons"])
    assert plan["new_total"] is None
