"""Spec §6: Cancel and Enter-manually must match the SEEDED copy, not an
English word that only appears in the English string.

Today `telegram_bot/bot.py:503` matches `.*(cancel|❌ cancel).*` while the DB
holds `❌ Bekor qilish` / `❌ Отмена` — so Cancel is dead in uz and ru, and the
tap escapes ADDRESS_LOCATION into the group-0 catch-all, where it is silently
filed as a support ticket with no reply."""

import re

import pytest

# NOTE: the plan brief's exact test source names the class `TelegramBot`, but
# telegram_bot/bot.py's actual class is `WaterBusinessBot` — there is no
# `TelegramBot` symbol anywhere in telegram_bot/. Importing the real name here;
# see the Task 11 report for the discrepancy note.
from bot import WaterBusinessBot

pytestmark = pytest.mark.unit

LABELS = {"uz": "❌ Bekor qilish", "ru": "❌ Отмена", "en": "❌ Cancel"}


@pytest.fixture
def seeded_cancel_copy(monkeypatch):
    monkeypatch.setattr("i18n.i18n.supported_languages", ["uz", "ru", "en"])
    monkeypatch.setattr(
        "i18n.i18n.get",
        lambda key, language=None, *a, **kw: LABELS.get(language, LABELS["en"]),
    )


def test_cancel_pattern_matches_every_language(seeded_cancel_copy):
    pattern = WaterBusinessBot._label_pattern("telegram.cancel")
    for language, label in LABELS.items():
        assert re.match(pattern, label), f"{label!r} ({language}) must match"


def test_cancel_pattern_is_anchored(seeded_cancel_copy):
    """The substring patterns this replaces would swallow an ordinary sentence."""
    pattern = WaterBusinessBot._label_pattern("telegram.cancel")
    assert not re.match(pattern, "I would like to cancel my order please")


def test_pattern_never_matches_when_copy_is_missing(monkeypatch):
    """An empty label list must produce a pattern that matches NOTHING — a
    naive empty alternation would match every message ever sent."""
    monkeypatch.setattr("i18n.i18n.supported_languages", ["uz", "ru", "en"])
    monkeypatch.setattr("i18n.i18n.get", lambda key, language=None, *a, **kw: "")

    pattern = WaterBusinessBot._label_pattern("telegram.cancel")
    assert not re.match(pattern, "")
    assert not re.match(pattern, "anything at all")
