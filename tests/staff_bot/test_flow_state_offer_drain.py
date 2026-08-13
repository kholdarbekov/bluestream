"""`flow_state.clear_and_drain` is the THIRD place that used to construct the
offer's text/keyboard by hand. It must now route through the same
`staff_bot.utils.offers.build_offer` the live webhook path uses, and — being
deferred, therefore non-urgent by definition — every drained send must be
silent (`disable_notification=True`), even for a diversion-shaped payload
that would have pinged if sent live (route-UX Plan 3, Task 10 brief).
"""
import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.i18n import i18n
from staff_bot.utils import flow_state

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
    resolved = {}
    for key in _OFFER_KEYS:
        value = _SEED_MODULE._curated_value(key, "en")
        assert value, f"{key} has no curated en value in seed_staff_translations.py"
        resolved[key] = value
    merged = {**i18n.translations.get("en", {}), **resolved}
    monkeypatch.setitem(i18n.translations, "en", merged)


PLAIN_PAYLOAD = {
    "delivery_id": 9, "order_no": "1055", "detour_km": 2.1, "detour_minutes": 8,
}
DIVERSION_PAYLOAD = {
    "delivery_id": 10, "order_no": "1056", "detour_km": 0.4, "detour_minutes": 9,
    "gain_minutes": 9.0, "committed_order_number": "1042",
}


@pytest.mark.unit
class TestDrainRoutesThroughTheSharedBuilder:
    def test_drain_renders_both_shapes_and_stays_silent(self, monkeypatch):
        monkeypatch.setattr(flow_state, "clear_active", AsyncMock())
        monkeypatch.setattr(
            flow_state, "drain_pool_suggestions",
            AsyncMock(return_value=[PLAIN_PAYLOAD, DIVERSION_PAYLOAD]),
        )
        bot = MagicMock()
        bot.send_message = AsyncMock()

        asyncio.run(flow_state.clear_and_drain(777, bot, language="en"))

        assert bot.send_message.await_count == 2
        for call in bot.send_message.await_args_list:
            assert call.kwargs["disable_notification"] is True

        first_text = bot.send_message.await_args_list[0].kwargs["text"]
        second_text = bot.send_message.await_args_list[1].kwargs["text"]
        assert "1055" in first_text
        assert "1056" in second_text and "1042" in second_text

    def test_drained_diversion_keyboard_uses_go_here_first_copy(self, monkeypatch):
        monkeypatch.setattr(flow_state, "clear_active", AsyncMock())
        monkeypatch.setattr(
            flow_state, "drain_pool_suggestions",
            AsyncMock(return_value=[DIVERSION_PAYLOAD]),
        )
        bot = MagicMock()
        bot.send_message = AsyncMock()

        asyncio.run(flow_state.clear_and_drain(777, bot, language="en"))

        kwargs = bot.send_message.await_args.kwargs
        labels = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
        assert any(i18n.get("staff.route.go_here_first", "en") in label for label in labels)
