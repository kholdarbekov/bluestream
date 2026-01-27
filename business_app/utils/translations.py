"""
Database-backed multi-language translation utilities for the Water Business Platform
Supports: English (en), Uzbek (uz), Russian (ru)
"""
from typing import Dict, Any, Optional
from flask import current_app, g, request
from functools import lru_cache
import redis
import json
from business_app.models.translation import Translation
from business_app import db
import logging

logger = logging.getLogger(__name__)

class TranslationService:
    """Service for managing database-backed translations"""

    # Category-based cache timeouts (in seconds)
    CACHE_TIMEOUTS = {
        'landing': 259200,      # 3 days (rarely changes - marketing pages)
        'ui': 86400,            # 1 day (admin UI, moderate changes)
        'telegram': 3600,       # 1 hour (bot messages, frequent changes)
        'email': 86400,         # 1 day (email templates)
        'sms': 86400,           # 1 day (SMS templates)
        'general': 3600,        # 1 hour (default fallback)
    }

    def __init__(self):
        self.redis_client = None
        self.cache_timeout = 3600  # Default fallback (deprecated, use CACHE_TIMEOUTS)
        self.cache_prefix = "translations"
    
    def _get_redis_client(self):
        """Get Redis client for caching"""
        if not self.redis_client:
            try:
                self.redis_client = redis.from_url(current_app.config['REDIS_URL'])
            except:
                self.redis_client = None
        return self.redis_client

    def _get_cache_timeout(self, key: str) -> int:
        """
        Get cache timeout based on translation key category

        Translation keys should follow format: category.section.key
        Examples:
            'landing.hero.title' → 3 days (259200s)
            'ui.orders.title' → 1 day (86400s)
            'telegram.welcome' → 1 hour (3600s)
            'unknown.key' → 1 hour (3600s, default)

        Args:
            key: Translation key

        Returns:
            Cache timeout in seconds
        """
        # Extract category from key (first part before dot)
        category = key.split('.')[0] if '.' in key else 'general'
        return self.CACHE_TIMEOUTS.get(category, self.CACHE_TIMEOUTS['general'])
    
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

        # Fallback chain: uz → en → ru → return key
        fallback_languages = ['uz', 'en', 'ru']
        if language in fallback_languages:
            fallback_languages.remove(language)

        for fallback_lang in fallback_languages:
            fallback_translation = self._get_db_translation(key, fallback_lang)
            if fallback_translation:
                return self._format_translation(fallback_translation, **kwargs)

        # Return key with indicator if no translation found (helps identify missing translations)
        return key
    
    def _get_cached_translation(self, key: str, language: str) -> Optional[str]:
        """Get translation from cache"""
        redis_client = self._get_redis_client()
        if not redis_client:
            logger.info(f"[CACHE] Redis client not available for key='{key}', lang='{language}'")
            return None
        
        try:
            cache_key = f"{self.cache_prefix}:{language}:{key}"
            cached = redis_client.get(cache_key)
            result = cached.decode('utf-8') if cached else None
            return result
        except Exception as e:
            logger.error(f"[CACHE] ERROR: cache_key lookup failed: {e}")
            return None
    
    def _cache_translation(self, key: str, language: str, translation: str):
        """Cache translation with category-based TTL"""
        redis_client = self._get_redis_client()
        if not redis_client:
            return

        try:
            cache_key = f"{self.cache_prefix}:{language}:{key}"
            timeout = self._get_cache_timeout(key)  # Dynamic TTL based on category
            redis_client.setex(cache_key, timeout, translation)
        except Exception as e:
            # Log error but don't break functionality
            logger.error(f"Error caching translation '{key}': {e}")
            pass
    
    def _get_db_translation(self, key: str, language: str) -> Optional[str]:
        """Get translation from database"""
        try:
            if key.startswith('landing.'):
                logger.info(f"[DB] Querying: key='{key}', language='{language}'")
            
            translation = Translation.query.filter_by(
                key=key,
                language=language,
                is_active=True
            ).first()

            if key.startswith('landing.'):
                if translation:
                    preview = translation.value[:30] if len(translation.value) > 30 else translation.value
                    logger.info(f"[DB] FOUND: key='{key}', lang='{language}', value='{preview}...'")
                else:
                    logger.info(f"[DB] NOT FOUND: key='{key}', lang='{language}'")

            return translation.value if translation else None
        except Exception as e:
            # Handle database transaction errors by rolling back
            try:
                from business_app import db
                db.session.rollback()
            except:
                pass
            
            logger.error(f"[DB] ERROR: key='{key}', lang='{language}', error={e}")
            return None
    
    def _format_translation(self, translation: str, **kwargs) -> str:
        """Format translation with variables"""
        try:
            return translation.format(**kwargs)
        except (KeyError, ValueError):
            return translation
    
    def _get_hardcoded_translation(self, key: str, language: str) -> Optional[str]:
        """DEPRECATED: Hardcoded translations removed - all translations now database-driven"""
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

            # Clear cache for updated language
            self._clear_translation_cache(key, language)

            # Also clear cache for all other languages (in case of key changes)
            # This ensures consistency when translation keys are updated
            for lang in ['uz', 'en', 'ru']:
                if lang != language:
                    self._clear_translation_cache(key, lang)

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
    
    def warm_cache_for_category(self, category: str, languages: list = None) -> dict:
        """
        Pre-load all translations for a category into Redis cache
        Call this on app startup for frequently-accessed categories like 'landing'

        Args:
            category: Translation category to warm (e.g., 'landing', 'ui')
            languages: List of language codes to warm (default: ['uz', 'en', 'ru'])

        Returns:
            Dictionary with statistics about cache warming

        Example:
            # Warm landing page cache on startup
            translation_service.warm_cache_for_category('landing')
        """
        if languages is None:
            languages = ['uz', 'en', 'ru']

        redis_client = self._get_redis_client()
        if not redis_client:
            logger.warning(f"Redis not available, skipping cache warming for '{category}'")
            return {'success': False, 'reason': 'Redis not available'}

        try:
            # Get all translations for this category
            translations = Translation.query.filter_by(
                category=category,
                is_active=True
            ).all()

            if not translations:
                logger.info(f"No translations found for category '{category}'")
                return {'success': True, 'count': 0, 'category': category}

            # Get timeout for this category
            sample_key = translations[0].key if translations else f"{category}.sample"
            timeout = self._get_cache_timeout(sample_key)

            # Cache all translations
            cached_count = 0
            for trans in translations:
                if trans.language in languages:
                    cache_key = f"{self.cache_prefix}:{trans.language}:{trans.key}"
                    redis_client.setex(cache_key, timeout, trans.value)
                    cached_count += 1

            logger.info(
                f"Cache warmed: {cached_count} translations for '{category}' "
                f"(TTL: {timeout}s = {timeout/86400:.1f} days)"
            )

            return {
                'success': True,
                'count': cached_count,
                'category': category,
                'ttl_seconds': timeout,
                'languages': languages
            }

        except Exception as e:
            logger.error(f"Error warming cache for '{category}': {e}")
            return {'success': False, 'reason': str(e)}

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
    from flask import g

    # Resolve language parameter
    if language is None:
        g_language = getattr(g, 'language', None)
        language = g_language if g_language else current_app.config.get('DEFAULT_LANGUAGE', 'en')

    # Get translation from service
    result = translation_service.get_translation(key, language, **kwargs)

    return result


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


def get_plural_rules(language: str) -> Dict[str, Any]:
    """
    Get pluralization rules for a language

    Simplified plural rules for supported languages:
    - Uzbek (uz): 2 forms - singular (n == 1), plural (other)
    - English (en): 2 forms - singular (n == 1), plural (other)
    - Russian (ru): 3 forms - one (n % 10 == 1 and n % 100 != 11),
                             few (n % 10 in [2,3,4] and n % 100 not in [12,13,14]),
                             other

    Args:
        language: Language code (uz, en, ru)

    Returns:
        Dictionary with plural rules
    """
    rules = {
        'uz': {
            'forms': ['singular', 'plural'],
            'rule': lambda n: 0 if n == 1 else 1
        },
        'en': {
            'forms': ['singular', 'plural'],
            'rule': lambda n: 0 if n == 1 else 1
        },
        'ru': {
            'forms': ['one', 'few', 'other'],
            'rule': lambda n: (
                0 if (n % 10 == 1 and n % 100 != 11) else
                1 if (n % 10 in [2, 3, 4] and n % 100 not in [12, 13, 14]) else
                2
            )
        }
    }

    return rules.get(language, rules['en'])  # Default to English rules


def get_plural_translation(key: str, count: int, language: str = None, **kwargs) -> str:
    """
    Get plural form translation based on count

    Uses convention: {key}_plural for plural forms
    For Russian with 3 forms: {key}_few for the "few" form

    Args:
        key: Base translation key (singular form)
        count: Number to determine plural form
        language: Language code (defaults to current language)
        **kwargs: Variables for string formatting (count is automatically included)

    Returns:
        Translated string in appropriate plural form

    Examples:
        # English/Uzbek (2 forms):
        get_plural_translation('product.item', 1)  # "1 item"
        get_plural_translation('product.item', 5)  # "5 items"

        # Russian (3 forms):
        get_plural_translation('product.item', 1)    # "1 товар" (one)
        get_plural_translation('product.item', 2)    # "2 товара" (few)
        get_plural_translation('product.item', 5)    # "5 товаров" (other)

    Translation Keys:
        - {key} - singular form (n == 1 for uz/en)
        - {key}_plural - plural form (n != 1 for uz/en, n > 4 or n == 0 for ru)
        - {key}_few - few form (Russian only, n in 2-4)
    """
    # Resolve language
    if language is None:
        g_language = getattr(g, 'language', None)
        language = g_language if g_language else current_app.config.get('DEFAULT_LANGUAGE', 'uz')

    # Get plural rules for language
    plural_rules = get_plural_rules(language)
    form_index = plural_rules['rule'](count)
    forms = plural_rules['forms']

    # Add count to kwargs
    kwargs['count'] = count
    kwargs['n'] = count  # Alternative variable name

    # Determine which key to use
    if form_index == 0:
        # Singular or "one" form - use base key
        translation_key = key
    elif form_index == 1:
        # Plural or "few" form
        if language == 'ru' and len(forms) == 3:
            # Russian "few" form
            translation_key = f"{key}_few"
        else:
            # Standard plural
            translation_key = f"{key}_plural"
    else:
        # Russian "other" form
        translation_key = f"{key}_plural"

    # Try to get the specific plural form
    translation = translation_service.get_translation(translation_key, language, **kwargs)

    # Fallback: if specific plural form not found, try base key
    if translation == translation_key:  # Translation not found (returns key)
        translation = translation_service.get_translation(key, language, **kwargs)

    return translation


def ngettext(singular_key: str, plural_key: str, count: int,
             language: str = None, **kwargs) -> str:
    """
    Get translation with explicit singular/plural keys (gettext-style)

    Alternative to get_plural_translation when you want to specify both keys explicitly.

    Args:
        singular_key: Translation key for singular form
        plural_key: Translation key for plural form
        count: Number to determine which form to use
        language: Language code (defaults to current language)
        **kwargs: Variables for string formatting

    Returns:
        Translated string in appropriate form

    Example:
        ngettext('message.one_item', 'message.many_items', 5, count=5)
    """
    # Resolve language
    if language is None:
        g_language = getattr(g, 'language', None)
        language = g_language if g_language else current_app.config.get('DEFAULT_LANGUAGE', 'uz')

    # Add count to kwargs
    kwargs['count'] = count
    kwargs['n'] = count

    # Simple rule: use singular if count == 1, otherwise plural
    key = singular_key if count == 1 else plural_key

    return translation_service.get_translation(key, language, **kwargs)


# Convenient aliases
plural = get_plural_translation
pluralize = get_plural_translation


# REMOVED: DEFAULT_TRANSLATIONS - All translations now purely database-driven
# Use the Translation model to store and manage all translations


def seed_default_translations():
    """DEPRECATED: Seeding function removed - all translations are now database-driven
    
    Use the admin interface or Translation.set_translation() method to add translations:
    Translation.set_translation('Home', 'en', 'Home', 'navigation')
    Translation.set_translation('Home', 'uz', 'Bosh sahifa', 'navigation') 
    Translation.set_translation('Home', 'ru', 'Главная', 'navigation')
    """
    print("⚠️  seed_default_translations() is deprecated - use Translation.set_translation() instead")
    return True