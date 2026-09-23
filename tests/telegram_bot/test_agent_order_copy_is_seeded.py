"""Every string the agent-order proposal renders must be real copy.

Same precedent as ``test_loyalty_screen_copy_is_seeded.py``:
``shared.i18n_rendering.render_translation`` NEVER raises. Drop a language,
rename a placeholder, or add an eighth key to the call sites and nothing fails
today — the store just silently reads a humanised key (raw English debug text)
or a proposal missing its total, for a message that asks them to commit money.

The last test is the one that keeps this file honest: it reads the two modules
that render `telegram.agent_order.*` and asserts the keys they use are EXACTLY
the keys listed here, so a new key cannot be added to the bot and skipped in
the seed.
"""

from __future__ import annotations

import re
from pathlib import Path
from string import Formatter

import pytest

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

LANGUAGES = ("en", "uz", "ru")

# key -> the placeholders the call sites (telegram_bot/webhook_server.py
# ::agent_order_proposed_handler and telegram_bot/handlers/agent_orders.py)
# actually pass. Written by hand, so it is only half the chain: this file pins
# the SEED against it, and tests/telegram_bot/test_webhook_agent_order_proposed.py
# ::test_the_call_site_passes_exactly_the_placeholders_the_seeded_copy_carries
# pins the handler's real kwargs against the same seed rows. Note the
# deliberate `address` here vs `delivery_address` on the wire (R30).
BOT_KEYS = {
    "telegram.agent_order.proposed": {
        "agent_name", "order_number", "outlet_name", "address", "items", "total", "delivery_date",
    },
    "telegram.agent_order.item": {"name", "qty"},
    "telegram.agent_order.confirm_button": set(),
    "telegram.agent_order.decline_button": set(),
    "telegram.agent_order.confirmed": set(),
    "telegram.agent_order.declined": set(),
    "telegram.agent_order.not_pending": set(),
}

_RENDERING_MODULES = (
    Path(__file__).resolve().parents[2] / "telegram_bot" / "webhook_server.py",
    Path(__file__).resolve().parents[2] / "telegram_bot" / "handlers" / "agent_orders.py",
)

_KEY_IN_SOURCE = re.compile(r"telegram\.agent_order\.[a-z_]+")
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(text) if name}


@pytest.mark.parametrize("key", sorted(BOT_KEYS))
def test_every_agent_order_key_is_seeded_in_all_three_languages(key):
    assert key in BACKEND_TRANSLATIONS, (
        f"{key} is rendered by the agent-order proposal but has no seed row; "
        "the store would read the humanised key as English debug text"
    )
    for language in LANGUAGES:
        value = BACKEND_TRANSLATIONS[key].get(language)
        assert value, f"{key} has no {language} copy"


@pytest.mark.parametrize("key", sorted(BOT_KEYS))
def test_every_agent_order_key_carries_exactly_the_values_it_is_passed(key):
    for language in LANGUAGES:
        assert _placeholders(BACKEND_TRANSLATIONS[key][language]) == BOT_KEYS[key], (
            f"{key} [{language}] does not carry the values the call site passes; "
            "shared.i18n_rendering degrades mismatched copy to the humanised key"
        )


@pytest.mark.parametrize("key", sorted(BOT_KEYS))
def test_the_russian_copy_is_actually_russian(key):
    """A ru row filled with the English string is invisible to every other
    check here and reads as untranslated to the customer."""
    assert _CYRILLIC.search(BACKEND_TRANSLATIONS[key]["ru"]), (
        f"{key} [ru] carries no Cyrillic: {BACKEND_TRANSLATIONS[key]['ru']!r}"
    )


def test_the_bot_renders_exactly_the_keys_this_file_lists():
    """The guard on the guard. Without it, an eighth key added to the webhook
    or the handler is simply not checked by anything above."""
    rendered = set()
    for path in _RENDERING_MODULES:
        rendered |= set(_KEY_IN_SOURCE.findall(path.read_text(encoding="utf-8")))

    assert rendered == set(BOT_KEYS), (
        "the telegram.agent_order.* keys the bot renders and the keys this file "
        f"checks have diverged: only in the bot {sorted(rendered - set(BOT_KEYS))}, "
        f"only in this file {sorted(set(BOT_KEYS) - rendered)}"
    )
