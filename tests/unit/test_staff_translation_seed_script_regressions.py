"""Static regressions for staff translation seed script behavior."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SEED_SCRIPT = ROOT / "scripts" / "seed_staff_translations.py"


def test_seed_script_adds_curated_keys():
    """
    Seed script must include curated key catalogs, not only source scanning.

    This prevents partial seeding when staff_bot source directory is unavailable
    in the runtime container.
    """
    text = SEED_SCRIPT.read_text(encoding="utf-8")
    assert "def _add_curated_keys(keys: Set[str]) -> None:" in text
    assert "keys.update(STAFF_TRANSLATIONS.keys())" in text
    assert "keys.update(EXTRA_TRANSLATIONS.keys())" in text
    assert "for suffix in DELIVERY_TEXT_TRANSLATIONS.keys()" in text
    assert "for suffix in OPERATOR_TEXT_TRANSLATIONS.keys()" in text


def test_seed_script_main_uses_curated_keys():
    """Main seeding flow must include curated keys before DB upsert."""
    text = SEED_SCRIPT.read_text(encoding="utf-8")
    assert "_add_dynamic_keys(keys)" in text
    assert "_add_curated_keys(keys)" in text
