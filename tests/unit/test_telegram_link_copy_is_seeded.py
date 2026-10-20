"""Every web-to-Telegram link string is seeded in en, uz and ru (spec 2026-10-19 D9).

Each value carries exactly the placeholders its caller fills, and none tells the customer to call
(Global Constraints). T3 (the customer bot) and T4 (the website) add their keys to KEYS.
"""

import re
from string import Formatter

import pytest

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, TELEGRAM_LINK_WEB_TRANSLATIONS

pytestmark = pytest.mark.unit

# key -> the placeholders its caller fills.
KEYS: dict[str, set[str]] = {
    "api.auth.telegram_link_not_available": set(),
    "api.auth.telegram_link_store_unavailable": set(),
    # The customer bot (D5, D8, D12, D15): handlers/profile.py and handlers/__init__.py.
    "telegram.web_link.confirm_prompt": {"account"},
    "telegram.web_link.yes_button": set(),
    "telegram.web_link.connected": {"name"},
    "telegram.web_link.expired": set(),
    "telegram.web_link.has_account": set(),
    "telegram.web_link.cancelled": set(),
    "telegram.web_link.failed": set(),
    "telegram.web_link.confirm_number_prompt": {"phone_masked"},
    "telegram.support.menu_intro": set(),
}
# The website (T4): dotless English keys rendered `|t(current_language)` on /my-account.
KEYS.update({key: set() for key in TELEGRAM_LINK_WEB_TRANSLATIONS})
_CALL = re.compile(r"\bcall\b|qo'ng'iroq|звон", re.IGNORECASE)


@pytest.mark.parametrize("key", sorted(KEYS))
def test_every_link_key_is_seeded_in_three_languages_with_its_placeholders_and_never_says_call(key):
    for language in ("en", "uz", "ru"):
        value = BACKEND_TRANSLATIONS[key][language]
        assert value and {n for _, n, _, _ in Formatter().parse(value) if n} == KEYS[key] and not _CALL.search(value)


@pytest.mark.parametrize("key", sorted(TELEGRAM_LINK_WEB_TRANSLATIONS))
def test_a_storefront_key_is_its_english_text(key):
    assert TELEGRAM_LINK_WEB_TRANSLATIONS[key]["en"] == key
