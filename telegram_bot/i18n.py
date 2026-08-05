"""
Internationalization (i18n) support for the Telegram Bot
Multi-language support with translation management
"""
import json
import logging
from typing import Dict, Any, Optional, List
from pathlib import Path

from config import config
from database import db_manager

logger = logging.getLogger(__name__)


class Translation:
    """Translation management system"""

    def __init__(self):
        self.translations: Dict[str, Dict[str, str]] = {}
        self.fallback_language = config.localization.fallback_language
        self.supported_languages = config.localization.supported_languages
        self.missing_keys: Dict[str, set] = {}  # Track missing keys by language
        self._missing_key_log_limit = 100  # Max missing keys to track

    def normalize_language(self, language: Optional[str]) -> str:
        """Normalize locale variants to one of the supported language codes.

        ``preferred_language`` is written raw from the caller's Telegram
        ``language_code`` (an IETF tag such as ``ru-RU``) and is also settable
        through ``POST /api/v1/auth/sync-profile`` with no validation. Without
        this, such a value misses the requested-language lookup in :meth:`get`
        entirely and every string is served in the FALLBACK language instead —
        silently, because from ``get()``'s point of view the fallback worked.

        Kept behaviourally identical to ``staff_bot/i18n.py::normalize_language``
        so one stored value cannot mean two different languages on the two bots.
        """
        if not language:
            return config.localization.default_language

        value = str(language).strip().lower().replace("_", "-")
        if not value:
            return config.localization.default_language

        if value in self.supported_languages:
            return value

        base = value.split("-", 1)[0]
        if base in self.supported_languages:
            return base

        aliases = {
            "english": "en",
            "uzbek": "uz",
            "russian": "ru",
        }
        return aliases.get(value, config.localization.default_language)

    async def load_translations(self):
        """Load translations from database"""
        try:
            query = """
            SELECT language, key, value
            FROM translations
            WHERE is_active = TRUE AND category = 'telegram'
            ORDER BY language, key
            """

            rows = await db_manager.fetchall(query)

            # Organize translations by language
            for row in rows:
                language = self.normalize_language(row['language'])
                key = row['key']
                value = row['value']

                if language not in self.translations:
                    self.translations[language] = {}

                self.translations[language][key] = value

            if self.translations:
                logger.info(f"Loaded {sum(len(keys) for keys in self.translations.values())} translations for languages: {list(self.translations.keys())}")
            else:
                logger.warning("No translations loaded from database. Bot may not function correctly. Please run the translation seeding script.")

        except Exception as e:
            logger.error(f"Failed to load translations from database: {e}")
            logger.error("Bot will not function correctly without translations. Please check database connection and ensure translations table exists.")
            raise

    async def reload_translations(self):
        """Reload translations from database at runtime without bot restart"""
        logger.info("Reloading translations from database...")
        self.translations = {}  # Clear existing translations
        self.missing_keys = {}  # Reset missing keys tracking
        await self.load_translations()
        logger.info("Translation reload complete")

    @staticmethod
    def humanised_missing_key(key: str) -> str:
        """The placeholder text :meth:`get` returns for an UNSEEDED key.

        ``telegram.orders.cod_restricted_place`` -> ``Cod restricted place``.

        Exposed so a call site can distinguish "this key rendered" from "this
        key is not seeded here" WITHOUT re-deriving the formula — a copy of it
        would silently stop matching the day this one changes. The comparison
        is exact even for keys that take kwargs, because the humanised text
        carries no ``{...}`` placeholder, so :meth:`get`'s ``.format()`` step
        leaves it byte-identical.
        """
        last_part = key.rsplit('.', 1)[-1] if '.' in key else key
        return last_part.replace('_', ' ').capitalize()

    def get(self, key: str, language: str = None, *args, **kwargs) -> str:
        """Get translation for key in specified language"""
        language = self.normalize_language(language)
        fallback_language = self.normalize_language(self.fallback_language)

        # Try to get translation in requested language
        if language in self.translations and key in self.translations[language]:
            translation = self.translations[language][key]
        # Fallback to default language
        elif fallback_language in self.translations and key in self.translations[fallback_language]:
            translation = self.translations[fallback_language][key]
            logger.debug(f"Using fallback language '{fallback_language}' for key '{key}'")
        # Return user-friendly fallback when translation is missing
        else:
            self._track_missing_key(key, language)
            logger.warning(f"Translation not found for key '{key}' in language '{language}' or fallback '{self.fallback_language}'")
            # Derive a readable fallback from the key (e.g. "telegram.menu.products" -> "Products")
            translation = self.humanised_missing_key(key)

        # Format with kwargs if provided
        if args or kwargs:
            try:
                translation = translation.format(*args, **kwargs)
            except (KeyError, ValueError) as e:
                logger.warning(f"Failed to format translation '{key}': {e}")

        return translation

    def _track_missing_key(self, key: str, language: str):
        """Track missing translation keys for monitoring"""
        if language not in self.missing_keys:
            self.missing_keys[language] = set()

        # Only track up to limit to prevent memory issues
        if len(self.missing_keys[language]) < self._missing_key_log_limit:
            self.missing_keys[language].add(key)

    def get_missing_keys(self, language: str = None) -> Dict[str, List[str]]:
        """Get list of missing translation keys"""
        if language:
            return {language: sorted(list(self.missing_keys.get(language, set())))}
        else:
            return {lang: sorted(list(keys)) for lang, keys in self.missing_keys.items()}

    def check_completeness(self, required_keys: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Check translation completeness across all supported languages

        Args:
            required_keys: Optional list of keys that MUST exist (critical keys)

        Returns:
            Dictionary with completeness statistics
        """
        # Get all unique keys across all languages
        all_keys = set()
        for lang_translations in self.translations.values():
            all_keys.update(lang_translations.keys())

        stats = {
            'total_unique_keys': len(all_keys),
            'languages': {},
            'critical_keys_missing': []
        }

        # Check each supported language
        for language in self.supported_languages:
            lang_keys = set(self.translations.get(language, {}).keys())
            missing = all_keys - lang_keys
            percentage = (len(lang_keys) / len(all_keys) * 100) if all_keys else 100

            stats['languages'][language] = {
                'total_keys': len(lang_keys),
                'missing_keys': len(missing),
                'percentage': round(percentage, 1),
                'sample_missing': sorted(list(missing))[:10] if missing else []
            }

        # Check critical keys if provided
        if required_keys:
            for key in required_keys:
                missing_in = []
                for language in self.supported_languages:
                    if key not in self.translations.get(language, {}):
                        missing_in.append(language)

                if missing_in:
                    stats['critical_keys_missing'].append({
                        'key': key,
                        'missing_in_languages': missing_in
                    })

        return stats

    async def add_translation(self, language: str, key: str, value: str):
        """Add new translation to database"""
        query = """
        INSERT INTO translations (language, key, value, category, is_active)
        VALUES ($1, $2, $3, 'telegram', TRUE)
        ON CONFLICT (key, language)
        DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
        """
        await db_manager.execute(query, language, key, value)

        # Update in-memory cache
        if language not in self.translations:
            self.translations[language] = {}
        self.translations[language][key] = value

    async def get_user_language(self, telegram_id: int) -> str:
        """Get user's preferred language"""
        query = """
        SELECT preferred_language FROM users WHERE telegram_id = $1
        """
        language = await db_manager.fetchval(query, str(telegram_id))
        # Normalised here too: callers pass this straight into keyboards and
        # date formatting, not only into `get()`.
        return self.normalize_language(language)

    def get_language_flag(self, language_code: str) -> str:
        """Get flag emoji for language"""
        flags = getattr(config.localization, 'language_flags', None) or {
            'en': '🇺🇸',
            'uz': '🇺🇿',
            'ru': '🇷🇺'
        }
        return flags.get(language_code, '🌐')

    def get_language_name(self, language_code: str, display_language: str = None) -> str:
        """Get language name in specified display language"""
        if not display_language:
            display_language = language_code

        names = getattr(config.localization, 'language_names', None) or {
            'en': {'en': 'English', 'uz': 'Inglizcha', 'ru': 'Английский'},
            'uz': {'en': 'Uzbek', 'uz': 'O\'zbekcha', 'ru': 'Узбекский'},
            'ru': {'en': 'Russian', 'uz': 'Ruscha', 'ru': 'Русский'}
        }

        return names.get(language_code, {}).get(display_language, language_code)


# Global translation instance
i18n = Translation()
