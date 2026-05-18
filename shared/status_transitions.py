"""
Canonical status-transition rules for the Water Business Platform.

Single source of truth used by:
- business_app (admin / order / delivery services)
- telegram_bot (customer)
- staff_bot (driver) via shared/staff_constants
- admin_ui (via GET /api/v1/orders/statuses)

When changing a transition rule, edit this file only. The /orders/statuses
endpoint exposes the maps to the admin UI; the bots and backend import them
directly.
"""
from typing import Dict, List

from shared.enums import OrderStatus, DeliveryStatus


ORDER_STATUS_TRANSITIONS: Dict[OrderStatus, List[OrderStatus]] = {
    OrderStatus.PENDING: [OrderStatus.CONFIRMED, OrderStatus.CANCELLED],
    OrderStatus.CONFIRMED: [OrderStatus.PREPARING, OrderStatus.DELIVERED, OrderStatus.CANCELLED],
    OrderStatus.PREPARING: [OrderStatus.OUT_FOR_DELIVERY, OrderStatus.CANCELLED],
    OrderStatus.OUT_FOR_DELIVERY: [OrderStatus.DELIVERED, OrderStatus.RETURNED, OrderStatus.CANCELLED],
    OrderStatus.DELIVERED: [],
    OrderStatus.CANCELLED: [],
    OrderStatus.RETURNED: [OrderStatus.PENDING],
}


DELIVERY_STATUS_TRANSITIONS: Dict[DeliveryStatus, List[DeliveryStatus]] = {
    DeliveryStatus.PENDING: [DeliveryStatus.ASSIGNED, DeliveryStatus.FAILED, DeliveryStatus.CANCELLED],
    DeliveryStatus.SCHEDULED: [
        DeliveryStatus.ASSIGNED,
        DeliveryStatus.PICKED_UP,
        DeliveryStatus.IN_TRANSIT,
        DeliveryStatus.ARRIVED,
        DeliveryStatus.DELIVERED,
        DeliveryStatus.FAILED,
        DeliveryStatus.CANCELLED,
    ],
    DeliveryStatus.ASSIGNED: [DeliveryStatus.PICKED_UP, DeliveryStatus.FAILED, DeliveryStatus.CANCELLED],
    DeliveryStatus.PICKED_UP: [DeliveryStatus.IN_TRANSIT, DeliveryStatus.FAILED, DeliveryStatus.CANCELLED],
    DeliveryStatus.IN_TRANSIT: [DeliveryStatus.ARRIVED, DeliveryStatus.FAILED, DeliveryStatus.CANCELLED],
    DeliveryStatus.ARRIVED: [DeliveryStatus.DELIVERED, DeliveryStatus.FAILED, DeliveryStatus.CANCELLED],
    DeliveryStatus.DELIVERED: [],
    DeliveryStatus.FAILED: [],
    DeliveryStatus.CANCELLED: [],
    DeliveryStatus.RETURNED: [DeliveryStatus.PENDING],
}


def is_valid_order_transition(current: OrderStatus, new: OrderStatus) -> bool:
    return new in ORDER_STATUS_TRANSITIONS.get(current, [])


def is_valid_delivery_transition(current: DeliveryStatus, new: DeliveryStatus) -> bool:
    return new in DELIVERY_STATUS_TRANSITIONS.get(current, [])


def allowed_next_order_statuses(current: OrderStatus) -> List[OrderStatus]:
    return list(ORDER_STATUS_TRANSITIONS.get(current, []))


def allowed_next_delivery_statuses(current: DeliveryStatus) -> List[DeliveryStatus]:
    return list(DELIVERY_STATUS_TRANSITIONS.get(current, []))


def order_transitions_as_strings() -> Dict[str, List[str]]:
    """Serialised view for the admin UI / JSON API responses."""
    return {cur.value: [nxt.value for nxt in nxts] for cur, nxts in ORDER_STATUS_TRANSITIONS.items()}


def delivery_transitions_as_strings() -> Dict[str, List[str]]:
    """Serialised view for the staff bot (legacy string-keyed callers) and JSON APIs."""
    return {cur.value: [nxt.value for nxt in nxts] for cur, nxts in DELIVERY_STATUS_TRANSITIONS.items()}
