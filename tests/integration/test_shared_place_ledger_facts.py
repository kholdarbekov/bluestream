"""Delivery facts the shared-place hook reads, and keyed admin adjustments."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from business_app.models.bottle import BottleLedger
from business_app.models.order import Order
from business_app.models.customer_link import AddressGroup
from business_app.models.user import UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.order_service import OrderService
from business_app.utils.exceptions import ConflictError
from shared.enums import BottleLedgerEventType, OrderStatus, PaymentMethod

pytestmark = pytest.mark.integration


def _address(db, user, title="Home", group=None, lat=41.31):
    address = UserAddress(
        user_id=user.id,
        title=title,
        full_address=f"{title} Street 1",
        street_address=f"{title} Street 1",
        city="Tashkent",
        latitude=lat,
        longitude=69.28,
        address_group_id=group.id if group else None,
    )
    db.session.add(address)
    db.session.commit()
    return address


def _order(db, user, address, status=OrderStatus.DELIVERED, number="ORD-SPL-1"):
    order = Order(
        order_number=number,
        user_id=user.id,
        status=status,
        subtotal=Decimal("10000"),
        delivery_fee=Decimal("0"),
        total_amount=Decimal("10000"),
        delivery_address_id=address.id,
        payment_method=PaymentMethod.CASH,
        order_source="web",
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.commit()
    return order


def test_has_delivered_order_excludes_the_given_order(db, sample_user):
    address = _address(db, sample_user)
    order = _order(db, sample_user, address)

    assert OrderService.customer_has_delivered_order(sample_user.id) is True
    assert OrderService.customer_has_delivered_order(sample_user.id, exclude_order_id=order.id) is False


def test_has_delivered_order_exclusion_keeps_other_delivered_orders(db, sample_user):
    address = _address(db, sample_user)
    first = _order(db, sample_user, address, number="ORD-SPL-A")
    _order(db, sample_user, address, number="ORD-SPL-B")

    assert OrderService.customer_has_delivered_order(sample_user.id, exclude_order_id=first.id) is True


def _seed_and_deliver(db, user, address, actor, *, start="3", delivered="2"):
    service = BottleTrackingService()
    service.set_initial_balance(user.id, address.id, Decimal(start), actor.id, "seed")
    order = _order(db, user, address, number="ORD-SPL-D")
    service.record_bottles_delivered(order.id, user.id, address.id, Decimal(delivered), actor.id)
    return order


def test_summary_publishes_balance_before_and_group(db, sample_user, admin_user):
    address = _address(db, sample_user)
    order = _seed_and_deliver(db, sample_user, address, admin_user)

    summary = BottleTrackingService.get_order_bottle_summary(order)

    assert summary["place_balance_before_delivery"] == Decimal("3")
    assert summary["delivery_place_group_id"] is None
    assert summary["balance"] == Decimal("5")


def test_summary_publishes_the_group_id_at_a_grouped_address(db, sample_user, admin_user):
    group = AddressGroup(label="Office")
    db.session.add(group)
    db.session.commit()
    address = _address(db, sample_user, title="Work", group=group)
    order = _seed_and_deliver(db, sample_user, address, admin_user)

    summary = BottleTrackingService.get_order_bottle_summary(order)

    assert summary["delivery_place_group_id"] == group.id
    assert summary["place_balance_before_delivery"] == Decimal("3")


def test_summary_before_is_none_without_delivery_row(db, sample_user):
    address = _address(db, sample_user)
    order = _order(db, sample_user, address)

    summary = BottleTrackingService.get_order_bottle_summary(order)

    assert summary["place_balance_before_delivery"] is None
    assert summary["delivery_place_group_id"] is None


def _adjustments(address_id):
    return BottleLedger.query.filter_by(
        address_id=address_id, event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT
    ).all()


def test_admin_adjust_with_key_is_replay_safe(db, sample_user, admin_user):
    address = _address(db, sample_user)
    service = BottleTrackingService()

    first = service.admin_adjust_balance(
        sample_user.id, address.id, Decimal("2"), admin_user.id, "fix", idempotency_key="shared_place:1:fix"
    )
    again = service.admin_adjust_balance(
        sample_user.id, address.id, Decimal("2"), admin_user.id, "fix", idempotency_key="shared_place:1:fix"
    )

    assert again.id == first.id
    assert len(_adjustments(address.id)) == 1
    assert BottleTrackingService.get_place_balance(address.id) == Decimal("2")


def test_admin_adjust_key_with_different_quantity_conflicts(db, sample_user, admin_user):
    address = _address(db, sample_user)
    service = BottleTrackingService()
    service.admin_adjust_balance(
        sample_user.id, address.id, Decimal("2"), admin_user.id, "fix", idempotency_key="shared_place:2:fix"
    )

    with pytest.raises(ConflictError) as exc:
        service.admin_adjust_balance(
            sample_user.id, address.id, Decimal("3"), admin_user.id, "fix", idempotency_key="shared_place:2:fix"
        )

    assert exc.value.error_code == "BOTTLE_ADJUSTMENT_KEY_CONFLICT"
    assert len(_adjustments(address.id)) == 1


def test_admin_adjust_key_with_different_address_conflicts(db, sample_user, admin_user):
    first_address = _address(db, sample_user, title="Home")
    other_address = _address(db, sample_user, title="Shop", lat=41.32)
    service = BottleTrackingService()
    service.admin_adjust_balance(
        sample_user.id, first_address.id, Decimal("2"), admin_user.id, "fix", idempotency_key="shared_place:3:fix"
    )

    with pytest.raises(ConflictError) as exc:
        service.admin_adjust_balance(
            sample_user.id, other_address.id, Decimal("2"), admin_user.id, "fix", idempotency_key="shared_place:3:fix"
        )

    assert exc.value.error_code == "BOTTLE_ADJUSTMENT_KEY_CONFLICT"
    assert len(_adjustments(first_address.id)) == 1
    assert len(_adjustments(other_address.id)) == 0
