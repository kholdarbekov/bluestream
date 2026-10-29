"""Every surface says "Any time" in the same words (spec 2026-10-27 D14, D15): the API's sentence, the bot, the
cabinet, the landing builder and the admin form read one wording in en, uz and ru."""
import pytest

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from scripts.seed_ui_subscriptions_translations import UI_SUBSCRIPTIONS_TRANSLATIONS

ANY_TIME_KEYS = ("api.subscriptions.time.any_time", "telegram.subscription.any_time", "cabinet.subs.slot.any",
                 "landing.custom_subscription.any_time")


def ui_value(key):
    """{lang: value} of an admin `subscriptions` namespace key, from the en/uz/ru blocks of its seed."""
    return {language: UI_SUBSCRIPTIONS_TRANSLATIONS[language][key] for language in ("en", "uz", "ru")}


@pytest.mark.unit
def test_every_any_time_key_says_the_same_words():
    values = {k: BACKEND_TRANSLATIONS[k] for k in ANY_TIME_KEYS} | {"sub_any_time": ui_value("sub_any_time")}
    assert len({tuple(sorted(v.items())) for v in values.values()}) == 1
    assert values["api.subscriptions.time.any_time"] == {"en": "Any time", "uz": "Istalgan vaqtda", "ru": "В любое время"}
