"""District-name text primitives shared by every place that compares geocoder district text.

One definition of "what is a locale suffix on a district name" and "which apostrophes fold away",
so the outlet district matcher and the geocoded-address shortener cannot drift apart.
"""

from typing import Dict, Optional

from shared.constants import TASHKENT_DISTRICTS

# Locale words a geocoder appends to a district NAME: "Chilanzar District",
# "Chilonzor Tumani", "Чиланзарский район". Stripped before the name match is
# retried, never instead of it.
DISTRICT_SUFFIX_TOKENS = ("district", "tumani", "район", "rayon")

# Every apostrophe a name can be spelled with. Uzbek needs one -- "Ulug'bek",
# "Shayxontohur" -- and nobody agrees which codepoint it is: our own constants
# use U+2018 (shared/constants.py), live Nominatim answers it with U+2018 too, and
# the Uzbek orthographic character is U+02BB. Folded away on BOTH sides so the
# match is about the letters, never about which quote mark a provider chose.
APOSTROPHES = "'’‘ʻʼ`"


def fold_district(raw: Optional[str]) -> str:
    """Lowercase and drop every apostrophe variant. The comparison form."""
    text = (raw or "").strip().lower()
    for apostrophe in APOSTROPHES:
        text = text.replace(apostrophe, "")
    return text.strip()


# Provider spellings that survive folding and still name no district. Kept as a
# tiny explicit table rather than fuzzy matching: a wrong district is worse than
# no district, and an entry here is a decision someone made on real evidence.
_DISTRICT_ALIASES = {
    # Live Nominatim, e.g. 41.32/69.24 -> county "Shayhontohur Tumani". Ours are
    # "Shaykhontohur" (en) and "Shayxontohur" (uz); the provider drops the k/x.
    "shayhontohur": "shaykhontohur",
}


def district_display_names() -> Dict[str, str]:
    """Every en/uz/ru display name, in comparison form, mapped to its canonical key."""
    return {
        fold_district(names.get(language)): key
        for key, names in TASHKENT_DISTRICTS.items()
        for language in ("en", "uz", "ru")
        if names.get(language)
    }


def canonical_district(raw: Optional[str]) -> Optional[str]:
    """Map free text onto a TASHKENT_DISTRICTS key, or None when nothing matches.

    `addresses.district` is free text, but every outlet write path (`_validate_common`,
    `bulk_assign_by_district`, `SalesAgentProfile.districts`) speaks canonical KEYS. Copying an
    address's district verbatim into an imported outlet therefore mints rows carrying "Chilonzor"
    or "Алмазар" that no district filter and no bulk assignment can ever reach. The inverse of
    `shared.constants.get_district_name`: accepts the key or any of its en/uz/ru display names.

    It also has to accept what a GEOCODER says, because `POST /addresses/reverse-geocode` is what
    the staff bot's "New outlet" pin step forwards: live Nominatim answers "Yashnobod Tumani",
    "Mirzo Ulug‘bek Tumani", "Shayhontohur Tumani". So the text is FOLDED (lowercased, every
    apostrophe variant removed), a trailing locale token is stripped, and the remainder matched --
    exactly, then through `_DISTRICT_ALIASES`, then as a PREFIX, because Russian renders the name
    adjectivally ("чиланзарский" for "чиланзар").

    The prefix rule is reachable ONLY after a suffix strip, so ordinary free text ("Mirobod
    street") still resolves to None, and no two folded district names are prefixes of one another
    (asserted in tests/unit/test_outlet_district_canonical.py), so it cannot resolve one district
    onto another. There is deliberately no fuzzy matching: the Russian adjectival stems that are
    not simple prefixes ("Учтепинский" for "Учтепа") return None, and the caller stores NULL,
    because a district guessed wrong is worse than a district left blank.

    The single canonicaliser: the outlet write paths (`outlet_service`) and `POST /addresses/reverse-geocode`
    (its `district_key`, which the website's address wizard selects) both call it. It is NOT the only name->key matcher
    in the codebase -- `admin_ui/src/pages/Users.js` and `Tryouts.js` each carry their own
    `includes`-based matching for their own pickers. Consolidating those is parked, not done. (The
    website's address wizard matches no names: it uses the `district_key` reverse-geocode publishes.)

    Pure: no session, no config, no I/O.
    """
    text = fold_district(raw)
    if not text:
        return None
    if text in TASHKENT_DISTRICTS:
        return text
    display = district_display_names()
    if text in display:
        return display[text]
    if text in _DISTRICT_ALIASES:
        return _DISTRICT_ALIASES[text]

    parts = text.split()
    if len(parts) > 1 and parts[-1] in DISTRICT_SUFFIX_TOKENS:
        stripped = " ".join(parts[:-1])
        if stripped in TASHKENT_DISTRICTS:
            return stripped
        if stripped in display:
            return display[stripped]
        if stripped in _DISTRICT_ALIASES:
            return _DISTRICT_ALIASES[stripped]
        for name, key in display.items():
            if stripped.startswith(name):
                return key
    return None
