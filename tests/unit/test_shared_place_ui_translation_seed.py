"""Guards for scripts/seed_shared_place_ui_translations.py, the Driver reports tab's admin copy.

The twin of ``test_ui_seed_english_matches_the_jsx_fallbacks_exactly`` in
test_place_group_translation_seeds.py, which checks only the place-group seed: these
tests read the seed as data and the admin UI's inline fallbacks through the SAME
extractor, so the seeded English can never differ from what the page renders before
the seed lands. Nothing here opens an app context: the seed's side effect is behind
its ``__main__`` guard, which a test below asserts, because ``exec_module`` would
otherwise run it against the dev database.
"""

import re

import pytest

from tests.unit.test_place_group_translation_seeds import (
    REPO_ROOT,
    _extract_ui_fallbacks,
    _i18next_placeholders,
    _load,
    _source,
)

SEED = "seed_shared_place_ui_translations"
PREFIX = "ui.users.shared_place_reports."
LANGUAGES = {"en", "uz", "ru"}

# Every admin-UI file that renders this tab's copy (the extractor's tuple lists them too).
CALL_SITE_FILES = (
    "src/pages/Users.js",
    "src/components/PlaceGroupConfirmModal.jsx",
    # The place note, shown read-only on the card.
    "src/components/PlaceGroupCard.jsx",
    "src/components/sharedPlace/SharedPlaceReportsPanel.jsx",
    "src/components/sharedPlace/SharedPlaceCaseDetail.jsx",
    "src/components/sharedPlace/SharedPlaceAdjustConfirm.jsx",
)


def _used_keys():
    pattern = re.compile(r"""['"](ui\.users\.shared_place_reports\.[A-Za-z0-9_]+)['"]""")
    used = set()
    for rel in CALL_SITE_FILES:
        used |= set(pattern.findall((REPO_ROOT / "admin_ui" / rel).read_text(encoding="utf-8")))
    return used


@pytest.mark.unit
def test_seed_side_effect_is_behind_a_main_guard():
    src = _source(SEED)
    assert 'if __name__ == "__main__":' in src
    body = src.split('if __name__ == "__main__":')[0]
    assert "run()" not in body.replace("def run()", "")


@pytest.mark.unit
def test_seed_claims_only_this_tab_keys_in_the_ui_category_trilingually():
    mod = _load(SEED)
    assert mod.CATEGORY == "ui"
    assert mod.KEYS, "the seed is empty"
    for key, langs in mod.KEYS.items():
        assert key.startswith(PREFIX), key
        assert set(langs) == LANGUAGES, key
        for lang, value in langs.items():
            assert value.strip(), f"{key}:{lang}"


@pytest.mark.unit
def test_seed_english_matches_the_jsx_fallbacks_exactly():
    """A mismatch means the page changes wording the moment the seed lands."""
    mod = _load(SEED)
    fallbacks = _extract_ui_fallbacks()
    missing = sorted(key for key in mod.KEYS if key not in fallbacks)
    assert not missing, f"seeded keys with no admin-UI call site: {missing}"
    for key, langs in mod.KEYS.items():
        assert langs["en"] == fallbacks[key], key


@pytest.mark.unit
def test_every_key_the_tab_renders_is_seeded():
    """The other direction: a call site with no seed row renders English to a uz/ru admin."""
    mod = _load(SEED)
    used = _used_keys()
    assert used, "no ui.users.shared_place_reports.* call site found — the components moved?"
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
def test_no_other_seed_claims_these_keys():
    """One key, one owning script: a later seed re-assigns the row's category."""
    mod = _load(SEED)
    for other in ("seed_place_group_ui_translations", "seed_customer_link_translations"):
        assert not (set(mod.KEYS) & set(_load(other).KEYS)), other
    for script in (REPO_ROOT / "scripts").glob("*.py"):
        if script.stem == SEED:
            continue
        assert PREFIX not in script.read_text(encoding="utf-8"), f"{script.name} seeds {PREFIX}*"
