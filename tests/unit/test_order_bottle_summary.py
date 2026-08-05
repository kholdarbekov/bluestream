"""Unit tests for BottleTrackingService.get_order_bottle_summary + format_bottle_quantity."""

from decimal import Decimal

import pytest

from business_app.models.bottle import BottleBalance
from business_app.models.order import Order, OrderItem
from business_app.models.user import UserAddress
from business_app.services.bottle_tracking_service import (
    BottleTrackingService,
    format_bottle_quantity,
)
from shared.enums import OrderStatus


def _make_bottle_order(db, user, product, address, *, order_number, per_unit, quantity):
    """Create a DELIVERED order with one bottle-bearing line item at `address`."""
    product.tracks_returnable_bottles = True
    product.returnable_bottles_per_unit = Decimal(str(per_unit))
    order = Order(
        user_id=user.id,
        order_number=order_number,
        status=OrderStatus.DELIVERED,
        subtotal=Decimal("15000.00"),
        total_amount=Decimal("15000.00"),
        delivery_address_id=address.id,
    )
    db.session.add(order)
    db.session.flush()
    item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        quantity=quantity,
        unit_price=Decimal("15000.00"),
        total_price=Decimal("15000.00") * Decimal(str(quantity)),
    )
    db.session.add(item)
    db.session.flush()
    return order


@pytest.mark.unit
def test_get_order_bottle_summary_reads_delivery_and_return_rows(db, sample_user, sample_product, user_address):
    order = _make_bottle_order(
        db, sample_user, sample_product, user_address,
        order_number="ORD-SUM-001", per_unit="2", quantity=2,
    )
    service = BottleTrackingService()
    service.record_bottles_delivered(order.id, sample_user.id, user_address.id, Decimal("4"))
    service.record_bottles_returned(
        sample_user.id, user_address.id, Decimal("3"), order_id=order.id, delivery_id=None
    )
    db.session.commit()

    summary = BottleTrackingService.get_order_bottle_summary(order)

    assert summary["expected_bottles"] == Decimal("4")
    assert summary["delivery_recorded"] is True
    assert summary["bottles_delivered"] == Decimal("4")
    assert summary["bottles_collected"] == Decimal("3")
    assert summary["balance"] == Decimal("1")  # +4 delivered, -3 returned


@pytest.mark.unit
def test_get_order_bottle_summary_returns_zeros_when_no_ledger_rows(db, sample_user, sample_product, user_address):
    order = _make_bottle_order(
        db, sample_user, sample_product, user_address,
        order_number="ORD-SUM-002", per_unit="2", quantity=2,
    )
    db.session.commit()

    summary = BottleTrackingService.get_order_bottle_summary(order)

    assert summary["expected_bottles"] == Decimal("4")
    assert summary["delivery_recorded"] is False
    assert summary["bottles_delivered"] == Decimal("0")
    assert summary["bottles_collected"] == Decimal("0")
    assert summary["balance"] == Decimal("0")


@pytest.mark.unit
def test_get_order_bottle_summary_preserves_fractional_quantities(db, sample_user, sample_product, user_address):
    order = _make_bottle_order(
        db, sample_user, sample_product, user_address,
        order_number="ORD-SUM-003", per_unit="1.5", quantity=1,
    )
    service = BottleTrackingService()
    service.record_bottles_delivered(order.id, sample_user.id, user_address.id, Decimal("1.5"))
    db.session.commit()

    summary = BottleTrackingService.get_order_bottle_summary(order)

    assert summary["expected_bottles"] == Decimal("1.5")
    assert summary["delivery_recorded"] is True
    assert summary["bottles_delivered"] == Decimal("1.5")
    assert summary["balance"] == Decimal("1.5")


@pytest.mark.unit
def test_get_order_bottle_summary_balance_is_scoped_to_order_address(db, sample_user, sample_product, user_address):
    order = _make_bottle_order(
        db, sample_user, sample_product, user_address,
        order_number="ORD-SUM-004", per_unit="2", quantity=2,
    )
    # A second PLACE (ungrouped, so address-keyed) carrying an unrelated balance
    # that must NOT leak into the order's place.
    other_address = UserAddress(
        user_id=sample_user.id,
        full_address="2 Other St, Tashkent",
        street_address="2 Other St",
        city="Tashkent",
        latitude=41.3111,
        longitude=69.2797,
    )
    db.session.add(other_address)
    db.session.flush()
    db.session.add(BottleBalance(address_id=other_address.id, balance=Decimal("99")))
    service = BottleTrackingService()
    service.record_bottles_delivered(order.id, sample_user.id, user_address.id, Decimal("4"))
    db.session.commit()

    summary = BottleTrackingService.get_order_bottle_summary(order)

    assert summary["balance"] == Decimal("4")  # order's address only, not the 99 on other_address


@pytest.mark.unit
@pytest.mark.parametrize(
    "value,expected",
    [
        (Decimal("4"), "4"),
        (Decimal("4.00"), "4"),
        (Decimal("1.5"), "1.5"),
        (Decimal("1.50"), "1.5"),
        (Decimal("0"), "0"),
        (Decimal("0.00"), "0"),
        (Decimal("100"), "100"),
        (0, "0"),
        (None, "0"),
    ],
)
def test_format_bottle_quantity_normalizes(value, expected):
    assert format_bottle_quantity(value) == expected
