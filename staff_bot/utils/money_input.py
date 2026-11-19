"""One rule for every money amount a driver types in the staff bot.

A thousands separator is a space, a dot or a comma, used the same way between
every group of three digits; a decimal mark is a dot or a comma followed by one
or two digits. "150.000" is therefore one hundred fifty thousand, never 150.
"""
import math
import re
from decimal import Decimal
from typing import Optional

_GROUPED = re.compile(
    r"(?P<int>\d{1,3}(?P<sep>[ .,])\d{3}(?:(?P=sep)\d{3})*|\d+)"
    r"(?:(?P<dec>[.,])(?P<frac>\d{1,2}))?",
    re.ASCII,
)
_SPACES = str.maketrans({" ": " ", " ": " "})


def parse_typed_money(raw_text: Optional[str]) -> Decimal:
    """Return the typed amount (>= 0, finite, at most two decimals).

    Raises ``ValueError`` for anything else, so every caller's existing
    ``except (TypeError, ValueError)`` keeps catching it.
    """
    text = (raw_text or "").translate(_SPACES).strip()
    match = _GROUPED.fullmatch(text)
    if match is None:
        raise ValueError("not a money amount")
    if match["sep"] and match["dec"] and match["sep"] == match["dec"]:
        raise ValueError("separator used as both grouping and decimal mark")
    digits = re.sub(r"[ .,]", "", match["int"])
    if match["frac"]:
        digits += "." + match["frac"]
    amount = Decimal(digits)
    if not math.isfinite(float(amount)):
        raise ValueError("amount overflows to infinity")
    return amount
