"""The admin-UI `subscriptions` namespace keys added for the Subscriptions page
must seed under category='ui_subscriptions' with bare keys, and actually
resolve through the real AdminUiTranslationService."""
import pytest

from business_app.services.admin_ui_translation_service import AdminUiTranslationService
from scripts.seed_ui_subscriptions_translations import (
    UI_SUBSCRIPTIONS_TRANSLATIONS,
    seed_ui_subscriptions_translations,
)


@pytest.mark.unit
def test_seed_ui_subscriptions_translations_resolve_via_subscriptions_namespace(app, db):
    with app.app_context():
        seed_ui_subscriptions_translations()

        en = AdminUiTranslationService.get_translations("en", "subscriptions")
        # en seed values must equal the Subscriptions.js inline defaultValue fallbacks
        assert en["create_button"] == "Create Subscription"
        assert en["section_overrides"] == "Danger zone / overrides"
        assert en["override_warning"] == (
            "Manual overrides can break automated billing. Use with care."
        )
        assert en["sub_updated"] == "Subscription updated"

        uz = AdminUiTranslationService.get_translations("uz", "subscriptions")
        assert uz["create_button"] == UI_SUBSCRIPTIONS_TRANSLATIONS["uz"]["create_button"]
        assert uz["section_overrides"] == UI_SUBSCRIPTIONS_TRANSLATIONS["uz"]["section_overrides"]
        assert uz["override_warning"] == UI_SUBSCRIPTIONS_TRANSLATIONS["uz"]["override_warning"]
        assert uz["sub_updated"] == "Obuna yangilandi"

        ru = AdminUiTranslationService.get_translations("ru", "subscriptions")
        assert ru["create_button"] == UI_SUBSCRIPTIONS_TRANSLATIONS["ru"]["create_button"]
        assert ru["section_overrides"] == UI_SUBSCRIPTIONS_TRANSLATIONS["ru"]["section_overrides"]
        assert ru["override_warning"] == UI_SUBSCRIPTIONS_TRANSLATIONS["ru"]["override_warning"]
        # Confirms the typo fix (brief draft had "обновлendi").
        assert ru["sub_updated"] == "Подписка обновлена"


# --- one frequency (spec 2026-10-23): the page asks for these keys, the seed ships them -------------

import re
from pathlib import Path

_PAGE = Path(__file__).resolve().parents[2] / "admin_ui" / "src" / "pages" / "Subscriptions.js"
_OLD_BILLING_KEYS = {"billing_cycle", "next_billing", "last_billing", "delivery_frequency"}


def _page_defaults() -> dict:
    """Every static `t('key', { defaultValue: '...' })` of the page."""
    source = _PAGE.read_text(encoding="utf-8")
    return {
        key: value.replace("\\'", "'")
        for key, value in re.findall(r"t\('(\w+)',\s*\{\s*defaultValue:\s*'((?:[^'\\]|\\.)*)'", source)
    }


@pytest.mark.unit
def test_every_static_default_on_the_page_equals_the_english_seed():
    en = UI_SUBSCRIPTIONS_TRANSLATIONS["en"]
    defaults = _page_defaults()
    assert {"frequency", "next_delivery", "bill_now", "override_dates", "sub_any_time", "sub_slot_or_any_time"} <= defaults.keys()
    assert {key: en.get(key) for key in defaults} == defaults


@pytest.mark.unit
@pytest.mark.parametrize("language", ["en", "uz", "ru"])
def test_the_billing_cycle_keys_are_gone_and_the_new_ones_exist_in_every_language(language):
    translations = UI_SUBSCRIPTIONS_TRANSLATIONS[language]
    assert not _OLD_BILLING_KEYS & translations.keys()
    for key in ("frequency", "next_delivery", "next_delivery_date", "sub_any_time", "sub_slot_or_any_time", "day_of_week"):
        assert translations[key]
    assert [translations[f"weekday_{i}"] for i in range(7)] == list(dict.fromkeys(translations[f"weekday_{i}"] for i in range(7)))
    assert set(UI_SUBSCRIPTIONS_TRANSLATIONS["en"]) == set(translations)


@pytest.mark.unit
def test_the_page_names_the_two_admin_actions_per_the_plan():
    en = UI_SUBSCRIPTIONS_TRANSLATIONS["en"]
    assert en["bill_now"] == "Create next order now"
    assert en["override_dates"] == "Set next delivery date"
    assert "Monday" == en["weekday_0"] and "Sunday" == en["weekday_6"]
