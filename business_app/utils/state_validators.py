"""ARCH-006: state-machine validators.

Centralised guards that enforce required FKs at terminal-state transitions.
Service layers call these before mutating model rows; the matching DB-level
CHECK constraints (see migration ``arch006_state_invariant_checks``) are the
defence-in-depth backstop.

Reference: docs/audit/01-architecture-backend.md#arch-006
"""

from __future__ import annotations

from typing import FrozenSet, Optional

from business_app.utils.constants import (
    DeliveryStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)
from business_app.utils.exceptions import InvalidStateTransition


STAFF_ORDER_SOURCES: FrozenSet[str] = frozenset({"phone", "admin"})


# Order: from CONFIRMED onward the customer-facing flow needs an address.
# CANCELLED is reachable from PENDING with no address; that is intentional.
ORDER_REQUIRES_ADDRESS_STATES: FrozenSet[OrderStatus] = frozenset(
    {
        OrderStatus.CONFIRMED,
        OrderStatus.PREPARING,
        OrderStatus.OUT_FOR_DELIVERY,
        OrderStatus.DELIVERED,
        OrderStatus.RETURNED,
    }
)


# Delivery: must have an assigned person from ASSIGNED onward.
DELIVERY_REQUIRES_PERSON_STATES: FrozenSet[DeliveryStatus] = frozenset(
    {
        DeliveryStatus.ASSIGNED,
        DeliveryStatus.PICKED_UP,
        DeliveryStatus.IN_TRANSIT,
        DeliveryStatus.ARRIVED,
        DeliveryStatus.DELIVERED,
    }
)


def _coerce_order_status(value) -> Optional[OrderStatus]:
    if value is None or isinstance(value, OrderStatus):
        return value
    try:
        return OrderStatus(value)
    except ValueError:
        return None


def _coerce_delivery_status(value) -> Optional[DeliveryStatus]:
    if value is None or isinstance(value, DeliveryStatus):
        return value
    try:
        return DeliveryStatus(value)
    except ValueError:
        return None


def _coerce_payment_status(value) -> Optional[PaymentStatus]:
    if value is None or isinstance(value, PaymentStatus):
        return value
    try:
        return PaymentStatus(value)
    except ValueError:
        return None


def _coerce_payment_method(value) -> Optional[PaymentMethod]:
    if value is None or isinstance(value, PaymentMethod):
        return value
    try:
        return PaymentMethod(value)
    except ValueError:
        return None


def assert_order_address_for_status(
    order,
    target_status: OrderStatus,
    *,
    delivery_address_id: Optional[int] = None,
) -> None:
    """Reject transitioning ``order`` into a delivery-bearing state without an address.

    ``delivery_address_id`` overrides ``order.delivery_address_id`` when the
    caller is about to assign one in the same unit of work.
    """
    target = _coerce_order_status(target_status)
    if target not in ORDER_REQUIRES_ADDRESS_STATES:
        return

    address_id = delivery_address_id if delivery_address_id is not None else getattr(order, "delivery_address_id", None)
    if address_id is None:
        current = _coerce_order_status(getattr(order, "status", None))
        raise InvalidStateTransition(
            f"Order cannot transition to {target.value} without a delivery address",
            entity="order",
            entity_id=getattr(order, "id", None),
            from_state=current.value if current else None,
            to_state=target.value,
            missing_field="delivery_address_id",
        )


def assert_order_creator_for_source(
    *,
    order_source: Optional[str],
    created_by_staff_id: Optional[int],
    order_id: Optional[int] = None,
) -> None:
    """Reject staff-channel orders without a creating-staff user id."""
    if order_source in STAFF_ORDER_SOURCES and not created_by_staff_id:
        raise InvalidStateTransition(
            f"Orders from order_source='{order_source}' require created_by_staff_id",
            entity="order",
            entity_id=order_id,
            to_state=order_source,
            missing_field="created_by_staff_id",
        )


def assert_delivery_person_for_status(
    delivery,
    target_status: DeliveryStatus,
    *,
    delivery_person_id: Optional[int] = None,
) -> None:
    """Reject moving ``delivery`` into ASSIGNED+ without a delivery person."""
    target = _coerce_delivery_status(target_status)
    if target not in DELIVERY_REQUIRES_PERSON_STATES:
        return

    person_id = delivery_person_id if delivery_person_id is not None else getattr(delivery, "delivery_person_id", None)
    if person_id is None:
        current = _coerce_delivery_status(getattr(delivery, "status", None))
        raise InvalidStateTransition(
            f"Delivery cannot transition to {target.value} without a delivery person",
            entity="delivery",
            entity_id=getattr(delivery, "id", None),
            from_state=current.value if current else None,
            to_state=target.value,
            missing_field="delivery_person_id",
        )


def assert_cash_payment_collector(
    payment,
    target_status: PaymentStatus,
    *,
    collected_by: Optional[int] = None,
) -> None:
    """Reject completing a cash payment without recording who collected it."""
    target = _coerce_payment_status(target_status)
    if target != PaymentStatus.COMPLETED:
        return

    method = _coerce_payment_method(getattr(payment, "payment_method", None))
    if method != PaymentMethod.CASH:
        return

    collector_id = collected_by if collected_by is not None else getattr(payment, "collected_by", None)
    if collector_id is None:
        current = _coerce_payment_status(getattr(payment, "status", None))
        raise InvalidStateTransition(
            "Cash payment cannot be marked COMPLETED without a collector",
            entity="payment",
            entity_id=getattr(payment, "id", None),
            from_state=current.value if current else None,
            to_state=target.value,
            missing_field="collected_by",
        )
