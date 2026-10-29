"""B3 — a case-B customer must be able to pay the debt we deliberately kept payable.

POLICY 2026-08-24, case B: the customer took delivery and did not pay the
driver. The order stays on the Click rail, its payment stays PENDING and its
link stays PAYABLE by design — the money can still arrive and the fiscal receipt
still has to be issued.

The customer bot could not express that. `OrderKeyboards.order_details` rendered
the Pay button only for `order_status == 'pending'`, and `retry_payment` ran no
payability check at all. So the one population the policy exists to serve — a
delivered, unpaid, still-payable order — was shown no way to pay, while a
CANCELLED order's stale Pay button still minted a Click link that the PREPARE
guard then had to refuse with -9.

Both halves are now the backend's published `customer_actions.pay` (spec
2026-10-27 §4.1-§4.3): the button is drawn from it and the tap goes to
`retry-payment`, which enforces the same function. The backend is replayed from
the shared fixtures (tests/fixtures/customer_order_actions/), and everything is
driven through the REAL PTB dispatcher: the keyboard the customer saw and the
backend call the tap produced are what is asserted, never a mock having been called.
"""

from __future__ import annotations

import pathlib

import pytest

from tests.telegram_bot.test_order_customer_actions_dispatcher import (
    Replay,
    _bot,
    _copy,
    actions,
    calls,
    callbacks,
    exchange,
    labels,
    pay_cb,
    tap,
    url_buttons,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

CASE_B = "delivered_unpaid_click"


class TestCaseBIsOfferedAPayButtonThatWorks:
    async def test_delivered_unpaid_click_order_shows_the_pay_button(self, monkeypatch):
        bot = await _bot(monkeypatch, CASE_B)
        Replay(bot, CASE_B)

        await tap(bot, "order_1")

        pay = actions(CASE_B)["pay"]
        assert pay_cb(bot) == f"payment_retry_1_{pay['expected_amount']}", (
            "case B is payable BY DESIGN; without a Pay button the customer "
            "cannot settle a debt we deliberately kept payable"
        )
        assert labels(bot)[0] == pay["label"]

    async def test_tapping_that_button_asks_the_one_pay_route_for_a_link(self, monkeypatch):
        """The whole journey, tapping the button the bot actually drew.

        The callback data is READ OFF the rendered keyboard rather than
        hard-coded, so this cannot pass while the screen offers nothing to tap.
        The answer is the backend's own recorded one for this order (the web
        exchange: the link is the same whichever channel asked).
        """
        bot = await _bot(monkeypatch, CASE_B)
        Replay(bot, CASE_B)
        recorded = exchange(CASE_B, "pay_web")
        bot.backend.route(
            "POST", "/api/v1/orders/1/retry-payment", lambda _call: recorded["response"],
        )

        await tap(bot, "order_1")
        await tap(bot, pay_cb(bot))

        (paid,) = calls(bot, "POST", "/api/v1/orders/1/retry-payment")
        assert paid.data == {"channel": "bot", "expected_amount": actions(CASE_B)["pay"]["expected_amount"]}
        assert not calls(bot, "POST", "/api/v1/payments/create")
        assert url_buttons(bot) == [recorded["response"]["data"]["payment_url"]], (
            "the customer must end up looking at a button that opens the Click link"
        )

    async def test_a_delivered_order_still_offers_no_self_cancel(self, monkeypatch):
        """Widening payability must NOT widen self-cancel.

        Self-cancel is the backend's published `customer_actions.cancel`,
        refused for a delivered order (F5), and the bot draws Cancel from it alone.
        """
        bot = await _bot(monkeypatch, CASE_B)
        Replay(bot, CASE_B)

        await tap(bot, "order_1")

        drawn = "cancel_order_1" in callbacks(bot)
        assert drawn is actions(CASE_B)["cancel"]["allowed"] is False


class TestADeadOrderIsNotOfferedAPayButton:
    async def test_cancelled_order_shows_no_pay_button(self, monkeypatch):
        bot = await _bot(monkeypatch, "cancelled")
        Replay(bot, "cancelled")

        await tap(bot, "order_1")

        assert not [data for data in callbacks(bot) if data.startswith("payment_")]

    async def test_a_stale_pay_tap_on_a_cancelled_order_mints_no_link(self, monkeypatch):
        """The button is gone, but the message carrying it survives in the chat."""
        bot = await _bot(monkeypatch, "cancelled")
        Replay(bot, "cancelled")

        await tap(bot, "payment_retry_1")

        assert not calls(bot, "POST", "/api/v1/orders/1/retry-payment")
        assert not calls(bot, "POST", "/api/v1/payments/create")
        assert url_buttons(bot) == [], (
            "an ungated retry mints a Click link the PREPARE guard then refuses "
            "with -9 — an avoidable customer-visible failure"
        )


# ---------------------------------------------------------------------------
# Fix round 1 — the widening moved a population past a door; the buttons on the
# other side of that door had not been re-audited.
# ---------------------------------------------------------------------------


class TestTheRecoveryScreenObeysTheSameSplit:
    """I1 — Pay and Cancel were split in `order_details`; `payment_failed` too.

    The recovery screen (a stale `payment_cancel_<id>`, or a checkout answer with
    no link) draws the same published pair `order_details` draws: a DELIVERED
    order reaches it with Pay and without Cancel, and a stale tap on a Cancel
    gets the coded refusal in the customer's language.
    """

    async def test_a_case_b_recovery_screen_offers_pay_and_no_self_cancel(self, monkeypatch):
        bot = await _bot(monkeypatch, CASE_B)
        Replay(bot, CASE_B)

        await tap(bot, "payment_cancel_1")

        pay = actions(CASE_B)["pay"]
        assert callbacks(bot) == [f"payment_retry_1_{pay['expected_amount']}", "menu_orders"], (
            "the recovery screen draws Pay and Cancel from the order's published answers, "
            "and must still answer 'does my order exist?'"
        )

    async def test_a_pending_order_recovery_screen_keeps_self_cancel(self, monkeypatch):
        bot = await _bot(monkeypatch, "click_unpaid")
        Replay(bot, "click_unpaid")

        await tap(bot, "payment_cancel_1")

        a = actions("click_unpaid")
        assert labels(bot) == [_copy("telegram.payment.retry"), _copy("telegram.menu.orders"), a["cancel"]["label"]]
        assert callbacks(bot) == [f"payment_retry_1_{a['pay']['expected_amount']}", "menu_orders", "cancel_order_1"]


class TestSwitchMethodIsGoneFromTheRecoveryScreen:
    """I2 — a button may not be payability-gated while its action points elsewhere.

    `payment_switch_{id}` parses the order id, logs it, and then renders
    `OrderKeyboards.payment_methods`, whose callbacks (`payment_cash` /
    `payment_card`) route to `orders.payment_handler` ->
    `_show_order_confirmation` — the CART checkout screen. The id is dropped and
    the existing order's rail is never touched; worse, the cart still holds the
    basket, so Confirm there places a SECOND order — the exact double-order the
    "your order is placed" copy on this screen exists to prevent.
    """

    async def test_the_recovery_screen_no_longer_offers_switch_method(self, monkeypatch):
        bot = await _bot(monkeypatch, CASE_B)
        Replay(bot, CASE_B)

        await tap(bot, "payment_cancel_1")

        assert "payment_switch_1" not in callbacks(bot)

    async def test_nothing_in_the_bot_renders_a_payment_switch_button(self):
        """No customer moves a rail from a recovery screen (spec 2026-10-27 §4.3):
        Switch-method was a second, broken expression of Pay."""
        bot_dir = pathlib.Path(__file__).resolve().parents[2] / "telegram_bot"
        renders = [
            f"{path}:{n}"
            for path in bot_dir.rglob("*.py")
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if "payment_switch_" in line and "callback_data" in line
        ]
        assert renders == [], f"a payment_switch_ button is rendered again at {renders}"


class TestAStalePaymentSwitchTapIsHarmless:
    """NEW-1 — the BUTTON is gone; the MESSAGES carrying it are permanent.

    Removing `payment_switch_{id}` from `payment_failed` stops the bot DRAWING
    it. It does nothing about every recovery screen already sitting in a
    customer's chat, and `bot.py` still routes `^payment_switch_` to a handler
    that once dropped the order id and rendered the CART confirmation — where
    Confirm places a SECOND order for the same basket, because an unpaid order
    deliberately keeps its cart.

    Driven by tapping the callback DIRECTLY — a stale message is precisely the
    case a test that reads its taps off a freshly rendered keyboard cannot see.
    """

    async def test_it_never_reaches_the_cart_confirmation(self, monkeypatch):
        bot = await _bot(monkeypatch, CASE_B)
        Replay(bot, CASE_B)

        await tap(bot, "payment_switch_1")

        drawn = callbacks(bot)
        # The id-less picker IS the doorway: `payment_cash` / `payment_card`
        # route to `orders.payment_handler` -> `_show_order_confirmation`.
        assert "payment_cash" not in drawn and "payment_card" not in drawn, (
            "the stale Switch-method tap re-opened the id-less rail picker, whose "
            "buttons lead to the CART confirmation — Confirm there buys the same "
            "basket a second time"
        )
        assert "confirm_order" not in drawn
        assert not calls(bot, "POST", "/api/v1/orders"), (
            "no order may be created by a tap on a stale payment button"
        )

    async def test_it_opens_the_order_with_its_published_pay_and_writes_nothing(self, monkeypatch):
        """Honest answer, not a dead end: the order, whose Pay carries the amount the tap lacked."""
        bot = await _bot(monkeypatch, CASE_B)
        Replay(bot, CASE_B)

        await tap(bot, "payment_switch_1")

        assert not calls(bot, "POST", "/api/v1/orders/1/retry-payment")
        assert pay_cb(bot) == f"payment_retry_1_{actions(CASE_B)['pay']['expected_amount']}"

    async def test_a_stale_tap_on_a_dead_order_mints_nothing(self, monkeypatch):
        bot = await _bot(monkeypatch, "cancelled")
        Replay(bot, "cancelled")

        await tap(bot, "payment_switch_1")

        assert not calls(bot, "POST", "/api/v1/orders/1/retry-payment")
        assert not calls(bot, "POST", "/api/v1/orders")
        assert "confirm_order" not in callbacks(bot)

    async def test_the_callback_is_still_claimed_by_a_handler(self, monkeypatch):
        """Keep the REGISTRATION. An unclaimed callback is a spinner nobody can
        stop — the rule recorded at `keyboards.py`'s tap-feedback note."""
        bot = await _bot(monkeypatch)

        update = bot.user.tap("payment_switch_1")
        assert bot.handlers_matching(update), "payment_switch_ must stay claimed"


# ---------------------------------------------------------------------------
# The pay message quotes the money the link will charge.
# ---------------------------------------------------------------------------


class TestThePayMessageQuotesTheChargedAmount:
    """The message under the link names the order and the amount `retry-payment`
    answered the link charges (`data.payment.amount`), in seeded copy."""

    async def test_the_message_names_the_order_and_the_payment_amount(self, monkeypatch):
        from utils import format_price

        bot = await _bot(monkeypatch, "click_unpaid")
        Replay(bot, "click_unpaid")
        recorded = exchange("click_unpaid", "pay_bot")

        await tap(bot, "order_1")
        await tap(bot, pay_cb(bot))

        (link,) = [call for call in bot.telegram.shown if url_buttons_of(call)]
        assert link.text == _copy("telegram.payment.pay_message").format(
            order_number="TG_000001_26",
            amount=format_price(recorded["response"]["data"]["payment"]["amount"]),
        )


def url_buttons_of(call) -> list[str]:
    return [
        button["url"]
        for row in call.reply_markup.get("inline_keyboard") or []
        for button in row
        if "url" in button
    ]
