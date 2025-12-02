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
        # Debug logging
        # if current_app.debug:
            # current_app.logger.debug(f"Translation requested: '{key}' in language '{language}'")
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
        
        # Return key with indicator if no translation found (helps identify missing translations)
        # if current_app.debug:
        #     current_app.logger.warning(f"Missing translation for key '{key}' in language '{language}'")
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

            # Debug logging (only in debug mode)
            # if current_app.debug:
            #     if translation:
            #         current_app.logger.debug(f"DB translation found for '{key}' [{language}]: {translation.value[:50]}...")
            #     else:
            #         current_app.logger.debug(f"No DB translation found for '{key}' [{language}]")

            return translation.value if translation else None
        except Exception as e:
            # Handle database transaction errors by rolling back
            try:
                from business_app import db
                db.session.rollback()
            except:
                pass
            
            current_app.logger.error(f"Error getting translation for '{key}' [{language}]: {e}")
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
    # Resolve language parameter
    if language is None:
        g_language = getattr(g, 'language', None)
        language = g_language if g_language else current_app.config.get('DEFAULT_LANGUAGE', 'en')

    # Debug logging (only in debug mode)
    # if current_app.debug:
    #     logger.debug(f"get_translation: key='{key}', language='{language}'")

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