"""The admin-UI `subscriptions` namespace keys added for the Subscriptions page
must seed under category='ui_subscriptions' with bare keys, and actually
resolve through the real AdminUiTranslationService."""
import pytest

from business_app.services.admin_ui_translation_service import AdminUiTranslationService
from scripts.seed_ui_subscriptions_translations import (
    UI_SUBSCRIPTIONS_TRANSLATIONS,
    seed_ui_subscriptions_translations,
)


@pytest.mark.unit
def test_seed_ui_subscriptions_translations_resolve_via_subscriptions_namespace(app, db):
    with app.app_context():
        seed_ui_subscriptions_translations()

        en = AdminUiTranslationService.get_translations("en", "subscriptions")
        # en seed values must equal the Subscriptions.js inline defaultValue fallbacks
        assert en["create_button"] == "Create Subscription"
        assert en["section_overrides"] == "Danger zone / overrides"
        assert en["override_warning"] == (
            "Manual overrides can break automated billing. Use with care."
        )
        assert en["updated"] == "Subscription updated"

        uz = AdminUiTranslationService.get_translations("uz", "subscriptions")
        assert uz["create_button"] == UI_SUBSCRIPTIONS_TRANSLATIONS["uz"]["create_button"]
        assert uz["section_overrides"] == UI_SUBSCRIPTIONS_TRANSLATIONS["uz"]["section_overrides"]
        assert uz["override_warning"] == UI_SUBSCRIPTIONS_TRANSLATIONS["uz"]["override_warning"]
        assert uz["updated"] == "Obuna yangilandi"

        ru = AdminUiTranslationService.get_translations("ru", "subscriptions")
        assert ru["create_button"] == UI_SUBSCRIPTIONS_TRANSLATIONS["ru"]["create_button"]
        assert ru["section_overrides"] == UI_SUBSCRIPTIONS_TRANSLATIONS["ru"]["section_overrides"]
        assert ru["override_warning"] == UI_SUBSCRIPTIONS_TRANSLATIONS["ru"]["override_warning"]
        # Confirms the typo fix (brief draft had "обновлendi").
        assert ru["updated"] == "Подписка обновлена"
