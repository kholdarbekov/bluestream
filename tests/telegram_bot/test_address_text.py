"""The one "title + full address" block: `address_lines` (plain) decides which parts show and in
what order; `address_block_markdown_v2` escapes those parts and bolds the title."""

import pytest
from address_text import address_block_markdown_v2, address_lines

from tests.bot_dispatcher_harness import telegram_entity_error

pytestmark = pytest.mark.unit


def test_the_plain_block_is_the_title_over_the_full_address_unescaped():
    assert address_lines("Uy", "15, Navoiy ko`chasi") == "Uy\n15, Navoiy ko`chasi"


@pytest.mark.parametrize("title, full_address, expected", [
    (None, "Chilonzor 15", "Chilonzor 15"),
    ("", "Chilonzor 15", "Chilonzor 15"),
    ("Uy", None, "Uy"),
    ("Uy", "", "Uy"),
    (None, None, ""),
])
def test_the_plain_block_leaves_out_an_empty_part(title, full_address, expected):
    assert address_lines(title, full_address) == expected


def test_the_block_is_a_bold_title_over_the_full_address():
    assert address_block_markdown_v2("Uy", "15, Navoiy ko`chasi, Toshkent") == (
        "*Uy*\n15, Navoiy ko\\`chasi, Toshkent"
    )


def test_every_markdown_v2_character_in_either_part_is_escaped():
    assert address_block_markdown_v2("Uy_1 *A*", "12-uy. (Navoiy) !") == (
        "*Uy\\_1 \\*A\\**\n12\\-uy\\. \\(Navoiy\\) \\!"
    )


@pytest.mark.parametrize("title, full_address, expected", [
    (None, "Chilonzor 15", "Chilonzor 15"),
    ("", "Chilonzor 15", "Chilonzor 15"),
    ("Uy", None, "*Uy*"),
    ("Uy", "", "*Uy*"),
    (None, None, ""),
])
def test_an_empty_part_is_left_out(title, full_address, expected):
    assert address_block_markdown_v2(title, full_address) == expected


@pytest.mark.parametrize("char", list("_*[]()~`>#+-=|{}.!\\"))
def test_telegram_accepts_the_block_whatever_the_address_holds(char):
    block = address_block_markdown_v2(f"Uy{char}1", f"Chilonzor {char} 15")
    assert telegram_entity_error(block, "MarkdownV2") is None
