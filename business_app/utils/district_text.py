"""District-name text primitives shared by every place that compares geocoder district text.

One definition of "what is a locale suffix on a district name" and "which apostrophes fold away",
so the outlet district matcher and the geocoded-address shortener cannot drift apart.
"""

from typing import Optional

# Locale words a geocoder appends to a district NAME: "Chilanzar District",
# "Chilonzor Tumani", "Чиланзарский район". Stripped before the name match is
# retried, never instead of it.
DISTRICT_SUFFIX_TOKENS = ("district", "tumani", "район", "rayon")

# Every apostrophe a name can be spelled with. Uzbek needs one -- "Ulug'bek",
# "Shayxontohur" -- and nobody agrees which codepoint it is: our own constants
# use U+0027, live Nominatim answers "Mirzo Ulug‘bek Tumani" with U+2018, and
# the Uzbek orthographic character is U+02BB. Folded away on BOTH sides so the
# match is about the letters, never about which quote mark a provider chose.
APOSTROPHES = "'’‘ʻʼ`"


def fold_district(raw: Optional[str]) -> str:
    """Lowercase and drop every apostrophe variant. The comparison form."""
    text = (raw or "").strip().lower()
    for apostrophe in APOSTROPHES:
        text = text.replace(apostrophe, "")
    return text.strip()
