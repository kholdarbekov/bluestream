"""Tests for FiscalizationAlertService — best-effort Slack + email fan-out."""

from unittest.mock import MagicMock, patch

import pytest

from business_app import db
from business_app.models.user import User
from business_app.services.fiscalization_alert_service import FiscalizationAlertService
from shared.enums import UserRole, UserStatus, UserType


def _make_user(email, role, status=UserStatus.ACTIVE):
    user = User(
        email=email,
        phone=f"+9989{abs(hash(email)) % 100000000:08d}",
        password_hash="x",
        first_name="T",
        last_name="U",
        user_type=UserType.STAFF,
        role=role,
        status=status,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.mark.unit
class TestNotifyTokenRefreshFailed:
    def test_posts_slack_and_emails_active_admins_and_managers_only(self, app, db):
        app.config["COMPANY_TIN"] = "306522134"
        admin = _make_user("a1@example.com", UserRole.ADMIN)
        manager = _make_user("m1@example.com", UserRole.MANAGER)
        _make_user("c1@example.com", UserRole.CUSTOMER)  # excluded: role
        _make_user("a2@example.com", UserRole.ADMIN, status=UserStatus.INACTIVE)  # excluded: status

        mock_ns = MagicMock()
        with patch("business_app.services.fiscalization_alert_service.send_slack_alert") as mock_slack, \
             patch("business_app.services.fiscalization_alert_service.NotificationService", return_value=mock_ns):
            FiscalizationAlertService().notify_token_refresh_failed(
                "http_error", status_code=401, body="Unauthorized"
            )

        # Slack posted exactly once, with the TIN + status in the text
        assert mock_slack.call_count == 1
        slack_text = mock_slack.call_args.args[0]
        assert "306522134" in slack_text
        assert "401" in slack_text

        # Email sent to the two eligible recipients only
        emailed_ids = {c.args[0] for c in mock_ns.send_notification.call_args_list}
        assert emailed_ids == {admin.id, manager.id}
        # Forced EMAIL channel + correct notification type
        first = mock_ns.send_notification.call_args_list[0]
        assert first.args[1] == "tax_committee_token_refresh_failed"
        from business_app.utils.constants import NotificationChannel
        assert first.kwargs["channels"] == [NotificationChannel.EMAIL]
        assert first.kwargs["template_data"]["reason"] == "http_error"
        assert first.kwargs["template_data"]["status_code"] == 401

    def test_email_failure_does_not_stop_slack_and_never_raises(self, app, db):
        _make_user("a1@example.com", UserRole.ADMIN)
        mock_ns = MagicMock()
        mock_ns.send_notification.side_effect = RuntimeError("brevo down")
        with patch("business_app.services.fiscalization_alert_service.send_slack_alert") as mock_slack, \
             patch("business_app.services.fiscalization_alert_service.NotificationService", return_value=mock_ns):
            # Must not raise despite the email backend blowing up
            FiscalizationAlertService().notify_token_refresh_failed("empty_token")

        assert mock_slack.call_count == 1

    def test_never_raises_when_slack_and_recipient_lookup_fail(self, app, db):
        # No admins in DB, and Slack raises — method still returns cleanly.
        with patch(
            "business_app.services.fiscalization_alert_service.send_slack_alert",
            side_effect=RuntimeError("boom"),
        ):
            FiscalizationAlertService().notify_token_refresh_failed("http_error", status_code=500)

    def test_outer_backstop_swallows_build_context_error(self, app, db):
        # _build_context is not wrapped by an inner handler, so making it raise
        # exercises the outer try/except in notify_token_refresh_failed.
        with patch.object(FiscalizationAlertService, "_build_context", side_effect=RuntimeError("boom")):
            # Must not raise.
            FiscalizationAlertService().notify_token_refresh_failed("http_error", status_code=500)

    def test_send_task_forwards_to_service(self, app, db):
        from business_app.tasks.notification_tasks import (
            send_tax_committee_token_refresh_alert_task,
        )
        with patch(
            "business_app.services.fiscalization_alert_service.FiscalizationAlertService"
        ) as mock_cls:
            # .apply() runs the task synchronously (eager) with app context.
            send_tax_committee_token_refresh_alert_task.apply(args=["http_error", 401, "Unauthorized"])
        mock_cls.return_value.notify_token_refresh_failed.assert_called_once_with(
            "http_error", status_code=401, body="Unauthorized"
        )
