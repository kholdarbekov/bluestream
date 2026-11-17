"""The one line-money formula: what an order line costs, with or without a price-change courtesy.

Every line-total writer and every split of a line not stored yet (a quote, an edit preview) calls
`line_money`, so the discount and the total are computed in one place and a client never subtracts
(`regular_quantity` is published for that reason). A STORED line is published as it is stored, by
`order_serializers.order_item_split`, and never recomputed. `rail_pricing.py` keeps its own
`new_unit * quantity` arithmetic on purpose: contract-covered lines never carry a courtesy and it never
re-prices a courtesy line, so that path has no discount to compute.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple, Optional

_TIYIN = Decimal("0.01")


class LineMoney(NamedTuple):
    discount_amount: Decimal
    total_price: Decimal
    regular_quantity: Optional[int]


def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(_TIYIN, rounding=ROUND_HALF_UP)


def line_money(
    unit_price: Decimal,
    quantity: int,
    courtesy_quantity: Optional[int] = None,
    courtesy_unit_price: Optional[Decimal] = None,
) -> LineMoney:
    """Return the discount, the total and the regular (full-price) unit count of one line.

    A courtesy is the pair (`courtesy_quantity`, `courtesy_unit_price`): that many units are sold at
    the old price. Both arguments are given together or not at all. `courtesy_quantity` must lie in
    `1..quantity` (0 is refused: a line without a courtesy stores NULL, never 0), and the old price
    cannot be above the current one.
    """
    if (courtesy_quantity is None) != (courtesy_unit_price is None):
        raise ValueError("courtesy_quantity and courtesy_unit_price must be given together")

    price = _money(unit_price)
    gross = _money(price * quantity)

    if courtesy_quantity is None:
        return LineMoney(Decimal("0.00"), gross, None)

    if not 1 <= courtesy_quantity <= quantity:
        raise ValueError(f"courtesy_quantity must be within 1..{quantity}, got {courtesy_quantity}")

    old_price = _money(courtesy_unit_price)
    if old_price > price:
        raise ValueError("courtesy_unit_price cannot be above unit_price")

    discount = _money(courtesy_quantity * (price - old_price))
    total = _money(gross - discount)
    return LineMoney(discount, total, quantity - courtesy_quantity)
