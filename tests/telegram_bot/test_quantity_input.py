"""Reading a quantity the customer TYPED on the cart quantity screen.

`parse_typed_quantity` decides three different fates for a message, and each
one is a promise to a different reader:

* a whole number  -> the cart line is SET to it (the number goes to the backend);
* "not whole"     -> the customer tried to type a number and gets a hint;
* not a quantity  -> the words go to the Support Inbox exactly as today.

Every expectation below is a hand-written literal. The table is the contract:
a spelling that moves from one fate to another is either a bug (a customer's
"7 ta" filed as a support ticket) or a decision someone must make on purpose.
"""

import pytest

from quantity_input import TypedQuantity, parse_typed_quantity


@pytest.mark.parametrize(
    "text, expected",
    [
        # Plain digits, with the whitespace people leave around them.
        ("7", 7),
        ("  7  ", 7),
        ("07", 7),
        ("12", 12),
        # Uzbek and Russian count words, in both scripts Uzbek is written in.
        ("7 ta", 7),
        ("7ta", 7),
        ("7 TA", 7),
        ("7 dona", 7),
        ("7 та", 7),
        ("7 дона", 7),
        ("7 шт", 7),
        ("7шт.", 7),
        ("7 штук", 7),
        ("7 штуки", 7),
        # English and the multiplication shorthands.
        ("7 pcs", 7),
        ("x7", 7),
        ("x 7", 7),
        ("7x", 7),
        ("×7", 7),
        # The Cyrillic letter a Russian/Uzbek keyboard types for "x" (U+0445).
        ("\u04457", 7),
        ("7\u0445", 7),
        ("7 \u0445", 7),
        # Digits phones type that are not ASCII.
        ("7\ufe0f\u20e3", 7),  # keycap 7️⃣
        ("\uff17", 7),  # full-width ７
        ("\u0667", 7),  # Arabic-Indic ٧
        ("\u06f7", 7),  # Extended Arabic-Indic (Persian) ۷
        ("1\ufe0f\u20e32\ufe0f\u20e3", 12),  # 1️⃣2️⃣
        # Zero and too-large counts ARE whole numbers; the bounds refuse them,
        # not the parser (so the customer is told the limit, not "unreadable").
        ("0", 0),
        ("000", 0),
        ("9999", 9999),
    ],
)
def test_a_whole_number_is_read_as_that_number(text, expected):
    assert parse_typed_quantity(text) == TypedQuantity(value=expected)


@pytest.mark.parametrize(
    "text",
    [
        "2.5",
        "2,5",
        "-3",
        "\u22123",  # − MINUS SIGN
        "+5",  # "add five" or "five"? Ambiguous, so ask for the total instead.
        "- 3",
        "2.5 ta",
        "1,5",
    ],
)
def test_a_number_that_is_not_a_whole_count_asks_for_one(text):
    """The customer clearly tried to type an amount, so filing it as a support
    ticket would leave them waiting on an operator for a typo."""
    assert parse_typed_quantity(text) == TypedQuantity(value=None)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "salom",
        "besh",  # number words are not read
        "пять",
        "5 or 6",
        "5 yoki 6",
        "5-6",
        "5 ta suv",
        "5 bottles",
        "+998 90 123 45 67",
        "1 000",
        "Buyurtma #1125 qayerda?",
        "7 ta 8",
        "x",
        "ta",
        # Five digits and more is an identifier, not a count: a phone number,
        # an order number, a verification code. No per-item cap comes close.
        "12345",
        "123456",
        "901234567",
        "998901234567",
        "+998901234567",
        "999999999999",
        # Dates and times an operator may have asked for: "half a bottle" has
        # one decimal digit, a date or a clock time has two.
        "25.09",
        "18.00",
        "9.30",
        "10,30",
        "1,000",
    ],
)
def test_anything_else_is_not_a_quantity(text):
    """These must keep reaching the Support Inbox: a phone number, an order
    question or "5 or 6" is somebody talking to a human."""
    assert parse_typed_quantity(text) is None
