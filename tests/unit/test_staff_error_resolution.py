"""How a backend refusal becomes the sentence staff read."""
from unittest.mock import MagicMock

import pytest

from staff_bot.handlers.base import BaseHandler
from staff_bot.i18n import i18n
from staff_bot.utils.api_errors import ErrorDetailCopy, displayable_backend_reason, render_detail_copy


@pytest.fixture
def copy(monkeypatch):
    table = {
        "staff.error.api.backend_reason": "Couldn't complete this: {reason}",
        "staff.error.api.validation": "Please check the entered data and try again.",
        "staff.test.plain": "Plain copy.",
        "staff.test.detail": "Has {available} of {required}.",
        "staff.test.money": "Owes {amount}.",
        "staff.currency.uzs": "UZS",
    }
    monkeypatch.setitem(i18n.translations, "en", {**i18n.translations.get("en", {}), **table})
    return table


def _handler():
    return BaseHandler.__new__(BaseHandler)


def test_detail_copy_uses_the_backends_numbers(copy):
    spec = ErrorDetailCopy("staff.test.detail", {"available": "available", "required": "required"})
    assert render_detail_copy(spec, {"available": 4, "required": 5}, "en") == "Has 4 of 5."


def test_detail_copy_falls_back_to_plain_key_without_details(copy, monkeypatch):
    monkeypatch.setitem(BaseHandler.API_ERROR_CODE_KEY_MAP, "TEST_CODE", "staff.test.plain")
    monkeypatch.setitem(
        BaseHandler.API_ERROR_DETAIL_COPY,
        "TEST_CODE",
        (ErrorDetailCopy("staff.test.detail", {"available": "available", "required": "required"}),),
    )
    for details in (None, {}, {"available": 4}):
        assert _handler()._resolve_api_error_message("en", "x", 400, "TEST_CODE", details=details) == "Plain copy."


def test_money_format_is_applied(copy):
    spec = ErrorDetailCopy("staff.test.money", {"amount": "debt"}, {"amount": "money"})
    assert render_detail_copy(spec, {"debt": 1250000}, "en") == "Owes 1,250,000 UZS."


def test_html_mode_escapes_only_the_interpolated_values(copy):
    spec = ErrorDetailCopy("staff.test.detail", {"available": "available", "required": "required"})
    assert render_detail_copy(spec, {"available": "<4>", "required": "A&B"}, "en", html=True) == (
        "Has &lt;4&gt; of A&amp;B."
    )
    reason = _handler()._resolve_api_error_message(
        "en", "x", 400, None, server_message="Tom & Jerry <x>", html=True
    )
    assert reason == "Couldn't complete this: Tom &amp; Jerry &lt;x&gt;"


@pytest.mark.parametrize("status", [400, 403, 404, 409, 412, 422])
def test_an_unmapped_refusal_shows_the_backends_sentence(copy, status):
    text = _handler()._resolve_api_error_message(
        "en", "Handoff amount must be positive", status, None,
        server_message="Handoff amount must be positive", error_type="VALIDATION_ERROR",
    )
    assert text == "Couldn't complete this: Handoff amount must be positive"


@pytest.mark.parametrize(
    "status, error_type, message",
    [
        (500, "INTERNAL_ERROR", "An unexpected error occurred"),
        (503, None, "Geocoding service temporarily unavailable"),
        (401, None, "The token has expired."),
        (429, "RATE_LIMIT_EXCEEDED", "Too many requests"),
        (400, "INVALID_VALUE", "invalid literal for int() with base 10: 'x'"),
        (400, "MISSING_KEY", "'COD_CASH_WARNING_THRESHOLD_UZS'"),
        (400, "VALIDATION_ERROR", "Validation failed"),
        (400, "VALIDATION_ERROR", "   "),
        (None, None, "Request failed after retries"),
    ],
)
def test_internals_and_non_reasons_are_never_shown(status, error_type, message):
    assert displayable_backend_reason(status, error_type, message) is None


def test_a_long_reason_is_cut():
    reason = displayable_backend_reason(400, "VALIDATION_ERROR", "word " * 200)
    assert reason is not None and len(reason) <= 300 and reason.endswith("…")


def test_a_mapped_code_beats_the_backends_sentence(copy):
    text = _handler()._resolve_api_error_message(
        "en", "x", 400, "STAFF_STATUS_REQUIRED", server_message="status field is required"
    )
    assert text == i18n.get(BaseHandler.API_ERROR_CODE_KEY_MAP["STAFF_STATUS_REQUIRED"], "en")


def test_mock_attributes_are_ignored(copy):
    response = MagicMock()
    response.error = "x"
    response.status_code = 400
    response.error_code = None
    text = _handler()._resolve_response_error("en", response)
    assert text == "Please check the entered data and try again."


def test_error_copy_keys_cover_every_target():
    keys = BaseHandler.error_copy_keys()
    assert set(BaseHandler.API_ERROR_CODE_KEY_MAP.values()) <= keys
    for specs in BaseHandler.API_ERROR_DETAIL_COPY.values():
        assert {spec.key for spec in specs} <= keys
    for _emoji, label, _callback in BaseHandler.API_ERROR_REMEDY_BUTTONS.values():
        assert label in keys


def _callbacks(markup):
    return [[button.callback_data for button in row] for row in markup.inline_keyboard]


def test_with_remedy_puts_the_remedy_row_above_the_screens_own_keyboard(copy, monkeypatch):
    """Error screens that edit in place keep their own keyboard; the code's
    remedy (API_ERROR_REMEDY_BUTTONS, the one place that rule lives) goes first."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    monkeypatch.setitem(BaseHandler.API_ERROR_REMEDY_BUTTONS, "TEST_CODE", ("📊", "staff.test.plain", "test_remedy"))
    own = InlineKeyboardMarkup([
        [InlineKeyboardButton("Back", callback_data="test_back")],
        [InlineKeyboardButton("Menu", callback_data="test_menu")],
    ])

    combined = _handler()._with_remedy("en", "TEST_CODE", own)
    assert _callbacks(combined) == [["test_remedy"], ["test_back"], ["test_menu"]]
    assert combined.inline_keyboard[0][0].text == "📊 Plain copy."

    assert _handler()._with_remedy("en", "NO_REMEDY_CODE", own) is own
    assert _handler()._with_remedy("en", None, own) is own
    assert _handler()._with_remedy("en", "NO_REMEDY_CODE") is None

    alone = _handler()._with_remedy("en", "TEST_CODE")
    assert _callbacks(alone) == [["test_remedy"]]


@pytest.mark.parametrize(
    "details, key, values",
    [
        ({"place_debt_count": 3}, "staff.error.api.cod_debt_limit_place_detail", {"count": "3"}),
        (
            {"debt_total": 1250000, "debt_limit": 1000000},
            "staff.error.api.cod_debt_limit_amount_detail",
            {"debt_total": "1,250,000 UZS", "debt_limit": "1,000,000 UZS"},
        ),
    ],
)
def test_the_cod_cap_names_its_numbers(copy, monkeypatch, details, key, values):
    template = "{" + "}/{".join(values) + "}"
    monkeypatch.setitem(i18n.translations["en"], key, template)
    text = _handler()._resolve_api_error_message("en", "x", 400, "COD_DEBT_LIMIT_REACHED", details=details)
    assert text == template.format(**values)


def test_the_cod_cap_without_details_keeps_its_plain_copy(copy, monkeypatch):
    monkeypatch.setitem(i18n.translations["en"], "staff.error.api.cod_debt_limit_reached", "Plain COD cap.")
    assert _handler()._resolve_api_error_message("en", "x", 400, "COD_DEBT_LIMIT_REACHED") == "Plain COD cap."
