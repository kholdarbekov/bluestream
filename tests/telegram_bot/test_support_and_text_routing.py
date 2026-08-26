"""Where a customer's typed words go — and where they must never go.

The customer bot has exactly one place free text can land when no flow claims
it: the admin Support Inbox. ``WaterBusinessBot._capture_support_message``
POSTs it to ``/api/v1/support/messages`` silently, with no acknowledgement, so
an operator can reply from the admin UI. That is a deliberate, useful feature —
and it is also, right now, a leak.

THE LEAK THAT WAS (now closed, and guarded by the tests below)
--------------------------------------------------------------
``_setup_handlers`` registers the catch-all::

    MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text_message)

in the DEFAULT group (0), while every ConversationHandler lives in group -2.
PTB dispatches at most one handler PER GROUP — not one handler per update — so
while no conversation callback raised ``ApplicationHandlerStop``, a free-text
answer typed INSIDE a conversation was processed TWICE: once by the
conversation that asked for it, and once by the catch-all, which filed it as a
support message.

Production proof, 2026-08-20 23:07:12 (+05), telegram user 251067721: the
delivery-instructions text they typed both saved the address AND was POSTed to
``/api/v1/support/messages``.

The fix is ``WaterBusinessBot._consumes`` in telegram_bot/bot.py: it wraps a
conversation callback so the state it returns is re-raised as
``ApplicationHandlerStop``, which ``ConversationHandler.handle_update`` turns
back into the new conversation state before stopping cross-group dispatch. The
tests below are the regression guard — the leak is what they now assert must
NOT happen, and the genuine capture path is what they assert must still work.

WHY THROUGH THE DISPATCHER
--------------------------
``test_support_capture.py`` and ``test_support_concern_flow.py`` already prove
the handlers do the right thing when they are CALLED. Neither can see this bug,
because the bug is not in a handler — it is in which handlers PTB calls. So
every test here goes in through ``Application.process_update`` on the real
application built by the real ``_setup_handlers()``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

# Module-level, before anything below touches them, so `i18n`, `keyboards` and
# `config` resolve as the BOT's versions. See tests/telegram_bot/conftest.py.
import bot as bot_module
from handlers.profile import (
    ADDRESS_APARTMENT,
    ADDRESS_DELIVERY_INSTRUCTIONS,
    ADDRESS_FLOOR,
    ADDRESS_LOCATION,
    ADDRESS_TITLE,
)
from support_capture import MAX_SUPPORT_CONTENT

from tests.telegram_bot.ptb_harness import (
    FakeDatabase,
    backend_failure,
    build_bot_harness,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


SUPPORT_ENDPOINT = "/api/v1/support/messages"

# The pin from the traced production sessions; inside TASHKENT_POLYGON so the
# real delivery-zone SSOT accepts it.
PIN_LAT = 41.32354
PIN_LNG = 69.241036

# The order behind the delivered summary whose "Report an issue" button arms
# the concern flow (telegram_bot/webhook_server.py renders
# ``report_issue_<order_id>``).
ORDER_ID = 555
ORDER_NUMBER = "ORD-2026-0555"

# Real seeded copy (scripts/seed_bottle_ledger_translations.py and
# scripts/seed_backend_translations.py). Assertions below compare against these
# exact strings, because "the customer was told something" is not the same
# claim as "the customer was told the right thing".
TRANSLATIONS = {
    "telegram.support.describe_issue_prompt": (
        "Iltimos, #{order_number}-buyurtma bo'yicha muammoni yozib yuboring."
    ),
    "telegram.support.cancel_button": "Bekor qilish",
    "telegram.support.cancelled": "Bekor qilindi.",
    "telegram.support.send_failed": (
        "Kechirasiz, xabaringizni yuborib bo'lmadi. Iltimos, qaytadan urinib ko'ring."
    ),
    ("uz", "telegram.support.ack"): "✅ Rahmat! Xabaringiz qo'llab-quvvatlash guruhiga yuborildi.",
    ("ru", "telegram.support.ack"): "✅ Спасибо! Ваше сообщение отправлено в службу поддержки.",
    "telegram.support.menu_coming_soon": "🆘 Yordam menyusi tez orada ishga tushadi!",
    "telegram.support.faq_coming_soon": "❓ Tez-tez so'raladigan savollar tez orada qo'shiladi!",
    "telegram.support.contact_message": "📞 Yordam bilan bog'lanish: @aqua_element_support",
    "telegram.help.command_hint": "🆘 Yordam: Mavjud bo'limlarni ko'rish uchun /menu dan foydalaning.",
    "telegram.back": "⬅️ Ortga",
    "telegram.main_menu": "Asosiy menyu",
    "telegram.address.title_prompt": "Manzilga nom bering:",
    "telegram.address.detected_location_prefix": "📍 *Aniqlangan joylashuv:*\n{address}\n\n",
}


class StatefulDatabase(FakeDatabase):
    """``FakeDatabase`` that actually SERVES the bot_state it stores.

    ``BotUserRepository.get_user_state`` reads ``SELECT bot_state FROM users``
    through ``fetchval``, and the shared fake answers that query with ``None``
    — i.e. "no flow armed", for every customer, always. The concern flow lives
    entirely in that column, so without this override every test below would
    drive an unarmed bot and quietly assert nothing.

    Not a re-implementation of production logic: the read is wired to the same
    dict the production write path (``execute``) already updates in the parent.
    """

    async def fetchval(self, query, *args):
        if "bot_state" in query:
            return self.user.get("bot_state")
        return await super().fetchval(query, *args)


def _reconnect_the_rate_limiter_in_this_event_loop(monkeypatch):
    """Force the REAL ``RateLimiter`` to open a fresh Redis connection.

    ``utils.rate_limiter`` is a module-level singleton that memoises its
    ``redis.asyncio`` client, and that client is bound to the event loop it was
    created on. Every test here gets a NEW loop, so the second test in a worker
    hits "attached to a different loop", the limiter marks Redis dead, and —
    because it FAILS CLOSED by design — starts denying every message. The
    symptom is a test that passes alone and, run with its neighbours, sees the
    customer told "rate limit exceeded" instead of the behaviour it asserts.

    Clearing the memo is not a stub: the production sliding-window logic still
    runs, against real Redis, exactly as `_handle_text_message` calls it.
    """
    import utils as utils_module

    monkeypatch.setattr(utils_module.rate_limiter, "_redis", None)
    monkeypatch.setattr(utils_module.rate_limiter, "_redis_available", False)
    monkeypatch.setattr(utils_module.rate_limiter, "_last_connect_attempt", None)


@pytest.fixture
async def bot(monkeypatch):
    _reconnect_the_rate_limiter_in_this_event_loop(monkeypatch)
    harness = await build_bot_harness(
        monkeypatch, translations=TRANSLATIONS, database=StatefulDatabase()
    )
    harness.backend.route(
        "GET",
        f"/api/v1/orders/{ORDER_ID}",
        lambda _c: {"data": {"order": {"id": ORDER_ID, "order_number": ORDER_NUMBER}}},
    )
    return harness


@pytest.fixture
def user(bot, request):
    """An update factory for a customer id unique to THIS test.

    ``_handle_text_message`` runs the REAL Redis-backed ``RateLimiter`` (30
    messages / 60s / user), and the harness does not stub it. Every test in
    this file sending text as the same customer would share one window, so a
    later test could start seeing "rate limit exceeded" instead of the
    behaviour it is asserting — a failure that appears only when the whole file
    runs, and only sometimes. Deriving the id from the node id keeps it unique,
    stable across runs, and identical no matter which xdist worker picks the
    test up.
    """
    digest = hashlib.sha256(request.node.nodeid.encode("utf-8")).hexdigest()[:8]
    telegram_id = 770_000_000 + int(digest, 16) % 1_000_000
    return bot.updates(user_id=telegram_id, chat_id=telegram_id)


# ---------------------------------------------------------------------------
# Reading what happened
# ---------------------------------------------------------------------------


def support_posts(bot) -> list[dict]:
    """The payloads that reached the admin Support Inbox, in order."""
    return [
        call.data
        for call in bot.backend.calls
        if call.method == "POST" and call.endpoint == SUPPORT_ENDPOINT
    ]


def filed_contents(bot) -> list[str]:
    return [payload["content"] for payload in support_posts(bot)]


def address_state(bot, user):
    return bot.conversation_state(
        "address_conversation", chat_id=user.chat_id, user_id=user.user_id
    )


async def walk_to_delivery_instructions(bot, user):
    """The real journey to the one free-text step the production trace hit."""
    await bot.send(user.tap("add_new_address"))
    assert address_state(bot, user) == ADDRESS_LOCATION
    await bot.send(user.location(PIN_LAT, PIN_LNG))
    assert address_state(bot, user) == ADDRESS_TITLE
    await bot.send(user.tap("addr_title_home"))
    assert address_state(bot, user) == ADDRESS_APARTMENT
    await bot.send(user.tap("skip_apartment"))
    assert address_state(bot, user) == ADDRESS_FLOOR
    await bot.send(user.tap("skip_floor"))
    assert address_state(bot, user) == ADDRESS_DELIVERY_INSTRUCTIONS


async def arm_the_concern_flow(bot, user, order_id=ORDER_ID):
    """Tap the delivered summary's 'Report an issue' button."""
    await bot.send(user.tap(f"report_issue_{order_id}"))
    return bot.telegram.last_shown()


# ===========================================================================
# The double dispatch. Fixed 2026-08-22; these are its regression guards.
# ===========================================================================


async def test_a_typed_delivery_instruction_is_never_filed_as_a_support_ticket(bot, user):
    """Regression guard for the production defect of 2026-08-20 23:07:12 (+05).

    Telegram user 251067721 was standing in the address flow at the
    delivery-instructions step and typed one sentence. It did two things: the
    conversation saved it onto the address (correct), and the group-0 catch-all
    ALSO filed it in the admin Support Inbox as an unsolicited customer message
    (wrong). An operator then saw a support ticket that read like a delivery
    note, from a customer who never asked for support and would never be
    replied to about it.

    WHY IT HAPPENED: `_setup_handlers` registers
    `MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text_message)`
    in the DEFAULT group (0); the ConversationHandlers live in group -2; and no
    conversation callback raised `ApplicationHandlerStop`. PTB runs at most one
    handler per GROUP, so both groups got the update.

    WHAT THE FIX GUARANTEES: every free-text step of a conversation is wrapped
    in `WaterBusinessBot._consumes`, which re-raises the state the callback
    returned as `ApplicationHandlerStop`. An answer a conversation asked for
    stops in group -2 — the address is still saved, and the Support Inbox never
    hears about it.
    """
    await walk_to_delivery_instructions(bot, user)

    instructions = "Eshik oldiga qo'ying, kelganda qo'ng'iroq qiling"
    await bot.send(user.text(instructions))

    # The half that was always correct and has to survive the fix.
    (address,) = bot.backend.addresses.values()
    assert address["delivery_instructions"] == instructions, (
        "the conversation still has to save what the customer typed"
    )

    assert support_posts(bot) == [], (
        "the sentence the address flow asked for must not also become a "
        "support ticket an operator has to triage and can never answer"
    )


async def test_the_apartment_number_a_customer_types_stays_out_of_the_inbox(bot, user):
    """The same defect, one step earlier, and worse.

    The leak was not specific to delivery instructions: EVERY free-text step of
    the address flow was double dispatched. An apartment number is the
    customer's home address, and it ended up in a support queue that operators
    read and reply from.

    WHAT THE FIX GUARANTEES: nothing typed inside the address conversation
    reaches `/api/v1/support/messages`, and the flow still advances on it.
    """
    await bot.send(user.tap("add_new_address"))
    await bot.send(user.location(PIN_LAT, PIN_LNG))
    await bot.send(user.tap("addr_title_home"))
    assert address_state(bot, user) == ADDRESS_APARTMENT

    await bot.send(user.text("45"))

    (address,) = bot.backend.addresses.values()
    assert address["apartment_number"] == "45", "the conversation consumed it"
    assert address_state(bot, user) == ADDRESS_FLOOR, "and advanced the flow"
    assert support_posts(bot) == [], (
        "the flat number is the customer's home address; it must never be "
        "filed as a support message"
    )


async def test_starting_the_address_flow_leaves_a_pending_concern_report_armed(
    bot, user
):
    """Was a RATCHET; the second half of that defect is now fixed too.

    Arming the concern flow is DB-backed, so it survives until the customer
    sends something or cancels. But `ProfileHandlers.add_address` opened with
    `update_user_state(user_id, {})` — "clear any pending database state before
    starting address flow" — which wiped the arming with no notice at all,
    while the prompt and its Cancel button stayed on the customer's screen
    still saying a report was open. A customer who tapped "Report an issue",
    wandered off to add an address, and came back to type their complaint filed
    nothing and was told nothing.

    The half fixed earlier: a sentence typed INSIDE the address flow is no
    longer ALSO filed in the Support Inbox as an unprefixed, unroutable note —
    `_consumes` stops it in group -2.

    WHAT THIS FIX GUARANTEES: `add_address` no longer clears `bot_state` at
    all. That clear was a guard against an armed `awaiting_input` eating the
    address flow's typed answers, and `_consumes` now makes that structurally
    impossible — so the guard is obsolete while its damage was not. The concern
    survives the detour, and the complaint the customer types once the address
    is saved is filed against the right order and acknowledged.
    """
    await arm_the_concern_flow(bot, user)

    await walk_to_delivery_instructions(bot, user)
    assert json.loads(bot.database.user["bot_state"]) == {
        "awaiting_input": "support_message",
        "support_order_id": ORDER_ID,
        "support_order_number": ORDER_NUMBER,
        "support_armed_at": json.loads(bot.database.user["bot_state"])["support_armed_at"],
    }, "entering the address flow must not wipe another flow's armed state"

    bot.telegram.reset()
    instructions = "Eshik oldiga qo'ying"
    await bot.send(user.text(instructions))

    (address,) = bot.backend.addresses.values()
    assert address["delivery_instructions"] == instructions, (
        "the conversation still saves what the customer typed"
    )
    assert support_posts(bot) == [], (
        "a delivery note must not be filed in the Support Inbox with no order "
        "reference and no way to tell it apart from a genuine message"
    )

    # The address flow is over. The report the customer armed before it is
    # still armed, so the complaint they type now goes where they meant it to.
    complaint = "Suv idishi ochilgan holda keldi"
    await bot.send(user.text(complaint))

    assert filed_contents(bot) == [f"[Order #{ORDER_NUMBER}] {complaint}"], (
        "the concern the customer armed before the detour must still be filed "
        "against its order"
    )
    assert TRANSLATIONS[("uz", "telegram.support.ack")] in [
        call.text for call in bot.telegram.shown
    ], "and the customer must be told it went somewhere"


async def test_a_conversation_answer_is_processed_by_the_conversation_alone(bot, user):
    """The MECHANISM at the dispatcher, not just the symptom at the backend.

    The two tests above assert that nothing reaches `/api/v1/support/messages`.
    A fix that merely filtered inside `_capture_support_message` would satisfy
    them while leaving the real problem in place: the catch-all would still RUN
    on every keystroke of every conversation, spending a rate-limit token, a
    `user_middleware` call and a `bot_state` read on text that was never meant
    for it — and any future side effect added there would leak all over again.

    So this test watches the group-0 slot itself. The catch-all still MATCHES
    the update (it matches all free text — that is what makes it a catch-all);
    what must never happen is that PTB reaches it, because the conversation
    step raised `ApplicationHandlerStop` first.
    """
    await walk_to_delivery_instructions(bot, user)

    typed = user.text("Eshik oldiga qo'ying")
    matched = bot.handlers_matching(typed)

    groups = [group for group, _handler in matched]
    assert groups == [-2, 0], (
        "both handlers still MATCH — the fix is about dispatch, not matching; "
        f"got {groups}"
    )

    catch_all = matched[-1][1]
    assert catch_all.callback.__func__ is bot_module.WaterBusinessBot._handle_text_message, (
        "the group-0 claimant is the support catch-all itself"
    )

    # Instrument the registered group-0 slot rather than the class: the
    # catch-all was bound at `_setup_handlers()` time, so patching
    # `WaterBusinessBot._handle_text_message` now would be a no-op that looked
    # like evidence. The real callback is still called through, so a regression
    # shows up here as "it ran" and not as a missing side effect elsewhere.
    entered = []
    real_callback = catch_all.callback

    async def _record_then_run(update, context):
        entered.append(update.message.text)
        return await real_callback(update, context)

    catch_all.callback = _record_then_run
    try:
        await bot.send(typed)
    finally:
        catch_all.callback = real_callback

    assert entered == [], (
        "the group-0 catch-all ran on text the address conversation had "
        "already consumed — ApplicationHandlerStop is not reaching PTB"
    )


def test_the_conversations_stop_dispatch_before_the_default_group_catch_all():
    """The source-level half of the contract the tests above check at runtime.

    The wiring that made the leak possible is still there and is meant to be:
    the catch-all belongs in the default group, BEHIND the conversations,
    because free text with no flow open really is a support message. What holds
    the two apart is `WaterBusinessBot._consumes` — the single place the rule
    is expressed. Asserting it here means a future edit that unwraps one
    conversation step (or drops the import) fails loudly instead of quietly
    reopening the inbox leak for that one step, which is exactly how this
    defect reached production.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "telegram_bot" / "bot.py"
    ).read_text(encoding="utf-8")

    registration = source.split(
        "MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text_message)", 1
    )
    assert len(registration) == 2, "the free-text catch-all is no longer registered"
    tail = registration[1][:60]
    assert "group=" not in tail, (
        "the catch-all now names a group; it is supposed to stay in the "
        "default group, behind the conversations"
    )
    assert "address_handler, group=-2" in source, (
        "the conversations moved out of group -2; re-check this whole file"
    )
    imports = source.split("from telegram.ext import", 1)[1].split(")", 1)[0]
    assert "ApplicationHandlerStop" in imports, (
        "bot.py no longer imports ApplicationHandlerStop — nothing can be "
        "stopping cross-group dispatch, so every conversation answer is being "
        "filed as a support message again"
    )

    # Every free-text step of every conversation, named. A new one added
    # without `_consumes` is a new leak, and nothing else would notice.
    for callback in (
        "profile_handlers.phone_text_received",
        "profile_handlers.link_account_otp",
        "profile_handlers.register_otp_received",
        "profile_handlers.skip_location_sharing",
        "profile_handlers.cancel_address_text",
        "profile_handlers.address_title_received",
        "profile_handlers.street_received",
        "profile_handlers.building_received",
        "profile_handlers.apartment_received",
        "profile_handlers.floor_received",
        "profile_handlers.delivery_instructions_received",
        "profile_handlers.phone_verify_text_received",
        "profile_handlers.phone_verify_name_received",
    ):
        assert f"self._consumes({callback})" in source, (
            f"{callback} is a free-text conversation step registered without "
            "_consumes — whatever the customer types into it is filed in the "
            "admin Support Inbox as well"
        )


# ===========================================================================
# The genuine catch-all: free text with NO flow active. This part is CORRECT.
# ===========================================================================


async def test_an_unprompted_question_is_filed_for_an_operator_and_never_auto_answered(
    bot, user
):
    """The whole point of the catch-all, and the reason it must not fire inside
    a flow.

    A customer who types "salom, suv bormi?" out of the blue has no flow open.
    Their words go to the Support Inbox verbatim, and the bot says nothing —
    an auto-reply would read as an answer and stop them waiting for the real
    one an operator is about to send.
    """
    question = "Salom, ertaga ertalab suv yetkazib bera olasizmi?"

    await bot.send(user.text(question))

    assert support_posts(bot) == [{"content": question, "message_type": "text"}], (
        "the exact words the customer typed must reach the inbox unaltered"
    )
    assert bot.telegram.shown == [], (
        "the capture is silent by design; any reply here is a robot answering "
        "a human question"
    )


async def test_the_words_are_filed_exactly_as_typed_including_the_leading_spaces_stripped(
    bot, user
):
    """`_handle_text_message` strips the message before capturing it, so what
    an operator reads is the sentence and not the customer's stray whitespace —
    while everything INSIDE the sentence, punctuation and emoji included, is
    left alone.
    """
    await bot.send(user.text("   Idishni qaytarib olasizmi? 🙏   "))

    assert filed_contents(bot) == ["Idishni qaytarib olasizmi? 🙏"]


async def test_a_backend_500_on_capture_never_reaches_the_customer(bot, user):
    """The inbox is a convenience for the operator, not a promise to the
    customer. If the backend rejects the write the customer must not be shown
    an error for a message they never asked us to send anywhere — and the bot
    must not fall over, because the next update is someone else's order.
    """
    bot.backend.route(
        "POST", SUPPORT_ENDPOINT, lambda _c: backend_failure("inbox down", status_code=500)
    )

    await bot.send(user.text("Salom"))

    assert support_posts(bot) == [{"content": "Salom", "message_type": "text"}], "it was attempted"
    assert bot.telegram.shown == [], (
        "a failed silent capture stays silent — an error toast here would be "
        "about a request the customer never made"
    )

    # And the bot is still alive for the next customer action.
    bot.telegram.reset()
    await bot.send(user.tap("menu_support"))
    assert bot.telegram.shown, "the bot must keep serving after a failed capture"


async def test_a_question_from_a_customer_who_cannot_be_authenticated_vanishes_without_a_word(
    bot, user, monkeypatch
):
    """The quiet half of a silent feature.

    `_capture_support_message` needs a user token, and gets one either from the
    cached TokenManager or by logging the customer back in. When both fail — a
    blocked account, an auth outage, a customer whose telegram-login the
    backend rejects — production logs "Support capture skipped: no auth token"
    and returns. Nothing is stored for an operator, and nothing on the
    customer's screen says so; they are left waiting for a reply to a message
    that was never filed.

    This is CURRENT, DELIBERATE behaviour (the capture is unsolicited, so an
    error toast about it would confuse more than it helps), and it is pinned
    here because it is the only place that states what the customer sees on the
    day support capture stops working.
    """
    token_manager = bot.application.bot_data["token_manager"]

    async def _no_cached_token(*_args, **_kwargs):
        return None

    monkeypatch.setattr(token_manager, "get_valid_token", _no_cached_token)
    bot.backend.route(
        "POST",
        "/api/v1/auth/telegram-login",
        lambda _c: backend_failure("account blocked", status_code=403),
    )

    await bot.send(user.text("Nega buyurtmam kelmadi?"))

    logins = [
        c
        for c in bot.backend.calls
        if c.method == "POST" and c.endpoint == "/api/v1/auth/telegram-login"
    ]
    assert logins, "the bot must at least try to authenticate before giving up"
    assert support_posts(bot) == [], (
        "an unauthenticated capture must not be POSTed with no token — the "
        "backend would 401 and the message would be lost anyway"
    )
    assert bot.telegram.shown == [], (
        "CURRENT BEHAVIOUR: the customer is told nothing at all, so from their "
        "side the question simply disappeared"
    )


# ===========================================================================
# /commands must never be filed
# ===========================================================================


async def test_a_slash_command_typed_mid_conversation_is_not_filed_as_a_support_ticket(
    bot, user
):
    """`filters.TEXT & ~filters.COMMAND` is load-bearing.

    A customer who loses patience in the address flow and types /menu is
    navigating, not complaining. Filing it would fill the Support Inbox with
    "/menu" tickets — and unlike the free-text leak above, this one would be
    indistinguishable from noise, so nobody would ever notice it started.
    """
    await walk_to_delivery_instructions(bot, user)
    bot.telegram.reset()

    command = user.command("menu")
    assert bot.handlers_matching(command), "/menu has to reach the menu handler"

    await bot.send(command)

    assert support_posts(bot) == [], "a command is not a support message"
    assert bot.telegram.shown, "and the customer must still get their menu"
    assert address_state(bot, user) == ADDRESS_DELIVERY_INSTRUCTIONS, (
        "the address flow is not a fallback for /menu, so the customer is "
        "still parked in it — the prompt they abandoned is still live"
    )


async def test_the_help_command_opens_help_instead_of_filing_a_ticket(bot, user):
    """/help with no flow open: the one command a confused customer is most
    likely to type is also the one most likely to be mistaken for a support
    message. It must answer, not file."""
    await bot.send(user.command("help"))

    assert support_posts(bot) == []
    assert bot.telegram.last_shown().text == TRANSLATIONS["telegram.help.command_hint"]
    assert "back_to_main" in bot.telegram.last_shown().callback_data(), (
        "a dead-end help screen is how customers end up typing free text at us"
    )


# ===========================================================================
# The support menu surfaces
# ===========================================================================


async def test_the_support_menu_opens_and_offers_a_way_back(bot, user):
    """A menu with no exit is a trap: the only thing left to tap is the
    keyboard, and whatever the customer then types becomes a support ticket."""
    await bot.send(user.tap("menu_support"))

    shown = bot.telegram.last_shown()
    assert shown.text == TRANSLATIONS["telegram.support.menu_coming_soon"]
    assert shown.callback_data() == ["back_to_main"]
    assert shown.button_labels() == [TRANSLATIONS["telegram.back"]], (
        "the exit has to be labelled in the customer's language, not left as a "
        "raw translation key"
    )


async def test_every_button_the_support_menu_renders_is_answered_by_a_handler(bot, user):
    """A tap with no matching handler shows a spinner and then nothing."""
    await bot.send(user.tap("menu_support"))

    for data in bot.telegram.last_shown().callback_data():
        assert bot.handlers_matching(user.tap(data)), (
            f"the '{data}' button on the support menu lands nowhere"
        )


async def test_back_from_the_support_menu_returns_the_customer_to_the_main_menu(bot, user):
    """The escape has to actually escape."""
    await bot.send(user.tap("menu_support"))
    bot.telegram.reset()

    await bot.send(user.tap("back_to_main"))

    shown = bot.telegram.last_shown()
    assert shown.text == TRANSLATIONS["telegram.main_menu"]
    assert "menu_support" in shown.callback_data(), (
        "the main menu must still offer the way back into support"
    )


async def test_the_contact_and_faq_screens_answer_with_real_copy(bot, user):
    """Both are one tap from the support menu, and both used to be the only
    place a customer could find a human. A screen that renders a raw
    translation key is worse than no screen."""
    for data, key in (
        ("contact_support", "telegram.support.contact_message"),
        ("faq", "telegram.support.faq_coming_soon"),
    ):
        bot.telegram.reset()
        await bot.send(user.tap(data))
        shown = bot.telegram.last_shown()
        assert shown.text == TRANSLATIONS[key], f"{data} rendered {shown.text!r}"
        assert "back_to_main" in shown.callback_data(), f"{data} is a dead end"


# ===========================================================================
# The guided concern flow (handlers/support.py)
# ===========================================================================


async def test_report_an_issue_arms_the_flow_and_names_the_order(bot, user):
    """The button lives on the delivered summary the webhook server sends, so
    the customer taps it hours after ordering. Naming the order in the prompt
    is what tells them WHICH delivery they are complaining about — and the
    prompt is a NEW message, so the summary and its button stay tappable if
    anything later goes wrong.
    """
    prompt = await arm_the_concern_flow(bot, user)

    assert prompt.method == "sendMessage", (
        "the prompt must not replace the delivered summary the button sits on"
    )
    assert ORDER_NUMBER in prompt.text, (
        f"the customer must see which order this is about; got {prompt.text!r}"
    )
    assert prompt.callback_data() == ["support_cancel"]
    assert prompt.button_labels() == [TRANSLATIONS["telegram.support.cancel_button"]]

    assert bot.handlers_matching(user.tap("support_cancel")), (
        "the Cancel button on the prompt must be wired, or the only way out of "
        "an armed flow is to type something that becomes a ticket"
    )


async def test_the_concern_reaches_the_inbox_tagged_with_the_order_and_is_acknowledged(
    bot, user
):
    """The whole reason this flow exists instead of the silent catch-all: an
    operator opening the ticket must see the order number, and the customer
    must be told their complaint was actually sent."""
    await arm_the_concern_flow(bot, user)
    bot.telegram.reset()

    await bot.send(user.text("Idish yorilgan holda keldi"))

    assert filed_contents(bot) == [f"[Order #{ORDER_NUMBER}] Idish yorilgan holda keldi"], (
        "an unprefixed concern is a ticket an operator cannot act on"
    )
    assert bot.telegram.last_shown().text == TRANSLATIONS[("uz", "telegram.support.ack")], (
        "unlike the silent catch-all, a concern the customer deliberately "
        "reported must be acknowledged"
    )


async def test_the_next_message_after_a_concern_is_filed_silently_again(bot, user):
    """The arming is one-shot. If the state survived, every later message would
    be tagged with a stale order number and acknowledged as if it were a new
    complaint about a delivery the customer has stopped thinking about."""
    await arm_the_concern_flow(bot, user)
    await bot.send(user.text("Idish yorilgan holda keldi"))
    bot.telegram.reset()

    await bot.send(user.text("Rahmat"))

    assert filed_contents(bot)[-1] == "Rahmat", "no stale order prefix"
    assert bot.telegram.shown == [], "and no second acknowledgement"


async def test_a_concern_typed_half_an_hour_late_loses_its_order_and_its_acknowledgement(
    bot, user
):
    """The customer taps Report, gets distracted, and writes back much later.

    `SupportHandlers._is_stale` treats arming older than 30 minutes as a
    reference it can no longer trust, so the message is filed UNPREFIXED and
    the customer gets no acknowledgement — it degrades into the plain silent
    capture. That is deliberate (a wrong order number on a complaint routes an
    operator to the wrong delivery), but it means the one customer who took
    their time composing a careful complaint is the one who is never thanked
    for it.

    The age is set by editing the stored `bot_state` — the same column
    `update_user_state` wrote — rather than by re-implementing the staleness
    rule, which stays entirely in production code.
    """
    await arm_the_concern_flow(bot, user)

    armed_state = json.loads(bot.database.user["bot_state"])
    assert armed_state["awaiting_input"] == "support_message", "the flow is armed"
    armed_state["support_armed_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=31)
    ).isoformat()
    bot.database.user["bot_state"] = json.dumps(armed_state)

    bot.telegram.reset()
    await bot.send(user.text("Kecha kelgan idish yorilgan edi"))

    assert filed_contents(bot) == ["Kecha kelgan idish yorilgan edi"], (
        "a half-hour-old arming must not stamp an order number it can no "
        "longer vouch for onto the message"
    )
    assert bot.telegram.shown == [], (
        "and the acknowledgement goes with it — the customer is not told their "
        "complaint reached anyone"
    )
    assert bot.database.user["bot_state"] == "{}", (
        "the stale arming must be cleared, or every later message would take "
        "this same silent path"
    )


async def test_cancelling_the_report_disarms_it_so_the_next_message_is_not_a_complaint(
    bot, user
):
    """The customer taps Report, reads the prompt, changes their mind. If
    Cancel only edited the message and left the state armed, the next thing
    they typed — about anything at all — would be filed as a complaint about
    that order and acknowledged as one."""
    await arm_the_concern_flow(bot, user)
    bot.telegram.reset()

    await bot.send(user.tap("support_cancel"))
    assert bot.telegram.last_shown().text == TRANSLATIONS["telegram.support.cancelled"]

    bot.telegram.reset()
    await bot.send(user.text("Ertaga yana buyurtma beraman"))

    assert filed_contents(bot) == ["Ertaga yana buyurtma beraman"], (
        "the cancelled order must not be stamped onto an unrelated message"
    )
    assert bot.telegram.shown == [], "and a silent capture stays silent"


async def test_an_impatient_double_tap_on_report_an_issue_prompts_once(bot, user):
    """The prompt takes a backend round-trip to resolve the order number, so
    the spinner sits there and people tap again. Two prompts means two Cancel
    buttons, and the customer cancelling the first one disarms a flow the
    second one is still telling them is open."""
    await bot.send(user.tap(f"report_issue_{ORDER_ID}"))
    await bot.send(user.tap(f"report_issue_{ORDER_ID}"))

    prompts = [c for c in bot.telegram.shown if ORDER_NUMBER in c.text]
    assert len(prompts) == 1, "the duplicate tap must be debounced"

    lookups = [
        c
        for c in bot.backend.calls
        if c.method == "GET" and c.endpoint == f"/api/v1/orders/{ORDER_ID}"
    ]
    assert len(lookups) == 1, "and it must not cost a second backend round-trip"


async def test_a_failed_order_lookup_still_arms_the_flow_with_the_raw_id(bot, user):
    """The order fetch is a nicety; the concern is not. If the backend is
    having a bad minute the customer must still be able to report the problem
    — which is exactly the minute they are most likely to want to."""
    bot.backend.route(
        "GET",
        f"/api/v1/orders/{ORDER_ID}",
        lambda _c: backend_failure("orders unavailable", status_code=500),
    )

    prompt = await arm_the_concern_flow(bot, user)
    assert str(ORDER_ID) in prompt.text

    bot.telegram.reset()
    await bot.send(user.text("Yetkazib berilmadi"))

    assert filed_contents(bot) == [f"[Order #{ORDER_ID}] Yetkazib berilmadi"], (
        "the raw id is still a reference an operator can resolve"
    )
    assert bot.telegram.last_shown().text == TRANSLATIONS[("uz", "telegram.support.ack")]


async def test_a_rejected_concern_says_so_instead_of_falsely_thanking_the_customer(
    bot, user
):
    """A false acknowledgement is the worst outcome here: the customer stops
    chasing a complaint that was never recorded, and the delivered summary's
    Report button is the only way back — which they have no reason to tap."""
    bot.backend.route(
        "POST", SUPPORT_ENDPOINT, lambda _c: backend_failure("inbox down", status_code=500)
    )
    await arm_the_concern_flow(bot, user)
    bot.telegram.reset()

    await bot.send(user.text("Idish yorilgan"))

    assert bot.telegram.last_shown().text == TRANSLATIONS["telegram.support.send_failed"], (
        "the customer must be told the send failed"
    )
    texts = [c.text for c in bot.telegram.shown]
    assert TRANSLATIONS[("uz", "telegram.support.ack")] not in texts, (
        "and must never be thanked for a message that was not stored"
    )


async def test_a_language_switch_between_the_prompt_and_the_reply_answers_in_the_new_language(
    bot, user
):
    """The concern flow spans two updates and stores nothing about language, so
    it re-reads it. A customer who switches to Russian while composing must be
    thanked in Russian; serving the language captured at arming time would show
    them Uzbek copy in a Russian chat.
    """
    await arm_the_concern_flow(bot, user)
    assert bot.telegram.last_shown().text.startswith("Iltimos"), "prompted in Uzbek"

    bot.database.user["preferred_language"] = "ru"
    bot.telegram.reset()

    await bot.send(user.text("Бутыль пришла треснутой"))

    assert filed_contents(bot) == [f"[Order #{ORDER_NUMBER}] Бутыль пришла треснутой"]
    assert bot.telegram.last_shown().text == TRANSLATIONS[("ru", "telegram.support.ack")], (
        "the acknowledgement follows the customer's CURRENT language"
    )


async def test_a_very_long_concern_is_truncated_to_something_telegram_will_accept(
    bot, user
):
    """The serializer caps support content at 4096 and so does Telegram. Prefix
    plus a full-length message would 422 — the customer's longest, angriest,
    most detailed complaint would be the one that silently failed to send.

    The cap comes from the production constant, so retuning it retunes this.
    """
    await arm_the_concern_flow(bot, user)
    essay = "a" * (MAX_SUPPORT_CONTENT * 2)
    bot.telegram.reset()

    await bot.send(user.text(essay))

    (content,) = filed_contents(bot)
    assert len(content) == MAX_SUPPORT_CONTENT
    assert content.startswith(f"[Order #{ORDER_NUMBER}] "), (
        "truncation must eat the customer's text, never the order reference "
        "an operator needs to route it"
    )
    assert bot.telegram.last_shown().text == TRANSLATIONS[("uz", "telegram.support.ack")]


async def test_the_report_button_the_delivered_summary_renders_is_claimed_by_a_handler(
    bot, user
):
    """The button is rendered in `telegram_bot/webhook_server.py` as
    `report_issue_<order_id>` and registered in `bot.py` as
    `^report_issue_\\d+$`. Those are two files that have to agree, and nothing
    else checks that they do — a rename on either side leaves the delivered
    summary showing a button that spins forever.
    """
    from pathlib import Path

    webhook_source = (
        Path(__file__).resolve().parents[2] / "telegram_bot" / "webhook_server.py"
    ).read_text(encoding="utf-8")
    assert "'callback_data': f'report_issue_{order_id}'" in webhook_source, (
        "the delivered summary no longer renders report_issue_<order_id>; "
        "update the pattern assertion below with it"
    )

    assert bot.handlers_matching(user.tap("report_issue_9001")), (
        "the Report button on the delivered summary is claimed by no handler"
    )
    assert not bot.handlers_matching(user.tap("report_issue_")), (
        "an id-less callback must not match — it would crash the int() parse "
        "inside the handler on every malformed tap"
    )
