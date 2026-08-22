"""What happens to the customer when TELEGRAM says no.

WHY THIS FILE EXISTS
--------------------
Every other module under ``tests/telegram_bot/`` assumes the Bot API always
answers 200. Production does not work that way. These five rejections are in
this project's own logs, and each one arrives in the middle of a flow a real
customer is standing in:

    editMessageText     "Bad Request: message is not modified"
    editMessageText     "Bad Request: message to edit not found"
    sendMessage         "Bad Request: can't parse entities ..."
    answerCallbackQuery "Bad Request: query is too old ..."
    deleteMessage       "Bad Request: message to delete not found"

A bot that dies on any of them looks, from the customer's side, exactly like a
bot that is broken: a spinner that never stops, a prompt whose buttons are
dead, an address they typed that was never saved. So every test here scripts a
real rejection onto the transport and then asks the only two questions that
matter:

    1. did the customer still get SOMETHING?
    2. is the data they had already given still safe?

The same seam also lets us test the callback-dedup middleware for real
(``telegram_bot/handlers/callback_dedup.py``): it exists *because* of these
rejections — a double-tap deletes a message under its own second invocation —
and it can only be observed at the dispatcher, which is where these tests sit.

RATCHET TESTS
-------------
Several handlers currently swallow a Telegram rejection into a bare
``except Exception`` that returns ``ConversationHandler.END``. That silently
kills the customer's flow. Those tests are marked RATCHET: they pin the
CURRENT behaviour so it cannot get worse, and each one names the behaviour that
would be correct. They must be inverted, not deleted, when the handler is
fixed.
"""

from __future__ import annotations

import time

import pytest
from telegram import Update

# Module-level so `i18n`, `keyboards` and `config` resolve as the BOT's
# versions before anything below touches them. See tests/telegram_bot/conftest.py.
from handlers import callback_dedup
from handlers.profile import (
    ADDRESS_APARTMENT,
    ADDRESS_DELIVERY_INSTRUCTIONS,
    ADDRESS_FLOOR,
    ADDRESS_LOCATION,
    ADDRESS_TITLE,
)

from tests.telegram_bot.helpers import overview_balance_row, overview_payload
from tests.telegram_bot.ptb_harness import (
    DEFAULT_CHAT_ID,
    DEFAULT_USER_ID,
    build_bot_harness,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


# The pin from the traced production session (tests/telegram_bot/helpers docstring
# neighbours); inside TASHKENT_POLYGON so the real delivery-zone SSOT accepts it.
PIN_LAT = 41.32354
PIN_LNG = 69.241036

# A reverse-geocoded address is interpolated RAW into a `parse_mode='Markdown'`
# message (telegram_bot/handlers/profile.py::location_received). Uzbek address
# strings really do carry these characters — building suffixes like "15_A" and
# geocoder annotations in brackets — and each one is a Markdown metacharacter.
MARKDOWN_HOSTILE_ADDRESS = "Chilonzor 15_A, [Yangi] uy, Toshkent"
MARKDOWN_SAFE_ADDRESS = "15, Chilonzor dahasi, Toshkent shahri"

# The real seeded copy (scripts/seed_backend_translations.py). The `*...*` is
# why parse_mode='Markdown' is set at all, and `{address}` is why an address
# with a stray `_` can break the whole message.
TRANSLATIONS = {
    "telegram.address.detected_location_prefix": "📍 *Aniqlangan joylashuv:*\n{address}\n\n",
    "telegram.address.title_prompt": "Manzilga nom bering:",
    "telegram.address.location_received": "Joylashuv qabul qilindi.",
    "telegram.address.saved_successfully": "✅ Manzil saqlandi!",
    "telegram.bottles.title": "Idishlarim",
    "telegram.bottles.no_balance": "Idish yo'q.",
    "telegram.bottles.load_error": "Yuklashda xatolik",
}


# ---------------------------------------------------------------------------
# Local scaffolding — nothing here re-implements production logic
# ---------------------------------------------------------------------------


class Rejection:
    """Script one Bot API endpoint to answer the way Telegram really answers.

    ``FakeTelegramTransport.fail()`` rejects EVERY call to an endpoint, which
    is wrong for the cases below: ``location_received`` sends two messages and
    only the second one carries ``parse_mode='Markdown'``, so a blanket
    sendMessage failure would test a different bug than the one in production.
    ``when`` narrows the rejection to the calls Telegram would actually refuse.

    It also records the exact params dict of each rejected call, because
    ``transport.shown`` records what the bot ATTEMPTED to show. A message
    Telegram refused is not a message the customer saw, and every assertion in
    this file about "what the customer got" has to tell those apart.
    """

    def __init__(self, transport, endpoint, description, *, when=None, status=400):
        self.transport = transport
        self.endpoint = endpoint
        self.description = description
        self.when = when
        self.status = status
        self.rejected: list[dict] = []
        transport.failures[endpoint] = self._respond

    def _respond(self, params):
        if self.when is not None and not self.when(params):
            return 200, {
                "ok": True,
                "result": self.transport._result_for(self.endpoint, params),
            }
        self.rejected.append(params)
        return self.status, {
            "ok": False,
            "error_code": self.status,
            "description": self.description,
        }


def reject(bot, endpoint, description, **kwargs) -> Rejection:
    return Rejection(bot.telegram, endpoint, description, **kwargs)


def delivered(bot, *rejections):
    """The messages the customer actually received, rejected attempts removed."""
    refused = [params for rejection in rejections for params in rejection.rejected]
    return [
        call
        for call in bot.telegram.shown
        if not any(call.params is params for params in refused)
    ]


def is_markdown(params) -> bool:
    return params.get("parse_mode") == "Markdown"


def fresh_tap(bot, callback_data, *, message_id=4242, age_seconds=5) -> Update:
    """A tap on a bubble Telegram would still let the bot DELETE.

    ``UpdateFactory.tap`` stamps every message with a fixed 2023 date, and the
    bot's own 47-hour deleteMessage policy guard
    (``ProfileHandlers._is_callback_message_deletable``) correctly declines to
    delete anything that old — so a deleteMessage rejection can never be
    observed through a factory-built tap. Tests that need the delete to be
    ATTEMPTED build the update here instead.
    """
    return Update.de_json(
        {
            "update_id": 99_000 + message_id,
            "callback_query": {
                "id": f"fresh-cb-{message_id}",
                "from": {
                    "id": DEFAULT_USER_ID,
                    "is_bot": False,
                    "first_name": "Kamola",
                    "language_code": "uz",
                },
                "chat_instance": "test-chat-instance",
                "data": callback_data,
                "message": {
                    "message_id": message_id,
                    "date": int(time.time()) - age_seconds,
                    "chat": {"id": DEFAULT_CHAT_ID, "type": "private"},
                    "from": {"id": 42, "is_bot": True, "first_name": "BlueStream"},
                    "text": "previous bot message",
                },
            },
        },
        bot.application.bot,
    )


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)

    # One product and a cart that behaves like the backend's: POST /cart/items
    # is an INCREMENT, which is exactly why a duplicate tap costs money.
    cart = {"cart_items": []}
    product = {
        "id": 7,
        "name": "Aqua Element 19L",
        "current_price": 25000,
        "inventory": {"min_order_quantity": 1, "stock_quantity": 50},
    }
    harness.backend.route(
        "GET", "/api/v1/products/7", lambda _c: {"data": {"product": product}}
    )
    harness.backend.route("GET", "/api/v1/cart", lambda _c: {"data": {"cart": cart}})

    def _add_item(call):
        line = next(
            (i for i in cart["cart_items"] if i["product_id"] == call.data["product_id"]),
            None,
        )
        if line is None:
            line = {"product_id": call.data["product_id"], "quantity": 0}
            cart["cart_items"].append(line)
        line["quantity"] += call.data["quantity"]
        return {"data": {"cart": cart}}

    def _set_item(call):
        for item in cart["cart_items"]:
            if item["product_id"] == 7:
                item["quantity"] = call.data["quantity"]
        return {"data": {"cart": cart}}

    harness.backend.route("POST", "/api/v1/cart/items", _add_item)
    harness.backend.route("PUT", "/api/v1/cart/items/7", _set_item)
    harness.cart = cart

    harness.backend.route(
        "GET",
        "/api/v1/orders/bottles/my-balances",
        lambda _c: {
            "data": overview_payload(
                [overview_balance_row(901, "Uy", 3), overview_balance_row(902, "Ish", -1)]
            )
        },
    )
    return harness


@pytest.fixture
def user(bot):
    return bot.updates()


# The group ``WaterBusinessBot.initialize()`` puts the dedup guard in. Pinned
# against bot.py itself by
# ``test_production_registers_the_dedup_guard_ahead_of_the_conversation_handlers``
# so this number cannot quietly drift away from production.
DEDUP_GROUP = -5


@pytest.fixture
def dedup_bot(bot):
    """The harness, which now carries the dedup guard production runs.

    The guard used to be registered in ``WaterBusinessBot.initialize()`` while
    the harness only ran ``_setup_handlers()``, so this fixture had to put it
    back by hand — a second copy of production wiring that could drift from the
    first. The registration moved into ``_setup_handlers()`` (2026-08-21), so
    the harness-built application has it by construction and this fixture is
    now just an alias kept for the tests below that read as "with the guard".
    """
    return bot


def backend_calls(bot, method, endpoint):
    return [c for c in bot.backend.calls if c.method == method and c.endpoint == endpoint]


async def open_pin_flow(bot, user, *, geocoded=MARKDOWN_SAFE_ADDRESS):
    """Enter the address flow and drop the pin, leaving the customer at the
    title step. This is the state from which an address is one tap away."""
    bot.backend.route(
        "POST",
        "/api/v1/addresses/reverse-geocode",
        lambda _c: {"data": {"formatted_address": geocoded}},
    )
    await bot.send(user.tap("add_new_address"))
    await bot.send(user.location(PIN_LAT, PIN_LNG))
    return bot.conversation_state("address_conversation")


# ---------------------------------------------------------------------------
# editMessageText -> "Bad Request: message is not modified"
# ---------------------------------------------------------------------------


async def test_a_not_modified_rejection_leaves_the_customers_message_alone(bot, user):
    """"Message is not modified" means the bubble ALREADY shows what we wanted
    to show. Treating it as a failure and falling back to delete-and-resend
    would destroy a message the customer is reading and replace it with an
    identical one — visible churn, and on a photo-hosted screen it loses the
    photo. The base handler must swallow this one and stop.
    """
    rejection = reject(bot, "editMessageText", "Bad Request: message is not modified")

    await bot.send(user.tap("my_bottles"))

    assert rejection.rejected, (
        "the bot never attempted the edit, so nothing was rejected and this "
        "test proved nothing"
    )
    assert bot.telegram.of("deleteMessage") == [], (
        "an unmodified message must not be deleted — the fallback path is for "
        "messages that could not be edited, not for ones that need no edit"
    )
    assert bot.telegram.of("sendMessage") == [], (
        "resending identical content leaves the customer with two copies"
    )


async def test_a_not_modified_rejection_mid_address_flow_leaves_the_flow_alive(
    bot, user
):
    """Was a RATCHET; the defect it pinned is fixed. Now the regression guard.

    ``ProfileHandlers._prompt_address_step`` used to call ``edit_message_text``
    bare, and ``skip_field_handler`` wraps the whole step in
    ``except Exception: return ConversationHandler.END``. So Telegram's most
    BENIGN rejection ended the conversation, and the customer was left looking
    at a correct-looking prompt whose Skip button was wired to nothing.

    WHAT THE FIX GUARANTEES: the prompt goes through
    ``BaseHandler._edit_or_replace_callback_message``, which already knows
    "message is not modified" is a success. A rendering rejection is no longer
    a flow event: the step advances to ADDRESS_FLOOR, and every button after it
    still works.
    """
    assert await open_pin_flow(bot, user) == ADDRESS_TITLE
    await bot.send(user.tap("addr_title_home"))
    assert bot.conversation_state("address_conversation") == ADDRESS_APARTMENT
    assert len(bot.backend.addresses) == 1

    reject(bot, "editMessageText", "Bad Request: message is not modified")
    await bot.send(user.tap("skip_apartment"))

    assert bot.conversation_state("address_conversation") == ADDRESS_FLOOR, (
        "a benign rendering rejection must not end the conversation"
    )
    # The one thing that was never lost, and must never become lost.
    assert len(bot.backend.addresses) == 1, (
        "the address created at the title step survives — creating early is "
        "what makes this failure survivable at all"
    )
    # And the proof the customer is not stranded: the next button they can see
    # still lands, still acks (so its spinner stops) and still advances.
    bot.telegram.reset()
    await bot.send(user.tap("skip_floor"))
    assert bot.conversation_state("address_conversation") == ADDRESS_DELIVERY_INSTRUCTIONS, (
        "the next Skip must still be wired to the flow"
    )
    assert bot.telegram.of("answerCallbackQuery"), (
        "the spinner on the Skip button has to stop"
    )


# ---------------------------------------------------------------------------
# editMessageText -> "Bad Request: message to edit not found"
# ---------------------------------------------------------------------------


async def test_an_edit_not_found_rejection_replaces_the_message_with_the_same_buttons(
    bot, user
):
    """The customer deleted the bot's bubble, or a double-tap deleted it under
    us. The screen must be rebuilt as a NEW message carrying the same text and
    the same buttons — a fallback that drops the keyboard leaves the customer
    reading a dead end.
    """
    rejection = reject(bot, "editMessageText", "Bad Request: message to edit not found")

    await bot.send(user.tap("my_bottles"))

    attempted = bot.telegram.of("editMessageText")[-1]
    received = delivered(bot, rejection)
    assert received, "the bot showed the customer nothing after the failed edit"

    replacement = received[-1]
    assert replacement.method == "sendMessage"
    assert replacement.text == attempted.params["text"], (
        "the replacement must carry the text the edit was going to show"
    )
    assert replacement.callback_data() == attempted.callback_data(), (
        "the replacement must carry the same buttons, or the screen is a dead end"
    )
    assert "Uy" in replacement.text and "Ish" in replacement.text, (
        "both places the backend returned must still be on screen"
    )


async def test_the_saved_address_confirmation_survives_a_rejected_final_edit(
    bot, user
):
    """Was a RATCHET; the defect it pinned is fixed. Now the regression guard.

    ``save_address_final`` writes the address and THEN confirms it. That
    confirmation used to be a bare ``query.edit_message_text``: when Telegram
    refused the edit the exception unwound into ``except Exception``, and the
    customer was never told the address was saved — they were left staring at
    the delivery-instructions prompt. They retry, and end up with a duplicate
    address.

    Data was always safe (the write already happened); the acknowledgement was
    not.

    WHAT THE FIX GUARANTEES: the confirmation goes through
    ``_edit_or_replace_callback_message``, so a refused edit is re-delivered as
    a fresh message carrying the same "address saved" text.
    """
    assert await open_pin_flow(bot, user) == ADDRESS_TITLE
    await bot.send(user.tap("addr_title_home"))
    await bot.send(user.tap("skip_apartment"))
    await bot.send(user.tap("skip_floor"))

    rejection = reject(bot, "editMessageText", "Bad Request: message to edit not found")
    bot.telegram.reset()
    await bot.send(user.tap("skip_delivery_instructions"))

    saved = list(bot.backend.addresses.values())
    assert len(saved) == 1 and saved[0]["latitude"] == PIN_LAT, (
        "the address must be on the server regardless of what Telegram did"
    )
    received = delivered(bot, rejection)
    assert received, "the save succeeded and the customer was told nothing at all"
    assert received[-1].method == "sendMessage"
    assert received[-1].text == TRANSLATIONS["telegram.address.saved_successfully"], (
        "the customer has to learn the address exists, or they file it again"
    )


# ---------------------------------------------------------------------------
# sendMessage -> "Bad Request: can't parse entities"
# ---------------------------------------------------------------------------


async def test_a_markdown_metacharacter_in_the_geocoded_address_is_escaped(bot, user):
    """A street name is data, not markup.

    ``location_received`` used to interpolate the reverse-geocoded address RAW
    into a message sent with ``parse_mode='Markdown'``. An address containing
    ``_``, ``*``, ``[`` or a backtick made Telegram refuse the whole message,
    and the bare ``except Exception`` turned that into
    ``ConversationHandler.END``: the customer shared their location, saw
    "Joylashuv qabul qilindi." and then NOTHING — no prompt, no error, no
    address — deterministically, for everyone living on that street.

    Fixed 2026-08-21 by escaping the interpolated address. This test fails if
    anyone interpolates a geocoder string into Markdown unescaped again.
    """
    bot.backend.route(
        "POST",
        "/api/v1/addresses/reverse-geocode",
        lambda _c: {"data": {"formatted_address": MARKDOWN_HOSTILE_ADDRESS}},
    )
    await bot.send(user.tap("add_new_address"))
    bot.telegram.reset()

    await bot.send(user.location(PIN_LAT, PIN_LNG))

    markdown_messages = [c for c in bot.telegram.shown if is_markdown(c.params)]
    assert markdown_messages, "the title prompt still goes out as Markdown"
    prompt = markdown_messages[-1]
    assert MARKDOWN_HOSTILE_ADDRESS not in prompt.text, (
        "the raw geocoder string reached Telegram unescaped — this is the "
        "exact payload that made it refuse the message"
    )
    assert "Chilonzor 15\\_A" in prompt.text, (
        f"the metacharacters must be escaped, got {prompt.text!r}"
    )
    assert bot.conversation_state("address_conversation") == ADDRESS_TITLE, (
        "the customer must reach the step that names the address"
    )


async def test_a_rejected_markdown_prompt_is_resent_as_plain_text(bot, user):
    """Backstop for the next unescapable thing Telegram objects to.

    Escaping fixes the hazard we know about. It cannot fix the one we do not,
    and the cost of being wrong here is total: the customer is left on a dead
    prompt with no way to self-rescue, because re-sharing the pin re-runs the
    same deterministic failure. So a refused Markdown prompt must be resent
    without ``parse_mode`` rather than ending the conversation.
    """
    await bot.send(user.tap("add_new_address"))
    rejection = reject(
        bot,
        "sendMessage",
        "Bad Request: can't parse entities: Can't find end of the entity "
        "starting at byte offset 31",
        when=is_markdown,
    )
    bot.telegram.reset()

    await bot.send(user.location(PIN_LAT, PIN_LNG))

    assert rejection.rejected, "the Markdown attempt is expected to be refused"
    survived = delivered(bot, rejection)
    assert any(MARKDOWN_SAFE_ADDRESS in call.text for call in survived), (
        "after the Markdown attempt was refused the customer must still be "
        f"shown the title prompt as plain text; they saw {[c.text for c in survived]}"
    )
    assert bot.conversation_state("address_conversation") == ADDRESS_TITLE, (
        "a formatting problem must not end the address flow"
    )


async def test_a_geocoded_address_without_metacharacters_reaches_the_title_step(
    bot, user
):
    """The control for the two tests above.

    Same rejection scripted, same flow, an address Telegram is happy to parse —
    and the flow completes. Without this, the two RATCHET tests above would
    still pass if the pin flow broke for some entirely different reason.
    """
    # Telegram only refuses the Markdown messages it cannot parse — here, the
    # ones carrying an underscore. Everything else goes through.
    rejection = reject(
        bot,
        "sendMessage",
        "Bad Request: can't parse entities",
        when=lambda params: is_markdown(params) and "_" in params.get("text", ""),
    )

    assert await open_pin_flow(bot, user, geocoded=MARKDOWN_SAFE_ADDRESS) == ADDRESS_TITLE
    assert rejection.rejected == []

    prompt = bot.telegram.last_shown()
    assert MARKDOWN_SAFE_ADDRESS in prompt.text, (
        "the geocoded address really is interpolated into the Markdown message "
        "— which is what makes the metacharacter case a live hazard"
    )
    assert "addr_title_home" in prompt.callback_data()


# ---------------------------------------------------------------------------
# answerCallbackQuery -> "Bad Request: query is too old"
# ---------------------------------------------------------------------------


async def test_a_too_old_callback_ack_still_lets_the_skip_step_do_its_work(bot, user):
    """Was a RATCHET; the defect it pinned is fixed. Now the regression guard.

    Telegram discards callback queries after ~60s and answers late acks with
    "query is too old and response timeout expired or query id is invalid".
    This is routine after a bot restart, when the pending-update backlog is
    redelivered.

    ``skip_field_handler`` used to ack FIRST (``await query.answer()`` on line
    1 of the body) and do the work second, so a rejected ack took the whole
    step with it: no state change, no prompt, no error.

    WHAT THE FIX GUARANTEES: every ack that has work behind it goes through
    ``BaseHandler._ack``, which treats a refused ack as what it is —
    cosmetic. The spinner may keep spinning; the step still advances to
    ADDRESS_FLOOR and the floor prompt is still rendered.
    """
    assert await open_pin_flow(bot, user) == ADDRESS_TITLE
    await bot.send(user.tap("addr_title_home"))
    assert bot.conversation_state("address_conversation") == ADDRESS_APARTMENT

    rejection = reject(
        bot,
        "answerCallbackQuery",
        "Bad Request: query is too old and response timeout expired or query "
        "id is invalid",
    )
    bot.telegram.reset()
    await bot.send(user.tap("skip_apartment"))

    assert rejection.rejected, (
        "the ack is expected to be attempted and refused, or this test proved "
        "nothing"
    )
    assert bot.conversation_state("address_conversation") == ADDRESS_FLOOR, (
        "a cosmetic ack failure must never cost the step the customer tapped for"
    )
    assert delivered(bot, rejection), "the customer must still see the next prompt"
    assert len(bot.backend.addresses) == 1, (
        "whatever else breaks, the address the customer already earned stays"
    )


async def test_a_too_old_ack_does_not_stop_the_dedup_middleware_dropping_a_duplicate(
    dedup_bot, user
):
    """The middleware answers the duplicate purely to dismiss its spinner, and
    wraps that ack in try/except for exactly this rejection. If a stale ack
    were allowed to escape, ``ApplicationHandlerStop`` would never be raised
    and the duplicate would reach the handler after all — turning a cosmetic
    Telegram error into a double charge.
    """
    reject(dedup_bot, "answerCallbackQuery", "Bad Request: query is too old")

    await dedup_bot.send(user.tap("add_to_cart_7"))
    await dedup_bot.send(user.tap("add_to_cart_7"))

    assert len(backend_calls(dedup_bot, "GET", "/api/v1/products/7")) == 1, (
        "the duplicate must still be stopped before the handler runs"
    )
    assert dedup_bot.cart["cart_items"] == [{"product_id": 7, "quantity": 1}]


async def test_a_stale_ack_on_my_bottles_still_renders_the_whole_screen(bot, user):
    """Incident: a rejected ack cost the customer the bottle screen entirely.

    ``BottleBalanceHandler.show_bottle_balance`` used to ack the tap BEFORE its
    ``try`` block:

        query = update.callback_query
        if query:
            await query.answer()
        try:
            ...

    So a rejected ack — routine when Telegram redelivers a backlog of taps
    after a deploy — escaped the handler entirely. Nothing was fetched, nothing
    was rendered. Production's global ``error_handler`` is no safety net here
    either: its one user-facing action is ``callback_query.answer(error_msg)``
    on the SAME dead query, which fails for the same reason.

    GUARANTEE: the ack is cosmetic (``handlers.base.BaseHandler._ack`` swallows
    the rejection), the balances are still fetched and the screen is still
    drawn.
    """
    rejection = reject(
        bot,
        "answerCallbackQuery",
        "Bad Request: query is too old and response timeout expired or query "
        "id is invalid",
    )

    await bot.send(user.tap("my_bottles"))

    assert rejection.rejected, "the ack this test is about was never attempted"
    assert len(backend_calls(bot, "GET", "/api/v1/orders/bottles/my-balances")) == 1, (
        "the handler must still ask the backend what the customer owns"
    )
    shown = delivered(bot, rejection)
    assert shown, "the customer got no screen at all"
    assert TRANSLATIONS["telegram.bottles.title"] in shown[-1].params["text"]
    # Both addresses from the fixture's overview, each with its History button.
    assert "Uy" in shown[-1].params["text"] and "Ish" in shown[-1].params["text"]


async def test_a_stale_ack_on_bottle_history_still_renders_the_page(bot, user):
    """The sibling handler had the identical pre-``try`` ack.

    ``show_bottle_history`` is reached from a button on the screen the test
    above proves survives, so fixing only one of the pair would leave the
    customer one tap further into the same dead end.
    """
    bot.backend.route(
        "GET",
        "/api/v1/orders/bottles/my-ledger/901",
        lambda _c: {"data": {"items": [], "total": 0, "page": 1, "per_page": 10}},
    )
    rejection = reject(
        bot,
        "answerCallbackQuery",
        "Bad Request: query is too old and response timeout expired or query "
        "id is invalid",
    )

    await bot.send(user.tap("bottle_history_901_1"))

    assert rejection.rejected, "the ack this test is about was never attempted"
    assert len(backend_calls(bot, "GET", "/api/v1/orders/bottles/my-ledger/901")) == 1
    assert delivered(bot, rejection), "the customer got no history page at all"


async def test_a_backend_500_reaches_the_customer_as_a_toast_and_leaves_the_screen(
    bot, user
):
    """The backend is down AND Telegram would refuse an edit. The customer must
    still be told, and their screen must survive.

    ``_handle_api_error`` delivers on answerCallbackQuery — a toast — and
    deliberately never touches the message, which is precisely why a broken
    edit cannot swallow the error. The assertion that the scripted edit
    rejection NEVER fires is the load-bearing half: it proves the error path
    does not depend on the call that is failing.
    """
    from tests.telegram_bot.ptb_harness import backend_failure

    bot.backend.route(
        "GET",
        "/api/v1/orders/bottles/my-balances",
        lambda _c: backend_failure("upstream exploded", status_code=500),
    )
    rejection = reject(bot, "editMessageText", "Bad Request: message to edit not found")

    await bot.send(user.tap("my_bottles"))

    toasts = [
        call.params.get("text")
        for call in bot.telegram.of("answerCallbackQuery")
        if call.params.get("text")
    ]
    assert toasts == ["\u274c Yuklashda xatolik"], (
        "the customer must get the localized load-error toast, not silence"
    )
    assert rejection.rejected == [], (
        "the error path must not route through editMessageText — if it starts "
        "doing so, a refused edit will start swallowing backend errors"
    )
    assert bot.telegram.of("sendMessage") == [], (
        "a failed read must not also replace the screen the customer was on"
    )


# ---------------------------------------------------------------------------
# deleteMessage failing
# ---------------------------------------------------------------------------


async def test_the_edit_and_delete_both_failing_still_delivers_the_text(bot, user):
    """The exact production warning PAIR that motivated the dedup middleware::

        Failed to edit callback message text ... Message to edit not found
        Failed to delete callback message before fallback send ... Message to
        delete not found

    Both arise from one deleted message. The fallback's ``reply_text`` is the
    last thing standing between the customer and a screen that never updates,
    so a failed delete must not abort it.
    """
    edit = reject(bot, "editMessageText", "Bad Request: message to edit not found")
    delete = reject(bot, "deleteMessage", "Bad Request: message to delete not found")

    await bot.send(user.tap("my_bottles"))

    assert edit.rejected and delete.rejected, (
        "both calls have to have been attempted AND refused, or this is not "
        "the production warning pair"
    )
    received = delivered(bot, edit)
    assert received, "the customer got nothing after both calls were refused"
    assert "Uy" in received[-1].text, "the bottle screen still has to arrive"


async def test_a_delete_rejection_when_opening_the_address_flow_still_shows_the_prompt(
    bot, user
):
    """``add_address`` deletes the menu bubble as cosmetic tidy-up before
    sending the location keyboard. Telegram refuses that delete for messages
    another admin already removed, or once it is out of the deletion window.
    Cosmetic cleanup must never cost the customer the prompt itself.
    """
    rejection = reject(bot, "deleteMessage", "Bad Request: message to delete not found")

    await bot.send(fresh_tap(bot, "add_new_address"))

    assert rejection.rejected, (
        "a recent bubble should have been deleted and refused; if this fails, "
        "the 47h policy guard or the fresh_tap helper has drifted"
    )
    assert bot.conversation_state("address_conversation") == ADDRESS_LOCATION
    shown = bot.telegram.last_shown()
    assert shown.method == "sendMessage" and shown.text, (
        "the customer must still be asked for their location"
    )
    assert shown.button_labels(), "the location-request keyboard must be attached"


async def test_the_bot_never_tries_to_delete_a_bubble_older_than_telegrams_limit(
    bot, user
):
    """Bot API deleteMessage only works for recent messages, so
    ``_is_callback_message_deletable`` refuses anything near 48h old. Calling
    it anyway would burn a request and log a warning on every single tap from
    an old chat — noise that buries the delete failures that DO matter.

    ``UpdateFactory.tap`` stamps 2023, which is exactly such a bubble.
    """
    reject(bot, "deleteMessage", "Bad Request: message to delete not found")

    await bot.send(user.tap("add_new_address"))

    assert bot.telegram.of("deleteMessage") == [], (
        "a bubble far outside the deletion window must not be sent to Telegram"
    )
    assert bot.conversation_state("address_conversation") == ADDRESS_LOCATION
    assert bot.telegram.last_shown().text, "the location prompt still goes out"


# ---------------------------------------------------------------------------
# The callback-dedup middleware (handlers/callback_dedup.py)
# ---------------------------------------------------------------------------


def test_production_registers_the_dedup_guard_ahead_of_the_conversation_handlers():
    """The guard is only a guard if it runs BEFORE the handlers it protects.

    ``ApplicationHandlerStop`` stops dispatch across the remaining groups, so a
    duplicate is only stopped when the middleware's group sorts ahead of the
    conversation handlers (group -2) and the plain callback handlers (group 0).
    Register it at 0 by accident and every duplicate is already through.

    This also pins the number ``DEDUP_GROUP`` above against production, so the
    ``dedup_bot`` fixture cannot go on testing a wiring that no longer exists.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "telegram_bot" / "bot.py"
    ).read_text(encoding="utf-8")

    registration = source.split("TypeHandler(Update, callback_dedup_middleware)", 1)
    assert len(registration) == 2, "the dedup middleware is no longer registered at all"
    assert f"group={DEDUP_GROUP}" in registration[1][:40], (
        f"the dedup guard is no longer registered at group {DEDUP_GROUP}"
    )
    assert "address_handler, group=-2" in source, (
        "conversation handlers moved; re-check that the dedup group still "
        "sorts ahead of them"
    )


async def test_setup_handlers_installs_the_dedup_guard_production_runs(bot, user):
    """The wiring lives in ONE place.

    ``WaterBusinessBot`` used to split handler registration across two methods:
    ``_setup_handlers()`` (everything a test harness builds) and
    ``initialize()`` (which additionally installed the debug logger and the
    callback-dedup guard). Anything that built a bot from ``_setup_handlers()``
    — this repo's PTB harness included — therefore drove a bot missing a
    production guard, and a double-tap regression was invisible to every
    dispatcher test.

    Moving the registration into ``_setup_handlers()`` closed that. If this
    test starts failing, the guard has been split back out and every dispatcher
    test in the repo has silently stopped exercising it.
    """
    installed = [
        handler
        for group, handlers in bot.application.handlers.items()
        for handler in handlers
        if getattr(handler, "callback", None) is callback_dedup.callback_dedup_middleware
        and group == DEDUP_GROUP
    ]
    assert len(installed) == 1, (
        "the callback-dedup guard must be registered exactly once, at group "
        f"{DEDUP_GROUP}, by _setup_handlers() — found {len(installed)}"
    )


async def test_an_impatient_double_tap_on_add_to_cart_runs_the_handler_once(
    dedup_bot, user
):
    """The bot acks inline taps at the END of the handler, so the spinner stays
    up for a second or two of backend round-trips and people tap again. With
    PTB's serial dispatch the second tap runs the handler a second time, and
    ``POST /cart/items`` is an INCREMENT on the backend — the customer is
    charged for water they did not order.

    Measured at the BACKEND, because "the handler ran once" is only meaningful
    as "the backend was told once".
    """
    await dedup_bot.send(user.tap("add_to_cart_7"))
    await dedup_bot.send(user.tap("add_to_cart_7"))

    assert len(backend_calls(dedup_bot, "GET", "/api/v1/products/7")) == 1, (
        "the second tap reached the handler"
    )
    posts = backend_calls(dedup_bot, "POST", "/api/v1/cart/items")
    assert [c.data for c in posts] == [{"product_id": 7, "quantity": 1}]
    assert dedup_bot.cart["cart_items"] == [{"product_id": 7, "quantity": 1}]


async def test_the_dropped_duplicate_tap_is_acknowledged_so_its_spinner_stops(
    dedup_bot, user
):
    """A dropped tap is the ONE callback no handler will ever ack, so the
    middleware has to do it. Miss this and the customer watches a loading
    spinner on the button until Telegram times it out — which reads as a hung
    bot and provokes a third tap.
    """
    first = user.tap("add_to_cart_7")
    await dedup_bot.send(first)
    acks_after_first = {c.params["callback_query_id"] for c in dedup_bot.telegram.of("answerCallbackQuery")}

    duplicate = user.tap("add_to_cart_7")
    await dedup_bot.send(duplicate)

    acked = {c.params["callback_query_id"] for c in dedup_bot.telegram.of("answerCallbackQuery")}
    assert duplicate.callback_query.id in acked - acks_after_first, (
        "the dropped duplicate's own query id was never answered"
    )


async def test_two_different_buttons_tapped_in_the_same_second_are_both_processed(
    dedup_bot, user
):
    """The lock is keyed on ``(user_id, callback_data)`` on purpose: a fast
    customer stepping product -> quantity -> cart taps three DIFFERENT buttons
    inside two seconds. Widening the key to "any callback from this user"
    would make the bot feel broken for exactly the people who use it most.
    """
    await dedup_bot.send(user.tap("add_to_cart_7"))
    await dedup_bot.send(user.tap("qty_inc_7_1"))

    assert len(backend_calls(dedup_bot, "GET", "/api/v1/products/7")) == 2, (
        "the second, DIFFERENT button must reach its handler"
    )
    assert dedup_bot.cart["cart_items"] == [{"product_id": 7, "quantity": 2}], (
        "the +1 really ran: the cart went 1 -> 2"
    )


async def test_a_deliberate_re_tap_after_the_dedup_window_is_processed_again(
    dedup_bot, user
):
    """Dedup is a debounce, not a one-shot. "Add another bottle" thirty seconds
    later is a real intent, and swallowing it silently is worse than the
    double-tap it was meant to prevent.

    Ages the module's own lock table rather than patching ``time.monotonic``:
    ``callback_dedup.time`` IS the stdlib module, so monkeypatching it there
    replaces the clock for the whole process — including
    ``asyncio.BaseEventLoop.time()`` — and any ``sleep``, JobQueue tick or
    ``wait_for`` inside the window would freeze with it.

    The offset comes from the module's own ``_DEDUP_TTL_SECONDS`` rather than a
    literal 2, so retuning the constant retunes the test with it.
    """
    await dedup_bot.send(user.tap("add_to_cart_7"))

    elapsed = callback_dedup._DEDUP_TTL_SECONDS + 0.5
    for key in list(callback_dedup._in_memory_locks):
        callback_dedup._in_memory_locks[key] -= elapsed

    await dedup_bot.send(user.tap("add_to_cart_7"))

    assert len(backend_calls(dedup_bot, "GET", "/api/v1/products/7")) == 2, (
        "a re-tap after the window must be honoured"
    )


async def test_a_double_tapped_conversation_entry_point_opens_the_flow_once(
    dedup_bot, user
):
    """"Add address" is a conversation ENTRY POINT with ``allow_reentry=True``,
    so a second tap does not just repeat the screen — it RE-ENTERS, wiping
    ``temp_address_data`` and stacking a second location keyboard on the
    customer. The dedup guard sits at group -5, ahead of the conversation
    handler at group -2, which is the only place that can stop it.
    """
    await dedup_bot.send(user.tap("add_new_address"))
    await dedup_bot.send(user.tap("add_new_address"))

    assert dedup_bot.conversation_state("address_conversation") == ADDRESS_LOCATION
    assert len(dedup_bot.telegram.of("sendMessage")) == 1, (
        "the customer must be asked for their location exactly once"
    )
