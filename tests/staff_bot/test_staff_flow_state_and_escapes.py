"""Walking out of a staff flow must actually leave it.

The staff bot's main menu is a REPLY keyboard, so a driver who changes their
mind mid-flow does not tap "cancel" — they tap "💰 Cash" and Telegram delivers
an ordinary text message. Whether that message ends the flow they were in
depends entirely on wiring: ``StaffBot._conv_menu_escape`` is prepended to the
TEXT states of each ``ConversationHandler``, and the catch-all router
(``_handle_text_message``) clears ``flow_state.PENDING_FLOW_USER_DATA_KEYS``
for the flows that live outside conversations.

Both halves have already failed in production, twice:

* ``staff_bot_text_router_state_leak`` — a menu tap parsed as a cash amount,
  and (worse) a menu tap consumed as the NOTE that finalized a real delivery.
* the ``BOTTLE_SESSION_REQUIRED`` trap — the inline Back button re-rendered the
  hub but returned ``None`` instead of ``ConversationHandler.END``, so the bot
  kept asking for a bottle quantity forever.

The existing staff tests check the two halves in isolation: with spy handlers
(``test_text_router_menu_escape.py``), against a hand-built Update
(``test_conversation_menu_escape.py``), or by counting ``menu_escape,`` in the
source (a source-text count cannot tell a wired state from an unwired one).
This file drives the REAL dispatcher instead, so what it measures is the thing
that actually decides a driver's fate: ``ConversationHandler._conversations``
after the tap.

Two rules are asserted everywhere:

1. after a main-menu tap, ``harness.conversation_state(name)`` is ``None``;
2. the driver SEES the screen they asked for — a flow that dies in silence is
   indistinguishable from a crashed bot, and drivers respond by tapping harder.

The harness configures ``flow_state`` with ``None``, i.e. production's degraded
path when Redis is unreachable, so the escape is exercised in the mode a Redis
outage puts it in.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from telegram.ext import ConversationHandler

from staff_bot.handlers.delivery.bottle_collection import (
    BOTTLE_TRANSFER_DRIVER_SELECT,
    BOTTLE_TRANSFER_QTY_INPUT,
)
from staff_bot.handlers.operator.create_order import (
    CONFIRM_ORDER as ORDER_CONFIRM_ORDER,
    SELECT_ADDRESS as ORDER_SELECT_ADDRESS,
    SELECT_PAYMENT as ORDER_SELECT_PAYMENT,
    SELECT_PRODUCTS as ORDER_SELECT_PRODUCTS,
    SELECT_QUANTITY as ORDER_SELECT_QUANTITY,
)
from staff_bot.handlers.operator.create_user import (
    CONFIRM_CREATE,
    ENTER_PHONE,
    SELECT_LANGUAGE as CREATE_USER_LANG,
)
from staff_bot.handlers.operator.manage_address import CONFIRM_ADDRESS
from staff_bot.handlers.start import SELECT_LANGUAGE as AUTH_SELECT_LANGUAGE
from staff_bot.utils.flow_state import PENDING_FLOW_USER_DATA_KEYS

from tests.staff_bot.ptb_harness import (
    DEFAULT_DRIVER_TELEGRAM_ID,
    FakeStaffDatabase,
    build_staff_harness,
    staff_backend_failure,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


# ---------------------------------------------------------------------------
# Real copy, not hand-pasted copy
# ---------------------------------------------------------------------------
# The menu labels are resolved live from the seed script through the same
# `_curated_value` that `seed_translations()` itself calls (the technique
# tests/staff_bot/test_conversation_menu_escape.py established). Pasting them
# would let a seed edit leave this file asserting copy production no longer
# ships — the file would go on passing while every button went quiet.

_SEED_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_staff_translations", _SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SEED = _load_seed_module()

LANGUAGES = ("en", "uz", "ru")

MENU_KEYS = (
    "staff.menu.new_orders",
    "staff.menu.active_deliveries",
    "staff.menu.tryouts",
    "staff.menu.cash",
    "staff.menu.create_client",
    "staff.menu.search_client",
    "staff.menu.create_order",
    "staff.menu.profile",
    "staff.menu.settings",
    "staff.menu.help",
)
MESSAGE_KEYS = (
    "staff.session_expired",
    "staff.menu.title",
    "staff.unauthorized",
    "staff.cancelled",
    "staff.auth_cancelled",
    # The language picker's re-prompt: what a staff member sees when the text
    # they sent names no language.
    "staff.select_language",
    # Leaving a flow by /cancel, and a flow that expired on its own.
    "staff.bottle_flow_cancelled",
    "staff.flow_timed_out",
    # The navigation destination an inline "Back" inside a bottle flow lands on.
    "staff.cash.hub_title",
)

LOGIN_ENDPOINT = "/api/v1/staff/auth/login"
SESSION_ENDPOINT = "/api/v1/staff/bottles/session/current"
OPEN_SESSION_ENDPOINT = "/api/v1/staff/bottles/session/open"
AVAILABLE_DRIVERS_ENDPOINT = "/api/v1/staff/bottles/sessions/available-drivers"
TRANSFERS_ENDPOINT = "/api/v1/staff/bottles/transfers"
OPERATOR_SEARCH_ENDPOINT = "/api/v1/staff/operator/users/search"
OPERATOR_USERS_ENDPOINT = "/api/v1/staff/operator/users"
CUSTOMER_SEARCH_ENDPOINT = "/api/v1/staff/customers/search"

OPEN_SESSION = {
    "id": 3,
    "session_ref": "SESS0001",
    "started_at": "2026-08-21T08:00:00",
    "bottles_loaded": 40,
    "current_inventory": 25,
}
OTHER_DRIVERS = [{"id": 12, "first_name": "Bek", "last_name": "Toshev", "name": "Bek Toshev"}]


def _curated(key: str, language: str) -> str:
    value = _SEED._curated_value(key, language)
    assert value, (
        f"{key} has no curated {language} value in scripts/seed_staff_translations.py — "
        "production would render a humanised placeholder in its place"
    )
    return value


def _translation_table() -> dict:
    """The staff translations these tests run against.

    Handed to ``build_staff_harness`` BEFORE ``_setup_handlers`` runs, so it is
    also the table the menu regexes are compiled from — the same coupling
    production has between the seeded rows and the router.
    """
    return {
        (language, key): _curated(key, language)
        for key in MENU_KEYS + MESSAGE_KEYS
        for language in LANGUAGES
    }


# ---------------------------------------------------------------------------
# A signed-in staff member
# ---------------------------------------------------------------------------


def _staff_row(roles, language):
    return {
        "id": 55,
        "telegram_id": str(DEFAULT_DRIVER_TELEGRAM_ID),
        "first_name": "Aziz",
        "last_name": "Karimov",
        "phone": "+998901112233",
        "preferred_language": language,
        "role": "delivery",
        "status": "active",
        "staff_roles": json.dumps(roles),
        "staff_bot_state": "{}",
    }


def _login_payload(roles, language):
    return {
        "access_token": "staff-access-token",
        "refresh_token": "staff-refresh-token",
        "expires_in": 3600,
        "user": {
            "id": 55,
            "first_name": "Aziz",
            "last_name": "Karimov",
            "phone": "+998901112233",
            "preferred_language": language,
            "staff_roles": roles,
            "delivery_person_id": 7,
        },
    }


async def build_staff(monkeypatch, *, roles=("delivery_driver", "operator"), language="en",
                      staff_roles_in_db=None):
    """A harness for a staff member holding ``roles``.

    Both roles by default: the flows under test span the driver's bottle
    conversations and the operator's client/order conversations, and a driver
    who is also an operator is a real (and the most exposed) configuration —
    every conversation in the bot is reachable from one keyboard.
    """
    roles = list(roles)
    harness = await build_staff_harness(
        monkeypatch,
        translations=_translation_table(),
        database=FakeStaffDatabase(
            staff_user=_staff_row(
                roles if staff_roles_in_db is None else staff_roles_in_db, language
            )
        ),
    )
    harness.backend.route("POST", LOGIN_ENDPOINT, lambda _call: _login_payload(roles, language))
    return harness


async def sign_in(harness):
    """Run the real ``/start`` login; return (update factory, rendered labels)."""
    staff_member = harness.updates()
    await harness.send(staff_member.command("start"))
    shown = harness.telegram.shown
    assert shown, "/start produced no message at all — the staff member sees a dead bot"
    labels = shown[-1].button_labels()
    assert labels, "login did not attach the reply-keyboard main menu"
    harness.telegram.reset()
    return staff_member, labels


def menu_label(labels, key, language="en") -> str:
    """The ONE rendered button carrying ``key``'s translation.

    Matching on the translated value rather than rebuilding ``f"{emoji} {v}"``
    keeps the emoji an implementation detail of the keyboard: these tests care
    that the label the driver sees escapes the flow, whatever decoration it
    wears.
    """
    value = _curated(key, language)
    hits = [label for label in labels if label.strip().endswith(value)]
    assert len(hits) == 1, f"expected exactly one button carrying {value!r}, found {hits}"
    return hits[0]


def user_data(harness) -> dict:
    return harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]


def capture_errors(harness) -> list:
    """Every exception PTB would have swallowed into its logs.

    Without a registered error handler a handler that raises looks exactly like
    one that quietly did nothing, and "quietly did nothing" is the whole failure
    mode this file is about.
    """
    errors = []

    async def _record(_update, context):
        errors.append(context.error)

    harness.application.add_error_handler(_record)
    return errors


# ---------------------------------------------------------------------------
# Opening each conversation for real
# ---------------------------------------------------------------------------
# Every staff ConversationHandler, with the button that opens it and the
# backend state its entry point demands. Driving the real entry point (rather
# than poking `_conversations`) is what makes the assertions below meaningful:
# the conversation is armed by the same code path production arms it with.

ENTRY_TAP = {
    "staff_create_user": "staff_create_client",
    "staff_search_user": "staff_search_client",
    "staff_create_order": "staff_create_order",
    "staff_add_address": "staff_op_add_addr_77",
    "staff_create_tryout": "staff_tryout_create",
    "staff_bottle_collection_search": "staff_bottle_collect_menu",
    "staff_bottle_loaded": "staff_bottle_log_loaded",
    "staff_bottle_returned_wh": "staff_bottle_return_warehouse",
    "staff_bottle_transfer": "staff_bottle_transfer_start",
    "staff_bottle_transfer_confirm": "staff_transfer_custom_9",
}

# `staff_auth` is entered by /start rather than a button and only parks an
# UNLINKED person; it gets its own test further down.
SELF_STANDING_CONVERSATIONS = {"staff_auth"}

# Conversations the sweep below must skip because a main-menu tap does not close
# them. Empty, and it must stay empty: `staff_bottle_transfer` was the last
# entry, and it was there only because its opening state
# (BOTTLE_TRANSFER_DRIVER_SELECT) is callback-only and the escape was wired to
# text states alone.
LEAKY_ON_MENU_TAP = set()


def prepare_backend(harness, name):
    """Backend state the conversation's entry point needs to get past its guards."""
    if name == "staff_bottle_loaded":
        # "Open a session" refuses outright when one is already open.
        harness.backend.route("GET", SESSION_ENDPOINT, lambda _call: None)
    elif name in {"staff_bottle_returned_wh", "staff_bottle_transfer"}:
        # Returning / transferring both refuse when there is no open session.
        harness.backend.route("GET", SESSION_ENDPOINT, lambda _call: dict(OPEN_SESSION))
        harness.backend.route("GET", AVAILABLE_DRIVERS_ENDPOINT, lambda _call: list(OTHER_DRIVERS))
    elif name in {"staff_create_user", "staff_create_order", "staff_search_user"}:
        harness.backend.route("GET", OPERATOR_SEARCH_ENDPOINT, lambda _call: [])


async def open_conversation(harness, staff_member, name):
    """Tap the real button that opens ``name`` and assert it actually opened."""
    prepare_backend(harness, name)
    harness.telegram.reset()
    await harness.send(staff_member.tap(ENTRY_TAP[name]))
    state = harness.conversation_state(name)
    assert state is not None, (
        f"tapping {ENTRY_TAP[name]!r} did not open the {name} conversation "
        f"(shown: {[call.text[:60] for call in harness.telegram.shown]})"
    )
    harness.telegram.reset()
    return state


@pytest.fixture
async def staff(monkeypatch):
    return await build_staff(monkeypatch)


# ---------------------------------------------------------------------------
# The systematic sweep
# ---------------------------------------------------------------------------


async def test_this_file_covers_every_conversation_the_staff_bot_registers(staff):
    """A new staff flow must be added to ENTRY_TAP, not silently uncovered.

    Every leak this file exists for was introduced by adding a conversation and
    forgetting the escape. If a new ConversationHandler can appear without any
    test noticing, the next one ships the same way — a driver stuck answering a
    prompt they already walked away from.
    """
    registered = set(staff.conversation_names())
    accounted = set(ENTRY_TAP) | SELF_STANDING_CONVERSATIONS

    assert registered - accounted == set(), (
        f"new staff conversations with no escape coverage: {sorted(registered - accounted)}. "
        "Add the button that opens each one to ENTRY_TAP so the menu-escape sweep runs "
        "against it."
    )
    assert accounted - registered == set(), (
        f"ENTRY_TAP names conversations that no longer exist: {sorted(accounted - registered)}"
    )


@pytest.mark.parametrize(
    "name", sorted(set(ENTRY_TAP) - LEAKY_ON_MENU_TAP)
)
async def test_a_main_menu_tap_closes_the_flow_the_staff_member_walked_out_of(monkeypatch, name):
    """The give-up path, once per flow, through the real dispatcher.

    A staff member opens a flow, changes their mind, and taps a main-menu
    button. If the conversation survives that tap it stays armed for five
    minutes, and its in-state MessageHandler outranks the catch-all router — so
    the next thing they type anywhere in the bot is swallowed as this flow's
    input. That is how a menu tap became "Invalid cash amount", and how a
    driver was left answering a bottle-quantity prompt they had already left.

    Both halves are asserted: the conversation is gone AND the driver is
    looking at the cash hub they asked for. Ending in silence would satisfy a
    "state is None" check while still reading as a crashed bot.
    """
    harness = await build_staff(monkeypatch)
    staff_member, labels = await sign_in(harness)
    errors = capture_errors(harness)

    await open_conversation(harness, staff_member, name)

    await harness.send(staff_member.text(menu_label(labels, "staff.menu.cash")))

    assert errors == [], f"escaping {name} raised {errors}"
    assert harness.conversation_state(name) is None, (
        f"{name} is still armed after a main-menu tap; the staff member's next message "
        "will be captured by a flow they already left"
    )
    assert harness.telegram.shown, f"escaping {name} left the staff member with no reply at all"
    assert harness.telegram.shown[-1].callback_data(), (
        f"escaping {name} produced a message with no buttons — the cash hub never rendered"
    )


@pytest.mark.parametrize("flow_key", PENDING_FLOW_USER_DATA_KEYS)
async def test_abandoning_a_conversation_drops_every_documented_pending_flow_flag(
    monkeypatch, flow_key
):
    """The two flow systems have to be cleared by ONE tap, not one each.

    A driver can be inside a ConversationHandler *and* carry a
    ``pending_*_flow`` flag from the text-router world at the same time — the
    bottle flows hand off between the two. ``_conv_menu_escape`` is the only
    thing that clears both, and it clears the flags through
    ``flow_state.clear_pending_flows``, whose key list is the SSOT imported
    here. Parametrising over the real tuple means a key added to production is
    covered the moment it lands; a key that production starts writing but never
    registers is invisible to this test and to the escape alike (see the
    transfer ratchet below).

    If this fails, the driver navigates away but the flag survives, and their
    next typed text is parsed as a cash amount or consumed as the note that
    finalizes a delivery.
    """
    harness = await build_staff(monkeypatch)
    staff_member, labels = await sign_in(harness)

    await open_conversation(harness, staff_member, "staff_bottle_collection_search")
    user_data(harness)[flow_key] = {"delivery_id": 9, "flow_type": "partial"}

    await harness.send(staff_member.text(menu_label(labels, "staff.menu.cash")))

    assert flow_key not in user_data(harness), (
        f"{flow_key} survived a main-menu tap taken from inside a conversation"
    )
    assert harness.conversation_state("staff_bottle_collection_search") is None


# ---------------------------------------------------------------------------
# Two flows, one after the other
# ---------------------------------------------------------------------------


async def test_two_flows_opened_back_to_back_do_not_cross_wire_the_number_typed(monkeypatch):
    """A number typed into flow B must not be read by flow A.

    Bottle collection asks for a customer SEARCH; opening a session asks for a
    bottle COUNT. Both are plain text in the same chat, and both conversations
    sit in the same handler group — the search one registered first, so if it
    is still armed it wins. "12" would then be searched for as a customer and
    the session would never open, with the driver watching a prompt that never
    advances.

    This is the exact ordering a driver produces in a warehouse: start a
    collection, realise they are at the depot, tap Cash, open the session.
    """
    harness = await build_staff(monkeypatch)
    staff_member, labels = await sign_in(harness)

    await open_conversation(harness, staff_member, "staff_bottle_collection_search")
    await harness.send(staff_member.text(menu_label(labels, "staff.menu.cash")))
    assert harness.conversation_state("staff_bottle_collection_search") is None

    harness.backend.route("POST", OPEN_SESSION_ENDPOINT, lambda _call: dict(OPEN_SESSION))
    await open_conversation(harness, staff_member, "staff_bottle_loaded")

    harness.backend.calls.clear()
    await harness.send(staff_member.text("12"))

    opened = [call for call in harness.backend.calls if call.endpoint == OPEN_SESSION_ENDPOINT]
    assert opened, (
        "typing the bottle count never reached the session-open call; some earlier "
        f"conversation claimed it (backend saw {[c.endpoint for c in harness.backend.calls]})"
    )
    assert opened[0].data == {"bottles_loaded": 12}
    assert not [
        call for call in harness.backend.calls if call.endpoint == CUSTOMER_SEARCH_ENDPOINT
    ], "the abandoned bottle-collection search consumed the driver's bottle count"
    assert harness.conversation_state("staff_bottle_loaded") is None, (
        "the session-open conversation did not close after it finished"
    )


async def test_a_flow_can_be_reopened_after_the_staff_member_walked_out_of_it(monkeypatch):
    """``allow_reentry`` is what makes "changed my mind twice" survivable.

    Without it the entry point is inert for the rest of the session: the driver
    taps "Log bottles loaded", nothing happens, and their only recourse is
    restarting the bot — from the road.
    """
    harness = await build_staff(monkeypatch)
    staff_member, labels = await sign_in(harness)

    await open_conversation(harness, staff_member, "staff_bottle_loaded")
    await harness.send(staff_member.text(menu_label(labels, "staff.menu.profile")))
    assert harness.conversation_state("staff_bottle_loaded") is None

    harness.telegram.reset()
    await harness.send(staff_member.tap(ENTRY_TAP["staff_bottle_loaded"]))

    assert harness.conversation_state("staff_bottle_loaded") is not None, (
        "opening a session is dead after the driver used it once and backed out"
    )
    assert harness.telegram.shown, "the re-opened flow prompted for nothing"


async def test_reentering_create_client_rescues_an_operator_from_the_leaked_language_step(
    monkeypatch,
):
    """The one thing that keeps the CREATE_USER_LANG leak (below) survivable.

    The conversation is left armed at a callback-only state, but the reply-
    keyboard entry point is checked BEFORE the current state whenever
    ``allow_reentry`` is set — so tapping "Create Client" again puts the
    operator back on the phone prompt with a blank client. If re-entry ever
    stopped winning, the leak would turn from "stale buttons stay live" into
    "the operator cannot create a client at all until the 5-minute timeout".
    """
    harness = await build_staff(monkeypatch)
    staff_member, labels = await sign_in(harness)
    harness.backend.route("GET", OPERATOR_SEARCH_ENDPOINT, lambda _call: [])

    await open_conversation(harness, staff_member, "staff_create_user")
    await harness.send(staff_member.text("+998901234567"))
    await harness.send(staff_member.text("Nodira"))
    await harness.send(staff_member.text("Yusupova"))
    assert harness.conversation_state("staff_create_user") == CREATE_USER_LANG

    await harness.send(staff_member.text(menu_label(labels, "staff.menu.cash")))

    harness.telegram.reset()
    await harness.send(staff_member.text(menu_label(labels, "staff.menu.create_client")))

    assert harness.conversation_state("staff_create_user") == ENTER_PHONE, (
        "re-entry no longer rescues the operator from an abandoned create-client flow"
    )
    assert user_data(harness)["new_client"] == {}, (
        "re-entry kept the half-entered client from the abandoned attempt"
    )


# ---------------------------------------------------------------------------
# Unhappy paths around the escape itself
# ---------------------------------------------------------------------------


async def test_the_escape_still_works_with_the_redis_flow_mirror_switched_off(monkeypatch):
    """Redis down must not pin a driver inside a flow.

    ``flow_state`` mirrors "this driver is mid-flow" into Redis so asynchronous
    pool-insertion webhooks defer instead of interrupting. The harness runs it
    the way production runs it during an outage — ``configure(None)`` — and the
    module's contract is that every call degrades to a no-op rather than
    raising. If a raise ever escaped ``clear_pending_flows``, the escape would
    abort BEFORE ``ConversationHandler.END`` and a Redis outage would silently
    trap every driver in whatever flow they were in.
    """
    from staff_bot.utils import flow_state

    harness = await build_staff(monkeypatch)
    staff_member, labels = await sign_in(harness)
    errors = capture_errors(harness)

    assert flow_state.is_enabled() is False, "this test is meaningless with Redis configured"
    assert await flow_state.queue_pool_suggestion(DEFAULT_DRIVER_TELEGRAM_ID, {"x": 1}) is False
    assert await flow_state.drain_pool_suggestions(DEFAULT_DRIVER_TELEGRAM_ID) == []
    assert await flow_state.get_active_flow(DEFAULT_DRIVER_TELEGRAM_ID) is None

    await open_conversation(harness, staff_member, "staff_create_tryout")
    user_data(harness)["pending_cod_collection_flow"] = {"customer_id": 1, "amount": 5000}

    await harness.send(staff_member.text(menu_label(labels, "staff.menu.cash")))

    assert errors == [], f"the escape blew up with the Redis mirror disabled: {errors}"
    assert harness.conversation_state("staff_create_tryout") is None
    assert "pending_cod_collection_flow" not in user_data(harness)
    assert harness.telegram.shown, "the driver got nothing back"


async def test_a_telegram_rejection_while_rendering_the_destination_still_closes_the_flow(
    monkeypatch,
):
    """Telegram rejects ``sendMessage`` for reasons the bot cannot predict.

    ``can't parse entities`` is the routine one here: every staff screen is
    ``parse_mode='HTML'`` and carries customer-supplied names. The escape
    dispatches the destination BEFORE it returns ``ConversationHandler.END``,
    so a raise on that render would skip the END and leave the driver both
    without a screen AND still inside the flow — the worst of both, because the
    next thing they type gets eaten by a prompt they can no longer see.
    """
    harness = await build_staff(monkeypatch)
    staff_member, labels = await sign_in(harness)
    errors = capture_errors(harness)

    await open_conversation(harness, staff_member, "staff_search_user")

    harness.telegram.fail("sendMessage", "Bad Request: can't parse entities")
    await harness.send(staff_member.text(menu_label(labels, "staff.menu.cash")))

    assert harness.telegram.of("sendMessage"), (
        "the destination never attempted a send, so the rejection was never exercised — "
        "this test would pass no matter what the escape did"
    )
    assert harness.conversation_state("staff_search_user") is None, (
        "a failed render left the abandoned search conversation armed; the operator's "
        "next message will be searched for instead of reaching the menu"
    )

    # And the bot is not wedged: the next tap works once Telegram recovers.
    harness.telegram.clear_failures()
    harness.telegram.reset()
    await harness.send(staff_member.text(menu_label(labels, "staff.menu.profile")))
    assert harness.telegram.shown, "the bot stayed dead after a transient Telegram failure"
    assert errors == [], f"a transient Telegram failure surfaced as a handler error: {errors}"


async def test_a_backend_failure_at_the_destination_still_closes_the_flow(monkeypatch):
    """Same contract, other dependency: the business API is down.

    The endpoints are discovered by tapping the button once and recording what
    the bot actually called, so this keeps testing the real dependency after a
    refactor moves it. A 500 on the destination screen must not strand the
    driver inside the flow they just left.
    """
    harness = await build_staff(monkeypatch)
    staff_member, labels = await sign_in(harness)
    active = menu_label(labels, "staff.menu.active_deliveries")

    await harness.send(staff_member.text(active))
    touched = {
        (call.method, call.endpoint)
        for call in harness.backend.calls
        if call.endpoint != LOGIN_ENDPOINT
    }
    assert touched, "the active-deliveries screen calls no backend endpoint to break"
    for method, endpoint in touched:
        harness.backend.route(
            method, endpoint, lambda _call: staff_backend_failure("boom", status_code=500)
        )

    errors = capture_errors(harness)
    await open_conversation(harness, staff_member, "staff_create_tryout")
    await harness.send(staff_member.text(active))

    assert errors == [], f"a backend 500 escaped as a handler error: {errors}"
    assert harness.conversation_state("staff_create_tryout") is None, (
        "a backend failure at the destination left the tryout conversation armed"
    )
    assert harness.telegram.shown, "the driver was told nothing at all about the failure"


async def test_tapping_the_same_menu_button_twice_from_inside_a_flow_answers_both_times(
    monkeypatch,
):
    """Double taps are normal on a phone in a moving van.

    A reply-keyboard tap can never be answered with a Telegram toast, so a new
    message is the ONLY feedback. The first tap escapes the conversation; the
    second arrives with no conversation left to escape and must still be
    answered by the catch-all router. If the second tap were swallowed, the
    driver would conclude the bot froze — precisely when they have just been
    told nothing by the first one either.
    """
    harness = await build_staff(monkeypatch)
    staff_member, labels = await sign_in(harness)
    cash = menu_label(labels, "staff.menu.cash")

    await open_conversation(harness, staff_member, "staff_bottle_collection_search")

    await harness.send(staff_member.text(cash))
    first = len(harness.telegram.shown)
    assert first, "the tap that escaped the flow showed nothing"
    assert harness.conversation_state("staff_bottle_collection_search") is None

    await harness.send(staff_member.text(cash))
    assert len(harness.telegram.shown) > first, (
        "the second Cash tap produced no message; with no conversation left to end, the "
        "catch-all router must still answer"
    )


async def test_a_keyboard_in_the_previous_language_still_escapes_the_flow(monkeypatch):
    """A driver switches to Russian; their PHONE still shows the Uzbek keyboard.

    Until Telegram redraws it, every tap is an Uzbek label arriving at a Russian
    session. The escape regex is compiled from ALL supported languages, so it
    claims the tap — and if the action matcher could not resolve it, the flow
    would be torn down with no output whatsoever and the driver would assume the
    bot crashed. That exact mismatch shipped once already.
    """
    harness = await build_staff(monkeypatch, language="ru")
    staff_member, labels = await sign_in(harness)

    russian_cash = menu_label(labels, "staff.menu.cash", "ru")
    uzbek_from_the_old_keyboard = f"💰 {_curated('staff.menu.cash', 'uz')}"
    assert russian_cash != uzbek_from_the_old_keyboard, "fixture produced identical languages"

    await open_conversation(harness, staff_member, "staff_bottle_collection_search")
    await harness.send(staff_member.text(uzbek_from_the_old_keyboard))

    assert harness.conversation_state("staff_bottle_collection_search") is None
    assert harness.telegram.shown, (
        "the stale-language tap killed the conversation without showing anything — the "
        "driver sees a bot that stopped responding"
    )


async def test_text_that_is_not_a_menu_label_stays_inside_the_flow(monkeypatch):
    """The escape must not be so eager that it eats real input.

    Everything a staff member types into these flows — phone numbers, names,
    bottle counts, notes — arrives as exactly the same kind of update as a menu
    tap. If the escape widened to claim them, the flow would collapse the
    instant the operator answered its first question, and nothing would ever be
    created.
    """
    harness = await build_staff(monkeypatch)
    staff_member, _labels = await sign_in(harness)
    harness.backend.route("GET", OPERATOR_SEARCH_ENDPOINT, lambda _call: [])

    await open_conversation(harness, staff_member, "staff_create_user")

    for typed in ("+998901234567", "Nodira"):
        await harness.send(staff_member.text(typed))
        assert harness.conversation_state("staff_create_user") is not None, (
            f"typing {typed!r} was mistaken for a menu tap and ended the flow"
        )

    assert user_data(harness)["new_client"]["phone"] == "+998901234567"
    assert user_data(harness)["new_client"]["first_name"] == "Nodira"


async def test_the_tap_that_ends_a_flow_is_removed_from_the_chat_but_typed_input_is_not(
    monkeypatch,
):
    """Menu echoes bury the pinned route card; the driver's own words must not
    be deleted.

    The reply keyboard sends TEXT, so every tap leaves the driver's message in
    the chat. ``_conv_menu_escape`` deletes the ones it consumed — a tap made
    inside a conversation leaves exactly the same litter as one made outside
    it. But deleting text a person actually typed reads as the bot eating their
    message, so the cleanup must be limited to taps the bot consumed as
    navigation.
    """
    harness = await build_staff(monkeypatch)
    staff_member, labels = await sign_in(harness)
    harness.backend.route("GET", OPERATOR_SEARCH_ENDPOINT, lambda _call: [])

    await open_conversation(harness, staff_member, "staff_create_user")

    await harness.send(staff_member.text("+998901234567"))
    assert not harness.telegram.of("deleteMessage"), (
        "the phone number the operator typed was deleted from the chat"
    )

    await harness.send(staff_member.text(menu_label(labels, "staff.menu.cash")))
    assert harness.telegram.of("deleteMessage"), (
        "the menu tap that ended the flow was left in the chat; echoes pile up on top of "
        "the pinned route card until it is scrolled out of reach"
    )


# ---------------------------------------------------------------------------
# Where a main-menu tap goes, from every state there is
# ---------------------------------------------------------------------------
# Not a ratchet: both sets below are the wiring INVARIANT, and the test under
# them measures the live handlers rather than the source text.

# Every conversation state must be escapable. This set is empty and the test
# below asserts it stays empty: `menu_escape` used to be prepended to TEXT
# states only, so nine callback-only states had no escape at all — PTB found no
# match, the conversation returned None, the catch-all router navigated, and the
# flow stayed armed at that state for the rest of its five-minute timeout with
# every inline button from its last message still live.
_STATES_WITH_NO_MENU_ESCAPE = set()

# The one state that claims a main-menu tap and answers it ITSELF instead of
# handing it to the escape: `staff_auth`'s language picker.
#
# That is DELIBERATE, and it is the fixed shape rather than a leak. This entry
# used to record a defect — the picker's MessageHandler accepts any text and
# `language_selected` answered 'en' for anything it did not recognise, so a
# stale main-menu tap silently logged a driver in as an ENGLISH user and
# overwrote their stored preference. The fix belonged in
# `staff_bot/handlers/start.py`, not in the wiring: unrecognised text now
# RE-PROMPTS (test_a_menu_tap_at_the_language_picker_re_prompts_instead_of_
# choosing_english below pins exactly that, in seeded copy, with nothing
# written).
#
# So the entry must STAY, and `menu_escape` must NOT be wired in front of this
# state: the escape ends the conversation and navigates, which for someone who
# is not linked to a staff account yet means walking them away from the only
# question standing between them and a login — and it would silently delete the
# re-prompt that regression test exists to guarantee. A state that answers the
# tap correctly on its own is not a state missing an escape.
_STATES_THAT_ANSWER_A_MENU_TAP_THEMSELVES = {
    ("staff_auth", AUTH_SELECT_LANGUAGE),
}


async def test_every_conversation_state_has_a_menu_escape_wired(staff):
    """A main-menu tap must end the flow from ANY state, not only text ones.

    Measured by asking the REAL registered handlers whether they claim a real
    main-menu Update, which is the same question PTB asks at dispatch time — a
    source-text count of ``menu_escape,`` cannot see a state that has none.

    ``ConversationHandler.TIMEOUT`` is excluded on purpose: its handlers exist
    to announce an expiry and deliberately claim every update shape, and they
    are only ever reachable from the timeout job.
    """
    _staff_member, labels = await sign_in(staff)
    tap = staff.updates().text(menu_label(labels, "staff.menu.cash"))

    unescaped = set()
    answered_in_place = set()
    for group in staff.application.handlers.values():
        for handler in group:
            if not isinstance(handler, ConversationHandler):
                continue
            for state, state_handlers in handler.states.items():
                if state == ConversationHandler.TIMEOUT:
                    continue
                claiming = [
                    inner for inner in state_handlers
                    if inner.check_update(tap) not in (None, False)
                ]
                if not claiming:
                    unescaped.add((handler.name, state))
                elif not any(
                    getattr(inner.callback, "__name__", "") == "_conv_menu_escape"
                    for inner in claiming
                ):
                    answered_in_place.add((handler.name, state))

    assert unescaped == _STATES_WITH_NO_MENU_ESCAPE, (
        "these conversation states cannot be escaped by a main-menu tap: "
        f"{sorted(unescaped - _STATES_WITH_NO_MENU_ESCAPE)}. "
        "A staff member parked on one of them taps a menu button, the "
        "destination opens, and the flow stays armed behind it."
    )
    assert answered_in_place == _STATES_THAT_ANSWER_A_MENU_TAP_THEMSELVES, (
        "the set of states that feed a main-menu tap to a non-escape handler has changed: "
        f"{sorted(answered_in_place ^ _STATES_THAT_ANSWER_A_MENU_TAP_THEMSELVES)}. "
        "A state added here owes the reader the same thing the language picker "
        "does: proof that its own handler answers the tap, in copy, without "
        "writing anything."
    )


# ---------------------------------------------------------------------------
# Ratchets: current behaviour, pinned so it cannot get worse
# ---------------------------------------------------------------------------
# Everything below documents a leak that exists TODAY. Read none of it as the
# desired behaviour; each docstring says what the fix would be, and the
# assertions are written to FAIL LOUDLY once the leak is closed so the ratchet
# gets deleted rather than quietly protecting a bug forever.


async def test_walking_out_of_the_transfer_picker_disarms_the_stale_bottle_ceiling(monkeypatch):
    """Was: ``test_RATCHET_the_transfer_driver_picker_survives_a_main_menu_tap``.

    ``staff_bottle_transfer`` opens directly into ``BOTTLE_TRANSFER_DRIVER_SELECT``,
    a callback-only state, so a main-menu tap there used to be handled by the
    catch-all router — which cannot end a conversation it is not part of. That
    half is a wiring fix in ``staff_bot/bot.py`` (the escape is registered on
    every state now) and is pinned by
    ``test_every_conversation_state_has_a_menu_escape_wired`` above.

    What this test now measures is the half that decides whether the leak can
    HURT: ``start_transfer_bottles`` stamps ``pending_transfer_available`` from
    the open session and ``receive_transfer_quantity`` enforces it as the "you
    cannot transfer more than you have" ceiling. Registered with the flow-state
    SSOT, that ceiling dies the moment the driver walks away, so it can never
    outlive the truck load it was measured from.
    """
    harness = await build_staff(monkeypatch)
    staff_member, labels = await sign_in(harness)

    await open_conversation(harness, staff_member, "staff_bottle_transfer")
    assert user_data(harness)["pending_transfer_available"] == OPEN_SESSION["current_inventory"], (
        "fixture: opening the transfer stamps the ceiling this test is about"
    )

    await harness.send(staff_member.text(menu_label(labels, "staff.menu.cash")))

    assert harness.telegram.shown, "the tap did at least navigate"
    assert "pending_transfer_available" not in user_data(harness), (
        "the bottle ceiling survived the driver walking away; the next tap on the stale "
        "picker measures the transfer against a truck load from hours ago"
    )
    assert "pending_transfer_receiver_id" not in user_data(harness)


async def test_a_stale_driver_button_can_no_longer_transfer_bottles_that_left_the_truck(
    monkeypatch,
):
    """Was: ``test_RATCHET_a_stale_driver_button_resumes_the_transfer_with_a_stale_bottle_count``.

    The driver opens a transfer while holding 25 bottles, changes their mind,
    taps Cash, and delivers all day. The transfer message is still in the chat
    above with its driver buttons on it.

    That tap used to resume the flow with ``pending_transfer_available`` still
    saying 25, so the bot's own "you cannot transfer more than you have" guard
    was measured against the morning's load and waved through 20 bottles out of
    a session holding 3. Whichever way the tap is handled now — the conversation
    ended (once the picker state gets its menu escape) or the ceiling is gone
    (``flow_state``) — the invariant is the same and is what this asserts: a
    transfer the driver abandoned can never reach the backend.
    """
    harness = await build_staff(monkeypatch)
    staff_member, labels = await sign_in(harness)

    await open_conversation(harness, staff_member, "staff_bottle_transfer")
    await harness.send(staff_member.text(menu_label(labels, "staff.menu.cash")))

    # A day of deliveries later: the session is down to 3 bottles.
    harness.backend.route(
        "GET", SESSION_ENDPOINT, lambda _call: {**OPEN_SESSION, "current_inventory": 3}
    )
    harness.backend.route("POST", TRANSFERS_ENDPOINT, lambda _call: {"transfer_ref": "TRF00001"})

    harness.telegram.reset()
    await harness.send(staff_member.tap("staff_transfer_driver_12"))
    # 20 is impossible against the 3 bottles actually on the van, and was
    # accepted for as long as the morning's 25 outlived the flow.
    await harness.send(staff_member.text("20"))

    assert not [c for c in harness.backend.calls if c.endpoint == TRANSFERS_ENDPOINT], (
        "an abandoned transfer still reached the backend: 20 bottles handed to a colleague "
        "out of a session holding 3"
    )


async def test_abandoning_create_client_at_the_language_step_can_no_longer_create_the_client(
    monkeypatch,
):
    """Was: ``test_RATCHET_abandoning_create_client_at_the_language_step_leaves_it_armed``.

    An operator fills in a client's phone, first name and last name, is shown
    the inline language picker, and then taps "💰 Cash". The cash hub opens, so
    it looks like they left.

    ``CREATE_USER_LANG`` is callback-only, so for a long time the conversation
    itself did not end (a wiring fix in ``staff_bot/bot.py``, pinned by
    ``test_every_conversation_state_has_a_menu_escape_wired``) and the language
    buttons on the message above stayed live. What used to make that dangerous
    was the SECOND half: ``new_client``
    was cleared by ``_conv_menu_escape`` and by nothing else, so a menu tap that
    landed OUTSIDE a text state left the half-entered client fully populated —
    and the stale language button walked it all the way to a real POST.

    ``new_client`` now belongs to ``flow_state``'s one key list, so leaving by
    ANY route drops it and the abandoned flow has nothing left to write.
    """
    harness = await build_staff(monkeypatch)
    staff_member, labels = await sign_in(harness)
    harness.backend.route("GET", OPERATOR_SEARCH_ENDPOINT, lambda _call: [])
    harness.backend.route(
        "POST", OPERATOR_USERS_ENDPOINT, lambda _call: {"id": 4242, "phone": "+998901234567"}
    )

    await open_conversation(harness, staff_member, "staff_create_user")
    await harness.send(staff_member.text("+998901234567"))
    await harness.send(staff_member.text("Nodira"))
    await harness.send(staff_member.text("Yusupova"))
    assert harness.conversation_state("staff_create_user") == CREATE_USER_LANG
    language_buttons = harness.telegram.last_shown().callback_data()
    assert "staff_op_lang_uz" in language_buttons, language_buttons

    harness.telegram.reset()
    await harness.send(staff_member.text(menu_label(labels, "staff.menu.cash")))

    assert harness.telegram.shown, "the tap did at least open the cash hub"
    assert "new_client" not in user_data(harness), (
        "the half-entered client survived the operator walking away, so the stale language "
        "buttons above can still walk it to a write"
    )

    # The stale buttons on the abandoned message no longer reach a write.
    await harness.send(staff_member.tap("staff_op_lang_uz"))
    await harness.send(staff_member.tap("staff_op_confirm_create_user"))

    assert not [
        call for call in harness.backend.calls if call.endpoint == OPERATOR_USERS_ENDPOINT
    ], "an abandoned create-client flow still created the client"


async def test_the_same_abandonment_one_step_earlier_is_also_clean(monkeypatch):
    """The control for the test above: escaping from a TEXT state is correct.

    Kept next to it deliberately. It WAS the evidence that the difference used
    to be the STATE and not the flow — the same conversation, the same button,
    the same driver, and a completely different outcome depending on whether the
    state the operator happened to be parked in read text. Wave 3 wired the menu
    escape to callback-only states too, so both now behave identically and this
    pair guards that they stay that way.

    Renamed out of the RATCHET namespace on 2026-08-22: it asserts correct
    behaviour, and leaving RATCHET in the name reads as "a defect is still
    pinned here" to anyone grepping for remaining work.
    """
    harness = await build_staff(monkeypatch)
    staff_member, labels = await sign_in(harness)
    harness.backend.route("GET", OPERATOR_SEARCH_ENDPOINT, lambda _call: [])
    harness.backend.route(
        "POST", OPERATOR_USERS_ENDPOINT, lambda _call: {"id": 4242, "phone": "+998901234567"}
    )

    await open_conversation(harness, staff_member, "staff_create_user")
    await harness.send(staff_member.text("+998901234567"))
    assert harness.conversation_state("staff_create_user") is not None

    await harness.send(staff_member.text(menu_label(labels, "staff.menu.cash")))

    assert harness.conversation_state("staff_create_user") is None
    assert "new_client" not in user_data(harness), (
        "the escape must drop the half-entered client, not just end the conversation"
    )

    # A stale button from the abandoned attempt is inert.
    harness.telegram.reset()
    await harness.send(staff_member.tap("staff_op_confirm_create_user"))
    assert not [
        call for call in harness.backend.calls if call.endpoint == OPERATOR_USERS_ENDPOINT
    ], "a stale confirm button wrote a client after the flow was properly closed"


async def test_a_menu_tap_at_the_language_picker_re_prompts_instead_of_choosing_english(
    monkeypatch,
):
    """Regression: ``staff_bot_language_picker_silent_english``.

    ``staff_auth``'s ``SELECT_LANGUAGE`` state has no menu escape, and its
    ``MessageHandler`` accepts ANY text. ``language_selected`` used to answer
    ``'en'`` for anything it did not recognise, so a staff member whose phone
    still showed a main-menu keyboard from an earlier session tapped "💰 Cash"
    at the picker and was silently logged in as an ENGLISH user — their stored
    ``uz`` preference overwritten in the database by ``update_user_language``.

    (The tap does also reach the cash hub: ``staff_auth`` lives in handler group
    -2 and the catch-all router in group 0, so both run. The navigation working
    is what made the language flip invisible.)

    A text that is not one of the picker's OWN buttons is not a language
    choice: the picker re-prompts, nothing is written, and nobody is logged in
    under a language they did not pick. ``/start`` and ``/cancel`` still leave
    the state (the handler is registered ``~filters.COMMAND``), so the
    re-prompt cannot trap anyone.
    """
    harness = await build_staff(monkeypatch, language="uz", staff_roles_in_db=[])
    person = harness.updates()
    assert harness.database.staff_user["preferred_language"] == "uz", (
        "fixture: this person's stored preference is Uzbek, and that is what the tap used to lose"
    )

    await harness.send(person.command("start"))
    assert harness.conversation_state("staff_auth") == AUTH_SELECT_LANGUAGE, (
        "fixture: an unlinked person should be parked on the language picker"
    )
    picker_labels = ["🇺🇸 English", "🇺🇿 O'zbekcha", "🇷🇺 Русский"]
    assert harness.telegram.last_shown().button_labels() == picker_labels

    harness.telegram.reset()
    harness.database.executed.clear()
    harness.backend.calls.clear()

    await harness.send(person.text(f"💰 {_curated('staff.menu.cash', 'uz')}"))

    assert user_data(harness).get("language") != "en", (
        "an unrecognised tap was read as a language choice again — the staff member is "
        "now an English user without ever having said so"
    )
    assert not any(
        "preferred_language" in query for query in harness.database.executed
    ), "nothing the staff member did not choose may reach users.preferred_language"
    # (The catch-all router in group 0 still tries to authenticate the tap on
    # its own — that path reads the stored preference, it never writes one, and
    # it is what the assertion above pins.)

    assert harness.conversation_state("staff_auth") == AUTH_SELECT_LANGUAGE, (
        "the staff member must stay on the picker until they actually pick"
    )
    reprompts = [
        call for call in harness.telegram.shown if call.button_labels() == picker_labels
    ]
    assert reprompts, (
        "the picker went silent: the tap was swallowed and the staff member is left "
        f"staring at a dead bot. shown={[call.text for call in harness.telegram.shown]}"
    )
    assert reprompts[-1].text == _curated("staff.select_language", "uz"), (
        "the re-prompt must be seeded copy in the language the picker was drawn in"
    )

    # And the real button still works, in the language it names.
    harness.telegram.reset()
    await harness.send(person.text("🇷🇺 Русский"))

    assert user_data(harness)["language"] == "ru"
    assert harness.conversation_state("staff_auth") is None


async def test_leaving_a_flow_clears_every_key_that_flow_wrote(monkeypatch):
    """Was: ``test_RATCHET_the_conversation_escape_and_the_catch_all_router_clear_different_things``.

    Leaving a flow by menu tap runs one of two clean-up paths depending on which
    state the driver was standing in — ``_conv_menu_escape`` when the state
    reads text, ``_handle_text_message`` when it does not — and the two used to
    clear DIFFERENT key sets. The escape dropped its own
    ``_CONVERSATION_WORK_KEYS`` list (``new_client``/``new_order``/
    ``new_address``/``new_tryout``) plus the ``flow_state`` flags; the router
    dropped only the flags. So the same gesture left different residue depending
    on where it landed, and four keys production actually writes —
    ``adding_address_for``, ``pending_transfer_available``,
    ``pending_transfer_receiver_id``, ``pending_confirm_transfer_id`` — were in
    NEITHER list and were cleared by nothing at all.

    There is now one clean-up (``flow_state.clear_pending_flows``) over one key
    list (``flow_state.PENDING_FLOW_USER_DATA_KEYS``), and both paths call it.
    This drives the two flows whose orphans were invisible.
    """
    harness = await build_staff(monkeypatch)
    staff_member, labels = await sign_in(harness)

    # `start_add_address` stamps the client id it is collecting an address for.
    await open_conversation(harness, staff_member, "staff_add_address")
    assert user_data(harness)["adding_address_for"] == 77
    assert user_data(harness)["new_address"] == {}

    await harness.send(staff_member.text(menu_label(labels, "staff.menu.cash")))

    assert harness.conversation_state("staff_add_address") is None
    assert "new_address" not in user_data(harness), (
        "the escape's own working-key list stopped working"
    )
    assert "adding_address_for" not in user_data(harness), (
        "the address flow's client id outlived the flow: the next address the operator "
        "adds anywhere is filed against a customer they walked away from"
    )

    # The receiver-side transfer flow left its own orphan behind.
    await open_conversation(harness, staff_member, "staff_bottle_transfer_confirm")
    assert user_data(harness)["pending_confirm_transfer_id"] == 9
    await harness.send(staff_member.text(menu_label(labels, "staff.menu.cash")))

    assert harness.conversation_state("staff_bottle_transfer_confirm") is None
    assert "pending_confirm_transfer_id" not in user_data(harness)
    assert "pending_confirm_transfer_qty" not in user_data(harness)


ORPHANED_UNTIL_THE_SSOT_COVERED_THEM = (
    "adding_address_for",
    "pending_transfer_available",
    "pending_transfer_receiver_id",
    "pending_confirm_transfer_id",
)


def test_no_second_list_of_flow_keys_survives_outside_the_ssot():
    """``flow_state`` is the only place that may enumerate a flow's keys.

    ``StaffBot._CONVERSATION_WORK_KEYS`` was the second list, consumed by
    ``_conv_menu_escape`` alone, and its existence IS the bug this file's
    ratchets recorded: two enumerations of one rule, kept in step by nothing.
    Its contents live in ``flow_state.PENDING_FLOW_USER_DATA_KEYS`` now, so the
    attribute should be deleted outright — and until it is, this holds it to a
    subset so a key can never be added there and nowhere else.

    Passes either way: an empty/absent attribute is the end state.
    """
    from staff_bot.bot import StaffBot

    second_list = set(getattr(StaffBot, "_CONVERSATION_WORK_KEYS", ()) or ())
    assert second_list <= set(PENDING_FLOW_USER_DATA_KEYS), (
        f"{sorted(second_list - set(PENDING_FLOW_USER_DATA_KEYS))} is enumerated by "
        "StaffBot._CONVERSATION_WORK_KEYS and not by flow_state — so the catch-all "
        "router does not clear it and the same gesture means two things again"
    )


@pytest.mark.parametrize("key", ORPHANED_UNTIL_THE_SSOT_COVERED_THEM)
def test_the_keys_no_clean_up_path_owned_are_registered_with_the_ssot(key):
    """The four keys that were in neither list, named so they cannot fall out again.

    A key production writes but never registers is invisible to every escape
    route AND to the sweep that parametrises over the tuple — which is exactly
    how these four survived a bot that has two documented state-leak incidents.
    """
    assert key in PENDING_FLOW_USER_DATA_KEYS


async def test_both_clean_up_paths_leave_exactly_the_same_residue(monkeypatch):
    """One rule, one implementation — asserted by comparing the two paths' output.

    A main-menu tap reaches its clean-up through ``_conv_menu_escape`` when the
    driver is parked in a conversation text state and through the catch-all
    ``_handle_text_message`` when they are not. Those are two call sites, and
    the incident this file exists for is what happens when two call sites grow
    two key lists: the driver's next message is swallowed by a flow they believe
    they left.

    So stamp EVERY key the SSOT knows about, take the same tap by both routes,
    and require the leftovers to be identical. A future key added to one path's
    list and not the other fails here regardless of which key it is.
    """
    residues = []
    for inside_a_conversation in (True, False):
        harness = await build_staff(monkeypatch)
        staff_member, labels = await sign_in(harness)
        if inside_a_conversation:
            # ENTER_PHONE is a text state, so the tap routes to `_conv_menu_escape`.
            await open_conversation(harness, staff_member, "staff_create_user")

        for key in PENDING_FLOW_USER_DATA_KEYS:
            user_data(harness)[key] = {"stamped_by": "the test"}

        await harness.send(staff_member.text(menu_label(labels, "staff.menu.cash")))
        residues.append(
            sorted(key for key in PENDING_FLOW_USER_DATA_KEYS if key in user_data(harness))
        )

    escape_residue, router_residue = residues
    assert escape_residue == [], (
        f"the conversation escape left {escape_residue} armed"
    )
    assert escape_residue == router_residue, (
        "the two clean-up paths disagree again: the conversation escape left "
        f"{escape_residue} and the catch-all router left {router_residue}. One rule, "
        "one implementation — see flow_state.clear_pending_flows."
    )


# ---------------------------------------------------------------------------
# Leaving by doing nothing at all
# ---------------------------------------------------------------------------


async def fire_timeout(harness, name, last_update):
    """Run a conversation's ``TIMEOUT`` state the way PTB's timeout job does.

    ``ConversationHandler._trigger_timeout`` re-offers the staff member's LAST
    real update to every handler registered under ``ConversationHandler.TIMEOUT``
    and then forces the conversation to END. That last part is why a missing
    TIMEOUT state is invisible: the flow really does end, in complete silence,
    and the driver is left on a prompt whose buttons are dead.

    Driving PTB's job queue from a test would mean waiting out a real 300s
    timer, so this reproduces the job's own loop against the REAL registered
    handlers instead of asserting on the state dict.
    """
    from telegram.ext import CallbackContext

    conv = harness.conversation(name)
    handlers = conv.states.get(ConversationHandler.TIMEOUT, [])
    assert handlers, f"{name} has no TIMEOUT state: it expires in silence"
    context = CallbackContext.from_update(last_update, harness.application)
    for handler in handlers:
        check = handler.check_update(last_update)
        if check is not None and check is not False:
            await handler.handle_update(last_update, harness.application, check, context)
    # PTB's job forces the conversation to END afterwards, whatever the
    # handlers returned. Mirrored here so the state left behind is the real one.
    conv._conversations.pop(
        (DEFAULT_DRIVER_TELEGRAM_ID, DEFAULT_DRIVER_TELEGRAM_ID), None
    )


async def test_a_flow_abandoned_at_a_text_prompt_says_so_when_it_expires(monkeypatch):
    """Five minutes of silence must not read as a working bot.

    ``conversation_timeout`` is not self-announcing: PTB looks for handlers
    under the TIMEOUT key, finds none, and ends the conversation without a
    word. Eleven staff conversations set a timeout and none of them had one.
    """
    harness = await build_staff(monkeypatch)
    staff_member, labels = await sign_in(harness)
    harness.backend.route("GET", OPERATOR_SEARCH_ENDPOINT, lambda _call: [])

    await open_conversation(harness, staff_member, "staff_create_user")
    last = staff_member.text("+998901234567")
    await harness.send(last)
    assert user_data(harness).get("new_client") is not None, "fixture: half-entered client"

    harness.telegram.reset()
    await fire_timeout(harness, "staff_create_user", last)

    answer = harness.telegram.last_shown()
    assert answer.text == _curated("staff.flow_timed_out", "en"), (
        f"the expiry was announced as {answer.text!r}"
    )
    assert answer.button_labels() == labels, (
        "the staff member must be handed back the menu they can act from"
    )
    assert "new_client" not in user_data(harness), (
        "the expired flow left its half-entered client for the next flow to trip over"
    )


async def test_a_flow_abandoned_on_an_inline_button_also_says_so_when_it_expires(monkeypatch):
    """The other half of the same rule.

    PTB re-offers the LAST update, so a flow abandoned on an inline step times
    out on a CALLBACK QUERY. A TIMEOUT state carrying only a ``MessageHandler``
    is silent for every flow that ends on a button — which is most of the
    bottle ones, since their last step before the prompt is a tap.
    """
    harness = await build_staff(monkeypatch)
    staff_member, labels = await sign_in(harness)

    harness.backend.route("GET", SESSION_ENDPOINT, lambda _call: None)
    harness.telegram.reset()
    last = staff_member.tap("staff_bottle_log_loaded")
    await harness.send(last)
    assert harness.conversation_state("staff_bottle_loaded") is not None, (
        "fixture: the driver is parked on the bottle-count prompt"
    )

    harness.telegram.reset()
    await fire_timeout(harness, "staff_bottle_loaded", last)

    answer = harness.telegram.last_shown()
    assert answer.text == _curated("staff.flow_timed_out", "en")
    assert answer.button_labels() == labels


# ---------------------------------------------------------------------------
# Leaving by the inline navigation buttons
# ---------------------------------------------------------------------------


async def test_the_cash_hub_back_button_closes_the_bottle_flow_it_was_tapped_in(monkeypatch):
    """The bottle prompts carry an inline back button to the cash hub.

    PTB handles at most ONE handler per group and ``application.handlers[group]``
    is insertion-ordered, so the plain
    ``CallbackQueryHandler(show_cash_hub, pattern="^staff_cash_hub$")`` used to
    be registered in group 0 BEFORE the conversations and shadowed the
    conversation fallback that would have ENDED the flow. The driver tapped
    "⬅️ Back", the cash hub rendered, and the flow stayed armed — so their next
    typed number opened a bottle session.

    Fixed by registering the hub in group 1, below every conversation: the
    fallback ends the flow in group 0 and the hub renders once in group 1.
    """
    harness = await build_staff(monkeypatch)
    staff_member, _labels = await sign_in(harness)
    harness.backend.route("POST", OPEN_SESSION_ENDPOINT, lambda _call: dict(OPEN_SESSION))

    await open_conversation(harness, staff_member, "staff_bottle_loaded")

    harness.telegram.reset()
    await harness.send(staff_member.tap("staff_cash_hub"))

    assert harness.conversation_state("staff_bottle_loaded") is None, (
        "the bottle-quantity prompt is still armed after the driver tapped Back to the "
        "cash hub; their next typed number opens a session"
    )
    hub_renders = [
        call for call in harness.telegram.shown
        if _curated("staff.cash.hub_title", "en") in call.text
    ]
    assert len(hub_renders) == 1, (
        f"the cash hub was drawn {len(hub_renders)} times for one tap — the conversation "
        "fallback and the global handler are both rendering it"
    )

    harness.backend.calls.clear()
    await harness.send(staff_member.text("12"))
    assert not [
        call for call in harness.backend.calls if call.endpoint == OPEN_SESSION_ENDPOINT
    ], "a number typed after backing out to the cash hub opened a bottle session"


# ---------------------------------------------------------------------------
# The other two ways a staff member gives up
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ENTRY_TAP))
async def test_the_cancel_command_closes_every_staff_conversation(monkeypatch, name):
    """``/cancel`` is the escape hatch the bot's own command menu advertises.

    It is also the only one that works from a callback-only state, which makes
    it the last line of defence for the flows the menu tap cannot leave. Every
    conversation lists it as a fallback; a fallback that returned ``None``
    instead of ``ConversationHandler.END`` would re-render its message and keep
    the driver exactly where they were — the same shape as the
    ``BOTTLE_SESSION_REQUIRED`` trap this bot already shipped.
    """
    harness = await build_staff(monkeypatch)
    staff_member, _labels = await sign_in(harness)
    errors = capture_errors(harness)

    await open_conversation(harness, staff_member, name)
    await harness.send(staff_member.command("cancel"))

    assert errors == [], f"/cancel inside {name} raised {errors}"
    assert harness.conversation_state(name) is None, (
        f"/cancel did not close {name}; the staff member is still trapped in it"
    )
    assert harness.telegram.shown, f"/cancel inside {name} answered with nothing"


async def test_cancelling_a_bottle_flow_keeps_the_drivers_menu_on_screen(monkeypatch):
    """Was: ``test_RATCHET_cancelling_a_bottle_flow_takes_the_drivers_menu_away``.

    The five bottle conversations have no cancel handler of their own, so
    ``bot.py`` used to wire ``start_handler.cancel`` — the handler written for
    abandoning LOGIN. It answered "Authentication cancelled." (simply untrue:
    the driver is still logged in) and, far worse, sent ``ReplyKeyboardRemove``.

    ``MenuKeyboards.main_menu`` is built with ``is_persistent=True`` precisely
    because a driver's control surface must always be on screen. One ``/cancel``
    at a bottle-count prompt removed it, and nothing put it back until the
    driver guessed ``/start`` — from the road, with no visible buttons to guess
    from.

    Both halves are asserted: the copy names the BOTTLE flow, and the reply
    re-attaches the main menu the driver already had.
    """
    harness = await build_staff(monkeypatch)
    staff_member, labels = await sign_in(harness)

    await open_conversation(harness, staff_member, "staff_bottle_loaded")
    await harness.send(staff_member.command("cancel"))

    answer = harness.telegram.last_shown()
    assert answer.text == _curated("staff.bottle_flow_cancelled", "en"), (
        f"cancelling a bottle flow answered with {answer.text!r}; the LOGIN copy tells a "
        "driver who is still signed in that their authentication was cancelled"
    )
    assert answer.reply_markup != {"remove_keyboard": True}, (
        "the driver's persistent main menu was stripped by a bottle-flow cancel"
    )
    assert answer.button_labels() == labels, (
        "the cancel must put back the exact keyboard the driver already had"
    )
    assert harness.conversation_state("staff_bottle_loaded") is None

    # The same gesture in an operator flow keeps its own copy.
    await open_conversation(harness, staff_member, "staff_create_user")
    await harness.send(staff_member.command("cancel"))
    operator_answer = harness.telegram.last_shown()
    assert operator_answer.text == _curated("staff.cancelled", "en")
    assert operator_answer.reply_markup != {"remove_keyboard": True}, (
        "the operator flows have regressed into stripping the keyboard too"
    )


@pytest.mark.parametrize("name", sorted(ENTRY_TAP))
async def test_start_closes_the_flow_the_staff_member_is_parked_in(monkeypatch, name):
    """Was: ``test_RATCHET_start_does_not_close_the_flow_the_staff_member_is_parked_in``.

    ``/start`` is what everyone does when a bot seems stuck, and this one
    answers it exactly like a reset: it clears every flow flag and re-renders
    the main menu with "Welcome back". It did not end the conversation.

    The reason was structural: ``staff_auth`` sits in handler group -2 and
    consumes the ``/start`` there, while the ten conversations in group 0 listed
    only ``CommandHandler("cancel")`` as a fallback — nothing in them matched
    ``/start`` at all. So the driver was shown the main menu and believed they
    were back at the top, while the flow they were in stayed armed and kept
    outranking the catch-all router.

    ``StartHandler.start`` calls itself "a hard reset to the top of the bot";
    every conversation now agrees with it.
    """
    harness = await build_staff(monkeypatch)
    staff_member, _labels = await sign_in(harness)

    await open_conversation(harness, staff_member, name)
    await harness.send(staff_member.command("start"))

    assert harness.telegram.shown, "/start showed the staff member nothing"
    assert harness.conversation_state(name) is None, (
        f"/start did not close {name}; the staff member is looking at the main menu "
        "while the flow they left is still armed to swallow their next message"
    )


async def test_a_number_typed_after_start_no_longer_opens_a_bottle_session(monkeypatch):
    """Was: ``test_RATCHET_a_number_typed_after_start_still_opens_a_bottle_session``.

    A driver at the depot taps "Log bottles loaded", is asked for a count, gets
    distracted, and hits ``/start`` to get back to a clean slate. The bot
    welcomes them back and draws the main menu, so they believe it worked.

    The next bare number they typed — an order number they were about to search
    for, a quantity meant for something else — was still captured by the prompt
    they thought they had escaped, and it opened a real bottle session against
    their name. Sessions are the ledger every bottle in their van is counted
    against, so it was a write they never asked for.
    """
    harness = await build_staff(monkeypatch)
    staff_member, _labels = await sign_in(harness)
    harness.backend.route("POST", OPEN_SESSION_ENDPOINT, lambda _call: dict(OPEN_SESSION))

    await open_conversation(harness, staff_member, "staff_bottle_loaded")
    await harness.send(staff_member.command("start"))
    assert harness.conversation_state("staff_bottle_loaded") is None

    harness.backend.calls.clear()
    harness.telegram.reset()
    await harness.send(staff_member.text("12"))

    assert not [
        call for call in harness.backend.calls if call.endpoint == OPEN_SESSION_ENDPOINT
    ], "a number typed after a /start reset opened a bottle session the driver never asked for"
    assert harness.telegram.shown, "the number was swallowed with no reply at all"
