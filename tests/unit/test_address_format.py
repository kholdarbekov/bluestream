"""shorten_geocoded_address: exact outputs for the owner's example and real prod/dev samples."""

import pytest

from business_app.utils.address_format import shorten_geocoded_address

pytestmark = pytest.mark.unit

CASES = [
    # Owner's example: postcode, country and the repeated "Yunusobod Tumani" go.
    (
        "54, Ahmad Donish ko'chasi, Yunusobod 11 mavzesi, Yunusobod, Yunusobod Tumani, Toshkent, 100000, Oʻzbekiston",
        "54, Ahmad Donish ko'chasi, Yunusobod 11 mavzesi, Yunusobod, Toshkent",
    ),
    # A district named once stays, and so does a leading place name.
    (
        "O'zbegim TJM, 44, Bobur ko'chasi, Muhandislar mahalla, Yakkasaroy Tumani, Toshkent, 100000, Oʻzbekiston",
        "O'zbegim TJM, 44, Bobur ko'chasi, Muhandislar mahalla, Yakkasaroy Tumani, Toshkent",
    ),
    (
        "5, Medgorodok g-30 ko'cha, Xislat maxallasi, Olmazor Tumani, Toshkent, 100000, Oʻzbekiston",
        "5, Medgorodok g-30 ko'cha, Xislat maxallasi, Olmazor Tumani, Toshkent",
    ),
    (
        "S. Joʻrayev koʻchasi, Sergeli tumani, Toshkent, 100209, Oʻzbekiston",
        "S. Joʻrayev koʻchasi, Sergeli tumani, Toshkent",
    ),
    # Near-duplicate neighbourhood spellings are left alone (no fuzzy matching).
    (
        "17, Sirg'ali 4 daxasi, Sergeli 4 dahasi, Sergeli tumani, Toshkent, 100000, Oʻzbekiston",
        "17, Sirg'ali 4 daxasi, Sergeli 4 dahasi, Sergeli tumani, Toshkent",
    ),
    (
        "88, Nukus ko'chasi, Mirobod mahallasi, Mirobod Tumani, Toshkent, 100000, Oʻzbekiston",
        "88, Nukus ko'chasi, Mirobod mahallasi, Mirobod Tumani, Toshkent",
    ),
    # Toshkent Viloyati is kept.
    (
        "40, Universitet ko'chasi, Salar, Qibray Tumani, Toshkent Viloyati, 111218, Oʻzbekiston",
        "40, Universitet ko'chasi, Salar, Qibray Tumani, Toshkent Viloyati",
    ),
    (
        "Oasis, 63, Oloy Bozor yo'nalishi, Oloy (C-2) dahasi, Yunusobod Tumani, Toshkent, 100000, Oʻzbekiston",
        "Oasis, 63, Oloy Bozor yo'nalishi, Oloy (C-2) dahasi, Yunusobod Tumani, Toshkent",
    ),
    (
        "116, Cho'ponota ko'chasi, Chilonzor 9 mavzesi, Chilonzor, Chilonzor Tumani, Toshkent, 100097, Oʻzbekiston",
        "116, Cho'ponota ko'chasi, Chilonzor 9 mavzesi, Chilonzor, Toshkent",
    ),
    (
        "Ramadon, Zangiota Tumani, Toshkent Viloyati, 111810, Oʻzbekiston",
        "Ramadon, Zangiota Tumani, Toshkent Viloyati",
    ),
    ("Sergeli tumani, Toshkent shahri, 100000, Oʻzbekiston", "Sergeli tumani, Toshkent shahri"),
    # Yandex-era text: country dropped, no reordering.
    (
        "Uzbekistan, Tashkent, Chilanzar District, Small Ring Road, 7, entrance 4",
        "Tashkent, Chilanzar District, Small Ring Road, 7, entrance 4",
    ),
    # Typed text with a district named once is untouched.
    ("Toshkent, Shayxontohur tumani, drive probe", "Toshkent, Shayxontohur tumani, drive probe"),
    # U+2018 apostrophe in the street does not matter; Qibray + Qibray Tumani folds.
    (
        "Оценка, 168, A.Navoiy ko‘chasi, Qibray, Qibray Tumani, Toshkent Viloyati, 111200, Oʻzbekiston",
        "Оценка, 168, A.Navoiy ko‘chasi, Qibray, Toshkent Viloyati",
    ),
    ("24, Chilonzor 26 mavzesi", "24, Chilonzor 26 mavzesi"),
    # Country spellings and a lone postcode.
    ("Tashkent, Uzbekistan", "Tashkent"),
    ("Toshkent, 100000, O'zbekiston", "Toshkent"),
    ("Ташкент, Узбекистан", "Ташкент"),
    ("Ташкент, Ўзбекистон", "Ташкент"),
    ("Ташкент, 100000, Чиланзарский район", "Ташкент, Чиланзарский район"),
    # A 6-digit part is a postcode; a house number is not.
    ("12, 1234567, Toshkent", "12, 1234567, Toshkent"),
    # District word with a matching stem in another language word list.
    ("Chilanzar, Chilanzar District, Tashkent", "Chilanzar, Tashkent"),
    # Nothing dropped: typed text is returned byte for byte (spacing, empty parts included).
    ("Chilonzor 5,kv 12", "Chilonzor 5,kv 12"),
    ("a,,b", "a,,b"),
    ("Chilonzor 5, ", "Chilonzor 5, "),
    # Everything would be dropped: keep the original, never a blank address.
    ("Oʻzbekiston", "Oʻzbekiston"),
    ("100000, Oʻzbekiston", "100000, Oʻzbekiston"),
]


@pytest.mark.parametrize("raw, expected", CASES)
def test_shortens_exactly(raw, expected):
    assert shorten_geocoded_address(raw) == expected


@pytest.mark.parametrize("raw", [None, ""])
def test_empty_passes_through_unchanged(raw):
    assert shorten_geocoded_address(raw) == raw
