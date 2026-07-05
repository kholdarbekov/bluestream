"""Seed the admin-UI `subscriptions` i18next namespace (category='ui_subscriptions').

The admin UI serves the `subscriptions` namespace from Translation rows with
category='ui_subscriptions' and BARE keys (see AdminUiTranslationService). These
are the keys used by the admin Subscriptions page. English values here MUST
match the inline t(key, { defaultValue: '...' }) fallback strings in
admin_ui/src/pages/Subscriptions.js.

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
        "customer": "Customer",
        "status": "Status",
        "billing_cycle": "Billing cycle",
        "billing_amount": "Amount",
        "next_billing": "Next billing",
        "last_billing": "Last billing date",
        "items": "Items",
        "actions": "Actions",
        "search_placeholder": "Search number / name / customer",
        "no_subscriptions": "No subscriptions found",
        "create_button": "Create Subscription",
        "create_title": "Create Subscription",
        "edit_title": "Edit Subscription",
        "section_details": "Details",
        "section_items": "Items",
        "section_billing": "Billing",
        "section_delivery": "Delivery schedule",
        "section_overrides": "Danger zone / overrides",
        "name": "Name",
        "description": "Description",
        "search_customer": "Search customer…",
        "product": "Product",
        "quantity": "Qty",
        "instructions": "Notes",
        "add_item": "Add item",
        "payment_method": "Payment method",
        "discount": "Discount %",
        "loyalty_multiplier": "Loyalty multiplier",
        "auto_payment": "Auto payment",
        "auto_renew": "Auto renew",
        "delivery_frequency": "Delivery frequency",
        "day_of_week": "Day of week (0=Mon)",
        "day_of_month": "Day of month",
        "address": "Delivery address",
        "select_address": "Select address",
        "time_slot": "Time slot",
        "start_date": "Start date",
        "end_date": "End date",
        "override_warning": "Manual overrides can break automated billing. Use with care.",
        "override_status": "Allow editing any status",
        "override_amount": "Manual billing amount",
        "override_dates": "Manual billing dates",
        "cancel": "Cancel",
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
        "bill_now": "Process billing now",
        "bill_now_confirm": "Generate an order and bill now?",
        "unit_price": "Unit price",
        "save": "Save",
        "remove_item_confirm": "Remove this item?",
        "created": "Subscription created",
        "updated": "Subscription updated",
        "create_failed": "Failed to create subscription",
        "update_failed": "Failed to update subscription",
        "action_done": "Done",
        "action_failed": "Action failed",
        "item_added": "Item added",
        "item_updated": "Item updated",
        "item_removed": "Item removed",
        "item_failed": "Failed",
    },
    "uz": {
        "number": "Raqam",
        "customer": "Mijoz",
        "status": "Holati",
        "billing_cycle": "To'lov davri",
        "billing_amount": "Summa",
        "next_billing": "Keyingi to'lov",
        "last_billing": "Oxirgi to'lov sanasi",
        "items": "Mahsulotlar",
        "actions": "Amallar",
        "search_placeholder": "Raqam / nom / mijoz bo'yicha qidirish",
        "no_subscriptions": "Obunalar topilmadi",
        "create_button": "Obuna yaratish",
        "create_title": "Obuna yaratish",
        "edit_title": "Obunani tahrirlash",
        "section_details": "Ma'lumotlar",
        "section_items": "Mahsulotlar",
        "section_billing": "To'lov",
        "section_delivery": "Yetkazib berish jadvali",
        "section_overrides": "Xavfli zona / o'zgartirishlar",
        "name": "Nomi",
        "description": "Tavsif",
        "search_customer": "Mijozni qidirish…",
        "product": "Mahsulot",
        "quantity": "Soni",
        "instructions": "Izoh",
        "add_item": "Mahsulot qo'shish",
        "payment_method": "To'lov usuli",
        "discount": "Chegirma %",
        "loyalty_multiplier": "Sodiqlik koeffitsiyenti",
        "auto_payment": "Avtomatik to'lov",
        "auto_renew": "Avtomatik yangilash",
        "delivery_frequency": "Yetkazib berish chastotasi",
        "day_of_week": "Hafta kuni (0=Dush)",
        "day_of_month": "Oyning kuni",
        "address": "Yetkazib berish manzili",
        "select_address": "Manzilni tanlang",
        "time_slot": "Vaqt oralig'i",
        "start_date": "Boshlanish sanasi",
        "end_date": "Tugash sanasi",
        "override_warning": "Qo'lda o'zgartirishlar avtomatik to'lovni buzishi mumkin. Ehtiyot bo'ling.",
        "override_status": "Har qanday holatni tahrirlashga ruxsat",
        "override_amount": "To'lov summasini qo'lda kiritish",
        "override_dates": "To'lov sanalarini qo'lda kiritish",
        "cancel": "Bekor qilish",
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
        "bill_now": "Hozir to'lovni amalga oshirish",
        "bill_now_confirm": "Buyurtma yaratib, hozir to'lov qilinsinmi?",
        "unit_price": "Birlik narxi",
        "save": "Saqlash",
        "remove_item_confirm": "Ushbu mahsulot o'chirilsinmi?",
        "created": "Obuna yaratildi",
        "updated": "Obuna yangilandi",
        "create_failed": "Obuna yaratib bo'lmadi",
        "update_failed": "Obunani yangilab bo'lmadi",
        "action_done": "Bajarildi",
        "action_failed": "Amal bajarilmadi",
        "item_added": "Mahsulot qo'shildi",
        "item_updated": "Mahsulot yangilandi",
        "item_removed": "Mahsulot o'chirildi",
        "item_failed": "Xatolik",
    },
    "ru": {
        "number": "Номер",
        "customer": "Клиент",
        "status": "Статус",
        "billing_cycle": "Период оплаты",
        "billing_amount": "Сумма",
        "next_billing": "Следующее списание",
        "last_billing": "Дата последнего списания",
        "items": "Позиции",
        "actions": "Действия",
        "search_placeholder": "Поиск по номеру / имени / клиенту",
        "no_subscriptions": "Подписки не найдены",
        "create_button": "Создать подписку",
        "create_title": "Создать подписку",
        "edit_title": "Редактировать подписку",
        "section_details": "Детали",
        "section_items": "Позиции",
        "section_billing": "Оплата",
        "section_delivery": "График доставки",
        "section_overrides": "Опасная зона / переопределения",
        "name": "Название",
        "description": "Описание",
        "search_customer": "Поиск клиента…",
        "product": "Продукт",
        "quantity": "Кол-во",
        "instructions": "Заметки",
        "add_item": "Добавить позицию",
        "payment_method": "Способ оплаты",
        "discount": "Скидка %",
        "loyalty_multiplier": "Множитель лояльности",
        "auto_payment": "Автоплатёж",
        "auto_renew": "Автопродление",
        "delivery_frequency": "Частота доставки",
        "day_of_week": "День недели (0=Пн)",
        "day_of_month": "День месяца",
        "address": "Адрес доставки",
        "select_address": "Выберите адрес",
        "time_slot": "Временной интервал",
        "start_date": "Дата начала",
        "end_date": "Дата окончания",
        "override_warning": "Ручные переопределения могут нарушить автобиллинг. Используйте осторожно.",
        "override_status": "Разрешить редактирование любого статуса",
        "override_amount": "Ручная сумма списания",
        "override_dates": "Ручные даты списания",
        "cancel": "Отмена",
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
        "bill_now": "Списать сейчас",
        "bill_now_confirm": "Создать заказ и списать сейчас?",
        "unit_price": "Цена за единицу",
        "save": "Сохранить",
        "remove_item_confirm": "Удалить эту позицию?",
        "created": "Подписка создана",
        # Fixed typo from the task brief's draft ("обновлendi" -> "обновлена").
        "updated": "Подписка обновлена",
        "create_failed": "Не удалось создать подписку",
        "update_failed": "Не удалось обновить подписку",
        "action_done": "Готово",
        "action_failed": "Действие не выполнено",
        "item_added": "Позиция добавлена",
        "item_updated": "Позиция обновлена",
        "item_removed": "Позиция удалена",
        "item_failed": "Ошибка",
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
