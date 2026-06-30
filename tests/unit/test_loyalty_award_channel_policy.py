"""D4 — Telegram-else-email channel policy for AquaCoins award notifications."""

from unittest.mock import patch

import pytest

from business_app.services.notification_service import NotificationService
from business_app.utils.constants import NotificationChannel, NotificationType


@pytest.mark.unit
def test_connected_telegram_user_gets_only_telegram(db, sample_user):
    sample_user.telegram_id = 555000111
    sample_user.is_bot_active = True
    db.session.commit()

    service = NotificationService()
    with patch.object(
        NotificationService, "send_notification", autospec=True
    ) as mock_send:
        mock_send.return_value = {"telegram": {"success": True}}
        service.send_loyalty_notification(
            sample_user.id,
            "earned",
            {"points": 20, "reason": "purchase", "balance": 120},
            NotificationType.LOYALTY_REWARD,
        )
    _self, user_id, notif_type, channels, template_data = mock_send.call_args.args
    assert channels == [NotificationChannel.TELEGRAM]
    assert NotificationChannel.EMAIL not in channels
    assert NotificationChannel.SMS not in channels


@pytest.mark.unit
def test_user_without_telegram_gets_only_email(db, sample_user):
    sample_user.telegram_id = None
    sample_user.is_bot_active = False
    db.session.commit()

    service = NotificationService()
    with patch.object(
        NotificationService, "send_notification", autospec=True
    ) as mock_send:
        mock_send.return_value = {"email": {"success": True}}
        service.send_loyalty_notification(
            sample_user.id,
            "earned",
            {"points": 20, "reason": "purchase", "balance": 120},
            NotificationType.LOYALTY_REWARD,
        )
    _self, user_id, notif_type, channels, template_data = mock_send.call_args.args
    assert channels == [NotificationChannel.EMAIL]
    assert NotificationChannel.TELEGRAM not in channels
    assert NotificationChannel.SMS not in channels


@pytest.mark.unit
def test_resolve_award_channels_helper():
    from types import SimpleNamespace

    service = NotificationService()
    connected = SimpleNamespace(telegram_id=123, is_bot_active=True)
    assert service._resolve_loyalty_award_channels(connected) == [NotificationChannel.TELEGRAM]
    no_bot = SimpleNamespace(telegram_id=None, is_bot_active=False)
    assert service._resolve_loyalty_award_channels(no_bot) == [NotificationChannel.EMAIL]
    # telegram_id present but bot inactive -> email (mirrors delivery policy).
    inactive = SimpleNamespace(telegram_id=123, is_bot_active=False)
    assert service._resolve_loyalty_award_channels(inactive) == [NotificationChannel.EMAIL]


@pytest.mark.unit
def test_non_earned_event_type_channels_is_none(db, sample_user):
    """Non-'earned' event types must NOT apply the Telegram-else-email policy."""
    # Give user a connected telegram to confirm the policy is NOT applied
    sample_user.telegram_id = 777000222
    sample_user.is_bot_active = True
    db.session.commit()

    service = NotificationService()
    with patch.object(
        NotificationService, "send_notification", autospec=True
    ) as mock_send:
        mock_send.return_value = {"email": {"success": True}}
        service.send_loyalty_notification(
            sample_user.id,
            "redeemed",
            {"points": 10, "balance": 90},
            NotificationType.LOYALTY_REWARD,
        )
    _self, user_id, notif_type, channels, template_data = mock_send.call_args.args
    # channels must be None (preserve existing default behavior for non-award events)
    assert channels is None


@pytest.mark.unit
def test_no_reason_call_uses_generic_label(db, sample_user):
    """Omitting 'reason' must inject the generic fallback label, not leave {reason_label} literal."""
    service = NotificationService()
    with patch.object(
        NotificationService, "send_notification", autospec=True
    ) as mock_send:
        mock_send.return_value = {"email": {"success": True}}
        service.send_loyalty_notification(
            sample_user.id,
            "earned",
            {"points": 15, "balance": 115},
            NotificationType.LOYALTY_REWARD,
        )
    _self, user_id, notif_type, channels, template_data = mock_send.call_args.args
    # reason_label should be injected with the generic fallback (not a literal placeholder)
    assert "reason_label" in template_data
    assert template_data["reason_label"] != "{reason_label}"
    # The generic fallback for an unknown/empty reason contains "AquaCoins"
    assert "AquaCoins" in template_data["reason_label"]
