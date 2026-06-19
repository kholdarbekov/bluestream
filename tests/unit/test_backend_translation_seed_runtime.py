"""Runtime regressions for backend translation seed helpers."""

from scripts.seed_backend_translations import _category_for, _resolve_seed_value, _ui_tr


def test_category_for_dotted_key_uses_first_segment():
    assert _category_for("ui.loyalty.export_members") == "ui"


def test_category_for_dotless_key_defaults_to_general_and_fits_column():
    """English-literal storefront keys (looked up by the literal text via the
    `t` filter) have no dot. main() used key.split('.')[0], so the category became
    the whole 70-char string and overflowed the varchar(50) column — aborting the
    entire reseed transaction (nothing committed)."""
    key = "Earn AquaCoins with every purchase, build streaks, and unlock exclusive rewards"

    category = _category_for(key)

    assert category == "general"
    assert len(category) <= 50


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
