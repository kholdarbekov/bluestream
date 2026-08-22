"""Structural contracts on the staff bot's wiring.

These are not "does the handler compute the right thing" tests — the rest of
``tests/staff_bot/`` covers that. These check the layer underneath it, which no
existing test can see: that the buttons the bot renders are actually connected
to something, in the state the driver is actually in.

The staff bot is unusually exposed here. Its main menu is a REPLY keyboard, and
reply-keyboard taps arrive as ordinary text, matched by regexes compiled from
localized labels at handler-build time. A label that changes shape, a missing
translation row, or a conversation state that forgot to register the menu
escape all produce the same symptom: the driver taps, and nothing happens.
"""

import pytest
from telegram.ext import CallbackQueryHandler, ConversationHandler

from tests.staff_bot.ptb_harness import build_staff_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
async def staff(monkeypatch):
    return await build_staff_harness(monkeypatch)


@pytest.fixture
def driver(staff):
    return staff.updates()


def _callback_handlers(application):
    """Every CallbackQueryHandler, tagged with the SCOPE it competes in.

    Scope matters more than it looks. Handlers only shadow each other when PTB
    would offer them the same update: top-level handlers within one group do,
    and so do the handlers listed under one conversation state — but a fallback
    inside `staff_create_user` never competes with the identical fallback inside
    `staff_search_user`, because a driver is only ever in one of them. Flattening
    those together turns every shared Cancel button into a false alarm.
    """
    for group in sorted(application.handlers):
        for handler in application.handlers[group]:
            if isinstance(handler, CallbackQueryHandler):
                yield ("group", group), handler
            elif isinstance(handler, ConversationHandler):
                for state, state_handlers in handler.states.items():
                    for inner in state_handlers:
                        if isinstance(inner, CallbackQueryHandler):
                            yield ("state", handler.name, state), inner
                for kind, group_of in (("entry", handler.entry_points), ("fallback", handler.fallbacks)):
                    for inner in group_of:
                        if isinstance(inner, CallbackQueryHandler):
                            yield (kind, handler.name), inner


# Staff conversations that time out with no TIMEOUT handler. EMPTY, and the
# test below asserts it stays empty in both directions.
#
# Eleven of them used to be listed here. Every one ended in silence after 5
# minutes: the driver was left on a prompt whose buttons were dead, and the
# flow's keys survived it in user_data for the next flow to trip over. It is
# the same defect that lost 20 of 33 customer addresses in the CUSTOMER bot's
# address_conversation (fixed 2026-08-21), and it is fixed here the same way —
# a `_flow_timeout()` state on every conversation, carrying BOTH a
# MessageHandler and a CallbackQueryHandler because PTB re-dispatches whichever
# shape the staff member's last update was.
_CONVERSATIONS_THAT_EXPIRE_IN_SILENCE = set()


async def test_no_new_staff_conversation_expires_in_silence(staff):
    """Ratchet, not a red bar: the known-silent set may shrink, never grow."""
    silent = {
        handler.name
        for group in staff.application.handlers.values()
        for handler in group
        if isinstance(handler, ConversationHandler)
        and handler.conversation_timeout
        and ConversationHandler.TIMEOUT not in handler.states
    }

    new = silent - _CONVERSATIONS_THAT_EXPIRE_IN_SILENCE
    assert not new, (
        f"new staff conversations that time out with no TIMEOUT handler: {sorted(new)}. "
        "They will end without telling the driver anything and leave their flow "
        "keys in user_data. Register a TIMEOUT state (see the customer bot's "
        "address_conversation) rather than adding them to the allowlist."
    )

    fixed = _CONVERSATIONS_THAT_EXPIRE_IN_SILENCE - silent
    assert not fixed, (
        f"these conversations now handle their timeout: {sorted(fixed)}. "
        "Remove them from _CONVERSATIONS_THAT_EXPIRE_IN_SILENCE so the ratchet "
        "keeps holding the new ground."
    )


def _sample_matching(pattern_source: str):
    """A concrete callback_data that ``pattern_source`` accepts, or None.

    PTB picks the FIRST registered handler whose pattern matches, so a real
    collision needs a real string — not merely one literal prefix being a prefix
    of another. `^staff_new_orders$` looks like it shadows
    `^staff_new_orders_unified$` right up until you notice the `$`.

    Returns None when the pattern uses a construct this cannot sample, so an
    unsupported pattern is SKIPPED rather than guessed at.
    """
    import re

    body = pattern_source
    if body.startswith("^"):
        body = body[1:]
    body = re.sub(r"\(\:\|\$\)$", "", body)
    if body.endswith("$"):
        body = body[:-1]

    body = body.replace(r"\d+", "7").replace(r"\d*", "7").replace(r"\w+", "x")
    body = re.sub(r"\\(.)", r"\1", body)

    if re.search(r"[\[\](){}*+?|]", body):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_:.\-]+", body):
        return None
    return body


async def test_no_callback_button_is_stolen_by_an_earlier_handler(staff):
    """Two handlers whose patterns both accept the same callback_data: the one
    registered first wins and the second button silently does the wrong thing.

    Checked with concrete strings against the real registration order, which is
    what PTB actually does at dispatch time.
    """
    import re

    registered = []  # (scope, order, compiled pattern, source, callback name)
    for order, (scope, handler) in enumerate(_callback_handlers(staff.application)):
        if handler.pattern is None:
            continue
        compiled = (
            handler.pattern
            if isinstance(handler.pattern, re.Pattern)
            else re.compile(handler.pattern)
        )
        registered.append((scope, order, compiled, compiled.pattern, handler.callback.__qualname__))

    thefts = []
    for scope, order, compiled, source, name in registered:
        sample = _sample_matching(source)
        if sample is None or not compiled.match(sample):
            continue
        for other_scope, other_order, other_compiled, other_source, other_name in registered:
            if other_scope != scope or other_order >= order:
                continue
            if other_compiled.match(sample):
                thefts.append(
                    f"{sample!r} is meant for {source} ({name}) but "
                    f"{other_source} ({other_name}) is registered earlier in {scope}"
                )
                break

    assert not thefts, "callback data claimed by the wrong handler:\n  " + "\n  ".join(thefts)


# Registered staff patterns `_sample_matching` cannot turn into a concrete
# callback_data. Every entry is a handler the collision check is BLIND to, so
# this exists as a named place for the failure rather than as an allowance.
_PATTERNS_THE_COLLISION_CHECK_CANNOT_READ = {
    r"^staff_route_view_(next|all)$",
}


async def test_every_registered_pattern_is_readable_by_the_collision_check(staff):
    """Without this, the collision check degrades silently instead of failing.

    `_sample_matching` returns None for any shape it cannot sample — on purpose,
    because a fabricated sample would report collisions that do not exist. But a
    skipped pattern is an UNCHECKED handler, and skipping is invisible: add an
    alternation tomorrow and the theft test still passes, having quietly stopped
    looking at it. The customer bot guards the same hole in
    tests/telegram_bot/test_callback_contract_customer.py.
    """
    import re

    unreadable = set()
    for _scope, handler in _callback_handlers(staff.application):
        if handler.pattern is None:
            continue
        source = (
            handler.pattern.pattern
            if isinstance(handler.pattern, re.Pattern)
            else str(handler.pattern)
        )
        if _sample_matching(source) is None:
            unreadable.add(source)

    new = unreadable - _PATTERNS_THE_COLLISION_CHECK_CANNOT_READ
    assert not new, (
        "these registered patterns are invisible to "
        "test_no_callback_button_is_stolen_by_an_earlier_handler:\n  "
        + "\n  ".join(sorted(new))
        + "\nTeach _sample_matching the shape rather than widening the allowlist."
    )

    healed = _PATTERNS_THE_COLLISION_CHECK_CANNOT_READ - unreadable
    assert not healed, (
        f"the sampler can now read {sorted(healed)}. Strike them off so the "
        "ratchet holds the new ground."
    )


async def test_the_start_command_reaches_the_auth_conversation(staff, driver):
    """A /start that matches no entry point leaves a new driver with a bot that
    does nothing. Checked through real dispatch, including the bot_command
    entity a hand-rolled text update would omit."""
    matched = staff.handlers_matching(driver.command("start"))

    assert matched, "/start is claimed by no handler at all"
    assert any(
        isinstance(handler, ConversationHandler) and handler.name == "staff_auth"
        for _group, handler in matched
    ), "/start must enter the staff_auth conversation"


async def test_an_unknown_callback_does_not_raise_out_of_the_dispatcher(staff, driver):
    """A stale button from an old message must degrade, not explode.

    PTB SWALLOWS a handler exception when no error handler is registered, so a
    bare `await staff.send(...)` here would be unfailable — it would pass no
    matter how badly a handler crashed. Registering an error handler is what
    turns this into a real assertion: an exception on this path takes down the
    update and the driver is left with a permanent spinner.
    """
    errors = []
    staff.application.add_error_handler(
        lambda update, context: errors.append(context.error) and None
    )

    await staff.send(driver.tap("this_button_no_longer_exists_42"))

    assert errors == [], f"a stale button raised out of the dispatcher: {errors}"
