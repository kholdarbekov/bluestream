"""
Internationalization (i18n) support for the Telegram Bot
Multi-language support with translation management
"""
import json
import logging
from typing import Dict, Any, Optional, List
from pathlib import Path

from config import config
from database import db_manager

logger = logging.getLogger(__name__)


class Translation:
    """Translation management system"""
    
    def __init__(self):
        self.translations: Dict[str, Dict[str, str]] = {}
        self.fallback_language = config.localization.fallback_language
        self.supported_languages = config.localization.supported_languages
        
    async def load_translations(self):
        """Load translations from database"""
        try:
            query = """
            SELECT language, key, value 
            FROM translations 
            WHERE is_active = TRUE AND category = 'telegram'
            ORDER BY language, key
            """
            
            rows = await db_manager.fetchall(query)
            
            # Organize translations by language
            for row in rows:
                language = row['language']
                key = row['key']
                value = row['value']
                
                if language not in self.translations:
                    self.translations[language] = {}
                
                self.translations[language][key] = value
            
            logger.info(f"Loaded translations for languages: {list(self.translations.keys())}")
            
            # Load default translations if database is empty
            if not self.translations:
                await self._load_default_translations()
                
        except Exception as e:
            logger.error(f"Failed to load translations from database: {e}")
            await self._load_default_translations()
    
    async def _load_default_translations(self):
        """Load default hardcoded translations"""
        default_translations = {
            'en': {
                # Main menu
                'welcome': '🌊 Welcome to BlueStream Water Solutions!\n\nI can help you with:\n• Ordering water\n• Tracking deliveries\n• Managing subscriptions\n• Account management',
                'main_menu': '🏠 Main Menu',
                'back': '⬅️ Back',
                'cancel': '❌ Cancel',
                'yes': '✅ Yes',
                'no': '❌ No',
                
                # Navigation
                'menu_products': '🛒 Order Water',
                'menu_orders': '📦 My Orders',
                'menu_subscriptions': '🔄 Subscriptions',
                'menu_profile': '👤 My Profile',
                'menu_loyalty': '🏆 Loyalty Points',
                'menu_support': '🆘 Support',
                'menu_language': '🌐 Language',
                
                # Products
                'products_title': '🛒 Water Products',
                'products_category': 'Select category:',
                'product_details': '📋 Product Details',
                'add_to_cart': '🛒 Add to Cart',
                'quantity': 'Quantity:',
                'price': 'Price:',
                'total': 'Total:',
                
                # Cart & Orders
                'cart_empty': '🛒 Your cart is empty',
                'cart_title': '🛒 Shopping Cart',
                'checkout': '💳 Checkout',
                'order_placed': '✅ Order placed successfully!',
                'order_number': 'Order number: {}',
                'delivery_address': '📍 Delivery Address',
                'payment_method': '💳 Payment Method',
                'order_total': 'Total: {} UZS',
                
                # Profile
                'profile_title': '👤 My Profile',
                'profile_name': 'Name: {}',
                'profile_phone': 'Phone: {}',
                'profile_email': 'Email: {}',
                'profile_language': 'Language: {}',
                'edit_profile': '✏️ Edit Profile',
                
                # Loyalty
                'loyalty_title': '🏆 Loyalty Points',
                'loyalty_balance': 'Your balance: {} points',
                'loyalty_history': '📊 Points History',
                'referral_code': 'Your referral code: {}',
                'loyalty_rewards': '🎁 Available Rewards',
                
                # Support
                'support_title': '🆘 Customer Support',
                'contact_support': '📞 Contact Support',
                'faq': '❓ Frequently Asked Questions',
                'business_hours': '🕐 Business Hours: 9:00 AM - 9:00 PM',
                
                # Common messages
                'error_occurred': '❌ An error occurred. Please try again.',
                'invalid_input': '❌ Invalid input. Please try again.',
                'action_cancelled': '❌ Action cancelled.',
                'please_wait': '⏳ Please wait...',
                'success': '✅ Success!',
                
                # Registration
                'registration_welcome': '👋 Welcome! Let\'s set up your account.',
                'enter_phone': '📱 Please share your phone number:',
                'phone_shared': '✅ Phone number received!',
                'enter_name': '👤 Please enter your full name:',
                'registration_complete': '🎉 Registration complete! Welcome to BlueStream!',
                
                # Payments
                'payment_cash': '💵 Cash on Delivery',
                'payment_card': '💳 Bank Card',
                'payment_payme': '📱 Payme',
                'payment_click': '💙 Click',
                'payment_loyalty': '🏆 Loyalty Points',
                'payment_processing': '⏳ Processing payment...',
                'payment_success': '✅ Payment successful!',
                'payment_failed': '❌ Payment failed. Please try again.',
                
                # Delivery
                'delivery_standard': '🚚 Standard Delivery (Free)',
                'delivery_express': '⚡ Express Delivery (+5000 UZS)',
                'delivery_scheduled': '📅 Scheduled Delivery',
                'select_time_slot': '⏰ Select delivery time:',
                'delivery_address_current': '📍 Current address: {}',
                'delivery_address_change': '📍 Change address',
                'share_location': '📍 Share location',
                
                # Subscriptions
                'subscription_title': '🔄 Subscriptions',
                'subscription_create': '➕ Create Subscription',
                'subscription_frequency': '📅 Delivery frequency:',
                'frequency_daily': '📆 Daily',
                'frequency_weekly': '📅 Weekly',
                'frequency_biweekly': '📅 Bi-weekly',
                'frequency_monthly': '📅 Monthly',
                'subscription_active': '✅ Active subscriptions: {}',
                'subscription_paused': '⏸️ Paused subscriptions: {}',
                
                # Admin
                'admin_panel': '🔧 Admin Panel',
                'admin_orders': '📊 Orders Overview',
                'admin_analytics': '📈 Analytics',
                'admin_users': '👥 Users',
                'admin_products': '🛍️ Products',
            },
            
            'uz': {
                # Main menu
                'welcome': '🌊 BlueStream Suv Yechimlariga xush kelibsiz!\n\nMen sizga yordam bera olaman:\n• Suv buyurtma qilish\n• Yetkazib berishni kuzatish\n• Obunalarni boshqarish\n• Hisob boshqaruvi',
                'main_menu': '🏠 Asosiy menyu',
                'back': '⬅️ Orqaga',
                'cancel': '❌ Bekor qilish',
                'yes': '✅ Ha',
                'no': '❌ Yo\'q',
                
                # Navigation
                'menu_products': '🛒 Suv buyurtma qilish',
                'menu_orders': '📦 Buyurtmalarim',
                'menu_subscriptions': '🔄 Obunalar',
                'menu_profile': '👤 Profilim',
                'menu_loyalty': '🏆 Sodiqlik ballari',
                'menu_support': '🆘 Yordam',
                'menu_language': '🌐 Til',
                
                # Products
                'products_title': '🛒 Suv mahsulotlari',
                'products_category': 'Kategoriyani tanlang:',
                'product_details': '📋 Mahsulot tafsilotlari',
                'add_to_cart': '🛒 Savatchaga qo\'shish',
                'quantity': 'Miqdor:',
                'price': 'Narx:',
                'total': 'Jami:',
                
                # Cart & Orders
                'cart_empty': '🛒 Savatchangiz bo\'sh',
                'cart_title': '🛒 Xarid savatchasi',
                'checkout': '💳 To\'lov',
                'order_placed': '✅ Buyurtma muvaffaqiyatli berildi!',
                'order_number': 'Buyurtma raqami: {}',
                'delivery_address': '📍 Yetkazib berish manzili',
                'payment_method': '💳 To\'lov usuli',
                'order_total': 'Jami: {} so\'m',
                
                # Profile
                'profile_title': '👤 Profilim',
                'profile_name': 'Ism: {}',
                'profile_phone': 'Telefon: {}',
                'profile_email': 'Email: {}',
                'profile_language': 'Til: {}',
                'edit_profile': '✏️ Profilni tahrirlash',
                
                # Common messages
                'error_occurred': '❌ Xatolik yuz berdi. Qaytadan urinib ko\'ring.',
                'invalid_input': '❌ Noto\'g\'ri ma\'lumot. Qaytadan urinib ko\'ring.',
                'action_cancelled': '❌ Amal bekor qilindi.',
                'please_wait': '⏳ Iltimos kuting...',
                'success': '✅ Muvaffaqiyat!',
                
                # Registration
                'registration_welcome': '👋 Xush kelibsiz! Hisobingizni sozlaylik.',
                'enter_phone': '📱 Iltimos telefon raqamingizni ulashing:',
                'phone_shared': '✅ Telefon raqami qabul qilindi!',
                'enter_name': '👤 Iltimos to\'liq ismingizni kiriting:',
                'registration_complete': '🎉 Ro\'yxatdan o\'tish tugallandi! BlueStream\'ga xush kelibsiz!',
            },
            
            'ru': {
                # Main menu
                'welcome': '🌊 Добро пожаловать в BlueStream Water Solutions!\n\nЯ могу помочь вам с:\n• Заказом воды\n• Отслеживанием доставки\n• Управлением подписками\n• Управлением аккаунтом',
                'main_menu': '🏠 Главное меню',
                'back': '⬅️ Назад',
                'cancel': '❌ Отмена',
                'yes': '✅ Да',
                'no': '❌ Нет',
                
                # Navigation
                'menu_products': '🛒 Заказать воду',
                'menu_orders': '📦 Мои заказы',
                'menu_subscriptions': '🔄 Подписки',
                'menu_profile': '👤 Мой профиль',
                'menu_loyalty': '🏆 Баллы лояльности',
                'menu_support': '🆘 Поддержка',
                'menu_language': '🌐 Язык',
                
                # Products
                'products_title': '🛒 Продукты воды',
                'products_category': 'Выберите категорию:',
                'product_details': '📋 Детали продукта',
                'add_to_cart': '🛒 Добавить в корзину',
                'quantity': 'Количество:',
                'price': 'Цена:',
                'total': 'Итого:',
                
                # Cart & Orders
                'cart_empty': '🛒 Ваша корзина пуста',
                'cart_title': '🛒 Корзина покупок',
                'checkout': '💳 Оплата',
                'order_placed': '✅ Заказ успешно размещен!',
                'order_number': 'Номер заказа: {}',
                'delivery_address': '📍 Адрес доставки',
                'payment_method': '💳 Способ оплаты',
                'order_total': 'Итого: {} сум',
                
                # Profile
                'profile_title': '👤 Мой профиль',
                'profile_name': 'Имя: {}',
                'profile_phone': 'Телефон: {}',
                'profile_email': 'Email: {}',
                'profile_language': 'Язык: {}',
                'edit_profile': '✏️ Редактировать профиль',
                
                # Common messages
                'error_occurred': '❌ Произошла ошибка. Попробуйте снова.',
                'invalid_input': '❌ Неверный ввод. Попробуйте снова.',
                'action_cancelled': '❌ Действие отменено.',
                'please_wait': '⏳ Пожалуйста, подождите...',
                'success': '✅ Успешно!',
                
                # Registration
                'registration_welcome': '👋 Добро пожаловать! Давайте настроим ваш аккаунт.',
                'enter_phone': '📱 Пожалуйста, поделитесь вашим номером телефона:',
                'phone_shared': '✅ Номер телефона получен!',
                'enter_name': '👤 Пожалуйста, введите ваше полное имя:',
                'registration_complete': '🎉 Регистрация завершена! Добро пожаловать в BlueStream!',
            }
        }
        
        self.translations = default_translations
        logger.info("Loaded default translations")
    
    def get(self, key: str, language: str = None, *args, **kwargs) -> str:
        """Get translation for key in specified language"""
        if not language:
            language = config.localization.default_language
            
        # Try to get translation in requested language
        if language in self.translations and key in self.translations[language]:
            translation = self.translations[language][key]
        # Fallback to default language
        elif self.fallback_language in self.translations and key in self.translations[self.fallback_language]:
            translation = self.translations[self.fallback_language][key]
        # Return key if no translation found
        else:
            logger.warning(f"Translation not found for key '{key}' in language '{language}'")
            translation = key
        
        # Format with kwargs if provided
        if args or kwargs:
            try:
                translation = translation.format(*args, **kwargs)
            except (KeyError, ValueError) as e:
                logger.warning(f"Failed to format translation '{key}': {e}")
        
        return translation
    
    async def add_translation(self, language: str, key: str, value: str):
        """Add new translation to database"""
        query = """
        INSERT INTO translations (language, key, value, category, is_active)
        VALUES ($1, $2, $3, 'telegram', TRUE)
        ON CONFLICT (key, language) 
        DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
        """
        await db_manager.execute(query, language, key, value)
        
        # Update in-memory cache
        if language not in self.translations:
            self.translations[language] = {}
        self.translations[language][key] = value
    
    async def get_user_language(self, telegram_id: int) -> str:
        """Get user's preferred language"""
        query = """
        SELECT language_code FROM bot_users WHERE telegram_id = $1
        """
        language = await db_manager.fetchval(query, telegram_id)
        return language or config.localization.default_language
    
    def get_language_flag(self, language_code: str) -> str:
        """Get flag emoji for language"""
        flags = {
            'en': '🇺🇸',
            'uz': '🇺🇿',
            'ru': '🇷🇺'
        }
        return flags.get(language_code, '🌐')
    
    def get_language_name(self, language_code: str, display_language: str = None) -> str:
        """Get language name in specified display language"""
        if not display_language:
            display_language = language_code
            
        names = {
            'en': {'en': 'English', 'uz': 'Inglizcha', 'ru': 'Английский'},
            'uz': {'en': 'Uzbek', 'uz': 'O\'zbekcha', 'ru': 'Узбекский'},
            'ru': {'en': 'Russian', 'uz': 'Ruscha', 'ru': 'Русский'}
        }
        
        return names.get(language_code, {}).get(display_language, language_code)


# Global translation instance
i18n = Translation()