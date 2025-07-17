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
            # --- End instantiate services ---
        except Exception as e:
            logger.error(f"Failed to initialize connections: {e}")
            raise

    def get_text(self, key: str, lang: str = 'en') -> str:
        """Get translated text"""
        return self.translations.get(lang, {}).get(key, self.translations['en'].get(key, key))

    async def get_main_keyboard(self, user_id: int, lang: str = 'en') -> InlineKeyboardMarkup:
        is_admin = False
        try:
            is_admin = await self.admin_service.is_admin(user_id)
        except Exception:
            pass
        keyboard = [
            [
                InlineKeyboardButton(f"🛒 {self.get_text('order_menu', lang)}", callback_data='order'),
                InlineKeyboardButton(f"📦 {self.get_text('track_menu', lang)}", callback_data='track')
            ],
            [
                InlineKeyboardButton("🔄 My Subscriptions", callback_data='mysubscriptions'),
                InlineKeyboardButton("➕ Subscribe", callback_data='subscribe')
            ],
            [
                InlineKeyboardButton("🌟 Loyalty & Analytics", callback_data='loyalty'),
                InlineKeyboardButton(f"👤 {self.get_text('account_menu', lang)}", callback_data='account')
            ],
            [
                InlineKeyboardButton("🔔 Notifications", callback_data='notifications'),
                InlineKeyboardButton("📊 Analytics", callback_data='analytics')
            ],
            [
                InlineKeyboardButton("ℹ️ Info", callback_data='info'),
                InlineKeyboardButton("🌐 Language", callback_data='language')
            ],
            [
                InlineKeyboardButton("🎯 VIP Services", callback_data='vip')
            ]
        ]
        if is_admin:
            keyboard.append([
                InlineKeyboardButton("🛠️ Admin Panel", callback_data='admin_panel')
            ])
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
            await self.loyalty_command(update, context)
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
        elif data.startswith('lang_'):
            new_lang = data.replace('lang_', '')
            await self.user_service.set_user_language(user_id, new_lang)
            await query.answer("Language updated!")
            await self.show_main_menu(query, new_lang)
        elif data == 'vip':
            await self.show_vip_menu(query, lang)
        elif data == 'admin_panel':
            await query.edit_message_text("🛠️ Admin Panel:\n- /admin_orders\n- /admin_stats")
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

    async def order_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str = "en"):
        user = update.effective_user
        user_id = user.id
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
                    await query.edit_message_text("Product not found.")
                    return
                state['selected_product'] = product
                state['state'] = ORDER_STATE['SELECT_QUANTITY']
                self.user_states[user_id] = state
                keyboard = [
                    [InlineKeyboardButton(str(q), callback_data=f"order_qty_{q}") for q in range(1, 6)]
                ]
                await query.edit_message_text(
                    self.get_text('select_quantity', lang).format(product['name']),
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
                state['state'] = ORDER_STATE['DELIVERY_SLOT']
                self.user_states[user_id] = state
                # For demo, use a static warehouse location
                user = await self.user_service.get_or_create_user(update.effective_user)
                user_location = {'latitude': 41.2995, 'longitude': 69.2401} # fallback
                if user.get('latitude') and user.get('longitude'):
                    user_location = {'latitude': user['latitude'], 'longitude': user['longitude']}
                warehouse_location = {'latitude': 41.2995, 'longitude': 69.2401}
                fee = await self.delivery_service.calculate_delivery_fee(user_location, warehouse_location)
                slots = await self.delivery_service.get_available_slots(user_location)
                if not slots:
                    await query.edit_message_text("No delivery slots available. Please try again later.")
                    return
                state['delivery_fee'] = float(fee)
                state['slots'] = slots
                keyboard = [
                    [InlineKeyboardButton(slot.slot_id, callback_data=f"order_slot_{slot.slot_id}")]
                    for slot in slots[:5]
                ]
                await query.edit_message_text(
                    self.get_text('select_delivery_slot', lang).format(fee),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            elif query.data == "order_cancel":
                del self.user_states[user_id]
                await query.edit_message_text(self.get_text('order_cancelled', lang))
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
                address = user.get('address', 'No address')
                payment_method = state['payment_method']
                total = float(sum(item['price']*item['quantity'] for item in cart)) + state.get('delivery_fee', 0)
                try:
                    order = await self.order_service.create_order(user['id'], cart, address, payment_method)
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
            application.add_handler(CallbackQueryHandler(self.button_handler))
            application.add_handler(MessageHandler(filters.LOCATION, self.location_handler))
            application.add_handler(MessageHandler(filters.PHOTO, self.photo_handler))
            application.add_handler(MessageHandler(filters.CONTACT, self.contact_handler))
            application.add_handler(CommandHandler("order", self.order_command))
            
            # Error handler
            application.add_error_handler(self.error_handler)

            # --- Subscription Management ---
            application.add_handler(CommandHandler("subscribe", self.subscribe_command))
            application.add_handler(CommandHandler("mysubscriptions", self.mysubscriptions_command))
            application.add_handler(CallbackQueryHandler(self.subscribe_callback_handler, pattern="^sub_"))

            # --- Order Tracking ---
            application.add_handler(CommandHandler("track", self.track_command))
            application.add_handler(CallbackQueryHandler(self.track_callback_handler, pattern="^track_"))

            # --- Loyalty & Analytics ---
            application.add_handler(CommandHandler("loyalty", self.loyalty_command))
            application.add_handler(CommandHandler("loyalty_history", self.loyalty_history_command))

            # --- Admin Features ---
            application.add_handler(CommandHandler("admin_orders", self.admin_orders_command))
            application.add_handler(CommandHandler("admin_stats", self.admin_stats_command))
            
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
        if query.data.startswith("track_"):
            order_id = query.data.replace("track_", "")
            tracking = await self.delivery_service.get_delivery_tracking(order_id)
            events = tracking.get('events', [])
            events_text = "\n".join([
                f"{e['time']}: {e['type']} - {e['description']}" for e in events
            ])
            await query.edit_message_text(
                f"Order {order_id}\nStatus: {tracking.get('status')}\nAddress: {tracking.get('address')}\nSlot: {tracking.get('slot')}\n\nEvents:\n{events_text or 'No events.'}"
            )

    # --- Loyalty & Analytics ---
    async def loyalty_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
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
        if update.message:
            await update.message.reply_text(text)
        elif update.callback_query:
            await update.callback_query.edit_message_text(text)

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
        if not await self.admin_service.is_admin(user['telegram_id']):
            if update.message:
                await update.message.reply_text(self.get_text('not_admin', lang))
            elif update.callback_query:
                await update.callback_query.edit_message_text(self.get_text('not_admin', lang))
            return
        orders = await self.admin_service.get_pending_orders()
        if not orders:
            if update.message:
                await update.message.reply_text(self.get_text('no_pending_orders', lang))
            elif update.callback_query:
                await update.callback_query.edit_message_text(self.get_text('no_pending_orders', lang))
            return
        text = "\n".join([
            f"Order {o['order_number']} by {o['username']} ({o['phone']}) - {o['status']}" for o in orders
        ])
        if update.message:
            await update.message.reply_text(self.get_text('pending_orders', lang) + "\n" + text)
        elif update.callback_query:
            await update.callback_query.edit_message_text(self.get_text('pending_orders', lang) + "\n" + text)

    async def admin_stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = await self.user_service.get_or_create_user(update.effective_user)
        lang = user.get('language_code', 'en')
        if not await self.admin_service.is_admin(user['telegram_id']):
            if update.message:
                await update.message.reply_text(self.get_text('not_admin', lang))
            elif update.callback_query:
                await update.callback_query.edit_message_text(self.get_text('not_admin', lang))
            return
        stats = await self.admin_service.get_system_stats()
        text = (
            f"👥 Total Users: {stats.get('total_users', 0)}\n"
            f"📦 Today's Orders: {stats.get('today_orders', 0)}\n"
            f"⏳ Pending Orders: {stats.get('pending_orders', 0)}\n"
            f"💰 Today's Revenue: {stats.get('today_revenue', 0)} UZS"
        )
        if update.message:
            await update.message.reply_text(text)
        elif update.callback_query:
            await update.callback_query.edit_message_text(text)

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