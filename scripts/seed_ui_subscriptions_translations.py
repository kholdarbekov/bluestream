"""Seed the admin-UI `subscriptions` i18next namespace (category='ui_subscriptions').

The admin UI serves the `subscriptions` namespace from Translation rows with
category='ui_subscriptions' and BARE keys (see AdminUiTranslationService). These
are the keys used by the admin Subscriptions page. English values here MUST
match the inline t(key, { defaultValue: '...' }) fallback strings in
admin_ui/src/pages/Subscriptions.js.

Every key here is page-unique. `translations` is unique on (key, language) only,
and `Translation.bulk_create_or_update` re-assigns `category` on every upsert, so
a bare key another seed also writes (`status`, `customer`, `save`, ...) would move
that page's row into `ui_subscriptions`. The generic ones therefore carry a `sub_`
prefix. tests/unit/test_admin_ui_seed_key_ownership.py fails if this seed shares
a key with any other admin-UI seed.

Run inside the business_app container (scripts/ is not mounted, so pipe it in):
    docker compose exec -T business_app python - < scripts/seed_ui_subscriptions_translations.py
Then restart business_app so the translations API serves them:
    docker compose restart business_app

Note on the nav label (`ui.nav.subscriptions`): admin nav labels are a
separate, existing mechanism seeded via scripts/seed_backend_translations.py
(category derived as "ui" from the dotted key prefix, consumed by
AdminUiTranslationService's legacy "navigation" namespace lookup). That is a
different SSOT than this page-scoped `ui_subscriptions` category, and
`ui.nav.subscriptions` is not yet present there, so AdminLayout currently
renders its English fallback ("Subscriptions") for all languages. Adding it
is out of scope for this script — see the seed_backend_translations.py
BACKEND_TRANSLATIONS/ADMIN_UI_ORDER_TRANSLATIONS dict if/when that's wanted.
"""

from business_app import create_app
from business_app.models.translation import Translation

UI_SUBSCRIPTIONS_CATEGORY = "ui_subscriptions"

UI_SUBSCRIPTIONS_TRANSLATIONS = {
    "en": {
        "number": "Number",
        "sub_customer": "Customer",
        "sub_status": "Status",
        "billing_amount": "Amount",
        "sub_items": "Items",
        "sub_actions": "Actions",
        "sub_search_placeholder": "Search number / name / customer",
        "no_subscriptions": "No subscriptions found",
        "create_button": "Create Subscription",
        "create_title": "Create Subscription",
        "edit_title": "Edit Subscription",
        "section_details": "Details",
        "section_items": "Items",
        "section_billing": "Billing",
        "section_delivery": "Delivery schedule",
        "section_overrides": "Danger zone / overrides",
        "sub_name": "Name",
        "sub_description": "Description",
        "search_customer": "Search customer…",
        "sub_product": "Product",
        "sub_quantity": "Qty",
        "instructions": "Notes",
        "add_item": "Add item",
        "payment_method": "Payment method",
        "discount": "Discount %",
        "loyalty_multiplier": "Loyalty multiplier",
        "auto_payment": "Auto payment",
        "auto_renew": "Auto renew",
        "day_of_week": "Delivery day",
        "day_of_month": "Day of month",
        "sub_address": "Delivery address",
        "select_address": "Select address",
        "time_slot": "Time slot",
        "start_date": "Start date",
        "end_date": "End date",
        "override_warning": "Manual overrides can break automated billing. Use with care.",
        "override_status": "Allow editing any status",
        "override_amount": "Manual billing amount",
        "override_dates": "Set next delivery date",
        "sub_cancel": "Cancel",
        "submit_update": "Update",
        "submit_create": "Create",
        "details": "Subscription",
        "delivery": "Delivery",
        "total_orders": "Orders generated",
        "lifecycle": "Actions",
        "pause": "Pause",
        "resume": "Resume",
        "cancel_sub": "Cancel",
        "cancel_confirm": "Cancel (delete) this subscription?",
        "bill_now": "Create next order now",
        "bill_now_confirm": "Create the next order now?",
        "unit_price": "Unit price",
        "sub_save": "Save",
        "remove_item_confirm": "Remove this item?",
        "sub_created": "Subscription created",
        "sub_updated": "Subscription updated",
        "sub_create_failed": "Failed to create subscription",
        "sub_update_failed": "Failed to update subscription",
        "action_done": "Done",
        "action_failed": "Action failed",
        "sub_no_order_created": "No order was created",
        "item_added": "Item added",
        "item_updated": "Item updated",
        "item_removed": "Item removed",
        "item_failed": "Failed",
        "frequency": "Frequency",
        "next_delivery": "Next delivery",
        "next_delivery_date": "Next delivery date",
        "time_to_be_confirmed": "time to be confirmed",
        "weekday_0": "Monday",
        "weekday_1": "Tuesday",
        "weekday_2": "Wednesday",
        "weekday_3": "Thursday",
        "weekday_4": "Friday",
        "weekday_5": "Saturday",
        "weekday_6": "Sunday",
    },
    "uz": {
        "number": "Raqam",
        "sub_customer": "Mijoz",
        "sub_status": "Holati",
        "billing_amount": "Summa",
        "sub_items": "Mahsulotlar",
        "sub_actions": "Amallar",
        "sub_search_placeholder": "Raqam / nom / mijoz bo'yicha qidirish",
        "no_subscriptions": "Obunalar topilmadi",
        "create_button": "Obuna yaratish",
        "create_title": "Obuna yaratish",
        "edit_title": "Obunani tahrirlash",
        "section_details": "Ma'lumotlar",
        "section_items": "Mahsulotlar",
        "section_billing": "To'lov",
        "section_delivery": "Yetkazib berish jadvali",
        "section_overrides": "Xavfli zona / o'zgartirishlar",
        "sub_name": "Nomi",
        "sub_description": "Tavsif",
        "search_customer": "Mijozni qidirish…",
        "sub_product": "Mahsulot",
        "sub_quantity": "Soni",
        "instructions": "Izoh",
        "add_item": "Mahsulot qo'shish",
        "payment_method": "To'lov usuli",
        "discount": "Chegirma %",
        "loyalty_multiplier": "Sodiqlik koeffitsiyenti",
        "auto_payment": "Avtomatik to'lov",
        "auto_renew": "Avtomatik yangilash",
        "day_of_week": "Yetkazib berish kuni",
        "day_of_month": "Oyning kuni",
        "sub_address": "Yetkazib berish manzili",
        "select_address": "Manzilni tanlang",
        "time_slot": "Vaqt oralig'i",
        "start_date": "Boshlanish sanasi",
        "end_date": "Tugash sanasi",
        "override_warning": "Qo'lda o'zgartirishlar avtomatik to'lovni buzishi mumkin. Ehtiyot bo'ling.",
        "override_status": "Har qanday holatni tahrirlashga ruxsat",
        "override_amount": "To'lov summasini qo'lda kiritish",
        "override_dates": "Keyingi yetkazib berish sanasini belgilash",
        "sub_cancel": "Bekor qilish",
        "submit_update": "Yangilash",
        "submit_create": "Yaratish",
        "details": "Obuna",
        "delivery": "Yetkazib berish",
        "total_orders": "Yaratilgan buyurtmalar",
        "lifecycle": "Amallar",
        "pause": "To'xtatish",
        "resume": "Davom ettirish",
        "cancel_sub": "Bekor qilish",
        "cancel_confirm": "Ushbu obunani bekor qilasizmi?",
        "bill_now": "Keyingi buyurtmani hozir yaratish",
        "bill_now_confirm": "Keyingi buyurtma hozir yaratilsinmi?",
        "unit_price": "Birlik narxi",
        "sub_save": "Saqlash",
        "remove_item_confirm": "Ushbu mahsulot o'chirilsinmi?",
        "sub_created": "Obuna yaratildi",
        "sub_updated": "Obuna yangilandi",
        "sub_create_failed": "Obuna yaratib bo'lmadi",
        "sub_update_failed": "Obunani yangilab bo'lmadi",
        "action_done": "Bajarildi",
        "action_failed": "Amal bajarilmadi",
        "sub_no_order_created": "Buyurtma yaratilmadi",
        "item_added": "Mahsulot qo'shildi",
        "item_updated": "Mahsulot yangilandi",
        "item_removed": "Mahsulot o'chirildi",
        "item_failed": "Xatolik",
        "frequency": "Chastota",
        "next_delivery": "Keyingi yetkazib berish",
        "next_delivery_date": "Keyingi yetkazib berish sanasi",
        "time_to_be_confirmed": "vaqti tasdiqlanadi",
        "weekday_0": "Dushanba",
        "weekday_1": "Seshanba",
        "weekday_2": "Chorshanba",
        "weekday_3": "Payshanba",
        "weekday_4": "Juma",
        "weekday_5": "Shanba",
        "weekday_6": "Yakshanba",
    },
    "ru": {
        "number": "Номер",
        "sub_customer": "Клиент",
        "sub_status": "Статус",
        "billing_amount": "Сумма",
        "sub_items": "Позиции",
        "sub_actions": "Действия",
        "sub_search_placeholder": "Поиск по номеру / имени / клиенту",
        "no_subscriptions": "Подписки не найдены",
        "create_button": "Создать подписку",
        "create_title": "Создать подписку",
        "edit_title": "Редактировать подписку",
        "section_details": "Детали",
        "section_items": "Позиции",
        "section_billing": "Оплата",
        "section_delivery": "График доставки",
        "section_overrides": "Опасная зона / переопределения",
        "sub_name": "Название",
        "sub_description": "Описание",
        "search_customer": "Поиск клиента…",
        "sub_product": "Продукт",
        "sub_quantity": "Кол-во",
        "instructions": "Заметки",
        "add_item": "Добавить позицию",
        "payment_method": "Способ оплаты",
        "discount": "Скидка %",
        "loyalty_multiplier": "Множитель лояльности",
        "auto_payment": "Автоплатёж",
        "auto_renew": "Автопродление",
        "day_of_week": "День доставки",
        "day_of_month": "День месяца",
        "sub_address": "Адрес доставки",
        "select_address": "Выберите адрес",
        "time_slot": "Временной интервал",
        "start_date": "Дата начала",
        "end_date": "Дата окончания",
        "override_warning": "Ручные переопределения могут нарушить автобиллинг. Используйте осторожно.",
        "override_status": "Разрешить редактирование любого статуса",
        "override_amount": "Ручная сумма списания",
        "override_dates": "Задать дату следующей доставки",
        "sub_cancel": "Отмена",
        "submit_update": "Обновить",
        "submit_create": "Создать",
        "details": "Подписка",
        "delivery": "Доставка",
        "total_orders": "Создано заказов",
        "lifecycle": "Действия",
        "pause": "Приостановить",
        "resume": "Возобновить",
        "cancel_sub": "Отменить",
        "cancel_confirm": "Отменить (удалить) эту подписку?",
        "bill_now": "Создать следующий заказ сейчас",
        "bill_now_confirm": "Создать следующий заказ сейчас?",
        "unit_price": "Цена за единицу",
        "sub_save": "Сохранить",
        "remove_item_confirm": "Удалить эту позицию?",
        "sub_created": "Подписка создана",
        # Fixed typo from the task brief's draft ("обновлendi" -> "обновлена").
        "sub_updated": "Подписка обновлена",
        "sub_create_failed": "Не удалось создать подписку",
        "sub_update_failed": "Не удалось обновить подписку",
        "action_done": "Готово",
        "action_failed": "Действие не выполнено",
        "sub_no_order_created": "Заказ не создан",
        "item_added": "Позиция добавлена",
        "item_updated": "Позиция обновлена",
        "item_removed": "Позиция удалена",
        "item_failed": "Ошибка",
        "frequency": "Частота",
        "next_delivery": "Следующая доставка",
        "next_delivery_date": "Дата следующей доставки",
        "time_to_be_confirmed": "время уточняется",
        "weekday_0": "Понедельник",
        "weekday_1": "Вторник",
        "weekday_2": "Среда",
        "weekday_3": "Четверг",
        "weekday_4": "Пятница",
        "weekday_5": "Суббота",
        "weekday_6": "Воскресенье",
    },
}


def seed_ui_subscriptions_translations(user_id: int | None = None) -> None:
    """Upsert the ui_subscriptions admin-UI translations (idempotent)."""
    Translation.bulk_create_or_update(
        UI_SUBSCRIPTIONS_TRANSLATIONS, category=UI_SUBSCRIPTIONS_CATEGORY, user_id=user_id
    )


def main() -> None:
    app = create_app()
    with app.app_context():
        seed_ui_subscriptions_translations()
        total = len(UI_SUBSCRIPTIONS_TRANSLATIONS["en"])
        print(f"Seeded {total} ui_subscriptions translation rows (x3 languages).")


if __name__ == "__main__":
    main()
