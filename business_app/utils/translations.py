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
        
        # Return key if no translation found
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
    }
}


def seed_default_translations():
    """Seed database with default translations"""
    return import_translations_from_dict(DEFAULT_TRANSLATIONS, 'default')