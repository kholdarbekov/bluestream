"""The strings the subscription day-and-slot screens render must be real, trilingual copy
(plan 2026-10-24 Task 4; spec 2026-10-23 §2.9, §9).

``render_translation`` never raises: a missing language or a key that was never seeded shows the customer a
humanised key ("Select day of week") in the one place they choose when their water arrives. The old card
read bare keys (``frequency_weekly``, ``subscription_status_active``) that exist nowhere in the database, so
it printed the raw enum; the last test keeps that from coming back.
"""

import re
from pathlib import Path

import pytest

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for

LANGUAGES = ("en", "uz", "ru")
BOT_ROOT = Path(__file__).resolve().parents[2] / "telegram_bot"

KEYS = (
    ["telegram.subscription.select_day_of_week", "telegram.subscription.select_day_of_month"]
    + ["telegram.subscription.select_time_slot", "telegram.subscription.any_time"]
    + ["telegram.subscription.delivery_day", "telegram.subscription.delivery_time"]
    + ["telegram.subscription.time_to_be_confirmed"]
    # The bot reading the backend's published answers (plan 2026-10-26 Task 5).
    + [f"telegram.subscription.{n}" for n in (
        "cancel_button", "cancel_confirm", "cancel_confirm_yes", "cancel_confirm_no", "skip_stale",
        "change_time_slot", "select_new_time_slot", "no_time_slots",
        "schedule_updated_successfully", "items_updated_successfully", "preview_title", "save_change",
    )]
    # The card's money line reads the backend's estimate (final fix wave F4).
    + ["telegram.subscription.per_delivery"]
    # The card prints the backend's published words around the bot's own labels, and the history screen (step 4 Task 14).
    + [f"telegram.subscription.{n}" for n in (
        "next_dates", "if_resume", "date_skipped", "per_month", "payment", "orders", "history_title", "history_empty",
    )]
    # Items the backend publishes as unavailable are left out, and both screens say so (re-review G10).
    + [f"telegram.subscription.{n}" for n in ("item_not_included", "some_unavailable_note", "none_available_note")]
    + [f"telegram.subscription.weekday_{n}" for n in range(7)]
    + [f"telegram.subscription.status_{s}" for s in ("active", "paused", "cancelled", "expired", "trial")]
    + [f"telegram.subscription.frequency_{f}" for f in ("daily", "weekly", "biweekly", "monthly")]
)
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_PHONE_COPY = re.compile(r"call|позвон|qo'ng'iroq|qoʻngʻiroq|телефон|telefon", re.IGNORECASE)


@pytest.mark.parametrize("key", KEYS)
def test_every_key_is_seeded_in_all_three_languages_under_the_bots_category(key):
    assert key in BACKEND_TRANSLATIONS, f"{key} has no seed row"
    for language in LANGUAGES:
        assert BACKEND_TRANSLATIONS[key].get(language), f"{key} has no {language} copy"
        assert "{" not in BACKEND_TRANSLATIONS[key][language], f"{key} [{language}] carries a placeholder"
    assert _category_for(key) == "telegram"  # telegram_bot/i18n.py loads ONLY this category


@pytest.mark.parametrize("key", KEYS)
def test_the_russian_copy_is_russian_and_no_copy_says_call(key):
    assert _CYRILLIC.search(BACKEND_TRANSLATIONS[key]["ru"]), f"{key} [ru] carries no Cyrillic"
    for language in LANGUAGES:
        assert not _PHONE_COPY.search(BACKEND_TRANSLATIONS[key][language]), f"{key} [{language}]"


def test_the_weekday_names_follow_the_backends_zero_is_monday():
    names = [BACKEND_TRANSLATIONS[f"telegram.subscription.weekday_{n}"]["en"] for n in range(7)]
    assert names == ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def test_no_screen_reads_the_bare_frequency_or_status_keys_again():
    for name in ("handlers/subscriptions.py", "keyboards.py"):
        source = (BOT_ROOT / name).read_text()
        assert not re.search(r"""f?["']frequency_\{""", source), f"{name} reads a bare frequency_ key"
        assert "subscription_status_" not in source, f"{name} reads a bare subscription_status_ key"
        assert "delivery_frequency" not in source, f"{name} still reads the retired delivery_frequency"
        assert "billing_cycle" not in source, f"{name} still reads the retired billing_cycle"
