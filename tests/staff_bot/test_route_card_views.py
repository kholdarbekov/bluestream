"""Pure-render tests for the route card's two views (spec §6.2/§6.3).

Also carries forward the assertions from the retired per-delivery list-card
test: phone, item lines and route position must stay visible (they come from
the shared format_active_delivery_summary — the one card format)."""

import importlib.util
import re
from pathlib import Path

import pytest
from telegram import InlineKeyboardMarkup

from staff_bot.handlers.delivery import route_card
from staff_bot.i18n import i18n
from staff_bot.utils.formatters import format_local_time

# Route-card copy is DB-backed (Task 2 seeded scripts/seed_staff_translations.py
# under category='staff_bot'), but nothing in this pure-render unit test loads
# translations from Postgres -- the shared `i18n` singleton starts with an
# empty `translations` dict here, same as every other staff_bot render test
# (see test_active_delivery_summary.py: "i18n falls back to humanized key
# tails"). That fallback is fine for structural assertions, but several
# assertions below check the actual product copy ("SUGGESTED NEXT", "Stop 3
# of 4", the ETA figures), so those specific keys need real values.
#
# Fix round 1, item 1: those values used to be pasted by hand into a local
# dict here. That is exactly the pattern CLAUDE.md forbids -- "a local copy
# of the rule keeps passing while production diverges" -- because an edit to
# scripts/seed_staff_translations.py would leave this test happily asserting
# stale copy while the card ships something else. Instead, load the script
# by path (scripts/ is not an importable package -- same technique as
# tests/unit/test_staff_route_translation_seed.py) and resolve every value
# through `_curated_value`, the SAME function `seed_translations()` calls to
# decide what actually gets written into Postgres.
_SEED_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_staff_translations", _SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SEED_MODULE = _load_seed_module()

# Every key this file's copy assertions depend on.
_ROUTE_CARD_KEYS = [
    "staff.route.suggested_next",
    "staff.route.current_stop",
    "staff.route.card_header",
    "staff.route.finish_by",
    "staff.route.updated_at",
    "staff.route.all_stops_header",
    "staff.route.all_stops_button",
    "staff.route.start_this_stop",
    "staff.route.open_stop",
    "staff.route.navigate_all",
    "staff.route.all_done",
    "staff.back",
    "staff.delivery.share_location_button",
    "staff.delivery.eta_minutes",
    "staff.delivery.distance_km",
    "staff.delivery.active_count",
]


@pytest.fixture(autouse=True)
def _seed_route_card_translations(monkeypatch):
    """Feed the real English route-card copy -- resolved live from the seed
    script -- into the i18n singleton for this file only. `monkeypatch.setitem`
    reverts the 'en' entry after every test (deleting it if it didn't exist
    before), so other test files that rely on the empty-dict fallback (e.g.
    test_active_delivery_summary.py) are never affected even when the whole
    suite runs in one process."""
    resolved = {}
    for key in _ROUTE_CARD_KEYS:
        value = _SEED_MODULE._curated_value(key, "en")
        assert value, f"{key} has no curated English value in seed_staff_translations.py"
        resolved[key] = value
    merged = {**i18n.translations.get("en", {}), **resolved}
    monkeypatch.setitem(i18n.translations, "en", merged)


def _item(delivery_id, order_no, *, status="assigned", lat=41.31, lng=69.27, district="Chilanzar",
          eta_suppressed=False, eta_source=None):
    return {
        "delivery_id": delivery_id, "order_id": delivery_id, "order_number": order_no,
        "status": status, "customer_name": "Umar", "customer_phone": "+998909150171",
        "district": district, "address": "Katta Qozirabot MFY",
        "items": [{"product_name": "19 litrlik suv", "quantity": 3}],
        "total_amount": 57000, "payment_method": "cash",
        "amount_collected": 0, "outstanding_amount": 57000,
        "expected_cash_to_collect": 57000, "cod_reserved_prepayment_amount": 0,
        "destination_latitude": lat, "destination_longitude": lng,
        "route_position": None, "is_next": False,
        "eta_minutes_from_current_location": None, "distance_km_to_next": None,
        # Plan 2 fields: exactly three legal states, see TestEtaBadgeGating.
        "eta_suppressed": eta_suppressed, "eta_source": eta_source,
    }


def _payload(items, *, committed=None, location_status="fresh"):
    for pos, it in enumerate(items):
        it["route_position"] = pos
        it["is_next"] = pos == 0
    return {
        "items": items,
        "total": len(items),
        "location_status": location_status,
        "route_summary": {
            "remaining": len(items),
            "stops_completed_today": 2,
            "stops_total_today": 2 + len(items),
            "committed_delivery_id": committed,
            "finish_eta": None,
            "updated_at": None,
        },
    }


def _buttons(kb: InlineKeyboardMarkup):
    return [(b.text, b.callback_data, b.url) for row in kb.inline_keyboard for b in row]


@pytest.mark.unit
class TestNextView:
    def test_unstarted_head_says_suggested_next_with_start_button(self):
        items = [_item(11, "1042"), _item(12, "1043")]
        items[0]["eta_minutes_from_current_location"] = 12
        items[0]["distance_km_to_next"] = 4.2
        text, kb = route_card.build_next_view(_payload(items), "en")

        assert "SUGGESTED NEXT" in text
        assert "next stop" not in text.lower()  # advisory copy only (spec §6.3)
        assert "Stop 3 of 4" in text            # 2 done + on the 3rd
        assert "#1042" in text
        assert "📞 +998909150171" in text        # shared formatter, not a fork
        assert "📦 19 litrlik suv ×3" in text
        assert "12" in text and "4.2" in text    # ETA line when not suppressed
        assert re.search(r"updated \d{2}:\d{2}", text)

        btns = _buttons(kb)
        callbacks = [c for _, c, _ in btns if c]
        assert "staff_view_active_11" in callbacks           # Start this stop
        assert "staff_route_view_all" in callbacks           # All stops
        assert "staff_share_location_prompt" in callbacks
        urls = [u for _, _, u in btns if u]
        assert len(urls) == 1 and urls[0].startswith("https://yandex.ru/maps/?rtext=~")

    def test_committed_head_says_current_stop_not_start(self):
        items = [_item(11, "1042", status="in_transit"), _item(12, "1043")]
        text, kb = route_card.build_next_view(_payload(items, committed=11), "en")
        assert "CURRENT STOP" in text
        assert "SUGGESTED NEXT" not in text
        labels = [t for t, _, _ in _buttons(kb)]
        assert not any("Start this stop" in t for t in labels)

    def test_missing_route_summary_degrades_header(self):
        """Deploy skew: an older backend without route_summary must not
        crash. Fix round 1, item 4: the original version of this test only
        asserted '#1042' was present, which would pass even with a garbled
        header -- assert the actual fallback line the header is supposed to
        degrade to."""
        payload = _payload([_item(11, "1042")])
        payload.pop("route_summary")
        text, _ = route_card.build_next_view(payload, "en")
        expected_count_line = _SEED_MODULE._curated_value("staff.delivery.active_count", "en").format(count=1)
        assert expected_count_line in text
        assert "#1042" in text

    def test_finish_eta_present_renders_local_time(self):
        """Fix round 1, item 3: the finish-ETA render path had no coverage.
        The backend emits an explicit-offset ISO string (never a bare 'Z',
        per Task 1's docstring), and `finish_eta` stays in UTC over the wire
        -- only `updated_at` gets timezone-middleware-rewritten -- so this
        must genuinely parse and convert, not print the raw string."""
        items = [_item(11, "1042")]
        payload = _payload(items)
        payload["route_summary"]["finish_eta"] = "2026-08-12T10:30:00+00:00"
        text, _ = route_card.build_next_view(payload, "en")
        assert "finish ~15:30" in text  # DISPLAY_TIMEZONE=Asia/Tashkent is UTC+05:00

    def test_finish_eta_absent_omits_fragment_entirely(self):
        """Honesty rule (Task 1): no route today / haversine fallback / no
        duration -> finish_eta is None -> never a substituted or computed
        default, the fragment must be fully absent."""
        items = [_item(11, "1042")]
        text, _ = route_card.build_next_view(_payload(items), "en")  # finish_eta=None
        assert "finish ~" not in text


@pytest.mark.unit
class TestEtaBadgeGating:
    """Fix round 1, item 2: the ETA badge must be keyed OFF the backend's
    `eta_suppressed` flag (Plan 2 SSOT), never re-derived from
    `location_status`. Exactly three backend states exist -- cover each, plus
    the regression this replaces (location_status alone must no longer gate
    the badge)."""

    def test_nothing_computed_state_shows_no_badge(self):
        """(eta_suppressed=False, eta_source=None) -- nothing computed."""
        items = [_item(11, "1042", eta_suppressed=False, eta_source=None)]
        text, _ = route_card.build_next_view(_payload(items), "en")
        assert "⏱" not in text and "📏" not in text

    def test_suppressed_state_hides_badge_even_if_values_were_present(self):
        """(eta_suppressed=True, eta_source=None) -- computed then
        deliberately withheld as untrustworthy. Today's backend always pairs
        eta_suppressed=True with null values, but the bot must not depend on
        that coincidence: setting real numbers here proves the render path
        actually branches on the flag itself, not merely on nulls."""
        items = [_item(11, "1042", eta_suppressed=True, eta_source=None)]
        items[0]["eta_minutes_from_current_location"] = 12
        items[0]["distance_km_to_next"] = 4.2
        text, _ = route_card.build_next_view(_payload(items), "en")
        assert "12" not in text and "4.2" not in text
        assert "⏱" not in text and "📏" not in text

    def test_error_state_shows_no_badge(self):
        """(eta_suppressed=False, eta_source="error") -- attempted and
        failed; the backend nulls the values on this path too."""
        items = [_item(11, "1042", eta_suppressed=False, eta_source="error")]
        text, _ = route_card.build_next_view(_payload(items), "en")
        assert "⏱" not in text and "📏" not in text

    def test_stale_location_status_no_longer_suppresses_a_trusted_eta(self):
        """Regression guard: the OLD rule gated on location_status=='fresh',
        a second place re-deciding a question the backend already answered
        via eta_suppressed. With eta_suppressed=False and real values, the
        badge must render even though the payload's location_status is
        stale -- the bot must not re-derive the decision."""
        items = [_item(11, "1042", eta_suppressed=False)]
        items[0]["eta_minutes_from_current_location"] = 12
        items[0]["distance_km_to_next"] = 4.2
        text, _ = route_card.build_next_view(_payload(items, location_status="stale"), "en")
        assert "12" in text and "4.2" in text


@pytest.mark.unit
class TestAllView:
    def test_rows_numbers_and_committed_marker(self):
        items = [
            _item(11, "1042", status="in_transit"),
            _item(12, "1043", district="Yunusobod"),
            _item(13, "1051", district="Sergeli"),
        ]
        text, kb = route_card.build_all_view(_payload(items, committed=11), "en")

        assert "3 remaining" in text
        assert "▶️ 1. #1042" in text
        assert "2. #1043" in text and "Yunusobod" in text
        assert " km" not in text  # no fabricated per-leg distances

        btns = _buttons(kb)
        assert ("1️⃣", "staff_view_active_11", None) in btns
        assert ("2️⃣", "staff_view_active_12", None) in btns
        assert ("3️⃣", "staff_view_active_13", None) in btns
        callbacks = [c for _, c, _ in btns if c]
        assert "staff_route_view_next" in callbacks  # Back

    def test_number_rows_capped_at_five(self):
        items = [_item(20 + n, f"20{n}") for n in range(7)]
        _, kb = route_card.build_all_view(_payload(items), "en")
        number_rows = [
            row for row in kb.inline_keyboard
            if row and (row[0].callback_data or "").startswith("staff_view_active_")
        ]
        assert [len(r) for r in number_rows] == [5, 2]


@pytest.mark.unit
class TestEmptyAndNav:
    def test_empty_view(self):
        text, kb = route_card.build_empty_view(
            {"items": [], "location_status": "missing", "route_summary": {}}, "en"
        )
        assert "🚚" in text
        callbacks = [c for _, c, _ in _buttons(kb) if c]
        assert "staff_share_location_prompt" in callbacks
        assert "staff_back_to_main" in callbacks

    def test_nav_url_multi_stop_in_route_order_capped(self):
        items = [_item(30 + n, f"30{n}", lat=41.3 + n * 0.01, lng=69.2 + n * 0.01) for n in range(6)]
        url = route_card.build_multi_stop_nav_url(items)
        assert url.startswith("https://yandex.ru/maps/?rtext=~41.3,69.2~")
        assert url.endswith("&rtt=auto")
        assert url.count("~") == 5  # leading ~ (my location) + 5 stops max

    def test_nav_url_skips_stops_without_coords_and_none_when_empty(self):
        items = [_item(40, "400", lat=None, lng=None)]
        assert route_card.build_multi_stop_nav_url(items) is None


@pytest.mark.unit
def test_format_local_time_shape():
    assert re.fullmatch(r"\d{2}:\d{2}", format_local_time())
