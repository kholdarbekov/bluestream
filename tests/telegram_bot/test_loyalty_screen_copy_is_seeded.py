"""Every string the Telegram loyalty screen renders must be real copy.

Same precedent as ``test_tier_quote_copy_is_seeded.py``: ``shared.i18n_rendering
.render_translation`` NEVER raises. Drop a language, rename a placeholder, or
let a date sneak back into this screen's copy and nothing fails today — the
screen just silently degrades to a humanised key (raw English) or a value
missing its number, invisibly, for real customers.

The date check exists because this screen explicitly promises a tier does not
expire (see ``telegram_bot/handlers/loyalty.py``'s "No date" comment):
``tier_valid_until`` is a downgrade-guarantee floor that rolls forward on
every award, not an expiry, so no value here may contain a date-shaped token.
"""

from __future__ import annotations

import re
from string import Formatter

import pytest

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

LANGUAGES = ("en", "uz", "ru")

# key -> the placeholders the bot's call site (telegram_bot/handlers/loyalty.py
# loyalty_menu) actually passes.
BOT_KEYS = {
    "telegram.loyalty.qualifying_12m": set(),
    "telegram.loyalty.tier_line": {"tier"},
    "telegram.loyalty.tier_cod_perk": {"pct"},
    "telegram.loyalty.tier_secured": set(),
    "telegram.loyalty.tier_keep_hint": {"points"},
    "telegram.loyalty.to_next_tier": {"tier", "points"},
}

# Date-like placeholder tokens that would imply the tier expires.
_DATE_PLACEHOLDER_TOKENS = ("{date}", "{valid_until}", "{until}")
# A %-style strftime directive (%Y, %d, %m, ...) — NOT a bare literal percent
# sign, which tier_cod_perk legitimately uses right after the {pct} value.
_STRFTIME_DIRECTIVE = re.compile(r"%[a-zA-Z]")


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(text) if name}


@pytest.mark.parametrize("key", sorted(BOT_KEYS))
def test_every_loyalty_screen_key_is_seeded_in_all_three_languages(key):
    assert key in BACKEND_TRANSLATIONS, (
        f"{key} is rendered by the loyalty screen but has no seed row; "
        "customers would read the humanised key as English debug text"
    )
    for language in LANGUAGES:
        value = BACKEND_TRANSLATIONS[key].get(language)
        assert value, f"{key} has no {language} copy"


@pytest.mark.parametrize("key", sorted(BOT_KEYS))
def test_every_loyalty_screen_key_carries_exactly_the_values_it_is_passed(key):
    for language in LANGUAGES:
        assert _placeholders(BACKEND_TRANSLATIONS[key][language]) == BOT_KEYS[key], (
            f"{key} [{language}] does not carry the values the call site passes; "
            "shared.i18n_rendering degrades mismatched copy to the humanised key"
        )


@pytest.mark.parametrize("key", sorted(BOT_KEYS))
def test_no_loyalty_screen_key_implies_a_tier_expires(key):
    """A tier does not expire; ``tier_valid_until`` is a downgrade-guarantee
    floor, not an expiry date, so no value here may carry a date-shaped token."""
    for language in LANGUAGES:
        value = BACKEND_TRANSLATIONS[key][language]
        for token in _DATE_PLACEHOLDER_TOKENS:
            assert token not in value, (
                f"{key} [{language}] contains {token!r}: {value!r}. "
                "This screen must never imply a tier expires."
            )
        assert not _STRFTIME_DIRECTIVE.search(value), (
            f"{key} [{language}] contains a %-style date directive: {value!r}. "
            "This screen must never imply a tier expires."
        )
