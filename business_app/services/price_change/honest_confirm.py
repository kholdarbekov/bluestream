"""The honest confirm: a create that carries the total its screen showed is refused when that total has moved.

`expected_total` is `pricing.final_total` of the estimate the customer saw. Order creation compares it with the
order's `total_amount` after a redeemed reward and before stock is reserved, so a refusal writes nothing. An absent
value means no check (subscription billing, repeat, cash rescue, old clients).
"""

from decimal import Decimal
from typing import Any, Optional

from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from business_app.serializers.types import ExpectedTotal
from business_app.services.customer_order_actions import money_value
from business_app.utils.exceptions import StateChangedError, ValidationError

PRICE_CHANGED_REASON = "price_changed"
EXPECTED_TOTAL_INVALID = "EXPECTED_TOTAL_INVALID"

_EXPECTED_TOTAL = TypeAdapter(Optional[ExpectedTotal])


def parse_expected_total(raw: Any) -> Optional[Decimal]:
    """The shown total as a `Decimal`, or None when the caller sent none. An unreadable one is a 400."""
    try:
        return _EXPECTED_TOTAL.validate_python(raw)
    except PydanticValidationError as exc:
        raise ValidationError(
            "expected_total must be a non-negative amount with at most two decimals",
            validation_errors=[str(error["msg"]) for error in exc.errors()],
            error_code=EXPECTED_TOTAL_INVALID,
        ) from exc


def assert_expected_total(expected_total: Optional[Decimal], total_amount: Decimal) -> None:
    """Refuse (409 `price_changed`) when the shown total differs from the total the order would charge."""
    if expected_total is None:
        return
    if money_value(expected_total) != money_value(total_amount):
        raise StateChangedError(details={"reason": PRICE_CHANGED_REASON})
