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


class TranslatableContent(db.Model, TimestampMixin):
    """
    Generic translatable content table
    Stores translations for any model field in any language
    """
    __tablename__ = 'translatable_content'
    
    id = Column(Integer, primary_key=True)
    
    # Reference to the original entity
    entity_type = Column(String(50), nullable=False, index=True)  # e.g., 'Product', 'Category'
    entity_id = Column(Integer, nullable=False, index=True)       # ID of the entity
    field_name = Column(String(50), nullable=False, index=True)   # e.g., 'name', 'description'
    
    # Translation details
    language = Column(String(5), nullable=False, index=True)      # e.g., 'en', 'uz', 'ru'
    content = Column(Text, nullable=False)                        # The translated content
    
    # Metadata
    is_active = Column(Boolean, default=True, nullable=False)
    version = Column(Integer, default=1)                          # Version for content history
    
    # Unique constraint: one translation per entity+field+language
    __table_args__ = (
        UniqueConstraint('entity_type', 'entity_id', 'field_name', 'language', 
                        name='uq_translatable_content'),
        Index('idx_entity_lookup', 'entity_type', 'entity_id'),
        Index('idx_content_search', 'entity_type', 'field_name', 'language'),
    )
    
    def __repr__(self):
        return f'<TranslatableContent {self.entity_type}:{self.entity_id}.{self.field_name}[{self.language}]>'
    
    @classmethod
    def get_content(cls, entity_type, entity_id, field_name, language=None):
        """Get translated content for a specific entity field"""
        if language is None:
            language = get_current_language()
        
        content = cls.query.filter_by(
            entity_type=entity_type,
            entity_id=entity_id,
            field_name=field_name,
            language=language,
            is_active=True
        ).first()
        
        if content:
            return content.content
        
        # Fallback to English if not found
        if language != 'en':
            content = cls.query.filter_by(
                entity_type=entity_type,
                entity_id=entity_id,
                field_name=field_name,
                language='en',
                is_active=True
            ).first()
            if content:
                return content.content
        
        return None
    
    @classmethod
    def set_content(cls, entity_type, entity_id, field_name, language, content):
        """Set translated content for a specific entity field"""
        existing = cls.query.filter_by(
            entity_type=entity_type,
            entity_id=entity_id,
            field_name=field_name,
            language=language
        ).first()
        
        if existing:
            existing.content = content
            existing.is_active = True
            existing.version += 1
        else:
            new_content = cls(
                entity_type=entity_type,
                entity_id=entity_id,
                field_name=field_name,
                language=language,
                content=content,
                is_active=True
            )
            db.session.add(new_content)
        
        return True
    
    @classmethod
    def get_all_translations(cls, entity_type, entity_id, field_name):
        """Get all translations for a specific entity field"""
        translations = cls.query.filter_by(
            entity_type=entity_type,
            entity_id=entity_id,
            field_name=field_name,
            is_active=True
        ).all()
        
        return {t.language: t.content for t in translations}
    
    @classmethod
    def bulk_set_content(cls, entity_type, entity_id, translations_dict):
        """
        Bulk set translations for an entity
        translations_dict: {field_name: {language: content}}
        """
        for field_name, translations in translations_dict.items():
            for language, content in translations.items():
                if content:  # Only set non-empty content
                    cls.set_content(entity_type, entity_id, field_name, language, content)
    
    def to_dict(self):
        """Convert to dictionary"""
        return {
            'id': self.id,
            'entity_type': self.entity_type,
            'entity_id': self.entity_id,
            'field_name': self.field_name,
            'language': self.language,
            'content': self.content,
            'is_active': self.is_active,
            'version': self.version,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class TranslatableMixin:
    """
    Mixin for models that need translatable content
    Provides helper methods for getting/setting translations
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
        
        # Get from translatable content
        translated = TranslatableContent.get_content(
            entity_type=self.__class__.__name__,
            entity_id=self.id,
            field_name=field_name,
            language=language
        )
        
        if translated is not None:
            return translated
        
        # Fallback to original field value if exists
        return getattr(self, field_name, None)
    
    def set_translated(self, field_name, content, language):
        """Set translated content for a field"""
        if field_name not in self._translatable_fields:
            raise ValueError(f"Field '{field_name}' is not translatable in {self.__class__.__name__}")
        
        return TranslatableContent.set_content(
            entity_type=self.__class__.__name__,
            entity_id=self.id,
            field_name=field_name,
            language=language,
            content=content
        )
    
    def get_all_translations(self, field_name):
        """Get all translations for a field"""
        if field_name not in self._translatable_fields:
            raise ValueError(f"Field '{field_name}' is not translatable in {self.__class__.__name__}")
        
        return TranslatableContent.get_all_translations(
            entity_type=self.__class__.__name__,
            entity_id=self.id,
            field_name=field_name
        )
    
    def set_translations(self, translations_dict):
        """Set multiple translations at once"""
        TranslatableContent.bulk_set_content(
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