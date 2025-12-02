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
    
    @app.template_filter('translate')
    def translate_filter(key, language=None, **kwargs):
        """Translate static text: {{ 'Home'|translate(current_language) }}"""
        from business_app.utils.translations import get_translation
        from flask import session, g, request
        import logging
        import sys
        import threading
        import time

        # HYPER-COMPREHENSIVE LOGGING - Step 1: Filter Entry with Thread Info
        logger = logging.getLogger(__name__)
        thread_id = threading.current_thread().ident
        request_id = getattr(request, 'id', 'NO_REQUEST') if request else 'NO_REQUEST'
        timestamp = time.time()

        # logger.debug(f"TRANSLATE FILTER START: key='{key}', thread={thread_id}, request_id={request_id}, timestamp={timestamp}")
        
        # HYPER-COMPREHENSIVE LOGGING - Step 2: Context Analysis with Request Info
        try:
            g_language = getattr(g, 'language', 'NOT_SET')
            session_language = session.get('language', 'NOT_SET')
            request_path = request.path if request else 'NO_REQUEST'
            request_method = request.method if request else 'NO_REQUEST'

            # logger.debug(f"CONTEXT [T:{thread_id}]: g.language='{g_language}', session.language='{session_language}', path='{request_path}', method='{request_method}'")

            # Check for cross-contamination
            if hasattr(g, '_translation_calls'):
                g._translation_calls += 1
            else:
                g._translation_calls = 1

            # logger.debug(f"CALL COUNT in this request: {g._translation_calls}")

        except RuntimeError as e:
            logger.error(f"CONTEXT ERROR [T:{thread_id}]: {e}")
            g_language = 'ERROR'
            session_language = 'ERROR'
        
        # HYPER-COMPREHENSIVE LOGGING - Step 3: Language Resolution with Cache Info
        resolved_language = None
        if language is None:
            try:
                from business_app.utils.helpers import get_current_language
                resolved_language = get_current_language()
                # logger.debug(f"LANGUAGE RESOLVED [T:{thread_id}]: get_current_language() returned '{resolved_language}'")
            except RuntimeError as e:
                logger.error(f"LANGUAGE RESOLUTION ERROR [T:{thread_id}]: {e}")
                # Fallback to g.language or default
                resolved_language = getattr(g, 'language', None)
                if not resolved_language:
                    from flask import current_app
                    resolved_language = current_app.config.get('DEFAULT_LANGUAGE', 'en')
                # logger.debug(f"FALLBACK LANGUAGE [T:{thread_id}]: '{resolved_language}'")
            language = resolved_language
        else:
            # logger.debug(f"LANGUAGE PROVIDED [T:{thread_id}]: using explicit language '{language}'")
            pass
        
        # HYPER-COMPREHENSIVE LOGGING - Step 4: Cache State Check
        try:
            from business_app.utils.translations import translation_service
            cache_key = f"translations:{language}:{key}"
            cached_value = translation_service._get_cached_translation(key, language)
            # logger.debug(f"CACHE STATE [T:{thread_id}]: key='{cache_key}', cached='{cached_value}'")
        except Exception as cache_e:
            logger.error(f"CACHE CHECK ERROR [T:{thread_id}]: {cache_e}")

        # HYPER-COMPREHENSIVE LOGGING - Step 5: Translation Call
        # logger.debug(f"CALLING get_translation [T:{thread_id}]: key='{key}', language='{language}', kwargs={kwargs}")

        result = get_translation(key, language, **kwargs)

        # logger.debug(f"TRANSLATION RESULT [T:{thread_id}]: '{key}' [{language}] -> '{result}'")

        # HYPER-COMPREHENSIVE LOGGING - Step 6: Final State
        final_timestamp = time.time()
        duration = final_timestamp - timestamp
        # logger.debug(f"TRANSLATE FILTER END [T:{thread_id}]: duration={duration:.4f}s, final_result='{result}'")
        
        return result


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