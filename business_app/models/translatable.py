"""
Translatable Content System
A comprehensive multilingual system for all user-facing content

Performance optimizations:
- Instance-level caching to avoid repeated DB queries
- Batch fetching for lists
- Cache invalidation on updates
"""
from datetime import datetime, UTC
from sqlalchemy import Column, Integer, String, Text, ForeignKey, Boolean, Index, UniqueConstraint
from sqlalchemy.orm import relationship, backref
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.ext.hybrid import hybrid_property
from business_app import db
from business_app.models.base import TimestampMixin
from business_app.utils.helpers import get_current_language
import json
from functools import lru_cache
from typing import Dict, Optional, List
from flask import current_app


# DEPRECATED: TranslatableContent class - replaced by unified Translation system
# This class is no longer used. All functionality has been migrated to the Translation model.
# The unified Translation system uses key format: EntityType.field.ID (e.g., Product.name.123)

# class TranslatableContent(db.Model, TimestampMixin):
#     """
#     DEPRECATED: Generic translatable content table - REPLACED BY UNIFIED TRANSLATION SYSTEM
#     All functionality moved to Translation model with unified key format
#     """
#     pass


class TranslatableMixin:
    """
    Mixin for models that need translatable content
    Now uses the unified Translation model instead of TranslatableContent

    Performance optimizations:
    - Instance-level cache for translations
    - Reduced database queries through caching
    - Batch fetching support for lists
    """

    @declared_attr
    def _translatable_fields(cls):
        """Override this in your model to specify which fields are translatable"""
        return []

    def __init__(self, *args, **kwargs):
        """Initialize translation cache"""
        super().__init__(*args, **kwargs)
        self._translation_cache: Dict[str, Dict[str, str]] = {}

    def _ensure_cache_initialized(self):
        """Ensure translation cache exists (for SQLAlchemy objects loaded from DB)"""
        if not hasattr(self, '_translation_cache'):
            self._translation_cache: Dict[str, Dict[str, str]] = {}

    def _get_cache_key(self, field_name: str, language: str) -> str:
        """Generate cache key for translation"""
        return f"{field_name}:{language}"

    def _clear_translation_cache(self, field_name: Optional[str] = None):
        """Clear translation cache for field or all fields"""
        self._ensure_cache_initialized()
        if field_name:
            # Clear cache for specific field
            self._translation_cache = {
                k: v for k, v in self._translation_cache.items()
                if not k.startswith(f"{field_name}:")
            }
        else:
            # Clear all cache
            self._translation_cache = {}

    def get_translated(self, field_name: str, language: Optional[str] = None) -> Optional[str]:
        """
        Get translated content for a field with caching

        Performance: Uses instance-level cache to avoid repeated DB queries
        """
        # Ensure cache is initialized (important for SQLAlchemy objects)
        self._ensure_cache_initialized()

        if field_name not in self._translatable_fields:
            # If not translatable, return the original field value
            return getattr(self, field_name, None)

        # Import here to avoid circular imports
        from business_app.models.translation import Translation
        from business_app.utils.helpers import get_current_language

        if language is None:
            language = get_current_language()

        # Check instance cache first
        cache_key = self._get_cache_key(field_name, language)
        if cache_key in self._translation_cache:
            return self._translation_cache[cache_key]

        # Get from unified Translation model
        translation_obj = Translation.get_entity_translation(
            entity_type=self.__class__.__name__,
            entity_id=self.id,
            field_name=field_name,
            language=language
        )

        if translation_obj and translation_obj.value:
            # Cache the result
            self._translation_cache[cache_key] = translation_obj.value
            return translation_obj.value

        # Get original value from the column
        original_value = getattr(self, field_name, None)
        
        # If the requested language is the default language (uz), 
        # prioritize the original column value over fallbacks
        # This prevents English translations from overriding the Uzbek default value
        # stored in the column when no explicit Uzbek translation exists.
        default_language = 'uz' # Default fallback
        if hasattr(current_app, 'config'):
            default_language = current_app.config.get('DEFAULT_LANGUAGE', 'uz')
            
        if language == default_language and original_value:
            self._translation_cache[cache_key] = original_value
            return original_value

        # Fallback chain: uz → en → ru
        fallback_languages = ['uz', 'en', 'ru']
        if language in fallback_languages:
            fallback_languages.remove(language)

        for fallback_lang in fallback_languages:
            # Check cache for fallback language
            fallback_cache_key = self._get_cache_key(field_name, fallback_lang)
            if fallback_cache_key in self._translation_cache:
                # Cache hit - return and also cache for requested language
                value = self._translation_cache[fallback_cache_key]
                self._translation_cache[cache_key] = value
                return value

            translation_obj = Translation.get_entity_translation(
                entity_type=self.__class__.__name__,
                entity_id=self.id,
                field_name=field_name,
                language=fallback_lang
            )
            if translation_obj and translation_obj.value:
                # Cache for both fallback language and requested language
                self._translation_cache[fallback_cache_key] = translation_obj.value
                self._translation_cache[cache_key] = translation_obj.value
                return translation_obj.value

        # Final fallback to original field value if exists
        # Cache the original value as fallback
        self._translation_cache[cache_key] = original_value
        return original_value
    
    def set_translated(self, field_name: str, content: str, language: str):
        """
        Set translated content for a field

        Automatically clears cache for the updated field
        """
        if field_name not in self._translatable_fields:
            raise ValueError(f"Field '{field_name}' is not translatable in {self.__class__.__name__}")

        # Import here to avoid circular imports
        from business_app.models.translation import Translation

        result = Translation.set_entity_translation(
            entity_type=self.__class__.__name__,
            entity_id=self.id,
            field_name=field_name,
            language=language,
            value=content
        )

        # Clear cache for this field since it was updated
        self._clear_translation_cache(field_name)

        return result
    
    def get_all_translations(self, field_name):
        """Get all translations for a field"""
        if field_name not in self._translatable_fields:
            raise ValueError(f"Field '{field_name}' is not translatable in {self.__class__.__name__}")
        
        # Import here to avoid circular imports
        from business_app.models.translation import Translation
        
        return Translation.get_all_entity_translations(
            entity_type=self.__class__.__name__,
            entity_id=self.id,
            field_name=field_name
        )
    
    def set_translations(self, translations_dict):
        """Set multiple translations at once"""
        # Import here to avoid circular imports
        from business_app.models.translation import Translation

        Translation.bulk_set_entity_translations(
            entity_type=self.__class__.__name__,
            entity_id=self.id,
            translations_dict=translations_dict
        )

        # Clear entire cache since multiple fields may have been updated
        self._clear_translation_cache()

    @classmethod
    def prefetch_translations(cls, instances: List, fields: Optional[List[str]] = None, language: Optional[str] = None):
        """
        Batch prefetch translations for multiple instances

        Performance: Reduces N+1 query problem when loading lists of entities

        Args:
            instances: List of model instances
            fields: List of field names to prefetch (None = all translatable fields)
            language: Language to prefetch (None = current language)

        Example:
            products = Product.query.limit(100).all()
            Product.prefetch_translations(products, fields=['name', 'description'], language='uz')
            # Now accessing product.get_translated('name', 'uz') uses cache, no DB queries
        """
        if not instances:
            return

        # Import here to avoid circular imports
        from business_app.models.translation import Translation
        from business_app.utils.helpers import get_current_language

        if language is None:
            language = get_current_language()

        # Determine which fields to prefetch
        if fields is None:
            fields = cls._translatable_fields

        # Get entity IDs
        entity_ids = [instance.id for instance in instances if hasattr(instance, 'id')]
        if not entity_ids:
            return

        # Batch fetch all translations for these entities and fields
        entity_type = cls.__name__
        translations = Translation.query.filter(
            Translation.key.like(f"{entity_type}.%.%"),
            Translation.language == language,
            Translation.is_active == True
        ).all()

        # Build lookup map: {entity_id: {field_name: value}}
        translation_map: Dict[int, Dict[str, str]] = {}
        for translation in translations:
            # Parse key: EntityType.field_name.entity_id
            parts = translation.key.split('.')
            if len(parts) == 3:
                _, field_name, entity_id_str = parts
                try:
                    entity_id = int(entity_id_str)
                    if entity_id in entity_ids and field_name in fields:
                        if entity_id not in translation_map:
                            translation_map[entity_id] = {}
                        translation_map[entity_id][field_name] = translation.value
                except ValueError:
                    continue

        # Populate caches for all instances
        for instance in instances:
            if not hasattr(instance, 'id'):
                continue

            # Ensure cache is initialized
            instance._ensure_cache_initialized()

            entity_translations = translation_map.get(instance.id, {})
            for field_name in fields:
                cache_key = instance._get_cache_key(field_name, language)
                value = entity_translations.get(field_name)
                if value:
                    instance._translation_cache[cache_key] = value

    def to_dict_multilingual(self, language=None, include_all_translations=False):
        """
        Convert to dictionary with multilingual support
        Override this method in your models to customize the output
        """
        result = {}
        
        # Get base fields
        for column in self.__table__.columns:
            if column.name not in self._translatable_fields:
                value = getattr(self, column.name)
                if hasattr(value, 'isoformat'):  # Handle datetime
                    value = value.isoformat()
                elif hasattr(value, 'value'):  # Handle enums
                    value = value.value
                result[column.name] = value
        
        # Get translatable fields
        for field_name in self._translatable_fields:
            if include_all_translations:
                # Include all translations
                result[f'{field_name}_translations'] = self.get_all_translations(field_name)
                # Also include the current language version
                result[field_name] = self.get_translated(field_name, language)
            else:
                # Only include current language
                result[field_name] = self.get_translated(field_name, language)
        
        return result


def create_translatable_properties(model_class, translatable_fields):
    """
    Helper function to create properties for translatable fields
    This creates properties like 'name_translated' that automatically get the current language
    """
    for field_name in translatable_fields:
        def make_property(field):
            def getter(self):
                return self.get_translated(field)
            
            def setter(self, value):
                current_lang = get_current_language()
                self.set_translated(field, value, current_lang)
            
            return property(getter, setter)
        
        # Create the property
        prop = make_property(field_name)
        setattr(model_class, f'{field_name}_translated', prop)


# Decorator to make a model translatable
def translatable(*fields):
    """
    Class decorator to make fields translatable
    Usage: @translatable('name', 'description')
    """
    def decorator(cls):
        # Set the translatable fields
        cls._translatable_fields = list(fields)
        
        # Add the mixin
        if TranslatableMixin not in cls.__bases__:
            cls.__bases__ = (TranslatableMixin,) + cls.__bases__
        
        # Create helper properties
        create_translatable_properties(cls, fields)
        
        return cls
    
    return decorator