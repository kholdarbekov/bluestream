"""
Loyalty-related Celery tasks for the Water Business Platform
Handles points expiration, reward processing, and loyalty notifications.
"""

from celery import shared_task
from celery.utils.log import get_task_logger
from datetime import datetime, timezone
from typing import Dict, Any

from business_app.services.loyalty_service import LoyaltyService
from business_app.services.notification_service import NotificationService

logger = get_task_logger(__name__)


@shared_task(time_limit=1800, soft_time_limit=1700)
def expire_loyalty_points() -> Dict[str, Any]:
    """
    Expire loyalty points that have passed their expiration date.

    This task should run daily at midnight to:
    1. Find all transactions with expired points
    2. Mark them as expired
    3. Create expiry transaction records
    4. Update user loyalty account balances
    5. Send notifications to affected users

    Returns:
        Dict with expiration results including count and affected users
    """
    try:
        logger.info("Starting loyalty points expiration task")

        loyalty_service = LoyaltyService()

        # Use the service method to expire points
        result = loyalty_service.expire_points()

        expired_count = result.get("total_expired_points", 0)
        affected_users = result.get("affected_users", 0)

        logger.info(
            f"Loyalty points expiration completed: " f"{expired_count} points expired for {affected_users} users"
        )

        return {
            "success": True,
            "expired_points": expired_count,
            "affected_users": affected_users,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        logger.error(f"Failed to expire loyalty points: {e}")
        return {"success": False, "error": str(e), "timestamp": datetime.now(timezone.utc).isoformat()}


@shared_task(time_limit=1800, soft_time_limit=1700)
def process_pending_referral_rewards() -> Dict[str, Any]:
    """
    Process pending referral rewards.

    This task checks for completed referrals that haven't been rewarded yet
    and processes the loyalty points for both referrer and referee.

    Returns:
        Dict with processing results
    """
    try:
        logger.info("Processing pending referral rewards")

        loyalty_service = LoyaltyService()

        # Get pending referrals and process rewards
        result = loyalty_service.process_pending_referrals()

        processed_count = result.get("processed_count", 0)
        total_points_awarded = result.get("total_points_awarded", 0)

        logger.info(
            f"Referral rewards processed: {processed_count} referrals, " f"{total_points_awarded} points awarded"
        )

        return {
            "success": True,
            "processed_count": processed_count,
            "total_points_awarded": total_points_awarded,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        logger.error(f"Failed to process referral rewards: {e}")
        return {"success": False, "error": str(e), "timestamp": datetime.now(timezone.utc).isoformat()}


@shared_task(time_limit=1800, soft_time_limit=1700)
def process_daily_surprise_rewards() -> Dict[str, Any]:
    """
    Share surprise rewards for the previous delivery day.

    Runs at business midnight: scans orders that were delivered and fully paid
    within yesterday's delivery day and randomly grants surprise bonuses to
    eligible individual customers (gated by per-user cooldown + global daily cap).

    Returns:
        Dict with processing results
    """
    try:
        logger.info("Processing daily surprise rewards")

        result = LoyaltyService().process_daily_surprise_rewards()

        awarded = result.get("awarded", 0)
        candidates = result.get("candidates", 0)
        logger.info(f"Surprise rewards processed: {awarded} awarded from {candidates} candidate orders")

        return {
            "success": True,
            "candidates": candidates,
            "awarded": awarded,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        logger.error(f"Failed to process daily surprise rewards: {e}")
        return {"success": False, "error": str(e), "timestamp": datetime.now(timezone.utc).isoformat()}


@shared_task(time_limit=1800, soft_time_limit=1700)
def send_points_expiring_soon_reminders() -> Dict[str, Any]:
    """
    Send reminders to users whose points are expiring soon.

    This task should run daily to notify users about points
    expiring in the next 7 days.

    Returns:
        Dict with reminder results
    """
    try:
        logger.info("Sending points expiring soon reminders")

        loyalty_service = LoyaltyService()
        notification_service = NotificationService()

        # Get users with points expiring in 7 days
        expiring_soon = loyalty_service.get_points_expiring_soon(days=7)

        sent_count = 0

        for user_data in expiring_soon:
            try:
                user_id = user_data.get("user_id")
                expiring_points = user_data.get("expiring_points", 0)
                expiry_date = user_data.get("expiry_date")

                notification_service.send_notification(
                    user_id,
                    "points_expiring_soon",
                    template_data={
                        "expiring_points": expiring_points,
                        "expiry_date": expiry_date.isoformat() if expiry_date else None,
                        "days_remaining": 7,
                    },
                )

                sent_count += 1

            except Exception as e:
                logger.error(f"Failed to send expiry reminder to user {user_data.get('user_id')}: {e}")
                continue

        logger.info(f"Sent {sent_count} points expiring soon reminders")

        return {
            "success": True,
            "sent_count": sent_count,
            "total_users": len(expiring_soon),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        logger.error(f"Failed to send points expiring reminders: {e}")
        return {"success": False, "error": str(e), "timestamp": datetime.now(timezone.utc).isoformat()}


@shared_task(time_limit=1800, soft_time_limit=1700)
def update_loyalty_tiers() -> Dict[str, Any]:
    """
    Update loyalty tiers for all active users based on their current points.

    This task recalculates and updates tier status for users who may have
    earned enough points to upgrade or need to be downgraded due to expiration.

    Returns:
        Dict with tier update results
    """
    try:
        logger.info("Updating loyalty tiers")

        loyalty_service = LoyaltyService()
        notification_service = NotificationService()

        # Get all users with loyalty accounts
        result = loyalty_service.update_all_tiers()

        upgrades = result.get("upgrades", [])
        downgrades = result.get("downgrades", [])

        # Send notifications for tier changes
        for user_data in upgrades:
            try:
                notification_service.send_notification(
                    user_data["user_id"],
                    "tier_upgraded",
                    template_data={
                        "old_tier": user_data.get("old_tier"),
                        "new_tier": user_data.get("new_tier"),
                        "benefits": user_data.get("benefits", []),
                    },
                )
            except Exception as e:
                logger.error(f"Failed to send tier upgrade notification: {e}")

        for user_data in downgrades:
            try:
                notification_service.send_notification(
                    user_data["user_id"],
                    "tier_downgraded",
                    template_data={
                        "old_tier": user_data.get("old_tier"),
                        "new_tier": user_data.get("new_tier"),
                        "points_needed": user_data.get("points_needed_for_restore"),
                    },
                )
            except Exception as e:
                logger.error(f"Failed to send tier downgrade notification: {e}")

        logger.info(f"Loyalty tiers updated: {len(upgrades)} upgrades, {len(downgrades)} downgrades")

        return {
            "success": True,
            "upgrades_count": len(upgrades),
            "downgrades_count": len(downgrades),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        logger.error(f"Failed to update loyalty tiers: {e}")
        return {"success": False, "error": str(e), "timestamp": datetime.now(timezone.utc).isoformat()}


@shared_task(time_limit=1800, soft_time_limit=1700)
def grant_birthday_bonuses() -> Dict[str, Any]:
    """
    Grant the birthday bonus to users whose birthday is today.

    Runs daily. Amount comes from LoyaltyProgram (DB SSOT); idempotent within a
    calendar year so repeated runs never double-grant.

    Returns:
        Dict with the number of birthday bonuses granted.
    """
    try:
        logger.info("Granting birthday bonuses")

        loyalty_service = LoyaltyService()
        result = loyalty_service.grant_birthday_bonuses()
        granted = result.get("granted", 0)

        logger.info(f"Birthday bonuses granted: {granted}")

        return {
            "success": True,
            "granted": granted,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        logger.error(f"Failed to grant birthday bonuses: {e}")
        return {"success": False, "error": str(e), "timestamp": datetime.now(timezone.utc).isoformat()}
