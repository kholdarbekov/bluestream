"""Reading a quantity the customer TYPED on the cart quantity screen.

Pure text in, verdict out: no I/O, no bounds. Whether 0 or 500 is ORDERABLE is
`ProductHandlers._purchase_bounds`'s question, and answering it here too would
be a second opinion about what the customer may buy. This module only decides
what the message MEANS, which has three answers:

* ``TypedQuantity(value=N)`` — a whole count (``7``, ``7 ta``, ``7 шт``,
  ``x7``, ``7️⃣``, ``７``, ``٧``);
* ``TypedQuantity(value=None)`` — the customer tried to type an amount but it
  is not a whole count (``2.5``, ``-3``, ``+5``), so they get a hint;
* ``None`` — not a quantity at all (words, phone numbers, ``5 or 6``), which
  must keep reaching the Support Inbox exactly as before.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

# Count words customers put after the number, in Uzbek (both scripts), Russian
# and English, plus the multiplication shorthands: Latin x, the sign ×, and the
# Cyrillic х (U+0445) a Russian/Uzbek keyboard types in its place. Longest
# spellings first so the alternation never stops at a prefix ("штук" before "шт").
_TIMES = r"[x\u0445\u00d7]"
_UNIT = rf"(?:dona|дона|ta|та|штук[аи]?|шт\.?|pcs|pc|{_TIMES})"

# A count is at most this many digits. No per-item cap comes near five; a
# longer run is a phone number, an order number or a verification code —
# words for a person, which must keep reaching the Support Inbox.
_MAX_DIGITS = 4
_COUNT = rf"\d{{1,{_MAX_DIGITS}}}"

_WHOLE = re.compile(rf"^(?:{_TIMES} ?)?({_COUNT}) ?{_UNIT}?$")
# Signed or fractional amounts: an attempt at a number, just not a count. One
# decimal digit is "half a bottle" (2.5, 1,5); two is a date or a clock time
# (25.09, 18.00) — an answer for a person, left to the Support Inbox.
_NOT_WHOLE = re.compile(
    rf"^(?:[+\-\u2212\u2013] ?{_COUNT}(?:[.,]\d)?|{_COUNT}[.,]\d) ?{_UNIT}?$"
)

# Emoji presentation selector and the combining keycap: "7️⃣" is '7' + both.
_KEYCAP_MARKS = str.maketrans("", "", "\ufe0f\u20e3")


@dataclass(frozen=True)
class TypedQuantity:
    """A message that reads as a quantity; ``value`` is None when it is not a
    whole count."""

    value: Optional[int]


def _ascii_digits(text: str) -> str:
    """Every Unicode decimal digit (full-width, Arabic-Indic, Persian) as ASCII.

    Deliberately NOT NFKC: that also folds superscripts, turning "5²" into 52.
    Only characters that ARE decimal digits are rewritten.
    """
    return "".join(
        str(unicodedata.decimal(ch)) if ch.isdecimal() and not ch.isascii() else ch
        for ch in text
    )


def parse_typed_quantity(text: str) -> Optional[TypedQuantity]:
    """What a typed message means as a quantity; None when it is not one."""
    normalized = _ascii_digits((text or "").translate(_KEYCAP_MARKS))
    normalized = " ".join(normalized.split()).casefold()

    whole = _WHOLE.match(normalized)
    if whole:
        return TypedQuantity(value=int(whole.group(1)))
    if _NOT_WHOLE.match(normalized):
        return TypedQuantity(value=None)
    return None
