"""Every string the typed-quantity path renders must be real copy.

Same precedent as ``test_agent_order_copy_is_seeded.py``:
``shared.i18n_rendering.render_translation`` NEVER raises. Drop a language or
rename ``{min_qty}`` and nothing fails — the customer who typed "1" is simply
told a humanised key instead of the product's minimum, and types "1" again.

The last test keeps this file honest: the keys the quantity screen renders must
be EXACTLY the keys listed here, so a new refusal cannot ship unseeded.
"""

from __future__ import annotations

import re
from pathlib import Path
from string import Formatter

import pytest

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

LANGUAGES = ("en", "uz", "ru")

# key -> the placeholders `ProductHandlers` passes (handlers/products.py:
# `_typed_quantity_refusal` and `_format_quantity_step_text`). Written by hand.
BOT_KEYS = {
    "telegram.products.type_quantity_hint": set(),
    "telegram.products.typed_quantity_below_min": {"min_qty"},
    "telegram.products.typed_quantity_above_max": {"max_qty"},
    "telegram.products.typed_quantity_not_whole": {"min_qty", "max_qty"},
}

_RENDERING_MODULE = Path(__file__).resolve().parents[2] / "telegram_bot" / "handlers" / "products.py"
_KEY_IN_SOURCE = re.compile(r"telegram\.products\.(?:type_quantity_hint|typed_quantity_[a-z_]+)")
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(text) if name}


@pytest.mark.parametrize("key", sorted(BOT_KEYS))
def test_every_typed_quantity_key_is_seeded_in_all_three_languages(key):
    assert key in BACKEND_TRANSLATIONS, f"{key} is rendered by the quantity screen but has no seed row"
    for language in LANGUAGES:
        assert BACKEND_TRANSLATIONS[key].get(language), f"{key} has no {language} copy"


@pytest.mark.parametrize("key", sorted(BOT_KEYS))
def test_every_typed_quantity_key_carries_exactly_the_values_it_is_passed(key):
    for language in LANGUAGES:
        assert _placeholders(BACKEND_TRANSLATIONS[key][language]) == BOT_KEYS[key], (
            f"{key} [{language}] does not carry the values the call site passes; "
            "shared.i18n_rendering degrades mismatched copy to the humanised key"
        )


@pytest.mark.parametrize("key", sorted(BOT_KEYS))
def test_the_russian_copy_is_actually_russian(key):
    assert _CYRILLIC.search(BACKEND_TRANSLATIONS[key]["ru"]), (
        f"{key} [ru] carries no Cyrillic: {BACKEND_TRANSLATIONS[key]['ru']!r}"
    )


def test_the_bot_renders_exactly_the_keys_this_file_lists():
    rendered = set(_KEY_IN_SOURCE.findall(_RENDERING_MODULE.read_text(encoding="utf-8")))

    assert rendered == set(BOT_KEYS), (
        "the typed-quantity keys the bot renders and the keys this file checks "
        f"have diverged: only in the bot {sorted(rendered - set(BOT_KEYS))}, "
        f"only in this file {sorted(set(BOT_KEYS) - rendered)}"
    )
