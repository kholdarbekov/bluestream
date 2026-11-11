"""Guards for scripts/seed_staff_shared_place_translations.py (mirrors the over-returned seed's)."""
import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_SCRIPT = "seed_staff_shared_place_translations"

SHARED_PLACE_KEYS = {
    "staff.delivery.shared_place.question",
    "staff.delivery.shared_place.candidate",
    "staff.delivery.shared_place.none",
    "staff.delivery.shared_place.skip",
    "staff.delivery.shared_place.picked",
    "staff.delivery.shared_place.note_prompt",
    "staff.delivery.shared_place.no_candidates",
    "staff.delivery.shared_place.thanks",
    "staff.delivery.shared_place.skipped",
    "staff.delivery.shared_place.note_too_long",
}

EXPECTED_PLACEHOLDERS = {
    "staff.delivery.shared_place.question": {"name"},
    "staff.delivery.shared_place.candidate": {"name", "empties", "metres"},
    "staff.delivery.shared_place.none": set(),
    "staff.delivery.shared_place.skip": set(),
    "staff.delivery.shared_place.picked": {"choice"},
    "staff.delivery.shared_place.note_prompt": set(),
    "staff.delivery.shared_place.no_candidates": {"name"},
    "staff.delivery.shared_place.thanks": set(),
    "staff.delivery.shared_place.skipped": set(),
    "staff.delivery.shared_place.note_too_long": {"max"},
}


def _load(name):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _placeholders(value):
    return set(re.findall(r"\{([a-z_]+)\}", value))


@pytest.mark.unit
def test_seed_script_side_effect_is_behind_a_main_guard():
    src = (REPO_ROOT / "scripts" / f"{SEED_SCRIPT}.py").read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in src
    body = src.split('if __name__ == "__main__":')[0]
    assert "run()" not in body.replace("def run()", "")


@pytest.mark.unit
def test_seed_script_ships_every_key_trilingually():
    mod = _load(SEED_SCRIPT)
    assert mod.CATEGORY == "staff_bot"
    assert set(mod.KEYS) == SHARED_PLACE_KEYS
    for key, langs in mod.KEYS.items():
        assert set(langs) == {"en", "uz", "ru"}, key
        for lang, value in langs.items():
            assert value.strip(), f"{key}:{lang}"


@pytest.mark.unit
def test_seed_script_placeholders_are_identical_in_every_language():
    mod = _load(SEED_SCRIPT)
    for key, langs in mod.KEYS.items():
        for lang, value in langs.items():
            assert _placeholders(value) == EXPECTED_PLACEHOLDERS[key], f"{key}:{lang}"


@pytest.mark.unit
def test_seed_script_keys_all_have_call_sites():
    """Mirrors `StaffI18n._extract_literal_staff_keys`: a seeded key nobody reads is dead weight."""
    mod = _load(SEED_SCRIPT)
    pattern = re.compile(r"""i18n\.get\(\s*(['"])(staff\.[^'"]+)\1\s*[,)]""")
    used = set()
    for path in (REPO_ROOT / "staff_bot").rglob("*.py"):
        for _, key in pattern.findall(path.read_text(encoding="utf-8")):
            used.add(key)
    assert set(mod.KEYS) <= used, f"seeded but never read: {sorted(set(mod.KEYS) - used)}"


@pytest.mark.unit
def test_seed_script_owns_these_keys_alone():
    mod = _load(SEED_SCRIPT)
    main_src = (REPO_ROOT / "scripts" / "seed_staff_translations.py").read_text(encoding="utf-8")
    for key in mod.KEYS:
        suffix = key.split(".", 2)[2]
        assert f'"{suffix}"' not in main_src, f"{key} already curated in seed_staff_translations"
        assert f"'{suffix}'" not in main_src, f"{key} already curated in seed_staff_translations"
        assert f'"{key}"' not in main_src

    for other in ("seed_staff_over_returned_translations", "seed_place_group_staff_translations"):
        assert not (set(mod.KEYS) & set(_load(other).KEYS)), other
