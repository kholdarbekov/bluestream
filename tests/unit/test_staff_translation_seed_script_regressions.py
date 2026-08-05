"""Static regressions for staff translation seed script behavior."""

import importlib.util
from pathlib import Path

import pytest

from business_app.models.translation import Translation


ROOT = Path(__file__).resolve().parents[2]
SEED_SCRIPT = ROOT / "scripts" / "seed_staff_translations.py"

# The eight place-group strings owned by scripts/seed_place_group_staff_translations.py.
PLACE_UNION_KEY = "staff.delivery.fine_place_union_hint"
PLACE_COD_COUNT_KEY = "staff.operator.cod_restricted_place"
PLACE_GROUP_VALUES = {
    PLACE_UNION_KEY: {
        "en": "Bottles at this place across all members: {union}",
        "uz": "Ushbu manzilda barcha a'zolar bo'yicha idishlar: {union}",
        "ru": "Тара по этому адресу по всем участникам: {union}",
    },
    PLACE_COD_COUNT_KEY: {
        "en": "Cash on delivery is unavailable: {place_active_cod_debt_count} outstanding COD debts.",
        "uz": "Naqd to'lash mavjud emas: {place_active_cod_debt_count} ta to'lanmagan qarz.",
        "ru": "Оплата наличными недоступна: {place_active_cod_debt_count} непогашенных задолженностей.",
    },
}


def _load_seed_script():
    spec = importlib.util.spec_from_file_location("seed_staff_translations", SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


@pytest.mark.unit
def test_place_group_keys_are_collected_by_the_source_scan():
    """The collision is real: the source scan DOES pick these keys up.

    ``_extract_literal_keys`` rglobs ``staff_bot/**/*.py`` for ``i18n.get('staff.…')``
    and the place-group handlers call these literally, so they enter this
    script's key set even though only seed_place_group_staff_translations.py
    knows their values. Without the absent-only guard below that is exactly what
    makes the destruction happen.
    """
    module = _load_seed_script()
    keys = module.collect_keys(ROOT)
    assert PLACE_UNION_KEY in keys
    assert PLACE_COD_COUNT_KEY in keys
    # …and this script has no value for them.
    for key in (PLACE_UNION_KEY, PLACE_COD_COUNT_KEY):
        for lang in ("en", "uz", "ru"):
            assert module._curated_value(key, lang) is None, f"{key}:{lang}"


@pytest.mark.unit
def test_seed_run_does_not_destroy_place_group_translations(db):
    """A full seed run must leave another seed's rows byte-identical.

    Regression: the writer used to do ``existing.value = _resolve_value(...)``
    unconditionally, and ``_resolve_value`` humanises anything uncurated. That
    turned "Bottles at this place across all members: {union}" into "Fine place
    union hint" in all three languages — placeholder gone, so the driver saw a
    label with no number — while staff_bot's /health stayed green because the
    row still existed.
    """
    module = _load_seed_script()

    for key, langs in PLACE_GROUP_VALUES.items():
        for lang, value in langs.items():
            db.session.add(
                Translation(key=key, language=lang, value=value, category="staff_bot", is_active=True)
            )
    db.session.commit()

    stats = module.seed_translations(module.collect_keys(ROOT))
    assert stats["skipped"] > 0, "uncurated existing rows must be skipped, not rewritten"

    for key, langs in PLACE_GROUP_VALUES.items():
        for lang, value in langs.items():
            row = Translation.query.filter_by(key=key, language=lang).first()
            assert row is not None, f"{key}:{lang} was deleted"
            assert row.value == value, f"{key}:{lang} was overwritten with {row.value!r}"


@pytest.mark.unit
def test_seed_run_still_writes_curated_keys(db):
    """The guard is absent-only for UNCURATED keys — curated ones still upsert."""
    module = _load_seed_script()
    key = "staff.menu.title"

    db.session.add(
        Translation(key=key, language="en", value="STALE", category="general", is_active=False)
    )
    db.session.commit()

    module.seed_translations({key})

    row = Translation.query.filter_by(key=key, language="en").first()
    assert row.value == module.STAFF_TRANSLATIONS[key]["en"]
    assert row.category == "staff_bot"
    assert row.is_active is True
