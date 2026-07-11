"""Single source of truth for payment methods.

Imported by the backend AND both bots, so this module must stay pure: no
Flask, no SQLAlchemy, no app-specific imports. Errors are plain
``ValueError`` subclasses; service layers translate them into the
application's ``ValidationError``.

Three distinct sets, which are NOT interchangeable:

``ORDER_PAYMENT_METHODS``
    What the ``orders.payment_method`` column may legally hold, including
    legacy ``payme`` rows and their webhooks.

``CUSTOMER_SELECTABLE_METHODS``
    What any surface may offer, and what any NEW order may be created with.
    ``create_order`` enforces this one.

``READABLE_PAYMENT_METHODS``
    Adds ``card``, which exists only on legacy rows. ``"card"`` as an input
    always normalizes to ``CLICK``; it is never written again.

``loyalty_points`` appears in none of them — points buy rewards, never orders.

``PAYMENT_METHOD_CATALOG`` is read-only. Consumers must call ``copy.deepcopy()``
before mutating any entry to avoid corrupting the shared state.
"""

import copy
from typing import Any, Dict, List, Optional, Union

from shared.enums import PaymentMethod

ORDER_PAYMENT_METHODS = frozenset(
    {
        PaymentMethod.CASH,
        PaymentMethod.CLICK,
        PaymentMethod.PAYME,
        PaymentMethod.BUSINESS_ACCOUNT,
    }
)

CUSTOMER_SELECTABLE_METHODS = frozenset(
    {
        PaymentMethod.CASH,
        PaymentMethod.CLICK,
        PaymentMethod.BUSINESS_ACCOUNT,
    }
)

READABLE_PAYMENT_METHODS = ORDER_PAYMENT_METHODS | {PaymentMethod.CARD}

# Historical alias. The UI has always called Click "Card"; the enum kept a
# separate CARD member that only legacy rows use.
PAYMENT_METHOD_ALIASES: Dict[str, PaymentMethod] = {"card": PaymentMethod.CLICK}


class UnknownPaymentMethodError(ValueError):
    """The supplied value is not a payment method at all."""


class UnsupportedPaymentMethodError(ValueError):
    """A real payment method that no customer surface may offer."""


def normalize_payment_method(value: Union[str, PaymentMethod, None]) -> PaymentMethod:
    """Coerce a raw value to a canonical ``PaymentMethod``.

    Raises ``UnknownPaymentMethodError`` for anything unrecognised — including
    ``None`` and ``""``. It never returns ``None``: the previous
    ``dict.get(...)`` behaviour silently produced NULL-``payment_method``
    orders (see the design spec, §1.2).
    """
    if isinstance(value, PaymentMethod):
        aliased = PAYMENT_METHOD_ALIASES.get(value.value, value)
        if aliased not in READABLE_PAYMENT_METHODS:
            raise UnknownPaymentMethodError(f"Unknown payment method: {value!r}")
        return aliased

    if not isinstance(value, str) or not value.strip():
        raise UnknownPaymentMethodError(f"Unknown payment method: {value!r}")

    key = value.strip().lower()
    if key in PAYMENT_METHOD_ALIASES:
        return PAYMENT_METHOD_ALIASES[key]

    try:
        method = PaymentMethod(key)
    except ValueError as exc:
        raise UnknownPaymentMethodError(f"Unknown payment method: {value!r}") from exc

    if method not in READABLE_PAYMENT_METHODS:
        # loyalty_points is a real enum member but never a payment method.
        raise UnknownPaymentMethodError(f"Unknown payment method: {value!r}")

    return method


def assert_customer_selectable(method: PaymentMethod) -> None:
    """Raise ``UnsupportedPaymentMethodError`` unless a surface may offer ``method``."""
    if method not in CUSTOMER_SELECTABLE_METHODS:
        raise UnsupportedPaymentMethodError(f"Payment method is not available: {method.value}")


def is_customer_selectable(method: PaymentMethod) -> bool:
    return method in CUSTOMER_SELECTABLE_METHODS


# Display metadata for the statically-known selectable methods. ``is_active``
# is deliberately absent: PaymentService derives it from configured provider
# credentials. ``business_account`` has no entry — the service appends it only
# for eligible users.
PAYMENT_METHOD_CATALOG: List[Dict[str, Any]] = [
    {
        "method": "cash",
        "name": "cash",
        "display_name": "Cash on Delivery",
        "icon_url": "/static/images/payment/cash.png",
        "description": "Pay with cash when order is delivered",
        "supported_currencies": ["UZS"],
        "min_amount": 0,
        "max_amount": 5000000,
        "processing_fee": 0.0,
        "supports_recurring": False,
        "supports_refunds": False,
    },
    {
        "method": "click",
        "name": "click",
        "display_name": "Click",
        "icon_url": "/static/images/payment/click.png",
        "description": "Pay with Click wallet or linked card",
        "supported_currencies": ["UZS"],
        "min_amount": 1000,
        "max_amount": 50000000,
        "processing_fee": 0.0,
        "supports_recurring": True,
        "supports_refunds": True,
    },
]


# Legacy electronic providers collapse to the one live provider on repeat.
# To a customer, Payme / Card / Click all mean "pay online by card"; refusing
# to repeat a historical Payme order, or silently switching them to cash,
# would both be worse than settling them on Click.
LEGACY_ELECTRONIC_REPEAT_ALIASES: Dict[PaymentMethod, PaymentMethod] = {
    PaymentMethod.PAYME: PaymentMethod.CLICK,
}


def resolve_repeatable_payment_method(value: Union[str, PaymentMethod, None]) -> PaymentMethod:
    """The method a repeat of a historical order should use.

    ``normalize_payment_method`` already folds ``card`` into ``click``. This
    additionally collapses the retired ``payme`` provider. A legacy order with
    no payment method at all cannot be resolved and raises.
    """
    method = normalize_payment_method(value)  # raises UnknownPaymentMethodError on None/unknown
    method = LEGACY_ELECTRONIC_REPEAT_ALIASES.get(method, method)
    assert_customer_selectable(method)  # raises UnsupportedPaymentMethodError
    return method


def catalog_entry(method: PaymentMethod) -> Optional[Dict[str, Any]]:
    """Return a deep copy of the catalog entry for ``method``, or None."""
    for entry in PAYMENT_METHOD_CATALOG:
        if entry["method"] == method.value:
            return copy.deepcopy(entry)
    return None
