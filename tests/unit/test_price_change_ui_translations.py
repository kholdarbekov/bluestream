"""Every string the price-change admin UI renders is seeded trilingual, as the page says it (plan Task 53).

Every admin-UI call passes an inline English fallback, so an unseeded key degrades silently to English for a uz or ru
admin and nothing fails. The owner is `ADMIN_UI_PRICE_CHANGE_TRANSLATIONS` in scripts/seed_backend_translations.py:
one dict for Tasks 53-57, whose later tasks append their files to `UI_FILES`. The seeded `en` is the call site's
fallback byte for byte; the placeholders are the same in the three languages; no copy types a price or a currency
word (prices come from the server's fields); every uz and ru line is marked for a native read.

Data only: nothing here opens an app context or touches a database.
"""

import re
from collections import defaultdict
from pathlib import Path

import pytest

from scripts.seed_backend_translations import ADMIN_UI_PRICE_CHANGE_TRANSLATIONS as SEEDED
from tests.unit.test_lapsed_customers_ui_contract import _declared_key_set, _declared_list  # noqa: F401
from tests.unit.test_place_group_translation_seeds import _JS_PAIR, _JS_STRING, _i18next_placeholders, _unquote_js

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_SCRIPT = REPO_ROOT / "scripts" / "seed_backend_translations.py"

UI_FILES = (
    "src/pages/PriceChanges.js",
    "src/components/priceChange/PriceChangeDrawer.jsx",
    "src/components/priceChange/PriceChangeFormModal.jsx",
    "src/components/layout/AdminLayout.js",
    "src/components/priceChange/PriceCourtesyStatus.jsx",
    "src/components/priceChange/PriceCourtesyPanel.jsx",
    "src/components/orders/OrderQuotePanel.jsx",
    "src/pages/Products.js",
)
OWNED_PREFIXES = ("ui.nav.price_changes", "ui.price_changes.", "ui.price_courtesy.", "ui.order_quote.")
LANGUAGES = ("en", "uz", "ru")

_KEY_LITERAL = re.compile(r"""\bt\(\s*(['"])(ui\.(?:nav\.price_changes|price_changes\.[\w.]+|price_courtesy\.[\w.]+|order_quote\.[\w.]+))\1""")
_PRICE_OR_CURRENCY = re.compile(r"\d{3}|UZS|so'm|so‘m|сум", re.IGNORECASE)


def _ui_sources():
    return [(REPO_ROOT / "admin_ui" / rel).read_text(encoding="utf-8") for rel in UI_FILES]


def _used_keys():
    return {key for text in _ui_sources() for _, key in _KEY_LITERAL.findall(text)}


def _fallbacks():
    """key -> every inline English fallback the owned call sites pass for it. A set, so drift shows up."""
    found = defaultdict(set)
    for text in _ui_sources():
        for _, key, expr in _JS_PAIR.findall(text):
            if key.startswith(OWNED_PREFIXES):
                found[key].add("".join(_unquote_js(lit) for lit in re.findall(_JS_STRING, expr, re.S)))
    return found


@pytest.mark.unit
def test_every_key_the_ui_renders_is_seeded_and_nothing_else():
    used = _used_keys()

    assert used, "no price-change key literal found: the scan is blind"
    assert used == set(SEEDED), (sorted(used - set(SEEDED)), sorted(set(SEEDED) - used))


@pytest.mark.unit
@pytest.mark.parametrize("key", sorted(SEEDED))
def test_key_is_seeded_in_three_languages_under_the_shared_ui_category(key):
    row = SEEDED[key]
    for lang in LANGUAGES:
        assert isinstance(row[lang], str) and row[lang].strip(), (key, lang)
    assert key.split(".")[0] == "ui"  # the seed's `_category_for` files the row under the shared category
    assert key.startswith(OWNED_PREFIXES), key


@pytest.mark.unit
@pytest.mark.parametrize("key", sorted(SEEDED))
def test_seeded_english_is_the_call_sites_fallback(key):
    assert _fallbacks()[key] == {SEEDED[key]["en"]}, key


@pytest.mark.unit
@pytest.mark.parametrize("key", sorted(SEEDED))
def test_translations_keep_every_interpolation_token(key):
    row = SEEDED[key]
    tokens = _i18next_placeholders(row["en"])
    assert _i18next_placeholders(row["uz"]) == tokens, key
    assert _i18next_placeholders(row["ru"]) == tokens, key


@pytest.mark.unit
@pytest.mark.parametrize("key", sorted(SEEDED))
def test_no_copy_types_a_price_or_a_currency_word(key):
    for lang in LANGUAGES:
        assert not _PRICE_OR_CURRENCY.search(SEEDED[key][lang]), (key, lang)


@pytest.mark.unit
def test_uz_and_ru_rows_are_marked_native_read():
    source = SEED_SCRIPT.read_text(encoding="utf-8")
    block = source.split("ADMIN_UI_PRICE_CHANGE_TRANSLATIONS = {", 1)[1].split(
        "BACKEND_TRANSLATIONS.update(ADMIN_UI_PRICE_CHANGE_TRANSLATIONS)", 1
    )[0]

    marked = re.findall(
        r"^    '(ui\.[\w.]+)': _ui_tr\(\n        .+,\n        .+,  # NATIVE-READ\n        .+,  # NATIVE-READ\n    \),$",
        block,
        re.MULTILINE,
    )

    assert sorted(marked) == sorted(SEEDED), sorted(set(SEEDED) - set(marked))
