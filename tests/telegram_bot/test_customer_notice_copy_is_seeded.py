"""Every string the customer-notice route renders must be real copy (plan 2026-10-24 Task 3; spec 2026-10-23 §9).

Same precedent as ``test_agent_order_copy_is_seeded.py``: ``shared.i18n_rendering.render_translation`` never
raises. Drop a language or rename a placeholder and nothing fails; the customer silently reads a humanised key
(raw English debug text) in the one message that tells them an order was created, or why a delivery was
skipped. The last test keeps this file honest: the keys telegram_bot/webhook_server.py renders are EXACTLY the
keys listed here.
"""

from __future__ import annotations

import re
from pathlib import Path
from string import Formatter

import pytest

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for

LANGUAGES = ("en", "uz", "ru")

# key -> the placeholders telegram_bot/webhook_server.py::_customer_notice_text passes it.
BOT_KEYS = {
    "telegram.customer_notice.order_created": {"order_number", "delivery", "items", "total", "payment"},
    "telegram.customer_notice.delivery_window": {"date", "start", "end"},
    "telegram.customer_notice.delivery_tbc": {"date"},
    "telegram.customer_notice.delivery_any_time": {"date"},
    "telegram.customer_notice.item": {"name", "qty"},
    "telegram.customer_notice.not_included": {"name"},
    "telegram.customer_notice.pay_cash": {"amount"},
    "telegram.customer_notice.pay_cash_prepaid": {"amount"},
    "telegram.customer_notice.pay_cash_covered": set(),
    "telegram.customer_notice.pay_company_account": set(),
    "telegram.customer_notice.pay_online": {"deadline"},
    "telegram.customer_notice.pay_online_paid": set(),
    "telegram.customer_notice.delivery_skipped": {"subscription_name", "dates", "reason", "next_date"},
    "telegram.customer_notice.paid_order_kept_skip": {"order_number", "date", "next_date"},
    "telegram.customer_notice.paid_order_kept_pause": {"order_number", "date"},
    "telegram.customer_notice.paid_order_kept_cancel": {"order_number", "date"},
    "telegram.customer_notice.payment_expired": {"order_number", "deadline"},
    # A slot write changed the schedule (spec 2026-10-27 §6.5): one line per change inside `schedule_changed`.
    "telegram.customer_notice.schedule_changed": {"subscription_name", "lines"},
    "telegram.customer_notice.schedule_changed_next": {"date"},
    "telegram.customer_notice.schedule_change.time_cleared": {"old_window"},
    "telegram.customer_notice.schedule_change.time_changed": {"new_window", "old_window"},
    "telegram.customer_notice.schedule_change.day_moved": {"old_day", "new_day"},
    "telegram.customer_notice.schedule_change.days_added": {"days"},
    "telegram.customer_notice.schedule_change.date_moved": {"old_day"},
}

_RENDERING_MODULE = Path(__file__).resolve().parents[2] / "telegram_bot" / "webhook_server.py"
# Dotted to the end: `telegram.customer_notice.schedule_change.time_cleared` is one key, not its prefix.
_KEY_IN_SOURCE = re.compile(r"telegram\.customer_notice\.[a-z_]+(?:\.[a-z_]+)*")
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_PHONE_COPY = re.compile(r"call|позвон|qo'ng'iroq|qoʻngʻiroq|телефон|telefon", re.IGNORECASE)


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(text) if name}


@pytest.mark.parametrize("key", sorted(BOT_KEYS))
def test_every_key_is_seeded_in_all_three_languages_under_the_bots_category(key):
    assert key in BACKEND_TRANSLATIONS, f"{key} is rendered by the customer-notice route but has no seed row"
    for language in LANGUAGES:
        assert BACKEND_TRANSLATIONS[key].get(language), f"{key} has no {language} copy"
    # telegram_bot/i18n.py loads ONLY category 'telegram'.
    assert _category_for(key) == "telegram"


@pytest.mark.parametrize("key", sorted(BOT_KEYS))
def test_every_key_carries_exactly_the_values_it_is_passed(key):
    for language in LANGUAGES:
        assert _placeholders(BACKEND_TRANSLATIONS[key][language]) == BOT_KEYS[key], (
            f"{key} [{language}] does not carry the values the route passes; "
            "shared.i18n_rendering degrades mismatched copy to the humanised key"
        )


@pytest.mark.parametrize("key", sorted(BOT_KEYS))
def test_the_russian_copy_is_actually_russian(key):
    assert _CYRILLIC.search(BACKEND_TRANSLATIONS[key]["ru"]), f"{key} [ru] carries no Cyrillic"


@pytest.mark.parametrize("key", sorted(BOT_KEYS))
def test_no_notice_tells_the_customer_to_call(key):
    """Customer copy points to the bot chat, never the phone (owner rule)."""
    for language in LANGUAGES:
        assert not _PHONE_COPY.search(BACKEND_TRANSLATIONS[key][language]), f"{key} [{language}]"


def test_the_route_renders_exactly_the_keys_this_file_lists():
    rendered = set(_KEY_IN_SOURCE.findall(_RENDERING_MODULE.read_text(encoding="utf-8")))

    assert rendered == set(BOT_KEYS), (
        "the telegram.customer_notice.* keys the bot renders and the keys this file checks have diverged: "
        f"only in the bot {sorted(rendered - set(BOT_KEYS))}, only in this file {sorted(set(BOT_KEYS) - rendered)}"
    )
