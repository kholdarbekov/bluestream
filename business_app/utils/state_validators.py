"""ARCH-006: state-machine validators.

Centralised guards that enforce required FKs at terminal-state transitions.
Service layers call these before mutating model rows; the matching DB-level
CHECK constraints (see migration ``arch006_state_invariant_checks``) are the
defence-in-depth backstop.

Reference: docs/audit/01-architecture-backend.md#arch-006
"""

from __future__ import annotations

from typing import FrozenSet, Optional

from shared.enums import (
    DeliveryStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)
from business_app.utils.exceptions import InvalidStateTransition


STAFF_ORDER_SOURCES: FrozenSet[str] = frozenset({"phone", "admin", "sales_agent"})


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


# Delivery: while unclaimed in the pool (scheduled/pending) it must NOT retain a
# driver. The inverse of DELIVERY_REQUIRES_PERSON_STATES. A scheduled/pending
# delivery that still has a delivery_person_id is stranded — hidden from both
# the driver's active list (status filter excludes scheduled/pending) and the
# pool (which only lists unassigned rows). See the delivery re-dispatch flow.
DELIVERY_POOL_UNASSIGNED_STATES: FrozenSet[DeliveryStatus] = frozenset(
    {
        DeliveryStatus.SCHEDULED,
        DeliveryStatus.PENDING,
    }
)


# Delivery: every status that must NOT carry a driver -- the claimable pool above
# plus RESCHEDULED, a delivery released once and now held for a later day (R3 of
# docs/superpowers/specs/2026-09-23-admin-order-reschedule-design.md). A held row
# is driverless but NOT claimable, which is why it joins this set and not the
# pool: the pool is what DispatchService and the staff pool offer to drivers;
# this set is what `assert_unassigned_for_pool_status`, the stranded-delivery
# monitor and the CHECK `ck_deliveries_no_driver_for_pool_status` enforce.
DELIVERY_DRIVERLESS_STATES: FrozenSet[DeliveryStatus] = DELIVERY_POOL_UNASSIGNED_STATES | {DeliveryStatus.RESCHEDULED}


# Orders considered "alive" — between placement and the order leaving the
# board (delivered / cancelled / returned). A delivery can sit FAILED for
# months after its order dies through a completely separate flow (the two are
# not kept in lockstep once the delivery reaches a terminal status), so any
# code reviving a delivery from FAILED must re-check the ORDER's current
# status against this set rather than assume it is still active. See the
# failed-delivery re-dispatch (StaffService.redispatch_failed_delivery ->
# OrderScheduleService.reschedule) and return_delivery_to_pool.
ACTIVE_ORDER_STATUSES: FrozenSet[OrderStatus] = frozenset(
    {
        OrderStatus.PENDING,
        OrderStatus.CONFIRMED,
        OrderStatus.PREPARING,
        OrderStatus.OUT_FOR_DELIVERY,
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
            error_code="ORDER_DELIVERY_ADDRESS_REQUIRED",
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


def assert_unassigned_for_pool_status(
    delivery,
    target_status: DeliveryStatus,
) -> None:
    """Reject leaving a delivery in a driverless status (the scheduled/pending
    pool, or a held `rescheduled` row) while it still has a delivery person.
    Such rows are stranded — invisible to both the driver's active list and the
    unassigned pool. Inverse of ``assert_delivery_person_for_status``. Callers
    mutate the delivery (clear the driver / set the status) before calling."""
    target = _coerce_delivery_status(target_status)
    if target not in DELIVERY_DRIVERLESS_STATES:
        return

    if getattr(delivery, "delivery_person_id", None) is not None:
        current = _coerce_delivery_status(getattr(delivery, "status", None))
        raise InvalidStateTransition(
            f"Delivery cannot rest in {target.value} while assigned to a driver; "
            "return it to the pool (clear the driver) instead",
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
