"""Unit tests for notification task edge cases and payload contracts."""

from unittest.mock import patch

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

    def test_send_registration_otp_task_calls_sms_for_phone_without_user(self, app):
        with (
            app.app_context(),
            patch("business_app.tasks.notification_tasks.NotificationService") as mock_service_cls,
        ):
            app.config["COMPANY_NAME"] = "Bluestream"
            mock_service = mock_service_cls.return_value
            mock_service.send_sms_to_phone.return_value = {"success": True, "message_id": "otp-1"}

            result = notification_tasks.send_registration_otp_task.run(
                "+998901231212",
                "123123",
                "uz",
            )

        assert result["success"] is True
        kwargs = mock_service.send_sms_to_phone.call_args.kwargs
        assert kwargs["template_key"] == "sms.registration.otp"
        assert kwargs["notification_type"] == NotificationType.SYSTEM

    def test_send_welcome_sms_task_user_not_found(self, app, db):
        with app.app_context():
            result = notification_tasks.send_welcome_sms_task.run(777777)

        assert result["success"] is False
        assert result["error"] == "User not found"

    def test_send_welcome_sms_task_uses_welcome_template(self, app, sample_user):
        with (
            app.app_context(),
            patch("business_app.tasks.notification_tasks.NotificationService") as mock_service_cls,
        ):
            app.config["COMPANY_NAME"] = "Bluestream"
            mock_service = mock_service_cls.return_value
            mock_service.send_sms_to_phone.return_value = {"success": True}

            result = notification_tasks.send_welcome_sms_task.run(sample_user.id)

        assert result["success"] is True
        kwargs = mock_service.send_sms_to_phone.call_args.kwargs
        assert kwargs["template_key"] == "sms.welcome"
