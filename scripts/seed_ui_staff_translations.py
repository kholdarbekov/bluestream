"""Seed the admin-UI `staff` i18next namespace (category='ui_staff').

The admin UI serves the `staff` namespace from Translation rows with
category='ui_staff' and BARE keys (see AdminUiTranslationService). These are the
keys added for the cash-reconciliation session-detail modal. English values here
MUST match the inline t(key, 'fallback') strings in
admin_ui/src/pages/DeliveryReports.js exactly.

Run inside the business_app container (scripts/ is not mounted, so pipe it in):
    docker compose exec -T business_app python - < scripts/seed_ui_staff_translations.py
"""

from business_app import create_app
from business_app.models.translation import Translation

UI_STAFF_CATEGORY = "ui_staff"

UI_STAFF_TRANSLATIONS = {
    "en": {
        "customer_phone": "Phone",
        "allocated": "Allocated",
        "result": "Result",
        "fully_paid": "✓ Fully paid",
        "partially_paid": "◐ Partially paid",
        "reversed": "Reversed",
    },
    "uz": {
        "customer_phone": "Telefon",
        "allocated": "Taqsimlangan",
        "result": "Natija",
        "fully_paid": "✓ To'liq to'langan",
        "partially_paid": "◐ Qisman to'langan",
        "reversed": "Bekor qilingan",
    },
    "ru": {
        "customer_phone": "Телефон",
        "allocated": "Распределено",
        "result": "Результат",
        "fully_paid": "✓ Полностью оплачено",
        "partially_paid": "◐ Частично оплачено",
        "reversed": "Отменено",
    },
}


def seed_ui_staff_translations(user_id: int | None = None) -> None:
    """Upsert the ui_staff admin-UI translations (idempotent)."""
    Translation.bulk_create_or_update(
        UI_STAFF_TRANSLATIONS, category=UI_STAFF_CATEGORY, user_id=user_id
    )


def main() -> None:
    app = create_app()
    with app.app_context():
        seed_ui_staff_translations()
        total = sum(len(v) for v in UI_STAFF_TRANSLATIONS.values())
        print(f"Seeded {total} ui_staff translation rows.")


if __name__ == "__main__":
    main()
