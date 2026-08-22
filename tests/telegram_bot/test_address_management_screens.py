"""The address MANAGEMENT screens: what Telegram parses, and what a tap forgets.

WHY THIS FILE EXISTS
--------------------
`test_manual_address_entry_journey.py` drives these same five screens (view /
edit menu / rename / instructions / delete confirmation) with copy and data
that are both innocent: plain seeded strings, addresses like "Amir Temur 7".
Production is not innocent on either side, and the two defects below only show
up when it is not.

1. THE COPY IS MARKUP, THE ADDRESS IS DATA.
   The seeded copy for these screens carries deliberate bold (`**{title}**`),
   which is why the messages go out with `parse_mode='Markdown'`. The address
   interpolated into it is written by a customer or by a geocoder, and Uzbek
   addresses really do carry `_` (building suffixes like "15_A") and `[`
   (geocoder annotations). Interpolated raw, one of those makes Telegram refuse
   the whole message — the same rejection that killed `location_received` for
   everyone living on such a street, fixed there on 2026-08-21 by escaping the
   DATA and leaving the copy's own `*bold*` alone.
   On these screens it was not fixed, and `_edit_or_replace_callback_message`
   cannot rescue it: its fallback re-sends with the SAME parse mode, so the
   second attempt is refused for the same reason and the handler's
   `except Exception` ends in a generic error toast. A customer whose street
   name contains an underscore could not view, rename or delete their own
   address.

2. NAVIGATING AWAY IS NOT "FORGET EVERYTHING".
   These screens open on top of DB-backed prompts (`awaiting_input`), so each
   has to disarm the prompt it owns or the customer's next sentence is written
   over their address title. Every one of them did that with
   `update_user_state(user_id, {})` — a blanket wipe that also threw away a
   concern report armed by "Report an issue" on the delivered summary, in
   silence, while its prompt and Cancel button stayed on screen still saying a
   report was open. `add_address` had exactly this defect and was fixed in the
   previous wave; the six navigation handlers here had it too.
   The fix is one targeted door — `BotUserRepository.clear_awaiting_input` —
   through which a screen names the prompts it owns and nothing else is
   touched.

Everything here goes in through `Application.process_update` on the real
application, so the real keyboards, the real handler groups and the real
`bot_state` round trip are all in the loop.
"""

from __future__ import annotations

import json
import re

import pytest

# Module level, before anything below touches them, so `i18n`, `keyboards` and
# `config` resolve as the BOT's versions. See tests/telegram_bot/conftest.py.
import utils as utils_module
from handlers import profile as profile_module

from tests.telegram_bot.ptb_harness import FakeDatabase, build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


# The order behind the delivered summary whose "Report an issue" button arms the
# concern flow (telegram_bot/webhook_server.py renders `report_issue_<order_id>`).
ORDER_ID = 555
ORDER_NUMBER = "ORD-2026-0555"

# Address data that Telegram's Markdown parser objects to. Not invented: `15_A`
# is an ordinary Tashkent building suffix and `[...]` is how geocoders annotate.
HOSTILE_TITLE = "Uy_2"
HOSTILE_ADDRESS = "Chilonzor 15_A, [Yangi] uy, Toshkent"
HOSTILE_STREET = "Bunyodkor_ko'chasi"
HOSTILE_INSTRUCTIONS = "Domofon 15_A, [ikkinchi] eshik"

# The REAL seeded copy (scripts/seed_backend_translations.py). Supplied verbatim
# because half of what these tests assert is that the copy's own `**bold**`
# survives untouched while the data inside it is escaped — a table of
# metacharacter-free stand-ins could not tell the fix from a blanket
# escape-everything, which is the trap on the other side of this bug.
UZ = {
    "telegram.address.details_title": "📍 **{title}**\n\n",
    "telegram.address.details_full_address": "**To'liq manzil:** {address}\n",
    "telegram.address.details_street": "**Ko'cha:** {street}\n",
    "telegram.address.details_city": "**Shahar:** {city}\n",
    "telegram.address.details_default_badge": "\n🏠 **Asosiy manzil**\n",
    "telegram.address.delete_confirmation": (
        "⚠️ **{title}** manzilini o'chirmoqchimisiz?\n\n{address}"
    ),
    "telegram.address.edit_title_prompt": (
        "📝 **Manzil nomini tahrirlash**\n\n**Joriy nom:** {current_title}\n\n"
        "Ushbu manzil uchun yangi nom kiriting:"
    ),
    "telegram.address.edit_instructions_prompt": (
        "📞 **Ko'rsatmalarni tahrirlash**\n\n**Joriy ko'rsatmalar:** "
        "{current_instructions}\n\nYangi ko'rsatmalarni kiriting:"
    ),
    "telegram.address.title_updated_success": (
        "✅ **Manzil nomi yangilandi!**\n\n**Yangi nom:** {title}"
    ),
    "telegram.address.select_edit_prompt": (
        "✏️ **Tahrirlash uchun manzilni tanlang:**\n\nManzilni bosing:"
    ),
    "telegram.address.select_delete_prompt": (
        "🗑️ **O'chirish uchun manzilni tanlang:**\n\n⚠️ **Ogohlantirish:** "
        "Bu amalni ortga qaytarib bo'lmaydi!"
    ),
    "telegram.address.edit_options_text": "✏️ **Manzilni tahrirlash variantlari:**",
    "telegram.address.list_header": "📍 Sizning manzillaringiz ({count}):\n\n",
    "telegram.address.untitled": "Nomsiz",
    "telegram.address.none_value": "Yo'q",
    "telegram.common.not_set": "Ko'rsatilmagan",
    "telegram.error_occurred": "Xatolik yuz berdi",
    # --- the concern flow armed from the delivered summary ---
    "telegram.support.describe_issue_prompt": (
        "Iltimos, #{order_number}-buyurtma bo'yicha muammoni yozib yuboring."
    ),
    "telegram.support.cancel_button": "Bekor qilish",
    "telegram.support.ack": "✅ Rahmat! Xabaringiz qo'llab-quvvatlash guruhiga yuborildi.",
}


class StatefulDatabase(FakeDatabase):
    """`FakeDatabase` that actually serves back the `bot_state` it stores.

    The stock fake answers `SELECT bot_state FROM users` with `None` — "no flow
    armed", for everyone, always — so every `awaiting_input` the bot writes is
    invisible on read-back and both halves of this file would assert nothing.
    Not a re-implementation: the read is wired to the same dict the production
    write path already updates in the parent.
    """

    async def fetchval(self, query, *args):
        if "bot_state" in query:
            return self.user.get("bot_state")
        return await super().fetchval(query, *args)


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(
        monkeypatch, translations=UZ, database=StatefulDatabase()
    )

    # The group-0 text catch-all consults the real Redis-backed rate limiter,
    # which FAILS CLOSED when Redis is unreachable. That is genuine external
    # I/O and the only seam in the text path the harness does not own.
    async def _always_allow(_user_id):
        return True

    monkeypatch.setattr(utils_module.rate_limiter, "allow_request", _always_allow)

    harness.backend.route(
        "GET",
        f"/api/v1/orders/{ORDER_ID}",
        lambda _c: {"data": {"order": {"id": ORDER_ID, "order_number": ORDER_NUMBER}}},
    )
    return harness


@pytest.fixture
def user(bot):
    return bot.updates()


@pytest.fixture
def address_book(bot):
    """Two saved addresses: one whose every field is hostile to the parser."""
    bot.backend.addresses.update(
        {
            901: {
                "id": 901,
                "title": HOSTILE_TITLE,
                "full_address": HOSTILE_ADDRESS,
                "street_address": HOSTILE_STREET,
                "city": "Toshkent",
                "is_default": True,
                "delivery_instructions": HOSTILE_INSTRUCTIONS,
            },
            902: {
                "id": 902,
                "title": "Ish",
                "full_address": "Amir Temur 7, Mirobod",
                "street_address": "Amir Temur",
                "city": "Toshkent",
                "is_default": False,
                "delivery_instructions": None,
            },
        }
    )
    return bot.backend.addresses


# ---------------------------------------------------------------------------
# Telegram's parser, as a seam
# ---------------------------------------------------------------------------

# An UNESCAPED `_` or `[` in a legacy-Markdown message is what Telegram refuses
# with "can't parse entities" — it opens an entity that never closes. `*` is
# NOT in this set on purpose: the seeded copy above uses it deliberately and
# Telegram is happy with it, so a fix that escaped the copy along with the data
# would still fail this file (it would produce a screen full of `\*\*`).
_UNESCAPED_METACHARACTER = re.compile(r"(?<!\\)[_\[]")


class MarkdownParser:
    """Answer `sendMessage`/`editMessageText` the way Telegram's parser does.

    Scripted rather than assumed because the interesting half of this bug is
    what happens AFTER the rejection: `_edit_or_replace_callback_message`
    retries by sending a new message with the same `parse_mode`, so a screen
    that fails to parse fails TWICE and the handler's `except Exception` turns
    it into a generic error toast. Only a transport that really refuses can
    show that.

    Records the exact params dict of each rejected call, because
    `transport.shown` records what the bot ATTEMPTED to show and a message
    Telegram refused is not a message the customer saw.
    """

    def __init__(self, transport):
        self.transport = transport
        self.rejected: list[dict] = []
        for endpoint in ("sendMessage", "editMessageText"):
            transport.failures[endpoint] = self._respond

    def _respond(self, params):
        text = params.get("text", "")
        if params.get("parse_mode") != "Markdown" or not _UNESCAPED_METACHARACTER.search(text):
            return 200, {
                "ok": True,
                "result": self.transport._result_for("sendMessage", params),
            }
        self.rejected.append(params)
        return 400, {
            "ok": False,
            "error_code": 400,
            "description": (
                "Bad Request: can't parse entities: Can't find end of the entity "
                "starting at byte offset 42"
            ),
        }


@pytest.fixture
def telegram_parses_markdown(bot):
    return MarkdownParser(bot.telegram)


def delivered(bot, parser):
    """The messages the customer actually received, refused attempts removed."""
    return [
        call
        for call in bot.telegram.shown
        if not any(call.params is params for params in parser.rejected)
    ]


def last_delivered_text(bot, parser) -> str:
    received = delivered(bot, parser)
    assert received, "the bot showed the customer nothing at all"
    return received[-1].text


def answered_toasts(bot) -> list[str]:
    return [call.params.get("text", "") for call in bot.telegram.of("answerCallbackQuery")]


def updates(bot):
    return [
        (int(call.endpoint.rsplit("/", 1)[-1]), call.data)
        for call in bot.backend.calls
        if call.method == "PUT" and call.endpoint.startswith("/api/v1/auth/addresses/")
    ]


def support_posts(bot) -> list[dict]:
    return [
        call.data
        for call in bot.backend.calls
        if call.method == "POST" and call.endpoint == "/api/v1/support/messages"
    ]


def armed_state(bot) -> dict:
    return json.loads(bot.database.user["bot_state"])


async def arm_the_concern_flow(bot, user):
    """Tap the delivered summary's 'Report an issue' button."""
    await bot.send(user.tap(f"report_issue_{ORDER_ID}"))
    assert armed_state(bot)["awaiting_input"] == "support_message", (
        "the concern flow did not arm, so nothing below is being tested"
    )


# ===========================================================================
# 1. The copy is markup, the address is data
# ===========================================================================


async def test_an_address_with_an_underscore_can_still_be_viewed(
    bot, user, address_book, telegram_parses_markdown
):
    """The detail screen is the gateway to rename, set-default and delete.

    With the street interpolated raw, Telegram refused the edit AND the
    re-send, and the customer got a "Xatolik yuz berdi" toast over an unchanged
    screen — every single time, for as long as they live on that street.
    """
    await bot.send(user.tap("view_address_901"))

    assert telegram_parses_markdown.rejected == [], (
        "Telegram refused the screen: address data reached it unescaped"
    )
    detail = last_delivered_text(bot, telegram_parses_markdown)
    assert UZ["telegram.error_occurred"] not in answered_toasts(bot)

    # The data is escaped...
    assert "Chilonzor 15\\_A" in detail and "\\[Yangi]" in detail
    assert "Bunyodkor\\_ko'chasi" in detail
    assert HOSTILE_ADDRESS not in detail
    # ...and the copy's own bold is NOT, or the customer reads a screen full of
    # backslashes instead of a formatted address.
    assert "**To'liq manzil:**" in detail and "**Ko'cha:**" in detail
    assert "\\*" not in detail
    # The screen is still the screen: same buttons, nothing dropped.
    assert delivered(bot, telegram_parses_markdown)[-1].callback_data() == [
        "edit_address_901",
        "delete_address_901",
        "manage_addresses",
    ]


async def test_deleting_an_underscored_address_still_asks_first(
    bot, user, address_book, telegram_parses_markdown
):
    """The confirmation has to NAME what is about to be destroyed.

    Refused, the customer saw only an error toast — and the Delete button they
    tapped is one they can reach again and again with the same result, so the
    address became undeletable from the bot.
    """
    await bot.send(user.tap("delete_address_901"))

    assert telegram_parses_markdown.rejected == []
    confirmation = delivered(bot, telegram_parses_markdown)[-1]
    assert "Uy\\_2" in confirmation.text and "Chilonzor 15\\_A" in confirmation.text
    assert "**Uy\\_2**" in confirmation.text, "the copy's bold must survive the escape"
    assert confirmation.callback_data() == [
        "confirm_delete_address_901",
        "view_address_901",
    ]
    assert UZ["telegram.error_occurred"] not in answered_toasts(bot)


async def test_the_rename_prompt_quotes_a_title_that_carries_an_underscore(
    bot, user, address_book, telegram_parses_markdown
):
    """The prompt quotes the CURRENT title back, and a title is customer text.

    A customer who once named an address "Uy_2" could never rename it again:
    the prompt that asks for the new name was the message Telegram refused.
    """
    await bot.send(user.tap("edit_title_901"))

    assert telegram_parses_markdown.rejected == []
    prompt = last_delivered_text(bot, telegram_parses_markdown)
    assert "**Joriy nom:** Uy\\_2" in prompt
    assert armed_state(bot)["awaiting_input"] == "edit_address_title", (
        "the prompt must still arm the completer that reads the next message"
    )


async def test_the_instructions_prompt_quotes_instructions_a_courier_wrote(
    bot, user, address_book, telegram_parses_markdown
):
    """Delivery instructions are free text with door codes in them — the field
    most likely of all to carry an underscore or a bracket.
    """
    await bot.send(user.tap("edit_instructions_901"))

    assert telegram_parses_markdown.rejected == []
    prompt = last_delivered_text(bot, telegram_parses_markdown)
    assert "Domofon 15\\_A" in prompt and "\\[ikkinchi]" in prompt
    assert armed_state(bot)["awaiting_input"] == "edit_address_instructions"


async def test_renaming_an_address_to_an_underscored_name_is_confirmed_not_mourned(
    bot, user, address_book, telegram_parses_markdown
):
    """The write happens BEFORE the confirmation is rendered.

    So a new title containing `_` was saved and then reported as a failure: the
    confirmation was refused, the `except Exception` replied with the generic
    "could not update" copy, and the customer renamed it again — over an
    address that already carried the name they wanted.
    """
    await bot.send(user.tap("edit_title_901"))
    bot.telegram.reset()

    await bot.send(user.text("Ofis_2"))

    assert updates(bot) == [(901, {"title": "Ofis_2"})]
    assert telegram_parses_markdown.rejected == []
    confirmation = last_delivered_text(bot, telegram_parses_markdown)
    assert "**Yangi nom:** Ofis\\_2" in confirmation
    assert UZ["telegram.error_occurred"] not in bot.telegram.texts()


async def test_the_pickers_survive_a_hostile_title_because_buttons_are_never_parsed(
    bot, user, address_book, telegram_parses_markdown
):
    """The control, and the reason the two picker screens need no escaping.

    `select_edit_address` and `select_delete_address` put the address TITLE in
    inline-button labels, which Telegram never runs through a parser, and their
    message text is pure seeded copy with nothing interpolated into it. So the
    hostile title reaches the customer verbatim — as a label — and the screen
    renders. This test exists so that stays true: the day somebody moves a
    title into the message text, it fails here rather than in production.
    """
    await bot.send(user.tap("select_edit_address"))
    assert telegram_parses_markdown.rejected == []
    picker = delivered(bot, telegram_parses_markdown)[-1]
    assert picker.text == UZ["telegram.address.select_edit_prompt"]
    assert HOSTILE_TITLE in " ".join(picker.button_labels())

    await bot.send(user.tap("select_delete_address"))
    assert telegram_parses_markdown.rejected == []
    picker = delivered(bot, telegram_parses_markdown)[-1]
    assert picker.text == UZ["telegram.address.select_delete_prompt"]
    assert HOSTILE_TITLE in " ".join(picker.button_labels())


# ===========================================================================
# 2. Navigating away is not "forget everything"
# ===========================================================================


async def test_browsing_the_profile_leaves_an_armed_concern_report_alone(
    bot, user, address_book
):
    """The defect `add_address` was fixed for, on the six screens that kept it.

    A customer taps "Report an issue" under a delivered order, then — before
    typing — goes to check the address that delivery went to. Profile, then
    Addresses, then the address itself, then its edit menu, then back to the
    profile edit sub-menu. Every one of those handlers opened with
    `update_user_state(user_id, {})`, so by the time they came back to type,
    the report had been disarmed in silence and their complaint was filed as an
    anonymous note against no order — or, if the concern prompt had already
    scrolled away, not filed at all.

    Arming is DB-backed precisely so it survives a detour.
    """
    await arm_the_concern_flow(bot, user)
    armed_before = armed_state(bot)

    for screen in (
        "menu_profile",
        "manage_addresses",
        "view_address_901",
        "edit_address_901",
        "edit_profile",
    ):
        await bot.send(user.tap(screen))
        assert armed_state(bot) == armed_before, (
            f"tapping {screen} disarmed a flow it does not own"
        )

    complaint = "Suv idishi ochilgan holda keldi"
    await bot.send(user.text(complaint))

    assert [post["content"] for post in support_posts(bot)] == [
        f"[Order #{ORDER_NUMBER}] {complaint}"
    ], "the concern armed before the detour must still be filed against its order"
    assert UZ["telegram.support.ack"] in bot.telegram.texts(), (
        "and the customer must be told it went somewhere"
    )


@pytest.mark.parametrize(
    "escape_screen",
    ["view_address_901", "manage_addresses", "menu_profile", "edit_address_901"],
)
async def test_an_abandoned_rename_does_not_survive_the_screen_it_was_left_for(
    bot, user, address_book, escape_screen
):
    """The other half, and the reason the blanket wipes were there at all.

    The rename prompt's Cancel button is just `view_address_<id>`; the other
    three screens are where the rest of the navigation lands. None of them is a
    "cancel" callback, so if they stop disarming `edit_address_title` the
    customer's next unrelated message is silently written over their address
    title. Keeping a foreign flow armed must not turn into keeping OUR OWN
    abandoned flow armed.
    """
    await bot.send(user.tap("edit_title_901"))
    assert armed_state(bot)["awaiting_input"] == "edit_address_title"

    await bot.send(user.tap(escape_screen))

    assert armed_state(bot) == {}, f"{escape_screen} left the rename prompt armed"

    await bot.send(user.text("qachon yetkazasiz"))

    assert updates(bot) == [], "an unrelated message was written as the address title"
    assert address_book[901]["title"] == HOSTILE_TITLE
    assert support_posts(bot) == [{"content": "qachon yetkazasiz"}], (
        "with no pending edit the message should reach the support inbox"
    )


async def test_the_profile_edit_menu_disarms_its_own_prompt_and_nothing_else(
    bot, user, address_book
):
    """`edit_profile` owns the name/birthday prompts, so backing out of one has
    to disarm it — a stray "Ha" typed later must not become the customer's
    name. It owns nothing else, which is what the concern half above pins.
    """
    await bot.send(user.tap("edit_profile_name"))
    assert armed_state(bot)["awaiting_input"] == "edit_profile_name"

    await bot.send(user.tap("edit_profile"))

    assert armed_state(bot) == {}, "the abandoned name prompt is still armed"

    await bot.send(user.text("Ha"))
    profile_writes = [
        call
        for call in bot.backend.calls
        if call.method == "PUT" and call.endpoint == "/api/v1/auth/profile"
    ]
    assert profile_writes == [], "an unrelated message was written as the user's name"


def test_leaving_a_flow_has_exactly_one_expression_in_this_handler_group():
    """The SSOT guard, not a style rule.

    This whole defect class is one rule with two expressions: a screen that
    disarms with `update_user_state(user_id, {})` cannot know whose flow it is
    wiping, and every copy of that line is a fresh chance to wipe somebody
    else's. `BotUserRepository.clear_awaiting_input` is now the single door —
    the caller names the prompts it owns — so a blanket wipe reappearing in
    this module is the regression, whatever screen it sits on.

    `update_user_state` itself stays legitimate for ARMING: those calls write a
    fresh state document, which is the invariant `clear_awaiting_input` relies
    on (see its docstring).
    """
    import inspect

    source = inspect.getsource(profile_module)
    blanket_wipes = re.findall(r"update_user_state\(\s*\w+\s*,\s*\{\s*\}\s*\)", source)

    assert blanket_wipes == [], (
        "a blanket bot_state wipe is back in handlers/profile.py; use "
        "`clear_awaiting_input(user_id, *<the prompts this screen owns>)` so a "
        "concern report armed elsewhere is not thrown away with it"
    )
