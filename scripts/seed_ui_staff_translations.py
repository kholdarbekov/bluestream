"""Seed the admin-UI `staff` i18next namespace (category='ui_staff').

The admin UI serves the `staff` namespace from Translation rows with
category='ui_staff' and BARE keys (see AdminUiTranslationService). These are the
keys added for the cash-reconciliation session-detail modal. English values here
MUST match the inline t(key, 'fallback') strings in
admin_ui/src/pages/DeliveryReports.js exactly.

`scope` / `attribution` belong to the Phase-2c place-group work but live here,
not in scripts/seed_place_group_ui_translations.py: DeliveryReports.js reads
them through the `staff:` namespace (`t('staff:scope', …)`), which is served
from category 'ui_staff' with BARE keys. Seeding them as dotted `ui.*` rows
would leave both column headers permanently English.

`sales_agent` (the Phase-1 sales-agent role label) belongs here for the same
reason: StaffManagement.js reads every role as `t('staff:' + role)`.

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
        # Phase 2c: place/cluster money attribution in the session-detail modal.
        "scope": "Scope",
        "attribution": "Paid by → settles",
        # Phase-1 sales-agent role: StaffManagement renders every staff role as
        # `t('staff:' + role)` with NO inline default, so without this row the
        # roles column and the invite-role Select show the literal
        # "staff:sales_agent".
        "sales_agent": "Sales agent",
        # Cash reconciliation: gross collected vs what the driver must hand in (expected_cash_on_hand).
        "cash_gross_collected": "Collected (gross)",
        "cash_to_hand_in": "To hand in",
        # Driver cash expenses on the cash-reconciliation screens (DeliveryReports, StaffManagement).
        "driver_expenses_column": "Expenses",
        "driver_expenses_waiting": "Waiting for approval",
        "driver_expenses_section": "Expenses on this trip",
        "driver_expenses_none": "No expenses on this trip",
        "driver_expenses_block_pending": "Decide the driver's waiting expenses first",
    },
    "uz": {
        "customer_phone": "Telefon",
        "allocated": "Taqsimlangan",
        "result": "Natija",
        "fully_paid": "✓ To'liq to'langan",
        "partially_paid": "◐ Qisman to'langan",
        "reversed": "Bekor qilingan",
        "scope": "Qamrov",
        "attribution": "To'lovchi → yopiladi",
        "sales_agent": "Savdo agenti",
        "cash_gross_collected": "Yig'ilgan (umumiy)",
        "cash_to_hand_in": "Topshirilishi kerak",
        "driver_expenses_column": "Xarajatlar",  # NATIVE-READ
        "driver_expenses_waiting": "Tasdiq kutilmoqda",  # NATIVE-READ
        "driver_expenses_section": "Ushbu reysdagi xarajatlar",  # NATIVE-READ
        "driver_expenses_none": "Bu reysda xarajatlar yo'q",  # NATIVE-READ
        "driver_expenses_block_pending": "Avval haydovchining kutilayotgan xarajatlari bo'yicha qaror qiling",  # NATIVE-READ
    },
    "ru": {
        "customer_phone": "Телефон",
        "allocated": "Распределено",
        "result": "Результат",
        "fully_paid": "✓ Полностью оплачено",
        "partially_paid": "◐ Частично оплачено",
        "reversed": "Отменено",
        "scope": "Область",
        "attribution": "Платит → погашает",
        "sales_agent": "Торговый агент",
        "cash_gross_collected": "Собрано (всего)",
        "cash_to_hand_in": "К сдаче",
        "driver_expenses_column": "Расходы",  # NATIVE-READ
        "driver_expenses_waiting": "Ожидают утверждения",  # NATIVE-READ
        "driver_expenses_section": "Расходы за этот рейс",  # NATIVE-READ
        "driver_expenses_none": "Расходов за этот рейс нет",  # NATIVE-READ
        "driver_expenses_block_pending": "Сначала примите решение по ожидающим расходам водителя",  # NATIVE-READ
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
