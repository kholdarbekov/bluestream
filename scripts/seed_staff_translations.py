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
RU_ALLOWED_LATIN_TOKENS = ("BlueStream", "Payme", "Click", "UZS", "API")


# Curated high-value strings.
STAFF_TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "staff.menu.title": {
        "en": "Staff Bot - Main Menu",
        "uz": "Xodimlar boti - Asosiy menyu",
        "ru": "Бот сотрудников BlueStream - Главное меню",
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
        "en": "Welcome to BlueStream Staff Bot!\n\nPlease select your language:",
        "uz": "BlueStream xodimlar botiga xush kelibsiz!\n\nIltimos, tilni tanlang:",
        "ru": "Добро пожаловать в бот сотрудников BlueStream!\n\nПожалуйста, выберите язык:",
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
        _validate_russian_translations(keys)

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
