"""Price-change request bodies and the one row the admin API publishes (spec 2026-11-16 §5.2).

The payloads document the contract; the bounds (a price above zero, a cap, the dates) are the service's, so every
refusal carries its `PRICE_CHANGE_*` code. `serialize_price_change` builds a RAW dict and `to_wire` makes it JSON.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict

from business_app.models.price_change import DEFAULT_COURTESY_DAYS, DEFAULT_REMINDER_LEAD_DAYS, PriceChange
from business_app.serializers.sales_pay_serializers import to_wire
from business_app.services.price_change.schedule import price_change_stage, row_actions


class PriceChangeItemPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int
    new_price: Decimal
    courtesy_cap: int


class PriceChangePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    announce_on: date
    change_on: date
    reminder_lead_days: int = DEFAULT_REMINDER_LEAD_DAYS
    courtesy_days: int = DEFAULT_COURTESY_DAYS
    items: List[PriceChangeItemPayload]


def _person_name(user) -> Any:
    return None if user is None else user.full_name


def serialize_price_change(price_change: PriceChange, *, viewer, now: datetime) -> Dict[str, Any]:
    """The ROW: one price change as the admin list and detail publish it."""
    raw = {
        "id": price_change.id,
        "status": price_change.status.value,
        "stage": price_change_stage(price_change, now=now),
        "announce_on": price_change.announce_on,
        "change_on": price_change.change_on,
        "reminder_on": price_change.reminder_on,
        "reminder_lead_days": price_change.reminder_lead_days,
        "courtesy_days": price_change.courtesy_days,
        "courtesy_last_day": price_change.courtesy_last_day,
        "price_valid_until": price_change.price_valid_until,
        "effective_at": price_change.effective_at,
        "courtesy_ends_at": price_change.courtesy_ends_at,
        "announcement_started_at": price_change.announcement_started_at,
        "reminder_started_at": price_change.reminder_started_at,
        "applied_at": price_change.applied_at,
        "cancelled_at": price_change.cancelled_at,
        "created_by_id": price_change.created_by_id,
        "created_by_name": _person_name(price_change.created_by),
        "cancelled_by_id": price_change.cancelled_by_id,
        "cancelled_by_name": _person_name(price_change.cancelled_by),
        "created_at": price_change.created_at,
        "allowed_actions": row_actions(price_change, viewer),
        "items": [
            {
                "product_id": item.product_id,
                "product_name": item.product.name,
                "current_price": item.product.catalogue_unit_price,
                "new_price": item.new_price,
                "old_price": item.old_price,
                "courtesy_cap": item.courtesy_cap,
                "min_order_quantity": int(item.product.min_order_quantity or 1),
            }
            for item in price_change.items
        ],
    }
    return to_wire(raw)
