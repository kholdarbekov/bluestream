"""
Database-backed multi-language translation utilities for the Water Business Platform
Supports: English (en), Uzbek (uz), Russian (ru)
"""
from typing import Dict, Any, Optional
from flask import current_app, g
from functools import lru_cache
import redis
import json
from business_app.models.translation import Translation
from business_app import db


class TranslationService:
    """Service for managing database-backed translations"""
    
    def __init__(self):
        self.redis_client = None
        self.cache_timeout = 3600  # 1 hour
        self.cache_prefix = "translations"
    
    def _get_redis_client(self):
        """Get Redis client for caching"""
        if not self.redis_client:
            try:
                self.redis_client = redis.from_url(current_app.config['REDIS_URL'])
            except:
                self.redis_client = None
        return self.redis_client
    
    def get_translation(self, key: str, language: str = 'en', **kwargs) -> str:
        """
        Get translation for a key in specified language
        
        Args:
            key: Translation key
            language: Language code (en, uz, ru)
            **kwargs: Variables for string formatting
        
        Returns:
            Translated string
        """
        # Try cache first
        cached_translation = self._get_cached_translation(key, language)
        if cached_translation:
            return self._format_translation(cached_translation, **kwargs)
        
        # Get from database
        translation = self._get_db_translation(key, language)
        if translation:
            # Cache the translation
            self._cache_translation(key, language, translation)
            return self._format_translation(translation, **kwargs)
        
        # Fallback to English if not found
        if language != 'en':
            en_translation = self._get_db_translation(key, 'en')
            if en_translation:
                return self._format_translation(en_translation, **kwargs)
        
        # If no database translation found, check if we have a hardcoded translation
        hardcoded_translation = self._get_hardcoded_translation(key, language)
        if hardcoded_translation:
            return self._format_translation(hardcoded_translation, **kwargs)
        
        # Return key if no translation found (this maintains existing behavior)
        return key
    
    def _get_cached_translation(self, key: str, language: str) -> Optional[str]:
        """Get translation from cache"""
        redis_client = self._get_redis_client()
        if not redis_client:
            return None
        
        try:
            cache_key = f"{self.cache_prefix}:{language}:{key}"
            cached = redis_client.get(cache_key)
            return cached.decode('utf-8') if cached else None
        except:
            return None
    
    def _cache_translation(self, key: str, language: str, translation: str):
        """Cache translation"""
        redis_client = self._get_redis_client()
        if not redis_client:
            return
        
        try:
            cache_key = f"{self.cache_prefix}:{language}:{key}"
            redis_client.setex(cache_key, self.cache_timeout, translation)
        except:
            pass
    
    def _get_db_translation(self, key: str, language: str) -> Optional[str]:
        """Get translation from database"""
        try:
            translation = Translation.query.filter_by(
                key=key,
                language=language,
                is_active=True
            ).first()
            
            return translation.value if translation else None
        except Exception as e:
            current_app.logger.error(f"Error getting translation: {e}")
            return None
    
    def _format_translation(self, translation: str, **kwargs) -> str:
        """Format translation with variables"""
        try:
            return translation.format(**kwargs)
        except (KeyError, ValueError):
            return translation
    
    def _get_hardcoded_translation(self, key: str, language: str) -> Optional[str]:
        """Get translation from hardcoded DEFAULT_TRANSLATIONS"""
        try:
            return DEFAULT_TRANSLATIONS.get(language, {}).get(key)
        except:
            return None
    
    def set_translation(self, key: str, language: str, value: str, 
                       category: str = 'general', description: str = None) -> bool:
        """
        Set/update translation in database
        
        Args:
            key: Translation key
            language: Language code
            value: Translation value
            category: Translation category
            description: Optional description
        
        Returns:
            Success status
        """
        try:
            # Check if translation exists
            translation = Translation.query.filter_by(
                key=key,
                language=language
            ).first()
            
            if translation:
                # Update existing
                translation.value = value
                translation.category = category
                translation.description = description or translation.description
                translation.is_active = True
            else:
                # Create new
                translation = Translation(
                    key=key,
                    language=language,
                    value=value,
                    category=category,
                    description=description,
                    is_active=True
                )
                db.session.add(translation)
            
            db.session.commit()
            
            # Clear cache
            self._clear_translation_cache(key, language)
            
            return True
        except Exception as e:
            current_app.logger.error(f"Error setting translation: {e}")
            return False
    
    def _clear_translation_cache(self, key: str, language: str):
        """Clear cached translation"""
        redis_client = self._get_redis_client()
        if redis_client:
            try:
                cache_key = f"{self.cache_prefix}:{language}:{key}"
                redis_client.delete(cache_key)
            except:
                pass
    
    def get_all_translations(self, language: str) -> Dict[str, str]:
        """Get all translations for a language"""
        try:
            translations = Translation.query.filter_by(
                language=language,
                is_active=True
            ).all()
            
            return {t.key: t.value for t in translations}
        except Exception as e:
            current_app.logger.error(f"Error getting all translations: {e}")
            return {}
    
    def import_translations(self, translations_data: Dict[str, Dict[str, str]], 
                          category: str = 'general') -> bool:
        """
        Import translations from dictionary
        
        Args:
            translations_data: Dict with structure {language: {key: value}}
            category: Translation category
        
        Returns:
            Success status
        """
        try:
            for language, translations in translations_data.items():
                for key, value in translations.items():
                    # Check if translation exists
                    translation = Translation.query.filter_by(
                        key=key,
                        language=language
                    ).first()
                    
                    if translation:
                        translation.value = value
                        translation.category = category
                        translation.is_active = True
                    else:
                        translation = Translation(
                            key=key,
                            language=language,
                            value=value,
                            category=category,
                            is_active=True
                        )
                        db.session.add(translation)
            
            db.session.commit()
            return True
        except Exception as e:
            current_app.logger.error(f"Error importing translations: {e}")
            return False
    
    def export_translations(self, language: str = None) -> Dict[str, Any]:
        """Export translations to dictionary"""
        try:
            query = Translation.query.filter_by(is_active=True)
            if language:
                query = query.filter_by(language=language)
            
            translations = query.all()
            
            result = {}
            for translation in translations:
                if translation.language not in result:
                    result[translation.language] = {}
                result[translation.language][translation.key] = translation.value
            
            return result
        except Exception as e:
            current_app.logger.error(f"Error exporting translations: {e}")
            return {}
    
    def clear_cache(self, language: str = None):
        """Clear translation cache"""
        redis_client = self._get_redis_client()
        if not redis_client:
            return
        
        try:
            if language:
                # Clear specific language cache
                pattern = f"{self.cache_prefix}:{language}:*"
            else:
                # Clear all translation cache
                pattern = f"{self.cache_prefix}:*"
            
            keys = redis_client.keys(pattern)
            if keys:
                redis_client.delete(*keys)
        except:
            pass


# Global translation service instance
translation_service = TranslationService()


def get_translation(key: str, language: str = None, **kwargs) -> str:
    """
    Get translation for a key
    
    Args:
        key: Translation key
        language: Language code (defaults to current language)
        **kwargs: Variables for string formatting
    
    Returns:
        Translated string
    """
    if language is None:
        language = getattr(g, 'language', current_app.config.get('DEFAULT_LANGUAGE', 'en'))
    
    return translation_service.get_translation(key, language, **kwargs)


def translate(key: str, language: str = None, **kwargs) -> str:
    """Alias for get_translation"""
    return get_translation(key, language, **kwargs)


def set_translation(key: str, language: str, value: str, 
                   category: str = 'general', description: str = None) -> bool:
    """Set translation in database"""
    return translation_service.set_translation(key, language, value, category, description)


def import_translations_from_dict(translations_data: Dict[str, Dict[str, str]], 
                                 category: str = 'general') -> bool:
    """Import translations from dictionary"""
    return translation_service.import_translations(translations_data, category)


# Default translations for seeding
DEFAULT_TRANSLATIONS = {
    'en': {
        # Common
        'welcome': 'Welcome',
        'hello': 'Hello',
        'goodbye': 'Goodbye',
        'thank_you': 'Thank you',
        'yes': 'Yes',
        'no': 'No',
        'ok': 'OK',
        'cancel': 'Cancel',
        'save': 'Save',
        'delete': 'Delete',
        'edit': 'Edit',
        'submit': 'Submit',
        'loading': 'Loading...',
        'error': 'Error',
        'success': 'Success',
        
        # Company
        'company_name': 'AquaPure Water Delivery',
        'product': 'Product',
        'products': 'Products',
        'water': 'Water',
        'price': 'Price',
        'quantity': 'Quantity',
        'total': 'Total',
        
        # Orders
        'order': 'Order',
        'orders': 'Orders',
        'place_order': 'Place Order',
        'order_confirmed': 'Order Confirmed',
        'add_to_cart': 'Add to Cart',
        'cart': 'Cart',
        'checkout': 'Checkout',
        
        # Delivery
        'delivery': 'Delivery',
        'delivery_address': 'Delivery Address',
        'delivery_time': 'Delivery Time',
        'free_delivery': 'Free Delivery',
        'track_order': 'Track Order',
        
        # Payment
        'payment': 'Payment',
        'pay_now': 'Pay Now',
        'cash_payment': 'Cash Payment',
        'card_payment': 'Card Payment',
        
        # User
        'login': 'Login',
        'logout': 'Logout',
        'register': 'Register',
        'profile': 'Profile',
        'email': 'Email',
        'password': 'Password',
        'phone': 'Phone Number',
        'address': 'Address',
        
        # Navigation
        'Home': 'Home',
        'Shop': 'Shop',
        'Services': 'Services',
        'About Us': 'About Us',
        'Contact': 'Contact',
        'Gallery': 'Gallery',
        'Pages': 'Pages',
        'Subscriptions': 'Subscriptions',
        
        # Cart & Shopping
        'Shopping Cart': 'Shopping Cart',
        'Your cart is empty': 'Your cart is empty',
        'Add some products to your cart to see them here': 'Add some products to your cart to see them here',
        'Continue Shopping': 'Continue Shopping',
        'Have a Coupon?': 'Have a Coupon?',
        'Enter coupon code': 'Enter coupon code',
        'Apply Coupon': 'Apply Coupon',
        'Cart Total': 'Cart Total',
        'Subtotal': 'Subtotal',
        'Discount': 'Discount',
        'Free': 'Free',
        'Proceed to Checkout': 'Proceed to Checkout',
        
        # Account Pages
        'My Account': 'My Account',
        'Dashboard': 'Dashboard',
        'Profile Settings': 'Profile Settings',
        'My Orders': 'My Orders',
        'Addresses': 'Addresses',
        'Security': 'Security',
        'My Addresses': 'My Addresses',
        'Account Security': 'Account Security',
        
        # Profile & Security
        'Personal Information': 'Personal Information',
        'Contact Information': 'Contact Information',
        'First Name': 'First Name',
        'Last Name': 'Last Name',
        'Date of Birth': 'Date of Birth',
        'Gender': 'Gender',
        'Male': 'Male',
        'Female': 'Female',
        'Select Gender': 'Select Gender',
        'Preferred Language': 'Preferred Language',
        'Phone Number': 'Phone Number',
        'Email Address': 'Email Address',
        'Verified': 'Verified',
        'Verify': 'Verify',
        'Account Status': 'Account Status',
        'Member Since': 'Member Since',
        'Last Login': 'Last Login',
        'Registration Source': 'Registration Source',
        'Active': 'Active',
        'Change Password': 'Change Password',
        'Current Password': 'Current Password',
        'New Password': 'New Password',
        'Confirm New Password': 'Confirm New Password',
        'Password strength': 'Password strength',
        'Weak': 'Weak',
        'Fair': 'Fair',
        'Good': 'Good',
        'Strong': 'Strong',
        'Very Strong': 'Very Strong',
        'Show passwords': 'Show passwords',
        'Two-Factor Authentication': 'Two-Factor Authentication',
        'SMS Authentication': 'SMS Authentication',
        'Enable 2FA': 'Enable 2FA',
        'Verify Phone First': 'Verify Phone First',
        'Recent Account Activity': 'Recent Account Activity',
        'Security Settings': 'Security Settings',
        
        # Address Management
        'Add New Address': 'Add New Address',
        'Add Your First Address': 'Add Your First Address',
        'No addresses saved yet': 'No addresses saved yet',
        'Add your delivery addresses to make ordering easier': 'Add your delivery addresses to make ordering easier',
        'Edit Address': 'Edit Address',
        'Address Title': 'Address Title',
        'Full Address': 'Full Address',
        'Street Address': 'Street Address',
        'City': 'City',
        'District': 'District',
        'Postal Code': 'Postal code',
        'Apartment/Floor': 'Apartment/Floor',
        'Landmark': 'Landmark',
        'Delivery Instructions': 'Delivery Instructions',
        'Set as default address': 'Set as default address',
        'This is a business address': 'This is a business address',
        'Default': 'Default',
        'Business': 'Business',
        'Edit': 'Edit',
        'Set Default': 'Set Default',
        'Delete': 'Delete',
        'Instructions': 'Instructions',
        
        # Common Actions
        'Save Changes': 'Save Changes',
        'Update Contact Info': 'Update Contact Info',
        'Save Preferences': 'Save Preferences',
        'Save Security Settings': 'Save Security Settings',
        'Save Address': 'Save Address',
        'Cancel': 'Cancel',
        
        # Messages & Notifications
        'Preferences': 'Preferences',
        'Receive email notifications about orders': 'Receive email notifications about orders',
        'Receive SMS notifications about deliveries': 'Receive SMS notifications about deliveries',
        'Receive promotional emails and offers': 'Receive promotional emails and offers',
        'Send email notifications for new logins': 'Send email notifications for new logins',
        'Send email notifications for password changes': 'Send email notifications for password changes',
        
        # Search & UI
        'Search products...': 'Search products...',
    },
    'uz': {
        # Common
        'welcome': 'Xush kelibsiz',
        'hello': 'Salom',
        'goodbye': 'Ko\'rishguncha',
        'thank_you': 'Rahmat',
        'yes': 'Ha',
        'no': 'Yo\'q',
        'ok': 'OK',
        'cancel': 'Bekor qilish',
        'save': 'Saqlash',
        'delete': 'O\'chirish',
        'edit': 'Tahrirlash',
        'submit': 'Jo\'natish',
        'loading': 'Yuklanmoqda...',
        'error': 'Xatolik',
        'success': 'Muvaffaqiyat',
        
        # Company
        'company_name': 'AquaPure Suv Yetkazib Berish',
        'product': 'Mahsulot',
        'products': 'Mahsulotlar',
        'water': 'Suv',
        'price': 'Narx',
        'quantity': 'Miqdor',
        'total': 'Jami',
        
        # Orders
        'order': 'Buyurtma',
        'orders': 'Buyurtmalar',
        'place_order': 'Buyurtma berish',
        'order_confirmed': 'Buyurtma tasdiqlandi',
        'add_to_cart': 'Savatga qo\'shish',
        'cart': 'Savat',
        'checkout': 'To\'lov',
        
        # Delivery
        'delivery': 'Yetkazib berish',
        'delivery_address': 'Yetkazib berish manzili',
        'delivery_time': 'Yetkazib berish vaqti',
        'free_delivery': 'Bepul yetkazib berish',
        'track_order': 'Buyurtmani kuzatish',
        
        # Payment
        'payment': 'To\'lov',
        'pay_now': 'Hozir to\'lash',
        'cash_payment': 'Naqd to\'lov',
        'card_payment': 'Karta orqali to\'lov',
        
        # User
        'login': 'Kirish',
        'logout': 'Chiqish',
        'register': 'Ro\'yxatdan o\'tish',
        'profile': 'Profil',
        'email': 'Email',
        'password': 'Parol',
        'phone': 'Telefon raqam',
        'address': 'Manzil',
        
        # Navigation
        'Home': 'Bosh sahifa',
        'Shop': 'Do\'kon',
        'Services': 'Xizmatlar',
        'About Us': 'Biz haqimizda',
        'Contact': 'Aloqa',
        'Gallery': 'Galereya',
        'Pages': 'Sahifalar',
        'Subscriptions': 'Obunalar',
        
        # Cart & Shopping
        'Shopping Cart': 'Savat',
        'Your cart is empty': 'Savatingiz bo\'sh',
        'Add some products to your cart to see them here': 'Bu yerda ko\'rish uchun savatga mahsulotlar qo\'shing',
        'Continue Shopping': 'Xarid qilishni davom ettirish',
        'Have a Coupon?': 'Kuponingiz bormi?',
        'Enter coupon code': 'Kupon kodini kiriting',
        'Apply Coupon': 'Kuponni qo\'llash',
        'Cart Total': 'Savat jami',
        'Subtotal': 'Oraliq jami',
        'Discount': 'Chegirma',
        'Free': 'Bepul',
        'Proceed to Checkout': 'To\'lovga o\'tish',
        
        # Account Pages
        'My Account': 'Mening hisobim',
        'Dashboard': 'Boshqaruv paneli',
        'Profile Settings': 'Profil sozlamalari',
        'My Orders': 'Mening buyurtmalarim',
        'Addresses': 'Manzillar',
        'Security': 'Xavfsizlik',
        'My Addresses': 'Mening manzillarim',
        'Account Security': 'Hisob xavfsizligi',
        
        # Profile & Security
        'Personal Information': 'Shaxsiy ma\'lumotlar',
        'Contact Information': 'Aloqa ma\'lumotlari',
        'First Name': 'Ism',
        'Last Name': 'Familiya',
        'Date of Birth': 'Tug\'ilgan sana',
        'Gender': 'Jins',
        'Male': 'Erkak',
        'Female': 'Ayol',
        'Select Gender': 'Jinsni tanlang',
        'Preferred Language': 'Afzal qilingan til',
        'Phone Number': 'Telefon raqam',
        'Email Address': 'Email manzil',
        'Verified': 'Tasdiqlangan',
        'Verify': 'Tasdiqlash',
        'Account Status': 'Hisob holati',
        'Member Since': 'A\'zo bo\'lgan vaqt',
        'Last Login': 'Oxirgi kirish',
        'Registration Source': 'Ro\'yxatdan o\'tish manbai',
        'Active': 'Faol',
        'Change Password': 'Parolni o\'zgartirish',
        'Current Password': 'Joriy parol',
        'New Password': 'Yangi parol',
        'Confirm New Password': 'Yangi parolni tasdiqlash',
        'Password strength': 'Parol mustahkamligi',
        'Weak': 'Zaif',
        'Fair': 'O\'rtacha',
        'Good': 'Yaxshi',
        'Strong': 'Mustahkam',
        'Very Strong': 'Juda mustahkam',
        'Show passwords': 'Parollarni ko\'rsatish',
        'Two-Factor Authentication': 'Ikki bosqichli autentifikatsiya',
        'SMS Authentication': 'SMS orqali autentifikatsiya',
        'Enable 2FA': '2FA ni yoqish',
        'Verify Phone First': 'Avval telefonni tasdiqlang',
        'Recent Account Activity': 'So\'nggi hisob faolligi',
        'Security Settings': 'Xavfsizlik sozlamalari',
        
        # Address Management
        'Add New Address': 'Yangi manzil qo\'shish',
        'Add Your First Address': 'Birinchi manzilingizni qo\'shing',
        'No addresses saved yet': 'Hali manzillar saqlanmagan',
        'Add your delivery addresses to make ordering easier': 'Buyurtma berishni osonlashtirish uchun yetkazib berish manzillarini qo\'shing',
        'Edit Address': 'Manzilni tahrirlash',
        'Address Title': 'Manzil nomi',
        'Full Address': 'To\'liq manzil',
        'Street Address': 'Ko\'cha manzili',
        'City': 'Shahar',
        'District': 'Tuman',
        'Postal Code': 'Pochta indeksi',
        'Apartment/Floor': 'Kvartira/Qavat',
        'Landmark': 'Mo\'ljal',
        'Delivery Instructions': 'Yetkazib berish ko\'rsatmalari',
        'Set as default address': 'Asosiy manzil sifatida belgilash',
        'This is a business address': 'Bu biznes manzil',
        'Default': 'Asosiy',
        'Business': 'Biznes',
        'Edit': 'Tahrirlash',
        'Set Default': 'Asosiy qilish',
        'Delete': 'O\'chirish',
        'Instructions': 'Ko\'rsatmalar',
        
        # Common Actions
        'Save Changes': 'O\'zgarishlarni saqlash',
        'Update Contact Info': 'Aloqa ma\'lumotlarini yangilash',
        'Save Preferences': 'Afzalliklarni saqlash',
        'Save Security Settings': 'Xavfsizlik sozlamalarini saqlash',
        'Save Address': 'Manzilni saqlash',
        'Cancel': 'Bekor qilish',
        
        # Messages & Notifications
        'Preferences': 'Afzalliklar',
        'Receive email notifications about orders': 'Buyurtmalar haqida email xabarnomalar olish',
        'Receive SMS notifications about deliveries': 'Yetkazib berish haqida SMS xabarnomalar olish',
        'Receive promotional emails and offers': 'Reklama emaillar va takliflar olish',
        'Send email notifications for new logins': 'Yangi kirishlar uchun email xabarnomalar yuborish',
        'Send email notifications for password changes': 'Parol o\'zgarishlari uchun email xabarnomalar yuborish',
        
        # Search & UI
        'Search products...': 'Mahsulotlarni qidirish...',
    },
    'ru': {
        # Common
        'welcome': 'Добро пожаловать',
        'hello': 'Привет',
        'goodbye': 'До свидания',
        'thank_you': 'Спасибо',
        'yes': 'Да',
        'no': 'Нет',
        'ok': 'OK',
        'cancel': 'Отмена',
        'save': 'Сохранить',
        'delete': 'Удалить',
        'edit': 'Редактировать',
        'submit': 'Отправить',
        'loading': 'Загрузка...',
        'error': 'Ошибка',
        'success': 'Успех',
        
        # Company
        'company_name': 'AquaPure Доставка Воды',
        'product': 'Продукт',
        'products': 'Продукты',
        'water': 'Вода',
        'price': 'Цена',
        'quantity': 'Количество',
        'total': 'Итого',
        
        # Orders
        'order': 'Заказ',
        'orders': 'Заказы',
        'place_order': 'Сделать заказ',
        'order_confirmed': 'Заказ подтвержден',
        'add_to_cart': 'Добавить в корзину',
        'cart': 'Корзина',
        'checkout': 'Оформить заказ',
        
        # Delivery
        'delivery': 'Доставка',
        'delivery_address': 'Адрес доставки',
        'delivery_time': 'Время доставки',
        'free_delivery': 'Бесплатная доставка',
        'track_order': 'Отследить заказ',
        
        # Payment
        'payment': 'Оплата',
        'pay_now': 'Оплатить сейчас',
        'cash_payment': 'Наличными',
        'card_payment': 'Картой',
        
        # User
        'login': 'Войти',
        'logout': 'Выйти',
        'register': 'Регистрация',
        'profile': 'Профиль',
        'email': 'Email',
        'password': 'Пароль',
        'phone': 'Номер телефона',
        'address': 'Адрес',
        
        # Navigation
        'Home': 'Главная',
        'Shop': 'Магазин',
        'Services': 'Услуги',
        'About Us': 'О нас',
        'Contact': 'Контакты',
        'Gallery': 'Галерея',
        'Pages': 'Страницы',
        'Subscriptions': 'Подписки',
        
        # Cart & Shopping
        'Shopping Cart': 'Корзина покупок',
        'Your cart is empty': 'Ваша корзина пуста',
        'Add some products to your cart to see them here': 'Добавьте товары в корзину, чтобы увидеть их здесь',
        'Continue Shopping': 'Продолжить покупки',
        'Have a Coupon?': 'Есть купон?',
        'Enter coupon code': 'Введите код купона',
        'Apply Coupon': 'Применить купон',
        'Cart Total': 'Итого в корзине',
        'Subtotal': 'Промежуточная сумма',
        'Discount': 'Скидка',
        'Free': 'Бесплатно',
        'Proceed to Checkout': 'Перейти к оплате',
        
        # Account Pages
        'My Account': 'Мой аккаунт',
        'Dashboard': 'Панель управления',
        'Profile Settings': 'Настройки профиля',
        'My Orders': 'Мои заказы',
        'Addresses': 'Адреса',
        'Security': 'Безопасность',
        'My Addresses': 'Мои адреса',
        'Account Security': 'Безопасность аккаунта',
        
        # Profile & Security
        'Personal Information': 'Личная информация',
        'Contact Information': 'Контактная информация',
        'First Name': 'Имя',
        'Last Name': 'Фамилия',
        'Date of Birth': 'Дата рождения',
        'Gender': 'Пол',
        'Male': 'Мужской',
        'Female': 'Женский',
        'Select Gender': 'Выберите пол',
        'Preferred Language': 'Предпочитаемый язык',
        'Phone Number': 'Номер телефона',
        'Email Address': 'Email адрес',
        'Verified': 'Подтвержден',
        'Verify': 'Подтвердить',
        'Account Status': 'Статус аккаунта',
        'Member Since': 'Участник с',
        'Last Login': 'Последний вход',
        'Registration Source': 'Источник регистрации',
        'Active': 'Активный',
        'Change Password': 'Изменить пароль',
        'Current Password': 'Текущий пароль',
        'New Password': 'Новый пароль',
        'Confirm New Password': 'Подтвердите новый пароль',
        'Password strength': 'Сложность пароля',
        'Weak': 'Слабый',
        'Fair': 'Средний',
        'Good': 'Хороший',
        'Strong': 'Сильный',
        'Very Strong': 'Очень сильный',
        'Show passwords': 'Показать пароли',
        'Two-Factor Authentication': 'Двухфакторная аутентификация',
        'SMS Authentication': 'SMS аутентификация',
        'Enable 2FA': 'Включить 2FA',
        'Verify Phone First': 'Сначала подтвердите телефон',
        'Recent Account Activity': 'Недавняя активность аккаунта',
        'Security Settings': 'Настройки безопасности',
        
        # Address Management
        'Add New Address': 'Добавить новый адрес',
        'Add Your First Address': 'Добавьте ваш первый адрес',
        'No addresses saved yet': 'Адреса еще не сохранены',
        'Add your delivery addresses to make ordering easier': 'Добавьте адреса доставки для удобства заказов',
        'Edit Address': 'Редактировать адрес',
        'Address Title': 'Название адреса',
        'Full Address': 'Полный адрес',
        'Street Address': 'Уличный адрес',
        'City': 'Город',
        'District': 'Район',
        'Postal Code': 'Почтовый индекс',
        'Apartment/Floor': 'Квартира/Этаж',
        'Landmark': 'Ориентир',
        'Delivery Instructions': 'Инструкции по доставке',
        'Set as default address': 'Установить как адрес по умолчанию',
        'This is a business address': 'Это бизнес-адрес',
        'Default': 'По умолчанию',
        'Business': 'Бизнес',
        'Edit': 'Редактировать',
        'Set Default': 'Установить по умолчанию',
        'Delete': 'Удалить',
        'Instructions': 'Инструкции',
        
        # Common Actions
        'Save Changes': 'Сохранить изменения',
        'Update Contact Info': 'Обновить контактную информацию',
        'Save Preferences': 'Сохранить предпочтения',
        'Save Security Settings': 'Сохранить настройки безопасности',
        'Save Address': 'Сохранить адрес',
        'Cancel': 'Отмена',
        
        # Messages & Notifications
        'Preferences': 'Предпочтения',
        'Receive email notifications about orders': 'Получать email уведомления о заказах',
        'Receive SMS notifications about deliveries': 'Получать SMS уведомления о доставке',
        'Receive promotional emails and offers': 'Получать рекламные emails и предложения',
        'Send email notifications for new logins': 'Отправлять email уведомления о новых входах',
        'Send email notifications for password changes': 'Отправлять email уведомления об изменении пароля',
        
        # Search & UI
        'Search products...': 'Поиск товаров...',
    }
}


def seed_default_translations():
    """Seed database with default translations"""
    return import_translations_from_dict(DEFAULT_TRANSLATIONS, 'default')