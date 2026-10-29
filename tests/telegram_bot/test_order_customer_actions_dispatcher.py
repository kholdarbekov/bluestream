"""The bot's order screens render the backend's published answers (spec 2026-10-27 §4.2, §4.3, §4.5, §5.3, §16 M5).

Every order payload publishes `customer_actions`: Pay (`allowed`, `label`, `expected_amount`, `deadline_label`) and
the removal (`via` skip / cancel / decline, `label`, `confirm_text`, `reasons`). The bot draws exactly those buttons
with exactly those words, and every tap goes back to the one write route with what the screen showed:
`retry-payment` with `{"channel": "bot", "expected_amount"}`, `cancel` with `{"expected_via", "reason_code"?}`. A
screen that went stale is answered 409 and the order is shown again as it stands.

The backend is replayed from `tests/fixtures/customer_order_actions/<case>.json`, the fixtures Task 10a records
through the real endpoints and the website's Vitest suite renders too, so both clients are held to the same answers.
A write is answered only when the bot's body is one the backend recorded. Everything runs through the real PTB
dispatcher (tests/telegram_bot/ptb_harness.py).
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.helpers import escape_markdown

# The harness first: it caches the BOT's `i18n`/`config` before the seed script (which puts the repo root first on
# sys.path) is imported (see tests/telegram_bot/conftest.py).
from tests.telegram_bot.ptb_harness import (
    FakeDatabase,
    backend_failure,
    build_bot_harness,
    menu_already_installed,
)
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from tests.telegram_bot.test_checkout_journey_dispatcher import (
    BOTTLE_19L,
    Shop,
    add_address,
    api_failure_with_body,
    fill_cart,
    reach_confirmation,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "customer_order_actions"
# The list as the website reads it; the bot's `GET /api/v1/orders/` (page 1, 10 a page) is answered from it.
LIST_KEY = "GET /api/v1/orders?page=1&per_page=10"
BOT_LIST_ENDPOINT = "/api/v1/orders/"
LANGUAGES = ("en", "uz", "ru")

# The bot's own copy as seeded, so every label asserted here is what a customer reads.
TRANSLATIONS = {
    (language, key): row[language]
    for key, row in BACKEND_TRANSLATIONS.items()
    if key.startswith("telegram.")
    for language in LANGUAGES
    if isinstance(row, dict) and row.get(language)
}


def _copy(key: str, language: str = "en") -> str:
    return BACKEND_TRANSLATIONS[key][language]


def load(case: str) -> dict:
    return json.loads((FIXTURES / f"{case}.json").read_text("utf-8"))


def actions(case: str) -> dict:
    """The case's order as the backend publishes it: its `customer_actions`."""
    fixture = load(case)
    return fixture["requests"][f"GET /api/v1/orders/{fixture['order_id']}"]["body"]["data"]["order"]["customer_actions"]


def exchange(case: str, name: str) -> dict:
    return load(case)["exchanges"][name]


def _answer(status: int, body: dict):
    """A recorded answer as the backend seam gives it: the body, or the failure `_make_request` builds."""
    body = copy.deepcopy(body)
    if status < 400:
        return body
    return backend_failure(body.get("message", f"HTTP {status}"), status, body)


def answered(recorded_exchange: dict):
    """What the backend answered one recorded write."""
    return _answer(recorded_exchange["status"], recorded_exchange["response"])


class Replay:
    """The backend for one fixture case: its reads, and the writes it recorded.

    `go_stale()` is the world moving after the screen was read: from then on the reads answer `requests_after`
    (the case records them for `click_unpaid_stale` and `stale_via`). `only` narrows the writes to the named
    exchanges, for a case that recorded two answers to the same body (`payme_unpaid`).
    """

    def __init__(self, bot, case: str, *, only: tuple = ()):
        self.fixture = load(case)
        self.stale = False
        reads = self.fixture["requests"]
        for key in reads:
            if not key.startswith("GET "):
                continue
            endpoint = key[len("GET "):]
            if "?" in endpoint:
                continue
            bot.backend.route("GET", endpoint, lambda _call, k=key: self._read(k))
        bot.backend.route("GET", BOT_LIST_ENDPOINT, lambda _call: self._read(LIST_KEY))

        writes: dict[str, list] = {}
        for name, recorded in self.fixture["exchanges"].items():
            if only and name not in only:
                continue
            writes.setdefault(recorded["path"], []).append(recorded)
        for path, recorded in writes.items():
            bot.backend.route(recorded[0]["method"], path, lambda call, r=recorded: self._write(call, r))

    def go_stale(self) -> None:
        assert "requests_after" in self.fixture, "this case recorded no after-state"
        self.stale = True

    def _read(self, key: str):
        reads = self.fixture["requests_after"] if self.stale else self.fixture["requests"]
        recorded = reads.get(key) or self.fixture["requests"][key]
        return _answer(recorded["status"], recorded["body"])

    @staticmethod
    def _write(call, recorded: list):
        for candidate in recorded:
            if candidate["body"] == (call.data or {}):
                return answered(candidate)
        return backend_failure(f"the backend recorded no answer to {call.data!r}", 599)


async def _bot(monkeypatch, case: str = None, *, strict_entities: bool = False, database: FakeDatabase = None):
    database = database or FakeDatabase()
    if case is not None and database.user is not None:
        database.user["preferred_language"] = load(case)["language"]
    bot = await build_bot_harness(
        monkeypatch, translations=TRANSLATIONS, database=database, strict_entities=strict_entities,
    )
    # ONE update factory per customer: it numbers the updates, so each tap is its own callback query (Telegram
    # shows only the first answer to a query, and `visible_answers` counts per query).
    bot.user = bot.updates()
    return bot


def _expire_dedup() -> None:
    """Age the in-memory callback-dedup locks: the customer taps again seconds later."""
    from handlers import callback_dedup

    stale = time.monotonic() - 1
    for key in list(callback_dedup._in_memory_locks):
        callback_dedup._in_memory_locks[key] = stale


async def tap(bot, data: str):
    _expire_dedup()
    await bot.send(bot.user.tap(data))


def labels(bot) -> list[str]:
    return bot.telegram.last_shown().button_labels()


def callbacks(bot) -> list[str]:
    return bot.telegram.last_shown().callback_data()


def pay_cb(bot) -> str:
    (pay,) = [data for data in callbacks(bot) if data.startswith("payment_")]
    return pay


def url_buttons(bot) -> list[str]:
    return [
        button["url"]
        for call in bot.telegram.shown
        for row in call.reply_markup.get("inline_keyboard") or []
        for button in row
        if "url" in button
    ]


def visible_alert(bot) -> str:
    (alert,) = [answer for answer in bot.telegram.visible_answers if answer.params.get("show_alert")]
    return alert.params.get("text")


def toast(bot) -> str:
    """The text the customer saw answer their last tap (a second answer to the same tap shows nothing)."""
    return bot.telegram.visible_answers[-1].params.get("text")


def calls(bot, method: str, endpoint: str) -> list:
    return [c for c in bot.backend.calls if c.method == method and c.endpoint == endpoint]


def call(bot, method: str, endpoint: str):
    (only,) = calls(bot, method, endpoint)
    return only


def gets(bot, endpoint: str) -> int:
    return len(calls(bot, "GET", endpoint))


def assert_the_subscription_card(bot, subscription_id: int = 1) -> None:
    """The last screen drawn is that subscription's own card: not an error reply, not the screen before it.

    It pins only rows Task 14's rewrite of the card keeps (Pay, Skip, Pause/Resume, Edit, Items, Orders, Cancel,
    Back). The card's Cancel acts on that subscription (`cancel_sub_<id>`; both subscription fixtures publish cancel
    as allowed), and its Back goes to the subscriptions list. It never pins the card's text, which Task 14
    rewrites. If `subscription_details` raises after its GET, `_reply_error` answers a deep link with a sendMessage
    that has no buttons, and a tap with a toast that leaves the order's confirmation up. Either way this fails.
    """
    shown = bot.telegram.last_shown()
    assert shown.refused is None, shown.refused
    data = shown.callback_data()
    assert {f"cancel_sub_{subscription_id}", "back_to_subscriptions"} <= set(data), data
    assert not [d for d in data if d.startswith("cancel_order_")], data


# --------------------------------------------------------------------------- the order screen


class TestTheOrderScreenDrawsThePublishedAnswers:
    async def test_an_unpaid_click_order_shows_the_published_pay_and_cancel(self, monkeypatch):
        bot = await _bot(monkeypatch, "click_unpaid", strict_entities=True)
        Replay(bot, "click_unpaid")
        a = actions("click_unpaid")

        await tap(bot, "order_1")

        shown = bot.telegram.last_shown()
        assert shown.refused is None, shown.refused
        assert labels(bot) == [a["pay"]["label"], a["cancel"]["label"], _copy("telegram.back")]
        assert pay_cb(bot) == f"payment_retry_1_{a['pay']['expected_amount']}"
        assert "cancel_order_1" in callbacks(bot)
        assert escape_markdown(f"⏳ {a['pay']['deadline_label']}", version=2) in shown.text

    async def test_a_pending_cash_order_offers_no_pay(self, monkeypatch):
        """§13: there is no cash → Click switch, so a cash order publishes no Pay and none is drawn."""
        bot = await _bot(monkeypatch, "cash_pending")
        Replay(bot, "cash_pending")

        await tap(bot, "order_1")

        assert not [data for data in callbacks(bot) if data.startswith("payment_")]
        assert labels(bot) == [actions("cash_pending")["cancel"]["label"], _copy("telegram.back")]
        assert "⏳" not in bot.telegram.last_shown().text

    async def test_a_partly_paid_order_offers_no_pay(self, monkeypatch):
        """§17 Q1: the driver collects the rest at the door; neither Pay nor Cancel is published."""
        bot = await _bot(monkeypatch, "partly_paid")
        Replay(bot, "partly_paid")

        await tap(bot, "order_1")

        assert labels(bot) == [_copy("telegram.back")]
        assert "⏳" not in bot.telegram.last_shown().text

    async def test_a_delivered_unpaid_click_order_offers_pay_and_no_cancel(self, monkeypatch):
        bot = await _bot(monkeypatch, "delivered_unpaid_click")
        Replay(bot, "delivered_unpaid_click")
        a = actions("delivered_unpaid_click")

        await tap(bot, "order_1")

        assert pay_cb(bot) == f"payment_retry_1_{a['pay']['expected_amount']}"
        assert labels(bot) == [a["pay"]["label"], _copy("telegram.order.reorder"), _copy("telegram.back")]
        assert not [data for data in callbacks(bot) if data.startswith("cancel_order_")]

    async def test_a_cancelled_order_offers_neither(self, monkeypatch):
        bot = await _bot(monkeypatch, "cancelled")
        Replay(bot, "cancelled")

        await tap(bot, "order_1")

        assert callbacks(bot) == ["back_to_orders"]


# --------------------------------------------------------------------------- Pay


class TestPayIsTheOneWriteRoute:
    async def test_pay_sends_the_bot_channel_and_the_amount_the_screen_showed(self, monkeypatch):
        bot = await _bot(monkeypatch, "click_unpaid")
        Replay(bot, "click_unpaid")
        recorded = exchange("click_unpaid", "pay_bot")

        await tap(bot, "order_1")
        await tap(bot, pay_cb(bot))

        assert call(bot, "POST", "/api/v1/orders/1/retry-payment").data == recorded["body"]
        assert not calls(bot, "POST", "/api/v1/payments/create")
        assert url_buttons(bot) == [recorded["response"]["data"]["payment_url"]]
        link = bot.telegram.last_shown()
        assert link.text == _copy("telegram.payment.pay_message").format(
            order_number="TG_000001_26", amount="30,000",
        )
        assert link.callback_data() == ["order_1"]

    async def test_a_payme_order_is_paid_through_the_same_route(self, monkeypatch):
        bot = await _bot(monkeypatch, "payme_unpaid")
        Replay(bot, "payme_unpaid", only=("pay_bot",))
        recorded = exchange("payme_unpaid", "pay_bot")

        await tap(bot, "order_1")
        await tap(bot, pay_cb(bot))

        assert call(bot, "POST", "/api/v1/orders/1/retry-payment").data == recorded["body"]
        assert url_buttons(bot) == [recorded["response"]["data"]["payment_url"]]

    async def test_a_changed_amount_is_refused_and_the_fresh_order_is_shown(self, monkeypatch):
        bot = await _bot(monkeypatch, "click_unpaid_stale")
        replay = Replay(bot, "click_unpaid_stale")
        await tap(bot, "order_1")
        stale_pay = pay_cb(bot)
        replay.go_stale()  # the total moved after the screen was read
        bot.telegram.reset()

        await tap(bot, stale_pay)

        assert visible_alert(bot) == exchange("click_unpaid_stale", "pay_bot")["response"]["message"]
        assert gets(bot, "/api/v1/orders/1") == 2 and url_buttons(bot) == []
        fresh = load("click_unpaid_stale")["requests_after"]["GET /api/v1/orders/1"]["body"]["data"]["order"]
        assert labels(bot)[0] == fresh["customer_actions"]["pay"]["label"]
        assert pay_cb(bot) == f"payment_retry_1_{fresh['customer_actions']['pay']['expected_amount']}"

    async def test_an_old_pay_button_after_the_deadline_is_refused_in_words(self, monkeypatch):
        """Review Focus 1: a Pay tapped after the deadline, from an old notice or a screen left open."""
        bot = await _bot(monkeypatch, "click_past_deadline")
        Replay(bot, "click_past_deadline")

        await tap(bot, "payment_retry_1_30000.00")

        assert visible_alert(bot) == "The time to pay for this order online has ended."
        assert visible_alert(bot) == exchange("click_past_deadline", "pay_bot")["response"]["message"]
        assert not [data for data in callbacks(bot) if data.startswith("payment_")]
        assert url_buttons(bot) == []

    @pytest.mark.parametrize("old", ["payment_retry_1", "payment_switch_1"])
    async def test_an_old_pay_button_without_an_amount_opens_the_order_and_writes_nothing(self, monkeypatch, old):
        bot = await _bot(monkeypatch, "click_unpaid")
        Replay(bot, "click_unpaid")

        await tap(bot, old)

        assert not calls(bot, "POST", "/api/v1/orders/1/retry-payment")
        assert labels(bot)[0] == actions("click_unpaid")["pay"]["label"]
        assert url_buttons(bot) == []

    async def test_a_refused_pay_shows_the_backends_words_and_no_retry_loop(self, monkeypatch):
        bot = await _bot(monkeypatch, "cash_pending")
        Replay(bot, "cash_pending")

        await tap(bot, "payment_retry_1_30000.00")

        assert call(bot, "POST", "/api/v1/orders/1/retry-payment").data == exchange("cash_pending", "pay_bot")["body"]
        assert visible_alert(bot) == exchange("cash_pending", "pay_bot")["response"]["message"]
        assert not [data for data in callbacks(bot) if data.startswith("payment_")]
        assert url_buttons(bot) == []

    async def test_a_short_marking_code_pool_shows_the_backends_sentence(self, monkeypatch):
        bot = await _bot(monkeypatch, "payme_unpaid")
        Replay(bot, "payme_unpaid", only=("payme_pool_short_bot",))
        recorded = exchange("payme_unpaid", "payme_pool_short_bot")

        await tap(bot, "order_1")
        await tap(bot, pay_cb(bot))

        assert call(bot, "POST", "/api/v1/orders/1/retry-payment").data == recorded["body"]
        assert visible_alert(bot) == recorded["response"]["message"]
        assert url_buttons(bot) == []

    async def test_a_pay_on_someone_elses_order_shows_the_routes_sentence(self, monkeypatch):
        """404 is one of the answers whose `message` the route translates (`api.orders.not_found`)."""
        bot = await _bot(monkeypatch)  # the harness customer reads uz
        not_found = {"success": False, "message": BACKEND_TRANSLATIONS["api.orders.not_found"]["uz"]}
        for method, endpoint in (("POST", "/api/v1/orders/1/retry-payment"), ("GET", "/api/v1/orders/1")):
            bot.backend.route(method, endpoint, lambda _call: backend_failure(not_found["message"], 404, not_found))

        await tap(bot, "payment_retry_1_30000.00")

        assert visible_alert(bot) == "Buyurtma topilmadi"
        assert url_buttons(bot) == []

    @pytest.mark.parametrize("language", LANGUAGES)
    async def test_a_pay_whose_sign_in_is_still_refused_says_so_in_the_customers_language(
        self, monkeypatch, language,
    ):
        """A 401 that outlives the API client's sign-in-again (a deactivated account, say) carries the JWT
        loader's English `message`. The customer reads the seeded sign-in sentence instead."""
        database = FakeDatabase()
        database.user["preferred_language"] = language
        bot = await _bot(monkeypatch, database=database)
        Replay(bot, "click_unpaid")
        revoked = {"error": "Token Revoked", "message": "The token has been revoked.", "reauth": True}
        bot.backend.route(
            "POST", "/api/v1/orders/1/retry-payment",
            lambda _call: backend_failure(revoked["message"], 401, revoked),
        )

        await tap(bot, "payment_retry_1_30000.00")

        assert bot.telegram.last_shown().text == _copy("telegram.error.auth_failed", language)
        assert revoked["message"] not in [
            answer.params.get("text") for answer in bot.telegram.of("answerCallbackQuery")
        ] + bot.telegram.texts()
        assert not [answer for answer in bot.telegram.visible_answers if answer.params.get("show_alert")]
        assert url_buttons(bot) == []

    @pytest.mark.parametrize(
        "status, message",
        [(403, "Forbidden"), (429, "Rate limit exceeded: 10 per 1 minute")],
    )
    async def test_a_pay_answered_with_another_4xx_shows_the_generic_error_and_keeps_the_screen(
        self, monkeypatch, status, message,
    ):
        """Only 400, 404 and 409 carry a sentence the route translated. Any other 4xx is a failure the customer
        can do nothing with: the seeded generic error, in their language, and the order screen stays up so they
        can tap Pay again."""
        bot = await _bot(monkeypatch)  # the harness customer reads uz
        Replay(bot, "click_unpaid")
        await tap(bot, "order_1")
        order_screen = callbacks(bot)
        bot.backend.route(
            "POST", "/api/v1/orders/1/retry-payment",
            lambda _call: backend_failure(message, status, {"success": False, "message": message}),
        )

        await tap(bot, pay_cb(bot))

        assert toast(bot) == _copy("telegram.error.generic", "uz")
        assert not [answer for answer in bot.telegram.visible_answers if answer.params.get("show_alert")]
        assert callbacks(bot) == order_screen
        assert gets(bot, "/api/v1/orders/1") == 1, "nothing was answered, so the order is not drawn again"
        assert url_buttons(bot) == []


# --------------------------------------------------------------------------- Skip / Cancel / Decline


class TestTheRemovalIsThePublishedOne:
    async def test_a_subscription_order_asks_skip_with_the_published_reasons(self, monkeypatch):
        bot = await _bot(monkeypatch, "subscription_later_order")
        Replay(bot, "subscription_later_order")
        c = actions("subscription_later_order")["cancel"]

        await tap(bot, "cancel_order_2")

        assert bot.telegram.last_shown().text == c["confirm_text"]
        assert labels(bot) == [r["label"] for r in c["reasons"]] + [
            _copy("telegram.orders.skip_no_reason"),
            _copy("telegram.orders.keep_delivery"),
        ]
        assert callbacks(bot) == [f"cancel_order_2_skip_{r['code']}" for r in c["reasons"]] + [
            "cancel_order_2_skip_none",
            "cancel_order_2_confirm_no",
        ]
        assert not calls(bot, "POST", "/api/v1/orders/2/cancel")

    async def test_a_reason_chip_skips_that_order_and_returns_to_its_subscription(self, monkeypatch):
        bot = await _bot(monkeypatch, "subscription_later_order")
        Replay(bot, "subscription_later_order")
        await tap(bot, "cancel_order_2")
        (chip,) = [data for data in callbacks(bot) if data.endswith("_still_have_water")]

        await tap(bot, chip)

        ex = exchange("subscription_later_order", "skip_still_have_water")
        assert call(bot, "POST", "/api/v1/orders/2/cancel").data == ex["body"]
        assert toast(bot) == ex["response"]["message"]
        assert gets(bot, "/api/v1/subscriptions/1") == 1
        assert not [data for data in callbacks(bot) if data.startswith("cancel_order_2")], (
            "the confirmation was replaced by the subscription's screen"
        )
        assert_the_subscription_card(bot, 1)

    async def test_the_other_chip_sends_its_code_and_no_text(self, monkeypatch):
        """Owner call 7: "Other" sends `reason_code: "other"` and never prompts for typed text."""
        bot = await _bot(monkeypatch, "subscription_later_order")
        Replay(bot, "subscription_later_order")
        # The backend recorded "other" only with typed text (the website's); any recorded skip answers this one.
        bot.backend.route(
            "POST", "/api/v1/orders/2/cancel",
            lambda _call: answered(exchange("subscription_later_order", "skip_reason")),
        )
        await tap(bot, "cancel_order_2")
        (chip,) = [data for data in callbacks(bot) if data.endswith("_other")]

        await tap(bot, chip)

        assert call(bot, "POST", "/api/v1/orders/2/cancel").data == {"expected_via": "skip", "reason_code": "other"}

    async def test_skip_without_a_reason_sends_no_reason_code(self, monkeypatch):
        bot = await _bot(monkeypatch, "subscription_later_order")
        Replay(bot, "subscription_later_order")
        await tap(bot, "cancel_order_2")

        await tap(bot, "cancel_order_2_skip_none")

        ex = exchange("subscription_later_order", "skip_no_reason")
        assert call(bot, "POST", "/api/v1/orders/2/cancel").data == ex["body"]
        assert toast(bot) == ex["response"]["message"]

    async def test_keep_this_delivery_goes_back_to_the_order_and_writes_nothing(self, monkeypatch):
        bot = await _bot(monkeypatch, "subscription_later_order")
        Replay(bot, "subscription_later_order")
        await tap(bot, "cancel_order_2")

        await tap(bot, "cancel_order_2_confirm_no")

        assert not calls(bot, "POST", "/api/v1/orders/2/cancel")
        assert "cancel_order_2" in callbacks(bot)

    async def test_a_leftover_on_a_paused_subscription_offers_plain_cancel(self, monkeypatch):
        bot = await _bot(monkeypatch, "subscription_leftover_paused")
        Replay(bot, "subscription_leftover_paused")
        c = actions("subscription_leftover_paused")["cancel"]

        await tap(bot, "cancel_order_1")
        assert bot.telegram.last_shown().text == c["confirm_text"]
        assert labels(bot) == [c["label"], _copy("telegram.orders.keep_order")]
        assert callbacks(bot) == ["cancel_order_1_confirm_yes_cancel", "cancel_order_1_confirm_no"]

        await tap(bot, "cancel_order_1_confirm_yes_cancel")

        ex = exchange("subscription_leftover_paused", "cancel")
        assert call(bot, "POST", "/api/v1/orders/1/cancel").data == {"expected_via": "cancel"} == ex["body"]
        assert toast(bot) == ex["response"]["message"]
        assert gets(bot, "/api/v1/subscriptions/1") == 1
        assert_the_subscription_card(bot, 1)

    async def test_an_agent_proposal_is_declined_through_the_cancel_route(self, monkeypatch):
        bot = await _bot(monkeypatch, "agent_pending")
        Replay(bot, "agent_pending")
        c = actions("agent_pending")["cancel"]

        await tap(bot, "cancel_order_1")
        assert bot.telegram.last_shown().text == c["confirm_text"]
        assert labels(bot) == [c["label"], _copy("telegram.orders.keep_order")]

        await tap(bot, "cancel_order_1_confirm_yes_decline")

        ex = exchange("agent_pending", "decline")
        assert call(bot, "POST", "/api/v1/orders/1/cancel").data == ex["body"]
        assert not calls(bot, "POST", "/api/v1/orders/1/agent-confirmation")
        assert toast(bot) == ex["response"]["message"]
        assert gets(bot, BOT_LIST_ENDPOINT) == 1, "an order without a subscription goes back to the orders list"

    async def test_a_stale_via_is_refused_and_the_fresh_order_is_shown(self, monkeypatch):
        bot = await _bot(monkeypatch, "stale_via")
        replay = Replay(bot, "stale_via")
        await tap(bot, "cancel_order_1")
        assert "cancel_order_1_skip_none" in callbacks(bot)
        replay.go_stale()  # the subscription was paused after the screen was read
        bot.telegram.reset()

        await tap(bot, "cancel_order_1_skip_none")

        ex = exchange("stale_via", "skip")
        assert call(bot, "POST", "/api/v1/orders/1/cancel").data == ex["body"]
        assert visible_alert(bot) == ex["response"]["message"]
        fresh = load("stale_via")["requests_after"]["GET /api/v1/orders/1"]["body"]["data"]["order"]
        assert fresh["customer_actions"]["cancel"]["label"] in labels(bot)
        assert not calls(bot, "GET", "/api/v1/subscriptions/1")

    async def test_an_old_yes_button_without_a_via_cancels_nothing(self, monkeypatch):
        bot = await _bot(monkeypatch, "click_unpaid")
        Replay(bot, "click_unpaid")

        await tap(bot, "cancel_order_1_confirm_yes")

        assert not calls(bot, "POST", "/api/v1/orders/1/cancel")
        assert bot.telegram.last_shown().text == actions("click_unpaid")["cancel"]["confirm_text"]
        assert callbacks(bot) == ["cancel_order_1_confirm_yes_cancel", "cancel_order_1_confirm_no"]

    async def test_a_cancel_tap_on_an_order_that_moved_on_says_why(self, monkeypatch):
        """The Cancel was drawn earlier; the order now publishes the removal refused, with its code."""
        bot = await _bot(monkeypatch, "partly_paid")
        Replay(bot, "partly_paid")

        await tap(bot, "cancel_order_1")

        refusal = actions("partly_paid")["cancel"]["refusal"]
        assert bot.telegram.last_shown().text == _copy(f"telegram.orders.cancel_refused.{refusal.lower()}")
        assert callbacks(bot) == ["order_1", "menu_orders"]
        assert not calls(bot, "POST", "/api/v1/orders/1/cancel")


# --------------------------------------------------------------------------- /start order_<id>, sub_<id>


class TestDeepLinks:
    async def test_start_order_deep_link_opens_that_order_as_a_new_message(self, monkeypatch):
        bot = await _bot(monkeypatch, "click_unpaid", strict_entities=True)
        Replay(bot, "click_unpaid")
        await menu_already_installed(bot)

        await bot.send(bot.user.command("start order_1"))

        shown = bot.telegram.last_shown()
        assert (shown.method, shown.refused) == ("sendMessage", None)
        assert escape_markdown("TG_000001_26", version=2) in shown.text
        assert shown.button_labels()[0] == actions("click_unpaid")["pay"]["label"]
        assert gets(bot, "/api/v1/orders/1") == 1

    async def test_start_sub_deep_link_opens_that_subscription(self, monkeypatch):
        bot = await _bot(monkeypatch, "subscription_later_order")
        Replay(bot, "subscription_later_order")
        await menu_already_installed(bot)

        await bot.send(bot.user.command("start sub_1"))

        shown = bot.telegram.last_shown()
        assert (shown.method, shown.refused) == ("sendMessage", None)
        assert gets(bot, "/api/v1/subscriptions/1") == 1
        assert not [c for c in bot.backend.calls if c.endpoint.startswith("/api/v1/orders")]
        assert_the_subscription_card(bot, 1)

    async def test_a_deep_link_to_an_order_the_account_does_not_own_says_not_found_with_the_menu(self, monkeypatch):
        bot = await _bot(monkeypatch, "click_unpaid")
        # Someone else's order: `GET /orders/<id>` answers 404 (`not_found_response`).
        bot.backend.route(
            "GET", "/api/v1/orders/77",
            lambda _call: backend_failure("Order not found", 404, {"success": False, "message": "Order not found"}),
        )
        await menu_already_installed(bot)

        await bot.send(bot.user.command("start order_77"))

        shown = bot.telegram.last_shown()
        assert shown.method == "sendMessage"
        assert shown.text == _copy("telegram.deep_link.not_found")
        assert shown.reply_markup.get("keyboard"), "the main menu comes with it"

    async def test_a_deep_link_to_a_subscription_the_account_does_not_own_says_not_found(self, monkeypatch):
        bot = await _bot(monkeypatch, "click_unpaid")
        bot.backend.route(
            "GET", "/api/v1/subscriptions/77",
            lambda _call: backend_failure("Not found", 404, {"success": False, "message": "Not found"}),
        )
        await menu_already_installed(bot)

        await bot.send(bot.user.command("start sub_77"))

        assert bot.telegram.last_shown().text == _copy("telegram.deep_link.not_found")

    async def test_a_deep_link_from_a_new_telegram_only_starts_registration(self, monkeypatch):
        bot = await _bot(monkeypatch, database=FakeDatabase(user=None))

        await bot.send(bot.user.command("start order_1"))

        assert not [c for c in bot.backend.calls if c.endpoint.startswith("/api/v1/orders")]
        card = bot.telegram.last_shown()
        assert card.method == "sendMessage"
        assert any(data.startswith("set_language_") for data in card.callback_data()), card.callback_data()

    async def test_a_malformed_deep_link_is_a_plain_start(self, monkeypatch):
        bot = await _bot(monkeypatch, "click_unpaid")
        await menu_already_installed(bot)

        await bot.send(bot.user.command("start order_x"))

        assert not [c for c in bot.backend.calls if c.endpoint.startswith("/api/v1/orders")]
        assert bot.telegram.last_shown().text == _copy("telegram.main_menu")


# --------------------------------------------------------------------------- checkout: the link POST /orders minted


def _checkout_bot(bot, monkeypatch):
    shop = Shop(bot.backend, monkeypatch)
    add_address(bot, 900, "Uy", "Chilonzor 15")
    return shop


class TestCheckoutReadsThePublishedWait:
    """Task 10b fix round 1 (3): `confirm_order` waits the seconds the backend counts, never its own clock."""

    @pytest.fixture
    def slept(self, monkeypatch):
        import handlers.orders as orders_module

        seconds: list = []

        async def _sleep(value):
            seconds.append(value)

        monkeypatch.setattr(orders_module, "asyncio", SimpleNamespace(sleep=_sleep))
        return seconds

    async def test_a_slow_link_waits_the_published_seconds_and_arrives_as_a_new_message(self, monkeypatch, slept):
        bot = await _bot(monkeypatch)
        shop = _checkout_bot(bot, monkeypatch)
        recorded = load("checkout_click_slow")["exchange"]
        shop.order_response = copy.deepcopy(recorded["response"])
        user = bot.user
        await fill_cart(bot, user, BOTTLE_19L, quantity=3)
        await reach_confirmation(bot, user, 900, payment="card")
        bot.telegram.reset()

        await bot.send(user.tap("confirm_order"))

        assert slept == [recorded["response"]["data"]["payment_ready_in_seconds"]] == [45]
        (link,) = [c for c in bot.telegram.of("sendMessage") if c.reply_markup.get("inline_keyboard")]
        assert url_buttons(bot) == [recorded["response"]["data"]["payment_url"]]
        assert link.callback_data() == ["order_1"]
        assert not calls(bot, "POST", "/api/v1/payments/create")

    async def test_a_fast_link_is_shown_in_place_with_no_wait(self, monkeypatch, slept):
        bot = await _bot(monkeypatch)
        shop = _checkout_bot(bot, monkeypatch)
        recorded = load("checkout_click_fast")["exchange"]
        shop.order_response = copy.deepcopy(recorded["response"])
        user = bot.user
        await fill_cart(bot, user, BOTTLE_19L, quantity=3)
        await reach_confirmation(bot, user, 900, payment="card")
        bot.telegram.reset()

        await bot.send(user.tap("confirm_order"))

        assert slept == []
        link = bot.telegram.last_shown()
        assert link.method == "editMessageText"
        assert url_buttons(bot) == [recorded["response"]["data"]["payment_url"]]


class TestTheTaxOutageRescueIsOnePublishedAnswer:
    """Task 10b fix round 1 (1): the 503's own sentence, and a cash rescue only when the limit allows it."""

    async def _outage(self, monkeypatch, case: str):
        bot = await _bot(monkeypatch)
        shop = _checkout_bot(bot, monkeypatch)
        recorded = load(case)["exchange"]
        body = recorded["response"]
        shop.raw_responses[("POST", "/api/v1/orders")] = api_failure_with_body(
            body["message"], recorded["status"], copy.deepcopy(body),
        )
        user = bot.user
        await fill_cart(bot, user, BOTTLE_19L, quantity=3)
        await reach_confirmation(bot, user, 900, payment="card")
        bot.telegram.reset()
        await bot.send(user.tap("confirm_order"))
        return bot, body

    async def test_a_capped_customer_is_offered_only_try_again(self, monkeypatch):
        bot, body = await self._outage(monkeypatch, "rescue_asl_down_capped")

        outage = bot.telegram.last_shown()
        assert outage.text == body["message"]
        assert outage.callback_data() == ["confirm_order"]
        assert outage.button_labels() == [_copy("telegram.orders.asl_belgisi_retry", "uz")]

    async def test_a_customer_cash_is_open_to_is_offered_both(self, monkeypatch):
        bot, body = await self._outage(monkeypatch, "rescue_asl_down")

        outage = bot.telegram.last_shown()
        assert outage.text == body["message"]
        assert outage.callback_data() == ["select_payment_cash", "confirm_order"]
