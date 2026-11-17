"""The published shape of the coming price: the one builder every customer surface calls (spec §4.3).

A card, a cart line, an estimate line, an operator-estimate line and an admin-quote line all take the block from here,
so the shape and the "who is told" rule live in one place.
"""

from typing import Any, Dict, Optional

from business_app.services.corporate_contract_service import PRICING_SOURCE_FALLBACK
from business_app.services.price_change.schedule import PriceChangeService


def upcoming_price_block(
    product_id: int,
    *,
    pricing_source: Optional[str] = None,
    contract_price_at_checkout: bool = False,
) -> Optional[Dict[str, Any]]:
    """`{"new_price", "effective_date"}` for a product with an announced change, else None.

    A contract price is not the catalogue, so the coming catalogue price is not stated beside it: a screen drawn
    before a payment method passes `contract_price_at_checkout`; a line priced on a chosen method passes its
    `pricing_source` (a contract customer paying by Click is charged the catalogue and is told).
    """
    if contract_price_at_checkout:
        return None
    if pricing_source not in (None, PRICING_SOURCE_FALLBACK):
        return None
    upcoming = PriceChangeService().upcoming_price(product_id)
    if upcoming is None:
        return None
    return {"new_price": float(upcoming.new_price), "effective_date": upcoming.change_on.isoformat()}
