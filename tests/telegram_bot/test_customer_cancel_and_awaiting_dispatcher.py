"""The customer bot's half of F5 and F15: Cancel, its refusal, and the awaiting label.

Spec: docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md §3.3
("Bot clients") and §4.2 ("Customer bot").

Order 1277 (TG_000508_26) is why. Its Cancel button was drawn while the order was
pending. The customer tapped it after the delivery had failed, and it ended an order
that was only waiting for a new date. Now:

* the bot draws Cancel from the backend's published `can_customer_cancel` and holds no
  copy of the rule, so a payload without the field offers no Cancel;
* when the backend refuses a stale tap, the bot says why in the customer's language,
  picked by the refusal's `reason_code`, pointing to this chat and never to a phone;
* an order awaiting a new date says so in the list, on its detail screen and on the
  Track screen, with no ETA.

Every journey goes through the real PTB dispatcher (tests/telegram_bot/ptb_harness.py),
against the payloads the backend really returns: `serialize_order` for an order, the
`error_response` envelope for a refusal, and the `/track` dict.
"""

from __future__ import annotations

import copy
import re
import time

import pytest
from telegram.helpers import escape_markdown

# `tests.telegram_bot.ptb_harness` imports the BOT's `i18n`/`config` first, so those
# names are cached in `sys.modules` before anything below can shadow them.
# `scripts.seed_backend_translations` inserts `/app` at `sys.path[0]` at import time
# (it is also a standalone script), which — if imported first here — repromotes the
# repo root over `telegram_bot/` and breaks every later `from config import config` in
# this session. See `tests/telegram_bot/conftest.py`'s `_prioritise_bot_path` docstring.
from tests.telegram_bot.ptb_harness import FakeDatabase, backend_failure, build_bot_harness
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for
from shared.constants import ORDER_DISPLAY_AWAITING_NEW_DATE, ORDER_STATUS_ICONS
from shared.i18n_rendering import render_translation

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

LANGUAGES = ("en", "uz", "ru")

ORDER_ID = 1277
ORDER_NUMBER = "TG_000508_26"
ORDER_ENDPOINT = f"/api/v1/orders/{ORDER_ID}"
CANCEL_ENDPOINT = f"/api/v1/orders/{ORDER_ID}/cancel"
BOT_URL = "https://t.me/aqua_element_bot"

AWAITING_LABEL_KEY = "telegram.orders.status_awaiting_new_date"
STATUS_HEADING_KEY = "telegram.orders.detail_status_label"
CURRENT_KEY = "telegram.orders.current_status"
CONFIRMED_LABEL_KEY = "telegram.orders.status_confirmed"

# (reason_code the backend refuses with, the web copy it sends as `message`, the chat
# copy the bot shows instead).
REFUSALS = (
    ("WITH_DRIVER", "api.orders.cancel_refused.with_driver", "telegram.orders.cancel_refused.with_driver"),
    (
        "AWAITING_NEW_DATE",
        "api.orders.cancel_refused.awaiting_new_date",
        "telegram.orders.cancel_refused.awaiting_new_date",
    ),
    ("PAID", "api.orders.cancel_refused.paid", "telegram.orders.cancel_refused.paid"),
    (
        "NOT_CANCELLABLE",
        "api.orders.cancel_refused.not_cancellable",
        "telegram.orders.cancel_refused.not_cancellable",
    ),
)
BOT_REFUSAL_KEYS = tuple(bot_key for _code, _web_key, bot_key in REFUSALS)
NEW_BOT_COPY_KEYS = BOT_REFUSAL_KEYS + (AWAITING_LABEL_KEY, STATUS_HEADING_KEY)

# The refusals that ask the customer to write to us (spec §3.3 copy table, F16), and
# the word each language uses for "this chat". `not_cancellable` asks for nothing.
CHAT_POINTING_KEYS = (
    "telegram.orders.cancel_refused.with_driver",
    "telegram.orders.cancel_refused.awaiting_new_date",
    "telegram.orders.cancel_refused.paid",
)
CHAT_WORDS = {"en": "chat", "uz": "chatda", "ru": "чате"}

# The screens here are served the real seeded copy, so every assertion reads what a
# customer reads. The `if` keeps a missing seed from breaking collection: it fails by
# name in `test_seeded_in_all_three_languages_in_the_category_the_bot_loads` instead.
TRANSLATIONS = {
    (language, key): BACKEND_TRANSLATIONS[key][language]
    for key in NEW_BOT_COPY_KEYS + (CURRENT_KEY, CONFIRMED_LABEL_KEY, "telegram.orders.status_created")
    for language in LANGUAGES
    if BACKEND_TRANSLATIONS.get(key, {}).get(language)
}

_UNPUBLISHED = object()


def _copy(key: str, language: str) -> str:
    """The seeded copy a customer reads for `key`."""
    return BACKEND_TRANSLATIONS[key][language]


def _order(*, status, can_customer_cancel=_UNPUBLISHED, display_status=None, payment_status="pending"):
    """What `GET /api/v1/orders/<id>` returns: the `serialize_order` fields the bot reads."""
    order = {
        "id": ORDER_ID,
        "order_number": ORDER_NUMBER,
        "created_at": "2026-09-20T05:00:00+00:00",
        "total_amount": 36000,
        "status": status,
        "display_status": display_status or status,
        "is_paid": False,
        "order_items": [],
        "payment_info": {"payment_method": "cash", "payment_status": payment_status, "is_payable": False},
    }
    if can_customer_cancel is not _UNPUBLISHED:
        order["can_customer_cancel"] = can_customer_cancel
    return order


PENDING = _order(status="pending", can_customer_cancel=True)
AWAITING_NEW_DATE_ORDER = _order(
    status="confirmed", display_status=ORDER_DISPLAY_AWAITING_NEW_DATE, can_customer_cancel=False
)


def _track_awaiting() -> dict:
    """What `GET /api/v1/orders/<id>/track` returns while the order awaits a new date.

    The order keeps its status and publishes `display_status`. There is no ETA, and the
    timeline's one current entry is the synthetic awaiting step, stamped with the
    failure time (spec §4.2).
    """
    return {
        "data": {
            "order": {
                "id": ORDER_ID,
                "order_number": ORDER_NUMBER,
                "status": "confirmed",
                "display_status": ORDER_DISPLAY_AWAITING_NEW_DATE,
                "total_amount": 36000,
                "created_at": "2026-09-20T05:00:00+00:00",
                "payment_info": None,
            },
            "delivery": {"id": 3301, "status": "failed"},
            "timeline": [
                {"status": "created", "timestamp": "2026-09-20T05:00:00+00:00", "notes": None, "is_current": False},
                {"status": "confirmed", "timestamp": "2026-09-20T05:04:00+00:00", "notes": None, "is_current": False},
                {
                    "status": ORDER_DISPLAY_AWAITING_NEW_DATE,
                    "timestamp": "2026-09-21T07:30:00+00:00",
                    "notes": None,
                    "is_current": True,
                },
            ],
            "estimated_time_remaining": None,
            "payment_timeline": [],
        }
    }


def _refusal_body(reason_code: str, web_key: str) -> dict:
    """The 400 body `POST /orders/<id>/cancel` answers with when F5 refuses.

    Built by `error_response(message=<web copy>, data={...})`: the code sits at
    `data.reason_code`, and `message` is the WEB wording with the bot link filled in.
    """
    return {
        "success": False,
        "message": render_translation(web_key, BACKEND_TRANSLATIONS[web_key]["uz"], (), {"bot_url": BOT_URL}),
        "data": {
            "error_code": "ORDER_NOT_CUSTOMER_CANCELLABLE",
            "reason_code": reason_code,
            "can_customer_cancel": False,
        },
    }


async def _harness(monkeypatch, language: str = "uz"):
    database = FakeDatabase()
    database.user["preferred_language"] = language
    return await build_bot_harness(monkeypatch, translations=TRANSLATIONS, database=database)


def _serve(harness, holder: dict) -> None:
    """Answer GET /orders/<id> with whatever `holder["order"]` is when the call arrives."""
    harness.backend.route(
        "GET",
        ORDER_ENDPOINT,
        lambda _call: {"data": {"order": copy.deepcopy(holder["order"]), "delivery": None}},
    )


def _refuse_cancel(harness, body: dict, status_code: int = 400) -> None:
    harness.backend.route(
        "POST", CANCEL_ENDPOINT, lambda _call: backend_failure(body["message"], status_code, body)
    )


def _order_calls(harness, since: int = 0) -> list[tuple[str, str]]:
    """Every backend call about orders, from `since` on."""
    return [
        (call.method, call.endpoint)
        for call in harness.backend.calls[since:]
        if call.endpoint.startswith("/api/v1/orders")
    ]


def _expire_dedup() -> None:
    """Age the in-memory callback-dedup locks: the customer re-taps seconds later."""
    from handlers import callback_dedup

    stale = time.monotonic() - 1
    for key in list(callback_dedup._in_memory_locks):
        callback_dedup._in_memory_locks[key] = stale


class TestCancelIsTheBackendsAnswer:
    """`customer_may_cancel` reads `can_customer_cancel` and nothing else (F5)."""

    @pytest.mark.parametrize(
        "order, drawn",
        [
            pytest.param(PENDING, True, id="pending-published-true"),
            # A completed payment whose `is_paid` flag has not caught up yet. The old
            # local rule read only `is_paid` and drew Cancel.
            pytest.param(
                _order(status="pending", can_customer_cancel=False, payment_status="completed"),
                False,
                id="pending-paid-published-false",
            ),
            # Confirmed and no driver has it: F5 allows it, and the old local rule hid it.
            pytest.param(_order(status="confirmed", can_customer_cancel=True), True, id="confirmed-driverless"),
            pytest.param(AWAITING_NEW_DATE_ORDER, False, id="awaiting-new-date"),
            pytest.param(_order(status="pending"), False, id="field-missing-fails-closed"),
        ],
    )
    async def test_the_detail_screen_draws_cancel_only_when_the_order_publishes_true(
        self, monkeypatch, order, drawn
    ):
        harness = await _harness(monkeypatch)
        _serve(harness, {"order": order})

        await harness.send(harness.updates().tap(f"order_{ORDER_ID}"))

        buttons = harness.telegram.last_shown().callback_data()
        assert (f"cancel_order_{ORDER_ID}" in buttons) is drawn, buttons

    def test_no_order_or_no_field_never_offers_cancel(self):
        from keyboards import customer_may_cancel

        assert customer_may_cancel(None) is False
        assert customer_may_cancel({}) is False
        # The fields the deleted local rule read decide nothing any more.
        assert customer_may_cancel({"id": ORDER_ID, "status": "pending", "is_paid": False}) is False


# What the refusal leaves on the card: back to the order, whose fresh read offers no Cancel, or to
# the orders list. The Track screen's pair; without it the refusal was a dead end.
BACK_FROM_REFUSAL = [f"order_{ORDER_ID}", "menu_orders"]


class TestARefusedCancelIsExplainedInTheChat:
    """A Yes tap on a stale confirmation card that the backend refuses."""

    @pytest.mark.parametrize("reason_code, web_key, bot_key", REFUSALS, ids=[code for code, _w, _b in REFUSALS])
    async def test_each_reason_code_gets_its_own_chat_copy(self, monkeypatch, reason_code, web_key, bot_key):
        harness = await _harness(monkeypatch, "uz")
        _refuse_cancel(harness, _refusal_body(reason_code, web_key))

        await harness.send(harness.updates().tap(f"cancel_order_{ORDER_ID}_confirm_yes"))

        refusal = harness.telegram.last_shown()
        assert refusal.method == "editMessageText", "the stale card must be replaced, not left standing"
        assert refusal.text == _copy(bot_key, "uz")
        assert BOT_URL not in refusal.text, "the web copy sends the customer to the bot they are already in"
        assert refusal.callback_data() == BACK_FROM_REFUSAL, (
            "the stale Yes/No must be gone, so it cannot be tapped again, and a way back left in its place"
        )
        assert len(harness.telegram.visible_answers) == 1, "the tap's spinner must stop"
        assert _order_calls(harness) == [("POST", CANCEL_ENDPOINT)]

    async def test_an_unknown_reason_code_shows_the_backends_message(self, monkeypatch):
        """A code this release has no copy for (a newer backend) falls back to `message`.

        On the card, never as a toast: Telegram refuses a callback answer longer than
        200 characters, and the web wording carries the bot link. This fake transport
        has no such limit, so what is pinned is where the text goes.
        """
        harness = await _harness(monkeypatch, "uz")
        body = _refusal_body("SOME_NEWER_REASON", "api.orders.cancel_refused.awaiting_new_date")
        _refuse_cancel(harness, body)

        await harness.send(harness.updates().tap(f"cancel_order_{ORDER_ID}_confirm_yes"))

        refusal = harness.telegram.last_shown()
        assert refusal.method == "editMessageText", "a toast cannot carry the backend's web wording"
        assert refusal.text == body["message"]
        assert refusal.callback_data() == BACK_FROM_REFUSAL, (
            "the stale Yes/No must be gone, so it cannot be tapped again, and a way back left in its place"
        )
        (answer,) = harness.telegram.visible_answers
        assert not answer.params.get("text"), "the refusal is on the card; the ack only stops the spinner"
        assert _order_calls(harness) == [("POST", CANCEL_ENDPOINT)]

    async def test_a_refusal_with_no_reason_code_shows_the_backends_message(self, monkeypatch):
        """A 404 carries only `message` and no code: today's toast, unchanged."""
        harness = await _harness(monkeypatch, "uz")
        _refuse_cancel(harness, {"success": False, "message": "Buyurtma topilmadi"}, status_code=404)

        await harness.send(harness.updates().tap(f"cancel_order_{ORDER_ID}_confirm_yes"))

        (answer,) = harness.telegram.visible_answers
        assert answer.params.get("text") == "❌ Buyurtma topilmadi"


class TestTheStaleCancelOnAnOrderAwaitingANewDate:
    """Review Focus 2: the order 1277 incident, end to end, in each language."""

    @pytest.mark.parametrize("language", LANGUAGES)
    async def test_the_customer_is_told_a_new_date_is_coming_and_nothing_else_happens(
        self, monkeypatch, language
    ):
        harness = await _harness(monkeypatch, language)
        served = {"order": copy.deepcopy(PENDING)}
        _serve(harness, served)
        _refuse_cancel(
            harness, _refusal_body("AWAITING_NEW_DATE", "api.orders.cancel_refused.awaiting_new_date")
        )
        user = harness.updates()

        # While the order was pending, the customer opened it, saw Cancel and tapped it.
        await harness.send(user.tap(f"order_{ORDER_ID}"))
        assert f"cancel_order_{ORDER_ID}" in harness.telegram.last_shown().callback_data()
        await harness.send(user.tap(f"cancel_order_{ORDER_ID}"))
        yes = next(data for data in harness.telegram.last_shown().callback_data() if data.endswith("_yes"))

        # The delivery failed before they answered the confirmation card.
        served["order"] = copy.deepcopy(AWAITING_NEW_DATE_ORDER)
        harness.telegram.reset()
        before = len(harness.backend.calls)

        await harness.send(user.tap(yes))

        refusal = harness.telegram.last_shown()
        assert refusal.text == _copy("telegram.orders.cancel_refused.awaiting_new_date", language)
        assert refusal.callback_data() == BACK_FROM_REFUSAL
        assert _order_calls(harness, before) == [("POST", CANCEL_ENDPOINT)], (
            "the refused cancel must be the bot's only call: no retry, no other write"
        )

        # Opened again: no Cancel, and the status line says a new date is coming.
        _expire_dedup()
        await harness.send(user.tap(f"order_{ORDER_ID}"))
        detail = harness.telegram.last_shown()
        assert f"cancel_order_{ORDER_ID}" not in detail.callback_data()
        assert escape_markdown(_copy(AWAITING_LABEL_KEY, language), version=2) in detail.text


class TestAStalePaymentCancelTapAsksTheOrder:
    """`payment_cancel_<id>`: nothing has drawn one since 553472c, but the messages stay."""

    async def test_it_fetches_the_order_and_draws_its_answers(self, monkeypatch):
        harness = await _harness(monkeypatch)
        _serve(harness, {"order": PENDING})

        await harness.send(harness.updates().tap(f"payment_cancel_{ORDER_ID}"))

        assert _order_calls(harness) == [("GET", ORDER_ENDPOINT)]
        assert harness.telegram.last_shown().callback_data() == [
            f"payment_retry_{ORDER_ID}",
            "menu_orders",
            f"cancel_order_{ORDER_ID}",
        ]

    async def test_an_order_that_moved_on_is_offered_neither_pay_nor_cancel(self, monkeypatch):
        harness = await _harness(monkeypatch)
        _serve(harness, {"order": AWAITING_NEW_DATE_ORDER})

        await harness.send(harness.updates().tap(f"payment_cancel_{ORDER_ID}"))

        assert _order_calls(harness) == [("GET", ORDER_ENDPOINT)]
        assert harness.telegram.last_shown().callback_data() == ["menu_orders"]

    async def test_a_failed_fetch_offers_neither_pay_nor_cancel(self, monkeypatch):
        harness = await _harness(monkeypatch)
        harness.backend.route(
            "GET",
            ORDER_ENDPOINT,
            lambda _call: backend_failure(
                "Buyurtma topilmadi", 404, {"success": False, "message": "Buyurtma topilmadi"}
            ),
        )

        await harness.send(harness.updates().tap(f"payment_cancel_{ORDER_ID}"))

        assert harness.telegram.last_shown().callback_data() == ["menu_orders"]


class TestTheAwaitingLabel:
    """F15: each bot screen names the state the backend publishes, with no ETA."""

    async def test_the_orders_list_marks_it(self, monkeypatch):
        harness = await _harness(monkeypatch)
        other = {**_order(status="confirmed", can_customer_cancel=True), "id": 1278, "order_number": "TG_000509_26"}
        harness.backend.route(
            "GET",
            "/api/v1/orders/",
            lambda _call: {"data": {"orders": [copy.deepcopy(AWAITING_NEW_DATE_ORDER), other]}},
        )

        await harness.send(harness.updates().tap("menu_orders"))

        awaiting_button, other_button = harness.telegram.last_shown().button_labels()[:2]
        assert ORDER_STATUS_ICONS[ORDER_DISPLAY_AWAITING_NEW_DATE] != ORDER_STATUS_ICONS["confirmed"]
        assert awaiting_button.startswith(ORDER_STATUS_ICONS[ORDER_DISPLAY_AWAITING_NEW_DATE])
        assert other_button.startswith(ORDER_STATUS_ICONS["confirmed"])

    @pytest.mark.parametrize("language", LANGUAGES)
    async def test_the_detail_screen_names_it_in_the_customers_language(self, monkeypatch, language):
        harness = await _harness(monkeypatch, language)
        _serve(harness, {"order": AWAITING_NEW_DATE_ORDER})

        await harness.send(harness.updates().tap(f"order_{ORDER_ID}"))

        line = (
            f"📊 {_copy(STATUS_HEADING_KEY, language)}: "
            f"{ORDER_STATUS_ICONS[ORDER_DISPLAY_AWAITING_NEW_DATE]} {_copy(AWAITING_LABEL_KEY, language)}"
        )
        assert escape_markdown(line, version=2) in harness.telegram.last_shown().text

    @pytest.mark.parametrize("language", LANGUAGES)
    async def test_the_track_screen_has_one_current_step_on_the_label_and_no_eta(self, monkeypatch, language):
        harness = await _harness(monkeypatch, language)
        harness.backend.route("GET", f"/api/v1/orders/{ORDER_ID}/track", lambda _call: _track_awaiting())

        await harness.send(harness.updates().tap(f"track_order_{ORDER_ID}"))

        lines = harness.telegram.last_shown().text.splitlines()
        current = [line for line in lines if line.endswith(f"← {_copy(CURRENT_KEY, language)}")]
        assert len(current) == 1, lines
        assert _copy(AWAITING_LABEL_KEY, language) in current[0]
        assert any(
            line.startswith("✅") and _copy(CONFIRMED_LABEL_KEY, language) in line for line in lines
        ), "the steps already passed stay listed"
        assert not any("⏰" in line for line in lines), "no ETA while the order waits for a new date"


class TestTheNewCopyIsRealCopy:
    """Seeded where the bot looks, rendered by the real `get()`, and never a phone."""

    @pytest.mark.parametrize("key", NEW_BOT_COPY_KEYS)
    def test_seeded_in_all_three_languages_in_the_category_the_bot_loads(self, key):
        row = BACKEND_TRANSLATIONS.get(key)
        assert row is not None, f"{key} is rendered by the customer bot but never seeded"
        for language in LANGUAGES:
            assert row.get(language), f"{key} has no {language} copy"
        # telegram_bot/i18n.py loads only category 'telegram'.
        assert _category_for(key) == "telegram"

    @pytest.mark.parametrize("key", NEW_BOT_COPY_KEYS)
    def test_renders_through_the_real_translation_get(self, key):
        """No placeholders: the call sites pass no values, and `render_translation`
        degrades a template it cannot fill to the humanised key."""
        from i18n import Translation

        row = BACKEND_TRANSLATIONS[key]
        translator = Translation()
        translator.translations = {language: {key: row[language]} for language in LANGUAGES}
        for language in LANGUAGES:
            assert translator.get(key, language) == row[language]

    @pytest.mark.parametrize("key", NEW_BOT_COPY_KEYS)
    def test_carries_no_phone_and_no_link(self, key):
        row = BACKEND_TRANSLATIONS[key]
        for language in LANGUAGES:
            assert not re.search(r"\d", row[language]), f"{key} [{language}] carries digits"
            assert "t.me" not in row[language], f"{key} [{language}] sends the customer out of this chat"
        assert re.search(r"[А-Яа-яЁё]", row["ru"]), f"{key} ru copy is not Russian"

    @pytest.mark.parametrize("key", CHAT_POINTING_KEYS)
    def test_the_refusals_that_ask_for_a_word_point_to_this_chat(self, key):
        """F16: inside the bot, the customer-cancel refusal points to the bot chat,
        whose free text lands in the admin Support Inbox."""
        row = BACKEND_TRANSLATIONS[key]
        for language, chat_word in CHAT_WORDS.items():
            assert chat_word in row[language].lower(), f"{key} [{language}] does not point to this chat"

    def test_the_awaiting_refusal_never_says_a_driver_has_the_order(self):
        """Spec §3.3: the delivery failed and a new date is being chosen. No driver has it."""
        row = BACKEND_TRANSLATIONS["telegram.orders.cancel_refused.awaiting_new_date"]
        assert "driver" not in row["en"].lower()
        assert "haydovchi" not in row["uz"].lower()
        assert "водител" not in row["ru"].lower()

    def test_every_reason_code_the_backend_refuses_with_has_chat_copy(self):
        from business_app.services.order_service import (
            CUSTOMER_CANCEL_AWAITING_NEW_DATE,
            CUSTOMER_CANCEL_NOT_CANCELLABLE,
            CUSTOMER_CANCEL_PAID,
            CUSTOMER_CANCEL_WITH_DRIVER,
        )
        from handlers.orders import _CANCEL_REFUSED_KEYS

        assert set(_CANCEL_REFUSED_KEYS) == {
            CUSTOMER_CANCEL_NOT_CANCELLABLE,
            CUSTOMER_CANCEL_AWAITING_NEW_DATE,
            CUSTOMER_CANCEL_PAID,
            CUSTOMER_CANCEL_WITH_DRIVER,
        }
        assert set(_CANCEL_REFUSED_KEYS.values()) == set(BOT_REFUSAL_KEYS)

    def test_the_recovery_keyboard_makes_every_caller_decide(self):
        """Every caller holds the order, so every caller decides. A forgotten decision
        fails loudly instead of defaulting to both buttons."""
        from keyboards import PaymentKeyboards

        with pytest.raises(TypeError):
            PaymentKeyboards.payment_failed(ORDER_ID, "uz")
        with pytest.raises(TypeError):
            PaymentKeyboards.payment_failed(ORDER_ID, "uz", True, True)
