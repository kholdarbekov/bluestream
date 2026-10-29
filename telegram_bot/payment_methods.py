"""Turn the backend's available_methods payload into keyboard buttons.

Shared by checkout and subscription creation so the two menus can never drift.
The backend already excludes payme and loyalty_points (shared/payment_methods.py);
this module only handles presentation: `click` is shown to customers as "Card".
"""

from typing import Any, Dict, List

from i18n import i18n

# Customers see one "Card" button; the provider is Click.
_CARD_PROVIDERS = {"click"}


def build_payment_method_buttons(available_methods: List[Dict[str, Any]], language: str) -> List[Dict[str, str]]:
    codes = {
        str(method.get("method"))
        for method in (available_methods or [])
        if method.get("is_active", True)
    }

    buttons: List[Dict[str, str]] = []
    if "cash" in codes:
        buttons.append({"type": "cash", "name": i18n.get("telegram.payment_cash", language)})
    if codes & _CARD_PROVIDERS:
        buttons.append({"type": "card", "name": i18n.get("telegram.payment_card", language)})
    if "business_account" in codes:
        buttons.append({"type": "business_account", "name": i18n.get("telegram.payment_business_account", language)})
    return buttons


def published_method(button_type: str, available_methods: List[Dict[str, Any]]) -> str:
    """The method code the backend published for a button: "Card" stands for the active card provider (`click`),
    every other button is its own code. A callback that must send the published code, not the button's name, asks here."""
    if button_type != "card":
        return button_type
    return next(
        (str(m["method"]) for m in available_methods or [] if m.get("method") in _CARD_PROVIDERS and m.get("is_active", True)),
        button_type,
    )
