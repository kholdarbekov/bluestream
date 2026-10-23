"""The test transport parses entities the way Telegram does, but only when asked.

Pins the port of tdlib's two Markdown parsers (tests/bot_dispatcher_harness.py)
against the one refusal production has logged: 2026-10-18, 4 x "Can't parse
entities: can't find end of the entity starting at byte offset 100".
"""

from __future__ import annotations

import pytest
from telegram import Bot
from telegram.error import BadRequest

from tests.bot_dispatcher_harness import FakeTelegramTransport, telegram_entity_error
from tests.telegram_bot.ptb_harness import build_bot_harness

pytestmark = [pytest.mark.unit, pytest.mark.anyio]

PARSE_ERROR = "Bad Request: can't parse entities: "
V2_DASH = PARSE_ERROR + "Character '-' is reserved and must be escaped with the preceding '\\'"

# The 2026-10-18 card, rebuilt: the uz `telegram.checkout.delivering_to` copy
# (29 bytes), a typed Cyrillic title (55 bytes) and the geocoder's Uzbek o' as ONE backtick.
INCIDENT_LABEL = "📍 Yetkazib berish manzili:"
INCIDENT_TITLE = "Бувимнинг уйи Навоий кўчасида"
INCIDENT_ADDRESS = "5, Navoiy ko`chasi, Toshkent"
INCIDENT_TEXT = f"{INCIDENT_LABEL}\n*{INCIDENT_TITLE}*\n{INCIDENT_ADDRESS}"


def test_an_unclosed_backtick_is_reported_at_its_utf8_byte_offset_like_on_2026_10_18():
    assert INCIDENT_TEXT.index("`") == 71, "a CHARACTER offset; Telegram counts UTF-8 bytes"
    assert telegram_entity_error(INCIDENT_TEXT, "Markdown") == (
        PARSE_ERROR + "Can't find end of the entity starting at byte offset 100"
    )


def test_a_paired_backtick_is_a_code_span_not_an_error():
    text = f"{INCIDENT_LABEL}\n*Uy*\n15, Navoiy ko`chasi` Toshkent"
    assert telegram_entity_error(text, "Markdown") is None


def test_legacy_markdown_does_not_nest_so_underscore_backtick_and_bracket_inside_bold_are_text():
    assert telegram_entity_error("*Uy_1 [A] `x*", "Markdown") is None


def test_only_an_asterisk_ends_a_legacy_bold_title_early():
    assert telegram_entity_error("*Uy*1*", "Markdown") == (
        PARSE_ERROR + "Can't find end of the entity starting at byte offset 5"
    )


def test_a_legacy_backslash_escapes_outside_an_entity_and_not_inside_one():
    assert telegram_entity_error("Navoiy ko\\`chasi", "Markdown") is None
    assert telegram_entity_error("*Uy\\*1*", "Markdown") == (
        PARSE_ERROR + "Can't find end of the entity starting at byte offset 6"
    )


def test_legacy_links_and_pre_blocks_parse_and_an_open_bracket_does_not():
    assert telegram_entity_error("[Navoiy](https://x.uz) ok", "Markdown") is None
    assert telegram_entity_error("```\nko`chasi\n```", "Markdown") is None
    assert telegram_entity_error("ok [Navoiy", "Markdown") == (
        PARSE_ERROR + "Can't find end of the entity starting at byte offset 3"
    )


def test_markdown_v2_refuses_an_unescaped_reserved_character_and_accepts_it_escaped():
    assert telegram_entity_error("12-uy.", "MarkdownV2") == V2_DASH
    assert telegram_entity_error("12\\-uy\\.", "MarkdownV2") is None


def test_markdown_v2_entities_close_and_an_unclosed_one_is_named():
    assert telegram_entity_error("*Uy \\*1\\**", "MarkdownV2") is None
    assert telegram_entity_error(
        "_a_ __b__ ~c~ ||d|| `e.f` ```g.h``` [i](https://x.uz/a\\)b)", "MarkdownV2"
    ) is None
    assert telegram_entity_error("*Uy", "MarkdownV2") == (
        PARSE_ERROR + "Can't find end of Bold entity at byte offset 0"
    )


def test_plain_text_is_never_parsed_here():
    assert telegram_entity_error(INCIDENT_TEXT, None) is None


def test_html_is_refused_where_telegram_refuses_it():
    """The confirm screen is HTML (2026-10-23): a raw "<" from an address must fail here as it would live."""
    assert telegram_entity_error("<b>x</b> &lt;2&gt; &amp; co", "HTML") is None
    assert telegram_entity_error("<b>x", "HTML") is not None
    assert telegram_entity_error("Uy <2>", "HTML") is not None
    assert telegram_entity_error("<b><i>x</b></i>", "HTML") is not None


def _bot(transport):
    return Bot("424242:TEST-TOKEN", request=transport, get_updates_request=transport)


async def test_a_strict_transport_refuses_the_incident_card_exactly_as_telegram_did():
    transport = FakeTelegramTransport(strict_entities=True)

    with pytest.raises(BadRequest) as refused:
        await _bot(transport).send_message(chat_id=700100200, text=INCIDENT_TEXT, parse_mode="Markdown")

    # Byte for byte the prod log line: PTB strips "Bad Request: " and capitalises.
    assert str(refused.value) == (
        "Can't parse entities: can't find end of the entity starting at byte offset 100"
    )
    (call,) = transport.of("sendMessage")
    assert call.refused == PARSE_ERROR + "Can't find end of the entity starting at byte offset 100"
    assert transport.delivered == []

    await _bot(transport).send_message(chat_id=700100200, text=INCIDENT_TEXT)
    assert [c.text for c in transport.delivered] == [INCIDENT_TEXT], "no parse mode, nothing to parse"


async def test_a_strict_transport_parses_edits_and_captions_too():
    transport = FakeTelegramTransport(strict_entities=True)
    bot = _bot(transport)

    with pytest.raises(BadRequest):
        await bot.edit_message_text(chat_id=700100200, message_id=5001, text="*Uy", parse_mode="Markdown")
    with pytest.raises(BadRequest):
        await bot.edit_message_caption(
            chat_id=700100200, message_id=5001, caption="12-uy.", parse_mode="MarkdownV2"
        )

    assert [c.refused for c in transport.calls] == [
        PARSE_ERROR + "Can't find end of the entity starting at byte offset 0",
        V2_DASH,
    ]


async def test_the_default_transport_parses_nothing():
    transport = FakeTelegramTransport()
    assert transport.strict_entities is False

    await _bot(transport).send_message(chat_id=700100200, text=INCIDENT_TEXT, parse_mode="Markdown")

    assert transport.delivered[-1].refused is None


async def test_a_scripted_failure_wins_over_strict_parsing_and_is_marked_refused():
    transport = FakeTelegramTransport(strict_entities=True)
    transport.fail("sendMessage", "Bad Request: chat not found")

    with pytest.raises(BadRequest):
        await _bot(transport).send_message(chat_id=700100200, text=INCIDENT_TEXT, parse_mode="Markdown")

    assert transport.calls[-1].refused == "Bad Request: chat not found"


async def test_build_bot_harness_hands_strict_entities_to_its_transport(monkeypatch):
    harness = await build_bot_harness(monkeypatch, strict_entities=True)
    assert harness.telegram.strict_entities is True
