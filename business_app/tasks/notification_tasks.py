"""
Notification-related Celery tasks for the Water Business Platform
This file should be placed in business_app/tasks/notification_tasks.py
"""

from celery import shared_task
from celery.utils.log import get_task_logger
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List
from flask import current_app

from business_app.models.notification import Notification
from business_app.models.user import User
from business_app.models.delivery import Delivery
from business_app.services.notification_service import NotificationService
from business_app.utils.constants import NotificationType, NotificationChannel
from business_app import db

logger = get_task_logger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=30, time_limit=1800, soft_time_limit=1700)
def execute_notification_campaign_task(self, campaign_id: int):
    """Execute one queued notification campaign."""
    try:
        logger.info("Executing notification campaign %s", campaign_id)
        notification_service = NotificationService()
        result = notification_service.execute_notification_campaign(campaign_id)
        logger.info("Notification campaign %s finished with result: %s", campaign_id, result)
        return result
    except Exception as exc:
        logger.error("Failed to execute notification campaign %s: %s", campaign_id, exc)
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=30, time_limit=120, soft_time_limit=100)
def send_telegram_security_alert_task(self, user_id: int, alert_type: str, message: str):
    """
    Send security alert notification via Telegram.

    Used for password changes, new logins from unknown devices, etc.

    Args:
        user_id: User ID
        alert_type: Type of alert (password_change, new_login, suspicious_activity)
        message: Alert message
    """
    try:
        logger.info(f"Sending telegram security alert for user {user_id}: {alert_type}")

        user = User.query.get(user_id)
        if not user:
            logger.error(f"User {user_id} not found")
            return {"success": False, "error": "User not found"}

        if not user.telegram_id:
            logger.info(f"User {user_id} has no telegram_id, skipping")
            return {"success": False, "error": "No telegram ID"}

        notification_service = NotificationService()

        # Format security alert message with timestamp
        from datetime import datetime, timezone

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        template_data = {
            "user_name": user.first_name or "User",
            "alert_type": alert_type,
            "message": message,
            "timestamp": timestamp,
            "company_name": current_app.config.get("COMPANY_NAME", "Bluestream"),
        }

        result = notification_service.send_notification(
            user_id, NotificationType.SECURITY, [NotificationChannel.TELEGRAM], template_data
        )

        if any(r.get("success") for r in result.values() if isinstance(r, dict)):
            logger.info(f"Telegram security alert sent successfully to user {user_id}")
        else:
            logger.warning(f"Telegram security alert send returned: {result}")

        return result

    except Exception as exc:
        logger.error(f"Failed to send telegram security alert for user {user_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=30, time_limit=120, soft_time_limit=100)
def send_account_locked_notification_task(self, user_id: int, lockout_until: str, lockout_minutes: int):
    """
    Send notification when user account is locked due to failed login attempts.

    Sends alerts via SMS and Telegram to warn user of potential unauthorized access.

    Args:
        user_id: User ID
        lockout_until: ISO format datetime when lockout expires
        lockout_minutes: Lockout duration in minutes
    """
    try:
        logger.info(f"Sending account locked notification for user {user_id}")

        user = User.query.get(user_id)
        if not user:
            logger.error(f"User {user_id} not found")
            return {"success": False, "error": "User not found"}

        notification_service = NotificationService()

        template_data = {
            "user_name": user.first_name or "User",
            "lockout_until": lockout_until,
            "lockout_minutes": lockout_minutes,
            "company_name": current_app.config.get("COMPANY_NAME", "Bluestream"),
            "support_contact": current_app.config.get("SUPPORT_PHONE", ""),
        }

        results = {}

        # Send SMS if user has phone
        if user.phone:
            try:
                sms_result = notification_service.send_sms_to_phone(
                    phone=user.phone,
                    notification_type=NotificationType.SECURITY,
                    template_key="sms.account_locked",
                    template_data=template_data,
                    language=user.preferred_language or "uz",
                )
                results["sms"] = sms_result
            except Exception as e:
                logger.warning(f"Failed to send account locked SMS: {e}")
                results["sms"] = {"success": False, "error": str(e)}

        # Send Telegram if user has telegram_id
        if user.telegram_id:
            try:
                tg_result = notification_service.send_notification(
                    user_id,
                    NotificationType.SECURITY,
                    [NotificationChannel.TELEGRAM],
                    {
                        **template_data,
                        "alert_type": "account_locked",
                        "message": f"Your account has been locked for {lockout_minutes} minutes due to too many failed login attempts.",  # noqa: E501
                    },
                )
                results["telegram"] = tg_result
            except Exception as e:
                logger.warning(f"Failed to send account locked Telegram notification: {e}")
                results["telegram"] = {"success": False, "error": str(e)}

        logger.info(f"Account locked notifications sent for user {user_id}: {results}")
        return results

    except Exception as exc:
        logger.error(f"Failed to send account locked notification for user {user_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, time_limit=120, soft_time_limit=100)
def send_loyalty_notification_task(
    self, user_id: int, event_type: str, data: Dict[str, Any], notification_type_str: str = None
):
    """Send loyalty program notification

    Args:
        user_id: User to notify
        event_type: Type of loyalty event (earned, redeemed, etc.)
        data: Template data
        notification_type_str: String value of NotificationType enum (e.g., 'loyalty_reward', 'reward_redeemed')
    """
    try:
        logger.info(
            f"Sending loyalty notification for user {user_id}, event: {event_type}, type: {notification_type_str}"
        )  # noqa: E501

        notification_service = NotificationService()

        # Convert string to NotificationType enum if provided
        notification_type = None
        if notification_type_str:
            try:
                notification_type = NotificationType(notification_type_str)
            except ValueError:
                logger.warning(f"Invalid notification type: {notification_type_str}, using default")

        result = notification_service.send_loyalty_notification(user_id, event_type, data, notification_type)

        logger.info(f"Loyalty notification sent successfully for user {user_id}")
        return result

    except Exception as exc:
        logger.error(f"Failed to send loyalty notification: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, time_limit=120, soft_time_limit=100)
def notify_driver_assignment_task(self, delivery_id: int):
    """Notify driver about new delivery assignment"""
    try:
        logger.info(f"Notifying driver about delivery assignment {delivery_id}")

        delivery = Delivery.query.get(delivery_id)
        if not delivery or not delivery.delivery_person:
            logger.error(f"Delivery {delivery_id} not found or no driver assigned")
            return {"success": False, "error": "Delivery or driver not found"}

        notification_service = NotificationService()

        order_address = delivery.order.delivery_address
        template_data = {
            "delivery_id": delivery.id,
            "tracking_code": delivery.tracking_number,
            "order_number": delivery.order.order_number,
            "customer_name": f"{delivery.order.user.first_name} {delivery.order.user.last_name}",
            "delivery_address": order_address.street_address if order_address else None,
            "customer_phone": delivery.order.user.phone,
            "estimated_delivery_time": (
                delivery.estimated_delivery_time.isoformat() if delivery.estimated_delivery_time else None
            ),  # noqa: E501
        }

        result = notification_service.send_notification(
            delivery.delivery_person_id,
            NotificationType.DELIVERY_UPDATE,
            [NotificationChannel.TELEGRAM],
            template_data,
        )

        logger.info(f"Driver notification sent successfully for delivery {delivery_id}")
        return result

    except Exception as exc:
        logger.error(f"Failed to notify driver: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, time_limit=120, soft_time_limit=100)
def notify_delivery_cancellation_task(self, delivery_id: int):
    """Notify about delivery cancellation"""
    try:
        logger.info(f"Sending delivery cancellation notification for delivery {delivery_id}")

        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            logger.error(f"Delivery {delivery_id} not found")
            return {"success": False, "error": "Delivery not found"}

        notification_service = NotificationService()

        template_data = {
            "order_number": delivery.order.order_number,
            "tracking_code": delivery.tracking_code,
            "cancellation_reason": delivery.cancellation_reason or "No reason provided",
            "customer_service_phone": current_app.config["COMPANY_PHONE"],
        }

        # Notify customer
        customer_result = notification_service.send_notification(
            delivery.order.user_id, NotificationType.DELIVERY_UPDATE, None, template_data
        )

        # Notify driver if assigned
        driver_result = {}
        if delivery.driver_id:
            driver_result = notification_service.send_notification(
                delivery.driver_id,
                NotificationType.DELIVERY_UPDATE,
                [NotificationChannel.TELEGRAM],
                template_data,
            )

        logger.info(f"Delivery cancellation notifications sent for delivery {delivery_id}")
        return {"customer_notification": customer_result, "driver_notification": driver_result}

    except Exception as exc:
        logger.error(f"Failed to send delivery cancellation notification: {exc}")
        raise self.retry(exc=exc)


@shared_task(time_limit=1800, soft_time_limit=1700)
def cleanup_old_notifications():
    """Clean up old notification records"""
    try:
        logger.info("Cleaning up old notifications")

        # Delete notifications older than 6 months
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=180)

        deleted_count = Notification.query.filter(Notification.created_at < cutoff_date).delete()

        db.session.commit()

        logger.info(f"Cleaned up {deleted_count} old notification records")
        return {"deleted_count": deleted_count}

    except Exception as e:
        logger.error(f"Failed to clean up old notifications: {e}")
        db.session.rollback()
        return {"error": str(e)}


@shared_task(bind=True, max_retries=3, default_retry_delay=60, time_limit=120, soft_time_limit=100)
def send_emergency_notification(self, user_ids: List[int], message: str, channels: List[str] = None):
    """Send emergency notification to specified users"""
    try:
        logger.info(f"Sending emergency notification to {len(user_ids)} users")

        if channels is None:
            channels = ["sms", "email", "telegram"]

        notification_channels = [NotificationChannel(ch) for ch in channels]
        notification_service = NotificationService()

        results = []

        for user_id in user_ids:
            try:
                result = notification_service.send_notification(
                    user_id,
                    NotificationType.SYSTEM_ALERT,
                    notification_channels,
                    {"emergency_message": message, "priority": "urgent"},
                )
                results.append({"user_id": user_id, "result": result})

            except Exception as e:
                logger.error(f"Failed to send emergency notification to user {user_id}: {e}")
                results.append({"user_id": user_id, "error": str(e)})

        logger.info(f"Emergency notification completed for {len(user_ids)} users")
        return results

    except Exception as exc:
        logger.error(f"Emergency notification failed: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, time_limit=120, soft_time_limit=100)
def send_order_notification_task(self, order_id: int, notification_type: str):
    """Send order-related notification"""
    try:
        logger.info(f"Sending order notification for order {order_id}, type: {notification_type}")

        notification_service = NotificationService()
        result = notification_service.send_order_notification(order_id, notification_type)

        logger.info(f"Order notification sent successfully for order {order_id}")
        return result

    except Exception as exc:
        logger.error(f"Failed to send order notification: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, time_limit=120, soft_time_limit=100)
def send_delivery_update_task(self, history_id: int):
    """Send delivery status update notification from a committed history event."""
    try:
        logger.info("Sending delivery update for history %s", history_id)

        notification_service = NotificationService()
        result = notification_service.send_delivery_status_change_notification(history_id)

        logger.info("Delivery update processed for history %s", history_id)
        return result

    except Exception as exc:
        logger.error("Failed to send delivery update for history %s: %s", history_id, exc)
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, time_limit=120, soft_time_limit=100)
def send_payment_confirmation_task(self, payment_id: int):
    """Send payment confirmation notification"""
    try:
        logger.info(f"Sending payment confirmation for payment {payment_id}")

        # Idempotency check via Redis
        from business_app import redis_client

        idempotency_key = f"notif:payment_confirm:{payment_id}"
        if redis_client.get(idempotency_key):
            logger.info(f"Payment confirmation already sent for {payment_id}, skipping")
            return {"success": True, "skipped": True, "reason": "already_sent"}

        notification_service = NotificationService()
        result = notification_service.send_payment_notification(payment_id)

        # Mark as sent with 24h TTL
        redis_client.setex(idempotency_key, 86400, "1")

        logger.info(f"Payment confirmation sent successfully for payment {payment_id}")
        return result

    except Exception as exc:
        logger.error(f"Failed to send payment confirmation: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, time_limit=120, soft_time_limit=100)
def send_verification_email_task(self, user_id: int, verification_token: str):
    """Send email verification notification"""
    try:
        logger.info(f"Sending email verification for user {user_id}")

        user = User.query.get(user_id)
        if not user:
            logger.error(f"User {user_id} not found")
            return {"success": False, "error": "User not found"}

        notification_service = NotificationService()

        template_data = {
            "user_name": f"{user.first_name} {user.last_name}",
            "verification_token": verification_token,
            "verification_code": verification_token,  # Alias for template compatibility
            "verification_url": f"{current_app.config['COMPANY_WEBSITE']}/verify-email?token={verification_token}",
            "company_name": current_app.config["COMPANY_NAME"],
        }

        result = notification_service.send_notification(
            user_id, NotificationType.EMAIL_VERIFICATION, [NotificationChannel.EMAIL], template_data
        )

        logger.info(f"Email verification sent successfully for user {user_id}")
        return result

    except Exception as exc:
        logger.error(f"Failed to send email verification: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, time_limit=120, soft_time_limit=100)
def send_verification_sms_task(self, user_id: int, otp_code: str, phone_number: str = None):
    """
    Send SMS verification notification

    Args:
        user_id: User ID
        otp_code: OTP code to send
        phone_number: Phone number to send to (optional, uses user's phone if not provided)
    """
    try:
        logger.info(f"Sending SMS verification for user {user_id} to {phone_number or 'user phone'}")

        user = User.query.get(user_id)
        if not user:
            logger.error(f"User {user_id} not found")
            return {"success": False, "error": "User not found"}

        # Use provided phone or user's phone
        target_phone = phone_number or user.phone
        if not target_phone:
            logger.error(f"No phone number available for user {user_id}")
            return {"success": False, "error": "No phone number available"}

        notification_service = NotificationService()

        template_data = {
            "user_name": user.first_name,
            "otp_code": otp_code,
            "phone_number": target_phone,
            "company_name": current_app.config["COMPANY_NAME"],
        }

        # If explicit phone provided (like for account linking), use send_sms_to_phone
        # since user.phone may be None or different
        if phone_number:
            result = notification_service.send_sms_to_phone(
                phone=target_phone,
                notification_type=NotificationType.SYSTEM,
                template_key="sms.verification.otp",
                template_data=template_data,
                language=getattr(user, "preferred_language", "en"),
            )
        else:
            # Use standard send_notification when using user's own phone
            result = notification_service.send_notification(
                user_id, NotificationType.SYSTEM, [NotificationChannel.SMS], template_data
            )

        logger.info(f"SMS verification sent successfully for user {user_id} to {target_phone}")
        return result

    except Exception as exc:
        logger.error(f"Failed to send SMS verification: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, time_limit=120, soft_time_limit=100)
def send_password_reset_email_task(self, user_id: int, reset_token: str):
    """Send password reset email"""
    try:
        logger.info(f"Sending password reset email for user {user_id}")

        user = User.query.get(user_id)
        if not user:
            logger.error(f"User {user_id} not found")
            return {"success": False, "error": "User not found"}

        notification_service = NotificationService()

        template_data = {
            "user_name": f"{user.first_name} {user.last_name}",
            "reset_token": reset_token,
            "reset_url": f"{current_app.config['COMPANY_WEBSITE']}/reset-password/{reset_token}",
            "company_name": current_app.config["COMPANY_NAME"],
            "expiry_hours": 24,
        }

        result = notification_service.send_notification(
            user_id, NotificationType.PASSWORD_RESET, [NotificationChannel.EMAIL], template_data
        )

        logger.info(f"Password reset email sent successfully for user {user_id}")
        return result

    except Exception as exc:
        logger.error(f"Failed to send password reset email: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=30, time_limit=120, soft_time_limit=100)
def send_password_reset_sms_task(self, user_id: int, otp_code: str):
    """
    Send password reset OTP via SMS.

    Used for telegram users with placeholder emails who have a verified phone number.
    """
    try:
        logger.info(f"Sending password reset SMS for user {user_id}")

        user = User.query.get(user_id)
        if not user:
            logger.error(f"User {user_id} not found")
            return {"success": False, "error": "User not found"}

        if not user.phone:
            logger.error(f"User {user_id} has no phone number")
            return {"success": False, "error": "No phone number"}

        notification_service = NotificationService()

        template_data = {
            "user_name": user.first_name or "User",
            "otp_code": otp_code,
            "phone_number": user.phone,
            "company_name": current_app.config.get("COMPANY_NAME", "Bluestream"),
            "expiry_minutes": 10,
        }

        # Send password reset OTP via SMS
        result = notification_service.send_sms_to_phone(
            phone=user.phone,
            notification_type=NotificationType.PASSWORD_RESET,
            template_key="sms.password_reset.otp",
            template_data=template_data,
            language=user.preferred_language or "uz",
        )

        if result.get("success"):
            logger.info(f"Password reset SMS sent successfully to user {user_id}")
        else:
            logger.warning(f"Password reset SMS send returned: {result}")

        return result

    except Exception as exc:
        logger.error(f"Failed to send password reset SMS for user {user_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, time_limit=120, soft_time_limit=100)
def send_subscription_confirmation_task(self, subscription_id: int):
    """Send subscription confirmation notification"""
    try:
        logger.info(f"Sending subscription confirmation for subscription {subscription_id}")

        notification_service = NotificationService()
        result = notification_service.send_subscription_notification(subscription_id, "confirmed")

        logger.info(f"Subscription confirmation sent successfully for subscription {subscription_id}")
        return result

    except Exception as exc:
        logger.error(f"Failed to send subscription confirmation: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, time_limit=120, soft_time_limit=100)
def send_subscription_notification_task(self, subscription_id: int, event_type: str):
    """Send subscription-related notification"""
    try:
        logger.info(f"Sending subscription notification for subscription {subscription_id}, event: {event_type}")

        notification_service = NotificationService()
        result = notification_service.send_subscription_notification(subscription_id, event_type)

        logger.info(f"Subscription notification sent successfully for subscription {subscription_id}")
        return result

    except Exception as exc:
        logger.error(f"Failed to send subscription notification: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, time_limit=1800, soft_time_limit=1700)
def send_bulk_notification_task(
    self,
    notification_type: str,
    recipient_ids: List[int],
    template_data: Dict[str, Any] = None,
    channel: str = "email",
    channels: List[str] = None,
    batch_size: int = 50,
):
    """Send bulk notifications to multiple recipients in batches"""
    import time as _time

    try:
        logger.info(
            f"Starting bulk notification send: {notification_type} to {len(recipient_ids)} recipients (batch_size={batch_size})"  # noqa: E501
        )

        template_data = template_data or {}

        notification_service = NotificationService()
        try:
            notification_type_enum = NotificationType(notification_type)
        except ValueError as exc:
            raise ValueError(f"Unsupported notification_type: {notification_type}") from exc

        channel_values = channels or [channel]
        if not channel_values:
            channel_values = [channel]

        channel_enums = []
        for channel_value in channel_values:
            try:
                channel_enums.append(NotificationChannel(channel_value))
            except ValueError as exc:
                raise ValueError(f"Unsupported channel: {channel_value}") from exc

        results = []

        for batch_start in range(0, len(recipient_ids), batch_size):
            batch = recipient_ids[batch_start : batch_start + batch_size]
            batch_num = batch_start // batch_size + 1
            logger.info(f"Processing batch {batch_num} ({len(batch)} recipients)")

            for recipient_id in batch:
                try:
                    result = notification_service.send_notification(
                        recipient_id, notification_type_enum, channels=channel_enums, template_data=template_data
                    )
                    successful_channels = [
                        ch.value
                        for ch in channel_enums
                        if isinstance(result.get(ch.value), dict) and result[ch.value].get("success")
                    ]
                    results.append(
                        {
                            "recipient_id": recipient_id,
                            "success": bool(successful_channels),
                            "channels": successful_channels,
                        }
                    )
                except Exception as e:
                    logger.error(f"Failed to send notification to recipient {recipient_id}: {e}")
                    results.append({"recipient_id": recipient_id, "success": False, "error": str(e)})

            # Brief pause between batches to avoid overwhelming external services
            if batch_start + batch_size < len(recipient_ids):
                _time.sleep(1)

        successful_sends = sum(1 for r in results if r["success"])
        failed_sends = len(results) - successful_sends

        logger.info(f"Bulk notification completed: {successful_sends} successful, {failed_sends} failed")

        return {
            "total_recipients": len(recipient_ids),
            "successful_sends": successful_sends,
            "failed_sends": failed_sends,
            "results": results,
        }

    except Exception as exc:
        logger.error(f"Bulk notification task failed: {exc}")
        raise self.retry(exc=exc)


# =============================================================================
# Phone Registration OTP Tasks
# =============================================================================


@shared_task(bind=True, max_retries=3, default_retry_delay=30, time_limit=120, soft_time_limit=100)
def send_registration_otp_task(self, phone: str, otp_code: str, language: str = "uz"):
    """
    Send registration OTP via SMS to a phone number.

    This task is used during phone-based registration before the user account exists.

    Args:
        phone: Phone number in normalized format (+998XXXXXXXXX)
        otp_code: 6-digit OTP code
        language: Language code for SMS template (uz, ru, en)
    """
    try:
        logger.info(f"Sending registration OTP to {phone[:4]}***{phone[-4:]}")

        notification_service = NotificationService()

        # Template data for registration OTP
        template_data = {
            "otp_code": otp_code,
            "phone_number": phone,
            "company_name": current_app.config.get("COMPANY_NAME", "Bluestream"),
            "expiry_minutes": 3,
        }

        # Send SMS directly without user_id (user doesn't exist yet)
        result = notification_service.send_sms_to_phone(
            phone=phone,
            notification_type=NotificationType.SYSTEM,
            template_key="sms.registration.otp",
            template_data=template_data,
            language=language,
        )

        if result.get("success"):
            logger.info(f"Registration OTP sent successfully to {phone[:4]}***{phone[-4:]}")
        else:
            logger.warning(f"Registration OTP send returned: {result}")

        return result

    except Exception as exc:
        logger.error(f"Failed to send registration OTP to {phone[:4]}***{phone[-4:]}: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=30, time_limit=120, soft_time_limit=100)
def send_welcome_sms_task(self, user_id: int):
    """
    Send welcome SMS after successful phone registration.

    Args:
        user_id: User ID of the newly registered user
    """
    try:
        logger.info(f"Sending welcome SMS for user {user_id}")

        user = User.query.get(user_id)
        if not user:
            logger.error(f"User {user_id} not found for welcome SMS")
            return {"success": False, "error": "User not found"}

        if not user.phone:
            logger.error(f"User {user_id} has no phone number for welcome SMS")
            return {"success": False, "error": "No phone number"}

        notification_service = NotificationService()

        template_data = {
            "first_name": user.first_name or "Customer",
            "user_name": user.first_name or "Customer",
            "phone_number": user.phone,
            "company_name": current_app.config.get("COMPANY_NAME", "Bluestream"),
        }

        # Send welcome SMS
        result = notification_service.send_sms_to_phone(
            phone=user.phone,
            notification_type=NotificationType.SYSTEM,
            template_key="sms.welcome",
            template_data=template_data,
            language=user.preferred_language or "uz",
        )

        if result.get("success"):
            logger.info(f"Welcome SMS sent successfully to user {user_id}")
        else:
            logger.warning(f"Welcome SMS send returned: {result}")

        return result

    except Exception as exc:
        logger.error(f"Failed to send welcome SMS for user {user_id}: {exc}")
        raise self.retry(exc=exc)
