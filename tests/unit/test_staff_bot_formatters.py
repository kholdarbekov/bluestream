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
from staff_bot.utils.formatters import (
    MINUS_SIGN,
    format_currency,
    format_delivery_window_line,
    format_local_date,
    format_number,
    format_order_card,
    format_pay_amount,
    format_pay_month,
    format_pay_product_name,
    signed_currency,
    signed_deduction,
)


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


# ---------------------------------------------------------------------------
# `format_local_date` — the sales card's visit dates (phase 2a, Task 10)
# ---------------------------------------------------------------------------


def test_format_local_date_reads_a_utc_stamp_as_the_local_calendar_day():
    """A visit closed at 00:30 Tashkent is stamped 19:30Z the day BEFORE.

    The backend publishes `last_visit.ended_at` / `next_visit_due_at` as UTC
    ISO strings, and the agent reads them as calendar days ("I was there on
    the 31st"). Slicing the ISO string, or formatting it without converting,
    is off by a day for every evening visit — five hours of every day.
    """
    assert format_local_date("2026-08-30T19:30:00+00:00") == "31.08.2026"
    assert format_local_date("2026-08-30T11:20:00+00:00") == "30.08.2026"


def test_format_local_date_handles_a_bare_date_a_zulu_stamp_and_junk():
    """`agent_next_visit_at` is a DATE column and serializes as `YYYY-MM-DD`;
    the same field on other payloads is a tz-aware datetime, and a gateway or
    a hand-edited row can send neither. One helper takes all three, and
    answers `''` — not a crash and not a half-rendered line — for the last,
    so every caller can gate its whole line on the returned string.
    """
    assert format_local_date("2026-09-10") == "10.09.2026"
    assert format_local_date("2026-09-10T00:00:00Z") == "10.09.2026"
    assert format_local_date(None) == ""
    assert format_local_date("") == ""
    assert format_local_date("not a date") == ""


def test_format_local_date_does_not_move_a_bare_date_behind_a_negative_offset(monkeypatch):
    """`YYYY-MM-DD` is a DATE, and a date has no timezone to convert.

    Read as midnight UTC it survives only because Tashkent is AHEAD of UTC:
    under any display timezone BEHIND it, `agent_next_visit_at` renders as the
    day before — the visit the agent planned for the 10th shows as the 9th.
    """
    monkeypatch.setattr("shared.constants.DISPLAY_TIMEZONE", "America/New_York")

    assert format_local_date("2026-09-10") == "10.09.2026"
    # The datetime path still converts — and the patched zone is really in play:
    # 02:00Z is 22:00 on the 9th in New York.
    assert format_local_date("2026-09-10T02:00:00+00:00") == "09.09.2026"


# ---- sales-agent pay (compensation spec §7.2) -----------------------------------------


def _seed_currency(monkeypatch, language="en", word="UZS"):
    """The one row these helpers read, served the way this suite serves `{time}` above."""
    merged = {**i18n.translations.get(language, {}), "staff.currency.uzs": word}
    monkeypatch.setitem(i18n.translations, language, merged)


def test_signed_currency_prints_the_true_minus_before_the_absolute_amount(monkeypatch):
    """U+2212, never the ASCII hyphen: "-20,000" next to a grouped figure reads as a dash."""
    _seed_currency(monkeypatch)
    assert MINUS_SIGN == "−"
    assert signed_currency(-20000, "en") == "−20,000 UZS"
    assert signed_currency(-2250.0, "en") == "−2,250 UZS"
    assert signed_currency(200000, "en") == "+200,000 UZS"
    assert signed_currency(0, "en") == "+0 UZS"
    assert "-" not in signed_currency(-1, "en")


def test_a_penalty_the_backend_publishes_positive_prints_as_the_deduction_it_is(monkeypatch):
    _seed_currency(monkeypatch)
    assert signed_deduction(150000, "en") == "−150,000 UZS"
    assert signed_deduction(150000.0, "en") == signed_currency(-150000, "en")


def test_a_normally_positive_figure_is_plain_until_it_goes_below_zero(monkeypatch):
    """C1 v3: nothing is floored, so a negative variable pay prints with its minus."""
    _seed_currency(monkeypatch)
    assert format_pay_amount(760000.0, "en") == "760,000 UZS"
    assert format_pay_amount(0, "en") == "0 UZS"
    assert format_pay_amount(-300000.0, "en") == "−300,000 UZS"


def test_the_russian_money_word_is_the_currency_row_not_a_literal(monkeypatch):
    _seed_currency(monkeypatch, "ru", "сум")
    assert signed_currency(-20000, "ru") == "−20,000 сум"


def test_a_pay_month_on_the_wire_reads_as_month_dot_year():
    assert format_pay_month("2026-10") == "10.2026"
    assert format_pay_month("2027-01") == "01.2027"
    assert format_pay_month(None) == ""
    assert format_pay_month("") == ""


def test_a_figure_is_grouped_once_with_or_without_a_currency(monkeypatch):
    """D-BANDS: tier bounds, unit counts and a tier's figures print without a currency, grouped
    the way money is. `format_currency` groups through `format_number`, so "1,001" on a tier row
    and "1,001 UZS" on a money row cannot drift, and money prints exactly as before: 0 for a
    missing figure, junk echoed rather than raised."""
    _seed_currency(monkeypatch)
    assert [format_number(v) for v in (1001, 12500000.0, 2000.0, 0, None, "n/a")] == [
        "1,001", "12,500,000", "2,000", "0", "0", "n/a",
    ]
    assert [format_currency(v, language="en") for v in (1001, 2800000.0, -20000, None, "n/a")] == [
        "1,001 UZS", "2,800,000 UZS", "-20,000 UZS", "0 UZS", "n/a UZS",
    ]


def test_a_product_is_named_in_the_agents_language_else_english_and_escaped():
    """§7.2: `product_name[language]`, falling back to `en`, through `escape_html` like every
    backend string placed in an HTML message."""
    names = {"en": "19L <b>promo</b> & co", "ru": "19 л"}
    assert format_pay_product_name(names, "ru") == "19 л"
    assert format_pay_product_name(names, "uz") == "19L &lt;b&gt;promo&lt;/b&gt; &amp; co"
    assert format_pay_product_name(None, "en") == ""
