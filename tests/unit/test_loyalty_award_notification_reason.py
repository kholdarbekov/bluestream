"""D2 — reason + balance propagate into loyalty notification template_data."""

from unittest.mock import patch

import pytest

from business_app.services.notification_service import NotificationService
from business_app.utils.constants import NotificationType


@pytest.mark.unit
def test_send_loyalty_notification_threads_reason_and_balance(db, sample_user):
    service = NotificationService()
    with patch.object(
        NotificationService, "send_notification", autospec=True
    ) as mock_send:
        mock_send.return_value = {"telegram": {"success": True}}
        service.send_loyalty_notification(
            sample_user.id,
            "earned",
            {"points": 30, "reason": "streak_bonus", "balance": 130},
            NotificationType.LOYALTY_REWARD,
        )
    assert mock_send.call_count == 1
    # autospec=True -> args[0] is self, args[1] user_id, args[2] notif_type,
    # args[3] channels, args[4] template_data.
    _self, user_id, notif_type, channels, template_data = mock_send.call_args.args
    assert user_id == sample_user.id
    assert notif_type == NotificationType.LOYALTY_REWARD
    assert template_data["points"] == 30
    assert template_data["reason"] == "streak_bonus"
    assert template_data["balance"] == 130
    assert template_data["event_type"] == "earned"


@pytest.mark.unit
def test_task_forwards_reason_and_balance_to_service(db, sample_user):
    from business_app.tasks.notification_tasks import send_loyalty_notification_task

    with patch.object(
        NotificationService, "send_loyalty_notification", autospec=True
    ) as mock_send:
        mock_send.return_value = {"telegram": {"success": True}}
        # Call the task body synchronously (.run bypasses Celery broker).
        send_loyalty_notification_task.run(
            sample_user.id,
            "earned",
            {"points": 30, "reason": "purchase", "balance": 200},
            "loyalty_reward",
        )
    assert mock_send.call_count == 1
    _self, user_id, event_type, data, notif_type = mock_send.call_args.args
    assert user_id == sample_user.id
    assert event_type == "earned"
    assert data == {"points": 30, "reason": "purchase", "balance": 200}
    assert notif_type == NotificationType.LOYALTY_REWARD
