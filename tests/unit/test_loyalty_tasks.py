"""Task-level regressions for loyalty background jobs."""

from unittest.mock import patch

import pytest

from business_app.tasks import loyalty_tasks


@pytest.mark.unit
class TestLoyaltyTasks:
    def test_expire_loyalty_points_reads_task_compatible_keys(self, app):
        with (
            app.app_context(),
            patch("business_app.tasks.loyalty_tasks.LoyaltyService") as loyalty_service_cls,
        ):
            loyalty_service_cls.return_value.expire_points.return_value = {
                "total_expired_points": 320,
                "affected_users": 4,
            }

            result = loyalty_tasks.expire_loyalty_points.run()

        assert result["success"] is True
        assert result["expired_points"] == 320
        assert result["affected_users"] == 4

    def test_process_pending_referral_rewards_uses_service_contract(self, app):
        with (
            app.app_context(),
            patch("business_app.tasks.loyalty_tasks.LoyaltyService") as loyalty_service_cls,
        ):
            loyalty_service_cls.return_value.process_pending_referrals.return_value = {
                "processed_count": 2,
                "total_points_awarded": 150,
            }

            result = loyalty_tasks.process_pending_referral_rewards.run()

        assert result["success"] is True
        assert result["processed_count"] == 2
        assert result["total_points_awarded"] == 150

    def test_process_daily_surprise_rewards_uses_service_contract(self, app):
        with (
            app.app_context(),
            patch("business_app.tasks.loyalty_tasks.LoyaltyService") as loyalty_service_cls,
        ):
            loyalty_service_cls.return_value.process_daily_surprise_rewards.return_value = {
                "candidates": 7,
                "awarded": 3,
            }

            result = loyalty_tasks.process_daily_surprise_rewards.run()

        loyalty_service_cls.return_value.process_daily_surprise_rewards.assert_called_once_with()
        assert result["success"] is True
        assert result["candidates"] == 7
        assert result["awarded"] == 3

    def test_send_points_expiring_soon_reminders_sends_expected_template_data(self, app):
        with (
            app.app_context(),
            patch("business_app.tasks.loyalty_tasks.LoyaltyService") as loyalty_service_cls,
            patch("business_app.tasks.loyalty_tasks.NotificationService") as notification_service_cls,
        ):
            loyalty_service_cls.return_value.get_points_expiring_soon.return_value = [
                {"user_id": 11, "expiring_points": 75, "expiry_date": None},
            ]
            notification_service = notification_service_cls.return_value

            result = loyalty_tasks.send_points_expiring_soon_reminders.run()

        assert result["success"] is True
        assert result["sent_count"] == 1
        notification_service.send_notification.assert_called_once_with(
            11,
            "points_expiring_soon",
            template_data={
                "expiring_points": 75,
                "expiry_date": None,
                "days_remaining": 7,
            },
        )

    def test_update_loyalty_tiers_sends_upgrade_and_downgrade_notifications(self, app):
        with (
            app.app_context(),
            patch("business_app.tasks.loyalty_tasks.LoyaltyService") as loyalty_service_cls,
            patch("business_app.tasks.loyalty_tasks.NotificationService") as notification_service_cls,
        ):
            loyalty_service_cls.return_value.update_all_tiers.return_value = {
                "upgrades": [
                    {"user_id": 21, "old_tier": "Silver", "new_tier": "Gold", "benefits": ["Priority support"]},
                ],
                "downgrades": [
                    {
                        "user_id": 22,
                        "old_tier": "Gold",
                        "new_tier": "Silver",
                        "points_needed_for_restore": 100,
                    },
                ],
            }
            notification_service = notification_service_cls.return_value

            result = loyalty_tasks.update_loyalty_tiers.run()

        assert result["success"] is True
        assert result["upgrades_count"] == 1
        assert result["downgrades_count"] == 1
        assert notification_service.send_notification.call_count == 2
        first_call = notification_service.send_notification.call_args_list[0]
        second_call = notification_service.send_notification.call_args_list[1]
        assert first_call.args[0] == 21
        assert first_call.args[1] == "tier_upgraded"
        assert second_call.args[0] == 22
        assert second_call.args[1] == "tier_downgraded"
