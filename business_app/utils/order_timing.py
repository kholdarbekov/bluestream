"""Shared SSOT helper for an order's delivery-completion timestamp.

Used by every post-delivery edit window (order-item edits and collected-cash
edits) so "when was this delivered" is computed one way everywhere.
"""

from datetime import datetime, timezone
from typing import Optional


def delivered_at_utc(order) -> Optional[datetime]:
    """Return the order's delivery-completion timestamp as tz-aware UTC.

    Prefers the Delivery row's actual delivery time; falls back to the order's
    ``paid_at`` (set when a cash order is marked DELIVERED). Returns None when
    neither is available. Naive datetimes are interpreted as UTC.
    """
    delivery = getattr(order, "delivery", None)
    if delivery is not None:
        value = getattr(delivery, "actual_delivery", None) or getattr(delivery, "actual_delivery_time", None)
        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value
    paid_at = getattr(order, "paid_at", None)
    if paid_at is not None:
        if paid_at.tzinfo is None:
            paid_at = paid_at.replace(tzinfo=timezone.utc)
        return paid_at
    return None
