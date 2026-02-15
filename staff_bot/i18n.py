"""
Internationalization (i18n) support for the Staff Bot
Multi-language support with translation management using 'staff_bot' category
"""
import logging
from typing import Dict, Any, Optional, List

from config import config
from database import db_manager

logger = logging.getLogger(__name__)


class Translation:
    """Translation management system for staff bot"""

    def __init__(self):
        self.translations: Dict[str, Dict[str, str]] = {}
        self.fallback_language = config.localization.fallback_language
        self.supported_languages = config.localization.supported_languages
        self.missing_keys: Dict[str, set] = {}
        self._missing_key_log_limit = 100

    async def load_translations(self):
        """Load translations from database (staff_bot category)"""
        try:
            query = """
            SELECT language, key, value
            FROM translations
            WHERE is_active = TRUE AND category = 'staff_bot'
            ORDER BY language, key
            """
            rows = await db_manager.fetchall(query)

            for row in rows:
                language = row['language']
                key = row['key']
                value = row['value']

                if language not in self.translations:
                    self.translations[language] = {}
                self.translations[language][key] = value

            if self.translations:
                logger.info(
                    f"Loaded {sum(len(keys) for keys in self.translations.values())} "
                    f"staff translations for languages: {list(self.translations.keys())}"
                )
            else:
                logger.warning(
                    "No staff translations loaded from database. "
                    "Staff bot may not function correctly. "
                    "Please run the staff translation seeding script."
                )

        except Exception as e:
            logger.error(f"Failed to load staff translations from database: {e}")
            raise

    async def reload_translations(self):
        """Reload translations from database at runtime"""
        logger.info("Reloading staff translations from database...")
        self.translations = {}
        self.missing_keys = {}
        await self.load_translations()
        logger.info("Staff translation reload complete")

    def get(self, key: str, language: str = None, *args, **kwargs) -> str:
        """Get translation for key in specified language"""
        if not language:
            language = config.localization.default_language

        # Try requested language
        if language in self.translations and key in self.translations[language]:
            translation = self.translations[language][key]
        # Fallback to default language
        elif self.fallback_language in self.translations and key in self.translations[self.fallback_language]:
            translation = self.translations[self.fallback_language][key]
        # Derive readable fallback from key
        else:
            self._track_missing_key(key, language)
            last_part = key.rsplit('.', 1)[-1] if '.' in key else key
            translation = last_part.replace('_', ' ').capitalize()

        if args or kwargs:
            try:
                translation = translation.format(*args, **kwargs)
            except (KeyError, ValueError) as e:
                logger.warning(f"Failed to format translation '{key}': {e}")

        return translation

    def _track_missing_key(self, key: str, language: str):
        """Track missing translation keys"""
        if language not in self.missing_keys:
            self.missing_keys[language] = set()
        if len(self.missing_keys[language]) < self._missing_key_log_limit:
            self.missing_keys[language].add(key)

    def get_missing_keys(self, language: str = None) -> Dict[str, List[str]]:
        """Get list of missing translation keys"""
        if language:
            return {language: sorted(list(self.missing_keys.get(language, set())))}
        return {lang: sorted(list(keys)) for lang, keys in self.missing_keys.items()}

    async def get_user_language(self, telegram_id: int) -> str:
        """Get user's preferred language"""
        query = "SELECT preferred_language FROM users WHERE telegram_id = $1"
        language = await db_manager.fetchval(query, str(telegram_id))
        return language or config.localization.default_language

    def get_language_flag(self, language_code: str) -> str:
        """Get flag emoji for language"""
        flags = {'en': '\U0001f1fa\U0001f1f8', 'uz': '\U0001f1fa\U0001f1ff', 'ru': '\U0001f1f7\U0001f1fa'}
        return flags.get(language_code, '\U0001f310')

    def get_language_name(self, language_code: str, display_language: str = None) -> str:
        """Get language name in specified display language"""
        if not display_language:
            display_language = language_code
        names = {
            'en': {'en': 'English', 'uz': 'Inglizcha', 'ru': '\u0410\u043d\u0433\u043b\u0438\u0439\u0441\u043a\u0438\u0439'},
            'uz': {'en': 'Uzbek', 'uz': "O'zbekcha", 'ru': '\u0423\u0437\u0431\u0435\u043a\u0441\u043a\u0438\u0439'},
            'ru': {'en': 'Russian', 'uz': 'Ruscha', 'ru': '\u0420\u0443\u0441\u0441\u043a\u0438\u0439'}
        }
        return names.get(language_code, {}).get(display_language, language_code)


# Global translation instance
i18n = Translation()
