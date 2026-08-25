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
    "staff.route.refresh",
    "staff.route.refreshed_toast",
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
        assert "staff_optimize_routes" in callbacks           # Optimize route
        assert "staff_share_location_prompt" not in callbacks  # the reveal step is gone
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
        # Assert against the BODY, not the whole card: the header carries an
        # `updated HH:MM` stamp, so a bare "12" substring check against the
        # full text fails for a solid hour every day (and any other 2-digit
        # value just moves which hour breaks).
        body = text.split("\n", 1)[1]
        assert "12" not in body and "4.2" not in body
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
        # All stops done: there is no route left to optimize, so the card
        # offers only the way back rather than a button that would no-op.
        assert "staff_optimize_routes" not in callbacks
        assert "staff_share_location_prompt" not in callbacks
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


@pytest.mark.unit
class TestRefreshAffordance:
    def test_all_three_views_offer_refresh(self):
        empty = {"items": [], "location_status": "missing", "route_summary": {}}
        for text, kb in (
            route_card.build_next_view(_payload([_item(1, "1")]), "en"),
            route_card.build_all_view(_payload([_item(1, "1")]), "en"),
            route_card.build_empty_view(empty, "en"),
        ):
            callbacks = [c for _, c, _ in _buttons(kb) if c]
            assert "staff_route_refresh" in callbacks

    def test_empty_view_offers_the_pool_and_keeps_back(self):
        empty = {"items": [], "location_status": "missing", "route_summary": {}}
        _, kb = route_card.build_empty_view(empty, "en")
        callbacks = [c for _, c, _ in _buttons(kb) if c]
        assert "staff_new_orders_unified" in callbacks
        assert "staff_back_to_main" in callbacks
        # Still no optimize button: there is no route left to optimize.
        assert "staff_optimize_routes" not in callbacks


@pytest.mark.unit
class TestAllViewStopDetail:
    """The all-stops list must name the customer and the goods (2026-08-25).

    A driver reading the hub used to see only `#1042  Chilanzar` — enough to
    tap, not enough to plan (which stop is the 3-bottle drop, which is the
    regular). Both fields already ride in the SAME payload the card renders,
    so this is a render change with no new API surface.
    """

    def test_each_stop_names_the_customer_and_lists_items(self):
        items = [_item(11, "1042"), _item(12, "1043", district="Yunusobod")]
        items[1]["customer_name"] = "Aziza"
        items[1]["items"] = [
            {"product_name": "19 litrlik suv", "quantity": 2},
            {"product_name": "Stakan", "quantity": 1},
        ]
        text, _ = route_card.build_all_view(_payload(items), "en")

        # Name and items are SEPARATE lines under the stop row (chosen layout),
        # and the items line packs a multi-item order onto one comma-joined line.
        assert "1. #1042  Chilanzar" in text
        assert "👤 Umar" in text
        assert "📦 19 litrlik suv ×3" in text
        assert "👤 Aziza" in text
        assert "📦 19 litrlik suv ×2, Stakan ×1" in text

        lines = [ln.strip() for ln in text.splitlines()]
        i = lines.index("👤 Aziza")
        assert lines[i - 1].endswith("2. #1043  Yunusobod")
        assert lines[i + 1] == "📦 19 litrlik suv ×2, Stakan ×1"

    def test_missing_name_or_items_skips_only_that_line(self):
        items = [_item(11, "1042"), _item(12, "1043")]
        items[0]["customer_name"] = ""
        items[1]["items"] = []
        text, _ = route_card.build_all_view(_payload(items), "en")

        assert text.count("👤") == 1  # only the second stop has a name
        assert text.count("📦") == 1  # only the first stop has items
        # Both stop rows survive regardless.
        assert "1. #1042" in text and "2. #1043" in text

    def test_customer_name_and_product_names_are_html_escaped(self):
        items = [_item(11, "1042")]
        items[0]["customer_name"] = "A & <b>B</b>"
        items[0]["items"] = [{"product_name": "5 L <tag>", "quantity": 1}]
        text, _ = route_card.build_all_view(_payload(items), "en")

        assert "A &amp; &lt;b&gt;B&lt;/b&gt;" in text
        assert "5 L &lt;tag&gt;" in text
        assert "<b>B</b>" not in text

    def test_quantities_use_format_quantity_not_int(self):
        """A 1.5-unit line must read ×1.5, and 3.0 must read ×3 — the same
        rule the detail card uses (int() would truncate 1.5 to 1)."""
        items = [_item(11, "1042")]
        items[0]["items"] = [
            {"product_name": "Katta suv", "quantity": 1.5},
            {"product_name": "Kichik suv", "quantity": 3.0},
        ]
        text, _ = route_card.build_all_view(_payload(items), "en")
        assert "📦 Katta suv ×1.5, Kichik suv ×3" in text

    def test_long_route_stays_under_the_telegram_limit(self):
        """40 stops × 3 lines would blow past 4096 and Telegram would reject
        the WHOLE card, leaving the driver with a stale message."""
        items = []
        for n in range(40):
            it = _item(100 + n, f"TG_00{n:04d}_26", district=f"Massiv nomi {n}")
            it["customer_name"] = f"Mijoz Ismi Familiyasi {n}"
            it["items"] = [
                {"product_name": "19 litrlik tozalangan suv", "quantity": 3},
                {"product_name": "Bir martalik stakan to'plami", "quantity": 2},
            ]
            items.append(it)
        text, kb = route_card.build_all_view(_payload(items), "en")

        assert len(text) <= 4096
        # EVERY stop still has its row and its tap target — only the extra
        # detail lines are dropped.
        for n in range(40):
            assert f"#TG_00{n:04d}_26" in text
        btns = _buttons(kb)
        for n in range(40):
            assert any(c == f"staff_view_active_{100 + n}" for _, c, _ in btns)

    def test_detail_drops_from_the_tail_as_a_contiguous_prefix(self):
        """Detail must survive on a leading run of stops and stop there — no
        interleaving where stop 30 gets detail because it happened to be
        shorter than stop 12."""
        items = []
        for n in range(40):
            it = _item(100 + n, f"TG_00{n:04d}_26", district=f"Massiv nomi {n}")
            it["customer_name"] = f"Mijoz Ismi Familiyasi {n}"
            it["items"] = [{"product_name": "19 litrlik tozalangan suv", "quantity": 3}]
            items.append(it)
        text, _ = route_card.build_all_view(_payload(items), "en")

        detailed = [n for n in range(40) if f"Mijoz Ismi Familiyasi {n}" in text]
        assert detailed, "at least the first stops must keep their detail"
        assert detailed == list(range(len(detailed))), "detail must be a leading run"
        assert len(detailed) < 40, "this fixture is meant to overflow"

    def test_short_route_keeps_detail_on_every_stop(self):
        items = [_item(10 + n, f"104{n}") for n in range(6)]
        for n, it in enumerate(items):
            it["customer_name"] = f"Mijoz {n}"
        text, _ = route_card.build_all_view(_payload(items), "en")
        for n in range(6):
            assert f"👤 Mijoz {n}" in text
