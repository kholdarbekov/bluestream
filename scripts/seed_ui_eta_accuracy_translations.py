"""Seed the admin "Delivery time accuracy" page copy (namespace `delivery` / category 'ui_delivery').

The admin UI serves the `delivery` namespace from Translation rows with category='ui_delivery' and BARE keys (see
AdminUiTranslationService). These are the keys used by admin_ui/src/pages/EtaAccuracy.js (honest-ETA Phase 2, Task 10).
English values here MUST equal the inline t(key, { defaultValue: '...' }) strings in that page.

Every key is page-unique (`eta_acc_` prefix): `translations` is unique on (key, language) only, and
`Translation.bulk_create_or_update` re-assigns `category` on every upsert, so a bare key another seed also writes would
move that page's row. tests/unit/test_admin_ui_seed_key_ownership.py fails if this seed shares a key with another one.
Values with `{{token}}` placeholders keep the page's token names in all three languages. The thresholds the copy names
(`{{morning_ends}}`, `{{daytime_from}}`, `{{daytime_to}}`, `{{tolerance}}`) come from the report's `definitions`, so no
value here states them.

The menu label (`ui.nav.eta_accuracy`) is seeded by scripts/seed_backend_translations.py, not here.

Run inside the business_app container (scripts/ is not mounted, so pipe it in):
    docker compose exec -T business_app python - < scripts/seed_ui_eta_accuracy_translations.py
Then restart business_app so the translations API serves them:
    docker compose restart business_app
"""

from business_app import create_app
from business_app.models.translation import Translation

UI_ETA_ACCURACY_CATEGORY = "ui_delivery"

UI_ETA_ACCURACY_TRANSLATIONS = {
    "en": {
        "eta_acc_title": "Delivery time accuracy",
        "eta_acc_gate": "Go-live check: {{kept}} kept over {{n}} orders (target {{target}})",
        "eta_acc_passing": "Passing",
        "eta_acc_not_yet": "Not yet",
        "eta_acc_before_eleven": "Orders placed before {{morning_ends}}: {{kept}} over {{n}}",
        "eta_acc_excluded": "Not counted: {{dated}} booked for a later date, {{no_window}} without a time, {{cancelled}} cancelled, {{returned}} returned, {{non_driver}} closed by staff, {{open}} still open",
        "eta_acc_col_stops_ahead": "Stops ahead",
        "eta_acc_col_started": "Driver started",
        "eta_acc_col_promises": "Promises",
        "eta_acc_col_kept": "Kept",
        "eta_acc_col_median_window": "Median window",
        "eta_acc_yes": "Yes",
        "eta_acc_no": "No",
        "eta_acc_median_window": "Median window: {{minutes}} overall, {{minutes_day}} for orders placed {{daytime_from}}–{{daytime_to}} (aim: 3 hours)",
        "eta_acc_same_day": "Delivered the same day as promised: {{share}}",
        "eta_acc_tomorrow_same_day": "Promised tomorrow but delivered the same day: {{share}}",
        "eta_acc_early": "Arrived before the window opened: {{share}}, by a median {{minutes}}",
        "eta_acc_drivers": "One driver working: {{kept}} kept, median window {{minutes}}. Two or more: {{kept2}}, {{minutes2}}",
        "eta_acc_updates": "Updates per order: none {{zero}}, at most one {{one}}",
        "eta_acc_on_the_way": "On the way: kept {{kept}}, within {{tolerance}} minutes {{within}}, median error Google {{google}} vs our engine {{engine}}",
        "eta_acc_on_the_way_stale": "On the way from a position too old for Google ({{n}}): kept {{kept}}, within {{tolerance}} minutes {{within}}, median error of our engine {{engine}}",
        "eta_acc_curve": "Stop time against the curve, by time of day: {{bands}}. By order size: {{sizes}}",
        "eta_acc_latest_fit": "Latest fit: {{date}}",
        "eta_acc_empty": "No delivery promises were recorded in this period yet",
        "eta_acc_load_failed": "The report could not be loaded",
    },
    "uz": {
        "eta_acc_title": "Yetkazib berish vaqti aniqligi",  # NATIVE-READ
        "eta_acc_gate": "Ishga tushirish tekshiruvi: {{n}} ta buyurtmaning {{kept}} qismida va'da bajarildi (maqsad {{target}})",  # NATIVE-READ
        "eta_acc_passing": "O'tdi",  # NATIVE-READ
        "eta_acc_not_yet": "Hali yo'q",  # NATIVE-READ
        "eta_acc_before_eleven": "Soat {{morning_ends}} gacha berilgan buyurtmalar: {{n}} tadan {{kept}}",  # NATIVE-READ
        "eta_acc_excluded": "Hisobga olinmadi: {{dated}} ta keyingi sanaga band qilingan, {{no_window}} ta vaqtsiz, {{cancelled}} ta bekor qilingan, {{returned}} ta qaytarilgan, {{non_driver}} ta xodim tomonidan yopilgan, {{open}} ta hali ochiq",  # NATIVE-READ
        "eta_acc_col_stops_ahead": "Oldindagi to'xtashlar",  # NATIVE-READ
        "eta_acc_col_started": "Haydovchi yo'lga chiqqan",  # NATIVE-READ
        "eta_acc_col_promises": "Va'dalar",  # NATIVE-READ
        "eta_acc_col_kept": "Bajarilgan",  # NATIVE-READ
        "eta_acc_col_median_window": "O'rtacha oraliq",  # NATIVE-READ
        "eta_acc_yes": "Ha",  # NATIVE-READ
        "eta_acc_no": "Yo'q",  # NATIVE-READ
        "eta_acc_median_window": "O'rtacha oraliq: umuman {{minutes}}, soat {{daytime_from}}–{{daytime_to}} da berilgan buyurtmalar uchun {{minutes_day}} (maqsad: 3 soat)",  # NATIVE-READ
        "eta_acc_same_day": "Va'da qilingan kunning o'zida yetkazildi: {{share}}",  # NATIVE-READ
        "eta_acc_tomorrow_same_day": "Ertaga deb va'da qilingan, lekin o'sha kuni yetkazilgan: {{share}}",  # NATIVE-READ
        "eta_acc_early": "Oraliq ochilishidan oldin yetib keldi: {{share}}, o'rtacha {{minutes}} ga",  # NATIVE-READ
        "eta_acc_drivers": "Bitta haydovchi ishlaganda: {{kept}} bajarilgan, o'rtacha oraliq {{minutes}}. Ikki va undan ko'p: {{kept2}}, {{minutes2}}",  # NATIVE-READ
        "eta_acc_updates": "Har bir buyurtma bo'yicha xabarlar: hech qanday {{zero}}, ko'pi bilan bitta {{one}}",  # NATIVE-READ
        "eta_acc_on_the_way": "Yo'lda: bajarilgan {{kept}}, {{tolerance}} daqiqa ichida {{within}}, o'rtacha xato Google {{google}}, bizning tizim {{engine}}",  # NATIVE-READ
        "eta_acc_on_the_way_stale": "Yo'lda, joylashuv Google uchun eskirgan ({{n}}): bajarilgan {{kept}}, {{tolerance}} daqiqa ichida {{within}}, bizning tizimning o'rtacha xatosi {{engine}}",  # NATIVE-READ
        "eta_acc_curve": "To'xtash vaqti egri chiziqqa nisbatan, kun vaqti bo'yicha: {{bands}}. Buyurtma hajmi bo'yicha: {{sizes}}",  # NATIVE-READ
        "eta_acc_latest_fit": "Oxirgi hisoblash: {{date}}",  # NATIVE-READ
        "eta_acc_empty": "Bu davrda hali yetkazib berish va'dalari qayd etilmagan",  # NATIVE-READ
        "eta_acc_load_failed": "Hisobotni yuklab bo'lmadi",  # NATIVE-READ
    },
    "ru": {
        "eta_acc_title": "Точность времени доставки",  # NATIVE-READ
        "eta_acc_gate": "Проверка перед запуском: выполнено {{kept}} из {{n}} заказов (цель {{target}})",  # NATIVE-READ
        "eta_acc_passing": "Пройдена",  # NATIVE-READ
        "eta_acc_not_yet": "Пока нет",  # NATIVE-READ
        "eta_acc_before_eleven": "Заказы, оформленные до {{morning_ends}}: {{kept}} из {{n}}",  # NATIVE-READ
        "eta_acc_excluded": "Не учтено: {{dated}} с датой на будущее, {{no_window}} без времени, {{cancelled}} отменено, {{returned}} возвращено, {{non_driver}} закрыто сотрудниками, {{open}} ещё открыто",  # NATIVE-READ
        "eta_acc_col_stops_ahead": "Остановок впереди",  # NATIVE-READ
        "eta_acc_col_started": "Водитель выехал",  # NATIVE-READ
        "eta_acc_col_promises": "Обещаний",  # NATIVE-READ
        "eta_acc_col_kept": "Выполнено",  # NATIVE-READ
        "eta_acc_col_median_window": "Медианное окно",  # NATIVE-READ
        "eta_acc_yes": "Да",  # NATIVE-READ
        "eta_acc_no": "Нет",  # NATIVE-READ
        "eta_acc_median_window": "Медианное окно: {{minutes}} в целом, {{minutes_day}} для заказов, оформленных с {{daytime_from}} до {{daytime_to}} (цель: 3 часа)",  # NATIVE-READ
        "eta_acc_same_day": "Доставлено в тот же день, что обещано: {{share}}",  # NATIVE-READ
        "eta_acc_tomorrow_same_day": "Обещано на завтра, но доставлено в тот же день: {{share}}",  # NATIVE-READ
        "eta_acc_early": "Приехали до начала окна: {{share}}, в среднем (медиана) на {{minutes}}",  # NATIVE-READ
        "eta_acc_drivers": "Один водитель на линии: выполнено {{kept}}, медианное окно {{minutes}}. Два и более: {{kept2}}, {{minutes2}}",  # NATIVE-READ
        "eta_acc_updates": "Сообщений на заказ: ни одного {{zero}}, не более одного {{one}}",  # NATIVE-READ
        "eta_acc_on_the_way": "В пути: выполнено {{kept}}, в пределах {{tolerance}} мин {{within}}, медианная ошибка Google {{google}} против нашей системы {{engine}}",  # NATIVE-READ
        "eta_acc_on_the_way_stale": "В пути, позиция устарела для Google ({{n}}): выполнено {{kept}}, в пределах {{tolerance}} мин {{within}}, медианная ошибка нашей системы {{engine}}",  # NATIVE-READ
        "eta_acc_curve": "Время остановки относительно кривой, по времени суток: {{bands}}. По размеру заказа: {{sizes}}",  # NATIVE-READ
        "eta_acc_latest_fit": "Последняя подгонка: {{date}}",  # NATIVE-READ
        "eta_acc_empty": "За этот период обещаний по доставке пока не записано",  # NATIVE-READ
        "eta_acc_load_failed": "Не удалось загрузить отчёт",  # NATIVE-READ
    },
}


def seed_ui_eta_accuracy_translations(user_id: int | None = None) -> None:
    """Upsert the ui_delivery eta_acc_* admin-UI translations (idempotent)."""
    Translation.bulk_create_or_update(UI_ETA_ACCURACY_TRANSLATIONS, category=UI_ETA_ACCURACY_CATEGORY, user_id=user_id)


def main() -> None:
    app = create_app()
    with app.app_context():
        seed_ui_eta_accuracy_translations()
        total = len(UI_ETA_ACCURACY_TRANSLATIONS["en"])
        print(f"Seeded {total} ui_delivery eta_acc_* translation rows (x3 languages).")


if __name__ == "__main__":
    main()
