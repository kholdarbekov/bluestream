"""SSOT builder for driver-facing order offers (route-UX Plan 3, Task 10).

Before this module, the offer's text + keyboard were built independently in
THREE places: the live webhook path (`pool_insertion_suggestion_handler`),
the deferred-drain path (`flow_state.clear_and_drain`), and -- as of Plan 1
-- a third shape, the diversion offer ("go here first instead of #X?").
`staff_bot.utils.offers.build_offer` is now the single place that decides
which of the two shapes to render, keyed on whether the backend published a
`gain_minutes`/`committed_order_number` pair. This file pins that decision
and the copy it produces; `test_pool_insertion_offer_routing.py` and
`test_flow_state_offer_drain.py` pin that the two call sites actually route
through it (no second construction left behind).

Fix note pinned here (past bug in this exact copy): an earlier version
showed the driver "+12 min" for an offer that SAVES 12 minutes -- the sign
was inverted. `test_diversion_gain_is_rendered_as_savings_not_a_penalty`
guards the fix: `gain_minutes` is already published positive-means-saved by
`RouteOptimizationService.compute_diversion_gain`, so the builder must never
negate it.
"""
import importlib.util
from pathlib import Path

import pytest
from telegram import InlineKeyboardMarkup

from staff_bot.i18n import i18n
from staff_bot.utils import offers

# Route-card / offer copy is DB-backed (Task 2 seeded
# scripts/seed_staff_translations.py under category='staff_bot'), but this
# pure-render unit test never touches Postgres -- the shared `i18n` singleton
# starts with an empty `translations` dict here. Assertions that check the
# actual product copy therefore need real values fed in, resolved through
# `_curated_value` -- the SAME function `seed_translations()` calls to decide
# what actually gets written -- so a copy edit there can never leave this
# test happily asserting stale text (CLAUDE.md: never re-implement production
# logic locally). Same technique as test_route_card_views.py /
# test_route_alert_cap.py.
_SEED_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_staff_translations", _SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SEED_MODULE = _load_seed_module()

_OFFER_KEYS = [
    "staff.delivery.pool_insertion_offer",
    "staff.delivery.accept",
    "staff.delivery.suggestion_declined_button",
    "staff.route.diversion_offer",
    "staff.route.go_here_first",
    "staff.route.keep_current",
]


@pytest.fixture(autouse=True)
def _seed_offer_translations(monkeypatch):
    """Feed the real English (+ Uzbek/Russian) offer copy -- resolved live
    from the seed script -- into the i18n singleton for this file only.
    `monkeypatch.setitem` reverts each language entry after every test."""
    for lang in ("en", "uz", "ru"):
        resolved = {}
        for key in _OFFER_KEYS:
            value = _SEED_MODULE._curated_value(key, lang)
            assert value, f"{key} has no curated {lang} value in seed_staff_translations.py"
            resolved[key] = value
        merged = {**i18n.translations.get(lang, {}), **resolved}
        monkeypatch.setitem(i18n.translations, lang, merged)


def _buttons(kb: InlineKeyboardMarkup):
    return [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]


class TestOfferBuilder:
    def test_plain_pool_offer_shape(self):
        text, kb = offers.build_offer(
            {"delivery_id": 9, "order_no": "1055", "detour_km": 2.1, "detour_minutes": 8},
            "en",
        )
        assert "1055" in text
        cbs = [cb for _, cb in _buttons(kb)]
        assert "staff_confirm_accept_9" in cbs
        assert any(cb and cb.startswith("staff_decline_suggestion") for cb in cbs)

    def test_diversion_offer_used_when_gain_present(self):
        text, kb = offers.build_offer(
            {
                "delivery_id": 9, "order_no": "1055", "detour_km": 0.4,
                "detour_minutes": 9, "gain_minutes": 9.0,
                "committed_order_number": "1042",
            },
            "en",
        )
        assert "1055" in text and "1042" in text
        cbs = [cb for _, cb in _buttons(kb)]
        assert "staff_confirm_accept_9" in cbs
        assert any(cb and cb.startswith("staff_decline_suggestion") for cb in cbs)

    def test_gain_none_falls_back_to_plain_offer(self):
        plain, _ = offers.build_offer(
            {"delivery_id": 9, "order_no": "1055", "detour_km": 2.1, "detour_minutes": 8}, "en"
        )
        nulled, _ = offers.build_offer(
            {
                "delivery_id": 9, "order_no": "1055", "detour_km": 2.1,
                "detour_minutes": 8, "gain_minutes": None,
                "committed_order_number": None,
            },
            "en",
        )
        assert plain == nulled

    def test_diversion_requires_committed_order_number(self):
        """gain without a committed order is not a diversion — render plain."""
        plain, _ = offers.build_offer(
            {"delivery_id": 9, "order_no": "1055", "detour_km": 2.1, "detour_minutes": 8}, "en"
        )
        text, _ = offers.build_offer(
            {
                "delivery_id": 9, "order_no": "1055", "detour_km": 2.1,
                "detour_minutes": 8, "gain_minutes": 9.0,
                "committed_order_number": None,
            },
            "en",
        )
        assert text == plain


class TestGainMinutesSign:
    """Pins the past +N/-N sign-inversion bug in this exact copy."""

    def test_diversion_gain_is_rendered_as_savings_not_a_penalty(self):
        text, _ = offers.build_offer(
            {
                "delivery_id": 9, "order_no": "1055", "detour_km": 0.4,
                "detour_minutes": 9, "gain_minutes": 12.0,
                "committed_order_number": "1042",
            },
            "en",
        )
        assert "saves ~12 min" in text
        assert "+12" not in text
        assert "-12" not in text

    def test_diversion_gain_is_rounded_not_truncated(self):
        text, _ = offers.build_offer(
            {
                "delivery_id": 9, "order_no": "1055", "detour_km": 0.4,
                "detour_minutes": 9, "gain_minutes": 12.6,
                "committed_order_number": "1042",
            },
            "en",
        )
        # Assert the whole rendered fragment, not a bare "13": a substring
        # check passes if 13 shows up anywhere (an order number, a detour),
        # so it would survive the gain being dropped from the copy entirely.
        assert "saves ~13 min" in text


class TestOfferButtonsUseSeededCopy:
    def test_plain_offer_buttons_use_accept_and_declined_labels(self):
        _, kb = offers.build_offer(
            {"delivery_id": 9, "order_no": "1055", "detour_km": 2.1, "detour_minutes": 8}, "en"
        )
        labels = [text for text, _ in _buttons(kb)]
        assert any(i18n.get("staff.delivery.accept", "en") in label for label in labels)
        assert any(
            i18n.get("staff.delivery.suggestion_declined_button", "en") in label
            for label in labels
        )

    def test_diversion_offer_buttons_use_go_here_first_and_keep_current(self):
        _, kb = offers.build_offer(
            {
                "delivery_id": 9, "order_no": "1055", "detour_km": 0.4,
                "detour_minutes": 9, "gain_minutes": 9.0,
                "committed_order_number": "1042",
            },
            "en",
        )
        labels = [text for text, _ in _buttons(kb)]
        assert any(i18n.get("staff.route.go_here_first", "en") in label for label in labels)
        assert any(i18n.get("staff.route.keep_current", "en") in label for label in labels)


class TestIsDiversionOfferIsTheSSOTPredicate:
    """`is_diversion_offer` is exposed (not `_`-prefixed) precisely so
    webhook_server.py's `disable_notification` decision reuses the SAME
    predicate `build_offer` uses to pick the copy -- one decision, not two.
    """

    @pytest.mark.parametrize(
        "payload,expected",
        [
            ({"gain_minutes": 9.0, "committed_order_number": "1042"}, True),
            ({"gain_minutes": None, "committed_order_number": "1042"}, False),
            ({"gain_minutes": 9.0, "committed_order_number": None}, False),
            ({"gain_minutes": 9.0, "committed_order_number": ""}, False),
            ({}, False),
        ],
    )
    def test_matches_build_offer_shape_choice(self, payload, expected):
        assert offers.is_diversion_offer(payload) is expected
