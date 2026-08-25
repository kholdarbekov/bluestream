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

Both halves are now the backend's published `payment_info.is_payable`
(`order_is_payable_online`, the single authority). Driven through the REAL PTB
dispatcher: the keyboard the customer saw and the backend call the tap produced
are what is asserted, never a mock having been called.
"""

from __future__ import annotations

import copy
import time

import pytest

from tests.telegram_bot.ptb_harness import build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ORDER_ID = 1125
CLICK_URL = "https://my.click.uz/services/pay?id=1&t=CASE-B"


def _order(*, status, is_paid, payment_method, payment_status, is_payable):
    """What GET /api/v1/orders/<id> really returns for this order."""
    return {
        "id": ORDER_ID,
        "order_number": "TG_001125_26",
        "status": status,
        "is_paid": is_paid,
        "payment_method": payment_method,
        "total_amount": 18000,
        "order_items": [],
        "payment_info": {
            "id": 1229,
            "payment_method": payment_method,
            "payment_status": payment_status,
            "payment_provider": payment_method,
            "amount": 18000,
            "amount_collected": 0,
            "outstanding_amount": 18000,
            "payment_link": CLICK_URL,
            "is_payable": is_payable,
            "payable_payment_link": CLICK_URL if is_payable else None,
        },
    }


CASE_B = _order(
    status="delivered",
    is_paid=False,
    payment_method="click",
    payment_status="pending",
    is_payable=True,
)

CANCELLED = _order(
    status="cancelled",
    is_paid=False,
    payment_method="click",
    payment_status="cancelled",
    is_payable=False,
)

PENDING_CASH = _order(
    status="pending",
    is_paid=False,
    payment_method="cash",
    payment_status="pending",
    # A cash order has no gateway link, so the authority says "not payable
    # ONLINE" — but the customer may still move it onto Click, and
    # POST /payments/create owns that flip and its marking-code pool guard.
    is_payable=False,
)


def _serve_order(harness, order):
    harness.backend.route(
        "GET",
        f"/api/v1/orders/{ORDER_ID}",
        lambda _call: {"data": {"order": copy.deepcopy(order), "delivery": None}},
    )
    harness.backend.route(
        "POST",
        "/api/v1/payments/create",
        lambda _call: {"data": {"payment_link": {"payment_url": CLICK_URL}}},
    )


def _callback_data(harness) -> list[str]:
    """Every inline button the customer can currently see."""
    data = []
    for call in harness.telegram.calls:
        for row in call.reply_markup.get("inline_keyboard", []) or []:
            for button in row:
                if button.get("callback_data"):
                    data.append(button["callback_data"])
    return data


def _expire_dedup():
    from handlers import callback_dedup

    stale = time.monotonic() - 1
    for key in list(callback_dedup._in_memory_locks):
        callback_dedup._in_memory_locks[key] = stale


class TestCaseBIsOfferedAPayButtonThatWorks:
    async def test_delivered_unpaid_click_order_shows_the_pay_button(self, monkeypatch):
        harness = await build_bot_harness(monkeypatch)
        _serve_order(harness, CASE_B)

        await harness.send(harness.updates().tap(f"order_{ORDER_ID}"))

        assert f"payment_retry_{ORDER_ID}" in _callback_data(harness), (
            "case B is payable BY DESIGN; without a Pay button the customer "
            "cannot settle a debt we deliberately kept payable"
        )

    async def test_tapping_that_button_really_produces_a_payable_link(self, monkeypatch):
        """The whole journey, tapping the button the bot actually drew.

        The callback data is READ OFF the rendered keyboard rather than
        hard-coded, so this cannot pass while the screen offers nothing to tap.
        """
        harness = await build_bot_harness(monkeypatch)
        _serve_order(harness, CASE_B)
        updates = harness.updates()

        await harness.send(updates.tap(f"order_{ORDER_ID}"))
        pay = [d for d in _callback_data(harness) if d.startswith("payment_retry_")]
        assert pay, "no Pay button was drawn, so there is nothing for the customer to tap"

        _expire_dedup()
        await harness.send(updates.tap(pay[0]))

        created = [c for c in harness.backend.calls if c.endpoint == "/api/v1/payments/create"]
        assert created, "the Pay button must actually mint a link for a case-B order"
        assert created[0].data["order_id"] == ORDER_ID
        assert any(CLICK_URL in str(call.reply_markup) for call in harness.telegram.calls), (
            "the customer must end up looking at a button that opens the Click link"
        )

    async def test_a_delivered_order_still_offers_no_self_cancel(self, monkeypatch):
        """Widening payability must NOT widen self-cancel.

        `OrderService.cancel_order` refuses DELIVERED/CANCELLED, so a Cancel
        button here is a button that can only fail.
        """
        harness = await build_bot_harness(monkeypatch)
        _serve_order(harness, CASE_B)

        await harness.send(harness.updates().tap(f"order_{ORDER_ID}"))

        assert f"cancel_order_{ORDER_ID}" not in _callback_data(harness)


class TestADeadOrderIsNotOfferedAPayButton:
    async def test_cancelled_order_shows_no_pay_button(self, monkeypatch):
        harness = await build_bot_harness(monkeypatch)
        _serve_order(harness, CANCELLED)

        await harness.send(harness.updates().tap(f"order_{ORDER_ID}"))

        assert f"payment_retry_{ORDER_ID}" not in _callback_data(harness)

    async def test_a_stale_pay_tap_on_a_cancelled_order_mints_no_link(self, monkeypatch):
        """The button is gone, but the message carrying it survives in the chat."""
        harness = await build_bot_harness(monkeypatch)
        _serve_order(harness, CANCELLED)

        await harness.send(harness.updates().tap(f"payment_retry_{ORDER_ID}"))

        created = [c for c in harness.backend.calls if c.endpoint == "/api/v1/payments/create"]
        assert not created, (
            "an ungated retry mints a Click link the PREPARE guard then refuses "
            "with -9 — an avoidable customer-visible failure"
        )


class TestTheCashFlipSurvives:
    """`is_payable` is False for a cash rail, and the widening must not narrow.

    A PENDING cash order has always been able to move onto Click from this
    screen (POST /payments/create re-points the rail, refusing only when the
    marking-code pool is short). Gating on `is_payable` ALONE would delete that.
    """

    async def test_pending_cash_order_keeps_its_pay_button(self, monkeypatch):
        harness = await build_bot_harness(monkeypatch)
        _serve_order(harness, PENDING_CASH)

        await harness.send(harness.updates().tap(f"order_{ORDER_ID}"))

        assert f"payment_retry_{ORDER_ID}" in _callback_data(harness)

    async def test_pending_cash_retry_still_asks_the_backend_to_flip_the_rail(self, monkeypatch):
        harness = await build_bot_harness(monkeypatch)
        _serve_order(harness, PENDING_CASH)

        await harness.send(harness.updates().tap(f"payment_retry_{ORDER_ID}"))

        created = [c for c in harness.backend.calls if c.endpoint == "/api/v1/payments/create"]
        assert created and created[0].data["payment_method"] == "click"


class TestTheRefusalCopyIsRealCopy:
    """The refusal `retry_payment` renders must be seeded WHERE THE BOT LOOKS.

    `telegram_bot/i18n.py:67` loads ONLY `category = 'telegram'`. A key seeded
    under any other category is invisible to the bot and renders as
    `humanised_missing_key` — English, to a customer who reads Uzbek, on the one
    screen whose job is to explain why a tap did nothing. The canonical seeder
    derives the category from the key's first segment, so seeding from
    `BACKEND_TRANSLATIONS` under a `telegram.*` key is what makes it loadable.
    """

    KEY = "telegram.payment.error_not_payable"

    def test_the_key_the_handler_renders_is_seeded_trilingually(self):
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        row = BACKEND_TRANSLATIONS.get(self.KEY)
        assert row is not None, f"{self.KEY} is rendered by retry_payment but never seeded"
        for language in ("en", "uz", "ru"):
            assert row.get(language), f"{self.KEY} has no {language} copy"

    def test_it_lands_in_the_only_category_the_customer_bot_loads(self):
        from scripts.seed_backend_translations import _category_for

        assert _category_for(self.KEY) == "telegram"

    def test_it_renders_through_the_real_translation_get(self):
        """No placeholders: the call site passes no values, and
        `render_translation` degrades a template it cannot fill to the
        humanised key — copy that looks right in the admin UI and is broken in
        Telegram."""
        from i18n import Translation
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        row = BACKEND_TRANSLATIONS[self.KEY]
        translator = Translation()
        translator.translations = {lang: {self.KEY: row[lang]} for lang in ("en", "uz", "ru")}

        for language in ("en", "uz", "ru"):
            rendered = translator.get(self.KEY, language)
            assert rendered == row[language]
            assert "{" not in rendered


# ---------------------------------------------------------------------------
# Fix round 1 — the widening moved a population past a door; the buttons on the
# other side of that door had not been re-audited.
# ---------------------------------------------------------------------------


def _failing_psp(harness):
    from tests.telegram_bot.ptb_harness import backend_failure

    harness.backend.route(
        "POST",
        "/api/v1/payments/create",
        lambda _call: backend_failure("Click gateway timeout", 502),
    )


class TestTheRecoveryScreenObeysTheSameSplit:
    """I1 — Pay and Cancel were split in `order_details`; `payment_failed` too.

    Before B3 a DELIVERED order could never reach this screen. It can now:
    `retry_payment` renders it on ANY create failure, and case B is exactly the
    population B3 newly routes here. `OrderService.cancel_order` refuses
    DELIVERED, so an unconditional Cancel button hands an Uzbek reader the
    backend's raw English "Order cannot be cancelled".
    """

    async def test_a_case_b_recovery_screen_offers_no_self_cancel(self, monkeypatch):
        harness = await build_bot_harness(monkeypatch)
        _serve_order(harness, CASE_B)
        _failing_psp(harness)

        await harness.send(harness.updates().tap(f"payment_retry_{ORDER_ID}"))

        assert f"cancel_order_{ORDER_ID}" not in _callback_data(harness), (
            "cancel_order refuses DELIVERED — this button can only fail, in English"
        )
        assert "menu_orders" in _callback_data(harness), (
            "the screen must still answer 'does my order exist?'"
        )

    async def test_a_pending_order_recovery_screen_keeps_self_cancel(self, monkeypatch):
        harness = await build_bot_harness(monkeypatch)
        _serve_order(harness, PENDING_CASH)
        _failing_psp(harness)

        await harness.send(harness.updates().tap(f"payment_retry_{ORDER_ID}"))

        assert f"cancel_order_{ORDER_ID}" in _callback_data(harness)


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
        harness = await build_bot_harness(monkeypatch)
        _serve_order(harness, CASE_B)
        _failing_psp(harness)

        await harness.send(harness.updates().tap(f"payment_retry_{ORDER_ID}"))

        assert f"payment_switch_{ORDER_ID}" not in _callback_data(harness)

    async def test_nothing_in_the_bot_renders_a_payment_switch_button(self):
        """The rail move a customer really has is Retry: POST /payments/create
        normalizes card->click and flips a pending cash order onto Click behind
        the pool guard. Switch-method was a second, broken expression of it."""
        import pathlib

        bot_dir = pathlib.Path(__file__).resolve().parents[2] / "telegram_bot"
        renders = [
            f"{path}:{n}"
            for path in bot_dir.rglob("*.py")
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if "payment_switch_" in line and "callback_data" in line
        ]
        assert renders == [], f"a payment_switch_ button is rendered again at {renders}"


class TestCustomerMayPayDisagreementCells:
    """M1/M4 — the function itself, over the cells where the two halves disagree.

    The dispatcher tests above prove the wiring. This proves the RULE, including
    the cell whose correctness lives three layers away in `create_payment`'s
    revival branch (a PENDING order whose payment the gateway CANCELLED is still
    payable, because create_payment resets the row to PENDING and mints a fresh
    link).
    """

    @staticmethod
    def _order(status, is_paid, is_payable, payment_status="pending"):
        return {
            "id": 1,
            "status": status,
            "is_paid": is_paid,
            "payment_info": {"is_payable": is_payable, "payment_status": payment_status},
        }

    @pytest.mark.parametrize(
        "status,is_paid,is_payable,payment_status,expected,why",
        [
            ("delivered", False, True, "pending", True, "case B: payable by design"),
            ("out_for_delivery", False, True, "pending", True, "live click order"),
            ("cancelled", False, False, "cancelled", False, "dead order"),
            ("returned", False, False, "cancelled", False, "dead order"),
            ("delivered", True, False, "completed", False, "settled at the door"),
            ("pending", False, False, "pending", True, "cash rail: the flip is still open"),
            ("pending", False, False, "cancelled", True,
             "create_payment REVIVES a gateway-cancelled row to PENDING and mints a fresh link"),
            ("pending", False, False, "failed", True, "same revival branch"),
            ("pending", True, False, "completed", False, "paid: never offer a second payment"),
            ("pending", False, False, "completed", False,
             "M4: a COMPLETED payment on a not-yet-flagged order — retrying "
             "DOWNGRADES it to PENDING and mints a second link"),
            ("delivered", False, False, "cancelled", False,
             "past the door and off the gateway: only an admin can re-open this"),
        ],
    )
    def test_the_rule(self, status, is_paid, is_payable, payment_status, expected, why):
        from keyboards import customer_may_pay

        order = self._order(status, is_paid, is_payable, payment_status)
        assert customer_may_pay(order) is expected, why

    def test_a_missing_or_empty_order_is_never_payable(self):
        from keyboards import customer_may_pay

        assert customer_may_pay(None) is False
        assert customer_may_pay({}) is False

    def test_a_payload_with_no_payment_info_falls_back_to_the_open_checkout_window(self):
        from keyboards import customer_may_pay

        assert customer_may_pay({"id": 1, "status": "pending"}) is True
        assert customer_may_pay({"id": 1, "status": "delivered"}) is False


class TestAStalePaymentSwitchTapIsHarmless:
    """NEW-1 — the BUTTON is gone; the MESSAGES carrying it are permanent.

    Removing `payment_switch_{id}` from `payment_failed` stops the bot DRAWING
    it. It does nothing about every recovery screen already sitting in a
    customer's chat, and `bot.py:674` still routes `^payment_switch_` to a
    handler that dropped the order id and rendered the CART confirmation — where
    Confirm places a SECOND order for the same basket, because an unpaid order
    deliberately keeps its cart.

    Exactly the argument `retry_payment` already carries: "the button is gone
    from the keyboard now, but the MESSAGE carrying it survives in the
    customer's chat forever, so the handler must refuse too."

    Driven by tapping the callback DIRECTLY — a stale message is precisely the
    case a test that reads its taps off a freshly rendered keyboard cannot see,
    which is why the render-sweep above could never have caught this.
    """

    async def test_it_never_reaches_the_cart_confirmation(self, monkeypatch):
        harness = await build_bot_harness(monkeypatch)
        _serve_order(harness, CASE_B)

        await harness.send(harness.updates().tap(f"payment_switch_{ORDER_ID}"))

        drawn = _callback_data(harness)
        # The id-less picker IS the doorway: `payment_cash` / `payment_card`
        # route to `orders.payment_handler` -> `_show_order_confirmation`.
        assert "payment_cash" not in drawn and "payment_card" not in drawn, (
            "the stale Switch-method tap re-opened the id-less rail picker, whose "
            "buttons lead to the CART confirmation — Confirm there buys the same "
            "basket a second time"
        )
        assert "confirm_order" not in drawn
        assert not [c for c in harness.backend.calls if c.endpoint == "/api/v1/orders"], (
            "no order may be created by a tap on a stale payment button"
        )

    async def test_it_gives_a_case_b_customer_the_rail_move_they_asked_for(self, monkeypatch):
        """Honest answer, not a dead end: Retry IS the rail move a customer has
        (POST /payments/create normalizes card->click and flips a pending cash
        order onto Click behind the pool guard)."""
        harness = await build_bot_harness(monkeypatch)
        _serve_order(harness, CASE_B)

        await harness.send(harness.updates().tap(f"payment_switch_{ORDER_ID}"))

        created = [c for c in harness.backend.calls if c.endpoint == "/api/v1/payments/create"]
        assert created and created[0].data["order_id"] == ORDER_ID
        assert any(CLICK_URL in str(call.reply_markup) for call in harness.telegram.calls)

    async def test_a_stale_tap_on_a_dead_order_mints_nothing(self, monkeypatch):
        harness = await build_bot_harness(monkeypatch)
        _serve_order(harness, CANCELLED)

        await harness.send(harness.updates().tap(f"payment_switch_{ORDER_ID}"))

        assert not [c for c in harness.backend.calls if c.endpoint == "/api/v1/payments/create"]
        assert not [c for c in harness.backend.calls if c.endpoint == "/api/v1/orders"]
        assert "confirm_order" not in _callback_data(harness)

    async def test_the_callback_is_still_claimed_by_a_handler(self, monkeypatch):
        """Keep the REGISTRATION. An unclaimed callback is a spinner nobody can
        stop — the rule recorded at `keyboards.py`'s tap-feedback note."""
        harness = await build_bot_harness(monkeypatch)

        update = harness.updates().tap(f"payment_switch_{ORDER_ID}")
        assert harness.handlers_matching(update), "payment_switch_ must stay claimed"
