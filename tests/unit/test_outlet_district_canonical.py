"""`_canonical_district`: the one place free-text district names become canonical keys.

Why this file exists: the staff bot's "New outlet" pin step forwards whatever
`POST /api/v1/addresses/reverse-geocode` said, VERBATIM. A geocoder does not
answer with `"chilanzar"` — it answers with `"Chilanzar District"`,
`"Chilonzor tumani"` or `"Чиланзарский район"`, and `_validate_common` used to
compare that string against `TASHKENT_DISTRICTS` keys and raise
`SALES_DISTRICT_INVALID`. The whole walk-in then failed at the final Confirm
tap, on a field the sales agent never typed and cannot correct. (It no longer
refuses at all — see `_validate_common`: an unresolvable hint is stored as NULL.
This function is still what decides whether there is anything to store.)

The strings below are not invented. They are what live Nominatim answers for
Tashkent pins under the configured provider (`MAPS_PROVIDER=osm`) — including
its apostrophe (U+2018, not the U+0027 our own constants use) and its spelling
of Shaykhontohur.

This is the outlet WRITE PATH's canonicaliser. It is not the only name→key
matcher in the tree — `admin_ui/src/pages/Users.js`, `Tryouts.js` and
`business_app/static/js/address-wizard.js` each carry their own — but the bot
deliberately has none: it posts the raw string
(tests/staff_bot/test_sales_new_outlet_journey.py asserts exactly that) and this
is what maps it.
"""
import pytest

from business_app.services.sales.outlet_service import _canonical_district, _district_display_names
from shared.constants import TASHKENT_DISTRICTS

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "raw,expected",
    [
        # What already worked, kept honest: the key itself and every display name.
        ("chilanzar", "chilanzar"),
        ("Chilanzar", "chilanzar"),
        ("Chilonzor", "chilanzar"),
        ("Чиланзар", "chilanzar"),
        # What a geocoder actually returns.
        ("Chilanzar District", "chilanzar"),
        ("chilanzar district", "chilanzar"),
        ("Chilonzor tumani", "chilanzar"),
        ("Чиланзарский район", "chilanzar"),
        ("Yunusabad District", "yunusabad"),
        ("Yunusobod tumani", "yunusabad"),
        ("Юнусабадский район", "yunusabad"),
        ("Yakkasaray Rayon", "yakkasaray"),
        # A two-word district survives the strip.
        ("Mirzo Ulugbek District", "mirzo_ulugbek"),
        ("Мирзо Улугбек район", "mirzo_ulugbek"),
        # --- Captured from LIVE Nominatim, which is what production reads. ---
        # U+2018 LEFT SINGLE QUOTATION MARK, where our own constant uses U+0027.
        ("Mirzo Ulug\u2018bek Tumani", "mirzo_ulugbek"),
        ("Mirzo Ulug'bek Tumani", "mirzo_ulugbek"),
        ("Mirzo Ulug\u02bbbek Tumani", "mirzo_ulugbek"),
        # The provider drops the k/x we spell it with -> `_DISTRICT_ALIASES`.
        ("Shayhontohur Tumani", "shaykhontohur"),
        ("Shaykhontohur Tumani", "shaykhontohur"),
        ("Shayxontohur Tumani", "shaykhontohur"),
        ("Yashnobod Tumani", "yashnobod"),
        ("Uchtepa Tumani", "uchtepa"),
        ("Yunusobod Tumani", "yunusabad"),
    ],
)
def test_decorated_district_names_resolve_to_their_key(raw, expected):
    assert _canonical_district(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "Nowhere",
        "Nowhere District",
        # The prefix rule must be reachable ONLY behind a suffix strip, so an
        # ordinary address line that merely STARTS with a district name is not
        # a district. Otherwise "Mirobod street" would silently become the
        # Mirobod district on every outlet created from a typed address.
        "Mirobod street",
        "Chilanzar 5-kvartal, 12",
        # A suffix token on its own names no district.
        "district",
        "район",
    ],
)
def test_text_that_names_no_district_resolves_to_none(raw):
    assert _canonical_district(raw) is None


# The ru adjectival forms split cleanly, and the boundary is worth stating
# rather than discovering: a name ending in a CONSONANT keeps its whole self in
# the adjective ("Чиланзар" → "Чиланзарский"), so the prefix rule reaches it. A
# name ending in "-а" loses that vowel ("Учтепа" → "Учтепинский", "Хамза" →
# "Хамзинский"), so it does not.
@pytest.mark.parametrize("raw", ["Чиланзарский район", "Юнусабадский район", "Яшнабадский район"])
def test_russian_adjectival_forms_of_consonant_names_still_resolve(raw):
    assert _canonical_district(raw) is not None


@pytest.mark.parametrize("raw", ["Учтепинский район", "Хамзинский район"])
def test_russian_adjectival_stems_that_are_not_prefixes_are_a_KNOWN_GAP(raw):
    """A documented gap, pinned so it stays a decision rather than a surprise.

    The fix would be fuzzy matching, and we are not doing that: a district
    guessed WRONG files an outlet under someone else's territory and misroutes
    bulk assignment, while None stores NULL and an admin picks the district in
    the Outlets drawer. If these ever need to resolve, add them to
    `_DISTRICT_ALIASES` — an explicit decision, on evidence, one string at a
    time.

    Reachable only for a ru-locale geocode: the configured provider answers
    Tashkent districts in Latin ("Yashnobod Tumani").
    """
    assert _canonical_district(raw) is None


def test_no_district_name_is_a_prefix_of_another():
    """The invariant the prefix rule rests on, asserted rather than assumed.

    `_canonical_district` falls back to `remainder.startswith(name)` after
    stripping a locale suffix, which is what lets the Russian adjectival form
    ("чиланзарский") reach "чиланзар". That is only safe while no district's
    display name is a prefix of another's — the day one is, this fails here
    instead of quietly filing outlets under the wrong district.
    """
    names = sorted(set(_district_display_names()) | set(TASHKENT_DISTRICTS))
    collisions = [
        (short, long) for short in names for long in names if short != long and long.startswith(short)
    ]
    assert collisions == []


def test_every_canonical_key_round_trips_through_every_language():
    """Each district, in each language, decorated and bare, lands on its own key."""
    for key, names in TASHKENT_DISTRICTS.items():
        assert _canonical_district(key) == key
        for language, suffix in (("en", "District"), ("uz", "Tumani"), ("ru", "район")):
            display = names.get(language)
            if not display:
                continue
            assert _canonical_district(display) == key, (language, display)
            assert _canonical_district(f"{display} {suffix}") == key, (language, display)


@pytest.mark.parametrize("apostrophe", ["'", "\u2019", "\u2018", "\u02bb", "\u02bc", "`"])
def test_every_apostrophe_variant_folds_to_the_same_key(apostrophe):
    """Nobody agrees which codepoint an Uzbek apostrophe is.

    `shared/constants.py` writes "Mirzo Ulug'bek" with U+0027, live Nominatim
    answers U+2018, and the orthographically correct character is U+02BB. The
    match is about the letters, so all of them fold away — on both sides, which
    is why the uz display name itself still resolves.
    """
    assert _canonical_district(f"Mirzo Ulug{apostrophe}bek Tumani") == "mirzo_ulugbek"


def test_the_alias_table_points_at_real_district_keys():
    """An alias whose target is not a district key is a silent write of garbage."""
    from business_app.services.sales.outlet_service import _DISTRICT_ALIASES

    unknown = {alias: key for alias, key in _DISTRICT_ALIASES.items() if key not in TASHKENT_DISTRICTS}
    assert unknown == {}
