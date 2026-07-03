"""The admin-UI `staff` namespace keys added for the reconciliation modal must
seed under category='ui_staff' with bare keys, and actually resolve through the
real AdminUiTranslationService (this is the first-ever ui_staff seed)."""
import pytest

from business_app.services.admin_ui_translation_service import AdminUiTranslationService
from scripts.seed_ui_staff_translations import (
    UI_STAFF_TRANSLATIONS,
    seed_ui_staff_translations,
)


@pytest.mark.unit
def test_seed_ui_staff_translations_resolve_via_staff_namespace(app, db):
    with app.app_context():
        seed_ui_staff_translations()

        en = AdminUiTranslationService.get_translations("en", "staff")
        # en seed values must equal the DeliveryReports.js inline fallbacks
        assert en["customer_phone"] == "Phone"
        assert en["allocated"] == "Allocated"
        assert en["result"] == "Result"
        assert en["fully_paid"] == "✓ Fully paid"
        assert en["partially_paid"] == "◐ Partially paid"
        assert en["reversed"] == "Reversed"

        uz = AdminUiTranslationService.get_translations("uz", "staff")
        assert uz["customer_phone"] == UI_STAFF_TRANSLATIONS["uz"]["customer_phone"]
        assert uz["fully_paid"] == UI_STAFF_TRANSLATIONS["uz"]["fully_paid"]

        ru = AdminUiTranslationService.get_translations("ru", "staff")
        assert ru["reversed"] == UI_STAFF_TRANSLATIONS["ru"]["reversed"]
        assert ru["customer_phone"] == "Телефон"
