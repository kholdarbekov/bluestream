"""D3 — loyalty_reward telegram template + reason-specific localized rendering."""

import pytest

from business_app.services.notification_service import (
    DEFAULT_TEMPLATES,
    NotificationService,
)
from business_app.utils.constants import NotificationChannel, NotificationType


@pytest.mark.unit
def test_default_templates_has_loyalty_reward_telegram():
    assert ("loyalty_reward", "telegram") in DEFAULT_TEMPLATES
    cfg = DEFAULT_TEMPLATES[("loyalty_reward", "telegram")]
    for lang in ("uz", "ru", "en"):
        assert "content" in cfg["translations"][lang]
        assert "AquaCoins" in cfg["translations"][lang]["content"]


@pytest.mark.unit
def test_build_default_template_returns_telegram_loyalty_template():
    service = NotificationService()
    tmpl = service._build_default_notification_template("loyalty_reward", "telegram")
    assert tmpl is not None
    assert tmpl.channel == "telegram"
    # Trilingual content resolvable via get_translated.
    assert "{points}" in tmpl.get_translated("content", "en")
    assert "{balance}" in tmpl.get_translated("content", "en")


@pytest.mark.unit
@pytest.mark.parametrize(
    "reason,language,expected_label",
    [
        ("welcome_bonus", "en", "Welcome bonus"),
        ("welcome_bonus", "uz", "Xush kelibsiz bonusi"),
        ("welcome_bonus", "ru", "Приветственный бонус"),
        ("purchase", "en", "Purchase"),
        ("referral", "ru", "Реферал"),
        ("streak_bonus", "uz", "Streak bonusi"),
        # actual enum value is consecutive_streak_bonus (not consecutive_strike_bonus)
        ("consecutive_streak_bonus", "en", "Consecutive streak bonus"),
        ("birthday_bonus", "en", "Birthday bonus"),
        ("surprise_reward", "en", "Surprise reward"),
        ("totally_new_future_type", "en", "AquaCoins reward"),  # generic fallback
    ],
)
def test_reason_label_localization(reason, language, expected_label):
    assert NotificationService._loyalty_reason_label(reason, language) == expected_label


@pytest.mark.unit
def test_render_telegram_template_is_reason_specific_per_language():
    service = NotificationService()
    tmpl = service._build_default_notification_template("loyalty_reward", "telegram")
    content_en = tmpl.get_translated("content", "en")
    rendered = service._render_template(
        content_en,
        {
            "points": 30,
            "balance": 130,
            "reason_label": NotificationService._loyalty_reason_label("streak_bonus", "en"),
        },
        "en",
    )
    assert "30 AquaCoins" in rendered
    assert "Streak bonus" in rendered
    assert "130" in rendered
