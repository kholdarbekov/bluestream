"""The Phase 3 route-card keys must exist in the CURATED catalog in all three
languages with matching placeholder sets. Loaded by path like the existing
seed-script regression tests (scripts/ is not an importable package)."""

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SEED_SCRIPT = ROOT / "scripts" / "seed_staff_translations.py"

ROUTE_KEYS = [
    "staff.route.suggested_next",
    "staff.route.current_stop",
    "staff.route.card_header",
    "staff.route.finish_by",
    "staff.route.updated_at",
    "staff.route.all_stops_header",
    "staff.route.all_stops_button",
    "staff.route.start_this_stop",
    "staff.route.open_stop",
    "staff.route.navigate_all",
    "staff.route.head_changed_alert",
    "staff.route.open_route_card",
    "staff.route.all_done",
    "staff.route.diversion_offer",
    "staff.route.go_here_first",
    "staff.route.keep_current",
]


def _load_seed_script():
    spec = importlib.util.spec_from_file_location("seed_staff_translations", SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_route_keys_curated_in_three_languages():
    module = _load_seed_script()
    for key in ROUTE_KEYS:
        assert key in module.STAFF_TRANSLATIONS, f"missing curated key {key}"
        entry = module.STAFF_TRANSLATIONS[key]
        for lang in ("en", "uz", "ru"):
            assert entry.get(lang), f"{key} missing {lang}"


@pytest.mark.unit
def test_route_keys_are_not_english_left_in_a_translated_slot():
    """A present-but-untranslated slot is invisible to the completeness test
    above: `entry.get(lang)` is truthy whether the value is real Uzbek or the
    English string pasted twice. Drivers read UZ/RU, so an English duplicate
    ships broken copy to exactly the people who need the translation."""
    module = _load_seed_script()
    for key in ROUTE_KEYS:
        entry = module.STAFF_TRANSLATIONS[key]
        for lang in ("uz", "ru"):
            assert entry[lang] != entry["en"], f"{key}: {lang} is the English string, not a translation"


@pytest.mark.unit
def test_route_key_placeholders_match_across_languages():
    """A dropped {placeholder} in one language crashes str.format at send
    time for exactly that language — the classic silent trilingual bug."""
    module = _load_seed_script()
    for key in ROUTE_KEYS:
        entry = module.STAFF_TRANSLATIONS[key]
        placeholder_sets = {
            lang: set(re.findall(r"\{(\w+)\}", entry[lang])) for lang in ("en", "uz", "ru")
        }
        assert placeholder_sets["en"] == placeholder_sets["uz"] == placeholder_sets["ru"], (
            f"{key} placeholder mismatch: {placeholder_sets}"
        )


@pytest.mark.unit
def test_suggested_next_is_not_an_instruction():
    """Spec §6.3: the copy must say SUGGESTED, never read as 'next stop' the
    driver is ordered to take."""
    module = _load_seed_script()
    assert "SUGGESTED" in module.STAFF_TRANSLATIONS["staff.route.suggested_next"]["en"].upper()
