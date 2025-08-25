"""
Translatable Content System
A comprehensive multilingual system for all user-facing content
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
    """
    
    @declared_attr
    def _translatable_fields(cls):
        """Override this in your model to specify which fields are translatable"""
        return []
    
    def get_translated(self, field_name, language=None):
        """Get translated content for a field"""
        if field_name not in self._translatable_fields:
            # If not translatable, return the original field value
            return getattr(self, field_name, None)
        
        # Import here to avoid circular imports
        from business_app.models.translation import Translation
        from business_app.utils.helpers import get_current_language
        
        if language is None:
            language = get_current_language()
        
        # Get from unified Translation model
        translation_obj = Translation.get_entity_translation(
            entity_type=self.__class__.__name__,
            entity_id=self.id,
            field_name=field_name,
            language=language
        )
        
        if translation_obj and translation_obj.value:
            return translation_obj.value
        
        # Fallback to English if not found and current language isn't English
        if language != 'en':
            translation_obj = Translation.get_entity_translation(
                entity_type=self.__class__.__name__,
                entity_id=self.id,
                field_name=field_name,
                language='en'
            )
            if translation_obj and translation_obj.value:
                return translation_obj.value
        
        # Final fallback to original field value if exists
        return getattr(self, field_name, None)
    
    def set_translated(self, field_name, content, language):
        """Set translated content for a field"""
        if field_name not in self._translatable_fields:
            raise ValueError(f"Field '{field_name}' is not translatable in {self.__class__.__name__}")
        
        # Import here to avoid circular imports
        from business_app.models.translation import Translation
        
        return Translation.set_entity_translation(
            entity_type=self.__class__.__name__,
            entity_id=self.id,
            field_name=field_name,
            language=language,
            value=content
        )
    
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