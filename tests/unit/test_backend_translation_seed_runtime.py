"""Runtime regressions for backend translation seed helpers."""

from scripts.seed_backend_translations import _resolve_seed_value, _ui_tr


def test_ui_catalog_fallback_preserves_existing_localized_rows():
    row = _ui_tr("Archive")

    value, preserve_existing = _resolve_seed_value(row, "uz")

    assert value == "Archive"
    assert preserve_existing is True


def test_ui_catalog_uses_explicit_localized_value_when_present():
    row = _ui_tr("Archive", "Arxivlash", "Arxiv")

    value, preserve_existing = _resolve_seed_value(row, "uz")

    assert value == "Arxivlash"
    assert preserve_existing is False


def test_english_seed_value_is_always_authoritative():
    row = _ui_tr("Archive")

    value, preserve_existing = _resolve_seed_value(row, "en")

    assert value == "Archive"
    assert preserve_existing is False
