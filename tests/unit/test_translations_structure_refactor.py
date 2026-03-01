"""Static regression checks for translations API/service boundaries."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TRANSLATIONS_API_FILE = ROOT / "business_app" / "api" / "translations.py"
TRANSLATION_SERVICE_FILE = ROOT / "business_app" / "services" / "admin_ui_translation_service.py"


def test_translations_api_delegates_queries_to_service_layer():
    text = TRANSLATIONS_API_FILE.read_text(encoding="utf-8")

    assert (
        "from business_app.services.admin_ui_translation_service import AdminUiTranslationService"
        in text
    )
    assert "AdminUiTranslationService.get_translations(" in text
    assert "AdminUiTranslationService.get_namespaces(" in text
    assert "from business_app.models.translation import Translation" not in text


def test_admin_ui_translation_service_exposes_expected_entrypoints():
    text = TRANSLATION_SERVICE_FILE.read_text(encoding="utf-8")

    assert "class AdminUiTranslationService:" in text
    assert "def get_translations(" in text
    assert "def get_namespaces(" in text
