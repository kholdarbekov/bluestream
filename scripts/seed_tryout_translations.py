#!/usr/bin/env python3
"""
Seed translations for the try-out / returnable bottle tracking feature.

This script upserts feature-specific translations used by:
- backend/API/domain messages
- reminder/task reporting
- admin UI try-out page labels
- staff bot try-out workflow labels

Run it inside the app container or project environment after migrations:
  python scripts/seed_tryout_translations.py
"""

import sys
from typing import Dict

sys.path.insert(0, "/app")

from business_app import create_app, db  # noqa: E402
from business_app.models.translation import Translation  # noqa: E402


TranslationRecord = Dict[str, object]


TRANSLATIONS: Dict[str, TranslationRecord] = {
    "api.tryout.created": {
        "category": "api",
        "description": "Try-out created successfully",
        "translations": {
            "en": "Try-out created successfully",
            "uz": "Sinov muvaffaqiyatli yaratildi",
            "ru": "Пробная выдача успешно создана",
        },
    },
    "api.tryout.updated": {
        "category": "api",
        "description": "Try-out updated successfully",
        "translations": {
            "en": "Try-out updated successfully",
            "uz": "Sinov muvaffaqiyatli yangilandi",
            "ru": "Пробная выдача успешно обновлена",
        },
    },
    "api.tryout.converted": {
        "category": "api",
        "description": "Try-out converted to customer",
        "translations": {
            "en": "Try-out converted to customer successfully",
            "uz": "Sinov mijozga muvaffaqiyatli aylantirildi",
            "ru": "Пробная выдача успешно конвертирована в клиента",
        },
    },
    "api.tryout.converted_created_user": {
        "category": "api",
        "description": "Try-out converted by creating a new user",
        "translations": {
            "en": "Try-out converted and a new user was created",
            "uz": "Sinov aylantirildi va yangi foydalanuvchi yaratildi",
            "ru": "Пробная выдача конвертирована, создан новый пользователь",
        },
    },
    "api.tryout.converted_linked_existing_user": {
        "category": "api",
        "description": "Try-out converted by linking an existing user",
        "translations": {
            "en": "Try-out converted and linked to an existing user",
            "uz": "Sinov aylantirildi va mavjud foydalanuvchiga bog'landi",
            "ru": "Пробная выдача конвертирована и связана с существующим пользователем",
        },
    },
    "api.tryout.task_assigned": {
        "category": "api",
        "description": "Try-out task assigned",
        "translations": {
            "en": "Try-out task assigned successfully",
            "uz": "Sinov vazifasi muvaffaqiyatli biriktirildi",
            "ru": "Задача по пробной выдаче успешно назначена",
        },
    },
    "api.tryout.handoff_completed": {
        "category": "api",
        "description": "Try-out handoff completed",
        "translations": {
            "en": "Try-out handoff completed successfully",
            "uz": "Sinov topshiruvi muvaffaqiyatli yakunlandi",
            "ru": "Передача пробной выдачи успешно завершена",
        },
    },
    "api.tryout.pickup_recorded": {
        "category": "api",
        "description": "Try-out bottle pickup recorded",
        "translations": {
            "en": "Bottle pickup recorded successfully",
            "uz": "Butilkalar qaytarilishi muvaffaqiyatli qayd etildi",
            "ru": "Возврат тары успешно зафиксирован",
        },
    },
    "api.tryout.bottle_adjusted": {
        "category": "api",
        "description": "Try-out bottle ledger adjusted",
        "translations": {
            "en": "Bottle adjustment saved successfully",
            "uz": "Butilka tuzatishi muvaffaqiyatli saqlandi",
            "ru": "Корректировка тары успешно сохранена",
        },
    },
    "api.tryout.export_ready": {
        "category": "api",
        "description": "Try-out export generated",
        "translations": {
            "en": "Try-out export generated successfully",
            "uz": "Sinov eksporti muvaffaqiyatli yaratildi",
            "ru": "Экспорт пробных выдач успешно сформирован",
        },
    },
    "error.tryout.not_found": {
        "category": "error",
        "description": "Try-out not found",
        "translations": {
            "en": "Try-out not found",
            "uz": "Sinov topilmadi",
            "ru": "Пробная выдача не найдена",
        },
    },
    "error.tryout.task_not_found": {
        "category": "error",
        "description": "Try-out task not found",
        "translations": {
            "en": "Try-out task not found",
            "uz": "Sinov vazifasi topilmadi",
            "ru": "Задача по пробной выдаче не найдена",
        },
    },
    "error.tryout.address_required": {
        "category": "error",
        "description": "Try-out address is required",
        "translations": {
            "en": "Try-out address is required",
            "uz": "Sinov manzili majburiy",
            "ru": "Адрес пробной выдачи обязателен",
        },
    },
    "error.tryout.phone_required": {
        "category": "error",
        "description": "Trial contact phone is required",
        "translations": {
            "en": "Trial contact phone is required",
            "uz": "Sinov oluvchining telefon raqami majburiy",
            "ru": "Телефон получателя пробной выдачи обязателен",
        },
    },
    "error.tryout.ineligible_product": {
        "category": "error",
        "description": "Product not eligible for try-outs",
        "translations": {
            "en": "Selected product is not eligible for try-outs",
            "uz": "Tanlangan mahsulot sinov uchun ruxsat etilmagan",
            "ru": "Выбранный товар недоступен для пробной выдачи",
        },
    },
    "error.tryout.quantity_positive": {
        "category": "error",
        "description": "Try-out quantity must be positive",
        "translations": {
            "en": "Try-out item quantity must be positive",
            "uz": "Sinov mahsuloti miqdori musbat bo'lishi kerak",
            "ru": "Количество товара в пробной выдаче должно быть положительным",
        },
    },
    "error.tryout.pickup_task_required": {
        "category": "error",
        "description": "Pickup task required",
        "translations": {
            "en": "Task is not a pickup task",
            "uz": "Vazifa qaytarib olish vazifasi emas",
            "ru": "Задача не является задачей на возврат",
        },
    },
    "error.tryout.handoff_task_required": {
        "category": "error",
        "description": "Handoff task required",
        "translations": {
            "en": "Task is not a handoff task",
            "uz": "Vazifa topshirish vazifasi emas",
            "ru": "Задача не является задачей на передачу",
        },
    },
    "error.tryout.pickup_exceeds_outstanding": {
        "category": "error",
        "description": "Pickup exceeds outstanding bottles",
        "translations": {
            "en": "Pickup quantity exceeds outstanding bottles",
            "uz": "Qaytarilayotgan miqdor qolgan butilkalardan oshib ketdi",
            "ru": "Количество возврата превышает остаток невозвращенной тары",
        },
    },
    "error.tryout.no_returnables": {
        "category": "error",
        "description": "Pickup task cannot be created without returnables",
        "translations": {
            "en": "Pickup task cannot be created for a try-out without returnable bottles",
            "uz": "Qaytariladigan butilkalarsiz sinov uchun qaytarib olish vazifasi yaratib bo'lmaydi",
            "ru": "Нельзя создать задачу на возврат для пробной выдачи без возвратной тары",
        },
    },
    "error.tryout.completed_task_reassign": {
        "category": "error",
        "description": "Completed task cannot be reassigned",
        "translations": {
            "en": "Completed task cannot be reassigned",
            "uz": "Yakunlangan vazifani qayta biriktirib bo'lmaydi",
            "ru": "Завершенную задачу нельзя переназначить",
        },
    },
    "error.tryout.completed_task_accept": {
        "category": "error",
        "description": "Completed task cannot be accepted",
        "translations": {
            "en": "Completed task cannot be accepted",
            "uz": "Yakunlangan vazifani qabul qilib bo'lmaydi",
            "ru": "Завершенную задачу нельзя принять",
        },
    },
    "error.tryout.completed_pickup": {
        "category": "error",
        "description": "Pickup task already completed",
        "translations": {
            "en": "Pickup task already completed",
            "uz": "Qaytarib olish vazifasi allaqachon yakunlangan",
            "ru": "Задача на возврат уже завершена",
        },
    },
    "error.tryout.adjustment_negative": {
        "category": "error",
        "description": "Bottle adjustment cannot go negative",
        "translations": {
            "en": "Bottle adjustment would make outstanding quantity negative",
            "uz": "Butilka tuzatishi qolgan miqdorni manfiy qilib qo'yadi",
            "ru": "Корректировка сделает остаток тары отрицательным",
        },
    },
    "task.tryout.reminder_due_soon_title": {
        "category": "task",
        "description": "Due-soon reminder title",
        "translations": {
            "en": "Try-out bottle return due soon",
            "uz": "Sinov butilkalarini qaytarish muddati yaqin",
            "ru": "Скоро срок возврата тары по пробной выдаче",
        },
    },
    "task.tryout.reminder_due_soon_message": {
        "category": "task",
        "description": "Due-soon reminder message",
        "translations": {
            "en": "Try-out {tryout_number} is due on {return_due_at} with {outstanding_bottles_total} bottles still outstanding.",
            "uz": "{tryout_number} sinovi uchun {return_due_at} sanasida muddat tugaydi va hali {outstanding_bottles_total} ta butilka qaytmagan.",
            "ru": "По пробной выдаче {tryout_number} срок возврата {return_due_at}, невозвращено {outstanding_bottles_total} бутылей.",
        },
    },
    "task.tryout.reminder_overdue_title": {
        "category": "task",
        "description": "Overdue reminder title",
        "translations": {
            "en": "Try-out bottle return overdue",
            "uz": "Sinov butilkalarini qaytarish muddati o'tib ketdi",
            "ru": "Просрочен возврат тары по пробной выдаче",
        },
    },
    "task.tryout.reminder_overdue_message": {
        "category": "task",
        "description": "Overdue reminder message",
        "translations": {
            "en": "Try-out {tryout_number} is overdue since {return_due_at} with {outstanding_bottles_total} bottles still outstanding.",
            "uz": "{tryout_number} sinovi bo'yicha muddat {return_due_at} dan beri o'tgan va hali {outstanding_bottles_total} ta butilka qaytmagan.",
            "ru": "По пробной выдаче {tryout_number} просрочка с {return_due_at}, невозвращено {outstanding_bottles_total} бутылей.",
        },
    },
    "ui.tryouts.title": {
        "category": "ui_tryouts",
        "description": "Try-outs page title",
        "translations": {
            "en": "Try-outs",
            "uz": "Sinovlar",
            "ru": "Пробные выдачи",
        },
    },
    "ui.tryouts.subtitle": {
        "category": "ui_tryouts",
        "description": "Try-outs page subtitle",
        "translations": {
            "en": "Free product handoffs and returnable bottle recovery",
            "uz": "Bepul mahsulot topshirish va qaytariladigan butilkalarni yig'ish",
            "ru": "Бесплатные пробные выдачи и возврат оборотной тары",
        },
    },
    "ui.tryouts.actions.export_csv": {
        "category": "ui_tryouts",
        "description": "Export action label",
        "translations": {
            "en": "Export CSV",
            "uz": "CSV eksport",
            "ru": "Экспорт CSV",
        },
    },
    "ui.tryouts.actions.create": {
        "category": "ui_tryouts",
        "description": "Create try-out action label",
        "translations": {
            "en": "Create Try-out",
            "uz": "Sinov yaratish",
            "ru": "Создать пробную выдачу",
        },
    },
    "ui.tryouts.actions.edit": {
        "category": "ui_tryouts",
        "description": "Edit try-out action label",
        "translations": {
            "en": "Edit",
            "uz": "Tahrirlash",
            "ru": "Редактировать",
        },
    },
    "ui.tryouts.actions.assign": {
        "category": "ui_tryouts",
        "description": "Assign task action label",
        "translations": {
            "en": "Assign",
            "uz": "Biriktirish",
            "ru": "Назначить",
        },
    },
    "ui.tryouts.actions.convert": {
        "category": "ui_tryouts",
        "description": "Convert try-out action label",
        "translations": {
            "en": "Convert",
            "uz": "Mijozga aylantirish",
            "ru": "Конвертировать",
        },
    },
    "ui.tryouts.actions.adjust_bottles": {
        "category": "ui_tryouts",
        "description": "Adjust bottle ledger action label",
        "translations": {
            "en": "Adjust Bottles",
            "uz": "Butilkalarni tuzatish",
            "ru": "Скорректировать тару",
        },
    },
    "ui.tryouts.stats.active": {
        "category": "ui_tryouts",
        "description": "Active KPI label",
        "translations": {
            "en": "Active",
            "uz": "Faol",
            "ru": "Активные",
        },
    },
    "ui.tryouts.stats.outstanding_bottles": {
        "category": "ui_tryouts",
        "description": "Outstanding bottles KPI label",
        "translations": {
            "en": "Outstanding Bottles",
            "uz": "Qaytmagan butilkalar",
            "ru": "Невозвращенная тара",
        },
    },
    "ui.tryouts.stats.due_soon": {
        "category": "ui_tryouts",
        "description": "Due soon KPI label",
        "translations": {
            "en": "Due Soon",
            "uz": "Tez orada muddati keladi",
            "ru": "Скоро срок возврата",
        },
    },
    "ui.tryouts.stats.overdue": {
        "category": "ui_tryouts",
        "description": "Overdue KPI label",
        "translations": {
            "en": "Overdue",
            "uz": "Muddati o'tgan",
            "ru": "Просроченные",
        },
    },
    "ui.tryouts.stats.converted": {
        "category": "ui_tryouts",
        "description": "Converted KPI label",
        "translations": {
            "en": "Converted",
            "uz": "Mijozga aylangan",
            "ru": "Конвертированные",
        },
    },
    "ui.tryouts.stats.collection_rate": {
        "category": "ui_tryouts",
        "description": "Collection rate KPI label",
        "translations": {
            "en": "Collection Rate",
            "uz": "Qaytarish ulushi",
            "ru": "Доля возврата",
        },
    },
    "ui.tryouts.filters.search_placeholder": {
        "category": "ui_tryouts",
        "description": "Try-out search placeholder",
        "translations": {
            "en": "Search try-out / phone / name",
            "uz": "Sinov / telefon / ism bo'yicha qidirish",
            "ru": "Поиск по пробной выдаче / телефону / имени",
        },
    },
    "ui.tryouts.filters.status": {
        "category": "ui_tryouts",
        "description": "Status filter label",
        "translations": {
            "en": "Status",
            "uz": "Holat",
            "ru": "Статус",
        },
    },
    "ui.tryouts.filters.outcome": {
        "category": "ui_tryouts",
        "description": "Outcome filter label",
        "translations": {
            "en": "Outcome",
            "uz": "Natija",
            "ru": "Результат",
        },
    },
    "ui.tryouts.filters.pickup_state": {
        "category": "ui_tryouts",
        "description": "Pickup state filter label",
        "translations": {
            "en": "Pickup State",
            "uz": "Qaytarib olish holati",
            "ru": "Состояние возврата",
        },
    },
    "ui.tryouts.filters.driver": {
        "category": "ui_tryouts",
        "description": "Driver filter label",
        "translations": {
            "en": "Driver",
            "uz": "Haydovchi",
            "ru": "Водитель",
        },
    },
    "ui.tryouts.filters.created_range": {
        "category": "ui_tryouts",
        "description": "Created date range filter label",
        "translations": {
            "en": "Created Range",
            "uz": "Yaratilgan sana oralig'i",
            "ru": "Период создания",
        },
    },
    "ui.tryouts.filters.due_range": {
        "category": "ui_tryouts",
        "description": "Due date range filter label",
        "translations": {
            "en": "Due Range",
            "uz": "Qaytarish muddati oralig'i",
            "ru": "Период срока возврата",
        },
    },
    "ui.tryouts.table.tryout": {
        "category": "ui_tryouts",
        "description": "Try-out table column",
        "translations": {
            "en": "Try-out",
            "uz": "Sinov",
            "ru": "Пробная выдача",
        },
    },
    "ui.tryouts.table.contact": {
        "category": "ui_tryouts",
        "description": "Contact table column",
        "translations": {
            "en": "Contact",
            "uz": "Kontakt",
            "ru": "Контакт",
        },
    },
    "ui.tryouts.table.status": {
        "category": "ui_tryouts",
        "description": "Status table column",
        "translations": {
            "en": "Status",
            "uz": "Holat",
            "ru": "Статус",
        },
    },
    "ui.tryouts.table.outcome": {
        "category": "ui_tryouts",
        "description": "Outcome table column",
        "translations": {
            "en": "Outcome",
            "uz": "Natija",
            "ru": "Результат",
        },
    },
    "ui.tryouts.table.outstanding_bottles": {
        "category": "ui_tryouts",
        "description": "Outstanding bottles table column",
        "translations": {
            "en": "Outstanding Bottles",
            "uz": "Qaytmagan butilkalar",
            "ru": "Невозвращенная тара",
        },
    },
    "ui.tryouts.table.pickup_state": {
        "category": "ui_tryouts",
        "description": "Pickup state table column",
        "translations": {
            "en": "Pickup State",
            "uz": "Qaytarib olish holati",
            "ru": "Состояние возврата",
        },
    },
    "ui.tryouts.table.due": {
        "category": "ui_tryouts",
        "description": "Due table column",
        "translations": {
            "en": "Due",
            "uz": "Muddat",
            "ru": "Срок",
        },
    },
    "ui.tryouts.table.actions": {
        "category": "ui_tryouts",
        "description": "Actions table column",
        "translations": {
            "en": "Actions",
            "uz": "Amallar",
            "ru": "Действия",
        },
    },
    "ui.tryouts.tabs.overview": {
        "category": "ui_tryouts",
        "description": "Overview tab label",
        "translations": {
            "en": "Overview",
            "uz": "Umumiy ko'rinish",
            "ru": "Обзор",
        },
    },
    "ui.tryouts.tabs.products": {
        "category": "ui_tryouts",
        "description": "Products tab label",
        "translations": {
            "en": "Products",
            "uz": "Mahsulotlar",
            "ru": "Товары",
        },
    },
    "ui.tryouts.tabs.tasks": {
        "category": "ui_tryouts",
        "description": "Tasks tab label",
        "translations": {
            "en": "Tasks",
            "uz": "Vazifalar",
            "ru": "Задачи",
        },
    },
    "ui.tryouts.tabs.timeline": {
        "category": "ui_tryouts",
        "description": "Timeline tab label",
        "translations": {
            "en": "Timeline",
            "uz": "Tarix",
            "ru": "Хронология",
        },
    },
    "ui.tryouts.modals.create_title": {
        "category": "ui_tryouts",
        "description": "Create try-out modal title",
        "translations": {
            "en": "Create Try-out",
            "uz": "Sinov yaratish",
            "ru": "Создать пробную выдачу",
        },
    },
    "ui.tryouts.modals.edit_title": {
        "category": "ui_tryouts",
        "description": "Edit try-out modal title",
        "translations": {
            "en": "Edit Try-out",
            "uz": "Sinovni tahrirlash",
            "ru": "Редактировать пробную выдачу",
        },
    },
    "ui.tryouts.modals.assign_title": {
        "category": "ui_tryouts",
        "description": "Assign try-out task modal title",
        "translations": {
            "en": "Assign Try-out Task",
            "uz": "Sinov vazifasini biriktirish",
            "ru": "Назначить задачу по пробной выдаче",
        },
    },
    "ui.tryouts.modals.adjust_title": {
        "category": "ui_tryouts",
        "description": "Adjust bottle ledger modal title",
        "translations": {
            "en": "Adjust Bottle Ledger",
            "uz": "Butilka hisobini tuzatish",
            "ru": "Скорректировать учет тары",
        },
    },
    "staff.menu.tryout_tasks": {
        "category": "staff_bot",
        "description": "Staff bot menu label for try-out tasks",
        "translations": {
            "en": "Try-out Tasks",
            "uz": "Sinov vazifalari",
            "ru": "Задачи по пробным выдачам",
        },
    },
    "staff.menu.create_tryout_now": {
        "category": "staff_bot",
        "description": "Staff bot menu label for instant try-out creation",
        "translations": {
            "en": "Create Try-out Now",
            "uz": "Hozir sinov yaratish",
            "ru": "Создать пробную выдачу",
        },
    },
    "staff.menu.active_tryouts": {
        "category": "staff_bot",
        "description": "Staff bot menu label for active try-outs",
        "translations": {
            "en": "Active Try-outs",
            "uz": "Faol sinovlar",
            "ru": "Активные пробные выдачи",
        },
    },
    "staff.tryout.tasks_title": {
        "category": "staff_bot",
        "description": "Try-out task pool title",
        "translations": {
            "en": "Try-out Task Pool",
            "uz": "Sinov vazifalari",
            "ru": "Список задач по пробным выдачам",
        },
    },
    "staff.tryout.no_tasks": {
        "category": "staff_bot",
        "description": "No try-out tasks message",
        "translations": {
            "en": "No try-out tasks are available right now.",
            "uz": "Hozircha sinov vazifalari yo'q.",
            "ru": "Сейчас нет задач по пробным выдачам.",
        },
    },
    "staff.tryout.active_title": {
        "category": "staff_bot",
        "description": "Active try-outs list title",
        "translations": {
            "en": "My Active Try-outs",
            "uz": "Mening faol sinovlarim",
            "ru": "Мои активные пробные выдачи",
        },
    },
    "staff.tryout.no_active": {
        "category": "staff_bot",
        "description": "No active try-outs message",
        "translations": {
            "en": "No active try-outs with outstanding bottles.",
            "uz": "Qaytarilishi kerak bo'lgan idishlari bor faol sinovlar yo'q.",
            "ru": "Нет активных пробных выдач с невозвращенной тарой.",
        },
    },
    "staff.tryout.outstanding": {
        "category": "staff_bot",
        "description": "Outstanding bottles label",
        "translations": {
            "en": "Outstanding bottles",
            "uz": "Qaytishi kerak bo'lgan butilkalar",
            "ru": "Невозвращенная тара",
        },
    },
    "staff.tryout.accept_task": {
        "category": "staff_bot",
        "description": "Accept try-out task action",
        "translations": {
            "en": "Accept Task",
            "uz": "Vazifani olish",
            "ru": "Принять задачу",
        },
    },
    "staff.tryout.complete_handoff": {
        "category": "staff_bot",
        "description": "Complete handoff action",
        "translations": {
            "en": "Complete Handoff",
            "uz": "Topshirishni yakunlash",
            "ru": "Завершить передачу",
        },
    },
    "staff.tryout.record_pickup": {
        "category": "staff_bot",
        "description": "Record pickup action",
        "translations": {
            "en": "Record Pickup",
            "uz": "Qaytarishni kiritish",
            "ru": "Зафиксировать возврат",
        },
    },
    "staff.tryout.view_tryout": {
        "category": "staff_bot",
        "description": "View try-out action",
        "translations": {
            "en": "View Try-out",
            "uz": "Sinovni ko'rish",
            "ru": "Открыть пробную выдачу",
        },
    },
    "staff.tryout.pickup_prompt": {
        "category": "staff_bot",
        "description": "Pickup quantity input prompt",
        "translations": {
            "en": "Send returned bottle quantities one per line.",
            "uz": "Qaytgan butilkalarni har qatorda yuboring.",
            "ru": "Отправьте возвращенную тару по одной строке.",
        },
    },
    "staff.tryout.pickup_recorded": {
        "category": "staff_bot",
        "description": "Pickup recorded confirmation",
        "translations": {
            "en": "Bottle pickup recorded.",
            "uz": "Butilka qaytarilishi qayd etildi.",
            "ru": "Возврат тары зафиксирован.",
        },
    },
    "staff.tryout.pickup_select_product": {
        "category": "staff_bot",
        "description": "Pickup overview prompt",
        "translations": {
            "en": "Choose a product and then tap the returned quantity.",
            "uz": "Mahsulotni tanlang, keyin qaytgan miqdorni bosing.",
            "ru": "Выберите товар, затем нажмите количество возвращенной тары.",
        },
    },
    "staff.tryout.pickup_selected": {
        "category": "staff_bot",
        "description": "Pickup overview selected quantity label",
        "translations": {
            "en": "selected: {selected}",
            "uz": "tanlandi: {selected}",
            "ru": "выбрано: {selected}",
        },
    },
    "staff.tryout.pickup_not_selected": {
        "category": "staff_bot",
        "description": "Pickup overview not selected label",
        "translations": {
            "en": "not selected yet",
            "uz": "hali tanlanmagan",
            "ru": "пока не выбрано",
        },
    },
    "staff.tryout.pickup_select_quantity": {
        "category": "staff_bot",
        "description": "Pickup quantity selection prompt",
        "translations": {
            "en": "Select how many bottles were returned for {product}.",
            "uz": "{product} uchun nechta butilka qaytganini tanlang.",
            "ru": "Выберите, сколько бутылей вернули по товару {product}.",
        },
    },
    "staff.tryout.pickup_current_quantity": {
        "category": "staff_bot",
        "description": "Pickup quantity status message",
        "translations": {
            "en": "Selected now: {quantity} of {outstanding}",
            "uz": "Hozir tanlangan: {quantity} / {outstanding}",
            "ru": "Сейчас выбрано: {quantity} из {outstanding}",
        },
    },
    "staff.tryout.pickup_submit": {
        "category": "staff_bot",
        "description": "Pickup submit button label",
        "translations": {
            "en": "Record Selected Bottles",
            "uz": "Tanlangan butilkalarni qayd etish",
            "ru": "Зафиксировать выбранную тару",
        },
    },
    "staff.tryout.pickup_clear_selection": {
        "category": "staff_bot",
        "description": "Pickup clear selection button label",
        "translations": {
            "en": "Clear Selection",
            "uz": "Tanlovni tozalash",
            "ru": "Очистить выбор",
        },
    },
    "staff.tryout.pickup_fill_all": {
        "category": "staff_bot",
        "description": "Pickup fill-all button label",
        "translations": {
            "en": "Fill All Outstanding",
            "uz": "Barchasini to'ldirish",
            "ru": "Заполнить весь остаток",
        },
    },
    "staff.tryout.pickup_clear_product": {
        "category": "staff_bot",
        "description": "Pickup clear current product button label",
        "translations": {
            "en": "Remove This Product",
            "uz": "Bu mahsulotni olib tashlash",
            "ru": "Убрать этот товар",
        },
    },
    "staff.tryout.pickup_nothing_selected": {
        "category": "staff_bot",
        "description": "Pickup submit without selection warning",
        "translations": {
            "en": "Select at least one returned quantity first.",
            "uz": "Avval kamida bitta qaytgan miqdorni tanlang.",
            "ru": "Сначала выберите хотя бы одно возвращенное количество.",
        },
    },
    "staff.tryout.pickup_no_outstanding": {
        "category": "staff_bot",
        "description": "Pickup requested when nothing is outstanding",
        "translations": {
            "en": "There are no outstanding bottles left for this try-out.",
            "uz": "Bu sinov bo'yicha qaytishi kerak bo'lgan butilkalar qolmagan.",
            "ru": "По этой пробной выдаче не осталось невозвращенной тары.",
        },
    },
    "staff.tryout.pickup_use_buttons": {
        "category": "staff_bot",
        "description": "Pickup text-entry nudge",
        "translations": {
            "en": "Use the buttons below to record bottle returns.",
            "uz": "Butilka qaytarilishini qayd etish uchun pastdagi tugmalardan foydalaning.",
            "ru": "Используйте кнопки ниже, чтобы зафиксировать возврат тары.",
        },
    },
    "staff.tryout.enter_phone": {
        "category": "staff_bot",
        "description": "Create try-out phone prompt",
        "translations": {
            "en": "Enter the customer's phone number.",
            "uz": "Mijozning telefon raqamini kiriting.",
            "ru": "Введите номер телефона клиента.",
        },
    },
    "staff.tryout.enter_name": {
        "category": "staff_bot",
        "description": "Create try-out name prompt",
        "translations": {
            "en": "Enter the customer's first name.",
            "uz": "Mijozning ismini kiriting.",
            "ru": "Введите имя клиента.",
        },
    },
    "staff.tryout.enter_address": {
        "category": "staff_bot",
        "description": "Create try-out address prompt",
        "translations": {
            "en": "Enter the try-out delivery address.",
            "uz": "Sinov topshiriladigan manzilni kiriting.",
            "ru": "Введите адрес пробной выдачи.",
        },
    },
    "staff.tryout.enter_address_or_location": {
        "category": "staff_bot",
        "description": "Create try-out address or location prompt",
        "translations": {
            "en": "Enter the try-out delivery address or send your location.",
            "uz": "Sinov topshiriladigan manzilni kiriting yoki joylashuvingizni yuboring.",
            "ru": "Введите адрес пробной выдачи или отправьте геолокацию.",
        },
    },
    "staff.tryout.send_location": {
        "category": "staff_bot",
        "description": "Send location button label",
        "translations": {
            "en": "Send Location",
            "uz": "Joylashuvni yuborish",
            "ru": "Отправить геолокацию",
        },
    },
    "staff.tryout.address_received": {
        "category": "staff_bot",
        "description": "Address accepted before product selection",
        "translations": {
            "en": "Address saved. Now choose the try-out products.",
            "uz": "Manzil saqlandi. Endi sinov mahsulotlarini tanlang.",
            "ru": "Адрес сохранен. Теперь выберите товары для пробной выдачи.",
        },
    },
    "staff.tryout.location_received": {
        "category": "staff_bot",
        "description": "Location reverse geocoded successfully",
        "translations": {
            "en": "Location received: {address}",
            "uz": "Joylashuv qabul qilindi: {address}",
            "ru": "Геолокация получена: {address}",
        },
    },
    "staff.tryout.outside_delivery_area": {
        "category": "staff_bot",
        "description": "Shared try-out location is outside the delivery coverage area",
        "translations": {
            "en": "⚠️ This location is outside the delivery area (Tashkent). Please share a location within the service area or type the address.",
            "uz": "⚠️ Bu joylashuv yetkazib berish hududidan (Toshkent) tashqarida. Iltimos, xizmat hududidagi joylashuvni yuboring yoki manzilni yozing.",
            "ru": "⚠️ Это местоположение вне зоны доставки (Ташкент). Отправьте местоположение в пределах зоны обслуживания или введите адрес.",
        },
    },
    "staff.tryout.location_geocode_failed": {
        "category": "staff_bot",
        "description": "Location reverse geocode failed",
        "translations": {
            "en": "Location received, but the address could not be resolved. Please type the address manually.",
            "uz": "Joylashuv qabul qilindi, lekin manzil aniqlanmadi. Iltimos, manzilni qo'lda kiriting.",
            "ru": "Геолокация получена, но адрес определить не удалось. Пожалуйста, введите адрес вручную.",
        },
    },
    "staff.tryout.select_products": {
        "category": "staff_bot",
        "description": "Create try-out product selection prompt",
        "translations": {
            "en": "Select try-out products.",
            "uz": "Sinov mahsulotlarini tanlang.",
            "ru": "Выберите товары для пробной выдачи.",
        },
    },
    "staff.tryout.select_quantity": {
        "category": "staff_bot",
        "description": "Create try-out quantity prompt",
        "translations": {
            "en": "Select quantity for {product}.",
            "uz": "{product} uchun miqdorni tanlang.",
            "ru": "Выберите количество для {product}.",
        },
    },
    "staff.tryout.current_quantity": {
        "category": "staff_bot",
        "description": "Current selected quantity for a create-flow product",
        "translations": {
            "en": "Current quantity: {quantity}",
            "uz": "Hozirgi miqdor: {quantity}",
            "ru": "Текущее количество: {quantity}",
        },
    },
    "staff.tryout.selected_products": {
        "category": "staff_bot",
        "description": "Selected products heading",
        "translations": {
            "en": "Selected products",
            "uz": "Tanlangan mahsulotlar",
            "ru": "Выбранные товары",
        },
    },
    "staff.tryout.done_selecting": {
        "category": "staff_bot",
        "description": "Done selecting button label",
        "translations": {
            "en": "Done Selecting",
            "uz": "Tanlashni yakunlash",
            "ru": "Завершить выбор",
        },
    },
    "staff.tryout.add_more_products": {
        "category": "staff_bot",
        "description": "Add more products button label",
        "translations": {
            "en": "Add More Products",
            "uz": "Yana mahsulot qo'shish",
            "ru": "Добавить еще товары",
        },
    },
    "staff.tryout.remove_product": {
        "category": "staff_bot",
        "description": "Remove selected product button label",
        "translations": {
            "en": "Remove Product",
            "uz": "Mahsulotni olib tashlash",
            "ru": "Убрать товар",
        },
    },
    "staff.tryout.confirm_create_title": {
        "category": "staff_bot",
        "description": "Confirm create try-out title",
        "translations": {
            "en": "Confirm Try-out",
            "uz": "Sinovni tasdiqlash",
            "ru": "Подтвердите пробную выдачу",
        },
    },
    "staff.tryout.created_success": {
        "category": "staff_bot",
        "description": "Try-out created success message",
        "translations": {
            "en": "Try-out created successfully: {tryout_number}",
            "uz": "Sinov muvaffaqiyatli yaratildi: {tryout_number}",
            "ru": "Пробная выдача создана: {tryout_number}",
        },
    },
}


def upsert_translation(
    key: str,
    language: str,
    value: str,
    *,
    category: str,
    description: str,
) -> str:
    existing = Translation.query.filter_by(key=key, language=language).first()
    if existing:
        changed = (
            existing.value != value
            or existing.category != category
            or existing.description != description
            or not existing.is_active
        )
        if changed:
            existing.value = value
            existing.category = category
            existing.description = description
            existing.is_active = True
            return "updated"
        return "skipped"

    db.session.add(
        Translation(
            key=key,
            language=language,
            value=value,
            category=category,
            description=description,
            is_active=True,
        )
    )
    return "created"


def main() -> None:
    app = create_app()

    with app.app_context():
        created = 0
        updated = 0
        skipped = 0

        print("=" * 72)
        print("TRY-OUT FEATURE TRANSLATION SEEDING")
        print("=" * 72)
        print(f"Processing {len(TRANSLATIONS)} translation keys")
        print()

        for key, record in TRANSLATIONS.items():
            category = str(record["category"])
            description = str(record["description"])
            translations = record["translations"]
            print(f"  Key: {key} [{category}]")

            for language, value in translations.items():
                result = upsert_translation(
                    key,
                    language,
                    value,
                    category=category,
                    description=description,
                )
                if result == "created":
                    created += 1
                elif result == "updated":
                    updated += 1
                else:
                    skipped += 1

        db.session.commit()

        print()
        print("=" * 72)
        print("TRY-OUT FEATURE TRANSLATION SEEDING COMPLETED")
        print("=" * 72)
        print(f"Keys processed: {len(TRANSLATIONS)}")
        print(f"Translations created: {created}")
        print(f"Translations updated: {updated}")
        print(f"Translations unchanged: {skipped}")


if __name__ == "__main__":
    main()
