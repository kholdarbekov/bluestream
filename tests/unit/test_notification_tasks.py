"""Unit tests for notification task edge cases and payload contracts."""

from unittest.mock import MagicMock, patch

import pytest

from business_app.models.user import User
from business_app.tasks import notification_tasks
from business_app.utils.constants import NotificationChannel, NotificationType
from business_app.utils.password_security import hash_password


@pytest.mark.unit
class TestNotificationTasks:
    def test_send_verification_email_task_user_not_found(self, app, db):
        with app.app_context():
            result = notification_tasks.send_verification_email_task.run(999999, "token-123")

        assert result["success"] is False
        assert result["error"] == "User not found"

    def test_send_verification_email_task_uses_email_channel(self, app, sample_user):
        with (
            app.app_context(),
            patch("business_app.tasks.notification_tasks.NotificationService") as mock_service_cls,
        ):
            app.config["COMPANY_WEBSITE"] = "https://example.test"
            app.config["COMPANY_NAME"] = "Bluestream"
            mock_service = mock_service_cls.return_value
            mock_service.send_notification.return_value = {"email": {"success": True}}

            result = notification_tasks.send_verification_email_task.run(sample_user.id, "abc-token")

        assert result["email"]["success"] is True
        mock_service.send_notification.assert_called_once()
        args = mock_service.send_notification.call_args[0]
        assert args[0] == sample_user.id
        assert args[1] == NotificationType.EMAIL_VERIFICATION
        assert args[2] == [NotificationChannel.EMAIL]
        assert args[3]["verification_token"] == "abc-token"

    def test_send_verification_sms_task_with_explicit_phone_uses_direct_sms(
        self, app, sample_user
    ):
        with (
            app.app_context(),
            patch("business_app.tasks.notification_tasks.NotificationService") as mock_service_cls,
        ):
            app.config["COMPANY_NAME"] = "Bluestream"
            mock_service = mock_service_cls.return_value
            mock_service.send_sms_to_phone.return_value = {"success": True, "message_id": "sms-1"}

            result = notification_tasks.send_verification_sms_task.run(
                sample_user.id,
                "112233",
                "+998901234000",
            )

        assert result["success"] is True
        mock_service.send_sms_to_phone.assert_called_once()
        mock_service.send_notification.assert_not_called()

    def test_send_verification_sms_task_without_phone_returns_error(self, app, db):
        user = User(
            email="no-phone@example.com",
            phone=None,
            password_hash=hash_password("StrongPass123!"),
            first_name="NoPhone",
        )
        db.session.add(user)
        db.session.commit()

        with app.app_context():
            result = notification_tasks.send_verification_sms_task.run(user.id, "445566")

        assert result["success"] is False
        assert result["error"] == "No phone number available"

    def test_send_password_reset_sms_task_returns_error_when_user_has_no_phone(self, app, db):
        user = User(
            email="reset-no-phone@example.com",
            phone=None,
            password_hash=hash_password("StrongPass123!"),
            first_name="Reset",
        )
        db.session.add(user)
        db.session.commit()

        with app.app_context():
            result = notification_tasks.send_password_reset_sms_task.run(user.id, "778899")

        assert result["success"] is False
        assert result["error"] == "No phone number"

    def test_send_password_reset_sms_task_uses_expected_template(self, app, sample_user):
        with (
            app.app_context(),
            patch("business_app.tasks.notification_tasks.NotificationService") as mock_service_cls,
        ):
            app.config["COMPANY_NAME"] = "Bluestream"
            mock_service = mock_service_cls.return_value
            mock_service.send_sms_to_phone.return_value = {"success": True}

            result = notification_tasks.send_password_reset_sms_task.run(sample_user.id, "889900")

        assert result["success"] is True
        mock_service.send_sms_to_phone.assert_called_once()
        kwargs = mock_service.send_sms_to_phone.call_args.kwargs
        assert kwargs["template_key"] == "sms.password_reset.otp"
        assert kwargs["notification_type"] == NotificationType.PASSWORD_RESET

    def test_welcome_sms_task_no_longer_exists(self):
        """SMS is OTP-only and a welcome message is not a passcode, so the
        task is gone rather than left dormant."""
        assert not hasattr(notification_tasks, "send_welcome_sms_task")

    def test_send_registration_otp_task_uses_the_moderated_verification_template(self, app):
        """Web signup used to send its own `sms.registration.otp` text, which
        never passed Eskiz moderation — every send in the 30 days to
        2026-08-14 was rejected with HTTP 400. It now shares the one moderated
        OTP text."""
        with (
            app.app_context(),
            patch("business_app.tasks.notification_tasks.NotificationService") as mock_service_cls,
        ):
            mock_service = mock_service_cls.return_value
            mock_service.send_sms_to_phone.return_value = {"success": True, "message_id": "otp-1"}

            result = notification_tasks.send_registration_otp_task.run(
                "+998901231212", "123123", "ru"
            )

        assert result["success"] is True
        kwargs = mock_service.send_sms_to_phone.call_args.kwargs
        assert kwargs["template_key"] == "sms.verification.otp"
        assert kwargs["template_data"]["otp_code"] == "123123"
        assert kwargs["language"] == "ru"

    def test_account_locked_notification_sends_no_sms(self, app, sample_user):
        with (
            app.app_context(),
            patch("business_app.tasks.notification_tasks.NotificationService") as mock_service_cls,
        ):
            mock_service = mock_service_cls.return_value
            mock_service.send_notification.return_value = {"telegram": {"success": True}}

            notification_tasks.send_account_locked_notification_task.run(
                sample_user.id, "2026-08-14T12:00:00+00:00", 15
            )

        mock_service.send_sms_to_phone.assert_not_called()

    def test_send_delivery_update_task_uses_history_based_contract(self, app):
        with (
            app.app_context(),
            patch("business_app.tasks.notification_tasks.NotificationService") as mock_service_cls,
        ):
            mock_service = mock_service_cls.return_value
            mock_service.send_delivery_status_change_notification.return_value = {
                "telegram": {"success": True}
            }

            result = notification_tasks.send_delivery_update_task.run(321)

        assert result["telegram"]["success"] is True
        mock_service.send_delivery_status_change_notification.assert_called_once_with(321)

    def test_notify_driver_assignment_task_excludes_sms_channel(self, app):
        with (
            app.app_context(),
            patch("business_app.tasks.notification_tasks.Delivery") as mock_delivery_cls,
            patch("business_app.tasks.notification_tasks.NotificationService") as mock_service_cls,
        ):
            mock_delivery = MagicMock()
            mock_delivery.estimated_delivery_time = None
            mock_delivery_cls.query.get.return_value = mock_delivery
            mock_service = mock_service_cls.return_value
            mock_service.send_notification.return_value = {"telegram": {"success": True}}

            notification_tasks.notify_driver_assignment_task.run(99)

            channels = mock_service.send_notification.call_args.args[2]
            assert NotificationChannel.SMS not in channels
            assert NotificationChannel.TELEGRAM in channels


@pytest.mark.unit
class TestSendPaymentConfirmationTaskIdempotency:
    """``_allocate_to_payment`` now fires this task on every real cash
    collection, not just once ever per payment (a shortfall's second, later
    partial collection is just as real as the first — see
    cash_collection_service.py). The 24h Redis key below predates that: it
    assumed a payment could reach COMPLETED at most once, so a flat
    per-payment key was safe. These tests pin that a genuinely different
    collection (a different ``collection_state_token``) is not swallowed by
    an earlier collection's key, while a bare retry of the SAME send (same
    token, e.g. after a transient Celery failure) still is — and that callers
    which omit the token (online-rail payments, which only ever complete
    once) keep the original payment-only key unchanged.
    """

    def test_different_token_is_not_swallowed_by_an_earlier_send(self, app):
        with (
            app.app_context(),
            patch("business_app.tasks.notification_tasks.NotificationService") as mock_service_cls,
        ):
            mock_service_cls.return_value.send_payment_notification.return_value = {"success": True}
            fake_redis = MagicMock()
            fake_redis.get.return_value = None

            with patch("business_app.redis_client", fake_redis):
                notification_tasks.send_payment_confirmation_task.run(
                    501, collection_state_token="5000.00"
                )
                notification_tasks.send_payment_confirmation_task.run(
                    501, collection_state_token="8000.00"
                )

        assert mock_service_cls.return_value.send_payment_notification.call_count == 2
        get_keys = [call.args[0] for call in fake_redis.get.call_args_list]
        assert get_keys[0] != get_keys[1]
        assert get_keys[0] == "notif:payment_confirm:501:5000.00"
        assert get_keys[1] == "notif:payment_confirm:501:8000.00"

    def test_retry_with_the_same_token_is_swallowed(self, app):
        with (
            app.app_context(),
            patch("business_app.tasks.notification_tasks.NotificationService") as mock_service_cls,
        ):
            mock_service_cls.return_value.send_payment_notification.return_value = {"success": True}
            store = {}
            fake_redis = MagicMock()
            fake_redis.get.side_effect = lambda key: store.get(key)
            fake_redis.setex.side_effect = lambda key, ttl, value: store.__setitem__(key, value)

            with patch("business_app.redis_client", fake_redis):
                first = notification_tasks.send_payment_confirmation_task.run(
                    502, collection_state_token="5000.00"
                )
                second = notification_tasks.send_payment_confirmation_task.run(
                    502, collection_state_token="5000.00"
                )

        assert mock_service_cls.return_value.send_payment_notification.call_count == 1
        assert first == {"success": True}
        assert second == {"success": True, "skipped": True, "reason": "already_sent"}

    def test_omitted_token_keeps_the_legacy_payment_only_key(self, app):
        """Online-rail payments (PaymentService) never pass a token and can
        only ever complete once — the key they get must be byte-identical to
        before this change."""
        with (
            app.app_context(),
            patch("business_app.tasks.notification_tasks.NotificationService") as mock_service_cls,
        ):
            mock_service_cls.return_value.send_payment_notification.return_value = {"success": True}
            fake_redis = MagicMock()
            fake_redis.get.return_value = None

            with patch("business_app.redis_client", fake_redis):
                notification_tasks.send_payment_confirmation_task.run(503)

        fake_redis.get.assert_called_once_with("notif:payment_confirm:503")
