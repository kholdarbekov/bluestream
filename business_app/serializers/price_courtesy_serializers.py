"""The formatters of the price-change courtesy: the status staff screens and the customer page read, and the quote
block every quote publishes."""

from typing import Optional

from pydantic import BaseModel, ConfigDict

from business_app.models.price_change import PriceCourtesy
from business_app.services.price_change.courtesy import CourtesyStatus


def serialize_courtesy_status(status: CourtesyStatus) -> dict:
    return {
        "state": status.state,
        "expires_on": status.expires_on.isoformat() if status.expires_on is not None else None,
        "caps": [
            {"product_id": cap.product_id, "product_name": cap.product_name, "cap": cap.cap} for cap in status.caps
        ],
        "used_order_id": status.used_order_id,
        "used_order_number": status.used_order_number,
    }


def serialize_price_courtesy(entry: Optional[PriceCourtesy]) -> Optional[dict]:
    """The quote's courtesy block: present only when `apply_courtesy` lowered a line, with the last local day an order
    can use it."""
    if entry is None:
        return None
    return {"applied": True, "expires_on": entry.price_change.courtesy_last_day.isoformat()}


class GrantPriceCourtesyPayload(BaseModel):
    """The manager exception's body: the reason is the only input. Blank is refused by `grant_exception`."""

    model_config = ConfigDict(extra="forbid")

    reason: str
