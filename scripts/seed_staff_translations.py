#!/usr/bin/env python3
"""
Seed staff bot translations into the database.

This script:
1. Collects literal i18n keys used in staff_bot code.
2. Adds dynamic key families used via f-strings.
3. Upserts translations with category='staff_bot' for en/uz/ru.
"""

import re
import sys
from pathlib import Path
from typing import Dict, Optional, Set

# Match existing project seeding scripts.
sys.path.insert(0, '/app')

from business_app import create_app, db  # noqa: E402
from business_app.models.translation import Translation  # noqa: E402
from shared.staff_constants import FAILED_DELIVERY_REASONS, STAFF_BOT_ROLES  # noqa: E402
from shared.enums import OrderStatus, PaymentMethod  # noqa: E402


LANGUAGES = ("en", "uz", "ru")
RU_ALLOWED_LATIN_TOKENS = ("BlueStream", "Payme", "Click", "UZS", "API", "COD")


# Curated high-value strings.
STAFF_TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "staff.menu.title": {
        "en": "Staff Bot - Main Menu",
        "uz": "Xodimlar boti - Asosiy menyu",
        "ru": "Бот сотрудников Aqua Element - Главное меню",
    },
    "staff.menu.new_orders": {
        "en": "New Orders",
        "uz": "Yangi buyurtmalar",
        "ru": "Новые заказы",
    },
    "staff.menu.new_orders_view": {
        "en": "New Orders (View)",
        "uz": "Yangi buyurtmalar (Korish)",
        "ru": "Новые заказы (просмотр)",
    },
    "staff.menu.active_deliveries": {
        "en": "My Active Deliveries",
        "uz": "Mening faol yetkazishlarim",
        "ru": "Мои активные доставки",
    },
    "staff.menu.delivery_history": {
        "en": "Delivery History",
        "uz": "Yetkazish tarixi",
        "ru": "История доставок",
    },
    "staff.menu.tryout_tasks": {
        "en": "Try-out Tasks",
        "uz": "Sinov vazifalari",
        "ru": "Задачи по пробным выдачам",
    },
    "staff.menu.create_tryout_now": {
        "en": "Create Try-out Now",
        "uz": "Hozir sinov yaratish",
        "ru": "Создать пробную выдачу",
    },
    "staff.menu.active_tryouts": {
        "en": "Active Try-outs",
        "uz": "Faol sinovlar",
        "ru": "Активные пробные выдачи",
    },
    "staff.menu.my_stats": {
        "en": "My Stats",
        "uz": "Mening statistikam",
        "ru": "Моя статистика",
    },
    "staff.menu.cash_reconciliation": {
        "en": "Cash Reconciliation",
        "uz": "Naqd pul yarashtiruvi",
        "ru": "Сверка наличных",
    },
    "staff.menu.create_client": {
        "en": "Create Client",
        "uz": "Mijoz yaratish",
        "ru": "Создать клиента",
    },
    "staff.menu.create_order": {
        "en": "Create Order",
        "uz": "Buyurtma yaratish",
        "ru": "Создать заказ",
    },
    "staff.menu.search_client": {
        "en": "Search Client",
        "uz": "Mijoz qidirish",
        "ru": "Поиск клиента",
    },
    "staff.menu.recent_orders": {
        "en": "Recent Orders",
        "uz": "Songgi buyurtmalar",
        "ru": "Последние заказы",
    },
    "staff.menu.profile": {
        "en": "Profile",
        "uz": "Profil",
        "ru": "Профиль",
    },
    "staff.menu.settings": {
        "en": "Settings",
        "uz": "Sozlamalar",
        "ru": "Настройки",
    },
    "staff.menu.help": {
        "en": "Help",
        "uz": "Yordam",
        "ru": "Помощь",
    },
    "staff.menu.tryouts": {
        "en": "Try-outs",
        "uz": "Sinovlar",
        "ru": "Пробные выдачи",
    },
    "staff.menu.cash": {
        "en": "Cash",
        "uz": "Naqd pul",
        "ru": "Наличные",
    },
    "staff.menu.collect_cod_debt": {
        "en": "Collect COD Debt",
        "uz": "COD qarzni yigish",
        "ru": "Сбор долга COD",
    },
    "staff.tryouts.hub_title": {
        "en": "Try-outs",
        "uz": "Sinovlar",
        "ru": "Пробные выдачи",
    },
    "staff.tryouts.create": {
        "en": "Create Try-out",
        "uz": "Sinov yaratish",
        "ru": "Создать пробную выдачу",
    },
    "staff.cash.hub_title": {
        "en": "Cash",
        "uz": "Naqd pul",
        "ru": "Наличные",
    },
    "staff.profile.view_stats": {
        "en": "My Stats",
        "uz": "Mening statistikam",
        "ru": "Моя статистика",
    },
    "staff.profile.view_history": {
        "en": "Delivery History",
        "uz": "Yetkazish tarixi",
        "ru": "История доставок",
    },
    "staff.profile.view_recent_orders": {
        "en": "Recent Orders",
        "uz": "Songgi buyurtmalar",
        "ru": "Последние заказы",
    },
    "staff.operator.pool_title": {
        "en": "Order Pool (View Only)",
        "uz": "Buyurtmalar havzasi (Faqat korish)",
        "ru": "Список заказов (только просмотр)",
    },
    "staff.operator.assigned_to": {
        "en": "Assigned To",
        "uz": "Biriktirilgan",
        "ru": "Назначен",
    },
    "staff.operator.address": {
        "en": "Address",
        "uz": "Manzil",
        "ru": "Адрес",
    },
    "staff.operator.payment_cash": {
        "en": "Cash",
        "uz": "Naqd pul",
        "ru": "Наличные",
    },
    "staff.operator.payment_payme": {
        "en": "Payme",
        "uz": "Payme",
        "ru": "Payme",
    },
    "staff.operator.payment_click": {
        "en": "Click",
        "uz": "Click",
        "ru": "Click",
    },
    "staff.operator.payment_business_account": {
        "en": "Business Account",
        "uz": "Hisob raqami orqali",
        "ru": "Безналичный счёт",
    },
    "staff.operator.cod_restricted": {
        "en": "Cash on delivery is unavailable for this customer until earlier COD debts are settled.",
        "uz": "Avvalgi COD qarzlari yopilmaguncha bu mijoz uchun yetkazib berishda naqd tolov mavjud emas.",
        "ru": "Оплата наличными при доставке недоступна для этого клиента, пока не будут погашены прежние долги COD.",
    },
    "staff.operator.payment_unavailable": {
        "en": "This payment method is not available for the selected customer.",
        "uz": "Tanlangan mijoz uchun bu tolov usuli mavjud emas.",
        "ru": "Этот способ оплаты недоступен для выбранного клиента.",
    },
    "staff.back": {"en": "Back", "uz": "Orqaga", "ru": "Назад"},
    "staff.confirm": {"en": "Confirm", "uz": "Tasdiqlash", "ru": "Подтвердить"},
    "staff.cancel": {"en": "Cancel", "uz": "Bekor qilish", "ru": "Отмена"},
    "staff.cancelled": {"en": "Cancelled", "uz": "Bekor qilindi", "ru": "Отменено"},
    "staff.yes": {"en": "Yes", "uz": "Ha", "ru": "Да"},
    "staff.no": {"en": "No", "uz": "Yoq", "ru": "Нет"},
    "staff.page": {"en": "Page", "uz": "Sahifa", "ru": "Страница"},
    "staff.common.not_available": {
        "en": "N/A",
        "uz": "Mavjud emas",
        "ru": "Недоступно",
    },
    "staff.currency.uzs": {
        "en": "UZS",
        "uz": "som",
        "ru": "сум",
    },
    "staff.unit.minutes": {
        "en": "min",
        "uz": "daq",
        "ru": "мин",
    },
    "staff.error_occurred": {
        "en": "An error occurred. Please try again.",
        "uz": "Xatolik yuz berdi. Qayta urinib koring.",
        "ru": "Произошла ошибка. Попробуйте снова.",
    },
    "staff.session_expired": {
        "en": "Session expired. Please login again.",
        "uz": "Sessiya tugadi. Qayta kiring.",
        "ru": "Сессия истекла. Войдите снова.",
    },
    "staff.unauthorized": {
        "en": "You are not allowed to perform this action.",
        "uz": "Bu amal uchun sizda ruxsat yoq.",
        "ru": "У вас нет прав для этого действия.",
    },
    "staff.select_language": {
        "en": "Select language",
        "uz": "Tilni tanlang",
        "ru": "Выберите язык",
    },
    "staff.language_changed": {
        "en": "Language updated.",
        "uz": "Til yangilandi.",
        "ru": "Язык обновлен.",
    },
    "staff.welcome_intro": {
        "en": "Welcome to Aqua Element Staff Bot!\n\nPlease select your language:",
        "uz": "Aqua Element xodimlar botiga xush kelibsiz!\n\nIltimos, tilni tanlang:",
        "ru": "Добро пожаловать в бот сотрудников Aqua Element!\n\nПожалуйста, выберите язык:",
    },
    "staff.notification.new_order": {
        "en": "New order available!",
        "uz": "Yangi buyurtma mavjud!",
        "ru": "Доступен новый заказ!",
    },
    "staff.notification.order_assigned": {
        "en": "Order #{number} has been assigned to you.",
        "uz": "#{number} buyurtma sizga biriktirildi.",
        "ru": "Заказ #{number} назначен вам.",
    },
    "staff.notification.order_reassigned_from": {
        "en": "Order #{number} was reassigned.",
        "uz": "#{number} buyurtma qayta biriktirildi.",
        "ru": "Заказ #{number} был переназначен.",
    },
    "staff.notification.order_cancelled": {
        "en": "Order #{number} was cancelled.",
        "uz": "#{number} buyurtma bekor qilindi.",
        "ru": "Заказ #{number} отменен.",
    },
    "staff.delivery.next_stop": {
        "en": "Next stop",
        "uz": "Keyingi manzil",
        "ru": "Следующая остановка",
    },
    "staff.delivery.eta_minutes": {
        "en": "ETA: {minutes} min",
        "uz": "Borish vaqti: {minutes} daq",
        "ru": "Прибытие: {minutes} мин",
    },
    "staff.delivery.distance_km": {
        "en": "{km} km away",
        "uz": "{km} km uzoqlikda",
        "ru": "{km} км до точки",
    },
    "staff.delivery.optimize_routes_button": {
        "en": "Optimize routes",
        "uz": "Yo'nalishni qayta hisoblash",
        "ru": "Оптимизировать маршрут",
    },
    "staff.delivery.route_updated_toast": {
        "en": "Route updated",
        "uz": "Yo'nalish yangilandi",
        "ru": "Маршрут обновлён",
    },
    "staff.delivery.share_location_prompt": {
        "en": "Tap the button below to share your current location — one tap is enough, you don't need live location. Share again whenever you accept a new order so the route stays accurate.",
        "uz": "Joriy joylashuvingizni yuborish uchun pastdagi tugmani bosing — bir marta bosish yetarli, jonli joylashuv shart emas. Yangi buyurtmani qabul qilganingizda yo'nalish aniq qolishi uchun qaytadan ulashing.",
        "ru": "Нажмите кнопку ниже, чтобы отправить текущую геопозицию — одного нажатия достаточно, живая геопозиция не нужна. Отправляйте снова после принятия каждого нового заказа, чтобы маршрут оставался актуальным.",
    },
    "staff.delivery.share_location_after_accept": {
        "en": "📍 Order accepted! Share your current location now so we can recalculate the optimal route from where you are.",
        "uz": "📍 Buyurtma qabul qilindi! Eng yaxshi yo'nalishni siz turgan joydan qayta hisoblashimiz uchun joriy joylashuvingizni yuboring.",
        "ru": "📍 Заказ принят! Отправьте текущую геопозицию, чтобы мы пересчитали оптимальный маршрут от вашего нынешнего местоположения.",
    },
    "staff.delivery.share_location_button": {
        "en": "Share location",
        "uz": "Joylashuvni yuborish",
        "ru": "Отправить геопозицию",
    },
    "staff.delivery.location_stale_notice": {
        "en": "Share your live location for better suggestions",
        "uz": "Yaxshiroq taklif uchun jonli joylashuvni yoqing",
        "ru": "Включите трансляцию геопозиции для точных подсказок",
    },
    "staff.delivery.pool_insertion_offer": {
        "en": "Order #{order_no} fits your route (+{km} km, +{minutes} min). Accept?",
        "uz": "#{order_no} buyurtma sizning yo'nalishingizga mos keladi (+{km} km, +{minutes} daq). Qabul qilasizmi?",
        "ru": "Заказ #{order_no} вписывается в ваш маршрут (+{km} км, +{minutes} мин). Принять?",
    },
    "staff.delivery.suggestion_declined": {
        "en": "Suggestion dismissed",
        "uz": "Taklif rad etildi",
        "ru": "Предложение отклонено",
    },
    "staff.delivery.location_required_notice": {
        "en": "Share your location to get the optimal delivery order",
        "uz": "Eng yaxshi yetkazib berish tartibini olish uchun joylashuvingizni yuboring",
        "ru": "Отправьте свою геопозицию, чтобы получить оптимальный порядок доставок",
    },
    "staff.delivery.share_location_first_toast": {
        "en": "Please share your location first — without it we can't compute the route",
        "uz": "Avval joylashuvingizni yuboring — usiz yo'nalishni hisoblay olmaymiz",
        "ru": "Сначала отправьте свою геопозицию — без неё мы не можем рассчитать маршрут",
    },
    "staff.delivery.location_received": {
        "en": "Location received",
        "uz": "Joylashuv qabul qilindi",
        "ru": "Геопозиция получена",
    },
    "staff.delivery.route_recalculated": {
        "en": "Route has been recalculated based on your current position.",
        "uz": "Yo'nalish joriy joylashuvingiz asosida qayta hisoblandi.",
        "ru": "Маршрут пересчитан на основе вашего текущего положения.",
    },
    "staff.delivery.tap_to_see_optimized": {
        "en": "Tap to see the optimized order:",
        "uz": "Optimallashtirilgan tartibni ko'rish uchun bosing:",
        "ru": "Нажмите, чтобы увидеть оптимизированный порядок:",
    },
    "staff.delivery.location_update_failed": {
        "en": "Couldn't save your location, please try again",
        "uz": "Joylashuvingizni saqlay olmadik, qayta urinib ko'ring",
        "ru": "Не удалось сохранить вашу геопозицию, попробуйте ещё раз",
    },
    "staff.delivery.cash_collected_label": {
        "en": "Collected",
        "uz": "Yigildi",
        "ru": "Собрано",
    },
    "staff.delivery.cash_outstanding_label": {
        "en": "Outstanding",
        "uz": "Qoldiq qarz",
        "ru": "Остаток долга",
    },
    "staff.delivery.no_cash_collected": {
        "en": "No cash collected",
        "uz": "Naqd pul olinmadi",
        "ru": "Наличные не получены",
    },
    "staff.delivery.enter_no_cash_reason": {
        "en": "Enter why no cash was collected for this delivery.",
        "uz": "Bu yetkazib berishda nima uchun naqd pul olinmaganini kiriting.",
        "ru": "Укажите, почему наличные не были получены по этой доставке.",
    },
    "staff.delivery.enter_partial_cash_reason": {
        "en": "Enter a note explaining the partial cash collection.",
        "uz": "Qisman olingan naqd pul uchun izoh kiriting.",
        "ru": "Укажите примечание по частичному получению наличных.",
    },
    "staff.delivery.note_required": {
        "en": "A note is required for this cash exception.",
        "uz": "Bu naqd pul istisnosi uchun izoh majburiy.",
        "ru": "Для этого исключения по наличным требуется примечание.",
    },
    "staff.delivery.submit_reconciliation": {
        "en": "Submit Reconciliation",
        "uz": "Yarashtiruvni yuborish",
        "ru": "Отправить сверку",
    },
    "staff.delivery.handoff_expected_cash": {
        "en": "Handoff all expected cash",
        "uz": "Kutilgan naqdning hammasini topshirish",
        "ru": "Сдать всю ожидаемую наличность",
    },
    "staff.delivery.edit_reconciliation_cash": {
        "en": "Enter different amount",
        "uz": "Boshqa summani kiritish",
        "ru": "Ввести другую сумму",
    },
    "staff.delivery.enter_declared_cash": {
        "en": "Enter the counted cash amount only if it differs from expected cash.",
        "uz": "Faqat kutilgan naqd puldan farq qilsa, sanalgan summani kiriting.",
        "ru": "Введите пересчитанную сумму только если она отличается от ожидаемой.",
    },
    "staff.delivery.reconciliation_submitted": {
        "en": "Reconciliation submitted.",
        "uz": "Yarashtiruv yuborildi.",
        "ru": "Сверка отправлена.",
    },
    "staff.delivery.handoff_remaining_cash": {
        "en": "Submit remaining {amount}",
        "uz": "Qolgan {amount} ni topshirish",
        "ru": "Сдать оставшиеся {amount}",
    },
    "staff.delivery.remaining_to_submit": {
        "en": "Remaining to submit",
        "uz": "Topshiriladigan qolgan summa",
        "ru": "Осталось сдать",
    },
    "staff.delivery.reconciliation_partial_recorded": {
        "en": "Partial handoff recorded. The session stays open until the remaining cash is submitted.",
        "uz": "Qisman topshirish yozib olindi. Qolgan summa topshirilmaguncha sessiya ochiq qoladi.",
        "ru": "Частичная сдача записана. Сессия остаётся открытой, пока не сдадите остаток.",
    },
    "staff.delivery.expected_cash_label": {
        "en": "Expected cash",
        "uz": "Kutilgan naqd pul",
        "ru": "Ожидаемые наличные",
    },
    "staff.delivery.expected_cash_on_hand_label": {
        "en": "Expected cash on hand",
        "uz": "Qo'lda bo'lishi kerak bo'lgan naqd pul",
        "ru": "Ожидаемая сумма на руках",
    },
    "staff.delivery.declared_cash_label": {
        "en": "Declared cash",
        "uz": "Topshirilgan naqd pul",
        "ru": "Заявленные наличные",
    },
    "staff.delivery.cash_variance_label": {
        "en": "Variance",
        "uz": "Farq",
        "ru": "Расхождение",
    },
    "staff.delivery.session_age_days": {
        "en": "Session age: {days} day(s)",
        "uz": "Sessiya yoshi: {days} kun",
        "ru": "Возраст сессии: {days} дн.",
    },
    "staff.delivery.reconciliation_warning_due": {
        "en": "This cash session is 7+ days old. Please hand off cash when possible.",
        "uz": "Bu naqd sessiya 7 kundan oshdi. Imkon bo'lsa naqdni topshiring.",
        "ru": "Этой сессии наличных 7+ дней. По возможности сдайте наличные.",
    },
    "staff.command.start": {
        "en": "Start bot and authenticate",
        "uz": "Botni ishga tushirish va kirish",
        "ru": "Запустить бота и войти",
    },
    "staff.command.menu": {
        "en": "Show main menu",
        "uz": "Asosiy menyuni korsatish",
        "ru": "Показать главное меню",
    },
    "staff.command.help": {
        "en": "Show help",
        "uz": "Yordamni korsatish",
        "ru": "Показать помощь",
    },
    "staff.command.language": {
        "en": "Change language",
        "uz": "Tilni ozgartirish",
        "ru": "Сменить язык",
    },
    "staff.error.api.validation": {
        "en": "Please check the entered data and try again.",
        "uz": "Kiritilgan malumotlarni tekshirib, qayta urinib koring.",
        "ru": "Проверьте введенные данные и попробуйте снова.",
    },
    "staff.error.api.auth_failed": {
        "en": "Authentication failed. Please login again.",
        "uz": "Autentifikatsiya amalga oshmadi. Qayta kiring.",
        "ru": "Ошибка авторизации. Войдите снова.",
    },
    "staff.error.api.forbidden": {
        "en": "You do not have permission for this action.",
        "uz": "Bu amal uchun sizda ruxsat yoq.",
        "ru": "У вас нет прав для этого действия.",
    },
    "staff.error.api.not_found": {
        "en": "Requested data was not found.",
        "uz": "Sorangan malumot topilmadi.",
        "ru": "Запрошенные данные не найдены.",
    },
    "staff.error.api.conflict": {
        "en": "This action cannot be completed because of a conflict.",
        "uz": "Bu amalni bajarib bolmadi, tizimda ziddiyat bor.",
        "ru": "Не удалось выполнить действие из-за конфликта.",
    },
    "staff.error.api.invalid_input": {
        "en": "Invalid input. Please correct it and try again.",
        "uz": "Noto'gri malumot kiritildi. Iltimos, tuzatib qayta urinib koring.",
        "ru": "Некорректный ввод. Исправьте и повторите.",
    },
    "staff.tryout.tasks_title": {
        "en": "Try-out Task Pool",
        "uz": "Sinov vazifalari",
        "ru": "Список задач по пробным выдачам",
    },
    "staff.tryout.no_tasks": {
        "en": "No try-out tasks are available right now.",
        "uz": "Hozircha sinov vazifalari yoq.",
        "ru": "Сейчас нет задач по пробным выдачам.",
    },
    "staff.tryout.active_title": {
        "en": "My Active Try-outs",
        "uz": "Mening faol sinovlarim",
        "ru": "Мои активные пробные выдачи",
    },
    "staff.tryout.no_active": {
        "en": "No active try-outs with outstanding bottles.",
        "uz": "Qaytarilishi kerak bo'lgan idishlari bor faol sinovlar yoq.",
        "ru": "Нет активных пробных выдач с невозвращенной тарой.",
    },
    "staff.tryout.task_type": {
        "en": "Task type",
        "uz": "Vazifa turi",
        "ru": "Тип задачи",
    },
    "staff.tryout.task_status": {
        "en": "Task status",
        "uz": "Vazifa holati",
        "ru": "Статус задачи",
    },
    "staff.tryout.outstanding": {
        "en": "Outstanding bottles",
        "uz": "Qaytishi kerak bolgan butilkalar",
        "ru": "Невозвращенная тара",
    },
    "staff.tryout.accept_task": {
        "en": "Accept Task",
        "uz": "Vazifani olish",
        "ru": "Принять задачу",
    },
    "staff.tryout.complete_handoff": {
        "en": "Complete Handoff",
        "uz": "Topshirishni yakunlash",
        "ru": "Завершить передачу",
    },
    "staff.tryout.record_pickup": {
        "en": "Record Pickup",
        "uz": "Qaytarishni kiritish",
        "ru": "Зафиксировать возврат",
    },
    "staff.tryout.view_tryout": {
        "en": "View Try-out",
        "uz": "Sinovni korish",
        "ru": "Открыть пробную выдачу",
    },
    "staff.tryout.open_tasks": {
        "en": "Open Tasks",
        "uz": "Ochiq vazifalar",
        "ru": "Открытые задачи",
    },
    "staff.tryout.task_accepted": {
        "en": "Try-out task accepted.",
        "uz": "Sinov vazifasi olindi.",
        "ru": "Задача по пробной выдаче принята.",
    },
    "staff.tryout.handoff_recorded": {
        "en": "Try-out handoff recorded.",
        "uz": "Sinov topshiruvi qayd etildi.",
        "ru": "Передача пробной выдачи зафиксирована.",
    },
    "staff.tryout.task_not_found": {
        "en": "Try-out task not found.",
        "uz": "Sinov vazifasi topilmadi.",
        "ru": "Задача по пробной выдаче не найдена.",
    },
    "staff.tryout.tryout_not_found": {
        "en": "Try-out not found.",
        "uz": "Sinov topilmadi.",
        "ru": "Пробная выдача не найдена.",
    },
    "staff.tryout.pickup_prompt": {
        "en": "Send returned bottle quantities one per line.",
        "uz": "Qaytgan butilkalarni har qatorda yuboring.",
        "ru": "Отправьте возвращенную тару по одной строке.",
    },
    "staff.tryout.pickup_invalid_format": {
        "en": "Invalid format. Use product_id:units on each line.",
        "uz": "Format notogri. Har qatorda product_id:units korinishida yuboring.",
        "ru": "Неверный формат. Используйте идентификатор товара и количество в каждой строке.",
    },
    "staff.tryout.pickup_recorded": {
        "en": "Bottle pickup recorded.",
        "uz": "Butilka qaytarilishi qayd etildi.",
        "ru": "Возврат тары зафиксирован.",
    },
    "staff.tryout.pickup_select_product": {
        "en": "Choose a product and then tap the returned quantity.",
        "uz": "Mahsulotni tanlang, keyin qaytgan miqdorni bosing.",
        "ru": "Выберите товар, затем нажмите количество возвращенной тары.",
    },
    "staff.tryout.pickup_selected": {
        "en": "selected: {selected}",
        "uz": "tanlandi: {selected}",
        "ru": "выбрано: {selected}",
    },
    "staff.tryout.pickup_not_selected": {
        "en": "not selected yet",
        "uz": "hali tanlanmagan",
        "ru": "пока не выбрано",
    },
    "staff.tryout.pickup_select_quantity": {
        "en": "Select how many bottles were returned for {product}.",
        "uz": "{product} uchun nechta butilka qaytganini tanlang.",
        "ru": "Выберите, сколько бутылей вернули по товару {product}.",
    },
    "staff.tryout.pickup_current_quantity": {
        "en": "Selected now: {quantity} of {outstanding}",
        "uz": "Hozir tanlangan: {quantity} / {outstanding}",
        "ru": "Сейчас выбрано: {quantity} из {outstanding}",
    },
    "staff.tryout.pickup_submit": {
        "en": "Record Selected Bottles",
        "uz": "Tanlangan butilkalarni qayd etish",
        "ru": "Зафиксировать выбранную тару",
    },
    "staff.tryout.pickup_clear_selection": {
        "en": "Clear Selection",
        "uz": "Tanlovni tozalash",
        "ru": "Очистить выбор",
    },
    "staff.tryout.pickup_fill_all": {
        "en": "Fill All Outstanding",
        "uz": "Barchasini to'ldirish",
        "ru": "Заполнить весь остаток",
    },
    "staff.tryout.pickup_clear_product": {
        "en": "Remove This Product",
        "uz": "Bu mahsulotni olib tashlash",
        "ru": "Убрать этот товар",
    },
    "staff.tryout.pickup_nothing_selected": {
        "en": "Select at least one returned quantity first.",
        "uz": "Avval kamida bitta qaytgan miqdorni tanlang.",
        "ru": "Сначала выберите хотя бы одно возвращенное количество.",
    },
    "staff.tryout.pickup_no_outstanding": {
        "en": "There are no outstanding bottles left for this try-out.",
        "uz": "Bu sinov bo'yicha qaytishi kerak bo'lgan butilkalar qolmagan.",
        "ru": "По этой пробной выдаче не осталось невозвращенной тары.",
    },
    "staff.tryout.pickup_use_buttons": {
        "en": "Use the buttons below to record bottle returns.",
        "uz": "Butilka qaytarilishini qayd etish uchun pastdagi tugmalardan foydalaning.",
        "ru": "Используйте кнопки ниже, чтобы зафиксировать возврат тары.",
    },
    "staff.tryout.enter_phone": {
        "en": "Enter the customer's phone number.",
        "uz": "Mijozning telefon raqamini kiriting.",
        "ru": "Введите номер телефона клиента.",
    },
    "staff.tryout.enter_name": {
        "en": "Enter the customer's first name.",
        "uz": "Mijozning ismini kiriting.",
        "ru": "Введите имя клиента.",
    },
    "staff.tryout.enter_address": {
        "en": "Enter the try-out delivery address.",
        "uz": "Sinov topshiriladigan manzilni kiriting.",
        "ru": "Введите адрес пробной выдачи.",
    },
    "staff.tryout.enter_address_or_location": {
        "en": "Enter the try-out delivery address or send your location.",
        "uz": "Sinov topshiriladigan manzilni kiriting yoki joylashuvingizni yuboring.",
        "ru": "Введите адрес пробной выдачи или отправьте геолокацию.",
    },
    "staff.tryout.send_location": {
        "en": "Send Location",
        "uz": "Joylashuvni yuborish",
        "ru": "Отправить геолокацию",
    },
    "staff.tryout.address_received": {
        "en": "Address saved. Now choose the try-out products.",
        "uz": "Manzil saqlandi. Endi sinov mahsulotlarini tanlang.",
        "ru": "Адрес сохранен. Теперь выберите товары для пробной выдачи.",
    },
    "staff.tryout.invalid_address": {
        "en": "Address is too short. Please enter a fuller address.",
        "uz": "Manzil juda qisqa. Iltimos, toliqroq manzil kiriting.",
        "ru": "Адрес слишком короткий. Введите полный адрес.",
    },
    "staff.tryout.location_received": {
        "en": "Location received: {address}",
        "uz": "Joylashuv qabul qilindi: {address}",
        "ru": "Геолокация получена: {address}",
    },
    "staff.tryout.location_geocode_failed": {
        "en": "Location received, but the address could not be resolved. Please type the address manually.",
        "uz": "Joylashuv qabul qilindi, lekin manzil aniqlanmadi. Iltimos, manzilni qo'lda kiriting.",
        "ru": "Геолокация получена, но адрес определить не удалось. Пожалуйста, введите адрес вручную.",
    },
    "staff.tryout.select_products": {
        "en": "Select try-out products.",
        "uz": "Sinov mahsulotlarini tanlang.",
        "ru": "Выберите товары для пробной выдачи.",
    },
    "staff.tryout.select_quantity": {
        "en": "Select quantity for {product}.",
        "uz": "{product} uchun miqdorni tanlang.",
        "ru": "Выберите количество для {product}.",
    },
    "staff.tryout.current_quantity": {
        "en": "Current quantity: {quantity}",
        "uz": "Hozirgi miqdor: {quantity}",
        "ru": "Текущее количество: {quantity}",
    },
    "staff.tryout.selected_products": {
        "en": "Selected products",
        "uz": "Tanlangan mahsulotlar",
        "ru": "Выбранные товары",
    },
    "staff.tryout.done_selecting": {
        "en": "Done Selecting",
        "uz": "Tanlashni yakunlash",
        "ru": "Завершить выбор",
    },
    "staff.tryout.add_more_products": {
        "en": "Add More Products",
        "uz": "Yana mahsulot qoshish",
        "ru": "Добавить еще товары",
    },
    "staff.tryout.no_products_selected": {
        "en": "Select at least one product first.",
        "uz": "Avval kamida bitta mahsulot tanlang.",
        "ru": "Сначала выберите хотя бы один товар.",
    },
    "staff.tryout.confirm_create_title": {
        "en": "Confirm Try-out",
        "uz": "Sinovni tasdiqlash",
        "ru": "Подтвердите пробную выдачу",
    },
    "staff.tryout.product_not_found": {
        "en": "Selected product was not found.",
        "uz": "Tanlangan mahsulot topilmadi.",
        "ru": "Выбранный товар не найден.",
    },
    "staff.tryout.remove_product": {
        "en": "Remove Product",
        "uz": "Mahsulotni olib tashlash",
        "ru": "Убрать товар",
    },
    "staff.tryout.created_success": {
        "en": "Try-out created successfully: {tryout_number}",
        "uz": "Sinov muvaffaqiyatli yaratildi: {tryout_number}",
        "ru": "Пробная выдача создана: {tryout_number}",
    },
    "staff.error.api.rate_limited": {
        "en": "Too many requests. Please wait and try again.",
        "uz": "Juda kop sorov yuborildi. Biroz kutib qayta urinib koring.",
        "ru": "Слишком много запросов. Подождите и попробуйте снова.",
    },
    "staff.error.api.service_unavailable": {
        "en": "Service is temporarily unavailable. Please try later.",
        "uz": "Xizmat vaqtincha mavjud emas. Keyinroq urinib koring.",
        "ru": "Сервис временно недоступен. Попробуйте позже.",
    },
    "staff.error.api.already_taken": {
        "en": "This order has already been taken by another courier.",
        "uz": "Bu buyurtma allaqachon boshqa kuryer tomonidan olingan.",
        "ru": "Этот заказ уже принят другим курьером.",
    },
    "staff.error.api.driver_cod_blocked": {
        "en": "You cannot accept new cash-on-delivery orders until your pending cash reconciliation is resolved. Please complete Cash Reconciliation first.",
        "uz": "Naqd pul yarashtiruvi hal qilinmaguncha siz yangi naqd tolovli buyurtmalarni qabul qila olmaysiz. Iltimos, avval Naqd pul yarashtiruvini bajaring.",
        "ru": "Вы не можете принимать новые заказы с оплатой наличными, пока не завершите сверку наличных. Пожалуйста, сначала выполните Сверку наличных.",
    },
    "staff.error.api.cod_debt_limit_reached": {
        "en": "This customer has reached the maximum number of unpaid cash-on-delivery debts and cannot take on more until earlier debts are settled.",
        "uz": "Bu mijoz to'lanmagan naqd to'lov qarzlarining maksimal soniga yetdi va eski qarzlar to'lanmagunicha yangi qarz olishi mumkin emas.",
        "ru": "Этот клиент достиг максимального количества непогашенных задолженностей по наложенному платежу — новые долги невозможны, пока не погашены прежние.",
    },
    "staff.error.api.invalid_invite": {
        "en": "Invite link is invalid or expired.",
        "uz": "Taklif havolasi yaroqsiz yoki muddati tugagan.",
        "ru": "Ссылка-приглашение неверна или просрочена.",
    },
    "staff.error.api.unexpected": {
        "en": "Unexpected server response. Please try again.",
        "uz": "Kutilmagan server javobi. Qayta urinib koring.",
        "ru": "Неожиданный ответ сервера. Попробуйте снова.",
    },

    # --- Bottle tracking translations ---
    "staff.menu.bottle_collection": {
        "en": "Bottle Collection",
        "uz": "Idish yigish",
        "ru": "Сбор тары",
    },
    "staff.delivery.bottle_statement_title": {
        "en": "Bottle Statement",
        "uz": "Idish hisoboti",
        "ru": "Отчёт по таре",
    },
    "staff.delivery.total_bottles": {
        "en": "Total bottles",
        "uz": "Jami idishlar",
        "ru": "Всего тара",
    },
    "staff.delivery.active_fines": {
        "en": "Active fines",
        "uz": "Faol jarimalar",
        "ru": "Активные штрафы",
    },
    "staff.delivery.no_bottle_balance": {
        "en": "No bottle balance on record.",
        "uz": "Idish balansi topilmadi.",
        "ru": "Баланс тары не обнаружен.",
    },
    "staff.delivery.bottle_collection_search_prompt": {
        "en": "Enter a customer name or phone number to find their bottle balance.",
        "uz": "Mijoz ismi yoki telefon raqamini kiriting.",
        "ru": "Введите имя клиента или номер телефона для поиска баланса тары.",
    },
    "staff.delivery.no_customer_bottle_results": {
        "en": "No customers with bottles found for \"{query}\".",
        "uz": "\"{query}\" uchun idishli mijoz topilmadi.",
        "ru": "Клиенты с тарой по запросу «{query}» не найдены.",
    },
    "staff.delivery.view_bottle_balance": {
        "en": "View Bottle Balance",
        "uz": "Idish balansini korish",
        "ru": "Баланс тары",
    },
    "staff.delivery.bottle_address_selected": {
        "en": "Address selected. Choose an action:",
        "uz": "Manzil tanlandi. Amalni tanlang:",
        "ru": "Адрес выбран. Выберите действие:",
    },
    "staff.delivery.collect_bottles": {
        "en": "Collect Bottles",
        "uz": "Idish yigish",
        "ru": "Собрать тару",
    },
    "staff.delivery.issue_bottle_fine": {
        "en": "Issue Fine",
        "uz": "Jarima berish",
        "ru": "Выписать штраф",
    },
    "staff.delivery.enter_bottle_collection_qty": {
        "en": "Tap the number of bottles you collected:",
        "uz": "Yigilgan idishlar sonini tanlang:",
        "ru": "Выберите количество собранной тары:",
    },
    "staff.delivery.enter_bottle_collection_note": {
        "en": "Add a note for this collection, or tap Save without note:",
        "uz": "Izoh kiriting yoki «Izohsiz saqlash»ni bosing:",
        "ru": "Добавьте примечание или нажмите «Сохранить без примечания»:",
    },
    "staff.delivery.collect_all": {
        "en": "All",
        "uz": "Hammasi",
        "ru": "Все",
    },
    "staff.delivery.save_without_note": {
        "en": "Save without note",
        "uz": "Izohsiz saqlash",
        "ru": "Сохранить без примечания",
    },
    "staff.delivery.bottle_search_results_title": {
        "en": "Found {count} customer(s). Tap one to view their bottle balance:",
        "uz": "{count} ta mijoz topildi. Idish balansini korish uchun bosing:",
        "ru": "Найдено клиентов: {count}. Нажмите для просмотра баланса тары:",
    },
    "staff.delivery.invalid_bottle_count": {
        "en": "Please enter a valid positive number.",
        "uz": "Iltimos, musbat son kiriting.",
        "ru": "Пожалуйста, введите положительное число.",
    },
    "staff.delivery.bottle_collection_recorded": {
        "en": "Collected {quantity} bottle(s). Remaining balance: {remaining}.",
        "uz": "{quantity} ta idish yigib olindi. Qolgan balans: {remaining}.",
        "ru": "Собрано {quantity} ед. тары. Остаток: {remaining}.",
    },
    "staff.delivery.enter_fine_bottle_qty": {
        "en": "How many bottles to fine for?",
        "uz": "Necha idish uchun jarima?",
        "ru": "За сколько единиц тары штраф?",
    },
    "staff.delivery.enter_fine_amount": {
        "en": "Enter the fine amount (in UZS):",
        "uz": "Jarima miqdorini kiriting (UZS):",
        "ru": "Введите сумму штрафа (в UZS):",
    },
    "staff.delivery.enter_fine_note": {
        "en": "Add a note for this fine:",
        "uz": "Jarima uchun izoh kiriting:",
        "ru": "Добавьте примечание для штрафа:",
    },
    "staff.delivery.invalid_amount": {
        "en": "Please enter a valid amount.",
        "uz": "Iltimos, togri miqdor kiriting.",
        "ru": "Пожалуйста, введите корректную сумму.",
    },
    "staff.delivery.bottle_fine_created": {
        "en": "Fine created: {quantity} bottle(s), amount {amount}.",
        "uz": "Jarima yaratildi: {quantity} ta idish, miqdori {amount}.",
        "ru": "Штраф создан: {quantity} ед. тары, сумма {amount}.",
    },
    # Bottle return during delivery
    "staff.delivery.bottles_returned_prompt": {
        "en": "How many bottles did the customer return?",
        "uz": "Mijoz necha idish qaytardi?",
        "ru": "Сколько тары вернул клиент?",
    },
    "staff.delivery.bottles_all_returned": {
        "en": "All {count} bottles returned",
        "uz": "Barcha {count} ta idish qaytarildi",
        "ru": "Все {count} ед. тары возвращены",
    },
    "staff.delivery.bottles_enter_count": {
        "en": "Enter count",
        "uz": "Sonini kiriting",
        "ru": "Ввести количество",
    },
    "staff.delivery.bottles_none_returned": {
        "en": "No bottles returned",
        "uz": "Idish qaytarilmadi",
        "ru": "Тара не возвращена",
    },
    "staff.delivery.enter_bottle_count": {
        "en": "Enter the number of bottles returned:",
        "uz": "Qaytarilgan idishlar sonini kiriting:",
        "ru": "Введите количество возвращённой тары:",
    },
    # Warehouse accountability flows
    "staff.menu.log_bottles_loaded": {
        "en": "Log Bottles Loaded (18.9 l)",
        "uz": "Yuklangan (18.9 l) idishlarni kiritish",
        "ru": "Внести загруженные бутылки (18.9 l)",
    },
    "staff.menu.return_to_warehouse": {
        "en": "Return to Warehouse",
        "uz": "Omborga qaytarish",
        "ru": "Возврат на склад",
    },
    "staff.menu.my_bottle_accountability": {
        "en": "My Bottle Accountability (18.9 l)",
        "uz": "Mening (18.9 l) idish hisobotim",
        "ru": "Мой учёт тары (18.9 l)",
    },
    "staff.delivery.enter_bottles_loaded_qty": {
        "en": "Enter the number of bottles (18.9 l) you loaded from the warehouse:",
        "uz": "Ombordan nechta (18.9 l) idish yuklab olganingizni kiriting:",
        "ru": "Введите количество бутылок (18.9 l), загруженных со склада:",
    },
    "staff.delivery.bottles_loaded_recorded": {
        "en": "\u2705 Recorded: {quantity} (18.9 l) bottle(s) loaded from warehouse.",
        "uz": "\u2705 Qayd etildi: ombordan {quantity} ta (18.9 l) idish yuklandi.",
        "ru": "\u2705 Записано: {quantity} ед. тары (18.9 l) загружено со склада.",
    },
    "staff.delivery.enter_bottles_returned_qty": {
        "en": "Enter the number of bottles (18.9 l) you returned to the warehouse:",
        "uz": "Omborga nechta (18.9 l) idish qaytarganingizni kiriting:",
        "ru": "Введите количество бутылок (18.9 l), возвращённых на склад:",
    },
    "staff.delivery.bottles_returned_wh_recorded": {
        "en": "\u2705 Recorded: {quantity} (18.9 l) bottle(s) returned to warehouse.",
        "uz": "\u2705 Qayd etildi: {quantity} ta (18.9 l) idish omborga qaytarildi.",
        "ru": "\u2705 Записано: {quantity} ед. тары (18.9 l) возвращено на склад.",
    },
    "staff.delivery.bottle_accountability_no_data": {
        "en": "No (18.9 l) bottle accountability data yet.",
        "uz": "(18.9 l) Idish hisobi yo'q.",
        "ru": "Данных по учёту тары (18.9 l) нет.",
    },
    # Bottle session menu buttons
    "staff.menu.transfer_bottles_to_driver": {
        "en": "Transfer (18.9 l) bottles to other driver",
        "uz": "Boshqa haydovchiga (18.9 l) idish o'tkazish",
        "ru": "Передать (18.9 l) бутылки другому водителю",
    },
    "staff.menu.incoming_transfers": {
        "en": "Incoming transfers",
        "uz": "Kiruvchi o'tkazmalar",
        "ru": "Входящие передачи",
    },
    # Bottle session state messages
    "staff.delivery.bottle_session_already_open": {
        "en": "🚫 <b>Cannot start a new load.</b>\n\nYou already have an active session started at <b>{started}</b> with <b>{loaded}</b> (18.9 l) bottles loaded.\n\nReturn to the warehouse and close your current session first.",
        "uz": "🚫 <b>Yangi yuklash boshlash mumkin emas.</b>\n\nSizda allaqachon <b>{started}</b> da boshlangan va <b>{loaded}</b> ta (18.9 l) idish yuklangan faol sessiya mavjud.\n\nAvval omborga qayting va joriy sessiyangizni yoping.",
        "ru": "🚫 <b>Нельзя начать новую загрузку.</b>\n\nУ вас уже есть активная сессия, начатая в <b>{started}</b> с <b>{loaded}</b> (18.9 l) бутылками.\n\nВернитесь на склад и закройте текущую сессию.",
    },
    "staff.delivery.bottle_session_already_open_short": {
        "en": "🚫 You already have an open session. Close it before loading new (18.9 l) bottles.",
        "uz": "🚫 Sizda allaqachon ochiq sessiya bor. Yangi (18.9 l) idish yuklashdan oldin uni yoping.",
        "ru": "🚫 У вас уже есть открытая сессия. Закройте её перед загрузкой новых (18.9 l) бутылок.",
    },
    "staff.delivery.bottle_session_opened": {
        "en": "✅ <b>Session opened!</b>\n\n📦 Loaded: <b>{count}</b> (18.9 l) bottles\nSession ref: <code>{ref}</code>\n\nDeliver your orders and return to WH when done.",
        "uz": "✅ <b>Sessiya ochildi!</b>\n\n📦 Yuklandi: <b>{count}</b> ta (18.9 l) idish\nSessiya raqami: <code>{ref}</code>\n\nBuyurtmalaringizni yetkazing va tugagach omborga qayting.",
        "ru": "✅ <b>Сессия открыта!</b>\n\n📦 Загружено: <b>{count}</b> (18.9 l) бутылок\nНомер сессии: <code>{ref}</code>\n\nВыполняйте заказы и возвращайтесь на склад по завершении.",
    },
    "staff.delivery.no_active_bottle_session": {
        "en": "ℹ️ You have no active bottle session. Nothing to close.",
        "uz": "ℹ️ Sizda faol idish sessiyasi yo'q. Yopish uchun hech narsa yo'q.",
        "ru": "ℹ️ У вас нет активной сессии бутылок. Нечего закрывать.",
    },
    "staff.delivery.bottle_session_closed": {
        "en": "✅ <b>Session closed.</b>\n\n🏢 Returned to WH: <b>{count}</b>\n{disc_line}\nRef: <code>{ref}</code>",
        "uz": "✅ <b>Sessiya yopildi.</b>\n\n🏢 Omborga qaytarildi: <b>{count}</b>\n{disc_line}\nRaqam: <code>{ref}</code>",
        "ru": "✅ <b>Сессия закрыта.</b>\n\n🏢 Возвращено на склад: <b>{count}</b>\n{disc_line}\nНомер: <code>{ref}</code>",
    },
    "staff.delivery.discrepancy_zero": {
        "en": "✅ Discrepancy: <b>0</b>  🎯",
        "uz": "✅ Farq: <b>0</b>  🎯",
        "ru": "✅ Расхождение: <b>0</b>  🎯",
    },
    "staff.delivery.discrepancy_nonzero": {
        "en": "⚠️ Discrepancy: <b>{discrepancy}</b> (18.9 l) bottles unaccounted",
        "uz": "⚠️ Farq: <b>{discrepancy}</b> ta (18.9 l) idish hisoblanmagan",
        "ru": "⚠️ Расхождение: <b>{discrepancy}</b> (18.9 l) бутылок не учтено",
    },
    # Bottle transfer messages
    "staff.delivery.no_bottles_to_transfer": {
        "en": "🚫 You have no bottles available to transfer.",
        "uz": "🚫 O'tkazish uchun idishlaringiz yo'q.",
        "ru": "🚫 У вас нет бутылок для передачи.",
    },
    "staff.delivery.no_active_drivers": {
        "en": "No other active drivers found.",
        "uz": "Boshqa faol haydovchilar topilmadi.",
        "ru": "Других активных водителей не найдено.",
    },
    "staff.delivery.select_transfer_driver": {
        "en": "Select the driver to transfer bottles to.\n(You have <b>{available}</b> bottles available)",
        "uz": "Idish o'tkazish uchun haydovchini tanlang.\n(Sizda <b>{available}</b> ta idish mavjud)",
        "ru": "Выберите водителя для передачи бутылок.\n(У вас <b>{available}</b> бутылок доступно)",
    },
    "staff.delivery.enter_transfer_qty": {
        "en": "📦 How many bottles are you transferring?\n(You have <b>{available}</b> available)",
        "uz": "📦 Nechta idish o'tkazmoqdasiz?\n(Sizda <b>{available}</b> ta mavjud)",
        "ru": "📦 Сколько бутылок вы передаёте?\n(У вас <b>{available}</b> доступно)",
    },
    "staff.delivery.transfer_qty_exceeds_available": {
        "en": "⚠️ You only have {available} bottle(s) available. Enter a smaller number.",
        "uz": "⚠️ Sizda faqat {available} ta idish mavjud. Kichikroq raqam kiriting.",
        "ru": "⚠️ У вас всего {available} бутылок. Введите меньшее число.",
    },
    "staff.delivery.bottle_transfer_initiated": {
        "en": "✅ <b>Transfer initiated!</b>\n\n📦 Quantity: <b>{qty}</b> bottles\nThe receiving driver will get a notification to confirm.\nRef: <code>{ref}</code>",
        "uz": "✅ <b>O'tkazish boshlandi!</b>\n\n📦 Miqdor: <b>{qty}</b> ta idish\nQabul qiluvchi haydovchi tasdiqlash uchun bildirishnoma oladi.\nRaqam: <code>{ref}</code>",
        "ru": "✅ <b>Передача инициирована!</b>\n\n📦 Количество: <b>{qty}</b> бутылок\nПринимающий водитель получит уведомление для подтверждения.\nНомер: <code>{ref}</code>",
    },
    "staff.delivery.no_pending_transfers": {
        "en": "No pending transfers waiting for your confirmation.",
        "uz": "Tasdiqlashingizni kutayotgan o'tkazmalar yo'q.",
        "ru": "Нет ожидающих подтверждения передач.",
    },
    "staff.delivery.pending_transfers_title": {
        "en": "📥 <b>Pending Incoming Transfers:</b>",
        "uz": "📥 <b>Kutilayotgan kiruvchi o'tkazmalar:</b>",
        "ru": "📥 <b>Ожидающие входящие передачи:</b>",
    },
    "staff.delivery.enter_actual_received_qty": {
        "en": "✏️ How many bottles did you actually receive?\nEnter the count:",
        "uz": "✏️ Aslida nechta idish oldiniz?\nSonni kiriting:",
        "ru": "✏️ Сколько бутылок вы фактически получили?\nВведите количество:",
    },
    "staff.delivery.transfer_confirm_failed": {
        "en": "Failed to confirm transfer. Please try again.",
        "uz": "O'tkazmani tasdiqlash muvaffaqiyatsiz. Qayta urinib ko'ring.",
        "ru": "Не удалось подтвердить передачу. Попробуйте ещё раз.",
    },
    "staff.delivery.transfer_confirmed": {
        "en": "✅ <b>Transfer confirmed!</b>\n\n📥 <b>{qty}</b> bottles added to your session.",
        "uz": "✅ <b>O'tkazma tasdiqlandi!</b>\n\n📥 <b>{qty}</b> ta idish sessiyangizga qo'hildi.",
        "ru": "✅ <b>Передача подтверждена!</b>\n\n📥 <b>{qty}</b> бутылок добавлено в вашу сессию.",
    },
    "staff.delivery.transfer_disputed": {
        "en": "⚠️ <b>Bottle transfer to other driver request filed.</b>\n\nSender declared <b>{declared}</b>, you received <b>{qty}</b>.\nAdmin has been notified. Your session has been credited with <b>{qty}</b> pending resolution.",
        "uz": "⚠️ <b>Idish o'tkazma so'rovi kiritildi.</b>\n\nJo'natuvchi <b>{declared}</b> ta deb ko'rsatdi, siz <b>{qty}</b> ta oldingiz.\nAdmin xabardor qilindi. Sessiyangizga hal bo'lgunga qadar <b>{qty}</b> ta yozildi.",
        "ru": "⚠️ <b>Запрос на передачу бутылки другому водителю подан.</b>\n\nОтправитель указал <b>{declared}</b>, вы получили <b>{qty}</b>.\nАдмин уведомлён. В вашу сессию записано <b>{qty}</b> до разрешения.",
    },
    # Session display labels (used in _format_session)
    "staff.delivery.session_ref_label": {
        "en": "Session",
        "uz": "Sessiya",
        "ru": "Сессия",
    },
    "staff.delivery.session_started_label": {
        "en": "Started",
        "uz": "Boshlangan",
        "ru": "Начата",
    },
    "staff.delivery.bottles_loaded_label": {
        "en": "Loaded",
        "uz": "Yuklangan",
        "ru": "Загружено",
    },
    "staff.delivery.bottles_delivered_label": {
        "en": "Delivered",
        "uz": "Yetkazilgan",
        "ru": "Доставлено",
    },
    "staff.delivery.bottles_collected_label": {
        "en": "Collected",
        "uz": "Yig'ilgan",
        "ru": "Собрано",
    },
    "staff.delivery.bottles_transferred_out_label": {
        "en": "Transferred out",
        "uz": "Chiqib ketgan",
        "ru": "Передано",
    },
    "staff.delivery.bottles_transferred_in_label": {
        "en": "Transferred in",
        "uz": "Kirib kelgan",
        "ru": "Получено",
    },
    "staff.delivery.bottles_on_truck_label": {
        "en": "On truck now",
        "uz": "Hozir mashinada",
        "ru": "На машине сейчас",
    },
    "staff.delivery.bottles_returned_wh_label": {
        "en": "Returned to WH",
        "uz": "Omborga qaytarildi",
        "ru": "Возвращено на склад",
    },
    "staff.delivery.discrepancy_label": {
        "en": "Discrepancy",
        "uz": "Farq",
        "ru": "Расхождение",
    },

    # --- Co-driver session membership ---
    "staff.bottles.session_required_to_accept": {
        "en": "⚠️ A bottle session is required to accept this order.\nPlease start your own session or join a colleague's.",
        "uz": "⚠️ Buyurtmani qabul qilish uchun shisha sessiyasi kerak.\nO'z sessiyangizni boshlang yoki hamkasbingiznikiga qo'shiling.",
        "ru": "⚠️ Для принятия заказа требуется сессия бутылок.\nНачните свою сессию или присоединитесь к сессии коллеги.",
    },
    "staff.bottles.start_session": {
        "en": "▶️ Start My Session",
        "uz": "▶️ O'z sessiyamni boshlash",
        "ru": "▶️ Начать свою сессию",
    },
    "staff.bottles.join_session": {
        "en": "🤝 Join Colleague's Session",
        "uz": "🤝 Hamkasb sessiyasiga qo'shilish",
        "ru": "🤝 Присоединиться к сессии коллеги",
    },
    "staff.bottles.leave_session": {
        "en": "🚪 Leave Session",
        "uz": "🚪 Sessiyadan chiqish",
        "ru": "🚪 Покинуть сессию",
    },
    "staff.bottles.no_open_sessions": {
        "en": "ℹ️ No open sessions available to join right now.",
        "uz": "ℹ️ Hozirda qo'shilish uchun ochiq sessiyalar yo'q.",
        "ru": "ℹ️ Сейчас нет открытых сессий для присоединения.",
    },
    "staff.bottles.choose_session_to_join": {
        "en": "Choose a session to join:",
        "uz": "Qo'shilish uchun sessiyani tanlang:",
        "ru": "Выберите сессию для присоединения:",
    },
    "staff.bottles.join_session_confirm_title": {
        "en": "Join Session?",
        "uz": "Sessiyaga qo'shilasizmi?",
        "ru": "Присоединиться к сессии?",
    },
    "staff.bottles.session_owner": {
        "en": "Session owner",
        "uz": "Sessiya egasi",
        "ru": "Владелец сессии",
    },
    "staff.bottles.bottles_on_truck": {
        "en": "Bottles on truck",
        "uz": "Mashinadagi shishalar",
        "ru": "Бутылок в машине",
    },
    "staff.bottles.join_session_confirm_note": {
        "en": "While joined, your orders will be tracked against this session's inventory.",
        "uz": "Qo'shilgandan so'ng buyurtmalaringiz ushbu sessiya inventariga hisoblanadi.",
        "ru": "После присоединения ваши заказы будут учитываться в рамках этой сессии.",
    },
    "staff.bottles.confirm_join": {
        "en": "Confirm Join",
        "uz": "Qo'shilishni tasdiqlash",
        "ru": "Подтвердить присоединение",
    },
    "staff.bottles.joined_session": {
        "en": "Joined {name}'s session!",
        "uz": "{name} sessiyasiga qo'shildingiz!",
        "ru": "Вы присоединились к сессии {name}!",
    },
    "staff.bottles.joined_session_info": {
        "en": "You can now accept orders. Bottles will be deducted from the shared session.",
        "uz": "Endi buyurtmalarni qabul qilishingiz mumkin. Shishalar umumiy sessiyadan hisoblanadi.",
        "ru": "Теперь вы можете принимать заказы. Бутылки будут списываться из общей сессии.",
    },
    "staff.bottles.left_session": {
        "en": "✅ You have left the session.",
        "uz": "✅ Siz sessiyadan chiqdingiz.",
        "ru": "✅ Вы покинули сессию.",
    },
    "staff.bottles.current_membership_title": {
        "en": "Active Co-Driver Session",
        "uz": "Faol hamkor sessiyasi",
        "ru": "Активная совместная сессия",
    },
    "staff.bottles.current_membership": {
        "en": "Using {name}'s session — <b>{qty}</b> bottles available",
        "uz": "{name} sessiyasida — <b>{qty}</b> shisha mavjud",
        "ru": "Сессия {name} — доступно <b>{qty}</b> бутылок",
    },
    "staff.bottles.session_closed_membership_revoked": {
        "en": "ℹ️ The session you joined has been closed. Start or join a new session when needed.",
        "uz": "ℹ️ Siz qo'shilgan sessiya yopildi. Kerak bo'lganda yangi sessiya boshlang yoki qo'shiling.",
        "ru": "ℹ️ Сессия, к которой вы присоединились, закрыта. При необходимости начните или присоединитесь к новой сессии.",
    },
    "staff.bottles.no_active_membership": {
        "en": "ℹ️ You are not currently joined to any colleague's session.",
        "uz": "ℹ️ Hozirda hech qanday hamkasb sessiyasiga qo'shilmagansiz.",
        "ru": "ℹ️ Вы сейчас не присоединены ни к чьей сессии.",
    },
    "staff.bottles.session_not_found": {
        "en": "❌ Session not found. It may have been closed.",
        "uz": "❌ Sessiya topilmadi. U yopilgan bo'lishi mumkin.",
        "ru": "❌ Сессия не найдена. Возможно, она уже закрыта.",
    },
    "staff.bottles.membership_status_active": {
        "en": "Active",
        "uz": "Faol",
        "ru": "Активен",
    },
    "staff.bottles.membership_status_left": {
        "en": "Left",
        "uz": "Chiqib ketdi",
        "ru": "Покинул",
    },
    "staff.bottles.membership_status_revoked": {
        "en": "Revoked",
        "uz": "Bekor qilindi",
        "ru": "Отозван",
    },
    "staff.bottles.invite_codriver": {
        "en": "👥 Invite Co-driver",
        "uz": "👥 Hamkorni taklif qilish",
        "ru": "👥 Пригласить напарника",
    },
    "staff.bottles.no_drivers_to_invite": {
        "en": "ℹ️ No available drivers to invite. All drivers either have their own session or are already in a session.",
        "uz": "ℹ️ Taklif qilish uchun mavjud haydovchi yo'q. Barcha haydovchilar o'z sessiyasiga ega yoki allaqachon sessiyada.",
        "ru": "ℹ️ Нет доступных водителей для приглашения. Все водители либо имеют собственную сессию, либо уже состоят в сессии.",
    },
    "staff.bottles.choose_driver_to_invite": {
        "en": "👥 Choose a driver to invite to your session:",
        "uz": "👥 Sessiyangizga taklif qilish uchun haydovchini tanlang:",
        "ru": "👥 Выберите водителя для приглашения в вашу сессию:",
    },
    "staff.bottles.invite_codriver_confirm": {
        "en": "Invite this driver to join your session as a co-driver?",
        "uz": "Bu haydovchini sessiyangizga hamkor sifatida taklif qilasizmi?",
        "ru": "Пригласить этого водителя присоединиться к вашей сессии как напарника?",
    },
    "staff.bottles.invite_codriver_confirm_note": {
        "en": "ℹ️ They will be able to deliver orders and collect bottles under your session.",
        "uz": "ℹ️ Ular sizning sessiyangiz doirasida buyurtmalarni yetkazib berish va shishalarni yig'ish imkoniyatiga ega bo'ladi.",
        "ru": "ℹ️ Они смогут доставлять заказы и собирать бутылки в рамках вашей сессии.",
    },
    "staff.bottles.confirm_invite": {
        "en": "✅ Confirm Invite",
        "uz": "✅ Taklif qilishni tasdiqlash",
        "ru": "✅ Подтвердить приглашение",
    },
    "staff.bottles.codriver_invited": {
        "en": "✅ <b>{name}</b> has been added to your session as a co-driver.",
        "uz": "✅ <b>{name}</b> sessiyangizga hamkor sifatida qo'shildi.",
        "ru": "✅ <b>{name}</b> добавлен в вашу сессию как напарник.",
    },
    "staff.bottles.no_open_session_to_invite": {
        "en": "❌ You must have an open session to invite a co-driver.",
        "uz": "❌ Hamkorni taklif qilish uchun ochiq sessiyangiz bo'lishi kerak.",
        "ru": "❌ Для приглашения напарника необходимо иметь открытую сессию.",
    },
    "staff.delivery.cod_prepaid_reserved": {
        "en": "COD prepaid reserved",
        "uz": "Naqd to'lov uchun band qilingan oldindan to'lov",
        "ru": "Зарезервированная предоплата за наложенный платёж",
    },
    "staff.delivery.cash_to_collect_now": {
        "en": "Cash to collect now",
        "uz": "Hozir yig'iladigan naqd pul",
        "ru": "Сумма к получению сейчас",
    },
    "staff.delivery.cod_prepaid_deduction": {
        "en": "COD prepaid deduction",
        "uz": "Naqd to'lovdan oldindan to'lov ushlanmasi",
        "ru": "Вычет предоплаты из наложенного платежа",
    },
    "staff.delivery.no_cash_due_after_cod": {
        "en": "No cash due after COD prepaid deduction",
        "uz": "Oldindan to'lov ushlangandan keyin naqd pul talab qilinmaydi",
        "ru": "Наличные не требуются после вычета предоплаты",
    },
    "staff.delivery.transfer_confirm_button": {
        "en": "Confirm {qty} from {sender}",
        "uz": "{sender} dan {qty} ni tasdiqlash",
        "ru": "Подтвердить {qty} от {sender}",
    },
    "staff.delivery.transfer_custom_count_button": {
        "en": "Different count",
        "uz": "Boshqa miqdor",
        "ru": "Другое количество",
    },
    "staff.tryout.pickup_all_label": {
        "en": "All ({label})",
        "uz": "Hammasi ({label})",
        "ru": "Все ({label})",
    },
    "staff.delivery.active_cod_debts": {
        "en": "Active COD (Cash on Delivery) debts",
        "uz": "Faol naqd to'lov qarzlari",
        "ru": "Активные долги по наложенным платежам",
    },
    "staff.delivery.bottles_return_prompt": {
        "en": "How many bottles (18.9 L) did the customer return? Expected: {expected}",
        "uz": "Mijoz nechta idish (18.9 L) qaytardi? Kutilgan: {expected}",
        "ru": "Сколько бутылок (18.9 L) вернул клиент? Ожидалось: {expected}",
    },
    "staff.delivery.cash_already_collected": {
        "en": "Cash already collected in full",
        "uz": "Naqd pul to'liq yig'ib olingan",
        "ru": "Наличные уже получены полностью",
    },
    "staff.delivery.cash_partially_collected": {
        "en": "Cash partially collected",
        "uz": "Naqd pul qisman yig'ib olingan",
        "ru": "Наличные получены частично",
    },
    "staff.delivery.cod_collection_amount_exceeds_outstanding": {
        "en": "Amount exceeds outstanding ({amount}). Please enter a smaller value.",
        "uz": "Summa qoldiqdan ({amount}) ortiq. Iltimos, kichikroq qiymat kiriting.",
        "ru": "Сумма превышает остаток ({amount}). Введите меньшее значение.",
    },
    "staff.delivery.cod_collection_amount_prompt": {
        "en": "Enter the amount you collected (UZS):",
        "uz": "Yig'ib olgan summani kiriting (UZS):",
        "ru": "Введите полученную сумму (UZS):",
    },
    "staff.delivery.cod_collection_note_prompt": {
        "en": "Add a note for this collection of {amount} (or send /skip to record without a note):",
        "uz": "Ushbu {amount} yig'imi uchun izoh qo'shing (izohsiz qayd qilish uchun /skip yuboring):",
        "ru": "Добавьте примечание для сбора {amount} (или отправьте /skip, чтобы записать без примечания):",
    },
    "staff.delivery.cod_collection_overpayment_confirm": {
        "en": "You entered {amount}, but the customer's outstanding COD debt is only {outstanding}. The surplus {overpayment} will be recorded as customer prepayment and auto-applied to future COD orders. Confirm?",
        "uz": "Siz {amount} kiritdingiz, ammo mijozning naqd to'lov qarzi atigi {outstanding}. Ortiqcha {overpayment} mijozning oldindan to'lovi sifatida qayd qilinadi va keyingi naqd to'lov buyurtmalariga avtomatik qo'llaniladi. Tasdiqlaysizmi?",
        "ru": "Вы ввели {amount}, но задолженность клиента по наложенному платежу — всего {outstanding}. Излишек {overpayment} будет записан как предоплата клиента и автоматически применён к будущим заказам с наложенным платежом. Подтверждаете?",
    },
    "staff.delivery.cod_collection_recorded": {
        "en": "Collection recorded successfully.",
        "uz": "Yig'im muvaffaqiyatli qayd qilindi.",
        "ru": "Сбор успешно записан.",
    },
    "staff.delivery.cod_collection_search_prompt": {
        "en": "Search by customer name, phone or order number:",
        "uz": "Mijoz ismi, telefon raqami yoki buyurtma raqami bo'yicha qidiring:",
        "ru": "Поиск по имени клиента, телефону или номеру заказа:",
    },
    "staff.delivery.cod_statement_title": {
        "en": "COD (Cash on Delivery) statement",
        "uz": "Naqd to'lovlar hisoboti",
        "ru": "Отчёт по наложенным платежам",
    },
    "staff.delivery.collect_custom_cod": {
        "en": "Collect custom amount",
        "uz": "Boshqa summa yig'ish",
        "ru": "Получить другую сумму",
    },
    "staff.delivery.collect_full_cod": {
        "en": "Collect full COD (Cash on Delivery)",
        "uz": "To'liq naqd to'lov qarzini yig'ish",
        "ru": "Получить долг полностью",
    },
    "staff.delivery.collection_notes_required": {
        "en": "A note is required for this collection. Please send the note text.",
        "uz": "Ushbu yig'im uchun izoh talab qilinadi. Iltimos, izoh matnini yuboring.",
        "ru": "Для этого сбора требуется примечание. Отправьте текст примечания.",
    },
    "staff.delivery.invalid_cash_amount": {
        "en": "Invalid cash amount. Please enter a positive number.",
        "uz": "Noto'g'ri naqd summa. Iltimos, musbat son kiriting.",
        "ru": "Некорректная сумма. Введите положительное число.",
    },
    "staff.delivery.no_cod_debt": {
        "en": "No outstanding COD (Cash on Delivery) debt.",
        "uz": "To'lanmagan naqd to'lov qarzi yo'q.",
        "ru": "Нет непогашенного долга по наложенным платежам.",
    },
    "staff.delivery.no_customer_cod_results": {
        "en": "No customers with outstanding COD (Cash on Delivery) found for \"{query}\".",
        "uz": "\"{query}\" bo'yicha to'lanmagan naqd to'lov bo'lgan mijozlar topilmadi.",
        "ru": "По запросу \"{query}\" клиенты с непогашенным наложенным платежом не найдены.",
    },
    "staff.delivery.risk_flags": {
        "en": "Risk flags",
        "uz": "Xavf belgilari",
        "ru": "Признаки риска",
    },
    "staff.delivery.total_outstanding": {
        "en": "Total outstanding",
        "uz": "Jami qoldiq",
        "ru": "Общий остаток",
    },
    "staff.delivery.view_cod_statement": {
        "en": "View COD (Cash on Delivery) statement",
        "uz": "Naqd to'lov hisobotini ko'rish",
        "ru": "Посмотреть отчёт по наложенным платежам",
    },
    "staff.order.unknown": {
        "en": "Unknown order",
        "uz": "Noma'lum buyurtma",
        "ru": "Неизвестный заказ",
    },
}


ROLE_TRANSLATIONS = {
    "delivery_driver": {"en": "Delivery Driver", "uz": "Kuryer", "ru": "Курьер"},
    "operator": {"en": "Operator", "uz": "Operator", "ru": "Оператор"},
}

DELIVERY_STATUS_TRANSLATIONS = {
    "assigned": {"en": "Assigned", "uz": "Biriktirilgan", "ru": "Назначен"},
    "picked_up": {"en": "Picked Up", "uz": "Olib ketildi", "ru": "Забран"},
    "in_transit": {"en": "In Transit", "uz": "Yolda", "ru": "В пути"},
    "arrived": {"en": "Arrived", "uz": "Yetib keldi", "ru": "Прибыл"},
    "delivered": {"en": "Delivered", "uz": "Yetkazildi", "ru": "Доставлен"},
    "failed": {"en": "Failed", "uz": "Muvaffaqiyatsiz", "ru": "Неудачно"},
}

FAILED_REASON_TRANSLATIONS = {
    "customer_unavailable": {
        "en": "Customer unavailable",
        "uz": "Mijoz javob bermadi",
        "ru": "Клиент недоступен",
    },
    "customer_refused": {
        "en": "Customer refused",
        "uz": "Mijoz rad etdi",
        "ru": "Клиент отказался",
    },
    "wrong_address": {
        "en": "Wrong address",
        "uz": "Noto'gri manzil",
        "ru": "Неправильный адрес",
    },
    "product_damaged": {
        "en": "Product damaged",
        "uz": "Mahsulot shikastlangan",
        "ru": "Товар поврежден",
    },
    "other": {
        "en": "Other",
        "uz": "Boshqa",
        "ru": "Другое",
    },
}

PAYMENT_TRANSLATIONS = {
    "cash": {"en": "Cash", "uz": "Naqd", "ru": "Наличные"},
    "card": {"en": "Card", "uz": "Karta", "ru": "Карта"},
    "payme": {"en": "Payme", "uz": "Payme", "ru": "Payme"},
    "click": {"en": "Click", "uz": "Click", "ru": "Click"},
    "loyalty_points": {
        "en": "Loyalty Points",
        "uz": "Bonus ballari",
        "ru": "Бонусные баллы",
    },
    "business_account": {
        "en": "Business Account",
        "uz": "Biznes hisobi",
        "ru": "Бизнес-счет",
    },
}

ORDER_STATUS_TRANSLATIONS = {
    "pending": {"en": "Pending", "uz": "Kutilmoqda", "ru": "В ожидании"},
    "confirmed": {"en": "Confirmed", "uz": "Tasdiqlangan", "ru": "Подтвержден"},
    "preparing": {"en": "Preparing", "uz": "Tayyorlanmoqda", "ru": "Готовится"},
    "out_for_delivery": {"en": "Out for Delivery", "uz": "Yetkazishda", "ru": "На доставке"},
    "delivered": {"en": "Delivered", "uz": "Yetkazildi", "ru": "Доставлен"},
    "cancelled": {"en": "Cancelled", "uz": "Bekor qilingan", "ru": "Отменен"},
    "returned": {"en": "Returned", "uz": "Qaytarilgan", "ru": "Возвращен"},
}

EXTRA_TRANSLATIONS = {
    "staff.addresses": {"en": "Addresses", "uz": "Manzillar", "ru": "Адреса"},
    "staff.orders": {"en": "Orders", "uz": "Buyurtmalar", "ru": "Заказы"},
    "staff.items": {"en": "items", "uz": "mahsulot", "ru": "позиций"},
    "staff.auth_cancelled": {"en": "Authentication cancelled.", "uz": "Autentifikatsiya bekor qilindi.", "ru": "Авторизация отменена."},
    "staff.login_failed": {
        "en": "Login failed: {error}",
        "uz": "Kirish muvaffaqiyatsiz: {error}",
        "ru": "Вход не выполнен: {error}",
    },
    "staff.login_success": {
        "en": "Welcome, {name}! Role: {role}",
        "uz": "Xush kelibsiz, {name}! Rol: {role}",
        "ru": "Добро пожаловать, {name}! Роль: {role}",
    },
    "staff.not_staff": {
        "en": "Your account does not have staff bot access.",
        "uz": "Hisobingizda staff botga kirish huquqi yoq.",
        "ru": "У вашей учетной записи нет доступа к боту сотрудников.",
    },
    "staff.welcome_back": {
        "en": "Welcome back, {name}!",
        "uz": "Qaytganingiz bilan, xush kelibsiz, {name}!",
        "ru": "С возвращением, {name}!",
    },
    "staff.help.text": {
        "en": "Use the menu below to manage deliveries and operator tasks.",
        "uz": "Yetkazish va operator amallarini boshqarish uchun menyudan foydalaning.",
        "ru": "Используйте меню ниже для управления доставками и задачами оператора.",
    },
    "staff.help.delivery": {
        "en": "Delivery: take orders, update statuses, and share location when needed.",
        "uz": "Kuryer: buyurtmalarni qabul qiling, holatni yangilang va kerak bolganda lokatsiyani ulashing.",
        "ru": "Курьер: принимайте заказы, обновляйте статусы и при необходимости отправляйте геолокацию.",
    },
    "staff.help.operator": {
        "en": "Operator: create clients, manage addresses, and place phone orders.",
        "uz": "Operator: mijoz yarating, manzillarni boshqaring va telefon buyurtmalarini yarating.",
        "ru": "Оператор: создавайте клиентов, управляйте адресами и оформляйте заказы по телефону.",
    },
    "staff.profile.title": {"en": "Profile", "uz": "Profil", "ru": "Профиль"},
    "staff.profile.name": {"en": "Name", "uz": "Ism", "ru": "Имя"},
    "staff.profile.phone": {"en": "Phone", "uz": "Telefon", "ru": "Телефон"},
    "staff.profile.roles": {"en": "Roles", "uz": "Rollar", "ru": "Роли"},
    "staff.profile.language": {"en": "Language", "uz": "Til", "ru": "Язык"},
    "staff.stats.title": {"en": "My Stats", "uz": "Mening statistikam", "ru": "Моя статистика"},
    "staff.stats.total": {"en": "Total deliveries", "uz": "Jami yetkazishlar", "ru": "Всего доставок"},
    "staff.stats.completed": {"en": "Completed", "uz": "Bajarilgan", "ru": "Завершено"},
    "staff.stats.failed": {"en": "Failed", "uz": "Muvaffaqiyatsiz", "ru": "Неудачно"},
    "staff.stats.avg_time": {"en": "Average time", "uz": "Ortacha vaqt", "ru": "Среднее время"},
    "staff.stats.rating": {"en": "Rating", "uz": "Reyting", "ru": "Рейтинг"},
    "staff.stats.cash": {"en": "Cash collected", "uz": "Yigilgan naqd", "ru": "Собранные наличные"},
    "staff.stats.period.day": {"en": "Day", "uz": "Kun", "ru": "День"},
    "staff.stats.period.week": {"en": "Week", "uz": "Hafta", "ru": "Неделя"},
    "staff.stats.period.month": {"en": "Month", "uz": "Oy", "ru": "Месяц"},
    # ---------------------------------------------------------------- #
    # Backend-emitted staff Telegram notifications (B-1).               #
    # business_app composes these in the driver's preferred_language    #
    # via business_app.utils.translations.get_translation, then sends   #
    # them through NotificationService.send_staff_telegram_message.     #
    # ---------------------------------------------------------------- #
    "staff.notification.reconciliation_reminder_due": {
        "en": "🔔 Reminder: cash reconciliation for {date} is pending. Expected on-hand cash: {expected_cash} UZS.",
        "uz": "🔔 Eslatma: {date} sanasidagi naqd hisobotini topshirish kutilmoqda. Kutilayotgan naqd: {expected_cash} soʻm.",
        "ru": "🔔 Напоминание: сверка наличных за {date} ещё не сдана. Ожидаемый остаток: {expected_cash} сум.",
    },
    "staff.notification.reconciliation_reminder_overdue": {
        "en": "⚠️ Cash session warning: session started {date} is 7+ days old. Expected on-hand cash: {expected_cash} UZS.",
        "uz": "⚠️ Naqd sessiya ogohlantirishi: {date} boshlangan sessiya 7 kundan oshdi. Kutilayotgan naqd: {expected_cash} soʻm.",
        "ru": "⚠️ Предупреждение по наличным: сессии от {date} уже 7+ дней. Ожидаемый остаток: {expected_cash} сум.",
    },
    "staff.notification.manager_exception_summary": {
        "en": "There are {count} driver cash sessions with mismatch or 7+ day warning status requiring review.",
        "uz": "{count} ta haydovchi naqd sessiyasida farq yoki 7+ kunlik ogohlantirish bor, ko'rib chiqish kerak.",
        "ru": "Есть {count} сессий наличных курьеров с расхождением или предупреждением 7+ дней.",
    },
    "staff.notification.subject.driver_cash_reconciliation": {
        "en": "Driver cash reconciliation",
        "uz": "Haydovchi naqd hisoboti",
        "ru": "Сверка наличных курьера",
    },
    "staff.notification.subject.driver_cash_exceptions": {
        "en": "Driver cash exceptions",
        "uz": "Haydovchi naqd istisnolari",
        "ru": "Расхождения наличных курьеров",
    },
    "staff.notification.bottle_session_reopened": {
        "en": "🔓 Your bottle session #{session_id} was reopened by admin because order #{order_id} was edited after delivery. Please re-close the session when you're ready so admin can verify it.",
        "uz": "🔓 Sizning #{session_id} idishlar sessiyangiz admin tomonidan qayta ochildi (buyurtma #{order_id} yetkazib berilgandan keyin tahrirlandi). Iltimos, tayyor bo'lsangiz sessiyani qayta yoping.",
        "ru": "🔓 Ваша сессия по таре #{session_id} была переоткрыта администратором (заказ #{order_id} изменён после доставки). Пожалуйста, закройте сессию заново для проверки.",
    },
}

DELIVERY_TEXT_TRANSLATIONS = {
    "accept": {"en": "Accept", "uz": "Qabul qilish", "ru": "Принять"},
    "accepted_success": {"en": "Order accepted successfully.", "uz": "Buyurtma muvaffaqiyatli qabul qilindi.", "ru": "Заказ успешно принят."},
    "active_count": {"en": "{count} active deliveries", "uz": "{count} ta faol yetkazish", "ru": "{count} активных доставок"},
    "active_title": {"en": "Active Deliveries", "uz": "Faol yetkazishlar", "ru": "Активные доставки"},
    "already_taken": {"en": "This order is already taken.", "uz": "Bu buyurtma allaqachon olingan.", "ru": "Этот заказ уже принят."},
    "cash_collection": {"en": "Confirm collected cash: {amount}", "uz": "Qabul qilingan naqdni tasdiqlang: {amount}", "ru": "Подтвердите собранные наличные: {amount}"},
    "cash_recorded": {"en": "Cash recorded: {amount}", "uz": "Naqd qayd etildi: {amount}", "ru": "Наличные зафиксированы: {amount}"},
    "confirm_accept": {"en": "Do you want to accept this order?", "uz": "Bu buyurtmani qabul qilasizmi?", "ru": "Принять этот заказ?"},
    "confirm_cash": {"en": "Confirm cash {amount}", "uz": "{amount} naqdni tasdiqlash", "ru": "Подтвердить наличные {amount}"},
    "confirm_status": {"en": "Confirm status update to: {status}?", "uz": "Holatni quyidagiga ozgartirishni tasdiqlaysizmi: {status}?", "ru": "Подтвердить изменение статуса на: {status}?"},
    "current_status": {"en": "Current status", "uz": "Joriy holat", "ru": "Текущий статус"},
    "delivered_success": {"en": "Delivery marked as delivered.", "uz": "Yetkazish bajarildi deb belgilandi.", "ru": "Доставка отмечена как выполненная."},
    "edit_cash": {"en": "Edit cash amount", "uz": "Naqd summasini ozgartirish", "ru": "Изменить сумму наличных"},
    "enter_cash_amount": {"en": "Enter collected cash amount:", "uz": "Qabul qilingan naqd summasini kiriting:", "ru": "Введите сумму собранных наличных:"},
    "fail_reason_label": {"en": "Reason", "uz": "Sabab", "ru": "Причина"},
    "history_title": {"en": "Delivery History", "uz": "Yetkazish tarixi", "ru": "История доставок"},
    "invalid_amount": {"en": "Invalid amount. Please enter a valid number.", "uz": "Noto'gri summa. Iltimos, togri son kiriting.", "ru": "Некорректная сумма. Введите правильное число."},
    "items": {"en": "Items", "uz": "Mahsulotlar", "ru": "Позиции"},
    "manage": {"en": "Manage", "uz": "Boshqarish", "ru": "Управлять"},
    "mark_preparing": {"en": "Mark as Preparing", "uz": "Tayyorlanmoqda deb belgilash", "ru": "Отметить как готовится"},
    "marked_failed": {"en": "Delivery marked as failed.", "uz": "Yetkazish muvaffaqiyatsiz deb belgilandi.", "ru": "Доставка отмечена как неудачная."},
    "marked_preparing": {"en": "Order marked as preparing.", "uz": "Buyurtma tayyorlanmoqda deb belgilandi.", "ru": "Заказ отмечен как готовится."},
    "navigate": {"en": "Navigate", "uz": "Yonaltirish", "ru": "Навигация"},
    "navigate_text": {"en": "Open route in map", "uz": "Marshrutni xaritada ochish", "ru": "Открыть маршрут на карте"},
    "no_active": {"en": "No active deliveries.", "uz": "Faol yetkazishlar yoq.", "ru": "Нет активных доставок."},
    "no_address": {"en": "Address coordinates are not available.", "uz": "Manzil koordinatalari mavjud emas.", "ru": "Координаты адреса недоступны."},
    "no_history": {"en": "No delivery history yet.", "uz": "Hozircha yetkazish tarixi yoq.", "ru": "История доставок пока пуста."},
    "not_found": {"en": "Delivery not found.", "uz": "Yetkazish topilmadi.", "ru": "Доставка не найдена."},
    "open_maps": {"en": "Open Maps", "uz": "Xaritani ochish", "ru": "Открыть карты"},
    "order_not_found": {"en": "Order not found.", "uz": "Buyurtma topilmadi.", "ru": "Заказ не найден."},
    "pool_count": {"en": "{count} orders available", "uz": "{count} ta buyurtma mavjud", "ru": "Доступно заказов: {count}"},
    "pool_empty": {"en": "No available orders in the pool.", "uz": "Havzada mavjud buyurtmalar yoq.", "ru": "В списке нет доступных заказов."},
    "pool_title": {"en": "Order Pool", "uz": "Buyurtmalar havzasi", "ru": "Список заказов"},
    "select_fail_reason": {"en": "Select failure reason:", "uz": "Muvaffaqiyatsizlik sababini tanlang:", "ru": "Выберите причину неудачи:"},
    "share_location_prompt": {"en": "Please share your current location.", "uz": "Iltimos, joriy lokatsiyangizni yuboring.", "ru": "Пожалуйста, отправьте вашу текущую геолокацию."},
    "status_updated": {"en": "Status updated to: {status}", "uz": "Holat yangilandi: {status}", "ru": "Статус обновлен: {status}"},
    "view_details": {"en": "View details", "uz": "Batafsil korish", "ru": "Посмотреть детали"},
}

OPERATOR_TEXT_TRANSLATIONS = {
    "add_address": {"en": "Add Address", "uz": "Manzil qoshish", "ru": "Добавить адрес"},
    "add_more_or_done": {"en": "Add more products or finish selection.", "uz": "Yana mahsulot qoshing yoki yakunlang.", "ru": "Добавьте еще товары или завершите выбор."},
    "address_saved": {"en": "Address saved successfully.", "uz": "Manzil muvaffaqiyatli saqlandi.", "ru": "Адрес успешно сохранен."},
    "addresses_title": {"en": "Client Addresses", "uz": "Mijoz manzillari", "ru": "Адреса клиента"},
    "cart": {"en": "Cart", "uz": "Savat", "ru": "Корзина"},
    "cart_empty": {"en": "Cart is empty.", "uz": "Savat bosh.", "ru": "Корзина пуста."},
    "confirm_address": {"en": "Confirm Address", "uz": "Manzilni tasdiqlash", "ru": "Подтвердить адрес"},
    "confirm_create_user": {"en": "Confirm Client Creation", "uz": "Mijoz yaratishni tasdiqlang", "ru": "Подтвердите создание клиента"},
    "confirm_order": {"en": "Confirm Order", "uz": "Buyurtmani tasdiqlash", "ru": "Подтвердить заказ"},
    "confirm_order_prompt": {"en": "Confirm order creation?", "uz": "Buyurtma yaratishni tasdiqlaysizmi?", "ru": "Подтвердить создание заказа?"},
    "create_order_for": {"en": "Create Order for Client", "uz": "Mijoz uchun buyurtma yaratish", "ru": "Создать заказ для клиента"},
    "create_user": {"en": "Create Client", "uz": "Mijoz yaratish", "ru": "Создать клиента"},
    "done_selecting": {"en": "Done selecting", "uz": "Tanlash yakunlandi", "ru": "Выбор завершен"},
    "enter_address_label": {"en": "Enter address label (e.g., Home, Office):", "uz": "Manzil nomini kiriting (masalan, Uy, Ofis):", "ru": "Введите название адреса (например, Дом, Офис):"},
    "enter_delivery_notes": {"en": "Enter delivery notes or type '-' to skip:", "uz": "Yetkazish izohini kiriting yoki '-' deb yozib otkazing:", "ru": "Введите примечание к доставке или '-' для пропуска:"},
    "enter_district": {"en": "Enter district or type '-' to skip:", "uz": "Tuman nomini kiriting yoki '-' deb yozib otkazing:", "ru": "Введите район или '-' для пропуска:"},
    "enter_first_name": {"en": "Enter first name:", "uz": "Ismni kiriting:", "ru": "Введите имя:"},
    "enter_full_address": {"en": "Enter full address:", "uz": "Tolik manzilni kiriting:", "ru": "Введите полный адрес:"},
    "enter_last_name": {"en": "Enter last name or '-' to skip:", "uz": "Familiyani kiriting yoki '-' deb yozib otkazing:", "ru": "Введите фамилию или '-' для пропуска:"},
    "enter_notes": {"en": "Enter order notes or skip:", "uz": "Buyurtma izohini kiriting yoki otkazing:", "ru": "Введите примечание к заказу или пропустите:"},
    "enter_phone": {"en": "Enter phone number:", "uz": "Telefon raqamini kiriting:", "ru": "Введите телефон:"},
    "invalid_address": {"en": "Address is too short.", "uz": "Manzil juda qisqa.", "ru": "Адрес слишком короткий."},
    "invalid_label": {"en": "Invalid label. Try again.", "uz": "Noto'gri nom. Qayta urinib koring.", "ru": "Некорректная метка. Повторите."},
    "invalid_name": {"en": "Invalid name format.", "uz": "Ism formati noto'gri.", "ru": "Некорректный формат имени."},
    "invalid_phone": {"en": "Invalid phone number format.", "uz": "Telefon raqami formati noto'gri.", "ru": "Некорректный формат телефона."},
    "manage_addresses": {"en": "Manage Addresses", "uz": "Manzillarni boshqarish", "ru": "Управлять адресами"},
    "no_addresses": {"en": "No addresses found for this client.", "uz": "Bu mijoz uchun manzillar topilmadi.", "ru": "У этого клиента нет адресов."},
    "no_items_selected": {"en": "No items selected yet.", "uz": "Hali mahsulot tanlanmagan.", "ru": "Товары еще не выбраны."},
    "no_products": {"en": "No products available.", "uz": "Mahsulotlar mavjud emas.", "ru": "Товары недоступны."},
    "no_recent_orders": {"en": "No recent orders yet.", "uz": "Hozircha songgi buyurtmalar yoq.", "ru": "Пока нет последних заказов."},
    "no_results": {"en": "No clients found for '{query}'.", "uz": "'{query}' boyicha mijoz topilmadi.", "ru": "По запросу '{query}' клиенты не найдены."},
    "order_created": {"en": "Order #{order_number} created successfully.", "uz": "#{order_number} buyurtma muvaffaqiyatli yaratildi.", "ru": "Заказ #{order_number} успешно создан."},
    "order_enter_phone": {"en": "Enter client phone number to create order:", "uz": "Buyurtma yaratish uchun mijoz telefonini kiriting:", "ru": "Введите телефон клиента для создания заказа:"},
    "recent_orders_title": {"en": "Recent Operator Orders", "uz": "Operatorning songgi buyurtmalari", "ru": "Последние заказы оператора"},
    "search_again": {"en": "Search again", "uz": "Yana qidirish", "ru": "Искать снова"},
    "search_prompt": {"en": "Enter phone or name to search client:", "uz": "Mijozni qidirish uchun telefon yoki ism kiriting:", "ru": "Введите телефон или имя для поиска клиента:"},
    "search_results": {"en": "Found clients: {count}", "uz": "Topilgan mijozlar: {count}", "ru": "Найдено клиентов: {count}"},
    "search_too_short": {"en": "Search query is too short.", "uz": "Qidiruv sorovi juda qisqa.", "ru": "Слишком короткий поисковый запрос."},
    "select_address": {"en": "Select delivery address:", "uz": "Yetkazish manzilini tanlang:", "ru": "Выберите адрес доставки:"},
    "select_client_language": {"en": "Select client language:", "uz": "Mijoz tilini tanlang:", "ru": "Выберите язык клиента:"},
    "select_payment": {"en": "Select payment method:", "uz": "Tolov usulini tanlang:", "ru": "Выберите способ оплаты:"},
    "select_products": {"en": "Select products:", "uz": "Mahsulotlarni tanlang:", "ru": "Выберите товары:"},
    "select_quantity": {"en": "Select quantity:", "uz": "Miqdorni tanlang:", "ru": "Выберите количество:"},
    "skip_notes": {"en": "Skip notes", "uz": "Izohsiz davom etish", "ru": "Пропустить примечание"},
    "subtotal": {"en": "Subtotal", "uz": "Oraliq jami", "ru": "Промежуточный итог"},
    "user_already_exists": {"en": "A client with this phone already exists.", "uz": "Bu telefon bilan mijoz allaqachon mavjud.", "ru": "Клиент с таким телефоном уже существует."},
    "user_created": {"en": "Client created successfully.", "uz": "Mijoz muvaffaqiyatli yaratildi.", "ru": "Клиент успешно создан."},
    "user_exists": {"en": "Client already exists.", "uz": "Mijoz allaqachon mavjud.", "ru": "Клиент уже существует."},
}


TOKEN_TRANSLATIONS = {
    "uz": {
        "address": "manzil",
        "active": "faol",
        "add": "qoshish",
        "again": "yana",
        "already": "allaqachon",
        "api": "API",
        "assigned": "biriktirilgan",
        "auth": "auth",
        "available": "mavjud",
        "back": "orqaga",
        "cancel": "bekor",
        "cash": "naqd",
        "changed": "ozgardi",
        "client": "mijoz",
        "collection": "yigim",
        "confirm": "tasdiq",
        "count": "soni",
        "created": "yaratildi",
        "current": "joriy",
        "delivery": "yetkazish",
        "details": "batafsil",
        "enter": "kiriting",
        "error": "xatolik",
        "failed": "muvaffaqiyatsiz",
        "history": "tarix",
        "invalid": "noto'gri",
        "items": "mahsulotlar",
        "language": "til",
        "login": "kirish",
        "menu": "menyu",
        "my": "mening",
        "new": "yangi",
        "no": "yoq",
        "not": "emas",
        "notification": "bildirishnoma",
        "operator": "operator",
        "order": "buyurtma",
        "payment": "tolov",
        "phone": "telefon",
        "pool": "havza",
        "profile": "profil",
        "prompt": "sorov",
        "results": "natijalar",
        "search": "qidiruv",
        "select": "tanlash",
        "session": "sessiya",
        "settings": "sozlamalar",
        "share": "ulashish",
        "stats": "statistika",
        "status": "holat",
        "success": "muvaffaqiyat",
        "title": "sarlavha",
        "too": "juda",
        "updated": "yangilandi",
        "user": "foydalanuvchi",
        "welcome": "xush kelibsiz",
    },
    "ru": {
        "address": "адрес",
        "active": "активный",
        "add": "добавить",
        "again": "снова",
        "already": "уже",
        "api": "API",
        "assigned": "назначен",
        "auth": "авторизация",
        "available": "доступен",
        "back": "назад",
        "cancel": "отмена",
        "cash": "наличные",
        "changed": "изменен",
        "client": "клиент",
        "collection": "сбор",
        "confirm": "подтвердить",
        "count": "количество",
        "created": "создано",
        "current": "текущий",
        "delivery": "доставка",
        "details": "детали",
        "enter": "введите",
        "error": "ошибка",
        "failed": "неудачно",
        "history": "история",
        "invalid": "некорректно",
        "items": "товары",
        "language": "язык",
        "login": "вход",
        "menu": "меню",
        "my": "мой",
        "new": "новый",
        "no": "нет",
        "not": "не",
        "notification": "уведомление",
        "operator": "оператор",
        "order": "заказ",
        "payment": "оплата",
        "phone": "телефон",
        "pool": "список",
        "profile": "профиль",
        "prompt": "запрос",
        "results": "результаты",
        "search": "поиск",
        "select": "выберите",
        "session": "сессия",
        "settings": "настройки",
        "share": "отправить",
        "stats": "статистика",
        "status": "статус",
        "success": "успешно",
        "title": "заголовок",
        "too": "слишком",
        "updated": "обновлен",
        "user": "пользователь",
        "welcome": "добро пожаловать",
    },
}


def _extract_literal_keys(repo_root: Path) -> Set[str]:
    """Collect literal keys from i18n.get('...') calls in staff bot files."""
    pattern = re.compile(r"""i18n\.get\(\s*(['"])([^'"]+)\1\s*[,)]""")
    keys: Set[str] = set()

    staff_root = repo_root / "staff_bot"
    for path in staff_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for _, match in pattern.findall(text):
            if match.startswith("staff."):
                keys.add(match)
    return keys


def _add_dynamic_keys(keys: Set[str]) -> None:
    """Add f-string based key families that static regex cannot enumerate."""
    # Role labels
    for role in STAFF_BOT_ROLES:
        keys.add(f"staff.role.{role}")

    # Delivery statuses
    for status in ("assigned", "picked_up", "in_transit", "arrived", "delivered", "failed"):
        keys.add(f"staff.delivery.status.{status}")

    # Failure reasons
    for reason in FAILED_DELIVERY_REASONS:
        keys.add(f"staff.delivery.reason.{reason}")

    # Payment labels
    for payment in PaymentMethod:
        keys.add(f"staff.delivery.payment.{payment.value}")
        keys.add(f"staff.operator.payment_{payment.value}")

    # Order status labels (operator order-pool details)
    for status in OrderStatus:
        keys.add(f"staff.order.status.{status.value}")


def _add_curated_keys(keys: Set[str]) -> None:
    """
    Add all curated key catalogs.

    This makes seeding deterministic even when staff_bot source files are not
    available in the current runtime (e.g. business_app container).
    """
    keys.update(STAFF_TRANSLATIONS.keys())
    keys.update(EXTRA_TRANSLATIONS.keys())

    for suffix in DELIVERY_TEXT_TRANSLATIONS.keys():
        keys.add(f"staff.delivery.{suffix}")

    for suffix in OPERATOR_TEXT_TRANSLATIONS.keys():
        keys.add(f"staff.operator.{suffix}")


def _auto_family_translation(key: str, language: str) -> Optional[str]:
    """Resolve known dynamic key families."""
    extra = EXTRA_TRANSLATIONS.get(key, {})
    if language in extra:
        return extra[language]
    if "en" in extra:
        return extra["en"]

    if key.startswith("staff.delivery."):
        suffix = key.split("staff.delivery.", 1)[1]
        if suffix in DELIVERY_TEXT_TRANSLATIONS:
            scoped = DELIVERY_TEXT_TRANSLATIONS[suffix]
            if language in scoped:
                return scoped[language]
            if "en" in scoped:
                return scoped["en"]

    if key.startswith("staff.operator."):
        suffix = key.split("staff.operator.", 1)[1]
        if suffix in OPERATOR_TEXT_TRANSLATIONS:
            scoped = OPERATOR_TEXT_TRANSLATIONS[suffix]
            if language in scoped:
                return scoped[language]
            if "en" in scoped:
                return scoped["en"]

    if key.startswith("staff.role."):
        role = key.rsplit(".", 1)[-1]
        return ROLE_TRANSLATIONS.get(role, {}).get(language)

    if key.startswith("staff.delivery.status."):
        status = key.rsplit(".", 1)[-1]
        return DELIVERY_STATUS_TRANSLATIONS.get(status, {}).get(language)

    if key.startswith("staff.delivery.reason."):
        reason = key.rsplit(".", 1)[-1]
        return FAILED_REASON_TRANSLATIONS.get(reason, {}).get(language)

    if key.startswith("staff.delivery.payment."):
        payment = key.rsplit(".", 1)[-1]
        return PAYMENT_TRANSLATIONS.get(payment, {}).get(language)

    if key.startswith("staff.operator.payment_"):
        payment = key.split("staff.operator.payment_", 1)[1]
        return PAYMENT_TRANSLATIONS.get(payment, {}).get(language)

    if key.startswith("staff.order.status."):
        status = key.rsplit(".", 1)[-1]
        return ORDER_STATUS_TRANSLATIONS.get(status, {}).get(language)

    return None


def _humanize_key(key: str, language: str) -> str:
    """Convert key tail into a readable fallback phrase with light localization."""
    tail = key.split("staff.", 1)[-1] if key.startswith("staff.") else key
    tokens = [token for token in re.split(r"[._]", tail) if token]

    if language in TOKEN_TRANSLATIONS:
        translated_tokens = [TOKEN_TRANSLATIONS[language].get(token, token) for token in tokens]
    else:
        translated_tokens = tokens

    phrase = " ".join(translated_tokens).strip()
    if not phrase:
        return key
    return phrase[0].upper() + phrase[1:]


def _resolve_value(key: str, language: str) -> str:
    """Resolve translation value from curated map, dynamic families, or fallback."""
    curated = STAFF_TRANSLATIONS.get(key, {})
    if language in curated:
        return curated[language]
    if "en" in curated:
        return curated["en"]

    dynamic_value = _auto_family_translation(key, language)
    if dynamic_value:
        return dynamic_value

    # Fallback for uncatalogued keys.
    return _humanize_key(key, language)


def _validate_russian_translations(keys: Set[str]) -> None:
    """
    Ensure generated RU translations are readable Cyrillic text.
    Allows placeholders and known Latin brand tokens only.
    """
    invalid: list[tuple[str, str]] = []

    for key in sorted(keys):
        value = _resolve_value(key, "ru")
        normalized = re.sub(r"\{[^{}]+\}", "", value)
        for token in RU_ALLOWED_LATIN_TOKENS:
            normalized = re.sub(re.escape(token), "", normalized, flags=re.IGNORECASE)

        if re.search(r"[A-Za-z]", normalized):
            invalid.append((key, value))

    if invalid:
        preview = "\n".join(f"  - {key}: {value}" for key, value in invalid[:15])
        raise ValueError(
            "Russian translation quality check failed: Latin transliteration detected.\n"
            f"{preview}\n"
            f"Total invalid RU values: {len(invalid)}"
        )


def main() -> int:
    app = create_app()
    repo_root = Path(__file__).resolve().parents[1]

    with app.app_context():
        keys = _extract_literal_keys(repo_root)
        _add_dynamic_keys(keys)
        _add_curated_keys(keys)
        # _validate_russian_translations(keys)

        total_keys = len(keys)
        created = 0
        updated = 0

        for key in sorted(keys):
            for lang in LANGUAGES:
                value = _resolve_value(key, lang)
                existing = Translation.query.filter_by(key=key, language=lang).first()
                if existing:
                    existing.value = value
                    existing.category = "staff_bot"
                    existing.is_active = True
                    updated += 1
                else:
                    db.session.add(
                        Translation(
                            key=key,
                            language=lang,
                            value=value,
                            category="staff_bot",
                            is_active=True,
                        )
                    )
                    created += 1

        db.session.commit()

        print(
            f"Staff translations seeded: keys={total_keys}, "
            f"created={created}, updated={updated}"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
