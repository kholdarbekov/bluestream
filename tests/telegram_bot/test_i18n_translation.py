"""Unit tests for telegram bot translation behavior."""

import pytest

from i18n import Translation


@pytest.mark.unit
class TestTranslation:
    def test_get_returns_requested_language_translation(self):
        tr = Translation()
        tr.translations = {"en": {"telegram.menu.products": "Products"}}

        assert tr.get("telegram.menu.products", "en") == "Products"

    def test_get_falls_back_to_fallback_language(self):
        tr = Translation()
        tr.fallback_language = "en"
        tr.translations = {
            "en": {"telegram.welcome": "Welcome"},
            "uz": {},
        }

        assert tr.get("telegram.welcome", "uz") == "Welcome"

    def test_get_generates_human_readable_fallback_and_tracks_missing(self):
        tr = Translation()
        tr.translations = {"en": {}}

        value = tr.get("telegram.profile.phone_number", "ru")

        assert value == "Phone number"
        assert "ru" in tr.missing_keys
        assert "telegram.profile.phone_number" in tr.missing_keys["ru"]

    def test_get_formats_translation_with_kwargs(self):
        tr = Translation()
        tr.translations = {"en": {"telegram.greeting": "Hello {name}!"}}

        assert tr.get("telegram.greeting", "en", name="Umar") == "Hello Umar!"

    def test_check_completeness_reports_missing_keys_and_critical_gaps(self):
        tr = Translation()
        tr.supported_languages = ["en", "uz", "ru"]
        tr.translations = {
            "en": {"telegram.a": "A", "telegram.b": "B"},
            "uz": {"telegram.a": "A_UZ"},
            "ru": {"telegram.b": "B_RU"},
        }

        report = tr.check_completeness(required_keys=["telegram.a", "telegram.b", "telegram.c"])

        assert report["total_unique_keys"] == 2
        assert report["languages"]["uz"]["missing_keys"] == 1
        assert report["languages"]["ru"]["missing_keys"] == 1
        critical_keys = {item["key"] for item in report["critical_keys_missing"]}
        assert "telegram.c" in critical_keys

    def test_get_language_flag_and_name_have_safe_fallbacks(self):
        tr = Translation()

        assert tr.get_language_flag("en") in {"🇺🇸", "🌐"}
        assert tr.get_language_flag("xx") == "🌐"
        assert tr.get_language_name("uz", "en")
        assert tr.get_language_name("unknown", "en") == "unknown"
