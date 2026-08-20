"""Delivery-window rendering on the staff bot's order card (Task 13,
scheduled-delivery-orders).

The backend publishes `delivery_window` = {start, end, kind, label} on every
order payload. `label` is an English log/fallback string — rendering it
directly to a driver is one of this codebase's known English-leak classes.
These tests prove `format_order_card` (and the shared
`format_delivery_window_line` helper it uses) branch on `kind` and build a
localized string from `start`/`end` instead of reading `label` or the legacy
`time_slot` string.
"""
import pytest

from staff_bot.i18n import i18n
from staff_bot.utils.formatters import format_delivery_window_line, format_order_card


def _seed_window_translations(monkeypatch, language="en"):
    """Match this suite's established idiom (see test_route_card_render.py) for
    exercising `{placeholder}` interpolation: staff_bot unit tests never load
    the real DB catalog, so `i18n.get` normally falls back to a capitalized
    key segment with no `{time}` placeholder to substitute into."""
    merged = {
        **i18n.translations.get(language, {}),
        "staff.delivery.window.anytime": "Anytime today",
        "staff.delivery.window.between": "Between {time}",
        "staff.delivery.window.until": "Deliver before {time}",
        "staff.delivery.window.after": "Deliver after {time}",
    }
    monkeypatch.setitem(i18n.translations, language, merged)


@pytest.mark.parametrize("window,expected_time", [
    ({"start": None, "end": "10:00", "kind": "until", "label": "until 10:00"}, "10:00"),
    ({"start": "19:00", "end": None, "kind": "after", "label": "after 19:00"}, "19:00"),
])
def test_order_card_renders_the_window_time(monkeypatch, window, expected_time):
    _seed_window_translations(monkeypatch)
    card = format_order_card({"order_number": "TG_1_26", "delivery_window": window}, "en")
    assert expected_time in card
    assert "🕐" in card


def test_order_card_renders_between_with_both_times(monkeypatch):
    """The `between` shape needs BOTH times, not just one side."""
    _seed_window_translations(monkeypatch)
    window = {"start": "12:00", "end": "18:00", "kind": "between", "label": "12:00-18:00"}
    card = format_order_card({"order_number": "TG_1_26", "delivery_window": window}, "en")
    assert "12:00-18:00" in card


def test_order_card_omits_an_anytime_window():
    card = format_order_card(
        {"order_number": "TG_1_26",
         "delivery_window": {"start": None, "end": None, "kind": "anytime", "label": "anytime"}},
        "en",
    )
    assert "🕐" not in card


def test_order_card_never_renders_the_backend_label(monkeypatch):
    """Even when `label` is present, the card must be built from `kind` +
    `start`/`end` — never by printing `label` directly."""
    _seed_window_translations(monkeypatch)
    window = {"start": None, "end": "10:00", "kind": "until",
              "label": "SENTINEL_ENGLISH_LABEL_MUST_NOT_LEAK"}
    card = format_order_card({"order_number": "TG_1_26", "delivery_window": window}, "en")
    assert "SENTINEL_ENGLISH_LABEL_MUST_NOT_LEAK" not in card


def test_order_card_ignores_legacy_time_slot_field():
    """The transitional `time_slot` string must no longer be read directly —
    only `delivery_window` drives the rendered line."""
    card = format_order_card(
        {"order_number": "TG_1_26", "time_slot": "LEGACY_TIME_SLOT_TEXT",
         "delivery_window": {"start": None, "end": None, "kind": "anytime", "label": "anytime"}},
        "en",
    )
    assert "LEGACY_TIME_SLOT_TEXT" not in card


def test_missing_delivery_window_renders_nothing():
    """Defensive: a payload without `delivery_window` must not crash and must
    not show a window line."""
    card = format_order_card({"order_number": "TG_1_26"}, "en")
    assert "🕐" not in card


def test_format_delivery_window_line_between_interpolates_both_times(monkeypatch):
    _seed_window_translations(monkeypatch)
    line = format_delivery_window_line(
        {"delivery_window": {"start": "12:00", "end": "18:00", "kind": "between", "label": "x"}},
        "en",
    )
    assert line == "🕐 Between 12:00-18:00"


def test_format_delivery_window_line_anytime_is_empty():
    line = format_delivery_window_line(
        {"delivery_window": {"start": None, "end": None, "kind": "anytime", "label": "anytime"}},
        "en",
    )
    assert line == ""
