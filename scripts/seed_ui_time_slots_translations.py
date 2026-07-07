"""Seed the admin-UI `time_slots` i18next namespace (category='ui_time_slots').

The admin UI serves the `time_slots` namespace from Translation rows with
category='ui_time_slots' and BARE keys (see AdminUiTranslationService). These
are the keys used by the admin TimeSlots page. English values here MUST
match the inline t(key, { defaultValue: '...' }) fallback strings in
admin_ui/src/pages/TimeSlots.js.

Run inside the business_app container (scripts/ is not mounted, so pipe it in):
    docker compose exec -T business_app python - < scripts/seed_ui_time_slots_translations.py
Then restart business_app so the translations API serves them:
    docker compose restart business_app
"""

from business_app import create_app
from business_app.models.translation import Translation

UI_TIME_SLOTS_CATEGORY = "ui_time_slots"

UI_TIME_SLOTS_TRANSLATIONS = {
    "en": {
        "page_title": "Delivery Time Slots Management",
        "create_time_slot": "Create Time Slot",
        "edit_time_slot": "Edit Time Slot",
        "name": "Name",
        "name_placeholder": "e.g., Morning, Afternoon, Evening",
        "name_required": "Please enter time slot name",
        "time_range": "Time Range",
        "start_time": "Start Time",
        "start_time_required": "Please select start time",
        "end_time": "End Time",
        "end_time_required": "Please select end time",
        "max_orders": "Max Orders",
        "max_orders_required": "Please enter maximum orders",
        "delivery_fee": "Delivery Fee",
        "delivery_fee_uzs": "Delivery Fee (UZS)",
        "delivery_fee_required": "Please enter delivery fee",
        "premium": "Premium",
        "premium_slot": "Premium Slot",
        "premium_fee_uzs": "Premium Fee (UZS)",
        "premium_fee_tooltip": "Additional fee for premium slots",
        "premium_tag": "Premium (+{{fee}})",
        "regular": "Regular",
        "available_days": "Available Days",
        "all_days": "All Days",
        "monday": "Monday",
        "tuesday": "Tuesday",
        "wednesday": "Wednesday",
        "thursday": "Thursday",
        "friday": "Friday",
        "saturday": "Saturday",
        "sunday": "Sunday",
        "day_mon": "Mon",
        "day_tue": "Tue",
        "day_wed": "Wed",
        "day_thu": "Thu",
        "day_fri": "Fri",
        "day_sat": "Sat",
        "day_sun": "Sun",
        "status": "Status",
        "active": "Active",
        "inactive": "Inactive",
        "actions": "Actions",
        "edit": "Edit",
        "delete": "Delete",
        "delete_confirm": "Are you sure you want to delete this time slot?",
        "yes": "Yes",
        "no": "No",
        "total_time_slots": "Total {{count}} time slots",
        "created": "Time slot created successfully",
        "create_failed": "Failed to create time slot",
        "updated": "Time slot updated successfully",
        "update_failed": "Failed to update time slot",
        "deleted": "Time slot deleted successfully",
        "delete_failed": "Failed to delete time slot",
    },
    "uz": {
        "page_title": "Yetkazib berish vaqt oralig'ini boshqarish",
        "create_time_slot": "Vaqt oralig'ini yaratish",
        "edit_time_slot": "Vaqt oralig'ini tahrirlash",
        "name": "Nomi",
        "name_placeholder": "Masalan: Ertalab, Kunduzi, Kechqurun",
        "name_required": "Vaqt oralig'i nomini kiriting",
        "time_range": "Vaqt oralig'i",
        "start_time": "Boshlanish vaqti",
        "start_time_required": "Boshlanish vaqtini tanlang",
        "end_time": "Tugash vaqti",
        "end_time_required": "Tugash vaqtini tanlang",
        "max_orders": "Maksimal buyurtmalar",
        "max_orders_required": "Maksimal buyurtmalar sonini kiriting",
        "delivery_fee": "Yetkazib berish narxi",
        "delivery_fee_uzs": "Yetkazib berish narxi (UZS)",
        "delivery_fee_required": "Yetkazib berish narxini kiriting",
        "premium": "Premium",
        "premium_slot": "Premium oraliq",
        "premium_fee_uzs": "Premium narxi (UZS)",
        "premium_fee_tooltip": "Premium oraliqlar uchun qo'shimcha narx",
        "premium_tag": "Premium (+{{fee}})",
        "regular": "Oddiy",
        "available_days": "Mavjud kunlar",
        "all_days": "Barcha kunlar",
        "monday": "Dushanba",
        "tuesday": "Seshanba",
        "wednesday": "Chorshanba",
        "thursday": "Payshanba",
        "friday": "Juma",
        "saturday": "Shanba",
        "sunday": "Yakshanba",
        "day_mon": "Dush",
        "day_tue": "Sesh",
        "day_wed": "Chor",
        "day_thu": "Pay",
        "day_fri": "Juma",
        "day_sat": "Shan",
        "day_sun": "Yak",
        "status": "Holati",
        "active": "Faol",
        "inactive": "Nofaol",
        "actions": "Amallar",
        "edit": "Tahrirlash",
        "delete": "O'chirish",
        "delete_confirm": "Ushbu vaqt oralig'ini o'chirishga ishonchingiz komilmi?",
        "yes": "Ha",
        "no": "Yo'q",
        "total_time_slots": "Jami {{count}} ta vaqt oralig'i",
        "created": "Vaqt oralig'i muvaffaqiyatli yaratildi",
        "create_failed": "Vaqt oralig'ini yaratib bo'lmadi",
        "updated": "Vaqt oralig'i muvaffaqiyatli yangilandi",
        "update_failed": "Vaqt oralig'ini yangilab bo'lmadi",
        "deleted": "Vaqt oralig'i muvaffaqiyatli o'chirildi",
        "delete_failed": "Vaqt oralig'ini o'chirib bo'lmadi",
    },
    "ru": {
        "page_title": "Управление временными интервалами доставки",
        "create_time_slot": "Создать интервал",
        "edit_time_slot": "Редактировать интервал",
        "name": "Название",
        "name_placeholder": "Например: Утро, День, Вечер",
        "name_required": "Введите название интервала",
        "time_range": "Временной диапазон",
        "start_time": "Время начала",
        "start_time_required": "Выберите время начала",
        "end_time": "Время окончания",
        "end_time_required": "Выберите время окончания",
        "max_orders": "Макс. заказов",
        "max_orders_required": "Введите максимальное количество заказов",
        "delivery_fee": "Стоимость доставки",
        "delivery_fee_uzs": "Стоимость доставки (UZS)",
        "delivery_fee_required": "Введите стоимость доставки",
        "premium": "Премиум",
        "premium_slot": "Премиум интервал",
        "premium_fee_uzs": "Премиум наценка (UZS)",
        "premium_fee_tooltip": "Дополнительная плата за премиум интервалы",
        "premium_tag": "Премиум (+{{fee}})",
        "regular": "Обычный",
        "available_days": "Доступные дни",
        "all_days": "Все дни",
        "monday": "Понедельник",
        "tuesday": "Вторник",
        "wednesday": "Среда",
        "thursday": "Четверг",
        "friday": "Пятница",
        "saturday": "Суббота",
        "sunday": "Воскресенье",
        "day_mon": "Пн",
        "day_tue": "Вт",
        "day_wed": "Ср",
        "day_thu": "Чт",
        "day_fri": "Пт",
        "day_sat": "Сб",
        "day_sun": "Вс",
        "status": "Статус",
        "active": "Активен",
        "inactive": "Неактивен",
        "actions": "Действия",
        "edit": "Изменить",
        "delete": "Удалить",
        "delete_confirm": "Вы уверены, что хотите удалить этот интервал?",
        "yes": "Да",
        "no": "Нет",
        "total_time_slots": "Всего {{count}} интервалов",
        "created": "Интервал успешно создан",
        "create_failed": "Не удалось создать интервал",
        "updated": "Интервал успешно обновлён",
        "update_failed": "Не удалось обновить интервал",
        "deleted": "Интервал успешно удалён",
        "delete_failed": "Не удалось удалить интервал",
    },
}


def seed_ui_time_slots_translations(user_id: int | None = None) -> None:
    """Upsert the ui_time_slots admin-UI translations (idempotent)."""
    Translation.bulk_create_or_update(
        UI_TIME_SLOTS_TRANSLATIONS, category=UI_TIME_SLOTS_CATEGORY, user_id=user_id
    )


def main() -> None:
    app = create_app()
    with app.app_context():
        seed_ui_time_slots_translations()
        total = len(UI_TIME_SLOTS_TRANSLATIONS["en"])
        print(f"Seeded {total} ui_time_slots translation rows (x3 languages).")


if __name__ == "__main__":
    main()
