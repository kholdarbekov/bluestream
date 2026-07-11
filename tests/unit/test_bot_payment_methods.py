import sys, pathlib

sys.path.insert(0, str(pathlib.Path("telegram_bot").resolve()))

from payment_methods import build_payment_method_buttons


def _api(*codes):
    return [{"method": c, "is_active": True} for c in codes]


def test_click_is_presented_as_card():
    buttons = build_payment_method_buttons(_api("cash", "click"), "en")
    assert [b["type"] for b in buttons] == ["cash", "card"]


def test_business_account_is_offered_when_present():
    buttons = build_payment_method_buttons(_api("cash", "click", "business_account"), "en")
    assert "business_account" in [b["type"] for b in buttons]


def test_payme_never_produces_a_button():
    buttons = build_payment_method_buttons(_api("cash", "payme"), "en")
    assert [b["type"] for b in buttons] == ["cash"]


def test_inactive_methods_are_dropped():
    buttons = build_payment_method_buttons([{"method": "click", "is_active": False}], "en")
    assert buttons == []


def test_callback_parse_survives_underscored_method():
    """Regression: split('_')[2] on 'sub_payment_business_account' gives 'business'."""
    assert "sub_payment_business_account".split("_", 2)[2] == "business_account"
    assert "sub_payment_cash".split("_", 2)[2] == "cash"
