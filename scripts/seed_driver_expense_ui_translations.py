# scripts/seed_driver_expense_ui_translations.py
"""Seed the admin-UI copy of the Driver expenses page (driver cash expense claims, spec 2026-11-17 §6.8).

category='ui' with DOTTED keys, the scripts/seed_shared_place_ui_translations.py mechanism: AdminUiTranslationService
resolves `ui.*` into the page's i18next namespace via LEGACY_NAMESPACE_PREFIXES, which requires category == 'ui'.

This script owns `ui.driver_expenses.*` and the menu label `ui.nav.driver_expenses`. `translations` is unique on
(key, language) and a later seed re-assigns the row's category, so one key has one owning script.

Every call site passes an inline English fallback, so the page renders in English BEFORE this seed lands; the `en`
values below reproduce those fallbacks byte for byte, and tests/unit/test_driver_expense_ui_translation_seed.py fails
on any drift, on a seeded key nobody reads, on a rendered key nobody seeds, and on a `{{placeholder}}` that differs
between languages.

Run inside the business_app container (scripts/ is NOT volume-mounted, so pipe the file in over stdin; piped stdin
still executes as __main__):
    docker compose exec -T business_app python - < scripts/seed_driver_expense_ui_translations.py
Then restart business_app so the translations API serves them:
    docker compose restart business_app
"""

from business_app import create_app
from business_app.models.translation import Translation

CATEGORY = "ui"

# {key: {language: value}}; `en` MUST match the inline t(key, 'fallback') string exactly. Every uz and ru line is
# NATIVE-READ: the owner skims them.
KEYS = {
    "ui.nav.driver_expenses": {
        "en": "Driver expenses",
        "uz": "Haydovchi xarajatlari",  # NATIVE-READ
        "ru": "Расходы водителей",  # NATIVE-READ
    },
    "ui.driver_expenses.title": {
        "en": "Driver expenses",
        "uz": "Haydovchi xarajatlari",  # NATIVE-READ
        "ru": "Расходы водителей",  # NATIVE-READ
    },
    "ui.driver_expenses.status.pending": {
        "en": "Waiting",
        "uz": "Kutilmoqda",  # NATIVE-READ
        "ru": "Ожидают",  # NATIVE-READ
    },
    "ui.driver_expenses.status.approved": {
        "en": "Approved",
        "uz": "Tasdiqlangan",  # NATIVE-READ
        "ru": "Утверждённые",  # NATIVE-READ
    },
    "ui.driver_expenses.status.rejected": {
        "en": "Rejected",
        "uz": "Rad etilgan",  # NATIVE-READ
        "ru": "Отклонённые",  # NATIVE-READ
    },
    "ui.driver_expenses.status.withdrawn": {
        "en": "Withdrawn",
        "uz": "Qaytarib olingan",  # NATIVE-READ
        "ru": "Отозванные",  # NATIVE-READ
    },
    "ui.driver_expenses.status.all": {
        "en": "All",
        "uz": "Hammasi",  # NATIVE-READ
        "ru": "Все",  # NATIVE-READ
    },
    "ui.driver_expenses.category.fuel": {
        "en": "Fuel",
        "uz": "Yoqilg'i",  # NATIVE-READ
        "ru": "Топливо",  # NATIVE-READ
    },
    "ui.driver_expenses.category.new_bottles": {
        "en": "New bottles",
        "uz": "Yangi idishlar",  # NATIVE-READ
        "ru": "Новые бутыли",  # NATIVE-READ
    },
    "ui.driver_expenses.category.caps": {
        "en": "Caps",
        "uz": "Qopqoqlar",  # NATIVE-READ
        "ru": "Крышки",  # NATIVE-READ
    },
    "ui.driver_expenses.category.labels": {
        "en": "Labels",
        "uz": "Yorliqlar",  # NATIVE-READ
        "ru": "Этикетки",  # NATIVE-READ
    },
    "ui.driver_expenses.category.other_supplies": {
        "en": "Other supplies",
        "uz": "Boshqa materiallar",  # NATIVE-READ
        "ru": "Прочие материалы",  # NATIVE-READ
    },
    "ui.driver_expenses.category.vehicle": {
        "en": "Car repair",
        "uz": "Mashina ta'miri",  # NATIVE-READ
        "ru": "Ремонт машины",  # NATIVE-READ
    },
    "ui.driver_expenses.category.parking_wash": {
        "en": "Parking/wash",
        "uz": "Avtoturargoh/yuvish",  # NATIVE-READ
        "ru": "Парковка/мойка",  # NATIVE-READ
    },
    "ui.driver_expenses.category.other": {
        "en": "Other",
        "uz": "Boshqa",  # NATIVE-READ
        "ru": "Другое",  # NATIVE-READ
    },
    "ui.driver_expenses.col_recorded": {
        "en": "Recorded",
        "uz": "Kiritilgan",  # NATIVE-READ
        "ru": "Записан",  # NATIVE-READ
    },
    "ui.driver_expenses.col_driver": {
        "en": "Driver",
        "uz": "Haydovchi",  # NATIVE-READ
        "ru": "Водитель",  # NATIVE-READ
    },
    "ui.driver_expenses.col_trip": {
        "en": "Trip",
        "uz": "Reys",  # NATIVE-READ
        "ru": "Рейс",  # NATIVE-READ
    },
    "ui.driver_expenses.col_category": {
        "en": "Category",
        "uz": "Toifa",  # NATIVE-READ
        "ru": "Категория",  # NATIVE-READ
    },
    "ui.driver_expenses.col_claimed": {
        "en": "Claimed",
        "uz": "So'ralgan",  # NATIVE-READ
        "ru": "Заявлено",  # NATIVE-READ
    },
    "ui.driver_expenses.col_approved": {
        "en": "Approved",
        "uz": "Tasdiqlangan",  # NATIVE-READ
        "ru": "Утверждено",  # NATIVE-READ
    },
    "ui.driver_expenses.col_note": {
        "en": "Note",
        "uz": "Izoh",  # NATIVE-READ
        "ru": "Комментарий",  # NATIVE-READ
    },
    "ui.driver_expenses.col_receipt": {
        "en": "Receipt",
        "uz": "Chek",  # NATIVE-READ
        "ru": "Чек",  # NATIVE-READ
    },
    "ui.driver_expenses.col_status": {
        "en": "Status",
        "uz": "Holat",  # NATIVE-READ
        "ru": "Статус",  # NATIVE-READ
    },
    "ui.driver_expenses.col_decided_by": {
        "en": "Decided by",
        "uz": "Qaror qilgan",  # NATIVE-READ
        "ru": "Решение принял",  # NATIVE-READ
    },
    "ui.driver_expenses.self_decided": {
        "en": "Self-decided",
        "uz": "O'zi qaror qilgan",  # NATIVE-READ
        "ru": "Решение о себе",  # NATIVE-READ
    },
    "ui.driver_expenses.totals_title": {
        "en": "Approved spend by category",
        "uz": "Toifalar bo'yicha tasdiqlangan xarajatlar",  # NATIVE-READ
        "ru": "Утверждённые расходы по категориям",  # NATIVE-READ
    },
    "ui.driver_expenses.approve": {
        "en": "Approve",
        "uz": "Tasdiqlash",  # NATIVE-READ
        "ru": "Утвердить",  # NATIVE-READ
    },
    "ui.driver_expenses.reject": {
        "en": "Reject",
        "uz": "Rad etish",  # NATIVE-READ
        "ru": "Отклонить",  # NATIVE-READ
    },
    "ui.driver_expenses.undo": {
        "en": "Undo approval",
        "uz": "Tasdiqni bekor qilish",  # NATIVE-READ
        "ru": "Отменить утверждение",  # NATIVE-READ
    },
    "ui.driver_expenses.amount_to_approve": {
        "en": "Amount to approve",
        "uz": "Tasdiqlanadigan summa",  # NATIVE-READ
        "ru": "Сумма к утверждению",  # NATIVE-READ
    },
    "ui.driver_expenses.max_hint": {
        "en": "At most {{amount}}",
        "uz": "Ko'pi bilan {{amount}}",  # NATIVE-READ
        "ru": "Не больше {{amount}}",  # NATIVE-READ
    },
    "ui.driver_expenses.effect": {
        "en": "Trip #{{session}}: on hand {{before}} → {{after}} · Hand in now {{handBefore}} → {{handAfter}}",
        "uz": "Reys #{{session}}: qo'lda {{before}} → {{after}} · Hozir topshirish {{handBefore}} → {{handAfter}}",  # NATIVE-READ
        "ru": "Рейс #{{session}}: на руках {{before}} → {{after}} · Сдать сейчас {{handBefore}} → {{handAfter}}",  # NATIVE-READ
    },
    "ui.driver_expenses.effect_closes": {
        "en": "the trip closes",
        "uz": "reys yopiladi",  # NATIVE-READ
        "ru": "рейс закроется",  # NATIVE-READ
    },
    "ui.driver_expenses.warning_duplicate": {
        "en": "Same driver, same amount, same day: {{ids}}",
        "uz": "Bir haydovchi, bir summa, bir kun: {{ids}}",  # NATIVE-READ
        "ru": "Тот же водитель, та же сумма, тот же день: {{ids}}",  # NATIVE-READ
    },
    "ui.driver_expenses.warning_recent_trips": {
        "en": "The driver's last closed trips",
        "uz": "Haydovchining oxirgi yopilgan reyslari",  # NATIVE-READ
        "ru": "Последние закрытые рейсы водителя",  # NATIVE-READ
    },
    "ui.driver_expenses.trip_status.submitted": {
        "en": "Not checked yet",
        "uz": "Hali tekshirilmagan",  # NATIVE-READ
        "ru": "Ещё не проверен",  # NATIVE-READ
    },
    "ui.driver_expenses.trip_status.verified": {
        "en": "Checked",
        "uz": "Tekshirilgan",  # NATIVE-READ
        "ru": "Проверен",  # NATIVE-READ
    },
    "ui.driver_expenses.trip_status.mismatch": {
        "en": "Mismatch, not resolved",
        "uz": "Farq bor, hal qilinmagan",  # NATIVE-READ
        "ru": "Расхождение, не урегулировано",  # NATIVE-READ
    },
    "ui.driver_expenses.trip_status.resolved": {
        "en": "Written off",
        "uz": "Hisobdan chiqarilgan",  # NATIVE-READ
        "ru": "Списано",  # NATIVE-READ
    },
    "ui.driver_expenses.trip_status.force_closed": {
        "en": "Force-closed",
        "uz": "Majburan yopilgan",  # NATIVE-READ
        "ru": "Закрыт принудительно",  # NATIVE-READ
    },
    "ui.driver_expenses.reason": {
        "en": "Reason",
        "uz": "Sabab",  # NATIVE-READ
        "ru": "Причина",  # NATIVE-READ
    },
    "ui.driver_expenses.reason_min": {
        "en": "At least {{min}} characters",
        "uz": "Kamida {{min}} ta belgi",  # NATIVE-READ
        "ru": "Не менее {{min}} символов",  # NATIVE-READ
    },
    "ui.driver_expenses.location": {
        "en": "Location",
        "uz": "Joylashuv",  # NATIVE-READ
        "ru": "Местоположение",  # NATIVE-READ
    },
    "ui.driver_expenses.no_location": {
        "en": "No location",
        "uz": "Joylashuv yo'q",  # NATIVE-READ
        "ru": "Нет местоположения",  # NATIVE-READ
    },
    "ui.driver_expenses.photo_unavailable": {
        "en": "Photo unavailable",
        "uz": "Rasm mavjud emas",  # NATIVE-READ
        "ru": "Фото недоступно",  # NATIVE-READ
    },
    "ui.driver_expenses.review": {
        "en": "Review",
        "uz": "Ko'rib chiqish",  # NATIVE-READ
        "ru": "Рассмотреть",  # NATIVE-READ
    },
    "ui.driver_expenses.load_failed": {
        "en": "Could not load the expenses",
        "uz": "Xarajatlarni yuklab bo'lmadi",  # NATIVE-READ
        "ru": "Не удалось загрузить расходы",  # NATIVE-READ
    },
    "ui.driver_expenses.block.not_pending": {
        "en": "Already decided",
        "uz": "Qaror allaqachon qabul qilingan",  # NATIVE-READ
        "ru": "Решение уже принято",  # NATIVE-READ
    },
    "ui.driver_expenses.block.trip_not_active": {
        "en": "The trip is no longer active",
        "uz": "Reys endi faol emas",  # NATIVE-READ
        "ru": "Рейс уже не активен",  # NATIVE-READ
    },
    "ui.driver_expenses.block.amount_above_claim": {
        "en": "More than the claim",
        "uz": "So'ralgan summadan ko'p",  # NATIVE-READ
        "ru": "Больше заявленной суммы",  # NATIVE-READ
    },
    "ui.driver_expenses.block.amount_above_trip_cash": {
        "en": "More than the driver's trip cash",
        "uz": "Haydovchining reys naqdidan ko'p",  # NATIVE-READ
        "ru": "Больше наличных рейса водителя",  # NATIVE-READ
    },
    "ui.driver_expenses.block.self_decision": {
        "en": "You cannot decide your own expense",
        "uz": "O'z xarajatingiz bo'yicha qaror qila olmaysiz",  # NATIVE-READ
        "ru": "Нельзя принимать решение по своему расходу",  # NATIVE-READ
    },
    "ui.driver_expenses.block.not_approved": {
        "en": "Not approved",
        "uz": "Tasdiqlanmagan",  # NATIVE-READ
        "ru": "Не утверждён",  # NATIVE-READ
    },
    "ui.driver_expenses.block.session_checked": {
        "en": "The trip is already closed or checked",
        "uz": "Reys allaqachon yopilgan yoki tekshirilgan",  # NATIVE-READ
        "ru": "Рейс уже закрыт или проверен",  # NATIVE-READ
    },
    "ui.driver_expenses.block.cash_already_handed_in": {
        "en": "The driver already handed in the amount this approval freed",
        "uz": "Haydovchi bu tasdiq bo'shatgan summani allaqachon topshirgan",  # NATIVE-READ
        "ru": "Водитель уже сдал сумму, которую освободило это утверждение",  # NATIVE-READ
    },
}


def run():
    app = create_app()
    with app.app_context():
        data = {"en": {}, "uz": {}, "ru": {}}
        for key, langs in KEYS.items():
            for lang, value in langs.items():
                data[lang][key] = value
        Translation.bulk_create_or_update(data, category=CATEGORY)
        print(f"Seeded {len(KEYS)} keys x3 langs into category '{CATEGORY}'.")


if __name__ == "__main__":
    run()
