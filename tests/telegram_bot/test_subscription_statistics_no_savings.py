"""The bot's subscription statistics screen no longer prints a "Total Savings" line.

Subscriptions give no discount, so that line was 0 for everyone and implied a saving that does not
exist. Driven through the real dispatcher with the real api_client path (`/subscriptions/statistics`).
"""

import pytest

from tests.telegram_bot.ptb_harness import build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STATS = "/api/v1/subscriptions/statistics"

TRANSLATIONS = {
    "telegram.subscription.statistics": "STATS-TITLE",
    "telegram.subscription.total_deliveries": "DELIVERIES",
    "telegram.subscription.total_spent": "SPENT",
    "telegram.subscription.average_order": "AVERAGE",
    "telegram.subscription.total_savings": "SAVINGS-LINE",
    "telegram.currency.uzs": "UZS",
    "telegram.back": "Back",
}


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    harness.backend.route(
        "GET",
        STATS,
        lambda _c: {
            "data": {
                "statistics": {
                    "total_deliveries": 4,
                    "total_spent": 400000,
                    "average_order_value": 100000,
                    # Even a non-zero value from the API must not become a line on the screen.
                    "total_savings": 31337,
                }
            }
        },
    )
    return harness


async def test_statistics_screen_shows_no_savings_line(bot):
    user = bot.updates()
    await bot.send(user.tap("subscription_statistics"))

    shown = [call.text for call in bot.telegram.shown if call.text]
    assert shown, "the statistics screen showed nothing"
    text = shown[-1]
    assert "STATS-TITLE" in text and "DELIVERIES" in text and "SPENT" in text
    assert "SAVINGS-LINE" not in text and "31337" not in text and "💚" not in text
    assert any(c.endpoint == STATS for c in bot.backend.calls)
