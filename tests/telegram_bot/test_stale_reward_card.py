"""Redeeming a reward from a card that has been sitting in the chat.

WHY THIS FILE EXISTS
--------------------
``redeem_reward`` stores the tapped ``reward_id`` and pops a modal saying the
reward is applied. It checks nothing at all.

A rewards card stays tappable forever, so by the time it is tapped the reward may
be one the customer can no longer have: they spent the coins elsewhere, an admin
deactivated it, it expired, or ``max_uses_per_user`` was reached. The bot tells
them it is applied anyway, stores it, and the customer finds out at the very last
step — when order creation refuses the reward they were promised.

``checkout_choose_reward`` in the same class already asks exactly the right
question of exactly the right payload (``can_redeem``), because it has to decide
which rewards to make tappable. This screen just never asked it.
"""

import pytest

from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID, build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


REWARDS = "/api/v1/loyalty/rewards"
POINTS = "/api/v1/loyalty/points"

TRANSLATIONS = {
    "telegram.loyalty.reward_selected": "REWARD-APPLIED",
    "telegram.loyalty.reward_not_available": "REWARD-NO-LONGER-AVAILABLE",
}


def _harness(monkeypatch, *, can_redeem: bool):
    async def build():
        harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
        harness.backend.route(
            "GET", REWARDS,
            lambda _c: {"data": {
                "rewards": [{
                    "id": 4,
                    "name": "Free 19L bottle",
                    "points_required": 500,
                    "can_redeem": can_redeem,
                    "reward_type": "free_product",
                }],
                "user_points_balance": 500 if can_redeem else 10,
            }},
        )
        harness.backend.route(
            "GET", POINTS,
            lambda _c: {"data": {"current_balance": 500 if can_redeem else 10}},
        )
        return harness
    return build


@pytest.fixture
async def bot(monkeypatch):
    return await _harness(monkeypatch, can_redeem=False)()


@pytest.fixture
async def bot_with_affordable_reward(monkeypatch):
    return await _harness(monkeypatch, can_redeem=True)()


def toasts(bot):
    return [
        c.params.get("text")
        for c in bot.telegram.of("answerCallbackQuery")
        if c.params.get("text")
    ]


def selected(bot):
    return bot.application.user_data[DEFAULT_USER_ID].get("selected_reward_id")


async def test_a_reward_the_customer_cannot_have_is_not_confirmed(bot):
    """Being told a reward is applied, and then refused at order creation, is
    worse than being told now."""
    await bot.send(bot.updates().tap("redeem_4"))

    assert not any("REWARD-APPLIED" in t for t in toasts(bot)), (
        f"the bot promised a reward the customer cannot redeem: {toasts(bot)}"
    )


async def test_a_reward_the_customer_cannot_have_is_not_stored(bot):
    """Storing it means order creation carries it all the way to the refusal."""
    await bot.send(bot.updates().tap("redeem_4"))

    assert selected(bot) != 4, (
        "an unredeemable reward was stored for checkout to trip over"
    )


async def test_an_affordable_reward_is_still_applied(bot_with_affordable_reward):
    """The guard must only refuse what the backend would refuse."""
    bot = bot_with_affordable_reward
    await bot.send(bot.updates().tap("redeem_4"))

    assert selected(bot) == 4, (
        f"a perfectly redeemable reward was refused: {toasts(bot)}"
    )
