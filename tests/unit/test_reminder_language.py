"""THE language a reorder reminder is written in (final review B1/E1, ruling: one rule, one default).

`shared.i18n_rendering.reminder_language(stored)` is the only answer. The backend asks it once per
send: the Telegram payload carries it as `language` and the customer bot renders in it, the email
is written in it, and the worklist publishes it as `recipient.reminder_language`. Its one default is
the system DEFAULT_LANGUAGE the customer bot falls back to (Telegram is the primary channel).
"""

import pytest

from shared import reorder_reminder_copy
from shared.i18n_rendering import SUPPORTED_LANGUAGES, reminder_language, system_default_language

pytestmark = pytest.mark.unit

# [stored `preferred_language`, the language the reminder is written in], under DEFAULT_LANGUAGE=uz.
CASES = [
    ("uz", "uz"),
    ("ru", "ru"),
    ("en", "en"),
    ("ru-RU", "ru"),
    ("ru_RU", "ru"),
    ("RU", "ru"),
    ("uz-Cyrl-UZ", "uz"),
    ("UZ_uz", "uz"),
    ("en-GB", "en"),
    ("  ru  ", "ru"),
    ("Russian", "ru"),
    ("english", "en"),
    ("uzbek", "uz"),
    (None, "uz"),
    ("", "uz"),
    ("   ", "uz"),
    ("de", "uz"),
    ("de_DE", "uz"),
    ("tr", "uz"),
    ("kk", "uz"),
    ("fr", "uz"),
]


@pytest.mark.parametrize("stored, expected", CASES)
def test_every_stored_value_reads_as_one_supported_language(monkeypatch, stored, expected):
    monkeypatch.setenv("DEFAULT_LANGUAGE", "uz")

    assert reminder_language(stored) == expected


@pytest.mark.parametrize("configured, expected", [("uz", "uz"), ("en", "en"), ("ru-RU", "ru"), ("RU", "ru")])
def test_the_one_default_is_the_system_default_language(monkeypatch, configured, expected):
    """The environment's DEFAULT_LANGUAGE, the setting the customer bot's own fallback reads."""
    monkeypatch.setenv("DEFAULT_LANGUAGE", configured)

    assert system_default_language() == expected
    assert reminder_language(None) == reminder_language("xx") == expected


@pytest.mark.parametrize("configured", [None, "", "fr"])
def test_an_unset_or_unsupported_system_default_reads_as_uzbek(monkeypatch, configured):
    """uz is the backend's own BaseConfig.DEFAULT_LANGUAGE, so a missing setting changes nothing."""
    if configured is None:
        monkeypatch.delenv("DEFAULT_LANGUAGE", raising=False)
    else:
        monkeypatch.setenv("DEFAULT_LANGUAGE", configured)

    assert system_default_language() == "uz"


def test_the_backend_default_and_the_seeded_languages_are_the_resolvers():
    from business_app.config.base import BaseConfig

    assert system_default_language.__module__ == "shared.i18n_rendering"
    assert BaseConfig.DEFAULT_LANGUAGE == "uz"
    assert set(SUPPORTED_LANGUAGES) == {"uz", "ru", "en"}
    # The reminder copy is seeded, and its items named, in exactly the resolver's languages.
    assert reorder_reminder_copy.LANGUAGES is SUPPORTED_LANGUAGES
