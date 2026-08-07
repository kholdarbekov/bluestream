"""Compact order payload for staff-bot notifications.

Lifted out of `business_app/api/admin.py` so services can build it without
importing an API module — a service depending on a route handler inverts the
layering the rest of the codebase follows.
"""

from typing import Any, Dict


def build_staff_order_info(delivery) -> Dict[str, Any]:
    """Build compact order payload for staff assignment notifications."""
    order = delivery.order
    if not order:
        return {"delivery_id": delivery.id, "order_id": delivery.order_id}

    address = order.delivery_address.full_address if order.delivery_address else None
    return {
        "delivery_id": delivery.id,
        "order_id": order.id,
        "order_number": order.order_number,
        "status": order.status.value if hasattr(order.status, "value") else order.status,
        "total_amount": float(order.total_amount or 0),
        "payment_method": order.payment_method.value if getattr(order, "payment_method", None) else None,
        "delivery_address": address,
    }
