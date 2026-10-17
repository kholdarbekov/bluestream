"""The reminder's Order-again button lands where the reminder promises (spec §7.3).

The buttons come from the REAL `ReorderReminderKeyboards.reminder` (the builder the
/internal/reorder-reminder route sends) and are tapped through the real dispatcher built by
`WaterBusinessBot._setup_handlers()`. `reorder_<id>` reopens that order through the existing
Quick Order flow (cart refilled, nothing placed on the tap); `menu_products` opens the
catalogue. The Later / Stop taps are wired in Task 16b
(tests/telegram_bot/test_reorder_reminder_callbacks.py).
"""

from __future__ import annotations

import pytest

import bot as bot_module  # noqa: F401
from handlers import product_handlers, quick_order_handlers
from keyboards import ReorderReminderKeyboards

from tests.telegram_bot.ptb_harness import build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
async def bot(monkeypatch):
    return await build_bot_harness(monkeypatch)


@pytest.fixture
def user(bot):
    return bot.updates()


def _callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


async def test_r1_offers_order_later_and_stop_and_r2_drops_later():
    r1 = ReorderReminderKeyboards.reminder("uz", episode_id=77, reorder_order_id=9001, show_later=True, snooze_days=14)
    r2 = ReorderReminderKeyboards.reminder("uz", episode_id=77, reorder_order_id=9001, show_later=False, snooze_days=14)

    assert _callbacks(r1) == ["reorder_9001", "rr_later_77", "rr_stop_77"]
    assert _callbacks(r2) == ["reorder_9001", "rr_stop_77"]


async def test_order_again_reopens_the_recipients_own_order(bot, user):
    order_again = _callbacks(
        ReorderReminderKeyboards.reminder("uz", episode_id=77, reorder_order_id=9001, show_later=True, snooze_days=14)
    )[0]
    claimed = [handler.callback for _group, handler in bot.handlers_matching(user.tap(order_again))]
    assert quick_order_handlers.handle_reorder_from_history in claimed

    await bot.send(user.tap(order_again))

    fetched = [c for c in bot.backend.calls if c.method == "GET" and c.endpoint == "/api/v1/orders/9001"]
    assert len(fetched) == 1
    assert fetched[0].user_token == "test-access-token"


async def test_without_an_own_order_the_button_opens_the_catalogue(bot, user):
    order_again = _callbacks(
        ReorderReminderKeyboards.reminder("uz", episode_id=77, reorder_order_id=None, show_later=False, snooze_days=14)
    )[0]

    assert order_again == "menu_products"
    claimed = [handler.callback for _group, handler in bot.handlers_matching(user.tap(order_again))]
    assert product_handlers.products_menu in claimed


async def test_every_callback_fits_telegrams_64_byte_limit():
    big = 2**31 - 1
    markup = ReorderReminderKeyboards.reminder(
        "ru", episode_id=big, reorder_order_id=big, show_later=True, snooze_days=14
    )

    for data in _callbacks(markup):
        assert len(data.encode("utf-8")) <= 64, data
