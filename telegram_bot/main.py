import os
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
import json

import asyncpg
import redis.asyncio as redis
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ParseMode
import httpx
from dotenv import load_dotenv
load_dotenv()

from services import (
    NotificationService,
    PaymentService,
    DeliveryService,
    AnalyticsService,
    SecurityService,
    AdminService,
    OrderService,
    ProductService,
    SubscriptionService,
    UserService,
    AddressService,
)

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

ORDER_STATE = {
    'SELECT_PRODUCT': 'select_product',
    'SELECT_QUANTITY': 'select_quantity',
    'CART': 'cart',
    'DELIVERY_SLOT': 'delivery_slot',
    'PAYMENT_METHOD': 'payment_method',
    'CONFIRM': 'confirm',
}

class WaterBusinessBot:
    def __init__(self):
        self.bot_token = os.getenv('BOT_TOKEN')
        self.db_url = os.getenv('DATABASE_URL')
        self.redis_url = os.getenv('REDIS_URL')
        self.business_app_url = os.getenv('BUSINESS_APP_URL')
        
        self.db_pool = None
        self.redis_client = None
        self.http_client = None
        # Service attributes (will be set after connections)
        self.notification_service = None
        self.payment_service = None
        self.delivery_service = None
        self.analytics_service = None
        self.security_service = None
        self.admin_service = None
        self.order_service = None
        self.product_service = None
        self.subscription_service = None
        self.user_service = None
        self.address_service = None
        
        # User states for conversation flow
        self.user_states = {}
        
        # Language translations
        self.translations = {
            'en': {
                'welcome': "🌊 Welcome to AquaPure Water Solutions!\n\nI'm here to help you with:\n• Water information & company details\n• Placing orders\n• Tracking deliveries\n• Managing subscriptions\n• Account management",
                'main_menu': "🏠 Main Menu",
                'info_menu': "ℹ️ Information",
                'order_menu': "🛒 Order Water",
                'track_menu': "📦 Track Orders",
                'account_menu': "👤 My Account",
                'help_text': "How can I help you today?"
            },
            'uz': {
                'welcome': "🌊 AquaPure Suv Yechimlari xizmatiga xush kelibsiz!\n\nMen sizga quyidagi masalalarda yordam beraman:\n• Suv va kompaniya haqida ma'lumot\n• Buyurtma berish\n• Yetkazib berish holatini kuzatish\n• Obunalarni boshqarish\n• Hisob boshqaruvi",
                'main_menu': "🏠 Asosiy menyu",
                'info_menu': "ℹ️ Ma'lumot",
                'order_menu': "🛒 Suv buyurtma qilish",
                'track_menu': "📦 Buyurtmalarni kuzatish",
                'account_menu': "👤 Mening hisobim",
                'help_text': "Bugun sizga qanday yordam bera olaman?"
            },
            'ru': {
                'welcome': "🌊 Добро пожаловать в AquaPure Water Solutions!\n\nЯ помогу вам с:\n• Информацией о воде и компании\n• Размещением заказов\n• Отслеживанием доставки\n• Управлением подписками\n• Управлением аккаунтом",
                'main_menu': "🏠 Главное меню",
                'info_menu': "ℹ️ Информация",
                'order_menu': "🛒 Заказать воду",
                'track_menu': "📦 Отследить заказы",
                'account_menu': "👤 Мой аккаунт",
                'help_text': "Как я могу помочь вам сегодня?"
            }
        }
        # Add to self.translations in __init__
        self.translations['en'].update({
            'no_products_subscription': "No products available for subscription.",
            'select_product_to_subscribe': "Select a product to subscribe:",
            'your_subscriptions': "Your subscriptions (tap to cancel):",
            'no_active_subscriptions': "You have no active subscriptions.",
            'subscription_created': "✅ Subscription created! You will receive regular deliveries.",
            'subscription_failed': "❌ Failed to create subscription.",
            'subscription_cancelled': "Subscription cancelled.",
            'subscription_cancel_failed': "Failed to cancel subscription.",
            'no_recent_orders': "You have no recent orders.",
            'your_recent_orders': "Your recent orders:",
            'no_loyalty_transactions': "No loyalty point transactions yet.",
            'pending_orders': "Pending Orders:",
            'no_pending_orders': "No pending orders.",
            'not_admin': "You are not an admin.",
            'order_success': "✅ Order placed successfully! You will be notified about delivery.",
            'order_error': "❌ Failed to place order. Please try again later.",
            'order_cancelled': "Order cancelled.",
            'cart_empty': "Your cart is empty.",
            'select_product': "Select a product to order:",
            'select_quantity': "How many '{product}' would you like to order?",
            'cart': "Cart:",
            'proceed_to_delivery': "Proceed to Delivery",
            'add_more': "Add more",
            'cancel': "Cancel",
            'select_delivery_slot': "Select a delivery slot (Delivery fee: {fee} UZS):",
            'choose_payment_method': "Choose payment method:",
            'order_summary': "Order summary:\n{cart}\nDelivery fee: {fee} UZS\nTotal: {total} UZS\n\nConfirm order?",
            'confirm': "Confirm",
            'back_main': "Back to Main Menu",
            'language_updated': "Language updated!",
            'select_language': "🌐 Select your language:",
            'no_products': "No products available at the moment.",
            'select_product_to_order': "Select a product to order:",
            'location_received': "📍 Location received! I'll use this for delivery.\n\nYou can now proceed with your order or save this address to your profile.",
            'photo_received': "📸 Photo received! Thank you for the delivery confirmation.\n\nYour delivery has been marked as completed.",
            'phone_saved': "📞 Phone number saved! This will help us with delivery coordination.\n\nYou can now place orders with delivery to your location.",
            'error_saving_contact': "Sorry, there was an error saving your contact.",
            'processing_payment': "💳 Processing payment...\n\nPlease wait while we process your payment securely.",
            'error': "Sorry, something went wrong. Please try again or contact support if the problem persists.",
            'order_tracking': "Order Tracking",
            'order_number': "Order Number",
            'status': "Status",
            'address': "Address",
            'slot': "Slot",
            'events': "Events",
            'no_events': "No events.",
            'profile': "Profile",
            'name': "Name",
            'phone': "Phone",
            'email': "Email",
            'vip_status': "VIP Status",
            'loyalty_points': "Loyalty Points",
            'edit_profile': "Edit Profile",
            'manage_addresses': "Manage Addresses",
            'your_addresses': "Your addresses:",
            'address': "Address",
            'default': "Default",
            'set_default': "Set Default",
            'delete': "Delete",
            'add_address': "Add Address",
            'enter_address_line1': "Please enter the address line:",
            'enter_city': "Please enter the city:",
            'address_added': "Address added!",
            'address_deleted': "Address deleted!",
            'default_set': "Default address set!",
            'select_address': "Select a delivery address:",
            'no_addresses': "You have no saved addresses. Please add one to continue.",
            'address_selected': "Address selected!",
            'edit': "Edit",
            'edit_address': "Edit Address",
            'enter_label': "Enter address label (e.g. Home, Office):",
            'enter_address_line2': "Enter address line 2 (or type '-' to skip):",
            'enter_state': "Enter state/region (or type '-' to skip):",
            'enter_postal_code': "Enter postal code (or type '-' to skip):",
            'enter_country': "Enter country (default: UZ):",
            'enter_instructions': "Enter delivery instructions (or type '-' to skip):",
            'address_updated': "Address updated!",
            'order_address': "Delivery Address:",
            'change_address': "Change Address",
            'edit_profile_menu': "What would you like to edit?",
            'edit_name': "Edit Name",
            'edit_phone': "Edit Phone",
            'edit_email': "Edit Email",
            'edit_language': "Edit Language",
            'enter_first_name': "Enter your first name:",
            'enter_last_name': "Enter your last name:",
            'enter_phone': "Enter your phone number:",
            'enter_email': "Enter your email:",
            'phone_verification_code': "Enter the code sent to your phone:",
            'email_verification_code': "Enter the code sent to your email:",
            'profile_updated': "Profile updated!",
            'phone_verified': "Phone verified!",
            'email_verified': "Email verified!",
            'invalid_code': "Invalid code. Please try again.",
            'send_location': "Send your location pin to autofill address.",
            'address_invalid': "Address must have at least a street and city. Please try again.",
            'out_of_stock': "Sorry, one or more products are out of stock. Please edit your cart.",
            'card_payment_confirm': "Please confirm your card payment (simulated). Type 'paid' to continue:",
            'order_cancelled_by_user': "Order cancelled.",
            'orders_list': "Your Orders:",
            'order_details': "Order Details:",
            'cancel_order': "Cancel Order",
            'order_already_delivered': "Order already delivered and cannot be cancelled.",
            'order_cancel_success': "Order cancelled successfully.",
            'my_deliveries': "My Deliveries:",
            'update_status': "Update Status",
            'mark_delivered': "Mark as Delivered",
            'mark_in_transit': "Mark as In Transit",
            'mark_failed': "Mark as Failed",
            'status_updated': "Delivery status updated!",
            'no_deliveries': "No deliveries assigned.",
            'subscription_menu': "Your Subscription:",
            'pause_subscription': "Pause Subscription",
            'resume_subscription': "Resume Subscription",
            'edit_subscription': "Edit Subscription",
            'subscription_paused': "Subscription paused!",
            'subscription_resumed': "Subscription resumed!",
            'subscription_edited': "Subscription updated!",
            'notify_renewal': "Your subscription will renew soon.",
            'notification_prefs': "Notification Preferences:",
            'sms_notifications': "SMS Notifications",
            'email_notifications': "Email Notifications",
            'telegram_notifications': "Telegram Notifications",
            'marketing_communications': "Marketing Communications",
            'prefs_updated': "Preferences updated!",
            'my_deliveries': "My Deliveries:",
            'update_status': "Update Status",
            'mark_delivered': "Mark as Delivered",
            'mark_in_transit': "Mark as In Transit",
            'mark_failed': "Mark as Failed",
            'status_updated': "Delivery status updated!",
            'no_deliveries': "No deliveries assigned.",
            'subscription_menu': "Your Subscription:",
            'pause_subscription': "Pause Subscription",
            'resume_subscription': "Resume Subscription",
            'edit_subscription': "Edit Subscription",
            'subscription_paused': "Subscription paused!",
            'subscription_resumed': "Subscription resumed!",
            'subscription_edited': "Subscription updated!",
            'notify_renewal': "Your subscription will renew soon.",
            'notification_prefs': "Notification Preferences:",
            'sms_notifications': "SMS Notifications",
            'email_notifications': "Email Notifications",
            'telegram_notifications': "Telegram Notifications",
            'marketing_communications': "Marketing Communications",
            'prefs_updated': "Preferences updated!",
            'not_enough_points': "You don't have enough points to redeem.",
            'points_redeemed': "You redeemed {points} points for a {discount} UZS discount!",
            'rate_limited': "You are sending requests too fast. Please try again later.",
            'not_delivery_person': "You are not authorized as a delivery person.",
            'map_link': "View on Map",
        })
        self.translations['uz'].update({
            'no_products_subscription': "Obuna uchun mahsulotlar mavjud emas.",
            'select_product_to_subscribe': "Obuna uchun mahsulotni tanlang:",
            'your_subscriptions': "Sizning obunalaringiz (bekor qilish uchun bosing):",
            'no_active_subscriptions': "Sizda faol obunalar yo'q.",
            'subscription_created': "✅ Obuna yaratildi! Sizga muntazam yetkazib beriladi.",
            'subscription_failed': "❌ Obuna yaratilmadi.",
            'subscription_cancelled': "Obuna bekor qilindi.",
            'subscription_cancel_failed': "Obunani bekor qilishda xatolik.",
            'no_recent_orders': "Sizda so'nggi buyurtmalar yo'q.",
            'your_recent_orders': "Sizning so'nggi buyurtmalaringiz:",
            'no_loyalty_transactions': "Hali sodiqlik ballari tranzaksiyalari yo'q.",
            'pending_orders': "Kutilayotgan buyurtmalar:",
            'no_pending_orders': "Kutilayotgan buyurtmalar yo'q.",
            'not_admin': "Siz admin emassiz.",
            'order_success': "✅ Buyurtma muvaffaqiyatli qabul qilindi! Yetkazib berish haqida xabar beramiz.",
            'order_error': "❌ Buyurtma qabul qilinmadi. Iltimos, keyinroq urinib ko'ring.",
            'order_cancelled': "Buyurtma bekor qilindi.",
            'cart_empty': "Savat bo'sh.",
            'select_product': "Buyurtma uchun mahsulotni tanlang:",
            'select_quantity': "Qancha '{product}' buyurtma qilmoqchisiz?",
            'cart': "Savat:",
            'proceed_to_delivery': "Yetkazib berishga o'tish",
            'add_more': "Yana qo'shish",
            'cancel': "Bekor qilish",
            'select_delivery_slot': "Yetkazib berish vaqtini tanlang (Yetkazib berish narxi: {fee} UZS):",
            'choose_payment_method': "To'lov usulini tanlang:",
            'order_summary': "Buyurtma yakuni:\n{cart}\nYetkazib berish narxi: {fee} UZS\nJami: {total} UZS\n\nBuyurtmani tasdiqlaysizmi?",
            'confirm': "Tasdiqlash",
            'back_main': "Asosiy menyuga qaytish",
            'language_updated': "Til yangilandi!",
            'select_language': "🌐 Tilni tanlang:",
            'no_products': "Hozircha mahsulotlar mavjud emas.",
            'select_product_to_order': "Buyurtma uchun mahsulotni tanlang:",
            'location_received': "📍 Manzil qabul qilindi! Yetkazib berish uchun ushbu manzildan foydalanaman.\n\nEndi buyurtma berishingiz yoki ushbu manzilni profilingizga saqlashingiz mumkin.",
            'photo_received': "📸 Rasm qabul qilindi! Yetkazib berishni tasdiqlaganingiz uchun rahmat.\n\nYetkazib berish yakunlandi.",
            'phone_saved': "📞 Telefon raqami saqlandi! Bu yetkazib berishni muvofiqlashtirishda yordam beradi.\n\nEndi manzilingizga buyurtma berishingiz mumkin.",
            'error_saving_contact': "Kechirasiz, kontaktni saqlashda xatolik yuz berdi.",
            'processing_payment': "💳 To'lov amalga oshirilmoqda...\n\nIltimos, to'lovingizni xavfsiz tarzda qayta ishlashimizni kuting.",
            'error': "Kechirasiz, xatolik yuz berdi. Iltimos, qayta urinib ko'ring yoki muammolar bo'lsa, yordamga murojaat qiling.",
            'order_tracking': "Buyurtmani kuzatish",
            'order_number': "Buyurtma raqami",
            'status': "Holat",
            'address': "Manzil",
            'slot': "Vaqt oralig'i",
            'events': "Voqealar",
            'no_events': "Voqealar yo'q.",
            'profile': "Profil",
            'name': "Ism",
            'phone': "Telefon raqami",
            'email': "Elektron pochta",
            'vip_status': "VIP Holati",
            'loyalty_points': "Sodiqlik ballari",
            'edit_profile': "Profilni tahrirlash",
            'manage_addresses': "Manzillarni boshqarish",
            'your_addresses': "Sizning manzillaringiz:",
            'address': "Manzil",
            'default': "Asosiy",
            'set_default': "Asosiy qilish",
            'delete': "O'chirish",
            'add_address': "Manzil qo'shish",
            'enter_address_line1': "Manzilni kiriting:",
            'enter_city': "Shaharni kiriting:",
            'address_added': "Manzil qo'shildi!",
            'address_deleted': "Manzil o'chirildi!",
            'default_set': "Asosiy manzil o'rnatildi!",
            'select_address': "Yetkazib berish manzilini tanlang:",
            'no_addresses': "Saqlangan manzilingiz yo'q. Davom etish uchun manzil qo'shing.",
            'address_selected': "Manzil tanlandi!",
            'edit': "Tahrirlash",
            'edit_address': "Manzilni tahrirlash",
            'enter_label': "Manzil yorlig'ini kiriting (masalan, Uy, Ish):",
            'enter_address_line2': "Manzilning 2-qatorini kiriting (yoki o'tkazib yuborish uchun '-' yozing):",
            'enter_state': "Viloyat/regionni kiriting (yoki o'tkazib yuborish uchun '-' yozing):",
            'enter_postal_code': "Pochta indeksini kiriting (yoki o'tkazib yuborish uchun '-' yozing):",
            'enter_country': "Mamlakatni kiriting (standart: UZ):",
            'enter_instructions': "Yetkazib berish uchun ko'rsatmalarni kiriting (yoki o'tkazib yuborish uchun '-' yozing):",
            'address_updated': "Manzil yangilandi!",
            'order_address': "Yetkazib berish manzili:",
            'change_address': "Manzilni o'zgartirish",
            'edit_profile_menu': "Nimani tahrirlashni xohlaysiz?",
            'edit_name': "Ismni tahrirlash",
            'edit_phone': "Telefonni tahrirlash",
            'edit_email': "Emailni tahrirlash",
            'edit_language': "Tilni tahrirlash",
            'enter_first_name': "Ismingizni kiriting:",
            'enter_last_name': "Familiyangizni kiriting:",
            'enter_phone': "Telefon raqamingizni kiriting:",
            'enter_email': "Email manzilingizni kiriting:",
            'phone_verification_code': "Telefoningizga yuborilgan kodni kiriting:",
            'email_verification_code': "Emailga yuborilgan kodni kiriting:",
            'profile_updated': "Profil yangilandi!",
            'phone_verified': "Telefon tasdiqlandi!",
            'email_verified': "Email tasdiqlandi!",
            'invalid_code': "Kod noto'g'ri. Qayta urinib ko'ring.",
            'send_location': "Manzilingizni avtomatik to'ldirish uchun lokatsiyani yuboring.",
            'address_invalid': "Manzilda kamida ko'cha va shahar bo'lishi kerak. Qayta urinib ko'ring.",
            'out_of_stock': "Kechirasiz, ba'zi mahsulotlar omborda yo'q. Savatingizni tahrirlang.",
            'card_payment_confirm': "Karta to'lovini tasdiqlang (simulyatsiya). Davom etish uchun 'paid' deb yozing:",
            'order_cancelled_by_user': "Buyurtma bekor qilindi.",
            'orders_list': "Buyurtmalaringiz:",
            'order_details': "Buyurtma tafsilotlari:",
            'cancel_order': "Buyurtmani bekor qilish",
            'order_already_delivered': "Buyurtma allaqachon yetkazilgan va bekor qilib bo'lmaydi.",
            'order_cancel_success': "Buyurtma muvaffaqiyatli bekor qilindi.",
            'my_deliveries': "Mening yetkazmalarim:",
            'update_status': "Holatni yangilash",
            'mark_delivered': "Yetkazildi deb belgilash",
            'mark_in_transit': "Yo'lda deb belgilash",
            'mark_failed': "Muvaffaqiyatsiz deb belgilash",
            'status_updated': "Yetkazma holati yangilandi!",
            'no_deliveries': "Sizga birorta yetkazma biriktirilmagan.",
            'subscription_menu': "Sizning obunangiz:",
            'pause_subscription': "Obunani to'xtatib turish",
            'resume_subscription': "Obunani davom ettirish",
            'edit_subscription': "Obunani tahrirlash",
            'subscription_paused': "Obuna to'xtatildi!",
            'subscription_resumed': "Obuna davom ettirildi!",
            'subscription_edited': "Obuna yangilandi!",
            'notify_renewal': "Obunangiz tez orada yangilanadi.",
            'notification_prefs': "Bildirishnoma sozlamalari:",
            'sms_notifications': "SMS bildirishnomalar",
            'email_notifications': "Email bildirishnomalar",
            'telegram_notifications': "Telegram bildirishnomalar",
            'marketing_communications': "Marketing xabarlari",
            'prefs_updated': "Sozlamalar yangilandi!",
            'my_deliveries': "Mening yetkazmalarim:",
            'update_status': "Holatni yangilash",
            'mark_delivered': "Yetkazildi deb belgilash",
            'mark_in_transit': "Yo'lda deb belgilash",
            'mark_failed': "Muvaffaqiyatsiz deb belgilash",
            'status_updated': "Yetkazma holati yangilandi!",
            'no_deliveries': "Sizga birorta yetkazma biriktirilmagan.",
            'subscription_menu': "Sizning obunangiz:",
            'pause_subscription': "Obunani to'xtatib turish",
            'resume_subscription': "Obunani davom ettirish",
            'edit_subscription': "Obunani tahrirlash",
            'subscription_paused': "Obuna to'xtatildi!",
            'subscription_resumed': "Obuna davom ettirildi!",
            'subscription_edited': "Obuna yangilandi!",
            'notify_renewal': "Obunangiz tez orada yangilanadi.",
            'notification_prefs': "Bildirishnoma sozlamalari:",
            'sms_notifications': "SMS bildirishnomalar",
            'email_notifications': "Email bildirishnomalar",
            'telegram_notifications': "Telegram bildirishnomalar",
            'marketing_communications': "Marketing xabarlari",
            'prefs_updated': "Sozlamalar yangilandi!",
            'not_enough_points': "Ballarni yechib olish uchun yetarli ball yo'q.",
            'points_redeemed': "{points} ball uchun {discount} UZS chegirma oldingiz!",
            'rate_limited': "Siz juda tez so'rov yuboryapsiz. Iltimos, keyinroq urinib ko'ring.",
            'not_delivery_person': "Siz yetkazib beruvchi sifatida ruxsat etilmagansiz.",
            'map_link': "Xaritada ko'rish",
        })
        self.translations['ru'].update({
            'no_products_subscription': "Нет доступных товаров для подписки.",
            'select_product_to_subscribe': "Выберите товар для подписки:",
            'your_subscriptions': "Ваши подписки (нажмите для отмены):",
            'no_active_subscriptions': "У вас нет активных подписок.",
            'subscription_created': "✅ Подписка создана! Вы будете получать регулярные доставки.",
            'subscription_failed': "❌ Не удалось создать подписку.",
            'subscription_cancelled': "Подписка отменена.",
            'subscription_cancel_failed': "Не удалось отменить подписку.",
            'no_recent_orders': "У вас нет недавних заказов.",
            'your_recent_orders': "Ваши недавние заказы:",
            'no_loyalty_transactions': "Пока нет транзакций по баллам лояльности.",
            'pending_orders': "Ожидающие заказы:",
            'no_pending_orders': "Нет ожидающих заказов.",
            'not_admin': "Вы не администратор.",
            'order_success': "✅ Заказ успешно оформлен! Мы уведомим вас о доставке.",
            'order_error': "❌ Не удалось оформить заказ. Пожалуйста, попробуйте позже.",
            'order_cancelled': "Заказ отменен.",
            'cart_empty': "Ваша корзина пуста.",
            'select_product': "Выберите товар для заказа:",
            'select_quantity': "Сколько '{product}' вы хотите заказать?",
            'cart': "Корзина:",
            'proceed_to_delivery': "Перейти к доставке",
            'add_more': "Добавить еще",
            'cancel': "Отмена",
            'select_delivery_slot': "Выберите время доставки (Стоимость: {fee} UZS):",
            'choose_payment_method': "Выберите способ оплаты:",
            'order_summary': "Сводка заказа:\n{cart}\nСтоимость доставки: {fee} UZS\nИтого: {total} UZS\n\nПодтвердить заказ?",
            'confirm': "Подтвердить",
            'back_main': "Назад в главное меню",
            'language_updated': "Язык обновлен!",
            'select_language': "🌐 Выберите язык:",
            'no_products': "В данный момент нет доступных товаров.",
            'select_product_to_order': "Выберите товар для заказа:",
            'location_received': "📍 Местоположение получено! Я буду использовать его для доставки.\n\nТеперь вы можете оформить заказ или сохранить этот адрес в своем профиле.",
            'photo_received': "📸 Фото получено! Спасибо за подтверждение доставки.\n\nВаша доставка отмечена как завершенная.",
            'phone_saved': "📞 Телефон сохранен! Это поможет нам с координацией доставки.\n\nТеперь вы можете оформлять заказы с доставкой на этот адрес.",
            'error_saving_contact': "Извините, произошла ошибка при сохранении контакта.",
            'processing_payment': "💳 Обработка платежа...\n\nПожалуйста, подождите, пока мы безопасно обработаем ваш платеж.",
            'error': "Извините, что-то пошло не так. Пожалуйста, попробуйте еще раз или обратитесь в поддержку, если проблема повторяется.",
            'order_tracking': "Отслеживание заказа",
            'order_number': "Номер заказа",
            'status': "Статус",
            'address': "Адрес",
            'slot': "Время",
            'events': "События",
            'no_events': "Нет событий.",
            'profile': "Профиль",
            'name': "Имя",
            'phone': "Телефон",
            'email': "Электронная почта",
            'vip_status': "VIP Статус",
            'loyalty_points': "Лояльность баллы",
            'edit_profile': "Редактировать профиль",
            'manage_addresses': "Управление адресами",
            'your_addresses': "Ваши адреса:",
            'address': "Адрес",
            'default': "Основной",
            'set_default': "Сделать основным",
            'delete': "Удалить",
            'add_address': "Добавить адрес",
            'enter_address_line1': "Пожалуйста, введите адрес:",
            'enter_city': "Пожалуйста, введите город:",
            'address_added': "Адрес добавлен!",
            'address_deleted': "Адрес удален!",
            'default_set': "Основной адрес установлен!",
            'select_address': "Выберите адрес доставки:",
            'no_addresses': "У вас нет сохраненных адресов. Пожалуйста, добавьте адрес для продолжения.",
            'address_selected': "Адрес выбран!",
            'edit': "Редактировать",
            'edit_address': "Редактировать адрес",
            'enter_label': "Введите метку адреса (например, Дом, Офис):",
            'enter_address_line2': "Введите вторую строку адреса (или '-' чтобы пропустить):",
            'enter_state': "Введите область/регион (или '-' чтобы пропустить):",
            'enter_postal_code': "Введите почтовый индекс (или '-' чтобы пропустить):",
            'enter_country': "Введите страну (по умолчанию: UZ):",
            'enter_instructions': "Введите инструкции для доставки (или '-' чтобы пропустить):",
            'address_updated': "Адрес обновлен!",
            'order_address': "Адрес доставки:",
            'change_address': "Изменить адрес",
            'edit_profile_menu': "Что вы хотите изменить?",
            'edit_name': "Изменить имя",
            'edit_phone': "Изменить телефон",
            'edit_email': "Изменить email",
            'edit_language': "Изменить язык",
            'enter_first_name': "Введите ваше имя:",
            'enter_last_name': "Введите вашу фамилию:",
            'enter_phone': "Введите ваш номер телефона:",
            'enter_email': "Введите ваш email:",
            'phone_verification_code': "Введите код, отправленный на ваш телефон:",
            'email_verification_code': "Введите код, отправленный на ваш email:",
            'profile_updated': "Профиль обновлен!",
            'phone_verified': "Телефон подтвержден!",
            'email_verified': "Email подтвержден!",
            'invalid_code': "Неверный код. Пожалуйста, попробуйте еще раз.",
            'send_location': "Отправьте свою геолокацию для автозаполнения адреса.",
            'address_invalid': "В адресе должны быть как минимум улица и город. Попробуйте еще раз.",
            'out_of_stock': "Извините, некоторые товары закончились на складе. Измените корзину.",
            'card_payment_confirm': "Пожалуйста, подтвердите оплату картой (симуляция). Введите 'paid' для продолжения:",
            'order_cancelled_by_user': "Заказ отменен.",
            'orders_list': "Ваши заказы:",
            'order_details': "Детали заказа:",
            'cancel_order': "Отменить заказ",
            'order_already_delivered': "Заказ уже доставлен и не может быть отменен.",
            'order_cancel_success': "Заказ успешно отменен.",
            'my_deliveries': "Мои доставки:",
            'update_status': "Обновить статус",
            'mark_delivered': "Отметить как доставлено",
            'mark_in_transit': "Отметить как в пути",
            'mark_failed': "Отметить как неудачно",
            'status_updated': "Статус доставки обновлен!",
            'no_deliveries': "Нет назначенных доставок.",
            'subscription_menu': "Ваша подписка:",
            'pause_subscription': "Приостановить подписку",
            'resume_subscription': "Возобновить подписку",
            'edit_subscription': "Редактировать подписку",
            'subscription_paused': "Подписка приостановлена!",
            'subscription_resumed': "Подписка возобновлена!",
            'subscription_edited': "Подписка обновлена!",
            'notify_renewal': "Ваша подписка скоро будет обновлена.",
            'notification_prefs': "Настройки уведомлений:",
            'sms_notifications': "SMS уведомления",
            'email_notifications': "Email уведомления",
            'telegram_notifications': "Telegram уведомления",
            'marketing_communications': "Маркетинговые сообщения",
            'prefs_updated': "Настройки обновлены!",
            'my_deliveries': "Мои доставки:",
            'update_status': "Обновить статус",
            'mark_delivered': "Отметить как доставлено",
            'mark_in_transit': "Отметить как в пути",
            'mark_failed': "Отметить как неудачно",
            'status_updated': "Статус доставки обновлен!",
            'no_deliveries': "Нет назначенных доставок.",
            'subscription_menu': "Ваша подписка:",
            'pause_subscription': "Приостановить подписку",
            'resume_subscription': "Возобновить подписку",
            'edit_subscription': "Редактировать подписку",
            'subscription_paused': "Подписка приостановлена!",
            'subscription_resumed': "Подписка возобновлена!",
            'subscription_edited': "Подписка обновлена!",
            'notify_renewal': "Ваша подписка скоро будет обновлена.",
            'notification_prefs': "Настройки уведомлений:",
            'sms_notifications': "SMS уведомления",
            'email_notifications': "Email уведомления",
            'telegram_notifications': "Telegram уведомления",
            'marketing_communications': "Маркетинговые сообщения",
            'prefs_updated': "Настройки обновлены!",
            'not_enough_points': "У вас недостаточно баллов для списания.",
            'points_redeemed': "Вы обменяли {points} баллов на скидку {discount} UZS!",
            'rate_limited': "Вы отправляете запросы слишком быстро. Пожалуйста, попробуйте позже.",
            'not_delivery_person': "У вас нет прав курьера.",
            'map_link': "Посмотреть на карте",
        })


    async def init_connections(self):
        """Initialize database and redis connections"""
        try:
            # Database connection
            self.db_pool = await asyncpg.create_pool(self.db_url)
            logger.info("Database connection established")
            
            # Redis connection
            self.redis_client = redis.from_url(self.redis_url)
            await self.redis_client.ping()
            logger.info("Redis connection established")
            
            # HTTP client
            self.http_client = httpx.AsyncClient(base_url=self.business_app_url)
            logger.info("HTTP client initialized")

            # --- Instantiate services ---
            # self.notification_service = NotificationService(self.redis_client, self.db_pool)
            self.payment_service = PaymentService(self.redis_client, self.db_pool)
            self.delivery_service = DeliveryService(self.redis_client, self.db_pool)
            self.analytics_service = AnalyticsService(self.db_pool)
            self.security_service = SecurityService(self.db_pool)
            self.admin_service = AdminService(self.db_pool)
            self.order_service = OrderService(self.db_pool, self.redis_client)
            self.product_service = ProductService(self.db_pool)
            self.subscription_service = SubscriptionService(self.db_pool)
            self.user_service = UserService(self.db_pool)
            self.address_service = AddressService(self.db_pool)
            # --- End instantiate services ---
        except Exception as e:
            logger.error(f"Failed to initialize connections: {e}")
            raise

    # --- Helper for Google Maps static link ---
    def get_static_map_link(self, address, lat=None, lon=None):
        if lat and lon:
            return f"https://maps.google.com/?q={lat},{lon}"
        elif address:
            return f"https://maps.google.com/?q={address.replace(' ', '+')}"
        return None

    # --- Role check helpers ---
    async def is_delivery_person(self, user):
        return user.get('role') == 'delivery'
    async def is_admin(self, user):
        return user.get('role') == 'admin'

    # --- Rate limiting wrapper ---
    async def check_rate_limit(self, user_id, action, limit=10):
        allowed = await self.security_service.check_rate_limit(user_id, action, limit)
        if not allowed:
            return False
        return True

    # --- Loyalty points redemption flow ---
    async def redeem_loyalty_points(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        points = user.get('loyalty_points', 0)
        if points < 100:
            await update.message.reply_text(self.get_text('not_enough_points', lang))
            return
        discount = int(points / 100) * 1000  # 100 points = 1000 UZS discount
        await self.payment_service.process_loyalty_payment(user['id'], points)
        await update.message.reply_text(self.get_text('points_redeemed', lang).format(points=points, discount=discount))

    # --- Order flow: add rate limit, notification, map link, loyalty redemption ---
    async def order_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str = "en"):
        user = await self.user_service.get_or_create_user(update.effective_user)
        user_id = user['id']
        if not await self.check_rate_limit(user_id, 'order', 5):
            await update.message.reply_text(self.get_text('rate_limited', lang))
            return
        self.user_states[user_id] = {
            'state': ORDER_STATE['SELECT_PRODUCT'],
            'cart': [],
        }
        products = await self.product_service.get_available_products()
        if not products:
            if update.message:
                await update.message.reply_text(self.get_text('no_products', lang))
            elif update.callback_query:
                await update.callback_query.edit_message_text(self.get_text('no_products', lang))
            return
        keyboard = [
            [InlineKeyboardButton(f"{p['name']} ({p['price']} UZS)", callback_data=f"order_product_{p['id']}")]
            for p in products
        ]
        if update.message:
            await update.message.reply_text(
                self.get_text('select_product', lang),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        elif update.callback_query:
            await update.callback_query.edit_message_text(
                self.get_text('select_product', lang),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    # --- Deliver command: restrict to delivery role ---
    async def deliver_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        if not await self.is_delivery_person(user):
            await update.message.reply_text(self.get_text('not_delivery_person', lang))
            return
        deliveries = await self.delivery_service.get_deliveries_for_person(user['id'], status='in_transit')
        if not deliveries:
            await update.message.reply_text(self.get_text('no_deliveries', lang))
            return
        keyboard = [
            [InlineKeyboardButton(f"{self.get_text('order_number', lang)} {d['order_id']} ({d['status']})", callback_data=f"deliver_update_{d['order_id']}")]
            for d in deliveries
        ]
        await update.message.reply_text(self.get_text('my_deliveries', lang), reply_markup=InlineKeyboardMarkup(keyboard))

    # --- Admin commands: restrict to admin ---
    async def admin_orders_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        if not await self.is_admin(user):
            await update.message.reply_text(self.get_text('not_admin', lang))
            return
        orders = await self.admin_service.get_pending_orders()
        if not orders:
            await update.message.reply_text(self.get_text('no_pending_orders', lang))
            return
        text = "\n".join([
            f"Order {o['order_number']} by {o['username']} ({o['phone']}) - {o['status']}" for o in orders
        ])
        await update.message.reply_text(self.get_text('pending_orders', lang) + "\n" + text)

    # --- Payment confirmation stub ---
    async def payment_confirmation_webhook(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Simulate webhook for payment confirmation
        # In production, this would be a webhook endpoint
        await update.message.reply_text("[Simulated] Payment confirmed!")

    # --- Wire up notifications in order, delivery, subscription flows ---
    # In order_callback_handler, after order is created:
    # await self.notification_service.send_order_notification(user['id'], order, 'order_confirmed')
    # In deliver_callback_handler, after status update:
    # await self.notification_service.send_event_notification(user['id'], 'delivery_status', {'order_id': order_id, 'status': status})
    # In process_subscription_renewals, after renewal:
    # await self.notification_service.send_event_notification(sub['user_id'], 'subscription_renewal', {'sub_id': sub['id']})

    # --- Map link in order summary and tracking ---
    # In order_details_callback_handler and track_callback_handler, add map link to address
    # In order summary, add map link if address has lat/lon

    

    def get_text(self, key: str, lang: str = 'en') -> str:
        """Get translated text"""
        return self.translations.get(lang, {}).get(key, self.translations['en'].get(key, key))

    # --- Main Menu ---
    async def get_main_keyboard(self, user_id: int, lang: str = 'en') -> InlineKeyboardMarkup:
        user = await self.user_service.get_or_create_user_by_telegram_id(user_id)
        is_admin = user.get('role') == 'admin' if user else False
        is_delivery = user.get('role') == 'delivery' if user else False
        keyboard = [
            [InlineKeyboardButton(f"🛒 {self.get_text('order_menu', lang)}", callback_data='order')],
            [InlineKeyboardButton(f"📦 {self.get_text('track_menu', lang)}", callback_data='track')],
            [InlineKeyboardButton("🔄 My Subscriptions", callback_data='mysubscriptions'), InlineKeyboardButton("➕ Subscribe", callback_data='subscribe')],
            [InlineKeyboardButton("🌟 Loyalty & Analytics", callback_data='loyalty')],
            [InlineKeyboardButton(f"👤 {self.get_text('account_menu', lang)}", callback_data='account')],
            [InlineKeyboardButton("🔔 Notifications", callback_data='notifications')],
            [InlineKeyboardButton("📊 Analytics", callback_data='analytics')],
            [InlineKeyboardButton("ℹ️ Info", callback_data='info'), InlineKeyboardButton("🌐 Language", callback_data='language')],
            [InlineKeyboardButton("🎯 VIP Services", callback_data='vip')]
        ]
        if is_admin:
            keyboard.append([InlineKeyboardButton("🛠️ Admin Panel", callback_data='admin_panel')])
        if is_delivery:
            keyboard.append([InlineKeyboardButton("🚚 My Deliveries", callback_data='my_deliveries')])
        return InlineKeyboardMarkup(keyboard)

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_data = await self.user_service.get_or_create_user(update.effective_user)
        lang = user_data.get('language_code', 'en')
        user_id = user_data.get('telegram_id', update.effective_user.id)
        welcome_text = self.get_text('welcome', lang)
        keyboard = await self.get_main_keyboard(user_id, lang)
        # Use reply_text only if update.message exists
        if update.message:
            await update.message.reply_text(
                welcome_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
        elif update.callback_query:
            await update.callback_query.edit_message_text(
                welcome_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        help_text = """
🌊 *AquaPure Water Solutions Bot Help*

*Available Commands:*
• /start - Start the bot and see main menu
• /help - Show this help message
• /order - Quick order water
• /track - Track your orders
• /account - Manage your account
• /subscribe - Manage subscriptions
• /contact - Contact support

*Features:*
• 🛒 Order premium filtered water
• 📦 Real-time order tracking
• 🔔 Smart notifications (SMS/Email)
• 📍 Location-based delivery
• 💳 Multiple payment options
• 🎯 Loyalty points system
• 📊 VIP customer benefits
• 🌐 Multi-language support
• 📱 Photo delivery confirmation

*Need Help?*
Contact our support team at +998901234567 or email info@aquapure.uz
        """
        await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user_id = query.from_user.id
        lang = 'en'
        user = await self.user_service.get_or_create_user(update.effective_user)
        if user:
            lang = user.get('language_code', 'en')
        data = query.data
        logger.info(f"button_handler: {data=}")
        if data == 'order':
            await self.order_command(update, context, lang=lang)
        elif data == 'track':
            await self.track_command(update, context)
        elif data == 'mysubscriptions':
            await self.mysubscriptions_command(update, context)
        elif data == 'subscribe':
            await self.subscribe_command(update, context)
        elif data == 'loyalty':
            await self.show_loyalty_menu(query, lang)
        elif data == 'redeem_points':
            await self.redeem_loyalty_points(update, context)
        elif data == 'loyalty_history':
            await self.loyalty_history_command(update, context)
        elif data == 'account':
            await self.show_account_menu(query, lang)
        elif data == 'notifications':
            await self.show_notifications_menu(query, lang)
        elif data == 'analytics':
            await self.show_analytics_menu(query, lang)
        elif data == 'info':
            await self.show_company_info(query, lang)
        elif data == 'language':
            await self.show_language_menu(query, lang)
        elif data == 'vip':
            await self.show_vip_menu(query, lang)
        elif data == 'admin_panel':
            await self.show_admin_panel(query, lang)
        elif data == 'admin_orders':
            await self.admin_orders_command(update, context)
        elif data == 'admin_stats':
            await self.admin_stats_command(update, context)
        elif data == 'optimize_route':
            await self.optimize_route_command(update, context)
        elif data == 'my_deliveries':
            await self.deliver_command(update, context)
        elif data == 'back_main':
            await self.show_main_menu(query, lang)
        else:
            await query.answer()
            await query.edit_message_text("Unknown action.")

    async def show_company_info(self, query, lang: str):
        """Show company information"""
        try:
            async with self.db_pool.acquire() as conn:
                info = await conn.fetchrow("SELECT * FROM company_info LIMIT 1")
                
            if info:
                text = f"""
🌊 *{info['company_name']}*

📝 *About Us:*
{info['description']}

📞 *Contact:*
• Phone: {info['phone']}
• Email: {info['email']}
• Website: {info['website']}

🏢 *Address:*
{info['address']}

🕒 *Business Hours:*
{info['business_hours']}

🚚 *Delivery Areas:*
{', '.join(info['delivery_areas'])}

💧 *Our Water Quality:*
• Advanced multi-stage filtration
• Regular quality testing
• Mineral balance optimization
• Safe and healthy drinking water

🎯 *Why Choose Us:*
• Premium quality water
• Reliable delivery service
• Competitive pricing
• Excellent customer support
• VIP customer programs
"""
                keyboard = [[InlineKeyboardButton("🔙 Back to Main Menu", callback_data='back_main')]]
                await query.edit_message_text(
                    text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN
                )
        except Exception as e:
            logger.error(f"Error showing company info: {e}")
            await query.edit_message_text("Sorry, there was an error loading company information.")

    async def show_order_menu(self, query, lang: str):
        """Show order menu"""
        try:
            async with self.db_pool.acquire() as conn:
                products = await conn.fetch(
                    "SELECT * FROM products WHERE is_active = TRUE ORDER BY price"
                )
            
            text = "🛒 *Order Water*\n\nChoose your water type:\n\n"
            
            keyboard = []
            for product in products:
                product_text = f"{product['name']} - {product['volume_liters']}L - {product['price']:,.0f} UZS"
                text += f"• {product_text}\n"
                keyboard.append([InlineKeyboardButton(
                    f"🛒 {product['name']} ({product['volume_liters']}L)",
                    callback_data=f"order_{product['id']}"
                )])
            
            keyboard.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data='back_main')])
            
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Error showing order menu: {e}")
            await query.edit_message_text("Sorry, there was an error loading the order menu.")

    async def show_track_menu(self, query, lang: str):
        user_id = query.from_user.id
        try:
            orders = await self.admin_service.get_recent_orders_for_user(user_id)
            if not orders:
                text = "📦 *Order Tracking*\n\nYou don't have any orders yet.\n\nStart by placing your first order!"
                keyboard = [[InlineKeyboardButton("🛒 Order Now", callback_data='order')]]
            else:
                text = "📦 *Your Recent Orders*\n\n"
                keyboard = []
                for order in orders:
                    status_emoji = {
                        'pending': '⏳',
                        'confirmed': '✅',
                        'preparing': '🔄',
                        'out_for_delivery': '🚚',
                        'delivered': '📦',
                        'cancelled': '❌'
                    }
                    emoji = status_emoji.get(order['status'], '❓')
                    text += f"{emoji} Order #{order['order_number']}\n"
                    text += f"   Status: {order['status'].replace('_', ' ').title()}\n"
                    text += f"   Amount: {order['total_amount']:,.0f} UZS\n"
                    text += f"   Date: {order['created_at'].strftime('%d.%m.%Y')}\n\n"
                    keyboard.append([InlineKeyboardButton(
                        f"📱 Track #{order['order_number']}",
                        callback_data=f"track_{order['id']}"
                    )])
            keyboard.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data='back_main')])
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Error showing track menu: {e}")
            await query.edit_message_text("Sorry, there was an error loading your orders.")

    async def show_account_menu(self, query, lang: str):
        """Show account management menu"""
        user_id = query.from_user.id
        try:
            async with self.db_pool.acquire() as conn:
                user = await conn.fetchrow(
                    "SELECT * FROM users WHERE telegram_id = $1",
                    user_id
                )
                
                # Get user stats
                stats = await conn.fetchrow(
                    """SELECT 
                           COUNT(*) as total_orders,
                           COALESCE(SUM(total_amount), 0) as total_spent,
                           COALESCE(AVG(total_amount), 0) as avg_order
                       FROM orders o
                       JOIN users u ON o.user_id = u.id
                       WHERE u.telegram_id = $1 AND o.status = 'delivered'""",
                    user_id
                )
            
            vip_status = "🎯 VIP Customer" if user['is_vip'] else "👤 Regular Customer"
            
            text = f"""
👤 *My Account*

*Profile Information:*
• Name: {user['first_name']} {user['last_name'] or ''}
• Phone: {user['phone'] or 'Not provided'}
• Email: {user['email'] or 'Not provided'}
• Status: {vip_status}

*Account Stats:*
• Loyalty Points: {user['loyalty_points']:,} pts
• Total Orders: {stats['total_orders']}
• Total Spent: {stats['total_spent']:,.0f} UZS
• Average Order: {stats['avg_order']:,.0f} UZS

*Member Since:* {user['created_at'].strftime('%B %Y')}
"""
            keyboard = [
                [InlineKeyboardButton("📝 Edit Profile", callback_data='edit_profile')],
                [InlineKeyboardButton("📍 Manage Addresses", callback_data='manage_addresses')],
                [InlineKeyboardButton("🔔 Notification Settings", callback_data='notification_settings')],
                [InlineKeyboardButton("💳 Payment Methods", callback_data='payment_methods')],
                [InlineKeyboardButton("🎯 Loyalty Program", callback_data='loyalty_program')],
                [InlineKeyboardButton("📊 Subscription Management", callback_data='subscriptions')],
                [InlineKeyboardButton("🔙 Back to Main Menu", callback_data='back_main')]
            ]
            
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Error showing account menu: {e}")
            await query.edit_message_text("Sorry, there was an error loading your account information.")

    async def show_notifications_menu(self, query, lang: str):
        """Show notifications menu"""
        text = """
🔔 *Smart Notifications*

*Notification Types:*
• 📱 Order confirmations
• 🚚 Delivery updates
• 💳 Payment confirmations
• 🎯 Loyalty rewards
• 📊 Special offers
• ⏰ Subscription reminders

*Delivery Channels:*
• 📱 Telegram messages
• 📧 Email notifications
• 📲 SMS alerts
• 🔔 Push notifications

*Settings:*
• Customize notification preferences
• Set delivery time preferences
• Choose notification language
• Emergency contact options
"""
        keyboard = [
            [InlineKeyboardButton("⚙️ Notification Settings", callback_data='notification_settings')],
            [InlineKeyboardButton("📱 SMS Settings", callback_data='sms_settings')],
            [InlineKeyboardButton("📧 Email Settings", callback_data='email_settings')],
            [InlineKeyboardButton("🔙 Back to Main Menu", callback_data='back_main')]
        ]
        
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )

    async def show_analytics_menu(self, query, lang: str):
        """Show analytics menu"""
        user_id = query.from_user.id
        try:
            async with self.db_pool.acquire() as conn:
                # Get user analytics
                analytics = await conn.fetchrow(
                    """SELECT 
                           COUNT(*) as total_orders,
                           COALESCE(SUM(total_amount), 0) as total_spent,
                           COALESCE(AVG(total_amount), 0) as avg_order,
                           COUNT(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '30 days' THEN 1 END) as orders_last_30_days,
                           COUNT(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '7 days' THEN 1 END) as orders_last_7_days
                       FROM orders o
                       JOIN users u ON o.user_id = u.id
                       WHERE u.telegram_id = $1""",
                    user_id
                )
                
                # Get favorite products
                favorite_products = await conn.fetch(
                    """SELECT p.name, COUNT(*) as order_count
                       FROM order_items oi
                       JOIN products p ON oi.product_id = p.id
                       JOIN orders o ON oi.order_id = o.id
                       JOIN users u ON o.user_id = u.id
                       WHERE u.telegram_id = $1
                       GROUP BY p.id, p.name
                       ORDER BY order_count DESC
                       LIMIT 3""",
                    user_id
                )
            
            text = f"""
📊 *Your Analytics & Insights*

*Order Statistics:*
• Total Orders: {analytics['total_orders']}
• Total Spent: {analytics['total_spent']:,.0f} UZS
• Average Order: {analytics['avg_order']:,.0f} UZS
• Orders (Last 30 days): {analytics['orders_last_30_days']}
• Orders (Last 7 days): {analytics['orders_last_7_days']}

*Your Favorite Products:*
"""
            
            for i, product in enumerate(favorite_products, 1):
                text += f"{i}. {product['name']} ({product['order_count']} times)\n"
            
            if not favorite_products:
                text += "No orders yet - start ordering to see your preferences!\n"
            
            text += f"""
*Recommendations:*
• 🎯 Consider subscribing to save money
• 💎 VIP membership for exclusive benefits
• 🏆 Refer friends to earn loyalty points
"""
            
            keyboard = [
                [InlineKeyboardButton("📈 Detailed Report", callback_data='detailed_analytics')],
                [InlineKeyboardButton("🎯 Recommendations", callback_data='recommendations')],
                [InlineKeyboardButton("🔙 Back to Main Menu", callback_data='back_main')]
            ]
            
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Error showing analytics menu: {e}")
            await query.edit_message_text("Sorry, there was an error loading analytics.")

    async def show_language_menu(self, query, lang: str):
        """Show language selection menu"""
        text = "🌐 *Choose Your Language*\n\nSelect your preferred language:"
        
        keyboard = [
            [InlineKeyboardButton("🇺🇸 English", callback_data='lang_en')],
            [InlineKeyboardButton("🇺🇿 O'zbekcha", callback_data='lang_uz')],
            [InlineKeyboardButton("🇷🇺 Русский", callback_data='lang_ru')],
            [InlineKeyboardButton("🔙 Back to Main Menu", callback_data='back_main')]
        ]
        
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )

    async def show_vip_menu(self, query, lang: str):
        """Show VIP services menu"""
        user_id = query.from_user.id
        try:
            async with self.db_pool.acquire() as conn:
                user = await conn.fetchrow(
                    "SELECT * FROM users WHERE telegram_id = $1",
                    user_id
                )
            
            if user['is_vip']:
                text = """
🎯 *VIP Customer Services*

*Your VIP Benefits:*
• ⚡ Priority delivery (within 2 hours)
• 🎁 20% discount on all orders
• 💎 Exclusive premium products
• 🏆 Double loyalty points
• 📞 Dedicated customer support
• 🚚 Free delivery on all orders
• 🎊 Birthday special offers
• 💳 Flexible payment terms

*VIP Statistics:*
• VIP Member Since: {user['created_at'].strftime('%B %Y')}
• VIP Points: {user['loyalty_points']:,}
• VIP Savings: Calculate your total savings
"""
                keyboard = [
                    [InlineKeyboardButton("⚡ Priority Order", callback_data='vip_priority_order')],
                    [InlineKeyboardButton("💎 Exclusive Products", callback_data='vip_exclusive')],
                    [InlineKeyboardButton("📞 VIP Support", callback_data='vip_support')],
                    [InlineKeyboardButton("🔙 Back to Main Menu", callback_data='back_main')]
                ]
            else:
                text = """
🎯 *Become a VIP Customer*

*VIP Benefits Include:*
• ⚡ Priority delivery (within 2 hours)
• 🎁 20% discount on all orders
• 💎 Access to exclusive premium products
• 🏆 Double loyalty points on every purchase
• 📞 Dedicated customer support line
• 🚚 Free delivery on all orders
• 🎊 Special birthday offers
• 💳 Flexible payment terms

*VIP Membership Requirements:*
• Monthly orders: 10+ bottles
• Total spent: 500,000+ UZS
• Loyalty points: 1,000+ points

*Current Progress:*
• Orders this month: {user['total_orders']}
• Total spent: {user['total_spent']:,.0f} UZS
• Loyalty points: {user['loyalty_points']:,}
"""
                keyboard = [
                    [InlineKeyboardButton("💎 Apply for VIP", callback_data='apply_vip')],
                    [InlineKeyboardButton("🏆 Earn More Points", callback_data='earn_points')],
                    [InlineKeyboardButton("🔙 Back to Main Menu", callback_data='back_main')]
                ]
            
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Error showing VIP menu: {e}")
            await query.edit_message_text("Sorry, there was an error loading VIP information.")

    async def show_main_menu(self, update_or_query, lang: str = 'en'):
        # Robustly handle both Update and CallbackQuery
        if isinstance(update_or_query, Update):
            if update_or_query.message:
                user_id = update_or_query.effective_user.id
                send = update_or_query.message.reply_text
            elif update_or_query.callback_query:
                user_id = update_or_query.callback_query.from_user.id
                send = update_or_query.callback_query.edit_message_text
            else:
                return
        else:
            user_id = update_or_query.from_user.id
            send = update_or_query.edit_message_text
        keyboard = await self.get_main_keyboard(user_id, lang)
        await send(
            self.get_text('main_menu', lang),
            reply_markup=keyboard
        )

    def get_cart_text(self, cart, lang):
        if not cart:
            return self.get_text('cart_empty', lang)
        lines = [f"{item['name']} x{item['quantity']} = {item['price']*item['quantity']} UZS" for item in cart]
        total = sum(item['price']*item['quantity'] for item in cart)
        return "\n".join(lines) + f"\n\nTotal: {total} UZS"

    async def order_callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user_id = query.from_user.id
        state = self.user_states.get(user_id, {})
        lang = "en"
        user = await self.user_service.get_or_create_user(update.effective_user)
        if user:
            lang = user.get('language_code', 'en')
        await query.answer()
        if not state:
            await query.edit_message_text(self.get_text('session_expired', lang))
            return
        if state['state'] == ORDER_STATE['SELECT_PRODUCT']:
            if query.data.startswith("order_product_"):
                product_id = query.data.replace("order_product_", "")
                product = await self.product_service.get_product_by_id(product_id)
                if not product:
                    await query.edit_message_text(self.get_text('no_products', lang))
                    return
                state['selected_product'] = product
                state['state'] = ORDER_STATE['SELECT_QUANTITY']
                self.user_states[user_id] = state
                keyboard = [
                    [InlineKeyboardButton(str(q), callback_data=f"order_qty_{q}") for q in range(1, 6)]
                ]
                await query.edit_message_text(
                    self.get_text('select_quantity', lang).format(product=product['name']),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        elif state['state'] == ORDER_STATE['SELECT_QUANTITY']:
            if query.data.startswith("order_qty_"):
                qty = int(query.data.replace("order_qty_", ""))
                product = state['selected_product']
                cart = state.get('cart', [])
                cart.append({
                    'id': product['id'],
                    'name': product['name'],
                    'price': product['price'],
                    'quantity': qty
                })
                state['cart'] = cart
                state['state'] = ORDER_STATE['CART']
                self.user_states[user_id] = state
                keyboard = [
                    [InlineKeyboardButton(self.get_text('add_more', lang), callback_data="order_add_more")],
                    [InlineKeyboardButton(self.get_text('proceed_to_delivery', lang), callback_data="order_delivery")],
                    [InlineKeyboardButton(self.get_text('cancel', lang), callback_data="order_cancel")],
                ]
                await query.edit_message_text(
                    self.get_text('cart', lang) + "\n" + self.get_cart_text(cart, lang=lang),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        elif state['state'] == ORDER_STATE['CART']:
            if query.data == "order_add_more":
                state['state'] = ORDER_STATE['SELECT_PRODUCT']
                self.user_states[user_id] = state
                products = await self.product_service.get_available_products()
                keyboard = [
                    [InlineKeyboardButton(f"{p['name']} ({p['price']} UZS)", callback_data=f"order_product_{p['id']}")]
                    for p in products
                ]
                await query.edit_message_text(
                    self.get_text('select_product', lang),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            elif query.data == "order_delivery":
                # Address selection step
                addresses = await self.address_service.get_user_addresses(user['id'])
                if not addresses:
                    await query.edit_message_text(self.get_text('no_addresses', lang))
                    return
                state['state'] = 'SELECT_ADDRESS'
                self.user_states[user_id] = state
                keyboard = [
                    [InlineKeyboardButton(f"{a['address_line1']}, {a['city']}", callback_data=f"select_addr_{a['id']}")]
                    for a in addresses
                ]
                keyboard.append([InlineKeyboardButton(self.get_text('add_address', lang), callback_data='add_address')])
                await query.edit_message_text(
                    self.get_text('select_address', lang),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            elif query.data == "order_cancel":
                del self.user_states[user_id]
                await query.edit_message_text(self.get_text('order_cancelled', lang))
        elif state.get('state') == 'SELECT_ADDRESS':
            if query.data.startswith('select_addr_'):
                addr_id = query.data.replace('select_addr_', '')
                state['selected_address_id'] = addr_id
                state['state'] = ORDER_STATE['DELIVERY_SLOT']
                self.user_states[user_id] = state
                await query.edit_message_text(self.get_text('address_selected', lang))
                # Now proceed to delivery slot selection (reuse existing logic)
                # For demo, use a static warehouse location
                user = await self.user_service.get_or_create_user(update.effective_user)
                user_location = {'latitude': 41.2995, 'longitude': 69.2401} # fallback
                warehouse_location = {'latitude': 41.2995, 'longitude': 69.2401}
                fee = await self.delivery_service.calculate_delivery_fee(user_location, warehouse_location)
                slots = await self.delivery_service.get_available_slots(user_location)
                if not slots:
                    await query.edit_message_text(self.get_text('no_delivery_slots', lang))
                    return
                state['delivery_fee'] = float(fee)
                state['slots'] = slots
                keyboard = [
                    [InlineKeyboardButton(slot.slot_id, callback_data=f"order_slot_{slot.slot_id}")]
                    for slot in slots[:5]
                ]
                await query.edit_message_text(
                    self.get_text('select_delivery_slot', lang).format(fee=fee),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        elif state['state'] == ORDER_STATE['DELIVERY_SLOT']:
            if query.data.startswith("order_slot_"):
                slot_id = query.data.replace("order_slot_", "")
                # slot_id format: DD.MM.YYYY HH:MM-HH:MM
                try:
                    delivery_date_str, hour_str = slot_id.split(' ')
                    delivery_date = datetime.strptime(delivery_date_str, "%d.%m.%Y").date()
                    time_slot = f"{hour_str}:00-{int(hour_str)+2}:00"
                except Exception:
                    # fallback if parsing fails
                    delivery_date = datetime.now().date()
                    time_slot = "09:00-11:00"
                state['selected_slot'] = slot_id
                state['delivery_date'] = delivery_date
                state['time_slot'] = time_slot
                state['state'] = ORDER_STATE['PAYMENT_METHOD']
                self.user_states[user_id] = state
                keyboard = [
                    [InlineKeyboardButton(self.get_text('cash', lang), callback_data="order_pay_cash")],
                    [InlineKeyboardButton(self.get_text('card', lang), callback_data="order_pay_card")],
                    [InlineKeyboardButton(self.get_text('loyalty_points', lang), callback_data="order_pay_loyalty")],
                ]
                await query.edit_message_text(
                    self.get_text('choose_payment_method', lang),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        elif state['state'] == ORDER_STATE['PAYMENT_METHOD']:
            payment_method = None
            if query.data == "order_pay_cash":
                payment_method = 'cash'
            elif query.data == "order_pay_card":
                payment_method = 'card'
            elif query.data == "order_pay_loyalty":
                payment_method = 'loyalty'
            if payment_method:
                state['payment_method'] = payment_method
                state['state'] = ORDER_STATE['CONFIRM']
                self.user_states[user_id] = state
                cart = state['cart']
                total = float(sum(item['price']*item['quantity'] for item in cart)) + state.get('delivery_fee', 0)
                await query.edit_message_text(
                    self.get_text('order_summary', lang).format(cart=self.get_cart_text(cart, lang=lang), fee=state.get('delivery_fee', 0), total=total),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(self.get_text('confirm', lang), callback_data="order_confirm")],
                        [InlineKeyboardButton(self.get_text('cancel', lang), callback_data="order_cancel")],
                    ])
                )
        elif state['state'] == ORDER_STATE['CONFIRM']:
            if query.data == "order_confirm":
                # Place order
                user = await self.user_service.get_or_create_user(update.effective_user)
                cart = state['cart']
                address_id = state.get('selected_address_id')
                payment_method = state['payment_method']
                total = float(sum(item['price']*item['quantity'] for item in cart)) + state.get('delivery_fee', 0)
                # Stock check
                for item in cart:
                    product = await self.product_service.get_product_by_id(item['id'])
                    if product['stock_quantity'] < item['quantity']:
                        await query.edit_message_text(self.get_text('out_of_stock', lang))
                        return
                # Card payment confirmation
                if payment_method == 'card' and not state.get('card_paid'):
                    state['card_paid'] = False
                    self.user_states[user_id] = state
                    await query.edit_message_text(self.get_text('card_payment_confirm', lang))
                    return
                try:
                    order = await self.order_service.create_order(user['id'], cart, address_id, payment_method)
                    # Schedule delivery with correct params
                    await self.delivery_service.schedule_delivery(
                        order['id'],
                        state['delivery_date'],
                        state['time_slot']
                    )
                    # Payment
                    if payment_method == 'card':
                        payment = await self.payment_service.create_payment_intent(total, currency='uzs', metadata={'order_id': order['id']})
                        # In production, send payment link or handle payment confirmation
                    elif payment_method == 'loyalty':
                        await self.payment_service.process_loyalty_payment(user['id'], total)
                    # Notification
                    # await self.notification_service.send_order_notification(user['id'], order, 'order_confirmed')
                    # Loyalty points
                    await self.payment_service.add_loyalty_points(user['id'], int(total*0.05))
                    await query.edit_message_text(self.get_text('order_success', lang))
                except Exception as e:
                    logger.error(f"Order error: {e}")
                    await query.edit_message_text(self.get_text('order_error', lang))
                del self.user_states[user_id]
            elif query.data == "order_cancel":
                del self.user_states[user_id]
                await query.edit_message_text(self.get_text('order_cancelled', lang))
        # Card payment confirmation step
        elif state['state'] == 'CARD_PAYMENT_CONFIRM':
            if update.message.text.strip().lower() == 'paid':
                state['card_paid'] = True
                state['state'] = ORDER_STATE['CONFIRM']
                self.user_states[user_id] = state
                await self.order_callback_handler(update, context)
            else:
                await update.message.reply_text(self.get_text('card_payment_confirm', lang))

    async def location_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle location messages"""
        location = update.message.location
        user_id = update.effective_user.id

        lang = "en"
        user = await self.user_service.get_or_create_user(update.effective_user)
        if user:
            lang = user.get('language_code', 'en')
        
        # Store location in redis for order processing
        await self.redis_client.setex(
            f"user_location:{user_id}",
            3600,  # 1 hour expiry
            json.dumps({
                'latitude': location.latitude,
                'longitude': location.longitude,
                'timestamp': datetime.now().isoformat()
            })
        )
        
        await update.message.reply_text(
            self.get_text('location_received', lang),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(self.get_text('order_now', lang), callback_data='order')],
                [InlineKeyboardButton(self.get_text('save_address', lang), callback_data='save_address')]
            ])
        )

    async def photo_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle photo uploads (for delivery confirmation)"""
        photo = update.message.photo[-1]  # Get highest resolution
        file = await context.bot.get_file(photo.file_id)
        
        # Save photo for delivery confirmation
        file_path = f"uploads/delivery_photos/{photo.file_id}.jpg"
        await file.download_to_drive(file_path)
        
        lang = "en"
        user = await self.user_service.get_or_create_user(update.effective_user)
        if user:
            lang = user.get('language_code', 'en')

        await update.message.reply_text(
            self.get_text('photo_received', lang)
        )

    async def contact_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle contact sharing"""
        contact = update.message.contact
        user_id = update.effective_user.id

        lang = "en"
        user = await self.user_service.get_or_create_user(update.effective_user)
        if user:
            lang = user.get('language_code', 'en')
        
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET phone = $1 WHERE telegram_id = $2",
                    contact.phone_number, user_id
                )
            
            await update.message.reply_text(
                self.get_text('phone_saved', lang),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(self.get_text('order_now', lang), callback_data='order')]
                ])
            )
        except Exception as e:
            logger.error(f"Error saving contact: {e}")
            await update.message.reply_text(self.get_text('error_saving_contact', lang))

    async def handle_payment_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle payment callbacks"""
        # This would integrate with your payment provider
        # For now, we'll simulate payment processing
        query = update.callback_query
        await query.answer()

        lang = "en"
        user = await self.user_service.get_or_create_user(update.effective_user)
        if user:
            lang = user.get('language_code', 'en')
        
        payment_data = query.data.replace('pay_', '')
        
        await query.edit_message_text(
            self.get_text('processing_payment', lang),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(self.get_text('back_orders', lang), callback_data='track')]
            ])
        )

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Handle errors"""
        logger.error(f"Exception while handling an update: {context.error}")

        lang = "en"
        user = await self.user_service.get_or_create_user(update.effective_user)
        if user:
            lang = user.get('language_code', 'en')
        
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                self.get_text('error', lang)
            )

    async def setup_periodic_tasks(self):
        """Setup periodic tasks"""
        while True:
            try:
                # Check for subscription renewals
                await self.process_subscription_renewals()
                
                # Send delivery reminders
                await self.send_delivery_reminders()
                
                # Update loyalty points
                await self.update_loyalty_points()
                
                # Sleep for 1 hour
                await asyncio.sleep(3600)
                
            except Exception as e:
                logger.error(f"Error in periodic tasks: {e}")
                await asyncio.sleep(300)  # Wait 5 minutes before retry

    async def process_subscription_renewals(self):
        """Process subscription renewals"""
        try:
            async with self.db_pool.acquire() as conn:
                # Get subscriptions due for renewal
                subscriptions = await conn.fetch(
                    """SELECT s.*, u.telegram_id, p.name, p.price
                       FROM subscriptions s
                       JOIN users u ON s.user_id = u.id
                       JOIN products p ON s.product_id = p.id
                       WHERE s.next_delivery_date <= CURRENT_DATE
                       AND s.status = 'active'"""
                )
                
                for sub in subscriptions:
                    # Create automatic order
                    order_id = await self.create_subscription_order(sub)
                    
                    # Send notification
                    await self.send_subscription_notification(sub['telegram_id'], order_id)
                    
                    # Update next delivery date
                    await conn.execute(
                        """UPDATE subscriptions 
                           SET next_delivery_date = next_delivery_date + INTERVAL '%s days'
                           WHERE id = $1""",
                        sub['frequency_days'], sub['id']
                    )
                    
        except Exception as e:
            logger.error(f"Error processing subscription renewals: {e}")
    
    async def update_loyalty_points(sefl):
        pass

    async def send_delivery_reminders(self):
        """Send delivery reminders"""
        try:
            async with self.db_pool.acquire() as conn:
                # Get deliveries for tomorrow
                deliveries = await conn.fetch(
                    """SELECT d.*, u.telegram_id, o.order_number
                       FROM deliveries d
                       JOIN orders o ON d.order_id = o.id
                       JOIN users u ON o.user_id = u.id
                       WHERE d.scheduled_date = CURRENT_DATE + INTERVAL '1 day'
                       AND d.status = 'scheduled'"""
                )
                
                for delivery in deliveries:
                    # Send reminder notification
                    await self.send_delivery_reminder(delivery['telegram_id'], delivery)
                    
        except Exception as e:
            logger.error(f"Error sending delivery reminders: {e}")

    async def run_bot(self):
        """Run the bot"""
        try:
            await self.init_connections()
            
            # Create application
            application = ApplicationBuilder().token(self.bot_token).build()
            
            # Add handlers
            application.add_handler(CommandHandler("start", self.start_command))
            application.add_handler(CommandHandler("help", self.help_command))
            application.add_handler(CallbackQueryHandler(self.button_handler))
            application.add_handler(MessageHandler(filters.LOCATION, self.location_handler))
            application.add_handler(MessageHandler(filters.PHOTO, self.photo_handler))
            application.add_handler(MessageHandler(filters.CONTACT, self.contact_handler))
            application.add_handler(CommandHandler("order", self.order_command))
            application.add_handler(CallbackQueryHandler(self.order_callback_handler, pattern="^order_"))
            
            # Error handler
            application.add_error_handler(self.error_handler)
            
            # Start periodic tasks
            asyncio.create_task(self.setup_periodic_tasks())
            
            # Start the bot
            application.run_polling(drop_pending_updates=True, close_loop=False)
            
        except Exception as e:
            logger.error(f"Error running bot: {e}")
            raise
        finally:
            if self.db_pool:
                await self.db_pool.close()
            if self.redis_client:
                await self.redis_client.aclose()
            if self.http_client:
                await self.http_client.aclose()
    
    def run_bot_sync(self):
        """Run the bot"""
        try:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(self.init_connections())

            # Create application
            application = ApplicationBuilder().token(self.bot_token).build()
            
            # Add handlers
            application.add_handler(CommandHandler("start", self.start_command))
            application.add_handler(CommandHandler("help", self.help_command))
            application.add_handler(CallbackQueryHandler(self.order_callback_handler, pattern="^order_"))
            application.add_handler(CallbackQueryHandler(self.track_callback_handler, pattern="^track_"))
            application.add_handler(CallbackQueryHandler(self.subscribe_callback_handler, pattern="^sub_"))
            application.add_handler(CommandHandler("redeem_points", self.redeem_loyalty_points))
            application.add_handler(CommandHandler("payment_confirm", self.payment_confirmation_webhook))
            application.add_handler(CommandHandler("optimize_route", self.optimize_route_command))
            application.add_handler(MessageHandler(filters.LOCATION, self.location_handler))
            application.add_handler(MessageHandler(filters.PHOTO, self.photo_handler))
            application.add_handler(MessageHandler(filters.CONTACT, self.contact_handler))
            application.add_handler(CommandHandler("order", self.order_command))
            application.add_handler(CommandHandler("account", self.account_command))

            # Register the /edit_profile command, callback, and message handler in run_bot
            application.add_handler(CommandHandler("edit_profile", self.edit_profile_command))
            application.add_handler(CallbackQueryHandler(self.edit_profile_callback_handler, pattern="^(edit_profile|edit_name|edit_phone|edit_email|edit_language)$"))
            application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), self.edit_profile_message_handler))

            # Register new handlers
            application.add_handler(CommandHandler("orders", self.orders_command))
            application.add_handler(CallbackQueryHandler(self.order_details_callback_handler, pattern="^(order_details_|cancel_order_).*"))
            application.add_handler(CommandHandler("deliver", self.deliver_command))
            application.add_handler(CallbackQueryHandler(self.deliver_callback_handler, pattern="^deliver_.*"))
            application.add_handler(CommandHandler("subscription", self.subscription_menu))
            application.add_handler(CallbackQueryHandler(self.subscription_callback_handler, pattern="^(pause_sub_|resume_sub_|edit_sub_).*$"))
            application.add_handler(CommandHandler("notifications", self.notification_prefs_menu))
            application.add_handler(CallbackQueryHandler(self.notification_prefs_callback_handler, pattern="^(toggle_sms|toggle_email|toggle_telegram|toggle_marketing)$"))
            
            # Error handler
            application.add_error_handler(self.error_handler)

            # --- Subscription Management ---
            application.add_handler(CommandHandler("subscribe", self.subscribe_command))
            application.add_handler(CommandHandler("mysubscriptions", self.mysubscriptions_command))
            

            # --- Order Tracking ---
            application.add_handler(CommandHandler("track", self.track_command))
            

            # --- Loyalty & Analytics ---
            application.add_handler(CommandHandler("loyalty", self.show_loyalty_menu))
            application.add_handler(CommandHandler("loyalty_history", self.loyalty_history_command))

            # --- Admin Features ---
            application.add_handler(CommandHandler("admin_orders", self.admin_orders_command))
            application.add_handler(CommandHandler("admin_stats", self.admin_stats_command))

            application.add_handler(CallbackQueryHandler(self.button_handler))
            
            # # Start periodic tasks
            # logger.info("Creating periodic tasks")
            # task = asyncio.create_task(self.setup_periodic_tasks())
            # logger.info("periodic tasks created")
            # loop.run_until_complete(task)
            
            logger.info("Starting the bot")
            # Start the bot
            application.run_polling(poll_interval=2, drop_pending_updates=True)
            
        except Exception as e:
            logger.error(f"Error running bot: {e}")
            raise
        finally:
            if self.db_pool:
                asyncio.run(self.db_pool.close())
            if self.redis_client:
                asyncio.run(self.redis_client.aclose())
            if self.http_client:
                asyncio.run(self.http_client.aclose())

    # --- Subscription Management ---
    async def subscribe_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        user_id = update.effective_user.id
        self.user_states[user_id] = {'state': 'subscribe_select_product'}
        products = await self.product_service.get_available_products()
        if not products:
            if update.message:
                await update.message.reply_text(self.get_text('no_products_subscription', lang))
            elif update.callback_query:
                await update.callback_query.edit_message_text(self.get_text('no_products_subscription', lang))
            return
        keyboard = [
            [InlineKeyboardButton(f"{p['name']} ({p['price']} UZS)", callback_data=f"sub_product_{p['id']}")]
            for p in products
        ]
        if update.message:
            await update.message.reply_text(
                self.get_text('select_product_to_subscribe', lang),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        elif update.callback_query:
            await update.callback_query.edit_message_text(
                self.get_text('select_product_to_subscribe', lang),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    async def mysubscriptions_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        subs = await self.subscription_service.get_user_subscriptions(user['id'])
        if not subs:
            if update.message:
                await update.message.reply_text(self.get_text('no_active_subscriptions', lang))
            elif update.callback_query:
                await update.callback_query.edit_message_text(self.get_text('no_active_subscriptions', lang))
            return
        keyboard = [
            [InlineKeyboardButton(f"{sub['product_name']} every {sub['frequency_days']}d x{sub['quantity']}", callback_data=f"sub_cancel_{sub['id']}")]
            for sub in subs
        ]
        if update.message:
            await update.message.reply_text(
                self.get_text('your_subscriptions', lang),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        elif update.callback_query:
            await update.callback_query.edit_message_text(
                self.get_text('your_subscriptions', lang),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    async def subscribe_callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user_id = query.from_user.id
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        state = self.user_states.get(user_id, {})
        await query.answer()
        if not state:
            await query.edit_message_text(self.get_text('session_expired', lang))
            return
        if state['state'] == 'subscribe_select_product':
            if query.data.startswith("sub_product_"):
                product_id = query.data.replace("sub_product_", "")
                state['product_id'] = product_id
                state['state'] = 'subscribe_frequency'
                self.user_states[user_id] = state
                keyboard = [
                    [InlineKeyboardButton(f"Every {d} days", callback_data=f"sub_freq_{d}")] for d in [3, 7, 14, 30]
                ]
                await query.edit_message_text(
                    self.get_text('how_often_delivery', lang),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        elif state['state'] == 'subscribe_frequency':
            if query.data.startswith("sub_freq_"):
                freq = int(query.data.replace("sub_freq_", ""))
                state['frequency_days'] = freq
                state['state'] = 'subscribe_quantity'
                self.user_states[user_id] = state
                keyboard = [
                    [InlineKeyboardButton(str(q), callback_data=f"sub_qty_{q}") for q in range(1, 6)]
                ]
                await query.edit_message_text(
                    self.get_text('how_many_units', lang),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        elif state['state'] == 'subscribe_quantity':
            if query.data.startswith("sub_qty_"):
                qty = int(query.data.replace("sub_qty_", ""))
                state['quantity'] = qty
                user = await self.user_service.get_or_create_user(update.effective_user)
                try:
                    sub = await self.subscription_service.create_subscription(
                        user['id'], state['product_id'], state['frequency_days'], qty
                    )
                    await query.edit_message_text(self.get_text('subscription_created', lang))
                except Exception as e:
                    logger.error(f"Subscription error: {e}")
                    await query.edit_message_text(self.get_text('subscription_failed', lang))
                del self.user_states[user_id]
        # Cancel subscription from /mysubscriptions
        elif query.data.startswith("sub_cancel_"):
            sub_id = query.data.replace("sub_cancel_", "")
            ok = await self.subscription_service.cancel_subscription(sub_id)
            if ok:
                await query.edit_message_text(self.get_text('subscription_cancelled', lang))
            else:
                await query.edit_message_text(self.get_text('subscription_cancel_failed', lang))

    # --- Order Tracking ---
    async def track_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        orders = await self.order_service.get_user_orders(user['id'], limit=5)
        if not orders:
            if update.message:
                await update.message.reply_text(self.get_text('no_recent_orders', lang))
            elif update.callback_query:
                await update.callback_query.edit_message_text(self.get_text('no_recent_orders', lang))
            return
        keyboard = [
            [InlineKeyboardButton(f"Order {o['order_number']} ({o['status']})", callback_data=f"track_{o['id']}")]
            for o in orders
        ]
        if update.message:
            await update.message.reply_text(
                self.get_text('your_recent_orders', lang),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        elif update.callback_query:
            await update.callback_query.edit_message_text(
                self.get_text('your_recent_orders', lang),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    async def track_callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        if query.data.startswith("track_"):
            order_id = query.data.replace("track_", "")
            tracking = await self.delivery_service.get_delivery_tracking(order_id)
            events = tracking.get('events', [])
            events_text = "\n".join([
                f"{e['time']}: {e['type']} - {e['description']}" for e in events
            ])
            text = (
                f"{self.get_text('order_tracking', lang)}\n"
                f"{self.get_text('order_number', lang)}: {tracking.get('order_id')}\n"
                f"{self.get_text('status', lang)}: {tracking.get('status')}\n"
                f"{self.get_text('address', lang)}: {tracking.get('address')}\n"
                f"{self.get_text('slot', lang)}: {tracking.get('slot')}\n\n"
                f"{self.get_text('events', lang)}:\n{events_text or self.get_text('no_events', lang)}"
            )
            await query.edit_message_text(text)

    # --- Loyalty & Analytics ---
    async def show_loyalty_menu(self, query, lang: str):
        user = await self.user_service.get_or_create_user(query.from_user)
        analytics = await self.analytics_service.get_customer_analytics(user['id'])
        points = user.get('loyalty_points', 0)
        text = (
            f"🌟 Loyalty Points: {points}\n"
            f"Total Orders: {analytics.get('total_orders', 0)}\n"
            f"Total Spent: {analytics.get('total_spent', 0)} UZS\n"
            f"Avg Order Value: {analytics.get('avg_order_value', 0)} UZS\n"
            f"Favorite Products: {', '.join(str(p[0]) for p in analytics.get('favorite_products', []))}\n"
            f"Last Order: {analytics.get('last_order_date', 'N/A')}"
        )
        keyboard = [
            [InlineKeyboardButton("🎁 Redeem Points", callback_data='redeem_points')],
            [InlineKeyboardButton("📜 View History", callback_data='loyalty_history')],
            [InlineKeyboardButton(self.get_text('back_main', lang), callback_data='back_main')]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    async def loyalty_history_command(self, update, context):
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        transactions = await self.payment_service.get_loyalty_transactions(user['id'])
        if not transactions:
            if update.message:
                await update.message.reply_text(self.get_text('no_loyalty_transactions', lang))
            elif update.callback_query:
                await update.callback_query.edit_message_text(self.get_text('no_loyalty_transactions', lang))
            return
        lines = [
            f"{t['created_at'].strftime('%Y-%m-%d %H:%M')}: {t['transaction_type'].capitalize()} {t['points']} pts ({t['reason']})"
            for t in transactions
        ]
        text = self.get_text('loyalty_transactions_header', lang) + "\n" + "\n".join(lines)
        if update.message:
            await update.message.reply_text(text)
        elif update.callback_query:
            await update.callback_query.edit_message_text(text)

    # --- Admin Features ---
    async def admin_orders_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        if not await self.is_admin(user):
            await update.message.reply_text(self.get_text('not_admin', lang))
            return
        orders = await self.admin_service.get_pending_orders()
        if not orders:
            await update.message.reply_text(self.get_text('no_pending_orders', lang))
            return
        text = "\n".join([
            f"Order {o['order_number']} by {o['username']} ({o['phone']}) - {o['status']}" for o in orders
        ])
        await update.message.reply_text(self.get_text('pending_orders', lang) + "\n" + text)

    async def admin_stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        if not await self.is_admin(user):
            await update.message.reply_text(self.get_text('not_admin', lang))
            return
        stats = await self.admin_service.get_system_stats()
        text = (
            f"👥 Total Users: {stats.get('total_users', 0)}\n"
            f"📦 Today's Orders: {stats.get('today_orders', 0)}\n"
            f"⏳ Pending Orders: {stats.get('pending_orders', 0)}\n"
            f"💰 Today's Revenue: {stats.get('today_revenue', 0)} UZS"
        )
        await update.message.reply_text(text)

    async def optimize_route_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        if not await self.is_admin(user):
            await update.message.reply_text(self.get_text('not_admin', lang))
            return
        deliveries = await self.delivery_service.get_deliveries_for_person(user['id'], status='in_transit')
        order_ids = [d['order_id'] for d in deliveries]
        if not order_ids:
            await update.message.reply_text(self.get_text('no_deliveries', lang))
            return
        route = await self.delivery_service.optimize_route(user['id'], order_ids)
        text = f"Optimized Route: {route.orders}\nEstimated Duration: {route.estimated_duration} min"
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(self.get_text('back_main', lang), callback_data='back_main')]]))

    async def account_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        text = (
            f"👤 <b>{self.get_text('profile', lang)}</b>\n"
            f"<b>{self.get_text('name', lang)}:</b> {user.get('first_name', '')} {user.get('last_name', '')}\n"
            f"<b>{self.get_text('phone', lang)}:</b> {user.get('phone', '-') }\n"
            f"<b>{self.get_text('email', lang)}:</b> {user.get('email', '-') }\n"
            f"<b>{self.get_text('language', lang)}:</b> {lang}\n"
            f"<b>{self.get_text('vip_status', lang)}:</b> {'VIP' if user.get('is_vip') else 'Regular'}\n"
            f"<b>{self.get_text('loyalty_points', lang)}:</b> {user.get('loyalty_points', 0)}\n"
        )
        keyboard = [
            [InlineKeyboardButton(self.get_text('edit_profile', lang), callback_data='edit_profile')],
            [InlineKeyboardButton(self.get_text('manage_addresses', lang), callback_data='manage_addresses')],
            [InlineKeyboardButton(self.get_text('back_main', lang), callback_data='back_main')]
        ]
        if update.message:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        elif update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

    async def show_address_menu(self, query, lang: str):
        user = await self.user_service.get_or_create_user(query.from_user)
        addresses = await self.address_service.get_user_addresses(user['id'])
        text = self.get_text('your_addresses', lang) + "\n\n"
        keyboard = []
        for addr in addresses:
            label = addr['label'] or self.get_text('address', lang)
            addr_str = f"{label}: {addr['address_line1']}, {addr['city']}"
            if addr['is_default']:
                addr_str += f" ({self.get_text('default', lang)})"
            text += f"• {addr_str}\n"
            keyboard.append([
                InlineKeyboardButton(self.get_text('set_default', lang), callback_data=f"set_default_addr_{addr['id']}"),
                InlineKeyboardButton(self.get_text('edit', lang), callback_data=f"edit_addr_{addr['id']}"),
                InlineKeyboardButton(self.get_text('delete', lang), callback_data=f"delete_addr_{addr['id']}")
            ])
        keyboard.append([InlineKeyboardButton(self.get_text('add_address', lang), callback_data='add_address')])
        keyboard.append([InlineKeyboardButton(self.get_text('back_main', lang), callback_data='back_main')])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    async def address_callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user = await self.user_service.get_or_create_user(query.from_user)
        lang = user.get('language_code', 'en')
        data = query.data
        if data == 'manage_addresses':
            await self.show_address_menu(query, lang)
        elif data == 'add_address':
            self.user_states[user['telegram_id']] = {'state': 'add_address_label'}
            await query.edit_message_text(self.get_text('enter_label', lang))
        elif data.startswith('edit_addr_'):
            addr_id = data.replace('edit_addr_', '')
            addr = await self.address_service.get_address_by_id(addr_id)
            self.user_states[user['telegram_id']] = {
                'state': 'edit_address_label',
                'edit_addr_id': addr_id,
                'edit_addr': addr
            }
            await query.edit_message_text(self.get_text('enter_label', lang) + f" (current: {addr.get('label','') or '-'})")
        elif data.startswith('set_default_addr_'):
            addr_id = data.replace('set_default_addr_', '')
            await self.address_service.set_default_address(user['id'], addr_id)
            await query.answer(self.get_text('default_set', lang))
            await self.show_address_menu(query, lang)
        elif data.startswith('delete_addr_'):
            addr_id = data.replace('delete_addr_', '')
            await self.address_service.delete_address(addr_id)
            await query.answer(self.get_text('address_deleted', lang))
            await self.show_address_menu(query, lang)

    async def address_message_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        state = self.user_states.get(user['telegram_id'], {})
        # Location pin support
        if update.message.location:
            # Simulate reverse geocoding
            state['address_line1'] = f"Street near {update.message.location.latitude:.4f},{update.message.location.longitude:.4f}"
            state['city'] = "Tashkent"  # Simulate city
            state['state'] = None
            state['country'] = 'UZ'
            state['state'] = 'add_address_label'
            self.user_states[user['telegram_id']] = state
            await update.message.reply_text(self.get_text('enter_label', lang) + f"\n{self.get_text('address_line1', lang)}: {state['address_line1']}\n{self.get_text('city', lang)}: {state['city']}")
            return
        # Add address flow (with validation)
        if state.get('state') == 'add_address_label':
            state['label'] = update.message.text
            state['state'] = 'add_address_line1'
            self.user_states[user['telegram_id']] = state
            await update.message.reply_text(self.get_text('enter_address_line1', lang))
        elif state.get('state') == 'add_address_line1':
            state['address_line1'] = update.message.text
            if not state['address_line1']:
                await update.message.reply_text(self.get_text('address_invalid', lang))
                return
            state['state'] = 'add_address_line2'
            self.user_states[user['telegram_id']] = state
            await update.message.reply_text(self.get_text('enter_address_line2', lang))
        elif state.get('state') == 'add_address_city':
            state['city'] = update.message.text
            if not state['city']:
                await update.message.reply_text(self.get_text('address_invalid', lang))
                return
            state['state'] = 'add_address_state'
            self.user_states[user['telegram_id']] = state
            await update.message.reply_text(self.get_text('enter_state', lang))
        elif state.get('state') == 'add_address_state':
            val = update.message.text
            state['state'] = 'add_address_postal_code'
            state['state_val'] = val
            state['state_val_type'] = 'state'
            state['state_val'] = None if val.strip() == '-' else val
            self.user_states[user['telegram_id']] = state
            await update.message.reply_text(self.get_text('enter_postal_code', lang))
        elif state.get('state') == 'add_address_postal_code':
            val = update.message.text
            state['postal_code'] = None if val.strip() == '-' else val
            state['state'] = 'add_address_country'
            self.user_states[user['telegram_id']] = state
            await update.message.reply_text(self.get_text('enter_country', lang))
        elif state.get('state') == 'add_address_country':
            val = update.message.text
            state['country'] = val if val.strip() else 'UZ'
            state['state'] = 'add_address_instructions'
            self.user_states[user['telegram_id']] = state
            await update.message.reply_text(self.get_text('enter_instructions', lang))
        elif state.get('state') == 'add_address_instructions':
            val = update.message.text
            state['delivery_instructions'] = None if val.strip() == '-' else val
            # Add address to DB
            await self.address_service.add_address(
                user['id'],
                label=state.get('label'),
                address_line1=state.get('address_line1'),
                address_line2=state.get('address_line2'),
                city=state.get('city'),
                state=state.get('state_val'),
                postal_code=state.get('postal_code'),
                country=state.get('country'),
                is_default=False,
                delivery_instructions=state.get('delivery_instructions')
            )
            await update.message.reply_text(self.get_text('address_added', lang))
            del self.user_states[user['telegram_id']]
        # Edit address flow
        elif state.get('state') == 'edit_address_label':
            state['label'] = update.message.text
            state['state'] = 'edit_address_line1'
            self.user_states[user['telegram_id']] = state
            await update.message.reply_text(self.get_text('enter_address_line1', lang) + f" (current: {state['edit_addr'].get('address_line1','') or '-'})")
        elif state.get('state') == 'edit_address_line1':
            state['address_line1'] = update.message.text
            state['state'] = 'edit_address_line2'
            self.user_states[user['telegram_id']] = state
            await update.message.reply_text(self.get_text('enter_address_line2', lang) + f" (current: {state['edit_addr'].get('address_line2','') or '-'})")
        elif state.get('state') == 'edit_address_line2':
            val = update.message.text
            state['address_line2'] = None if val.strip() == '-' else val
            state['state'] = 'edit_address_city'
            self.user_states[user['telegram_id']] = state
            await update.message.reply_text(self.get_text('enter_city', lang) + f" (current: {state['edit_addr'].get('city','') or '-'})")
        elif state.get('state') == 'edit_address_city':
            state['city'] = update.message.text
            state['state'] = 'edit_address_state'
            self.user_states[user['telegram_id']] = state
            await update.message.reply_text(self.get_text('enter_state', lang) + f" (current: {state['edit_addr'].get('state','') or '-'})")
        elif state.get('state') == 'edit_address_state':
            val = update.message.text
            state['state_val'] = None if val.strip() == '-' else val
            state['state'] = 'edit_address_postal_code'
            self.user_states[user['telegram_id']] = state
            await update.message.reply_text(self.get_text('enter_postal_code', lang) + f" (current: {state['edit_addr'].get('postal_code','') or '-'})")
        elif state.get('state') == 'edit_address_postal_code':
            val = update.message.text
            state['postal_code'] = None if val.strip() == '-' else val
            state['state'] = 'edit_address_country'
            self.user_states[user['telegram_id']] = state
            await update.message.reply_text(self.get_text('enter_country', lang) + f" (current: {state['edit_addr'].get('country','') or '-'})")
        elif state.get('state') == 'edit_address_country':
            val = update.message.text
            state['country'] = val if val.strip() else 'UZ'
            state['state'] = 'edit_address_instructions'
            self.user_states[user['telegram_id']] = state
            await update.message.reply_text(self.get_text('enter_instructions', lang) + f" (current: {state['edit_addr'].get('delivery_instructions','') or '-'})")
        elif state.get('state') == 'edit_address_instructions':
            val = update.message.text
            state['delivery_instructions'] = None if val.strip() == '-' else val
            # Update address in DB
            await self.address_service.update_address(
                state['edit_addr_id'],
                label=state.get('label'),
                address_line1=state.get('address_line1'),
                address_line2=state.get('address_line2'),
                city=state.get('city'),
                state=state.get('state_val'),
                postal_code=state.get('postal_code'),
                country=state.get('country'),
                delivery_instructions=state.get('delivery_instructions')
            )
            await update.message.reply_text(self.get_text('address_updated', lang))
            del self.user_states[user['telegram_id']]

    # In order confirmation, show full address summary and allow user to change address before confirming
    # (Assume in order_callback_handler, before showing order summary)
    # Fetch address by state['selected_address_id'] and show summary in confirmation message

    async def edit_profile_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        keyboard = [
            [InlineKeyboardButton(self.get_text('edit_name', lang), callback_data='edit_name')],
            [InlineKeyboardButton(self.get_text('edit_phone', lang), callback_data='edit_phone')],
            [InlineKeyboardButton(self.get_text('edit_email', lang), callback_data='edit_email')],
            [InlineKeyboardButton(self.get_text('edit_language', lang), callback_data='edit_language')],
            [InlineKeyboardButton(self.get_text('back_main', lang), callback_data='back_main')]
        ]
        text = self.get_text('edit_profile_menu', lang)
        if update.message:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    async def edit_profile_callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user = await self.user_service.get_or_create_user(query.from_user)
        lang = user.get('language_code', 'en')
        data = query.data
        if data == 'edit_profile':
            await self.edit_profile_command(update, context)
        elif data == 'edit_name':
            self.user_states[user['telegram_id']] = {'state': 'edit_first_name'}
            await query.edit_message_text(self.get_text('enter_first_name', lang))
        elif data == 'edit_phone':
            self.user_states[user['telegram_id']] = {'state': 'edit_phone'}
            await query.edit_message_text(self.get_text('enter_phone', lang))
        elif data == 'edit_email':
            self.user_states[user['telegram_id']] = {'state': 'edit_email'}
            await query.edit_message_text(self.get_text('enter_email', lang))
        elif data == 'edit_language':
            await self.show_language_menu(query, lang)

    async def edit_profile_message_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        state = self.user_states.get(user['telegram_id'], {})
        if state.get('state') == 'edit_first_name':
            state['first_name'] = update.message.text
            state['state'] = 'edit_last_name'
            self.user_states[user['telegram_id']] = state
            await update.message.reply_text(self.get_text('enter_last_name', lang))
        elif state.get('state') == 'edit_last_name':
            state['last_name'] = update.message.text
            await self.user_service.update_profile(user['telegram_id'], first_name=state['first_name'], last_name=state['last_name'])
            await update.message.reply_text(self.get_text('profile_updated', lang))
            del self.user_states[user['telegram_id']]
        elif state.get('state') == 'edit_phone':
            phone = update.message.text
            code = await self.user_service.start_phone_verification(user['telegram_id'], phone)
            state['phone'] = phone
            state['state'] = 'verify_phone_code'
            state['verification_code'] = code
            self.user_states[user['telegram_id']] = state
            await update.message.reply_text(self.get_text('phone_verification_code', lang))
        elif state.get('state') == 'verify_phone_code':
            code = update.message.text
            if await self.user_service.verify_phone_code(user['telegram_id'], code):
                await self.user_service.update_profile(user['telegram_id'], phone=state['phone'])
                await update.message.reply_text(self.get_text('phone_verified', lang))
                del self.user_states[user['telegram_id']]
            else:
                await update.message.reply_text(self.get_text('invalid_code', lang))
        elif state.get('state') == 'edit_email':
            email = update.message.text
            code = await self.user_service.start_email_verification(user['telegram_id'], email)
            state['email'] = email
            state['state'] = 'verify_email_code'
            state['verification_code'] = code
            self.user_states[user['telegram_id']] = state
            await update.message.reply_text(self.get_text('email_verification_code', lang))
        elif state.get('state') == 'verify_email_code':
            code = update.message.text
            if await self.user_service.verify_email_code(user['telegram_id'], code):
                await self.user_service.update_profile(user['telegram_id'], email=state['email'])
                await update.message.reply_text(self.get_text('email_verified', lang))
                del self.user_states[user['telegram_id']]
            else:
                await update.message.reply_text(self.get_text('invalid_code', lang))

    # Order flow enhancements
    async def orders_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        orders = await self.order_service.get_user_orders(user['id'], limit=10)
        if not orders:
            await update.message.reply_text(self.get_text('no_recent_orders', lang))
            return
        keyboard = [
            [InlineKeyboardButton(f"{self.get_text('order_number', lang)} {o['order_number']} ({o['status']})", callback_data=f"order_details_{o['id']}")]
            for o in orders
        ]
        await update.message.reply_text(self.get_text('orders_list', lang), reply_markup=InlineKeyboardMarkup(keyboard))

    async def order_details_callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user = await self.user_service.get_or_create_user(query.from_user)
        lang = user.get('language_code', 'en')
        if query.data.startswith('order_details_'):
            order_id = query.data.replace('order_details_', '')
            order = await self.order_service.get_order_details(order_id)
            address = await self.address_service.get_address_by_id(order['delivery_address_id']) if order.get('delivery_address_id') else None
            address_str = f"{address['label'] or ''}, {address['address_line1']}, {address['city']}" if address else '-'
            items = order.get('items', [])
            items_str = '\n'.join([f"{item['product_name']} x{item['quantity']} = {item['total_price']} UZS" for item in items])
            text = (
                f"{self.get_text('order_details', lang)}\n"
                f"{self.get_text('order_number', lang)}: {order['order_number']}\n"
                f"{self.get_text('status', lang)}: {order['status']}\n"
                f"{self.get_text('order_address', lang)}: {address_str}\n"
                f"{self.get_text('events', lang)}: {order['created_at']}\n"
                f"{self.get_text('cart', lang)}:\n{items_str}\n"
            )
            keyboard = []
            if order['status'] not in ['delivered', 'cancelled']:
                keyboard.append([InlineKeyboardButton(self.get_text('cancel_order', lang), callback_data=f"cancel_order_{order_id}")])
            keyboard.append([InlineKeyboardButton(self.get_text('back_main', lang), callback_data='back_main')])
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif query.data.startswith('cancel_order_'):
            order_id = query.data.replace('cancel_order_', '')
            order = await self.order_service.get_order_details(order_id)
            if order['status'] == 'delivered':
                await query.answer(self.get_text('order_already_delivered', lang))
                return
            await self.order_service.update_order_status(order_id, 'cancelled', user['id'])
            await query.edit_message_text(self.get_text('order_cancel_success', lang))


    # Step 4: Delivery Person Flow
    async def deliver_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        if not await self.is_delivery_person(user):
            await update.message.reply_text(self.get_text('not_delivery_person', lang))
            return
        deliveries = await self.delivery_service.get_deliveries_for_person(user['id'], status='in_transit')
        if not deliveries:
            await update.message.reply_text(self.get_text('no_deliveries', lang))
            return
        keyboard = [
            [InlineKeyboardButton(f"{self.get_text('order_number', lang)} {d['order_id']} ({d['status']})", callback_data=f"deliver_update_{d['order_id']}")]
            for d in deliveries
        ]
        await update.message.reply_text(self.get_text('my_deliveries', lang), reply_markup=InlineKeyboardMarkup(keyboard))

    async def deliver_callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user = await self.user_service.get_or_create_user(query.from_user)
        lang = user.get('language_code', 'en')
        if query.data.startswith('deliver_update_'):
            order_id = query.data.replace('deliver_update_', '')
            keyboard = [
                [InlineKeyboardButton(self.get_text('mark_delivered', lang), callback_data=f"deliver_status_{order_id}_delivered")],
                [InlineKeyboardButton(self.get_text('mark_in_transit', lang), callback_data=f"deliver_status_{order_id}_in_transit")],
                [InlineKeyboardButton(self.get_text('mark_failed', lang), callback_data=f"deliver_status_{order_id}_failed")],
            ]
            await query.edit_message_text(self.get_text('update_status', lang), reply_markup=InlineKeyboardMarkup(keyboard))
        elif query.data.startswith('deliver_status_'):
            parts = query.data.split('_')
            order_id = parts[2]
            status = parts[3]
            await self.delivery_service.update_delivery_status(order_id, status, delivery_person_id=user['id'])
            await query.edit_message_text(self.get_text('status_updated', lang))

    # Step 5: Subscriptions
    async def subscription_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        subs = await self.subscription_service.get_user_subscriptions(user['id'])
        if not subs:
            await update.message.reply_text(self.get_text('no_active_subscriptions', lang))
            return
        sub = subs[0]  # For simplicity, show first
        keyboard = [
            [InlineKeyboardButton(self.get_text('pause_subscription', lang), callback_data=f"pause_sub_{sub['id']}")],
            [InlineKeyboardButton(self.get_text('resume_subscription', lang), callback_data=f"resume_sub_{sub['id']}")],
            [InlineKeyboardButton(self.get_text('edit_subscription', lang), callback_data=f"edit_sub_{sub['id']}")],
            [InlineKeyboardButton(self.get_text('back_main', lang), callback_data='back_main')]
        ]
        text = self.get_text('subscription_menu', lang) + f"\n{self.get_text('order_number', lang)}: {sub['id']}\nStatus: {sub['status']}"
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    async def subscription_callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user = await self.user_service.get_or_create_user(query.from_user)
        lang = user.get('language_code', 'en')
        if query.data.startswith('pause_sub_'):
            sub_id = query.data.replace('pause_sub_', '')
            await self.subscription_service.pause_subscription(sub_id)
            await query.edit_message_text(self.get_text('subscription_paused', lang))
        elif query.data.startswith('resume_sub_'):
            sub_id = query.data.replace('resume_sub_', '')
            await self.subscription_service.resume_subscription(sub_id)
            await query.edit_message_text(self.get_text('subscription_resumed', lang))
        elif query.data.startswith('edit_sub_'):
            sub_id = query.data.replace('edit_sub_', '')
            # For simplicity, just simulate edit
            await self.subscription_service.edit_subscription(sub_id, frequency_days=7)
            await query.edit_message_text(self.get_text('subscription_edited', lang))

    # Simulate renewal notification in process_subscription_renewals
    async def process_subscription_renewals(self):
        try:
            due_renewals = await self.subscription_service.get_due_renewals()
            for sub in due_renewals:
                await self.subscription_service.notify_renewal(sub['user_id'], sub['id'])
        except Exception as e:
            logger.error(f"Error processing subscription renewals: {e}")

    # Step 6: Notification Preferences
    async def notification_prefs_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        prefs = await self.notification_service.get_user_preferences(user['id'])
        keyboard = [
            [InlineKeyboardButton(f"{self.get_text('sms_notifications', lang)}: {'✅' if prefs and prefs.get('notification_sms') else '❌'}", callback_data='toggle_sms')],
            [InlineKeyboardButton(f"{self.get_text('email_notifications', lang)}: {'✅' if prefs and prefs.get('notification_email') else '❌'}", callback_data='toggle_email')],
            [InlineKeyboardButton(f"{self.get_text('telegram_notifications', lang)}: {'✅' if prefs and prefs.get('notification_telegram') else '❌'}", callback_data='toggle_telegram')],
            [InlineKeyboardButton(f"{self.get_text('marketing_communications', lang)}: {'✅' if prefs and prefs.get('marketing_communications') else '❌'}", callback_data='toggle_marketing')],
            [InlineKeyboardButton(self.get_text('back_main', lang), callback_data='back_main')]
        ]
        await update.message.reply_text(self.get_text('notification_prefs', lang), reply_markup=InlineKeyboardMarkup(keyboard))

    async def notification_prefs_callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user = await self.user_service.get_or_create_user(query.from_user)
        lang = user.get('language_code', 'en')
        prefs = await self.notification_service.get_user_preferences(user['id'])
        data = query.data
        updates = {}
        if data == 'toggle_sms':
            updates['notification_sms'] = not prefs.get('notification_sms', True)
        elif data == 'toggle_email':
            updates['notification_email'] = not prefs.get('notification_email', True)
        elif data == 'toggle_telegram':
            updates['notification_telegram'] = not prefs.get('notification_telegram', True)
        elif data == 'toggle_marketing':
            updates['marketing_communications'] = not prefs.get('marketing_communications', True)
        if updates:
            await self.notification_service.set_user_preferences(user['id'], **updates)
        await self.notification_prefs_menu(update, context)
        await query.answer(self.get_text('prefs_updated', lang))


    # 4. Wire up NotificationService in order placement, delivery, subscription
    # In order_callback_handler, after order is created:
    # await self.notification_service.send_order_notification(user['id'], order, 'order_confirmed')
    # In deliver_callback_handler, after status update:
    # await self.notification_service.send_event_notification(user['id'], 'delivery_status', {'order_id': order_id, 'status': status})
    # In process_subscription_renewals, after renewal:
    # await self.notification_service.send_event_notification(sub['user_id'], 'subscription_renewal', {'sub_id': sub['id']})

    # 5. Add map links to order summary, tracking, and delivery messages
    # In order_details_callback_handler and track_callback_handler, add map link to address
    # In order summary, add map link if address has lat/lon

    # 6. Add /optimize_route command for delivery staff/admin
    async def optimize_route_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        if not await self.is_admin(user):
            await update.message.reply_text(self.get_text('not_admin', lang))
            return
        deliveries = await self.delivery_service.get_deliveries_for_person(user['id'], status='in_transit')
        order_ids = [d['order_id'] for d in deliveries]
        if not order_ids:
            await update.message.reply_text(self.get_text('no_deliveries', lang))
            return
        route = await self.delivery_service.optimize_route(user['id'], order_ids)
        text = f"Optimized Route: {route.orders}\nEstimated Duration: {route.estimated_duration} min"
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(self.get_text('back_main', lang), callback_data='back_main')]]))


    # 7. Use SecurityService.log_security_event for admin/delivery actions
    # In admin_orders_command and deliver_command, log security events
    # await self.security_service.log_security_event('admin_access', user['id'], 'Accessed admin orders')
    # await self.security_service.log_security_event('delivery_access', user['id'], 'Accessed deliveries')


def main():
    """Main function"""
    bot = WaterBusinessBot()
    # asyncio.run(bot.run_bot())
    # loop = asyncio.get_running_loop()
    # loop = asyncio.get_event_loop()
    # loop.run_until_complete(bot.run_bot())
    bot.run_bot_sync()

if __name__ == "__main__":
    main()