"""Helpers for deriving consistent payment amount projections."""

from decimal import Decimal
from typing import Any, Dict, Optional

from shared.enums import PaymentMethod, PaymentStatus

PREPAID_METHOD_VALUES = {
    PaymentMethod.CARD.value,
    PaymentMethod.PAYME.value,
    PaymentMethod.CLICK.value,
    PaymentMethod.LOYALTY_POINTS.value,
    PaymentMethod.BUSINESS_ACCOUNT.value,
}


def _enum_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(Decimal("0.01"))


def is_settled_prepayment(payment: Any) -> bool:
    """Return True when payment is a non-COD prepaid payment marked completed."""
    method_value = _enum_value(getattr(payment, "payment_method", None))
    status_value = _enum_value(getattr(payment, "status", None))
    return method_value in PREPAID_METHOD_VALUES and status_value == PaymentStatus.COMPLETED.value


def get_payment_projection(payment: Any) -> Dict[str, Any]:
    """
    Return normalized payment projection fields for read/write surfaces.

    For completed prepaid payments, projection is always fully settled:
    amount_collected == amount and outstanding_amount == 0.
    """
    amount = max(Decimal("0.00"), _to_decimal(getattr(payment, "amount", 0)))
    amount_collected = max(
        Decimal("0.00"),
        min(amount, _to_decimal(getattr(payment, "amount_collected", 0))),
    )
    outstanding_amount = max(
        Decimal("0.00"),
        _to_decimal(getattr(payment, "outstanding_amount", 0)),
    )

    if is_settled_prepayment(payment):
        amount_collected = amount
        outstanding_amount = Decimal("0.00")
    else:
        method_value = _enum_value(getattr(payment, "payment_method", None))
        if method_value == PaymentMethod.CASH.value:
            outstanding_amount = max(Decimal("0.00"), amount - amount_collected)
        else:
            outstanding_amount = min(amount, outstanding_amount)

    return {
        "amount": amount,
        "amount_collected": amount_collected,
        "outstanding_amount": outstanding_amount,
        "payment_method": _enum_value(getattr(payment, "payment_method", None)),
        "payment_status": _enum_value(getattr(payment, "status", None)),
        "is_settled_prepayment": is_settled_prepayment(payment),
    }
