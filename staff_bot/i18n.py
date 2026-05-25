"""
Internationalization (i18n) support for the Staff Bot
Multi-language support with translation management using 'staff_bot' category
"""
import logging
import re
from pathlib import Path
from typing import Dict, Any, Optional, List, Set

from staff_bot.config import config
from staff_bot.database import db_manager
from shared.staff_constants import FAILED_DELIVERY_REASONS, STAFF_BOT_ROLES
from shared.enums import OrderStatus, PaymentMethod

logger = logging.getLogger(__name__)


class Translation:
    """Translation management system for staff bot"""

    def __init__(self):
        self.translations: Dict[str, Dict[str, str]] = {}
        self.fallback_language = config.localization.fallback_language
        self.supported_languages = config.localization.supported_languages
        self.missing_keys: Dict[str, set] = {}
        self._missing_key_log_limit = 100
        self._required_keys_cache: Optional[Set[str]] = None

    def normalize_language(self, language: Optional[str]) -> str:
        """Normalize locale variants to one of the supported language codes."""
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
        """
        Load staff translations from DB.

        Primary source is `category='staff_bot'`, with `staff.*` key fallback
        to tolerate rows that were created with a non-staff category.
        """
        try:
            query = """
            SELECT language, key, value, category
            FROM translations
            WHERE is_active = TRUE
              AND (category = 'staff_bot' OR key LIKE 'staff.%')
            ORDER BY key, language, CASE WHEN category = 'staff_bot' THEN 0 ELSE 1 END
            """
            rows = await db_manager.fetchall(query)

            for row in rows:
                language = self.normalize_language(row['language'])
                key = row['key']
                value = row['value']

                if language not in self.translations:
                    self.translations[language] = {}
                # Preserve highest-priority row for a language/key pair.
                self.translations[language].setdefault(key, value)

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
        language = self.normalize_language(language)
        fallback_language = self.normalize_language(self.fallback_language)

        # Try requested language
        if language in self.translations and key in self.translations[language]:
            translation = self.translations[language][key]
        # Fallback to default language
        elif fallback_language in self.translations and key in self.translations[fallback_language]:
            translation = self.translations[fallback_language][key]
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
            language = self.normalize_language(language)
            return {language: sorted(list(self.missing_keys.get(language, set())))}
        return {lang: sorted(list(keys)) for lang, keys in self.missing_keys.items()}

    @staticmethod
    def _extract_literal_staff_keys(staff_root: Path) -> Set[str]:
        """Extract literal i18n keys used in staff bot source files."""
        pattern = re.compile(r"""i18n\.get\(\s*(['"])(staff\.[^'"]+)\1\s*[,)]""")
        keys: Set[str] = set()

        for path in staff_root.rglob("*.py"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue

            for _, key in pattern.findall(text):
                keys.add(key)

        return keys

    @staticmethod
    def _add_dynamic_family_keys(keys: Set[str]):
        """Add dynamic key families that are built via f-strings in handlers."""
        for role in STAFF_BOT_ROLES:
            keys.add(f"staff.role.{role}")

        for status in ("assigned", "picked_up", "in_transit", "arrived", "delivered", "failed"):
            keys.add(f"staff.delivery.status.{status}")

        for reason in FAILED_DELIVERY_REASONS:
            keys.add(f"staff.delivery.reason.{reason}")

        for payment in PaymentMethod:
            keys.add(f"staff.delivery.payment.{payment.value}")
            keys.add(f"staff.operator.payment_{payment.value}")

        for status in OrderStatus:
            keys.add(f"staff.order.status.{status.value}")

    def get_required_staff_keys(self, force_refresh: bool = False) -> Set[str]:
        """Return the full set of translation keys required by staff bot."""
        if self._required_keys_cache is not None and not force_refresh:
            return set(self._required_keys_cache)

        staff_root = Path(__file__).resolve().parent
        keys = self._extract_literal_staff_keys(staff_root)
        self._add_dynamic_family_keys(keys)
        self._required_keys_cache = set(keys)
        return keys

    def get_missing_translation_keys(self, languages: Optional[List[str]] = None) -> Dict[str, List[str]]:
        """Return required translation keys missing from loaded catalog per language."""
        languages = languages or list(self.supported_languages)
        required_keys = self.get_required_staff_keys()

        missing_by_language: Dict[str, List[str]] = {}
        for language in languages:
            language = self.normalize_language(language)
            available = self.translations.get(language, {})
            missing = sorted(key for key in required_keys if key not in available)
            if missing:
                missing_by_language[language] = missing

        return missing_by_language

    async def get_user_language(self, telegram_id: int) -> str:
        """Get user's preferred language"""
        query = "SELECT preferred_language FROM users WHERE telegram_id = $1"
        language = await db_manager.fetchval(query, str(telegram_id))
        return self.normalize_language(language)

    def get_language_flag(self, language_code: str) -> str:
        """Get flag emoji for language"""
        language_code = self.normalize_language(language_code)
        flags = {'en': '🇺🇸', 'uz': '🇺🇿', 'ru': '🇷🇺'}
        return flags.get(language_code, '🌐')

    def get_language_name(self, language_code: str, display_language: str = None) -> str:
        """Get language name in specified display language"""
        language_code = self.normalize_language(language_code)
        display_language = self.normalize_language(display_language or language_code)
        names = {
            'en': {'en': 'English', 'uz': 'Inglizcha', 'ru': 'Английский'},
            'uz': {'en': 'Uzbek', 'uz': "O'zbekcha", 'ru': 'Узбекский'},
            'ru': {'en': 'Russian', 'uz': 'Ruscha', 'ru': 'Русский'}
        }
        return names.get(language_code, {}).get(display_language, language_code)


# Global translation instance
i18n = Translation()
