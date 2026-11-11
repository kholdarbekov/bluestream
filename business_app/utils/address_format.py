"""Shorter geocoded address text.

Nominatim answers "54, Ahmad Donish ko'chasi, Yunusobod 11 mavzesi, Yunusobod, Yunusobod Tumani,
Toshkent, 100000, Oʻzbekiston". The postcode, the country and a district repeated in two
spellings add length, not information. Pure: no session, no config, no I/O.
"""

import re
from typing import Optional

from business_app.utils.district_text import DISTRICT_SUFFIX_TOKENS, fold_district

_POSTCODE = re.compile(r"\d{6}")

# Folded (lowercase, apostrophes removed) spellings of "Uzbekistan" a provider can return.
_COUNTRIES = frozenset({"ozbekiston", "uzbekistan", "узбекистан", "ўзбекистон"})


def shorten_geocoded_address(text: Optional[str]) -> Optional[str]:
    """Drop the postcode, the country and a redundant "<X> <district word>" part.

    Splits on commas, keeps order, drops only:
    - a part that is exactly six digits;
    - a part that is the country;
    - "<X> Tumani"/"<X> District"/... when "<X>" is itself another part ("Yunusobod, Yunusobod
      Tumani" keeps "Yunusobod"). A district named once stays: drivers use it.
    No fuzzy matching. None and empty text pass through unchanged, and so does text where
    nothing was dropped (typed spacing is not ours to normalise) or where everything would be
    dropped (never return a blank address).
    """
    if not text:
        return text
    parts = [part.strip() for part in text.split(",") if part.strip()]
    folded = [fold_district(part) for part in parts]
    kept = []
    dropped = False
    for index, (part, key) in enumerate(zip(parts, folded)):
        if _POSTCODE.fullmatch(key) or key in _COUNTRIES:
            dropped = True
            continue
        words = key.split()
        if len(words) > 1 and words[-1] in DISTRICT_SUFFIX_TOKENS:
            stem = " ".join(words[:-1])
            if any(stem == other for j, other in enumerate(folded) if j != index):
                dropped = True
                continue
        kept.append(part)
    if not dropped or not kept:
        return text
    return ", ".join(kept)
