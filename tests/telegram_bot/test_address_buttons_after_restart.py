"""Address-flow buttons tapped after the conversation that owned them died.

WHY THIS FILE EXISTS
--------------------
Manual address entry is seven prompts long and ``conversation_timeout`` is
86400s — deliberately, because customers put the phone down mid-way. So a
customer sitting on one of these screens across a deploy is the normal case, not
an edge one.

Every button on every one of those screens is registered ONLY inside the address
conversation's ``states``:

    ^cancel_address_creation$   ^skip_street$   ^skip_building$
    ^skip_apartment$            ^skip_floor$    ^skip_delivery_instructions$
    ^region_                    ^district_      ^back_to_region$
    ^addr_title_                ^confirm_geocode$  ^retry_geocode$

The conversation's state map is in memory. It dies with the process, while the
inline keyboard stays on the customer's phone forever. So after a deploy every
one of those buttons matches nothing in any group: no handler runs, nothing
answers the callback query, and Telegram spins the button until its own client
timeout and then gives up in silence.

Cancel is the cruellest of them. The one button whose entire job is "get me out
of here" is the one button that cannot.

Nothing is logged either, which is why this survived: an unanswered callback
query is indistinguishable, from the server side, from a tap that never
happened.
"""

import pytest

from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID, build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


TRANSLATIONS = {
    "telegram.address.flow_timed_out": "THIS-ADDRESS-FORM-EXPIRED",
    "telegram.back": "Back",
}

# Every button the seven address screens can render. Named individually rather
# than harvested from a keyboard builder so that deleting a registration cannot
# quietly shrink what this test covers.
ADDRESS_BUTTONS = [
    "cancel_address_creation",
    "skip_street",
    "skip_building",
    "skip_apartment",
    "skip_floor",
    "skip_delivery_instructions",
    "region_tashkent_city",
    "district_chilonzor",
    "back_to_region",
    "addr_title_home",
    "addr_title_work",
    "addr_title_other",
    "confirm_geocode",
    "retry_geocode",
]


@pytest.fixture
async def bot(monkeypatch):
    return await build_bot_harness(monkeypatch, translations=TRANSLATIONS)


@pytest.fixture
def user(bot):
    return bot.updates()


def restart(bot):
    bot.application.user_data[DEFAULT_USER_ID].clear()
    for group in bot.application.handlers.values():
        for handler in group:
            conversations = getattr(handler, "_conversations", None)
            if conversations is not None:
                conversations.clear()
    bot.telegram.reset()


def acting_handlers(bot, update):
    """Handlers that would actually DO something with this update.

    The pattern-less debug logger at group -1 matches every callback and
    processes none, so counting it would make this test answer "yes, wired" for
    any string at all.
    """
    return [
        (group, handler)
        for group, handler in bot.handlers_matching(update)
        if getattr(getattr(handler, "callback", None), "__name__", "")
        != "debug_callback_handler"
    ]


def answers(bot):
    return [
        call.params.get("text")
        for call in bot.telegram.of("answerCallbackQuery")
    ]


@pytest.mark.parametrize("callback", ADDRESS_BUTTONS)
async def test_every_address_button_is_claimed_after_a_restart(bot, user, callback):
    """A tap nobody claims spins to the client timeout and is never logged."""
    restart(bot)

    assert acting_handlers(bot, user.tap(callback)), (
        f"{callback!r} reaches no handler once the conversation is gone — the "
        "customer taps it and the bot does not so much as answer the query"
    )


async def test_the_expired_form_says_so_rather_than_spinning(bot, user):
    """Being claimed is not enough; the customer has to be told."""
    restart(bot)

    await bot.send(user.tap("cancel_address_creation"))

    said = [a for a in answers(bot) if a] + [
        c.text for c in bot.telegram.shown if c.text
    ]
    assert any("THIS-ADDRESS-FORM-EXPIRED" in s for s in said), (
        f"the customer was told nothing useful; the bot said {said}"
    )


async def test_a_live_address_flow_still_owns_its_own_buttons(bot, user):
    """The fallback must never shadow the real conversation. With the flow open,
    Cancel has to cancel — not report that the form expired."""
    await bot.send(user.tap("add_new_address"))
    assert bot.conversation_state("address_conversation") is not None, (
        "setup: the address conversation never opened"
    )
    bot.telegram.reset()

    await bot.send(user.tap("cancel_address_creation"))

    said = [a for a in answers(bot) if a] + [
        c.text for c in bot.telegram.shown if c.text
    ]
    assert not any("THIS-ADDRESS-FORM-EXPIRED" in s for s in said), (
        "the expiry fallback stole a tap from the live conversation"
    )
    assert bot.conversation_state("address_conversation") is None, (
        "Cancel did not actually end the live flow"
    )
