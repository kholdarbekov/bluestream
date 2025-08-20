"""
Translation models for database-backed multi-language support
This file should be placed in business_app/models/translation_models.py
"""
from datetime import datetime, UTC
from sqlalchemy import Index
from business_app import db


class Translation(db.Model):
    """Translation model for storing multi-language text"""
    
    __tablename__ = 'translations'
    
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(255), nullable=False, index=True)
    language = db.Column(db.String(5), nullable=False, index=True)
    value = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(50), default='general', index=True)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    updated_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    
    # Relationships
    creator = db.relationship('User', foreign_keys=[created_by], backref='created_translations')
    updater = db.relationship('User', foreign_keys=[updated_by], backref='updated_translations')
    
    # Composite unique constraint
    __table_args__ = (
        db.UniqueConstraint('key', 'language', name='uq_translation_key_language'),
        Index('idx_translation_key_lang_active', 'key', 'language', 'is_active'),
        Index('idx_translation_category_lang', 'category', 'language'),
    )
    
    def __repr__(self):
        return f'<Translation {self.key}:{self.language}>'
    
    def to_dict(self):
        """Convert to dictionary"""
        return {
            'id': self.id,
            'key': self.key,
            'language': self.language,
            'value': self.value,
            'category': self.category,
            'description': self.description,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'created_by': self.created_by,
            'updated_by': self.updated_by
        }
    
    @classmethod
    def get_translation(cls, key: str, language: str = 'en'):
        """Get translation by key and language"""
        return cls.query.filter_by(
            key=key,
            language=language,
            is_active=True
        ).first()
    
    @classmethod
    def get_translations_by_category(cls, category: str, language: str = 'en'):
        """Get all translations in a category"""
        return cls.query.filter_by(
            category=category,
            language=language,
            is_active=True
        ).all()
    
    @classmethod
    def get_all_translations(cls, language: str = 'en'):
        """Get all translations for a language"""
        return cls.query.filter_by(
            language=language,
            is_active=True
        ).all()
    
    @classmethod
    def bulk_create_or_update(cls, translations_data: dict, category: str = 'general', user_id: int = None):
        """
        Bulk create or update translations
        
        Args:
            translations_data: Dict with structure {language: {key: value}}
            category: Translation category
            user_id: User ID for audit
        """
        for language, translations in translations_data.items():
            for key, value in translations.items():
                existing = cls.query.filter_by(key=key, language=language).first()
                
                if existing:
                    existing.value = value
                    existing.category = category
                    existing.is_active = True
                    existing.updated_by = user_id
                    existing.updated_at = datetime.now(UTC)
                else:
                    new_translation = cls(
                        key=key,
                        language=language,
                        value=value,
                        category=category,
                        is_active=True,
                        created_by=user_id,
                        updated_by=user_id
                    )
                    db.session.add(new_translation)
        
        db.session.commit()


class TranslationCategory(db.Model):
    """Categories for organizing translations"""
    
    __tablename__ = 'translation_categories'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    
    # Relationships
    creator = db.relationship('User', backref='created_translation_categories')
    
    def __repr__(self):
        return f'<TranslationCategory {self.name}>'
    
    def to_dict(self):
        """Convert to dictionary"""
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'created_by': self.created_by
        }


class Language(db.Model):
    """Supported languages"""
    
    __tablename__ = 'languages'
    
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(5), unique=True, nullable=False)  # en, uz, ru
    name = db.Column(db.String(50), nullable=False)  # English, O'zbek, Русский
    native_name = db.Column(db.String(50), nullable=False)  # English, O'zbek, Русский
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    is_default = db.Column(db.Boolean, default=False, nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    flag_icon = db.Column(db.String(10))  # Unicode flag emoji or icon class
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    
    def __repr__(self):
        return f'<Language {self.code}:{self.name}>'
    
    def to_dict(self):
        """Convert to dictionary"""
        return {
            'id': self.id,
            'code': self.code,
            'name': self.name,
            'native_name': self.native_name,
            'is_active': self.is_active,
            'is_default': self.is_default,
            'sort_order': self.sort_order,
            'flag_icon': self.flag_icon,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
    
    @classmethod
    def get_active_languages(cls):
        """Get all active languages"""
        return cls.query.filter_by(is_active=True).order_by(cls.sort_order, cls.name).all()
    
    @classmethod
    def get_default_language(cls):
        """Get default language"""
        return cls.query.filter_by(is_default=True, is_active=True).first()
    
    @classmethod
    def get_by_code(cls, code: str):
        """Get language by code"""
        return cls.query.filter_by(code=code, is_active=True).first()


class TranslationAudit(db.Model):
    """Audit trail for translation changes"""
    
    __tablename__ = 'translation_audit'
    
    id = db.Column(db.Integer, primary_key=True)
    translation_id = db.Column(db.Integer, db.ForeignKey('translations.id'), nullable=False)
    action = db.Column(db.String(20), nullable=False)  # CREATE, UPDATE, DELETE
    old_value = db.Column(db.Text)
    new_value = db.Column(db.Text)
    changed_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    changed_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.Text)
    
    # Relationships
    translation = db.relationship('Translation', backref='audit_logs')
    user = db.relationship('User', backref='translation_audits')
    
    def __repr__(self):
        return f'<TranslationAudit {self.action}:{self.translation_id}>'
    
    def to_dict(self):
        """Convert to dictionary"""
        return {
            'id': self.id,
            'translation_id': self.translation_id,
            'action': self.action,
            'old_value': self.old_value,
            'new_value': self.new_value,
            'changed_by': self.changed_by,
            'changed_at': self.changed_at.isoformat() if self.changed_at else None,
            'ip_address': self.ip_address,
            'user_agent': self.user_agent
        }


# Initialize default languages
def seed_languages():
    """Seed default languages"""
    languages_data = [
        {
            'code': 'en',
            'name': 'English',
            'native_name': 'English',
            'is_default': True,
            'sort_order': 1,
            'flag_icon': '🇺🇸'
        },
        {
            'code': 'uz',
            'name': 'Uzbek',
            'native_name': 'O\'zbek',
            'is_default': False,
            'sort_order': 2,
            'flag_icon': '🇺🇿'
        },
        {
            'code': 'ru',
            'name': 'Russian',
            'native_name': 'Русский',
            'is_default': False,
            'sort_order': 3,
            'flag_icon': '🇷🇺'
        }
    ]
    
    for lang_data in languages_data:
        existing = Language.query.filter_by(code=lang_data['code']).first()
        if not existing:
            language = Language(**lang_data)
            db.session.add(language)
    
    db.session.commit()


def seed_translation_categories():
    """Seed default translation categories"""
    categories_data = [
        {'name': 'general', 'description': 'General application text'},
        {'name': 'ui', 'description': 'User interface elements'},
        {'name': 'messages', 'description': 'System messages and notifications'},
        {'name': 'errors', 'description': 'Error messages'},
        {'name': 'emails', 'description': 'Email templates'},
        {'name': 'sms', 'description': 'SMS templates'},
        {'name': 'products', 'description': 'Product-related text'},
        {'name': 'orders', 'description': 'Order-related text'},
        {'name': 'delivery', 'description': 'Delivery-related text'},
        {'name': 'payments', 'description': 'Payment-related text'},
        {'name': 'loyalty', 'description': 'Loyalty program text'},
        {'name': 'subscription', 'description': 'Subscription-related text'},
        {'name': 'admin', 'description': 'Admin interface text'},
        {'name': 'telegram', 'description': 'Telegram bot messages'},
        {'name': 'validation', 'description': 'Validation messages'}
    ]
    
    for cat_data in categories_data:
        existing = TranslationCategory.query.filter_by(name=cat_data['name']).first()
        if not existing:
            category = TranslationCategory(**cat_data)
            db.session.add(category)
    
    db.session.commit()