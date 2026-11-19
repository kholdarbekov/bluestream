"""Guards for scripts/seed_driver_expense_ui_translations.py, the Driver expenses page's admin copy.

The twin of test_shared_place_ui_translation_seed.py: the seed is read as data and the admin UI's inline fallbacks
through the SAME extractor, so the seeded English can never differ from what the page renders before the seed lands.
Nothing here opens an app context: the seed's side effect is behind its ``__main__`` guard.
"""

import re

import pytest

from tests.unit.test_place_group_translation_seeds import (
    _JS_PAIR,
    _JS_STRING,
    REPO_ROOT,
    _i18next_placeholders,
    _load,
    _source,
    _unquote_js,
)

SEED = "seed_driver_expense_ui_translations"
PREFIX = "ui.driver_expenses."
NAV_KEY = "ui.nav.driver_expenses"
LANGUAGES = {"en", "uz", "ru"}

# Every admin-UI file that renders this page's copy.
CALL_SITE_FILES = (
    "src/pages/DriverExpenses.js",
    "src/components/driverExpenses/DecideExpenseModal.jsx",
    "src/components/driverExpenses/ExpenseReceiptThumb.jsx",
    "src/components/driverExpenses/expenseCopy.js",
    "src/components/layout/AdminLayout.js",
)
_KEY = re.compile(r"""['"](ui\.(?:driver_expenses\.[A-Za-z0-9_.]+|nav\.driver_expenses))['"]""")


def _texts():
    return [(REPO_ROOT / "admin_ui" / rel).read_text(encoding="utf-8") for rel in CALL_SITE_FILES]


def _used_keys():
    return {key for text in _texts() for key in _KEY.findall(text)}


def _fallbacks():
    found = {}
    for text in _texts():
        for _, key, expr in _JS_PAIR.findall(text):
            if key == NAV_KEY or key.startswith(PREFIX):
                found.setdefault(key, "".join(_unquote_js(lit) for lit in re.findall(_JS_STRING, expr, re.S)))
    return found


@pytest.mark.unit
def test_seed_side_effect_is_behind_a_main_guard():
    src = _source(SEED)
    assert 'if __name__ == "__main__":' in src
    body = src.split('if __name__ == "__main__":')[0]
    assert "run()" not in body.replace("def run()", "")


@pytest.mark.unit
def test_seed_claims_only_this_page_keys_in_the_ui_category_trilingually():
    mod = _load(SEED)
    assert mod.CATEGORY == "ui"
    assert mod.KEYS, "the seed is empty"
    for key, langs in mod.KEYS.items():
        assert key == NAV_KEY or key.startswith(PREFIX), key
        assert set(langs) == LANGUAGES, key
        for lang, value in langs.items():
            assert value.strip(), f"{key}:{lang}"


@pytest.mark.unit
def test_seed_english_matches_the_jsx_fallbacks_exactly():
    """A mismatch means the page changes wording the moment the seed lands."""
    mod = _load(SEED)
    fallbacks = _fallbacks()
    missing = sorted(key for key in mod.KEYS if key not in fallbacks)
    assert not missing, f"seeded keys with no admin-UI call site: {missing}"
    for key, langs in mod.KEYS.items():
        assert langs["en"] == fallbacks[key], key


@pytest.mark.unit
def test_every_key_the_page_renders_is_seeded_and_every_seeded_key_is_rendered():
    mod = _load(SEED)
    used = _used_keys()
    assert used, "no ui.driver_expenses.* call site found: the components moved?"
    assert not used - set(mod.KEYS), f"unseeded: {sorted(used - set(mod.KEYS))}"
    assert not set(mod.KEYS) - used, f"seeded but never read: {sorted(set(mod.KEYS) - used)}"


@pytest.mark.unit
def test_placeholders_are_identical_in_every_language():
    """A dropped ``{{n}}`` is a number the admin never sees."""
    mod = _load(SEED)
    for key, langs in mod.KEYS.items():
        expected = _i18next_placeholders(langs["en"])
        for lang, value in langs.items():
            assert _i18next_placeholders(value) == expected, f"{key}:{lang}"


@pytest.mark.unit
def test_every_uz_and_ru_line_is_marked_for_the_native_read():
    lines = _source(SEED).splitlines()
    for line in lines:
        if line.strip().startswith(('"uz":', '"ru":')):
            assert line.rstrip().endswith("# NATIVE-READ"), line


@pytest.mark.unit
def test_no_other_seed_claims_these_keys():
    """One key, one owning script: a later seed re-assigns the row's category."""
    for script in (REPO_ROOT / "scripts").glob("*.py"):
        if script.stem == SEED:
            continue
        text = script.read_text(encoding="utf-8")
        assert PREFIX not in text, f"{script.name} seeds {PREFIX}*"
        assert NAV_KEY not in text, f"{script.name} seeds {NAV_KEY}"
