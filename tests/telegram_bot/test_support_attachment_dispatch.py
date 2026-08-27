"""Attachments sent with no flow open reach the support inbox — and, just as
importantly, attachments the address flow asked for do NOT.

Driven through the REAL PTB dispatcher, because the thing under test is handler
ORDERING across groups, which a direct handler call cannot see.

PIN RULING (2026-08-25): a pin only belongs to the address flow when the bot
actually ASKED for one (`ProfileKeyboards.location_request(...)`, armed via
`utils.arm_location_request`). A spontaneous, unprompted pin is a support
message. `filters.LOCATION` is an entry point of the address conversation with
`allow_reentry=True`, so naively "is a conversation active?" is the wrong
question — it would misroute the zero-address-checkout pin, which the bot DID
ask for without ever entering the conversation. See
`_route_address_location_entry` in bot.py.
"""
from datetime import datetime, timedelta, timezone

import pytest
from telegram import Update

from handlers.profile import ADDRESS_TITLE

from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID, build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SUPPORT_ENDPOINT = "/api/v1/support/messages"


@pytest.fixture
async def bot(monkeypatch):
    return await build_bot_harness(monkeypatch)


@pytest.fixture
def user(bot):
    return bot.updates()


def _support_calls(bot):
    return [c for c in bot.backend.calls if c.endpoint == SUPPORT_ENDPOINT]


async def test_a_pin_dropped_during_the_address_flow_is_not_captured_as_support(bot, user):
    """THE regression this task risks. The address conversation lives in group
    -2 and must keep winning the location update."""
    await bot.send(user.tap("add_new_address"))

    await bot.send(user.location(41.32354, 69.241036))

    assert _support_calls(bot) == [], (
        "the address flow's pin leaked into the support inbox — the attachment "
        "catch-all is running ahead of the address ConversationHandler"
    )


async def test_a_spontaneous_pin_is_captured_as_support_and_does_not_start_address_creation(
    bot, user
):
    """Nothing armed the location keyboard and no flow is open — this pin was
    never asked for, so it is a support message, not a new address."""
    await bot.send(user.location(41.31, 69.25))

    calls = _support_calls(bot)
    assert len(calls) == 1
    assert calls[0].data["message_type"] == "location"
    assert calls[0].data["latitude"] == pytest.approx(41.31)

    assert bot.conversation_state("address_conversation") is None, (
        "a spontaneous pin must not silently start an address draft"
    )


async def test_a_pin_after_the_bot_armed_the_keyboard_starts_the_address_flow(bot, user):
    """Drives the real zero-address checkout path (`orders.checkout_handler`)
    so the arming is genuine — not hand-set on `context.user_data` — then
    proves the pin it prompted for lands in the address flow, not support.

    A fresh harness starts with zero saved addresses (`FakeBackend.addresses`
    is empty), so tapping "checkout" alone reaches the zero-address branch
    without needing a populated cart.
    """
    await bot.send(user.tap("checkout"))

    prompt = bot.telegram.last_shown()
    assert "Share location button" in prompt.button_labels(), (
        "setup didn't reach the zero-address checkout prompt — the pin below "
        "would not be testing a genuinely armed keyboard"
    )

    await bot.send(user.location(41.32354, 69.241036))

    assert bot.conversation_state("address_conversation") == ADDRESS_TITLE, (
        "a pin the bot asked for at checkout must start the address flow"
    )
    assert _support_calls(bot) == [], (
        "a pin the bot asked for must never be filed as a support message"
    )


async def test_a_cancelled_checkout_prompt_does_not_leave_a_later_pin_misrouted(bot, user):
    """CRITICAL regression (2026-08-26): `arm_location_request` sets
    `awaiting_location`, but the only place that ever CLEARED it was a pin
    arriving. Every path that ends the flow WITHOUT a pin — cancel, the text
    Cancel button, a timeout, or a successful save — left it `True` forever.
    A zero-address customer who tapped Checkout, then backed out with Cancel
    instead of sharing a pin, would have every later, unrelated pin swept
    into address creation instead of the support inbox — for the life of the
    process."""
    await bot.send(user.tap("checkout"))
    prompt = bot.telegram.last_shown()
    assert "Cancel" in prompt.button_labels(), (
        "setup didn't reach the zero-address checkout prompt — the flag "
        "below would not be testing a genuine arming"
    )

    await bot.send(user.text("Cancel"))
    assert bot.conversation_state("address_conversation") is None, (
        "setup: cancelling must actually end the flow before the real test runs"
    )

    bot.telegram.reset()
    await bot.send(user.location(41.31, 69.25))

    calls = _support_calls(bot)
    assert len(calls) == 1 and calls[0].data["message_type"] == "location", (
        "a pin long after cancelling checkout must be a spontaneous support "
        "message, not a stale-armed address draft"
    )
    assert bot.conversation_state("address_conversation") is None, (
        "the stale awaiting_location flag must not resurrect address creation"
    )


async def test_a_stale_armed_checkout_pin_prompt_is_captured_as_support(bot, user):
    """FIX 6: no ConversationHandler ever starts at the zero-address-checkout
    site, so if the customer neither pins NOR cancels there is no timeout to
    clear `awaiting_location` — only the arrival of a pin, or one of the
    address flow's own teardown sites, ever popped it. A customer who saw the
    prompt, walked away, and dropped an unrelated pin hours later must not
    have that pin silently reopen address creation. Mirrors
    `handlers/support.py::_is_stale`'s 30-minute rule."""
    await bot.send(user.tap("checkout"))
    prompt = bot.telegram.last_shown()
    assert "Share location button" in prompt.button_labels(), (
        "setup didn't reach the zero-address checkout prompt — the arming "
        "below would not be testing a genuine stale-flag scenario"
    )

    # Back-date the stamp `arm_location_request` just wrote, simulating a
    # customer who went quiet for well over 30 minutes.
    stale_at = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
    bot.application.user_data[DEFAULT_USER_ID]["awaiting_location_at"] = stale_at

    bot.telegram.reset()
    await bot.send(user.location(41.31, 69.25))

    calls = _support_calls(bot)
    assert len(calls) == 1 and calls[0].data["message_type"] == "location", (
        "a pin arriving long after a stale checkout arming must be a "
        "spontaneous support message, not a resurrected address draft"
    )
    assert bot.conversation_state("address_conversation") is None, (
        "a stale awaiting_location flag must not start address creation"
    )
    assert "awaiting_location" not in bot.application.user_data.get(DEFAULT_USER_ID, {}), (
        "the stale marker must be consumed, not left armed for next time"
    )


async def test_a_pin_while_the_concern_flow_is_armed_gets_the_order_prefix_and_ack(bot, user):
    """IMPORTANT-1 regression: the spontaneous-pin branch used to call
    `capture_support_message` directly, skipping the concern-flow check every
    other attachment goes through. With "Report an issue" armed, a PHOTO
    correctly got the `[Order #N]` prefix, an acknowledgement, and cleared the
    concern state — a PIN did none of that: filed bare and unacked, with the
    concern left armed so the customer's NEXT unrelated message would be
    wrongly acked under that order."""
    await bot.send(user.tap("report_issue_42"))
    bot.telegram.reset()

    await bot.send(user.location(41.31, 69.25))

    calls = _support_calls(bot)
    assert len(calls) == 1
    assert calls[0].data["message_type"] == "location"
    assert calls[0].data["content"] == "[Order #42]", (
        "a pin sent while a concern is armed must carry the same order "
        "prefix a text message or photo would"
    )

    assert bot.telegram.of("sendMessage") != [], (
        "the pin must be acknowledged like any other concern message"
    )
    assert bot.database.user["bot_state"] == "{}", (
        "the concern flow must be disarmed after the pin, or the customer's "
        "next unrelated message gets wrongly acked under this order"
    )


async def test_an_edited_location_message_is_not_filed_as_junk_support(bot, user):
    """IMPORTANT-2 regression: `filters.LOCATION` is a `MessageFilter`, which
    matches `edited_message` too — a live-location share emits one of those
    per tick, carrying no `update.message` at all. Before the null-message
    guard, that fell into `capture_support_message`, which read `update.message`
    as `None` and posted one junk `message_type=unsupported` row per tick."""
    base = user.location(41.31, 69.25).to_dict()
    base["edited_message"] = base.pop("message")
    base["update_id"] += 1
    edited_update = Update.de_json(base, user.bot)

    await bot.send(edited_update)

    assert _support_calls(bot) == [], (
        "an edited (live-location tick) update must never be filed as support"
    )
    assert bot.conversation_state("address_conversation") is None, (
        "an edited-message location must not start an address draft either"
    )


async def test_a_live_location_tick_does_not_kill_an_open_address_flow(bot, user):
    """The null-message guard must skip the update, not END the conversation.

    `filters.LOCATION` is a `MessageFilter`, so it matches `edited_message` —
    and `allow_reentry=True` makes PTB search entry points BEFORE the current
    state's handlers (conversationhandler.py:765-771), so a live-location tick
    reaches this entry point even mid-flow.

    `ApplicationHandlerStop(ConversationHandler.END)` is not "skip this update":
    PTB catches it at conversationhandler.py:853 (`new_state = exception.state`)
    and `_update_state` then does `del self._conversations[key]`. So one tick of
    a live-location share silently deleted the customer's open address form —
    leaving `temp_address_data` stranded, which sends their next typed answer to
    the Support Inbox and lets a later pin resurrect the dead draft.

    A BARE `ApplicationHandlerStop()` carries `state=None`, and `_update_state`
    no-ops on None — dispatch still stops, the conversation is untouched.

    The neighbouring `test_an_edited_location_message_is_not_filed_as_junk_support`
    cannot catch this: it sends the tick with NO conversation open, so its
    `conversation_state(...) is None` assertion passes either way.
    """
    await bot.send(user.tap("add_new_address"))
    await bot.send(user.location(41.32354, 69.241036))
    assert bot.conversation_state("address_conversation") == ADDRESS_TITLE, (
        "precondition: the customer is parked mid-flow on the title prompt"
    )

    tick = user.location(41.3236, 69.2411).to_dict()
    tick["edited_message"] = tick.pop("message")
    tick["update_id"] += 1000
    await bot.send(Update.de_json(tick, user.bot))

    assert bot.conversation_state("address_conversation") == ADDRESS_TITLE, (
        "a live-location tick ended the customer's open address conversation; "
        "the null-message guard in _route_address_location_entry must raise a "
        "BARE ApplicationHandlerStop, never ApplicationHandlerStop(END)"
    )
    assert _support_calls(bot) == [], (
        "the tick must not be filed as support either"
    )


async def test_a_photo_with_no_flow_open_is_captured_silently(bot, user):
    await bot.send(user.photo(caption="the cap is cracked", file_id="tg-photo-9"))

    calls = _support_calls(bot)
    assert len(calls) == 1
    assert calls[0].data["message_type"] == "photo"
    assert calls[0].data["telegram_file_id"] == "tg-photo-9"
    assert calls[0].data["content"] == "the cap is cracked"

    # Silent by design — no auto-acknowledgement, matching the text path.
    assert bot.telegram.of("sendMessage") == []


async def test_a_document_is_captured_with_its_file_name(bot, user):
    await bot.send(user.document(file_name="receipt.pdf"))

    calls = _support_calls(bot)
    assert len(calls) == 1
    assert calls[0].data["attachment_file_name"] == "receipt.pdf"


async def test_a_voice_note_is_captured_and_no_longer_answered_with_not_supported(bot, user):
    """Decision D2: once voice reaches the inbox, 'not supported' is a lie."""
    await bot.send(user.voice())

    calls = _support_calls(bot)
    assert len(calls) == 1
    assert calls[0].data["message_type"] == "voice"
    assert bot.telegram.of("sendMessage") == []


async def test_a_sticker_with_no_flow_open_is_captured_as_unsupported(bot, user):
    """FIX 1: stickers matched none of the catch-all's filters, so
    `build_support_payload`'s UNSUPPORTED branch was unreachable in
    production and a sticker vanished with no record — the exact silent
    drop Goal 4 forbids. `filters.Sticker.ALL` closes that gap."""
    await bot.send(user.sticker())

    calls = _support_calls(bot)
    assert len(calls) == 1
    assert calls[0].data["message_type"] == "unsupported"

    payload_only_fields = {
        "telegram_file_id", "attachment_mime_type", "attachment_file_name",
        "attachment_size", "latitude", "longitude", "content",
    }
    assert not payload_only_fields & set(calls[0].data), (
        "an unsupported message must carry a type label only, no payload fields"
    )

    # Silent by design — no auto-acknowledgement, matching every other
    # attachment type.
    assert bot.telegram.of("sendMessage") == []


async def test_a_forwarded_message_keeps_its_attribution(bot, user):
    await bot.send(user.forwarded_text("look at this", sender_name="Dilnoza K"))

    calls = _support_calls(bot)
    assert len(calls) == 1
    assert calls[0].data["forwarded_from"] == "Dilnoza K"
    assert calls[0].data["forwarded_origin_type"] == "user"
