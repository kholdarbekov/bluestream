"""Traced production incidents from BOTH bots, pinned so they cannot come back.

Every test in this file names one incident, its date, and the evidence it was
diagnosed from. Nothing here is hypothetical: each one is a defect that reached
real customers or real drivers, was root-caused, and was fixed.

The incidents
-------------
1. **Shared-pin address loss** — customer bot, 30 days to 2026-08-21 (Loki).
   33 pin flows reached the title step; 13 wrote an address. The other 20
   evaporated, because the flow persisted only at the TERMINAL step of a chain
   of questions it itself renders a *Skip* button for. Traced session: telegram
   user 1009661971, 2026-08-19 11:35-11:44 — pin, title, two Skips, then
   silence. Fixed 2026-08-21 (`ProfileHandlers._create_address_now`).
2. **A corrected pin used to strand the first one** — `location_received` is a
   conversation ENTRY POINT and `address_conversation` sets
   `allow_reentry=True`, so a customer who spots a bad pin and drops a better
   one re-enters the flow with an address already in hand. Creating a second
   row would leave the first at the WRONG coordinates for a driver to drive to.
3. **A conversation that expired in silence** — `address_conversation` carried
   `conversation_timeout=600` with no `ConversationHandler.TIMEOUT` state, so
   PTB ended it without telling anyone and left `temp_address_data` /
   `address_flow_origin` stranded in `user_data`. A stale
   `address_flow_origin == 'checkout'` then hijacks the NEXT, unrelated address
   save and bounces that customer into checkout.
4. **A street name that killed the flow** — `location_received` interpolated the
   reverse-geocoded address RAW into a `parse_mode='Markdown'` message. A street
   carrying `_`, `*`, `[` or a backtick made Telegram refuse the message, the
   handler's `except Exception` returned `ConversationHandler.END`, and the
   customer saw "location received" and then nothing — deterministically,
   forever, for everyone living on that street.
5. **A guard no test could see** — the two dispatcher middlewares (debug logger
   at group -10, callback-dedup at group -5) were registered in
   `WaterBusinessBot.initialize()` rather than `_setup_handlers()`. Every
   harness-built bot therefore ran WITHOUT the callback-dedup guard, so a
   double-tap regression was invisible to every dispatcher test in the repo.
6. **The shared HTTP client closed under other handlers** — 2026-08-13
   (commit 15a0501). `BusinessAPIClient` is a module-level singleton; its
   `__aexit__` used to close the client, which was survivable only while PTB
   processed updates strictly one at a time. Concurrent processing turned it
   into `RuntimeError: client has been closed` for whoever was still in flight.
7. **Staff-bot update timing could not be instrumented** — 2026-08-13
   (commit 15a0501, "restore staff_bot /health"). Drivers reported the bot
   hanging; the bot logged an update's arrival and then nothing, so "slow" could
   not be told from "idle". `application.process_update = ...` is impossible —
   PTB's `Application` defines `__slots__` — so the fix had to be a subclass.
8. **"Invalid cash amount"** — staff bot. The main menu is a REPLY keyboard, so
   every tap is ordinary TEXT. `StaffBot._handle_text_message` used to route
   that text into whatever `pending_*_flow` was armed BEFORE checking whether it
   was a menu label, so a driver tapping "Cash" mid-flow was told their button
   was not a number — or worse, had it consumed as the NOTE that finalises a
   real transaction.
9. **One slow handler blocked every user** — both bots. PTB's default is to
   process updates strictly serially, so one driver's slow backend call delayed
   everyone else's taps. Fixed with `PerChatSerialUpdateProcessor`.
10. **The MANUAL address flow looped forever** — customer bot, caught during the
    2026-08-07 geo-address-details work and written up as a correction in
    `docs/superpowers/plans/2026-08-07-telegram-geo-address-details.md` (the
    "Correction (made during implementation)" note). The change that saves a
    pinned address early was applied to the title step WITHOUT the
    `_is_shared_pin_address` gate, and `ADDRESS_TITLE` is reached from
    `confirm_geocode` too — so a manually typed address ran
    `title → apartment → floor → instructions → geocode → confirm → title → …`
    and could never be saved. The doc marks that gate load-bearing; nothing
    tested it.

Why these are DISPATCHER tests
------------------------------
Six of the ten live in the seam BETWEEN handlers — conversation state, handler
groups, entry-point re-entry, middleware ordering. A test that calls a handler
coroutine directly cannot see any of that, which is exactly how a suite of
eighty-odd bot test files shipped incident 1. So every behavioural test here
goes in through `Application.process_update` on an application built by the REAL
`_setup_handlers()`, and the two structural pins (5 and 9) read the production
syntax tree rather than trusting a comment.
"""

from __future__ import annotations

import ast
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.append(str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
# `tests/regression/` has no conftest of its own, and BOTH bots are driven from
# this one file. Each bot's import bootstrap lives in ITS conftest, which pytest
# only loads when that directory is collected — so running this file alone would
# otherwise fail at collection:
#
#   * the customer bot's modules import by BARE name (`from config import
#     config`), which needs `telegram_bot/` ranked FIRST on sys.path, and
#   * `staff_bot/bot.py` runs with WORKDIR=/app/staff_bot in production, so its
#     `from logging_config import ...` needs the alias the staff conftest installs.
#
# They are imported here rather than re-implemented so this file cannot drift
# from the bootstrap the two bot suites actually run under. ORDER MATTERS and is
# deliberate: a full `tests/` run collects `tests/staff_bot/` before
# `tests/telegram_bot/`, so the staff aliases win today. Importing the staff
# bootstrap first reproduces exactly that, rather than silently changing which
# bot owns the bare `logging_config` name for the rest of the worker process.
import tests.staff_bot.conftest  # noqa: E402,F401  (import for side effects)
import tests.telegram_bot.conftest as _customer_bootstrap  # noqa: E402

_customer_bootstrap._prioritise_bot_path()

from telegram.ext import (  # noqa: E402
    CallbackContext,
    ConversationHandler,
    TypeHandler,
)
from telegram.ext._handlers.conversationhandler import (  # noqa: E402
    _ConversationTimeoutContext,
)

# Bare-name imports, resolved as the CUSTOMER bot's modules by the bootstrap above.
from handlers import callback_dedup  # noqa: E402
from handlers.profile import (  # noqa: E402
    ADDRESS_APARTMENT,
    ADDRESS_BUILDING,
    ADDRESS_DELIVERY_INSTRUCTIONS,
    ADDRESS_DISTRICT,
    ADDRESS_FLOOR,
    ADDRESS_GEOCODE_CONFIRM,
    ADDRESS_LOCATION,
    ADDRESS_REGION,
    ADDRESS_STREET,
    ADDRESS_TITLE,
)

from tests.staff_bot.ptb_harness import (  # noqa: E402
    DEFAULT_DRIVER_TELEGRAM_ID,
    FakeStaffDatabase,
    build_staff_harness,
)
from tests.telegram_bot.ptb_harness import (  # noqa: E402
    DEFAULT_USER_ID,
    build_bot_harness,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


def _source(*parts: str) -> str:
    """Read one production file, for the structural pins."""
    return _REPO_ROOT.joinpath(*parts).read_text(encoding="utf-8")


def _bot_ast(bot_dir: str) -> ast.Module:
    """A bot's `bot.py` parsed, not grepped.

    Every structural pin below reads the SYNTAX TREE. Grepping the file cannot
    tell code from a comment, and both of these files carry long comments that
    quote the very construct they are warning against — `telegram_bot/bot.py`
    contains the words "A bare `.concurrent_updates(True)` is not safe here",
    which a substring assertion reads as the bug it is describing.
    """
    return ast.parse(_source(bot_dir, "bot.py"))


def _function_named(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name}() no longer exists — re-read this test before changing it")


def _type_handler_groups(scope: ast.AST) -> dict:
    """``{middleware callback name: handler group}`` for TypeHandlers added in `scope`.

    Only `add_handler(TypeHandler(Update, <name>), group=<int>)` counts, which
    is exactly the shape both dispatcher middlewares are registered in.
    """
    found = {}
    for node in ast.walk(scope):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "add_handler" or not node.args:
            continue
        handler = node.args[0]
        if not (
            isinstance(handler, ast.Call)
            and isinstance(handler.func, ast.Name)
            and handler.func.id == "TypeHandler"
            and len(handler.args) > 1
            and isinstance(handler.args[1], ast.Name)
        ):
            continue
        group = next((kw.value for kw in node.keywords if kw.arg == "group"), None)
        found[handler.args[1].id] = 0 if group is None else ast.literal_eval(group)
    return found


def _calls_to(scope: ast.AST, attribute: str) -> list:
    """Every ``….<attribute>(…)`` call in `scope`, as its argument list."""
    return [
        node.args
        for node in ast.walk(scope)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attribute
    ]


# ---------------------------------------------------------------------------
# The customer bot
# ---------------------------------------------------------------------------

# Stands in for the pin telegram user 1009661971 dropped on 2026-08-19. The
# trace carries the STEPS, not the coordinates, so what matters about these two
# numbers is only that they are inside TASHKENT_POLYGON and therefore survive
# the real delivery-zone SSOT in `location_received`.
PIN_LAT = 41.32354
PIN_LNG = 69.241036

# A second, different in-zone pin: the correction a customer drops when the
# street the bot detected is not theirs.
CORRECTED_LAT = 41.31122
CORRECTED_LNG = 69.27981

# Moscow. Outside every polygon this business delivers to.
OUT_OF_ZONE_LAT = 55.7558
OUT_OF_ZONE_LNG = 37.6173

GEOCODED = "15, Chilonzor dahasi, Toshkent shahri"

# Uzbek address strings really do carry these: building suffixes like "15_A",
# geocoder annotations in brackets, and the odd asterisk from an OSM note. Every
# one of them is a Markdown metacharacter, and incident 4 is what happened when
# they reached Telegram unescaped.
HOSTILE_GEOCODED = "Chilonzor 15_A, [Yangi] *uy*, `Toshkent`"

# The real seeded shapes. `detected_location_prefix` carries deliberate `*bold*`
# — that is WHY `parse_mode='Markdown'` is set at all, and why a stray `_` in
# `{address}` used to poison the whole message.
TRANSLATIONS = {
    "telegram.address.detected_location_prefix": "📍 *Aniqlangan joylashuv:*\n{address}\n\n",
    "telegram.address.title_prompt": "Manzilga nom bering:",
    "telegram.address.location_received": "Joylashuv qabul qilindi.",
    "telegram.address.saved_successfully": "✅ Manzil saqlandi!",
    "telegram.address.save_failed": "❌ Manzil saqlanmadi.",
    "telegram.address.outside_delivery_area": "Bu hudud yetkazib berish zonasidan tashqarida.",
    "telegram.address.flow_timed_out": "⏳ Manzil qo'shish vaqti tugadi.",
    "telegram.address.flow_timed_out_saved": "⏳ Vaqt tugadi — manzil saqlab qolindi.",
    "telegram.action_cancelled": "Bekor qilindi.",
    "telegram.action_cancelled_short": "Bekor qilindi.",
    "telegram.orders.no_address_prompt": "Buyurtma uchun manzil kerak.",
    "telegram.address.enter_manually_button": "✍️ Qo'lda kiritish",
    "telegram.cancel": "❌ Bekor qilish",
}


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    harness.backend.route(
        "POST",
        "/api/v1/addresses/reverse-geocode",
        lambda _c: {"data": {"formatted_address": GEOCODED}},
    )
    return harness


@pytest.fixture
def user(bot):
    return bot.updates()


def geocodes_to(bot, formatted_address: str):
    bot.backend.route(
        "POST",
        "/api/v1/addresses/reverse-geocode",
        lambda _c: {"data": {"formatted_address": formatted_address}},
    )


def address_writes(bot):
    """Every write the bot made to the address collection, in order."""
    return [
        call
        for call in bot.backend.calls
        if call.endpoint.startswith("/api/v1/auth/addresses")
        and call.method in {"POST", "PUT", "PATCH", "DELETE"}
    ]


def creates(bot):
    return [c for c in address_writes(bot) if c.method == "POST"]


def user_data(bot, user_id=DEFAULT_USER_ID):
    """The customer's live `user_data` dict, as the handlers see it."""
    return bot.application.user_data[user_id]


def deliberate_retap(bot, update):
    """Send a tap the customer made DELIBERATELY, seconds after an identical one.

    Since 2026-08-21 the harness carries production's callback-dedup guard
    (incident 5), which debounces `(user_id, callback_data)` for two real
    seconds. Firing the same button twice in the same microsecond therefore
    models an IMPATIENT DOUBLE-TAP — correct for the test that is ABOUT the
    debounce, wrong for a test that merely needs the same button pressed in two
    separate flows. Ageing the module's own lock table is what the wall clock
    would do, without putting two real seconds into the suite.
    """
    stale = time.monotonic() - 1
    for key in list(callback_dedup._in_memory_locks):
        callback_dedup._in_memory_locks[key] = stale
    return bot.send(update)


async def open_address_flow(bot, user, *, from_checkout: bool = False):
    """Tap "Add address" — the entry point that arms the location keyboard."""
    await bot.send(user.tap("add_new_address_checkout" if from_checkout else "add_new_address"))
    return bot.conversation_state("address_conversation")


async def drop_pin_and_name_it(bot, user, *, lat=PIN_LAT, lng=PIN_LNG, title="addr_title_work"):
    """The two steps that make an address DELIVERABLE: a pin, and a name."""
    await bot.send(user.location(lat, lng))
    assert bot.conversation_state("address_conversation") == ADDRESS_TITLE
    await deliberate_retap(bot, user.tap(title))
    return bot.conversation_state("address_conversation")


async def expire_the_conversation(bot, last_update):
    """Fire PTB's OWN timeout path, with only the wall clock simulated.

    `ConversationHandler._trigger_timeout` IS the production code that runs
    when `conversation_timeout` expires: it looks up the handlers registered
    under the `TIMEOUT` state key, offers each the LAST REAL update, dispatches
    the ones that claim it, and forces the conversation to END. It is invoked
    here with the same `_ConversationTimeoutContext` PTB's JobQueue would carry,
    so none of that dispatch is re-implemented in this file — a local copy of it
    would keep passing while PTB's real one stopped calling anything.

    What is faked is the timer, and only the timer: the harness never calls
    `Application.start()`, so no JobQueue is running to fire it after ten real
    minutes.
    """
    conversation = bot.conversation("address_conversation")
    assert conversation.states.get(ConversationHandler.TIMEOUT), (
        "no handler is registered under ConversationHandler.TIMEOUT, so PTB "
        "would end this conversation in total silence — incident 3"
    )

    key = conversation._get_key(last_update)
    timeout_context = _ConversationTimeoutContext(
        key,
        last_update,
        bot.application,
        CallbackContext.from_update(last_update, bot.application),
    )
    job = SimpleNamespace(data=timeout_context)
    async with conversation._timeout_jobs_lock:
        conversation.timeout_jobs[key] = job

    await conversation._trigger_timeout(SimpleNamespace(job=job))


# ===========================================================================
# INCIDENT 1 — the shared-pin flow lost 20 of 33 addresses
# ===========================================================================


async def test_incident_20260821_an_abandoned_pin_flow_still_leaves_a_usable_address(bot, user):
    """Telegram user 1009661971, 2026-08-19 11:35-11:44, step for step —
    including the part where they put the phone down.

    The trace stops on the delivery-instructions prompt with the bot holding a
    complete address (title + coordinates + reverse-geocoded street) in volatile
    `user_data` and NOTHING on the server. Twenty customers in thirty days
    walked away from exactly here, and every one of them came back to an empty
    address list.

    Depends on `ProfileHandlers.address_title_callback` calling
    `await self._create_address_now(update, context, language)` before it
    prompts for the apartment. Delete that line and this goes red.
    """
    assert await open_address_flow(bot, user) == ADDRESS_LOCATION

    await bot.send(user.location(PIN_LAT, PIN_LNG))          # shares the pin
    assert bot.conversation_state("address_conversation") == ADDRESS_TITLE

    await bot.send(user.tap("addr_title_work"))              # names it "Ish"
    assert bot.conversation_state("address_conversation") == ADDRESS_APARTMENT

    await bot.send(user.tap("skip_apartment"))               # no flat number
    assert bot.conversation_state("address_conversation") == ADDRESS_FLOOR

    await bot.send(user.tap("skip_floor"))                   # no floor either
    assert bot.conversation_state("address_conversation") == ADDRESS_DELIVERY_INSTRUCTIONS

    # ... and nothing more, ever.
    saved = list(bot.backend.addresses.values())
    assert len(saved) == 1, (
        "the pin the customer dropped evaporated — this is the incident, "
        f"backend saw {address_writes(bot)}"
    )
    (address,) = saved
    assert (address["latitude"], address["longitude"]) == (PIN_LAT, PIN_LNG)
    assert address["title"], "an address with no name is unusable in the picker"
    assert address["full_address"] == GEOCODED, (
        "a driver needs the street, not just a dot on a map"
    )


async def test_incident_20260821_the_address_is_born_at_the_title_step_not_at_the_end(bot, user):
    """WHERE the write happens is the whole fix, so it is asserted directly.

    Every question after the title renders a Skip button — the flow itself calls
    those fields optional. An address that is only written once the customer has
    answered all of them is an address held hostage to questions nobody has to
    answer. So the create must land BEFORE the first optional prompt goes out.
    """
    await open_address_flow(bot, user)
    await bot.send(user.location(PIN_LAT, PIN_LNG))

    bot.telegram.reset()
    calls_before = len(bot.backend.calls)
    await bot.send(user.tap("addr_title_home"))

    made = bot.backend.calls[calls_before:]
    posts = [c for c in made if c.method == "POST" and c.endpoint == "/api/v1/auth/addresses"]
    assert len(posts) == 1, f"expected exactly one create at the title step, got {made}"

    (created,) = creates(bot)
    assert created.data["latitude"] == PIN_LAT
    assert created.data["longitude"] == PIN_LNG
    assert created.data["title"], "the create must carry the name the customer picked"
    assert "apartment_number" not in created.data, (
        "the create payload is None-filtered: an unanswered optional field must "
        "not be written as an explicit blank"
    )

    prompt = bot.telegram.last_shown()
    assert "skip_apartment" in prompt.callback_data(), (
        "the customer is now being asked the first OPTIONAL question — which "
        "means the address was already safe when it was asked"
    )


async def test_incident_20260821_a_typed_title_creates_the_address_too(bot, user):
    """The suggestion buttons are not the only way to name an address.

    A customer who types "Dachaga" instead of tapping Home/Work enters through
    `address_title_received`, a completely different handler. It carries its own
    copy of the create call, and a fix applied to only one of the two would lose
    every address named by hand — a silent half-fix that looks green in a test
    that only ever taps buttons.
    """
    await open_address_flow(bot, user)
    await bot.send(user.location(PIN_LAT, PIN_LNG))

    await bot.send(user.text("Dachaga"))
    assert bot.conversation_state("address_conversation") == ADDRESS_APARTMENT

    (address,) = bot.backend.addresses.values()
    assert address["title"] == "Dachaga"
    assert (address["latitude"], address["longitude"]) == (PIN_LAT, PIN_LNG)


# ===========================================================================
# INCIDENT 10 — the same fix, applied one step too widely, looped the manual flow
# ===========================================================================


async def enter_manually(bot, user):
    """Tap "Enter manually" on the location keyboard, exactly as rendered.

    The label is read back off the keyboard the bot drew rather than retyped
    here: `ADDRESS_LOCATION` matches this button with a regex compiled from the
    SAME translation at handler-build time, so hard-coding the string would let
    the test pass with a label the router can no longer match.
    """
    offered = [label for call in bot.telegram.shown for label in call.button_labels()]
    assert TRANSLATIONS["telegram.address.enter_manually_button"] in offered, (
        "the location prompt does not offer manual entry, so the customer whose "
        f"phone will not give a GPS fix is stuck; it offered {offered}"
    )
    await bot.send(user.text(TRANSLATIONS["telegram.address.enter_manually_button"]))
    return bot.conversation_state("address_conversation")


def newest_button_starting(bot, prefix: str) -> str:
    """The newest button offered whose `callback_data` starts with `prefix`.

    Scanned backwards across everything the bot has shown rather than read off
    `last_shown()`, because the group-0 catch-all text handler ALSO claims every
    text the address conversation consumes — PTB runs one handler per GROUP —
    and it sometimes answers (a rate-limit notice, say). So the newest bubble on
    the customer's screen is not reliably the one the conversation just drew.
    That leak has its own ratchets in
    `tests/telegram_bot/test_support_and_text_routing.py`; here it is only a
    reason not to trust `last_shown()`.
    """
    for call in reversed(bot.telegram.shown):
        hits = [data for data in call.callback_data() if data.startswith(prefix)]
        if hits:
            return hits[0]
    raise AssertionError(
        f"no {prefix!r} button anywhere on screen; the bot showed "
        f"{[call.callback_data() for call in bot.telegram.shown]}"
    )


async def test_incident_20260807_a_manually_typed_address_saves_instead_of_looping(bot, user):
    """The customer with no GPS fix, typing their address out — end to end.

    `ADDRESS_TITLE` is reached from TWO places: `location_received` (a shared
    pin, which still has apartment/floor/instructions ahead of it) and
    `confirm_geocode` (a manually typed address, which reaches the title step
    LAST). Saving early at the title step is right for the first and fatal for
    the second: routing every title answer into the detail chain made the manual
    flow run `title → apartment → floor → instructions → geocode → confirm →
    title` forever, so a manually typed address could never be saved at all.

    Depends on the `if _is_shared_pin_address(context):` gate in
    `ProfileHandlers.address_title_callback`. Delete it — which is precisely
    what over-applying the incident-1 fix looks like — and the title tap below
    prompts for an apartment instead of ending the conversation.
    """
    await open_address_flow(bot, user)
    assert await enter_manually(bot, user) == ADDRESS_REGION

    region = newest_button_starting(bot, "region_")
    await bot.send(user.tap(region))
    assert bot.conversation_state("address_conversation") == ADDRESS_DISTRICT

    district = newest_button_starting(bot, "district_")
    await bot.send(user.tap(district))
    assert bot.conversation_state("address_conversation") == ADDRESS_STREET

    await bot.send(user.text("Amir Temur"))
    assert bot.conversation_state("address_conversation") == ADDRESS_BUILDING
    await bot.send(user.text("15"))
    assert bot.conversation_state("address_conversation") == ADDRESS_APARTMENT
    await bot.send(user.tap("skip_apartment"))
    await bot.send(user.tap("skip_floor"))

    # Nothing typed can be saved yet: a manual address has no coordinates until
    # it has been geocoded, so — unlike a pin — there is nothing to write early.
    assert creates(bot) == [], (
        f"a manual address was written before it was geocoded: {creates(bot)}"
    )

    await bot.send(user.tap("skip_delivery_instructions"))
    assert bot.conversation_state("address_conversation") == ADDRESS_GEOCODE_CONFIRM, (
        "the manual flow must geocode and ask the customer to confirm the pin "
        "it found, not save an address with no coordinates"
    )

    await bot.send(user.tap("confirm_geocode"))
    assert bot.conversation_state("address_conversation") == ADDRESS_TITLE

    await bot.send(user.tap("addr_title_home"))

    state = bot.conversation_state("address_conversation")
    assert state != ADDRESS_APARTMENT, (
        "the manual flow was sent back round the detail chain it has already "
        "finished — this is the loop, and it never terminates"
    )
    assert state is None, f"the flow did not end; it is parked in {state}"

    assert len(creates(bot)) == 1, f"expected exactly one create, got {address_writes(bot)}"
    (address,) = bot.backend.addresses.values()
    assert address["street_address"] == "Amir Temur"
    assert address["title"], "an address with no name is unusable in the picker"
    assert address["latitude"] and address["longitude"], (
        "a manually typed address must carry the coordinates the geocoder "
        f"returned, got {address}"
    )
    assert TRANSLATIONS["telegram.address.saved_successfully"] in bot.telegram.texts(), (
        "the customer was never told their address was saved"
    )


# ===========================================================================
# INCIDENT 2 — a corrected pin must MOVE the address, not orphan it
# ===========================================================================


async def test_incident_20260821_re_sharing_a_corrected_pin_moves_the_address(bot, user):
    """The customer looks at the detected street, sees it is wrong, and shares
    a better pin.

    `location_received` is a conversation ENTRY POINT and `address_conversation`
    sets `allow_reentry=True`, so PTB re-enters mid-flow rather than ignoring
    the second pin. Since 2026-08-21 the flow already owns a row by then. If
    that second pass CREATED instead of UPDATED, the customer would end up with
    two addresses and the first — the wrong one — would still be in their picker
    and still be deliverable-to.

    Depends on the `if existing_id:` branch of
    `ProfileHandlers._create_address_now`, which routes to
    `client.update_user_address` instead of `add_user_address`.
    """
    await open_address_flow(bot, user)
    await drop_pin_and_name_it(bot, user, title="addr_title_home")
    (first_id,) = bot.backend.addresses

    geocodes_to(bot, "7, Yakkasaroy, Toshkent shahri")
    await bot.send(user.location(CORRECTED_LAT, CORRECTED_LNG))
    assert bot.conversation_state("address_conversation") == ADDRESS_TITLE, (
        "the corrected pin must re-enter the flow, not fall through to the "
        "group-0 catch-all"
    )
    await deliberate_retap(bot, user.tap("addr_title_home"))

    assert len(bot.backend.addresses) == 1, (
        f"the corrected pin created a second address: {bot.backend.addresses}"
    )
    assert len(creates(bot)) == 1, "the address must be MOVED, not created twice"

    (address,) = bot.backend.addresses.values()
    assert list(bot.backend.addresses) == [first_id], "the row identity must be preserved"
    assert (address["latitude"], address["longitude"]) == (CORRECTED_LAT, CORRECTED_LNG), (
        "the driver would have been sent to the pin the customer corrected"
    )
    assert address["full_address"] == "7, Yakkasaroy, Toshkent shahri"


async def test_incident_20260821_a_corrected_pin_outside_the_zone_leaves_the_address_alone(
    bot, user
):
    """A mis-drag onto Moscow is not a correction.

    The delivery-zone SSOT rejects it in `location_received` BEFORE any of the
    coordinates reach `temp_address_data`, so the address already on the server
    must be untouched — not moved, not deleted, not duplicated — and the
    customer must be asked for a pin again rather than dropped.
    """
    await open_address_flow(bot, user)
    await drop_pin_and_name_it(bot, user)
    writes_before = len(address_writes(bot))

    await bot.send(user.location(OUT_OF_ZONE_LAT, OUT_OF_ZONE_LNG))

    assert bot.conversation_state("address_conversation") == ADDRESS_LOCATION, (
        "an out-of-zone pin must re-ask, not advance"
    )
    assert len(address_writes(bot)) == writes_before, (
        "an out-of-zone pin must not touch the saved address at all"
    )
    (address,) = bot.backend.addresses.values()
    assert (address["latitude"], address["longitude"]) == (PIN_LAT, PIN_LNG)
    assert TRANSLATIONS["telegram.address.outside_delivery_area"] in bot.telegram.texts()


async def test_incident_20260821_cancelling_after_a_correction_removes_exactly_one_address(
    bot, user
):
    """Cancel is the one exit that means "I do not want this address".

    It must delete the row the flow owns — and after a correction there is still
    only ONE row to delete. If the correction had duplicated, Cancel would clean
    up the second and leave the first behind at the wrong coordinates: an
    address the customer explicitly rejected, still in their picker.
    """
    await open_address_flow(bot, user)
    await drop_pin_and_name_it(bot, user, title="addr_title_home")

    await bot.send(user.location(CORRECTED_LAT, CORRECTED_LNG))
    await deliberate_retap(bot, user.tap("addr_title_home"))
    assert len(bot.backend.addresses) == 1

    await bot.send(user.tap("cancel_address_creation"))

    assert bot.backend.addresses == {}, "cancel must leave nothing behind"
    deletes = [c for c in address_writes(bot) if c.method == "DELETE"]
    assert len(deletes) == 1, f"expected exactly one delete, got {address_writes(bot)}"
    assert bot.conversation_state("address_conversation") is None


# ===========================================================================
# INCIDENT 3 — a conversation that expired in silence, and the origin it left
# ===========================================================================


async def test_incident_20260821_the_address_conversation_answers_its_own_timeout(bot):
    """`conversation_timeout` is not self-announcing.

    PTB looks for handlers under the `TIMEOUT` state key when the timer fires
    and, finding none, ends the conversation without running anything at all.
    `address_conversation` carried a 600-second timeout and no such handler, so
    a customer who stepped away was left on a prompt whose buttons had silently
    stopped working.

    Both handler shapes are required: the synthetic timeout update carries
    whatever the LAST REAL update was, which for this flow is a tap on a Skip
    button as often as it is a typed line.
    """
    conversation = bot.conversation("address_conversation")
    assert conversation.conversation_timeout, "the timeout itself is still configured"

    timeout_handlers = conversation.states.get(ConversationHandler.TIMEOUT)
    assert timeout_handlers, (
        "address_conversation times out with no TIMEOUT handler — it will end "
        "in silence and strand its keys in user_data (incident 3)"
    )

    factory = bot.updates()
    for update, shape in (
        (factory.text("45"), "a typed answer"),
        (factory.tap("skip_floor"), "a tapped Skip"),
    ):
        assert any(
            handler.check_update(update) not in (None, False) for handler in timeout_handlers
        ), f"the timeout would fire into nothing when the last update was {shape}"


async def test_incident_20260821_a_timed_out_flow_keeps_the_address_and_drops_the_flow_keys(
    bot, user
):
    """A timeout is not a cancel.

    The customer dropped a pin and named it; that address is theirs to keep. But
    the in-flight flow state must go, because `address_flow_origin` and
    `temp_address_data` outlive the conversation in `user_data` and there is
    nothing left to consume them.

    Depends on `ProfileHandlers.address_flow_timeout`.
    """
    await open_address_flow(bot, user, from_checkout=True)
    await drop_pin_and_name_it(bot, user)
    assert len(bot.backend.addresses) == 1
    assert user_data(bot)["address_flow_origin"] == "checkout"

    last = user.tap("skip_apartment")
    await bot.send(last)
    bot.telegram.reset()

    await expire_the_conversation(bot, last)

    assert len(bot.backend.addresses) == 1, (
        "timing out must not delete the address the customer already earned"
    )
    assert "address_flow_origin" not in user_data(bot), (
        "a stale checkout origin hijacks the NEXT, unrelated address save"
    )
    assert "temp_address_data" not in user_data(bot)
    assert bot.conversation_state("address_conversation") is None

    shown = bot.telegram.shown
    assert shown, "the customer was told nothing at all — the silent expiry"
    assert shown[-1].text == TRANSLATIONS["telegram.address.flow_timed_out_saved"], (
        "the copy has to say the address survived; 'cancelled' would send them "
        f"round the flow again, got {shown[-1].text!r}"
    )


async def test_incident_20260821_a_checkout_address_flow_really_does_resume_checkout(bot, user):
    """The control that makes the next test mean something.

    `address_flow_origin == 'checkout'` is a live mechanism, not a dead key: a
    customer who added their first address from the checkout screen is carried
    straight back into checkout instead of being dumped on the main menu with a
    full cart. That is what makes a STALE copy of the key dangerous.
    """
    await open_address_flow(bot, user, from_checkout=True)
    await drop_pin_and_name_it(bot, user)
    await bot.send(user.tap("skip_apartment"))
    await bot.send(user.tap("skip_floor"))

    bot.telegram.reset()
    calls_before = len(bot.backend.calls)
    await bot.send(user.tap("skip_delivery_instructions"))

    resumed = bot.backend.calls[calls_before:]
    assert any(
        c.method == "GET" and c.endpoint == "/api/v1/auth/addresses" for c in resumed
    ), f"checkout was never resumed; the bot only did {resumed}"

    (address_id,) = bot.backend.addresses
    card = bot.telegram.last_shown().callback_data()
    assert f"address_{address_id}" in card, (
        "the customer is not looking at checkout's 'Delivering to…' card — the "
        f"last screen offered {card}"
    )
    assert "back_to_cart" in card, (
        "the card's Back must return to the cart they were checking out, which "
        "is what proves this is the checkout screen and not the address list"
    )
    assert TRANSLATIONS["telegram.address.saved_successfully"] not in bot.telegram.texts(), (
        "a checkout-origin save goes back to checkout, it does not stop on a "
        "confirmation screen"
    )


async def test_incident_20260821_a_timed_out_checkout_origin_cannot_hijack_the_next_save(
    bot, user
):
    """The bite of incident 3, end to end.

    Add an address from CHECKOUT, walk away, let it expire. Come back an hour
    later and add a completely unrelated address from the profile screen. With
    the stale `address_flow_origin == 'checkout'` still sitting in `user_data`,
    that second, innocent save used to fling the customer into a checkout they
    never asked for — with whatever was in their cart.

    UPDATED 2026-08-26 (pin-routing ruling, Task 6): the fix for this incident
    is no longer merely "the second flow happens not to see the stale key" —
    `ProfileHandlers._clear_address_flow_keys` (added to close a SEPARATE
    hole: `awaiting_location` outliving a cancelled/timed-out flow) now pops
    `address_flow_origin` on every teardown, timeout included. A stale origin
    can no longer survive a timed-out checkout flow AT ALL, so this test now
    asserts that directly — the strongest available statement, independent of
    how the second flow is entered.

    The second flow re-enters by TAPPING "Add address", not by sharing a bare
    pin. That is not merely a simplification: as of the 2026-08-25 pin ruling,
    a pin the bot never asked for is filed as a support message rather than
    opening the address conversation (`_route_address_location_entry` in
    bot.py — see tests/telegram_bot/test_support_attachment_dispatch.py), so a
    bare unprompted pin is no longer a valid way to open this second flow at
    all. Do not "restore" the old vehicle; it would silently re-break that
    rule while looking like a faithful revert of this test.
    """
    await open_address_flow(bot, user, from_checkout=True)
    await drop_pin_and_name_it(bot, user)
    last = user.tap("skip_apartment")
    await bot.send(last)
    await expire_the_conversation(bot, last)

    assert "address_flow_origin" not in user_data(bot), (
        "a timed-out checkout flow must not leave a stale address_flow_origin "
        "behind — this incident is now structurally impossible, not merely "
        "defended against"
    )

    bot.telegram.reset()
    cart_reads_before = len([c for c in bot.backend.calls if c.endpoint == "/api/v1/cart"])

    # An hour later, a different, unrelated address from the profile screen —
    # armed the ordinary way (tap "Add address"), not a bare unprompted pin.
    await open_address_flow(bot, user)
    await drop_pin_and_name_it(bot, user, lat=CORRECTED_LAT, lng=CORRECTED_LNG)
    await deliberate_retap(bot, user.tap("skip_apartment"))
    await deliberate_retap(bot, user.tap("skip_floor"))
    await deliberate_retap(bot, user.tap("skip_delivery_instructions"))

    cart_reads_after = len([c for c in bot.backend.calls if c.endpoint == "/api/v1/cart"])
    assert cart_reads_after == cart_reads_before, (
        "the second, unrelated address save bounced the customer into checkout"
    )
    assert TRANSLATIONS["telegram.address.saved_successfully"] in bot.telegram.texts(), (
        "the customer should simply be told their address was saved"
    )
    assert len(bot.backend.addresses) == 2, "two pins, two addresses"


# ===========================================================================
# INCIDENT 4 — a street name with Markdown metacharacters killed the flow
# ===========================================================================


async def test_incident_20260821_a_markdown_hostile_street_name_no_longer_kills_the_flow(
    bot, user
):
    """`_`, `*`, `[` and a backtick are DATA in a street name and MARKUP to
    Telegram.

    Unescaped, the Bot API refused the whole title prompt, the handler's bare
    `except Exception` returned `ConversationHandler.END`, and the customer saw
    "Joylashuv qabul qilindi." and then nothing — with no way to self-rescue,
    because re-sharing the pin re-runs the same deterministic failure.

    Depends on `escape_markdown(reverse_geocoded_address)` in
    `ProfileHandlers.location_received`.
    """
    geocodes_to(bot, HOSTILE_GEOCODED)
    await open_address_flow(bot, user)
    bot.telegram.reset()

    await bot.send(user.location(PIN_LAT, PIN_LNG))

    markdown = [c for c in bot.telegram.shown if c.params.get("parse_mode") == "Markdown"]
    assert markdown, "the title prompt still goes out as Markdown"
    prompt = markdown[-1]

    assert HOSTILE_GEOCODED not in prompt.text, (
        "the raw geocoder string reached Telegram — this is the exact payload "
        "that made it refuse the message"
    )
    for metacharacter in ("_", "*", "[", "`"):
        assert f"\\{metacharacter}" in prompt.text, (
            f"{metacharacter!r} was not escaped; got {prompt.text!r}"
        )

    assert bot.conversation_state("address_conversation") == ADDRESS_TITLE, (
        "the customer must reach the step that names the address"
    )
    assert "addr_title_home" in prompt.callback_data(), (
        "and the buttons that get them there must be attached"
    )


async def test_incident_20260821_escaping_the_street_name_does_not_corrupt_what_is_stored(
    bot, user
):
    """The escape is presentation, and must never leak into the data.

    `full_address` is what a driver reads off their card and what the dispatch
    map labels a stop with. If the backslashes the Markdown escape adds were
    written to the server, incident 4 would be traded for a permanent one:
    every address on a street with an underscore stored with junk in it.
    """
    geocodes_to(bot, HOSTILE_GEOCODED)
    await open_address_flow(bot, user)
    await drop_pin_and_name_it(bot, user)

    (address,) = bot.backend.addresses.values()
    assert address["full_address"] == HOSTILE_GEOCODED, (
        "the stored street must be exactly what the geocoder returned, "
        f"got {address['full_address']!r}"
    )
    (created,) = creates(bot)
    assert created.data["full_address"] == HOSTILE_GEOCODED


async def test_incident_20260821_a_prompt_telegram_still_refuses_is_resent_as_plain_text(
    bot, user
):
    """Backstop for the next unescapable thing Telegram objects to.

    Escaping fixes the hazard that was diagnosed. It cannot fix the one that has
    not been, and the cost of being wrong is total — a refused prompt used to
    end the conversation and leave the customer on a dead screen. Losing the
    bold is a much smaller price than losing the flow.

    Depends on `ProfileHandlers._reply_markdown_or_plain`.
    """
    await open_address_flow(bot, user)

    refused = []

    def _refuse_markdown(params):
        if params.get("parse_mode") != "Markdown":
            return 200, {"ok": True, "result": bot.telegram._result_for("sendMessage", params)}
        refused.append(params)
        return 400, {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: can't parse entities: Can't find end of the "
            "entity starting at byte offset 31",
        }

    bot.telegram.failures["sendMessage"] = _refuse_markdown
    bot.telegram.reset()

    await bot.send(user.location(PIN_LAT, PIN_LNG))

    assert refused, "the Markdown attempt is expected to be refused"
    delivered = [
        call
        for call in bot.telegram.shown
        if not any(call.params is params for params in refused)
    ]
    assert any(GEOCODED in call.text for call in delivered), (
        "the customer must still be shown the title prompt as plain text; they "
        f"saw {[c.text for c in delivered]}"
    )
    assert delivered[-1].callback_data(), "the plain-text resend must keep the buttons"
    assert bot.conversation_state("address_conversation") == ADDRESS_TITLE, (
        "a formatting problem must not end the address flow"
    )


# ===========================================================================
# INCIDENT 5 — the dispatcher middlewares lived in initialize(), not _setup_handlers()
# ===========================================================================


def test_incident_20260821_production_registers_both_middlewares_in_setup_handlers():
    """The wiring lives in ONE place, and this reads it from production.

    `WaterBusinessBot` used to split registration across two methods:
    `_setup_handlers()` (everything a harness builds) and `initialize()` (which
    additionally installed the debug logger and the callback-dedup guard).
    Anything built from `_setup_handlers()` alone — this repo's PTB harness
    included — therefore drove a bot missing a production guard, and every
    dispatcher test in the repo was silently exercising a different bot from the
    one customers use.
    """
    tree = _bot_ast("telegram_bot")

    registered = _type_handler_groups(_function_named(tree, "_setup_handlers"))
    assert registered.get("log_all_updates") == -10, (
        "the debug logger is no longer registered at group -10 inside "
        f"_setup_handlers(); that function now installs {registered}"
    )
    assert registered.get("callback_dedup_middleware") == -5, (
        "the callback-dedup guard is no longer registered at group -5 inside "
        "_setup_handlers(); every harness-built bot has just stopped carrying "
        f"it, and that function now installs {registered}"
    )

    in_initialize = _type_handler_groups(_function_named(tree, "initialize"))
    assert "callback_dedup_middleware" not in in_initialize, (
        "the dedup guard has been split back out into initialize() — that is "
        "the incident, not the fix"
    )


async def test_incident_20260821_a_harness_built_bot_carries_both_middlewares(bot, user):
    """And this reads it off the assembled Application, not off the source.

    Both statements are needed: the source check catches the registration moving
    back, this one catches it being registered somewhere that does not actually
    reach a built application. The groups are asserted because
    `ApplicationHandlerStop` only protects the handlers that sort AFTER the
    guard — register the guard at group 0 and every duplicate is already through
    the conversation handlers at -2.
    """
    installed = {}
    for group, handlers in bot.application.handlers.items():
        for handler in handlers:
            if isinstance(handler, TypeHandler):
                installed[getattr(handler.callback, "__name__", repr(handler.callback))] = group

    assert installed.get("log_all_updates") == -10, (
        f"the debug logger is not at group -10; found {installed}"
    )
    assert installed.get("callback_dedup_middleware") == -5, (
        f"the callback-dedup guard is not at group -5; found {installed}"
    )

    conversation_groups = {
        group
        for group, handlers in bot.application.handlers.items()
        for handler in handlers
        if isinstance(handler, ConversationHandler)
    }
    assert conversation_groups and min(conversation_groups) > -5, (
        "a conversation handler now sorts ahead of the dedup guard, so "
        f"duplicates reach it first: {sorted(conversation_groups)}"
    )

    # And the middlewares really are offered every update, catch-alls included.
    matched = bot.handlers_matching(user.tap("add_new_address"), include_catch_alls=True)
    assert [group for group, _handler in matched][:2] == [-10, -5], (
        f"the middlewares are not the first two handlers to see a tap: {matched}"
    )


async def test_incident_20260821_the_guard_the_harness_carries_debounces_a_double_tap(bot, user):
    """Proof the guard is not merely present but load-bearing.

    "Add address" is a conversation ENTRY POINT with `allow_reentry=True`, so an
    impatient second tap does not repeat a screen — it RE-ENTERS, resetting
    `temp_address_data` and stacking a second location keyboard on the customer.
    The guard at group -5 is the only thing between them and that, and until
    2026-08-21 no harness-built bot had it.
    """
    await bot.send(user.tap("add_new_address"))
    duplicate = user.tap("add_new_address")
    await bot.send(duplicate)

    assert bot.conversation_state("address_conversation") == ADDRESS_LOCATION
    assert len(bot.telegram.of("sendMessage")) == 1, (
        "the customer was asked for their location twice"
    )
    acked = {c.params["callback_query_id"] for c in bot.telegram.of("answerCallbackQuery")}
    assert duplicate.callback_query.id in acked, (
        "the dropped duplicate was never answered, so its spinner never stops "
        "— which reads as a hung bot and provokes a third tap"
    )


# ===========================================================================
# INCIDENT 6 — __aexit__ closed the singleton HTTP client under other handlers
# ===========================================================================


class _SpyHttpClient:
    """Stands in for the singleton's `httpx.AsyncClient` and counts closes."""

    def __init__(self):
        self.closed = 0

    async def aclose(self):
        self.closed += 1


async def test_incident_20260813_a_whole_journey_never_closes_the_shared_http_client(
    bot, user, monkeypatch
):
    """`async with api_client` is a SCOPE MARKER, not ownership.

    `BusinessAPIClient` is a module-level singleton. Every handler that talks to
    the backend enters its context manager, and `__aexit__` used to close the
    client. That was survivable only while PTB processed updates strictly one at
    a time; the moment updates began processing concurrently (incident 9), one
    handler's exit closed the client another was still reading from —
    `RuntimeError: client has been closed`, mid-flow, for an unrelated customer.

    Driven as a whole address journey precisely because the singleton's context
    manager is entered once per handler: this counts closes across nine of them.
    """
    import api_client as api_client_module

    spy = _SpyHttpClient()
    monkeypatch.setattr(api_client_module.api_client, "_client", spy)

    await open_address_flow(bot, user)
    await bot.send(user.location(PIN_LAT, PIN_LNG))
    await bot.send(user.tap("addr_title_home"))
    await bot.send(user.tap("skip_apartment"))
    await bot.send(user.tap("skip_floor"))
    await bot.send(user.tap("skip_delivery_instructions"))

    (address_id,) = bot.backend.addresses
    scopes = {(call.method, call.endpoint) for call in bot.backend.calls}
    for scope in (
        ("POST", "/api/v1/addresses/reverse-geocode"),   # location_received
        ("POST", "/api/v1/auth/addresses"),              # _create_address_now
        ("PUT", f"/api/v1/auth/addresses/{address_id}"),  # save_address_final
    ):
        assert scope in scopes, (
            f"{scope} never happened, so that handler never entered the "
            f"singleton's context manager; the journey only did {sorted(scopes)}"
        )

    assert spy.closed == 0, (
        f"the shared HTTP client was closed {spy.closed} time(s) mid-journey"
    )
    assert api_client_module.api_client._client is spy, (
        "the singleton's client was torn down and replaced during the journey"
    )
    assert len(bot.backend.addresses) == 1, (
        "and the journey itself still has to have worked"
    )


# ===========================================================================
# INCIDENT 9 — one slow handler blocked every user (both bots)
# ===========================================================================


@pytest.mark.parametrize("bot_dir", ["telegram_bot", "staff_bot"])
def test_incident_20260813_both_bots_process_updates_serially_per_chat(bot_dir):
    """PTB's default is to process EVERY update strictly one at a time.

    One driver's slow backend call therefore delayed every other driver's taps,
    and one customer's geocode delayed everyone's. The naive fix —
    `concurrent_updates(True)` — is unsafe here, because PTB itself warns that
    stateful handlers (this codebase is nothing but conversation handlers) can
    interleave and corrupt each other's state.

    `PerChatSerialUpdateProcessor` is the compromise both bots run: concurrent
    ACROSS chats, strictly ordered WITHIN one. This pins that neither bot has
    quietly gone back to the default, and that neither has "fixed" it by
    switching to the unsafe flag.
    """
    tree = _bot_ast(bot_dir)

    configured = _calls_to(tree, "concurrent_updates")
    assert len(configured) == 1, (
        f"{bot_dir}/bot.py configures concurrent_updates {len(configured)} "
        "times; PTB's default is serial, so zero means one slow handler blocks "
        "every user again"
    )
    (arguments,) = configured
    assert len(arguments) == 1, (
        f"concurrent_updates() is called with no argument in {bot_dir}/bot.py"
    )
    (argument,) = arguments

    assert not (isinstance(argument, ast.Constant) and argument.value is True), (
        f"{bot_dir}/bot.py passes a bare concurrent_updates(True): PTB warns "
        "that stateful handlers interleave under it, and this bot is nothing "
        "but conversation handlers"
    )
    assert (
        isinstance(argument, ast.Call)
        and isinstance(argument.func, ast.Name)
        and argument.func.id == "PerChatSerialUpdateProcessor"
    ), (
        f"{bot_dir}/bot.py no longer passes PerChatSerialUpdateProcessor to "
        f"concurrent_updates(); it now passes {ast.dump(argument)[:120]}"
    )

    imported_from = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and any(alias.name == "PerChatSerialUpdateProcessor" for alias in node.names)
    }
    assert imported_from == {"shared.telegram_update_processor"}, (
        f"{bot_dir}/bot.py has its own copy of the update processor instead of "
        f"the shared one: {imported_from}"
    )


# ---------------------------------------------------------------------------
# The staff bot
# ---------------------------------------------------------------------------

# Sentinel copy: distinctive enough that "did the driver see the invalid-amount
# error?" is answerable by exact string, and shaped like the real seeds
# (emoji-free values; the reply keyboard prepends its own emoji).
STAFF_TRANSLATIONS = {
    "staff.menu.new_orders": "New Orders",
    "staff.menu.active_deliveries": "Active Deliveries",
    "staff.menu.tryouts": "Try-outs",
    "staff.menu.cash": "Cash",
    "staff.menu.profile": "Profile",
    "staff.menu.settings": "Settings",
    "staff.menu.help": "Help",
    "staff.menu.title": "Main menu",
    "staff.session_expired": "Session expired",
    "staff.delivery.invalid_amount": "INVALID-CASH-AMOUNT",
    "staff.delivery.enter_partial_cash_reason": "ASK-FOR-REASON",
}

STAFF_LOGIN_ENDPOINT = "/api/v1/staff/auth/login"


def _driver_row():
    return {
        "id": 55,
        "telegram_id": str(DEFAULT_DRIVER_TELEGRAM_ID),
        "first_name": "Aziz",
        "last_name": "Karimov",
        "phone": "+998901112233",
        "preferred_language": "en",
        "role": "delivery",
        "status": "active",
        "staff_roles": '["delivery_driver"]',
        "staff_bot_state": "{}",
    }


def _driver_login(_call):
    return {
        "access_token": "staff-access-token",
        "refresh_token": "staff-refresh-token",
        "expires_in": 3600,
        "user": {
            "id": 55,
            "first_name": "Aziz",
            "last_name": "Karimov",
            "phone": "+998901112233",
            "preferred_language": "en",
            "staff_roles": ["delivery_driver"],
            "delivery_person_id": 7,
        },
    }


@pytest.fixture
async def staff(monkeypatch):
    harness = await build_staff_harness(
        monkeypatch,
        translations=STAFF_TRANSLATIONS,
        database=FakeStaffDatabase(staff_user=_driver_row()),
    )
    harness.backend.route("POST", STAFF_LOGIN_ENDPOINT, _driver_login)
    return harness


async def sign_the_driver_in(staff):
    """Run the real `/start` login and hand back the menu the bot drew.

    The labels are read OFF the rendered keyboard rather than rebuilt here, so
    the tap fed back in is byte-for-byte the string the driver's phone would
    send. That is the loop the staff bot's reply-keyboard menu actually depends
    on.
    """
    driver = staff.updates()
    await staff.send(driver.command("start"))

    shown = staff.telegram.shown
    assert shown, "/start produced no message at all — the driver sees a dead bot"
    labels = shown[-1].button_labels()
    assert labels, "login did not attach the reply-keyboard main menu"
    staff.telegram.reset()
    return driver, labels


def menu_label(labels, value: str) -> str:
    hits = [label for label in labels if label.strip().endswith(value)]
    assert len(hits) == 1, f"expected exactly one menu button carrying {value!r}, got {labels}"
    return hits[0]


def staff_user_data(staff):
    return staff.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]


# ===========================================================================
# INCIDENT 7 — the staff bot could not be instrumented without a subclass
# ===========================================================================


async def test_incident_20260813_process_update_cannot_be_patched_onto_an_application(staff):
    """Drivers reported the bot hanging and it could not be measured.

    The bot logged `Update: ...` on arrival and then nothing — no elapsed time,
    no completion marker — so "slow" could not be told from "idle". The obvious
    instrumentation, assigning over `application.process_update`, is IMPOSSIBLE:
    PTB's `Application` defines `__slots__`, so the assignment raises
    `AttributeError: 'Application' object attribute 'process_update' is
    read-only`.

    This proves the constraint against the live class rather than trusting the
    comment, so if a future PTB drops `__slots__` and someone "simplifies" the
    subclass away, the reason it existed is still on record.
    """
    from telegram.ext import Application

    assert "__slots__" in vars(Application), (
        "telegram.ext.Application no longer defines __slots__ — re-read "
        "staff_bot/bot.py::TimedApplication before changing anything"
    )

    async def _noop(_update):
        return None

    with pytest.raises(AttributeError):
        staff.application.process_update = _noop


def test_incident_20260813_the_staff_bot_measures_every_update_via_an_application_subclass():
    """The only supported hook for the above is `ApplicationBuilder.application_class`.

    It also has to be an override of `process_update` rather than another
    handler: a `TypeHandler` at group -10 returns before the real handlers run,
    and a last-group handler is skipped entirely when something raises
    `ApplicationHandlerStop` or an earlier group errors. Only the override sees
    the whole update, and only its `finally:` measures an update that failed.
    """
    source = _source("staff_bot", "bot.py")

    installed = [
        arguments[0].id
        for arguments in _calls_to(_bot_ast("staff_bot"), "application_class")
        if arguments and isinstance(arguments[0], ast.Name)
    ]
    assert installed == ["TimedApplication"], (
        "the staff bot no longer builds a TimedApplication — every update's "
        f"duration goes unmeasured again; application_class() got {installed}"
    )

    timed = source.split("class TimedApplication", 1)
    assert len(timed) == 2, "TimedApplication is gone"
    body = timed[1].split("\nclass ", 1)[0]
    assert "async def process_update" in body, (
        "TimedApplication no longer overrides process_update"
    )
    assert "finally:" in body, (
        "the timing must be reported from a finally: block, or an update that "
        "raised is silently unmeasured — which is the state the incident was "
        "diagnosed from"
    )
    assert "slow_update elapsed=" in body, (
        "the greppable Loki marker is gone; slow updates become invisible again"
    )


# ===========================================================================
# INCIDENT 8 — "Invalid cash amount": a menu tap parsed as flow input
# ===========================================================================


async def test_incident_the_reported_invalid_cash_amount_bug_is_a_menu_tap(staff):
    """The reported bug, verbatim.

    The staff main menu is a REPLY keyboard, so a tap arrives as ordinary text
    and nothing about it says "button". `StaffBot._handle_text_message` used to
    hand that text to whatever `pending_*_flow` was armed BEFORE it checked
    whether the text was a menu label — so a driver standing at a door, mid
    cash-collection, who tapped "Cash" to look something up was told
    "Invalid cash amount" by a button the bot itself had drawn.

    Depends on the ORDER of two blocks in `StaffBot._handle_text_message`: the
    `menu_action = self._match_menu_action(...)` branch must sit ABOVE the
    `cash_flow = context.user_data.get('pending_delivery_cash_flow')` branch.
    Swap them back and this goes red.

    The flow is armed directly on `user_data` because driving a delivery all the
    way to the cash prompt is covered elsewhere; what is under test here is the
    routing decision, and the key's shape is production's own.
    """
    driver, labels = await sign_the_driver_in(staff)
    cash_button = menu_label(labels, STAFF_TRANSLATIONS["staff.menu.cash"])

    staff_user_data(staff)["pending_delivery_cash_flow"] = {
        "flow_type": "partial",
        "delivery_id": 9,
        "cash_amount": None,
    }

    await staff.send(driver.text(cash_button))

    assert STAFF_TRANSLATIONS["staff.delivery.invalid_amount"] not in staff.telegram.texts(), (
        "the driver tapped a button and was told it was not a number — this is "
        "the reported bug"
    )
    assert "pending_delivery_cash_flow" not in staff_user_data(staff), (
        "navigating away must disarm the flow, or the NEXT thing the driver "
        "types is silently read as a cash amount"
    )
    assert staff.telegram.shown, "the tap produced nothing at all"


async def test_incident_a_menu_tap_at_the_cash_note_step_never_files_the_transaction(staff):
    """The same defect one step later, where it costs money rather than face.

    At the note step the armed flow is waiting for free text, and free text is
    exactly what a menu label is. Consumed as a note, it does not merely confuse
    the driver — it FINALISES the delivery with the word "Cash" as its audit
    reason and a cash amount nobody confirmed.
    """
    driver, labels = await sign_the_driver_in(staff)
    profile_button = menu_label(labels, STAFF_TRANSLATIONS["staff.menu.profile"])

    staff_user_data(staff)["pending_delivery_cash_flow"] = {
        "flow_type": "partial",
        "delivery_id": 9,
        "cash_amount": 25000.0,
    }
    calls_before = len(staff.backend.calls)

    await staff.send(driver.text(profile_button))

    wrote = [
        call
        for call in staff.backend.calls[calls_before:]
        if call.method in {"POST", "PUT", "PATCH"}
    ]
    assert wrote == [], f"the menu tap was filed as a real transaction: {wrote}"
    assert "pending_delivery_cash_flow" not in staff_user_data(staff)


async def test_incident_a_real_cash_amount_at_the_same_prompt_still_reaches_the_flow(staff):
    """The control, without which the two tests above pass for a bot that has
    simply stopped routing text at all.

    A number typed at the cash prompt is NOT a menu label and must still reach
    the cash handler, be parsed, and move the flow on to the mandatory audit
    note. Break the amount branch and the driver can no longer close a delivery.
    """
    driver, _labels = await sign_the_driver_in(staff)

    staff_user_data(staff)["pending_delivery_cash_flow"] = {
        "flow_type": "partial",
        "delivery_id": 9,
        "cash_amount": None,
    }

    await staff.send(driver.text("45000"))

    flow = staff_user_data(staff).get("pending_delivery_cash_flow")
    assert flow, "the flow was cleared by a value that is not a menu label"
    assert flow["cash_amount"] == 45000, f"the amount never reached the handler: {flow}"
    assert STAFF_TRANSLATIONS["staff.delivery.enter_partial_cash_reason"] in staff.telegram.texts(), (
        "the driver must now be asked for the mandatory audit note"
    )


async def test_incident_text_that_is_not_money_at_the_cash_prompt_is_refused_not_navigated(staff):
    """The other half of the control.

    "abc" is neither a menu label nor a number. It must be refused BY THE FLOW —
    the driver stays where they are and retypes — rather than falling through to
    the menu, which would silently abandon a delivery they are standing at.
    """
    driver, _labels = await sign_the_driver_in(staff)

    staff_user_data(staff)["pending_delivery_cash_flow"] = {
        "flow_type": "partial",
        "delivery_id": 9,
        "cash_amount": None,
    }

    await staff.send(driver.text("abc"))

    assert STAFF_TRANSLATIONS["staff.delivery.invalid_amount"] in staff.telegram.texts(), (
        "the driver was not told their input was rejected"
    )
    flow = staff_user_data(staff).get("pending_delivery_cash_flow")
    assert flow is not None and flow["cash_amount"] is None, (
        "a bad amount must leave the driver on the same prompt, still armed"
    )
