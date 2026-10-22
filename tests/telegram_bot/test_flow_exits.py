# tests/telegram_bot/test_flow_exits.py
"""Leaving unfinished prompts (spec §2): every exit gesture ends every flow except signup.

An exit gesture is a bottom-keyboard label, /start, /menu, /help, /language, or a
callback that an old inline menu still sends. The flow-exit handler in group -3
ends every open conversation and disarms the saved prompts. It never stops
dispatch, so group 0 then draws the destination exactly once.
"""
import inspect
import json
import re
from datetime import datetime, timezone

import pytest
import telegram
from telegram.ext import BaseHandler, ConversationHandler, MessageHandler

import utils as utils_module
from flow_exit import (
    FLOW_EXIT_GROUP,
    FREE_TEXT_FILTER,
    LEGACY_MENU_CALLBACK_PATTERN,
    PROMPTS_LEFT_BY_EXIT,
    FlowExitHandler,
)
from handlers.profile import ADDRESS_APARTMENT, ADDRESS_LOCATION, ADDRESS_STREET, PHONE_VERIFY_PHONE
from keyboards import MAIN_MENU_KEYS

from tests.telegram_bot.ptb_harness import DEFAULT_CHAT_ID, DEFAULT_USER_ID, build_bot_harness
from tests.telegram_bot.test_cart_and_quantity_journeys import expire_dedup_window
from tests.telegram_bot.test_menu_label_routing import MENU_LABELS, MENU_LABEL_TRANSLATIONS

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SUPPORT = "/api/v1/support/messages"
KEY = (DEFAULT_CHAT_ID, DEFAULT_USER_ID)
HELP_LABEL = MENU_LABELS["telegram.menu.support"]["uz"]

# What old inline menus already in customers' chats still send (spec §1).
LEGACY_MENU_CALLBACKS = (
    "back_to_main", "menu_products", "menu_orders", "menu_subscriptions", "menu_loyalty",
    "menu_profile", "menu_support", "menu_language", "cart_view",
)

TRANSLATIONS = {
    **MENU_LABEL_TRANSLATIONS,
    "telegram.main_menu": "ASOSIY-MENYU",
    "telegram.welcome": "YANA-XUSH-KELIBSIZ",
    "telegram.profile_title": "PROFIL",
    "telegram.support.menu_intro": "YORDAM-EKRANI",
    "telegram.help.command_hint": "PASTDAGI-TUGMALAR",
    "telegram.address.flow_timed_out": "MANZIL-VAQTI-TUGADI",
    "telegram.address.flow_timed_out_saved": "MANZIL-SAQLANDI-VAQTI-TUGADI",
}

GESTURES = [
    *(
        pytest.param(lambda u, k=k: u.text(MENU_LABELS[k]["uz"]), MENU_LABELS[k]["uz"], id=f"uz:{k}")
        for k in MAIN_MENU_KEYS
    ),
    pytest.param(
        lambda u: u.text(MENU_LABELS["telegram.menu.orders"]["ru"]),
        MENU_LABELS["telegram.menu.orders"]["ru"],
        id="ru:telegram.menu.orders",
    ),
    *(pytest.param(lambda u, c=c: u.command(c), None, id=f"/{c}") for c in ("start", "menu", "help", "language")),
    *(pytest.param(lambda u, d=d: u.tap(d), None, id=d) for d in LEGACY_MENU_CALLBACKS),
]

# One gesture of each kind, with how its destination's message starts.
EXITS_FROM_A_SAVED_PROMPT = [
    pytest.param(lambda u: u.text(HELP_LABEL), "YORDAM-EKRANI", id="label"),
    pytest.param(lambda u: u.tap("menu_profile"), "PROFIL", id="menu_profile"),
    pytest.param(lambda u: u.command("menu"), "ASOSIY-MENYU", id="/menu"),
]

# Each prompt seeded exactly as its arming site writes it
# (support.py:57-63, profile.py:2152, 2218, 4173-4177, 4253-4257).
ARMED_AS_THE_BOT_ARMS_IT = {
    "support_message": {
        "support_order_id": 555,
        "support_order_number": "ORD-2026-0555",
        "support_armed_at": datetime.now(timezone.utc).isoformat(),
    },
    "edit_profile_name": {},
    "edit_profile_birthday": {},
    "edit_address_title": {"edit_address_id": "901"},
    "edit_address_instructions": {"edit_address_id": "901"},
}


def _always(value):
    async def _answer(*_args, **_kwargs):
        return value

    return _answer


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    monkeypatch.setattr(utils_module.rate_limiter, "allow_request", _always(True))
    return harness


@pytest.fixture
def user(bot):
    return bot.updates()


def conversations(bot):
    for group in sorted(bot.application.handlers):
        for handler in bot.application.handlers[group]:
            if isinstance(handler, ConversationHandler):
                yield handler


def park(bot, name, state):
    bot.conversation(name)._conversations[KEY] = state


def bot_state(bot):
    return json.loads(bot.database.user.get("bot_state") or "{}")


def flow_data(bot):
    return bot.application.user_data[DEFAULT_USER_ID]


def support_posts(bot):
    return [c.data for c in bot.backend.calls if c.method == "POST" and c.endpoint == SUPPORT]


def calls_carrying(bot, text):
    return [
        c for c in bot.backend.calls
        if text in json.dumps([c.data, c.params], ensure_ascii=False, default=str)
    ]


async def send_fresh(bot, update):
    """One gesture from a clean slate: only what THIS send showed and called."""
    expire_dedup_window()
    bot.telegram.reset()
    bot.backend.calls.clear()
    await bot.send(update)


@pytest.mark.parametrize("gesture,typed", GESTURES)
async def test_every_conversation_state_is_left_by_every_exit_gesture(bot, user, gesture, typed):
    swept = set()
    for conv in conversations(bot):
        if conv.name == "registration":
            continue
        for state in conv.states:
            if state == ConversationHandler.TIMEOUT:
                continue
            conv._conversations[KEY] = state

            update = gesture(user)
            await send_fresh(bot, update)

            where = f"{conv.name} @ {state}"
            assert bot.conversation_state(conv.name) is None, f"{where}: the exit did not end the flow"
            assert (
                bot.telegram.of("editMessageReplyMarkup")
                if getattr(update.callback_query, "data", None) == "back_to_main"
                else bot.telegram.shown
            ), f"{where}: the destination drew nothing"
            assert support_posts(bot) == [], f"{where}: the gesture was filed to the Support Inbox"
            if typed:
                assert calls_carrying(bot, typed) == [], f"{where}: the label reached the backend as an answer"
            swept.add(conv.name)

    assert swept == {
        "address_conversation", "phone_verification", "subscription_creation",
        "item_management", "update_item",
    }


async def test_an_exit_leaves_exactly_the_registration_states_open(bot, user):
    states_an_exit_does_not_end = set()
    for conv in conversations(bot):
        for state in conv.states:
            if state == ConversationHandler.TIMEOUT:
                continue
            conv._conversations[KEY] = state
            await send_fresh(bot, user.command("menu"))
            if bot.conversation_state(conv.name) is not None:
                states_an_exit_does_not_end.add((conv.name, state))
            conv._conversations.pop(KEY, None)

    registration = bot.conversation("registration")
    assert states_an_exit_does_not_end == {
        ("registration", state) for state in registration.states if state != ConversationHandler.TIMEOUT
    }


async def test_one_tap_ends_two_open_flows_and_the_next_text_reaches_support(bot, user):
    await bot.send(user.tap("add_new_address"))       # the real entry writes the address draft
    park(bot, "address_conversation", ADDRESS_STREET)
    await bot.send(user.tap("add_phone_number"))      # address claims nothing for this tap
    assert bot.conversation_state("phone_verification") == PHONE_VERIFY_PHONE
    assert "address_draft" in bot_state(bot)

    await send_fresh(bot, user.text(HELP_LABEL))

    assert bot.conversation_state("address_conversation") is None
    assert bot.conversation_state("phone_verification") is None
    assert bot.telegram.last_shown().text == "YORDAM-EKRANI\n\nPASTDAGI-TUGMALAR"
    assert "address_draft" not in bot_state(bot)

    await send_fresh(bot, user.text("Suvni ertaga olib keling"))

    assert support_posts(bot) == [{"content": "Suvni ertaga olib keling", "message_type": "text"}]
    assert bot.telegram.shown == [], "nothing answered it as a street, a phone or a timed-out form"


def test_every_prompt_an_exit_leaves_is_seeded_here():
    assert set(ARMED_AS_THE_BOT_ARMS_IT) == set(PROMPTS_LEFT_BY_EXIT)


@pytest.mark.parametrize("gesture,opens_with", EXITS_FROM_A_SAVED_PROMPT)
@pytest.mark.parametrize("prompt", PROMPTS_LEFT_BY_EXIT)
async def test_a_tap_disarms_the_saved_prompt_and_the_next_text_is_not_fed_to_it(
    bot, user, prompt, gesture, opens_with
):
    bot.database.user["bot_state"] = json.dumps(
        {"awaiting_input": prompt, **ARMED_AS_THE_BOT_ARMS_IT[prompt]}
    )

    await send_fresh(bot, gesture(user))

    assert bot.telegram.last_shown().text.startswith(opens_with)
    assert "awaiting_input" not in bot_state(bot)

    await send_fresh(bot, user.text("Ertaga ertalab keling"))

    assert support_posts(bot) == [{"content": "Ertaga ertalab keling", "message_type": "text"}], (
        "an ordinary message: no '[Order #…]' prefix"
    )
    assert [c for c in bot.backend.calls if c.method in ("PUT", "PATCH")] == [], (
        "not saved as a name, a birthday or an address"
    )


async def test_start_inside_the_address_form_ends_it_and_shows_the_menu(bot, user):
    await bot.send(user.tap("add_new_address"))
    assert bot.conversation_state("address_conversation") == ADDRESS_LOCATION

    await send_fresh(bot, user.command("start"))

    assert bot.conversation_state("address_conversation") is None
    assert bot.telegram.last_shown().text == "ASOSIY-MENYU"
    assert not {"address_draft", "awaiting_location_at"} & set(bot_state(bot))


async def test_a_legacy_back_to_main_inside_a_flow_ends_it_and_only_takes_that_bubbles_buttons(bot, user):
    await bot.send(user.tap("add_new_address"))
    park(bot, "address_conversation", ADDRESS_STREET)

    await send_fresh(bot, user.tap("back_to_main"))

    assert bot.conversation_state("address_conversation") is None
    assert [c.method for c in bot.telegram.calls if c.method != "answerCallbackQuery"] == ["editMessageReplyMarkup"]
    assert "MANZIL-VAQTI-TUGADI" not in bot.telegram.texts()


async def test_leaving_the_form_after_the_pin_saved_the_address_keeps_that_address(bot, user):
    await bot.send(user.tap("add_new_address"))
    park(bot, "address_conversation", ADDRESS_APARTMENT)
    flow_data(bot)["temp_address_data"] = {"location_source": "shared", "address_id": 901, "title": "Uy"}

    await send_fresh(bot, user.text(MENU_LABELS["telegram.menu.profile"]["uz"]))

    assert bot.conversation_state("address_conversation") is None
    assert "temp_address_data" not in flow_data(bot)
    assert [c for c in bot.backend.calls if c.method == "DELETE"] == [], "an exit is not Cancel"


def test_the_legacy_callbacks_swept_here_are_exactly_the_pattern():
    assert len(set(LEGACY_MENU_CALLBACKS)) == 9
    assert all(re.fullmatch(LEGACY_MENU_CALLBACK_PATTERN, data) for data in LEGACY_MENU_CALLBACKS)


async def test_the_ptb_internals_the_exit_reads_are_pinned(bot, user):
    """leave_all_flows reads `_get_key` and `_conversations` and hands `handle_update`
    the 4-tuple (state, key, handler, check). The order is taken from what PTB
    22.3's own `check_update` returns."""
    assert telegram.__version__ == "22.3"
    assert list(inspect.signature(ConversationHandler.handle_update).parameters) == [
        "self", "update", "application", "check_result", "context",
    ]
    for conv in conversations(bot):
        assert (conv.per_chat, conv.per_user, conv.per_message) == (True, True, False), conv.name
        assert conv._get_key(user.text("x")) == KEY

    park(bot, "address_conversation", ADDRESS_STREET)
    check = bot.conversation("address_conversation").check_update(user.text("Chilonzor"))

    assert len(check) == 4
    assert check[:2] == (ADDRESS_STREET, KEY)
    assert isinstance(check[2], BaseHandler)


async def test_an_exit_cancels_the_flow_timeout_so_it_never_announces_later(bot, user):
    await bot.application.job_queue.start()
    try:
        await bot.send(user.tap("add_new_address"))
        address = bot.conversation("address_conversation")
        timeout_job = address.timeout_jobs[KEY]

        await send_fresh(bot, user.text(HELP_LABEL))

        assert bot.conversation_state("address_conversation") is None
        assert KEY not in address.timeout_jobs
        assert timeout_job.removed
        shown_after_exit = bot.telegram.texts()

        await timeout_job.run(bot.application)  # the 24-hour timer firing anyway

        assert bot.telegram.texts() == shown_after_exit
        assert "MANZIL-VAQTI-TUGADI" not in shown_after_exit
    finally:
        await bot.application.job_queue.stop()


async def test_wiring_checks_do_not_count_the_flow_exit(bot, user):
    """Like the catch-alls, it claims every exit gesture and draws nothing. If
    `handlers_matching` counted it, a menu button whose group-0 handler was
    deleted would still look wired."""
    for gesture in (user.tap("menu_profile"), user.command("menu"), user.text(HELP_LABEL)):
        claimed = bot.handlers_matching(gesture, include_catch_alls=True)
        assert [g for g, h in claimed if isinstance(h, FlowExitHandler)] == [FLOW_EXIT_GROUP]
        assert [h for _g, h in bot.handlers_matching(gesture) if isinstance(h, FlowExitHandler)] == []


async def test_the_exit_and_the_text_catch_all_read_free_text_through_one_filter(bot, user):
    """The router no longer resolves labels itself; it trusts `pending_menu_tap`.
    So every text the group-0 catch-all receives must be one the exit could
    resolve. One filter object, not two copies that can drift: widen the
    catch-all to captions or edits and the exit sees them too."""
    [catch_all] = [
        h for h in bot.application.handlers[0]
        if isinstance(h, MessageHandler) and getattr(h.callback, "__name__", "") == "_handle_text_message"
    ]
    [flow_exit] = [h for h in bot.application.handlers[FLOW_EXIT_GROUP] if isinstance(h, FlowExitHandler)]

    assert catch_all.filters is FREE_TEXT_FILTER

    assert flow_exit.check_update(user.text(HELP_LABEL)) == "telegram.menu.support"
    for update in (user.text(HELP_LABEL), user.edited_text(HELP_LABEL), user.photo(caption=HELP_LABEL)):
        exit_claims = flow_exit.check_update(update) not in (None, False)
        assert exit_claims == bool(catch_all.check_update(update)), update
