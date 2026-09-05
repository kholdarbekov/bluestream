"""Typing the next line of your address after the form's process died.

WHY THIS FILE EXISTS
--------------------
Manual address entry is seven typed prompts. The conversation that asks them
lives in memory, so a deploy mid-form leaves the customer looking at "Enter the
street" with nothing listening.

What they type next reaches the group-0 catch-all, which files it as a support
message — silently, by design. So the customer's street name, building number
and delivery instructions land one by one in the admin Support Inbox as
unsolicited messages, the address is never created, and nothing on their screen
changes to say the form is gone. They keep typing.

The durable evidence that they were mid-form already exists: every step
dual-writes ``address_draft`` into ``users.bot_state``
(SDD 2026-08-26-address-flow-bot-state), which a restart cannot touch. The text
router simply never asked.
"""

import json

import pytest

from tests.telegram_bot.ptb_harness import (
    DEFAULT_USER_ID,
    FakeDatabase,
    build_bot_harness,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


SUPPORT = "/api/v1/support/messages"

TRANSLATIONS = {
    "telegram.address.flow_timed_out": "THE-ADDRESS-FORM-EXPIRED",
}


class StatefulDatabase(FakeDatabase):
    """Serves back the bot_state it stores, so a durable draft is visible."""

    async def fetchval(self, query, *args):
        if "bot_state" in query:
            return (self.user or {}).get("bot_state")
        return await super().fetchval(query, *args)


@pytest.fixture
async def bot(monkeypatch):
    return await build_bot_harness(
        monkeypatch, translations=TRANSLATIONS, database=StatefulDatabase()
    )


@pytest.fixture
def user(bot):
    return bot.updates()


def support_posts(bot):
    return [c.data for c in bot.backend.calls if c.endpoint == SUPPORT]


def texts(bot):
    return [c.text for c in bot.telegram.shown if c.text]


def restart(bot):
    bot.application.user_data[DEFAULT_USER_ID].clear()
    for group in bot.application.handlers.values():
        for handler in group:
            conversations = getattr(handler, "_conversations", None)
            if conversations is not None:
                conversations.clear()
    bot.telegram.reset()


def arm_a_durable_draft(bot):
    """What every address step already writes as it goes."""
    bot.database.user["bot_state"] = json.dumps({
        "address_draft": {"step": "street", "temp_address_data": {"title": "Home"}}
    })


async def test_a_street_name_typed_after_a_deploy_is_not_filed_as_support(bot, user):
    """The customer is answering a question the bot asked. Filing it as an
    unsolicited support message loses the answer AND leaks it."""
    arm_a_durable_draft(bot)
    restart(bot)

    await bot.send(user.text("Amir Temur 15"))

    leaked = [p for p in support_posts(bot) if "Amir Temur 15" in json.dumps(p)]
    assert not leaked, (
        f"the customer's address line was filed into the Support Inbox: {leaked}"
    )


async def test_the_customer_is_told_the_form_is_gone(bot, user):
    """Silence is why they keep typing — each line filed separately."""
    arm_a_durable_draft(bot)
    restart(bot)

    await bot.send(user.text("Amir Temur 15"))

    assert any("THE-ADDRESS-FORM-EXPIRED" in t for t in texts(bot)), (
        f"nothing told the customer the form had gone; they saw {texts(bot)}"
    )


async def test_ordinary_free_text_is_still_captured_as_support(bot, user):
    """With no draft armed, free text is still a support message — that is the
    feature, and this guard must not swallow it."""
    await bot.send(user.text("my delivery never arrived"))

    assert support_posts(bot), (
        "free text with no address draft stopped reaching the Support Inbox"
    )
