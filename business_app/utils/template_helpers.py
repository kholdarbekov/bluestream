"""
Template helpers for multilingual content
Provides Jinja2 filters and functions for easy access to translatable content
"""
from flask import current_app, g


def get_translated_content(entity, field_name, language=None):
    """
    Get translated content for an entity field
    Can be used in templates as a function or filter
    """
    if language is None:
        from business_app.utils.helpers import get_current_language
        language = get_current_language()
    
    if hasattr(entity, 'get_translated'):
        # Entity supports translatable mixin
        return entity.get_translated(field_name, language)
    else:
        # Fallback to direct field access
        return getattr(entity, field_name, None)


def get_all_translations_for_field(entity, field_name):
    """Get all translations for a specific field"""
    if hasattr(entity, 'get_all_translations'):
        return entity.get_all_translations(field_name)
    else:
        # Fallback to just the current field value
        from business_app.utils.helpers import get_current_language
        return {get_current_language(): getattr(entity, field_name, None)}


def format_multilingual_list(items, field_name='name', language=None, separator=', '):
    """Format a list of translatable items into a string"""
    if not items:
        return ''
    
    if language is None:
        from business_app.utils.helpers import get_current_language
        language = get_current_language()
    
    names = []
    for item in items:
        if hasattr(item, 'get_translated'):
            name = item.get_translated(field_name, language)
        else:
            name = getattr(item, field_name, str(item))
        
        if name:
            names.append(name)
    
    return separator.join(names)


def get_language_label(language_code):
    """Get the display label for a language code"""
    language_labels = {
        'en': 'English',
        'uz': 'O\'zbek',
        'ru': 'Русский'
    }
    return language_labels.get(language_code, language_code.upper())


def get_available_languages():
    """Get list of available languages"""
    return current_app.config.get('LANGUAGES', ['en', 'uz', 'ru'])


def is_field_translated(entity, field_name, language=None):
    """Check if a field has translation for a specific language"""
    if language is None:
        from business_app.utils.helpers import get_current_language
        language = get_current_language()
    
    if hasattr(entity, 'get_translated'):
        content = entity.get_translated(field_name, language)
        return content is not None and content.strip() != ''
    
    return False


def get_translation_completeness(entity, language=None):
    """
    Get translation completeness percentage for an entity
    Returns a dict with completion info
    """
    if language is None:
        from business_app.utils.helpers import get_current_language
        language = get_current_language()
    
    if not hasattr(entity, '_translatable_fields'):
        return {'percentage': 100, 'translated_fields': 0, 'total_fields': 0}
    
    total_fields = len(entity._translatable_fields)
    translated_fields = 0
    
    for field_name in entity._translatable_fields:
        if is_field_translated(entity, field_name, language):
            translated_fields += 1
    
    percentage = (translated_fields / total_fields * 100) if total_fields > 0 else 100
    
    return {
        'percentage': round(percentage, 1),
        'translated_fields': translated_fields,
        'total_fields': total_fields,
        'missing_fields': [
            field for field in entity._translatable_fields
            if not is_field_translated(entity, field, language)
        ]
    }


def get_fallback_content(entity, field_name, language=None):
    """
    Get content with smart fallback logic:
    1. Try requested language
    2. Try English
    3. Try Uzbek (default)
    4. Try any available translation
    5. Return field value or None
    """
    if language is None:
        from business_app.utils.helpers import get_current_language
        language = get_current_language()
    
    # Try requested language first
    if hasattr(entity, 'get_translated'):
        content = entity.get_translated(field_name, language)
        if content and content.strip():
            return content
        
        # Try fallback languages
        fallback_languages = ['en', 'uz']
        if language not in fallback_languages:
            fallback_languages.insert(0, language)
        
        for fallback_lang in fallback_languages:
            content = entity.get_translated(field_name, fallback_lang)
            if content and content.strip():
                return content
        
        # Try any available translation
        all_translations = entity.get_all_translations(field_name)
        for lang_code, content in all_translations.items():
            if content and content.strip():
                return content
    
    # Final fallback to field value
    return getattr(entity, field_name, None)


def render_multilingual_text(entity, field_name, language=None, fallback=True, show_language_badge=False):
    """
    Render multilingual text with optional language badge
    Returns HTML-safe string
    """
    from markupsafe import Markup
    
    if language is None:
        from business_app.utils.helpers import get_current_language
        language = get_current_language()
    
    if fallback:
        content = get_fallback_content(entity, field_name, language)
    else:
        content = get_translated_content(entity, field_name, language)
    
    if not content:
        return ''
    
    # Escape HTML content
    content = Markup.escape(content)
    
    if show_language_badge:
        # Add language badge
        lang_label = get_language_label(language)
        badge = f'<span class="language-badge language-{language}" title="Content in {lang_label}">{language.upper()}</span> '
        return Markup(badge + content)
    
    return content


def get_entity_translations_summary(entity):
    """Get a summary of all translations for an entity"""
    if not hasattr(entity, '_translatable_fields'):
        return {}
    
    summary = {}
    available_languages = get_available_languages()
    
    for language in available_languages:
        completeness = get_translation_completeness(entity, language)
        summary[language] = {
            'label': get_language_label(language),
            'completeness': completeness,
            'is_complete': completeness['percentage'] == 100
        }
    
    return summary


# Jinja2 filters
def register_multilingual_filters(app):
    """Register multilingual template filters"""
    
    @app.template_filter('translated')
    def translated_filter(entity, field_name, language=None):
        """Get translated content: {{ product|translated('name', 'en') }}"""
        return get_translated_content(entity, field_name, language)
    
    @app.template_filter('multilingual')
    def multilingual_filter(entity, field_name, language=None, fallback=True):
        """Get multilingual content with fallback: {{ product|multilingual('name') }}"""
        if fallback:
            return get_fallback_content(entity, field_name, language)
        else:
            return get_translated_content(entity, field_name, language)
    
    @app.template_filter('multilingual_list')
    def multilingual_list_filter(items, field_name='name', language=None, separator=', '):
        """Format multilingual list: {{ categories|multilingual_list('name') }}"""
        return format_multilingual_list(items, field_name, language, separator)
    
    @app.template_filter('translation_completeness')
    def translation_completeness_filter(entity, language=None):
        """Get translation completeness: {{ product|translation_completeness }}"""
        return get_translation_completeness(entity, language)
    
    @app.template_filter('render_multilingual')
    def render_multilingual_filter(entity, field_name, language=None, fallback=True, show_badge=False):
        """Render multilingual text with badge: {{ product|render_multilingual('name', show_badge=True) }}"""
        return render_multilingual_text(entity, field_name, language, fallback, show_badge)


# Jinja2 global functions
def register_multilingual_globals(app):
    """Register multilingual template global functions"""
    
    @app.template_global()
    def get_current_lang():
        """Get current language in templates"""
        from business_app.utils.helpers import get_current_language
        return get_current_language()
    
    @app.template_global()
    def get_available_langs():
        """Get available languages in templates"""
        return get_available_languages()
    
    @app.template_global()
    def get_lang_label(language_code):
        """Get language label in templates"""
        return get_language_label(language_code)
    
    @app.template_global()
    def translate_static(key, language=None, **kwargs):
        """Translate static text in templates"""
        from business_app.utils.translations import get_translation
        return get_translation(key, language, **kwargs)
    
    @app.template_global()
    def entity_translations_summary(entity):
        """Get entity translations summary in templates"""
        return get_entity_translations_summary(entity)


# CSS styles for language badges
MULTILINGUAL_CSS = """
.language-badge {
    display: inline-block;
    padding: 2px 6px;
    border-radius: 3px;
    font-size: 0.75em;
    font-weight: bold;
    text-transform: uppercase;
    margin-right: 4px;
}

.language-badge.language-en {
    background-color: #007bff;
    color: white;
}

.language-badge.language-uz {
    background-color: #28a745;
    color: white;
}

.language-badge.language-ru {
    background-color: #dc3545;
    color: white;
}

.translation-completeness {
    display: flex;
    align-items: center;
    gap: 8px;
}

.translation-progress {
    flex: 1;
    height: 6px;
    background-color: #e9ecef;
    border-radius: 3px;
    overflow: hidden;
}

.translation-progress-bar {
    height: 100%;
    background-color: #28a745;
    transition: width 0.3s ease;
}

.translation-progress-bar.incomplete {
    background-color: #ffc107;
}

.translation-progress-bar.minimal {
    background-color: #dc3545;
}
"""