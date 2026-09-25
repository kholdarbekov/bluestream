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
from shared.staff_constants import (
    FAILED_DELIVERY_REASONS,
    RECONCILIATION_RISK_FLAGS,
    SALES_EVENTS,
    STAFF_BOT_ROLES,
)
from shared.i18n_rendering import humanise_key, render_translation
from shared.enums import (
    DeliveryStatus,
    DriverBottleSessionStatus,
    DriverCashSessionStatus,
    OrderStatus,
    PaymentMethod,
)

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

    def get(self, key: str, language: str = None, /, *args, **kwargs) -> str:
        """Get translation for key in specified language.

        ``key`` and ``language`` are POSITIONAL-ONLY on purpose, exactly as on
        the customer bot: everything a call site interpolates arrives in
        ``**kwargs``, so a parameter this method could bind by keyword would be
        a word the COPY may never use as a placeholder. ``{language}`` was
        unfillable that way -- ``get(key, code, language=name)`` raised
        ``TypeError: got multiple values for argument 'language'`` -- and
        behind the ``/`` the names are free for the copy again.

        Never raises and never emits an unresolved ``{placeholder}``: the
        rendering rule is ``shared.i18n_rendering.render_translation``, the same
        one ``telegram_bot/i18n.py`` uses, so one bad translation row cannot
        mean two different things on the two bots. Callers must pass their
        values HERE rather than calling ``.format()`` on the result — a template
        no longer survives the trip out of this method.
        """
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
            translation = humanise_key(key)

        return render_translation(key, translation, args, kwargs)

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
        """Add dynamic key families that are built via f-strings in handlers.

        Every family here MUST be derived from the enum that feeds the renderer.
        This function is the required-key set `/health` checks against, so a
        hand-written subset does not merely miss a translation — it makes the
        gap *undetectable*. That is exactly how `staff.delivery.status.cancelled`
        shipped: this list named six statuses, `DELIVERY_STATUS_TRANSITIONS`
        offers CANCELLED as a successor of all four active statuses, and so
        every active-delivery card rendered an English "Cancelled" button while
        /health stayed green.
        """
        for role in STAFF_BOT_ROLES:
            keys.add(f"staff.role.{role}")

        # The error renderer's TARGETS (`BaseHandler.error_copy_keys`: the code
        # map, the detail copy, the remedy-button labels and the backend-reason
        # frame). `BaseHandler._resolve_api_error_message`
        # resolves these at runtime out of a dict VALUE, so the literal
        # extractor's regex over literal `staff.*` arguments cannot see a
        # single one of them (and note that extractor scans COMMENTS too, so
        # this one must not spell the call out) -- twelve of the fifteen
        # `staff.sales.error.*` keys are addressed only that way. DERIVED
        # from the map for the same reason the delivery statuses are derived
        # from the enum: a hand-written subset here would not merely miss a
        # translation, it would make the gap undetectable. Imported inside the
        # function because `handlers/base.py` imports this module at import
        # time.
        from staff_bot.handlers.base import BaseHandler

        keys.update(BaseHandler.error_copy_keys())

        # Sales events: webhook_server.sales_event_handler builds the key with
        # f'staff.sales.notify.{event}' from the backend-supplied event name,
        # and refuses any event outside this same tuple. ONE vocabulary in
        # `shared/` rather than three copies (this loop, the allowlist and the
        # seed script's twin) — `shared/` is the only tree all three can import.
        for event in SALES_EVENTS:
            keys.add(f"staff.sales.notify.{event}")

        # The outlet card and its lists build these three families with
        # f-strings from BACKEND-supplied values: `keyboards/sales.py`
        # (stage_label) and `handlers/sales/hub.py` (the type label and the
        # per-scope list title). The stage/type tuples mirror
        # `business_app/models/sales.py::OUTLET_STAGES / OUTLET_TYPES`, which is
        # the SSOT — a value added there and not here renders the humanised key
        # tail to a sales agent while /health stays green.
        for stage in ("prospect", "trial", "activation_requested", "active", "at_risk", "dormant", "lost"):
            keys.add(f"staff.sales.stage.{stage}")

        for outlet_type in ("grocery_store", "workplace", "individual"):
            keys.add(f"staff.sales.type.{outlet_type}")

        for scope in ("due", "prospects", "all"):
            keys.add(f"staff.sales.list.title_{scope}")

        # The operator's rejection-reason buttons build their key with
        # f'staff.sales.approvals.reason.{reason}'. Derived from
        # `REJECT_REASONS`, which is the SSOT for which buttons
        # `SalesKeyboards.approval_actions` draws — a fifth reason added there
        # and hand-forgotten here would ship an English key tail to an operator
        # while /health stayed green. Imported inside the function because
        # `keyboards/sales.py` imports this module at import time.
        from staff_bot.keyboards.sales import (
            NEXT_VISIT_CHOICES,
            NO_ORDER_REASONS,
            PAY_METHODS,
            PHOTO_KINDS,
            REJECT_REASONS,
            STATS_PERIODS,
            VISIT_OUTCOMES,
        )

        for reason in REJECT_REASONS:
            keys.add(f"staff.sales.approvals.reason.{reason}")

        # The visit conversation's four picker families, from the SAME tuples
        # `SalesKeyboards.close_outcome` / `.no_order_reasons` /
        # `.payment_methods` / `.next_visit` draw their buttons from. The full
        # VISIT_OUTCOMES tuple is registered, not CLOSE_OUTCOMES: the outlet
        # card renders `order_placed` for the last visit even though it is
        # never a button.
        for outcome in VISIT_OUTCOMES:
            keys.add(f"staff.sales.visit.outcome.{outcome}")

        for reason in NO_ORDER_REASONS:
            keys.add(f"staff.sales.visit.reason.{reason}")

        for method in PAY_METHODS:
            keys.add(f"staff.sales.visit.pay.{method}")

        for choice in NEXT_VISIT_CHOICES:
            keys.add(f"staff.sales.visit.next.{choice}")

        # The photo picker's own family, from the tuple `SalesKeyboards.
        # photo_kind` draws its buttons from -- the same arrangement as the
        # four above, and the same reason: an f-string key is invisible to
        # the literal extractor, so only this loop makes a gap visible.
        for kind in PHOTO_KINDS:
            keys.add(f"staff.sales.visit.photo_kind.{kind}")

        # The period switch on the sales agent's KPI card, from the same tuple
        # `SalesKeyboards.stats` draws its three buttons from. An f-string key
        # again, so this loop is the only thing that makes a missing row
        # visible to /health -- the card's other twenty-seven keys are
        # literals the extractor finds on its own.
        for period in STATS_PERIODS:
            keys.add(f"staff.sales.stats.period_{period}")

        for status in DeliveryStatus:
            keys.add(f"staff.delivery.status.{status.value}")

        for status in DriverCashSessionStatus:
            keys.add(f"staff.delivery.cash_session_status.{status.value}")

        for status in DriverBottleSessionStatus:
            keys.add(f"staff.delivery.bottle_session_status.{status.value}")

        for flag in RECONCILIATION_RISK_FLAGS:
            keys.add(f"staff.delivery.risk_flag.{flag}")

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
