"""The reported production journey, driven through the REAL PTB dispatcher.

`test_address_pin_flow_persistence.py` proves the handlers do the right thing
when they are called. This module proves they are called at all: every update
here goes in through `Application.process_update`, so the conversation state
machine, the handler groups, the callback-dedup middleware and the real
keyboard `callback_data` are all in the loop.

That distinction is the whole point. The suite had 30 bot test files and 8793
green backend tests while a customer could drop a pin, tap Skip twice and end
up with nothing — because nothing tested the WIRING.
"""

import pytest

from handlers.profile import (
    ADDRESS_APARTMENT,
    ADDRESS_DELIVERY_INSTRUCTIONS,
    ADDRESS_FLOOR,
    ADDRESS_LOCATION,
    ADDRESS_TITLE,
)

from tests.telegram_bot.ptb_harness import build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


# The pin the customer in the traced session actually dropped.
PIN_LAT = 41.32354
PIN_LNG = 69.241036


@pytest.fixture
async def bot(monkeypatch):
    return await build_bot_harness(monkeypatch)


@pytest.fixture
def user(bot):
    return bot.updates()


async def open_address_flow(bot, user):
    await bot.send(user.tap("add_new_address"))
    return bot.conversation_state("address_conversation")


# ---------------------------------------------------------------------------
# The journey that lost 20 of 33 addresses in production
# ---------------------------------------------------------------------------


async def test_pin_skip_skip_then_walk_away_leaves_a_usable_address(bot, user):
    """Telegram user 1009661971, 2026-08-19, step for step — including the part
    where they stopped."""
    assert await open_address_flow(bot, user) == ADDRESS_LOCATION

    await bot.send(user.location(PIN_LAT, PIN_LNG))
    assert bot.conversation_state("address_conversation") == ADDRESS_TITLE

    await bot.send(user.tap("addr_title_work"))
    assert bot.conversation_state("address_conversation") == ADDRESS_APARTMENT

    await bot.send(user.tap("skip_apartment"))
    assert bot.conversation_state("address_conversation") == ADDRESS_FLOOR

    await bot.send(user.tap("skip_floor"))
    assert bot.conversation_state("address_conversation") == ADDRESS_DELIVERY_INSTRUCTIONS

    # The customer puts the phone down. This is where the production trace ends.
    saved = list(bot.backend.addresses.values())
    assert len(saved) == 1, "the pin the customer dropped must not evaporate"
    assert saved[0]["latitude"] == PIN_LAT
    assert saved[0]["longitude"] == PIN_LNG
    assert saved[0]["title"], "an address with no name is unusable in the picker"


async def test_every_button_the_pin_flow_renders_is_answered_by_a_handler(bot, user):
    """A tap with no matching handler shows a spinner and then nothing — the
    single most common way a Telegram flow dies silently. Walk the flow and
    check, at each step, that EVERY button on the message the customer is
    looking at is claimed by some registered handler."""
    await open_address_flow(bot, user)

    journey = [
        (user.location(PIN_LAT, PIN_LNG), "after sharing the pin"),
        (user.tap("addr_title_home"), "after naming the address"),
        (user.tap("skip_apartment"), "after skipping the apartment"),
        (user.tap("skip_floor"), "after skipping the floor"),
    ]

    for update, where in journey:
        bot.telegram.reset()
        await bot.send(update)

        rendered = bot.telegram.shown
        assert rendered, f"the bot showed nothing {where}"

        for data in rendered[-1].callback_data():
            probe = user.tap(data)
            assert bot.handlers_matching(probe), (
                f"the '{data}' button rendered {where} lands nowhere: no "
                f"registered handler claims it in this conversation state"
            )


async def test_the_customer_sees_a_reply_to_every_tap_in_the_pin_flow(bot, user):
    """Every step must produce something visible. A step that only edits state
    reads to the customer as a frozen bot."""
    await open_address_flow(bot, user)

    for update in (
        user.location(PIN_LAT, PIN_LNG),
        user.tap("addr_title_home"),
        user.tap("skip_apartment"),
        user.tap("skip_floor"),
        user.tap("skip_delivery_instructions"),
    ):
        bot.telegram.reset()
        await bot.send(update)
        assert bot.telegram.shown, f"no reply to {update}"


async def test_finishing_the_chain_ends_the_conversation_with_one_address(bot, user):
    """The completed happy path still produces exactly one row — creating early
    must not mean creating twice."""
    await open_address_flow(bot, user)
    await bot.send(user.location(PIN_LAT, PIN_LNG))
    await bot.send(user.tap("addr_title_home"))
    await bot.send(user.tap("skip_apartment"))
    await bot.send(user.tap("skip_floor"))
    await bot.send(user.tap("skip_delivery_instructions"))

    assert bot.conversation_state("address_conversation") is None, "flow should be over"
    assert len(bot.backend.addresses) == 1

    creates = [
        call
        for call in bot.backend.calls
        if call.method == "POST" and call.endpoint == "/api/v1/auth/addresses"
    ]
    assert len(creates) == 1, f"duplicate address creates: {creates}"


async def test_typed_details_reach_the_backend_before_the_next_question(bot, user):
    """An answer already given must survive abandoning the NEXT step, so it has
    to be written when it is given."""
    await open_address_flow(bot, user)
    await bot.send(user.location(PIN_LAT, PIN_LNG))
    await bot.send(user.tap("addr_title_home"))

    await bot.send(user.text("45"))
    assert bot.conversation_state("address_conversation") == ADDRESS_FLOOR

    (address,) = bot.backend.addresses.values()
    assert address["apartment_number"] == "45", (
        "the apartment number is only safe once it is on the server"
    )


async def test_out_of_zone_pin_keeps_the_customer_in_the_location_step(bot, user):
    """A pin outside TASHKENT_POLYGON must re-ask, not advance and not create."""
    await open_address_flow(bot, user)

    await bot.send(user.location(55.7558, 37.6173))  # Moscow

    assert bot.conversation_state("address_conversation") == ADDRESS_LOCATION
    assert bot.backend.addresses == {}, "an out-of-zone pin must never be saved"


async def test_cancelling_removes_the_address_the_flow_created(bot, user):
    """Cancel is the one exit that means the customer does not want it."""
    await open_address_flow(bot, user)
    await bot.send(user.location(PIN_LAT, PIN_LNG))
    await bot.send(user.tap("addr_title_home"))
    assert len(bot.backend.addresses) == 1

    await bot.send(user.tap("cancel_address_creation"))

    assert bot.backend.addresses == {}
    assert bot.conversation_state("address_conversation") is None


# ---------------------------------------------------------------------------
# Telegram itself misbehaving — never simulated before this harness
# ---------------------------------------------------------------------------


async def test_a_failed_edit_cannot_take_the_saved_address_with_it(bot, user):
    """Telegram rejects editMessageText with "message is not modified" and
    "message to edit not found" in this project's production logs, and
    `skip_field_handler` swallows both into `ConversationHandler.END` — so the
    customer IS stranded on a dead prompt. That defect is pinned separately in
    tests/telegram_bot/test_telegram_api_failure_modes.py.

    What this asserts is the property that makes it survivable: because the
    address was written back at the title step, a formatting failure three
    steps later costs the customer a prompt, not their address. Before
    2026-08-21 the same rejection lost everything.
    """
    await open_address_flow(bot, user)
    await bot.send(user.location(PIN_LAT, PIN_LNG))
    await bot.send(user.tap("addr_title_home"))
    (saved,) = bot.backend.addresses.values()

    bot.telegram.fail("editMessageText", "Bad Request: message is not modified")
    await bot.send(user.tap("skip_apartment"))

    assert list(bot.backend.addresses.values()) == [saved], (
        "a failed edit must not delete, duplicate or blank the address"
    )
    assert saved["latitude"] == PIN_LAT and saved["title"], (
        "the surviving row must still be deliverable"
    )


async def test_a_pin_arriving_before_the_flow_starts_still_opens_it(bot, user):
    """Zero-address checkout arms the location keyboard before the conversation
    exists, so the pin must be an ENTRY POINT. Without it the pin escapes to
    the group-0 catch-all and is filed as a support ticket."""
    await bot.send(user.location(PIN_LAT, PIN_LNG))

    assert bot.conversation_state("address_conversation") == ADDRESS_TITLE
    support_posts = [
        call for call in bot.backend.calls if call.endpoint == "/api/v1/support/messages"
    ]
    assert support_posts == [], "a shared pin is not a support message"
