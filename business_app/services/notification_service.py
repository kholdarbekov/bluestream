"""
Notification service for the Water Business Platform
Handles SMS, Email, Telegram, and Push notifications
"""

import html
import json
import os
import re
import uuid
from celery.utils.log import get_task_logger
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from typing import Dict, Any, List, Optional
from flask import current_app
import requests
from eskiz_sms import EskizSMS

from business_app.models.notification import (
    Notification,
    NotificationCampaign,
    NotificationTemplate,
    NotificationPreference,
    PushNotificationToken,
)
from business_app.models.analytics import UserSegment
from business_app.models.loyalty import LoyaltyPoints
from business_app.models.user import User
from business_app.models.order import Order
from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.payment import Payment
from business_app.models.subscription import Subscription
from business_app.models.translation import Translation
from business_app.utils.exceptions import (
    NotificationError,
    ConfigurationError,
    ValidationError,
    NotFoundError,
    ForbiddenError,
    ConflictError,
)
from business_app.utils.constants import (
    NotificationType,
    NotificationChannel,
    NotificationStatus,
    Priority,
)
from shared.enums import (
    DeliveryStatus,
    UserRole,
)
from business_app.utils.translations import get_translation
from business_app.utils.helpers import scrub_bot_token
from business_app.services.email_template_service import get_email_template_service
from business_app.services.bottle_tracking_service import (
    BottleTrackingService,
    format_bottle_quantity,
)
from business_app.utils.bot_webhook import trigger_bot_webhook
from business_app import db, redis_client
from sqlalchemy import func

# Use standard logging that works in both Flask and Celery contexts
# logger = logging.getLogger(__name__)
logger = get_task_logger(__name__)

# SSOT: loyalty event_type -> NotificationType. Every event a caller can emit
# needs an entry here AND its own template; send_loyalty_notification refuses
# to send anything not listed rather than falling back to LOYALTY_REWARD.
LOYALTY_EVENT_NOTIFICATION_TYPES = {
    "earned": NotificationType.LOYALTY_REWARD,
    "redeemed": NotificationType.REWARD_REDEEMED,
    "tier_upgrade": NotificationType.LOYALTY_TIER_UPGRADE,
    "points_expired": NotificationType.LOYALTY_POINTS_EXPIRED,
}

# Customer-facing AquaCoins messages delivered on exactly ONE channel
# (Telegram when the bot is connected, else email) — never both, never SMS.
_LOYALTY_SINGLE_CHANNEL_TYPES = frozenset(
    {
        NotificationType.LOYALTY_REWARD,
        NotificationType.LOYALTY_TIER_UPGRADE,
        NotificationType.LOYALTY_POINTS_EXPIRED,
    }
)

# Notification types whose message is pure courtesy: a leaked {placeholder}
# means the copy is wrong, so drop it rather than show the customer a broken
# message. Operational types (orders, deliveries, payments) are only logged —
# a slightly-wrong message still beats no message at all.
_PLACEHOLDER_STRICT_TYPES = frozenset(
    {
        NotificationType.LOYALTY_REWARD.value,
        NotificationType.REWARD_REDEEMED.value,
        NotificationType.LOYALTY_TIER_UPGRADE.value,
        NotificationType.LOYALTY_POINTS_EXPIRED.value,
    }
)

# A placeholder is a bare snake_case identifier in braces. CSS blocks
# ("body { font-family: x; }") and JSON ('{"a": 1}') never match.
_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}", re.IGNORECASE)

# Configure logger to ensure it outputs in both Flask and Celery
# if not logger.handlers:
#     handler = logging.StreamHandler()
#     handler.setFormatter(logging.Formatter(
#         '[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
#     ))
#     logger.addHandler(handler)
#     logger.setLevel(logging.INFO)
#     logger.propagate = True


# ---------------------------------------------------------------------------
# SMS is OTP-only.
#
# Two hard constraints, both learned from production:
#
# 1. Eskiz refuses any text that has not passed moderation, with HTTP 400
#    ("Этот смс текст еще не прошёл модерацию"). So the sendable set is a
#    CLOSED ALLOWLIST, not a default-open template lookup. Adding a key here
#    without first getting its exact text moderated at my.eskiz.uz means every
#    send fails — silently, from the caller's point of view.
# 2. Cyrillic forces UCS-2, which segments every 70 characters instead of
#    GSM-7's 160, and is billed per segment. The Russian verification text was
#    78 chars = 2 billed parts; transliterated to Latin it is 81 chars = 1.
#    Every text below is therefore GSM-7-safe Latin. Beware: the GSM-7
#    alphabet is narrower than "Latin". U+2018/U+2019 (typographic quotes) are
#    outside it, so writing Uzbek o' / g' with a curly quote rather than an
#    ASCII apostrophe (U+0027) silently doubles the bill again.
#    tests/unit/test_sms_otp_only.py asserts every text here stays in-alphabet.
#
# Anything that is not a one-time passcode must use Telegram or email.
# ---------------------------------------------------------------------------
OTP_SMS_TEMPLATES = {
    "sms.verification.otp": {
        "uz": "Aqua Element platformasida telefon raqamingizni tasdiqlash uchun kod: {otp_code}",
        "ru": "Kod dlya podtverjdeniya vashego nomera telefona na platforme Aqua Element: {otp_code}",
        "en": "Code to verify your phone number on the Aqua Element platform: {otp_code}",
    },
    # Allowed, but no moderated text exists yet, so every send returns
    # "template not found" rather than burning a provider call. Fill this in
    # once a password-reset text has been moderated at my.eskiz.uz.
    "sms.password_reset.otp": {},
}

OTP_SMS_TEMPLATE_KEYS = frozenset(OTP_SMS_TEMPLATES)


class BottleSummaryNotReady(Exception):
    """Raised when a delivered order's bottle ledger has not committed yet.

    The dispatch raises this WITHOUT claiming the idempotency key so the
    send_delivery_update_task retry re-runs it once the ledger commit lands
    (design §2 commit race / §3.2 step 3).
    """


class NotificationService:
    """Service for handling notifications across multiple channels"""

    DELIVERY_TELEGRAM_STATUS_UPDATES_PREF_KEY = "delivery_telegram_status_updates"
    NOTIFICATION_CAMPAIGN_STATUSES = {"draft", "scheduled", "sending", "sent", "failed", "cancelled"}
    NOTIFICATION_CAMPAIGN_AUDIENCES = {
        "all_customers",
        "active_customers",
        "new_customers",
        "loyalty_members",
        "custom_segment",
    }

    NOTIFICATION_TYPE_GROUPS = {
        "order": [
            NotificationType.ORDER_CONFIRMATION.value,
            NotificationType.ORDER_STATUS_UPDATE.value,
            NotificationType.ORDER_UPDATE.value,
        ],
        "delivery": [
            NotificationType.DELIVERY_UPDATE.value,
            NotificationType.DELIVERY_REMINDER.value,
        ],
        "payment": [NotificationType.PAYMENT_CONFIRMATION.value],
        "promotion": [NotificationType.PROMOTIONAL.value],
        "system": [
            NotificationType.SYSTEM.value,
            NotificationType.SYSTEM_ALERT.value,
            NotificationType.EMAIL_VERIFICATION.value,
            NotificationType.PASSWORD_RESET.value,
        ],
        "loyalty": [
            NotificationType.LOYALTY_REWARD.value,
            NotificationType.REWARD_REDEEMED.value,
            NotificationType.LOYALTY_TIER_UPGRADE.value,
            NotificationType.LOYALTY_POINTS_EXPIRED.value,
        ],
        "security": [NotificationType.SECURITY.value],
        "reminder": [NotificationType.SUBSCRIPTION_REMINDER.value],
        "subscription": [
            NotificationType.SUBSCRIPTION_CREATED.value,
            NotificationType.SUBSCRIPTION_RENEWAL.value,
            NotificationType.SUBSCRIPTION_CANCELLED.value,
            NotificationType.SUBSCRIPTION_CANCELLATION_SCHEDULED.value,
            NotificationType.SUBSCRIPTION_REMINDER.value,
        ],
    }

    DELIVERY_STATUS_LABEL_FALLBACKS = {
        "uz": {
            "scheduled": "Rejalashtirilgan",
            "pending": "Kutilmoqda",
            "assigned": "Tayinlandi",
            "picked_up": "Olib ketildi",
            "out_for_delivery": "Yetkazib berishga chiqarildi",
            "in_transit": "Yo'lda",
            "arrived": "Yetib keldi",
            "delivered": "Yetkazib berildi",
            "failed": "Yetkazib berib bo'lmadi",
            "cancelled": "Bekor qilindi",
            "returned": "Qaytarildi",
        },
        "ru": {
            "scheduled": "Запланирован",
            "pending": "В ожидании",
            "assigned": "Назначен",
            "picked_up": "Забран",
            "out_for_delivery": "Передан в доставку",
            "in_transit": "В пути",
            "arrived": "Прибыл",
            "delivered": "Доставлен",
            "failed": "Не доставлен",
            "cancelled": "Отменен",
            "returned": "Возвращен",
        },
        "en": {
            "scheduled": "Scheduled",
            "pending": "Pending",
            "assigned": "Assigned",
            "picked_up": "Picked Up",
            "out_for_delivery": "Out For Delivery",
            "in_transit": "In Transit",
            "arrived": "Arrived",
            "delivered": "Delivered",
            "failed": "Failed",
            "cancelled": "Cancelled",
            "returned": "Returned",
        },
    }

    PAYMENT_FOLLOW_UP_MESSAGES = {
        "uz": {
            "processing": "Buyurtmangiz qayta ishlanmoqda. Keyingi holat bo'yicha sizni xabardor qilamiz.",
            "delivered": "Buyurtmangiz allaqachon yetkazib berilgan. Ushbu xabar to'lovingiz qabul qilinganini tasdiqlaydi.",  # noqa: E501
        },
        "en": {
            "processing": "Your order is being processed. We'll notify you about the next status update.",
            "delivered": "Your order has already been delivered. This message confirms that we have received your payment.",  # noqa: E501
        },
        "ru": {
            "processing": "Ваш заказ обрабатывается. Мы сообщим вам о следующем обновлении статуса.",
            "delivered": "Ваш заказ уже доставлен. Это сообщение подтверждает, что ваша оплата получена.",
        },
    }

    def __init__(self):
        # Email configuration (Brevo)
        self.brevo_api_key = current_app.config.get("BREVO_API_KEY")
        self.default_sender_email = current_app.config.get("BREVO_SENDER_EMAIL") or current_app.config.get(
            "MAIL_DEFAULT_SENDER"
        )
        self.default_sender_name = current_app.config.get("BREVO_SENDER_NAME") or current_app.config.get(
            "COMPANY_NAME", "Bluestream"
        )

        # SMS configuration (Eskiz)
        self.eskiz_email = current_app.config.get("ESKIZ_EMAIL")
        self.eskiz_password = current_app.config.get("ESKIZ_PASSWORD")
        self.eskiz_from = current_app.config.get("ESKIZ_FROM", "4546")

        # Telegram configuration
        self.telegram_bot_token = current_app.config.get("TELEGRAM_BOT_TOKEN")
        self.staff_telegram_bot_token = (
            current_app.config.get("STAFF_BOT_TOKEN")
            or current_app.config.get("STAFF_TELEGRAM_BOT_TOKEN")
            or os.environ.get("STAFF_BOT_TOKEN")
            or os.environ.get("STAFF_TELEGRAM_BOT_TOKEN")
        )

        # Company information
        self.company_name = current_app.config.get("COMPANY_NAME", "Aqua Element")
        self.company_phone = current_app.config.get("COMPANY_PHONE")
        self.company_email = current_app.config.get("COMPANY_EMAIL")

        # Initialize clients
        self._init_clients()

    def _init_clients(self):
        """Initialize notification service clients"""
        # Brevo email - no client needed, we use requests directly
        if self.brevo_api_key:
            logger.info("Brevo API key configured for email sending")
        else:
            logger.warning("Brevo API key not configured - email notifications will fail")

        # Eskiz SMS client
        logger.info(
            f"DEBUG: Initializing Eskiz SMS - email: {self.eskiz_email}, has_password: {bool(self.eskiz_password)}"
        )
        if self.eskiz_email and self.eskiz_password:
            try:
                logger.info("DEBUG: Creating EskizSMS client instance...")
                self.eskiz_client = EskizSMS(
                    email=self.eskiz_email, password=self.eskiz_password, save_token=True, env_file_path=".env"
                )
                logger.info(f"DEBUG: Eskiz SMS client initialized successfully: {type(self.eskiz_client)}")
            except Exception:
                logger.exception("Failed to initialize Eskiz SMS client")
                self.eskiz_client = None
        else:
            logger.warning(
                f"DEBUG: Eskiz credentials missing - email: {bool(self.eskiz_email)}, password: {bool(self.eskiz_password)}"  # noqa: E501
            )
            self.eskiz_client = None

    def send_notification(
        self,
        user_id: int,
        notification_type: NotificationType,
        channels: List[NotificationChannel] = None,
        template_data: Dict[str, Any] = None,
        priority: str = "normal",
        template_override=None,
        campaign_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Send notification to user across specified channels

        Args:
            user_id: User ID
            notification_type: Type of notification
            channels: List of channels to send to (defaults to user preferences)
            template_data: Data for template rendering
            priority: Notification priority (low, normal, high, urgent)

        Returns:
            Dictionary with send results for each channel
        """
        user = User.query.get(user_id)
        if not user:
            raise NotificationError(get_translation("error.not_found"))

        # Get user's notification preferences
        if channels is None:
            channels = self._get_user_preferred_channels(user_id, notification_type)

        # Get user's language preference
        user_language = getattr(user, "preferred_language", "en")

        template_data = template_data or {}
        results = {}

        for channel in channels:
            try:
                if channel == NotificationChannel.EMAIL:
                    result = self._send_email_notification(
                        user, notification_type, template_data, user_language, template_override=template_override
                    )
                elif channel == NotificationChannel.SMS:
                    # SMS is OTP-only (see OTP_SMS_TEMPLATES). OTPs are
                    # delivered by send_sms_to_phone(); this generic fan-out
                    # must never reach the provider, whatever channel a caller
                    # or a stored preference asks for.
                    result = {"success": False, "skipped": True, "reason": "sms_is_otp_only"}
                elif channel == NotificationChannel.TELEGRAM:
                    result = self._send_telegram_notification(
                        user, notification_type, template_data, user_language, template_override=template_override
                    )
                elif channel == NotificationChannel.IN_APP:
                    result = self._send_in_app_notification(
                        user, notification_type, template_data, user_language, template_override=template_override
                    )
                elif channel == NotificationChannel.PUSH:
                    result = self._send_push_notification(
                        user, notification_type, template_data, user_language, template_override=template_override
                    )
                else:
                    result = {"success": False, "error": f"Unsupported channel: {channel.value}"}

                results[channel.value] = result

            except Exception as e:
                logger.exception("Failed to send %s notification", channel.value)
                results[channel.value] = {"success": False, "error": str(e)}

        # Create notification record
        self._create_notification_record(
            user_id, notification_type, channels, template_data, results, campaign_id=campaign_id
        )

        return results

    def send_bulk_notification(
        self, user_ids: List[int], notification_type: NotificationType, template_data: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Send notification to multiple users"""
        results = {"successful": 0, "failed": 0, "errors": []}

        for user_id in user_ids:
            try:
                self.send_notification(user_id, notification_type, None, template_data)
                results["successful"] += 1
            except Exception as e:
                results["failed"] += 1
                results["errors"].append({"user_id": user_id, "error": str(e)})

        return results

    def send_order_notification(self, order_id: int, event_type: str) -> Dict[str, Any]:
        """Send order-related notification"""
        order = Order.query.get(order_id)
        if not order:
            raise NotificationError(get_translation("error.not_found"))

        # Map event types to notification types
        event_mapping = {
            "order_created": NotificationType.ORDER_CONFIRMATION,
            "status_changed_confirmed": NotificationType.ORDER_STATUS_UPDATE,
            "status_changed_preparing": NotificationType.ORDER_STATUS_UPDATE,
            "status_changed_out_for_delivery": NotificationType.DELIVERY_UPDATE,
            "status_changed_delivered": NotificationType.DELIVERY_UPDATE,
            "status_changed_cancelled": NotificationType.ORDER_STATUS_UPDATE,
            "order_edited": NotificationType.ORDER_EDITED,
        }

        notification_type = event_mapping.get(event_type, NotificationType.ORDER_STATUS_UPDATE)

        template_data = {
            "order_number": order.order_number,
            "order_status": order.status.value,
            "order_total": float(order.total_amount) if order.total_amount is not None else None,
            "delivery_address": order.delivery_address.street_address if order.delivery_address else None,
            "estimated_delivery": (
                order.estimated_delivery_time.isoformat()
                if hasattr(order, "estimated_delivery_time") and order.estimated_delivery_time
                else None
            ),
            "items": [
                {
                    "name": item.product.name if item.product else "Unknown",
                    "quantity": item.quantity,
                    "price": float(item.total_price) if item.total_price is not None else None,
                }
                for item in order.order_items
            ],
        }

        if notification_type == NotificationType.DELIVERY_UPDATE:
            order_status_value = self._status_value(order.status)
            delivery = Delivery.query.filter_by(order_id=order.id).first()
            language = getattr(order.user, "preferred_language", "en") if order.user else "en"
            template_data.update(
                {
                    "delivery_status_code": order_status_value,
                    "delivery_status": self._get_localized_delivery_status_label(order_status_value, language),
                    "tracking_code": (
                        delivery.tracking_number if delivery and getattr(delivery, "tracking_number", None) else ""
                    ),
                }
            )

        return self.send_notification(order.user_id, notification_type, None, template_data)

    def send_delivery_notification(self, delivery_id: int, event_type: str) -> Dict[str, Any]:
        """Send delivery-related notification"""
        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            raise NotificationError(get_translation("error.not_found"))

        template_data = {
            "tracking_code": delivery.tracking_number,
            "order_number": delivery.order.order_number if delivery.order else "",
            "delivery_status": delivery.status.value,
            "estimated_delivery": (
                delivery.estimated_delivery_time.isoformat() if delivery.estimated_delivery_time else None
            ),
            "event_type": event_type,
        }

        return self.send_notification(delivery.order.user_id, NotificationType.DELIVERY_UPDATE, None, template_data)

    def send_delivery_status_change_notification(self, history_id: int) -> Dict[str, Any]:
        """Send delivery-status notification from a committed history event snapshot."""
        history = DeliveryStatusHistory.query.get(history_id)
        if not history:
            logger.warning("Delivery status history %s not found; skipping notification", history_id)
            return {"success": False, "error": "Delivery status history not found"}

        delivery = Delivery.query.get(history.delivery_id)
        if not delivery:
            logger.warning(
                "Delivery %s not found for delivery status history %s; skipping notification",
                history.delivery_id,
                history_id,
            )
            return {"success": False, "error": "Delivery not found"}

        order = delivery.order
        if not order:
            logger.warning(
                "Order missing for delivery %s (history %s); skipping notification",
                delivery.id,
                history_id,
            )
            return {"success": False, "error": "Order not found"}

        user = User.query.get(order.user_id)
        if not user:
            logger.warning(
                "User %s not found for delivery %s (history %s); skipping notification",
                order.user_id,
                delivery.id,
                history_id,
            )
            return {"success": False, "error": "User not found"}

        history_status = self._status_value(history.new_status)
        live_status = self._status_value(delivery.status)
        if history_status != live_status:
            logger.warning(
                "Delivery status mismatch for history %s: event_status=%s live_status=%s delivery_id=%s",
                history_id,
                history_status,
                live_status,
                delivery.id,
            )

        # Delivered: dispatch the bottle summary webhook to the customer bot
        # instead of the (empty-channel) status notification. One hook covers all
        # three DELIVERED code paths (design §2/§3.2).
        if history_status == DeliveryStatus.DELIVERED.value:
            return self._dispatch_bottle_delivery_summary(history, delivery, order, user)

        language = getattr(user, "preferred_language", "en") or "en"
        template_data = self._build_delivery_status_template_data(
            delivery=delivery,
            history=history,
            language=language,
        )
        channels = self._resolve_delivery_status_channels(user, history_status)

        return self.send_notification(
            user.id,
            NotificationType.DELIVERY_UPDATE,
            channels,
            template_data,
        )

    def _dispatch_bottle_delivery_summary(self, history, delivery, order, user) -> Dict[str, Any]:
        """Send the delivered bottle summary to the customer's Telegram (design §3.2).

        Hooked from send_delivery_status_change_notification's delivered branch.
        Returns a result dict; RAISES to trigger send_delivery_update_task's retry
        when the dispatch must be re-attempted (ledger not committed yet, or a
        transient bot-webhook failure).
        """
        # Step 1 — no Telegram means nothing to send (email fallback is a non-goal).
        if not getattr(user, "telegram_id", None):
            logger.info(
                "Bottle summary skipped: user %s has no telegram_id (order %s)",
                user.id,
                order.id,
            )
            return {"success": True, "skipped": True, "reason": "no_telegram"}

        # Step 1b — a legacy/non-numeric telegram_id can't address a chat. Skip
        # with the same skipped shape rather than raising ValueError deep in
        # payload-building (which would burn the task's 3 retries on an
        # unfixable value). Do this BEFORE claiming the idempotency key so the
        # (never-sent) summary isn't marked dispatched.
        try:
            telegram_chat_id = int(user.telegram_id)
        except (TypeError, ValueError):
            logger.warning(
                "Bottle summary skipped: user %s has a non-numeric telegram_id %r (order %s)",
                user.id,
                user.telegram_id,
                order.id,
            )
            return {"success": True, "skipped": True, "reason": "invalid_telegram_id"}

        # Step 2 — read the SSOT summary.
        summary = BottleTrackingService.get_order_bottle_summary(order)

        # Step 3 — readiness guard (fixes the §2 commit race): a bottle-bearing
        # order whose DELIVERY ledger row is not yet visible means the ledger
        # commit is still in flight on the DeliveryService/OrderService-direct
        # paths. Raise WITHOUT claiming the idempotency key so the task retries
        # after the commit lands. A genuinely non-bottle order (expected 0)
        # proceeds — no ledger row will ever exist.
        if summary["expected_bottles"] > 0 and not summary["delivery_recorded"]:
            logger.info(
                "Bottle summary not ready for order %s: expected=%s but no DELIVERY ledger row yet; will retry",
                order.id,
                summary["expected_bottles"],
            )
            raise BottleSummaryNotReady(f"Bottle ledger not committed yet for order {order.id}")

        # Step 4 — claim idempotency (72h TTL, above the task's max retry window).
        idempotency_key = f"notif:bottle_summary:{order.id}"
        claimed = redis_client.set(idempotency_key, 1, nx=True, ex=259200)
        if not claimed:
            logger.info("Bottle summary already dispatched for order %s, skipping", order.id)
            return {"success": True, "skipped": True, "reason": "already_dispatched"}

        # Step 5 — build the webhook payload (quantities pre-formatted).
        payload = {
            "order_id": order.id,
            "order_number": order.order_number,
            "telegram_id": telegram_chat_id,
            "bottles_delivered": format_bottle_quantity(summary["bottles_delivered"]),
            "bottles_collected": format_bottle_quantity(summary["bottles_collected"]),
            "balance": format_bottle_quantity(summary["balance"]),
        }

        # Step 6 — dispatch. trigger_bot_webhook never raises; inspect success.
        result = trigger_bot_webhook("/internal/delivery-completed", payload)
        if result.get("success"):
            logger.info("Bottle delivery summary dispatched for order %s", order.id)
            return {"success": True, "dispatched": True, "order_id": order.id}

        # Failure — the key is only durable after a successful dispatch, so
        # release it in both failure branches (§4: "key not kept").
        message = str(result.get("message", ""))
        redis_client.delete(idempotency_key)
        if "not configured" in message.lower():
            # Unfixable misconfiguration — retrying cannot help; do NOT raise.
            logger.error(
                "Bottle summary dispatch failed for order %s: bot webhook not configured (%s); not retrying",
                order.id,
                message,
            )
            return {"success": False, "reason": "bot_webhook_unconfigured", "message": message}

        # Transient (bot unreachable / non-2xx / timeout) — raise so the task retries.
        logger.error(
            "Bottle summary dispatch failed for order %s (transient: %s); released key, retrying",
            order.id,
            message,
        )
        raise RuntimeError(f"Bottle summary webhook failed for order {order.id}: {message}")

    def send_payment_notification(self, payment_id: int) -> Dict[str, Any]:
        """Send payment confirmation notification via Telegram (if user has telegram) or email"""
        payment = Payment.query.get(payment_id)
        if not payment:
            raise NotificationError(get_translation("error.not_found"))

        user = User.query.get(payment.user_id)
        if not user:
            raise NotificationError(get_translation("error.not_found"))

        language = self._normalize_language_code(getattr(user, "preferred_language", "en") or "en")

        template_data = {
            "order_number": payment.order.order_number if payment.order else "N/A",
            "payment_amount": float(payment.amount) if payment.amount is not None else 0.0,
            "payment_method": payment.payment_method.value if payment.payment_method else "unknown",
            "payment_reference": payment.payment_id,  # Use payment_id as reference
            "payment_follow_up_message": self._get_payment_follow_up_message(payment, language),
        }

        # Determine channels: use Telegram if user has telegram_id, otherwise email
        channels = []
        if user.telegram_id:
            channels.append(NotificationChannel.TELEGRAM)
        else:
            channels.append(NotificationChannel.EMAIL)

        return self.send_notification(payment.user_id, NotificationType.PAYMENT_CONFIRMATION, channels, template_data)

    def send_subscription_notification(self, subscription_id: int, event_type: str) -> Dict[str, Any]:
        """Send subscription-related notification"""
        subscription = Subscription.query.get(subscription_id)
        if not subscription:
            raise NotificationError(get_translation("error.not_found"))

        template_data = {
            "subscription_id": subscription.id,
            "plan_name": subscription.name or "Standard",
            "frequency": subscription.billing_cycle.value,
            "total_amount": float(subscription.billing_amount) if subscription.billing_amount is not None else None,
            "next_billing_date": subscription.next_billing_date.isoformat() if subscription.next_billing_date else None,
            "event_type": event_type,
        }

        return self.send_notification(subscription.user_id, NotificationType.SUBSCRIPTION_REMINDER, None, template_data)

    def send_loyalty_notification(
        self, user_id: int, event_type: str, data: Dict[str, Any], notification_type: NotificationType = None
    ) -> Dict[str, Any]:
        """Send loyalty program notification

        Args:
            user_id: User to notify
            event_type: Type of loyalty event (earned, redeemed, etc.)
            data: Template data
            notification_type: Notification type to use (defaults to LOYALTY_REWARD)
        """
        template_data = {"event_type": event_type, **data}

        # Route on event_type. An explicit notification_type still wins for
        # back-compat, but an UNMAPPED event must never fall through to
        # LOYALTY_REWARD: that fallback is what made a tier_upgrade event
        # render the "AquaCoins earned" template with raw {points}/{balance}.
        notif_type = notification_type or LOYALTY_EVENT_NOTIFICATION_TYPES.get(event_type)
        if notif_type is None:
            logger.error(
                "Refusing to send unmapped loyalty event: user_id=%s event_type=%s. "
                "Add it to LOYALTY_EVENT_NOTIFICATION_TYPES with its own template.",
                user_id,
                event_type,
            )
            return {"skipped": True, "reason": "unmapped_loyalty_event", "event_type": event_type}

        user = User.query.get(user_id)
        if not user:
            raise NotificationError(get_translation("error.not_found"))

        language = getattr(user, "preferred_language", None) or "uz"

        # Inject the per-language labels the templates interpolate. Always
        # inject (even when the caller omits the source field) so no label
        # placeholder can leak as a literal.
        if notif_type == NotificationType.LOYALTY_REWARD and "reason_label" not in template_data:
            template_data["reason_label"] = self._loyalty_reason_label(template_data.get("reason") or "", language)
        if notif_type == NotificationType.LOYALTY_TIER_UPGRADE and "tier_label" not in template_data:
            template_data["tier_label"] = self._loyalty_tier_label(
                template_data.get("tier_config_id"), template_data.get("tier") or "", language
            )

        # Telegram-else-email for every customer-facing AquaCoins message —
        # never both, never SMS (same policy as coin awards).
        if notif_type in _LOYALTY_SINGLE_CHANNEL_TYPES:
            channels = self._resolve_loyalty_award_channels(user)
        else:
            channels = None

        return self.send_notification(user_id, notif_type, channels, template_data)

    def update_notification_preferences(self, user_id: int, preferences: Dict[str, Any]) -> bool:
        """Update user's notification preferences"""
        try:
            for notification_type, channels in preferences.items():
                # Remove existing preferences for this type
                NotificationPreference.query.filter_by(user_id=user_id, notification_type=notification_type).delete()

                # Add new preferences
                for channel, enabled in channels.items():
                    if enabled:
                        preference = NotificationPreference(
                            user_id=user_id, notification_type=notification_type, channel=channel, is_enabled=True
                        )
                        db.session.add(preference)

            db.session.commit()
            return True

        except Exception:
            logger.exception("Failed to update notification preferences")
            db.session.rollback()
            return False

    def get_notification_preferences(self, user_id: int) -> Dict[str, Any]:
        """Get user's notification preferences"""
        preferences = NotificationPreference.query.filter_by(user_id=user_id, is_enabled=True).all()

        result = {}
        for pref in preferences:
            if pref.notification_type not in result:
                result[pref.notification_type] = []
            result[pref.notification_type].append(pref.channel)

        return result

    def create_notification_template(
        self,
        name: str,
        notification_type: NotificationType,
        channel: NotificationChannel,
        language: str,
        subject: str = None,
        content: str = None,
    ) -> NotificationTemplate:
        """Create notification template"""
        template = NotificationTemplate(
            name=name,
            notification_type=notification_type,
            channel=channel,
            language=language,
            subject=subject,
            content=content,
            is_active=True,
        )

        db.session.add(template)
        db.session.commit()

        return template

    def get_user_notifications_paginated(
        self,
        user_id: int,
        page: int,
        per_page: int,
        status: Optional[str] = None,
        notification_type: Optional[str] = None,
        channel: Optional[str] = None,
        unread_only: bool = False,
    ) -> Dict[str, Any]:
        """Get paginated notifications for a user with filters."""
        query = Notification.query.filter_by(user_id=user_id)

        if status:
            try:
                query = query.filter_by(delivery_status=NotificationStatus(status))
            except ValueError as exc:
                raise ValidationError("Invalid status value") from exc

        if notification_type:
            try:
                NotificationType(notification_type)
            except ValueError as exc:
                raise ValidationError("Invalid notification type") from exc
            query = query.filter_by(notification_type=notification_type)

        if channel:
            try:
                query = query.filter_by(channel=NotificationChannel(channel))
            except ValueError as exc:
                raise ValidationError("Invalid channel value") from exc

        if unread_only:
            query = query.filter(Notification.delivery_status != NotificationStatus.READ)

        query = query.order_by(Notification.created_at.desc())
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        unread_count = (
            Notification.query.filter_by(user_id=user_id)
            .filter(Notification.delivery_status != NotificationStatus.READ)
            .count()
        )

        return {
            "items": pagination.items,
            "page": page,
            "per_page": per_page,
            "total": pagination.total,
            "unread_count": unread_count,
        }

    def get_notification_for_user(
        self,
        notification_id: int,
        user_id: int,
        mark_as_read: bool = False,
    ) -> Notification:
        """Get a user-owned notification and optionally mark it as read."""
        notification = Notification.query.filter_by(
            id=notification_id,
            user_id=user_id,
        ).first()
        if not notification:
            raise NotFoundError("Notification not found")

        if mark_as_read and notification.delivery_status != NotificationStatus.READ:
            self._mark_notification_read(notification)
            db.session.commit()

        return notification

    def mark_notification_read(self, notification_id: int, user_id: int) -> None:
        """Mark one notification as read."""
        notification = self.get_notification_for_user(notification_id, user_id, mark_as_read=False)
        if notification.delivery_status != NotificationStatus.READ:
            self._mark_notification_read(notification)
            db.session.commit()

    def mark_all_notifications_read(self, user_id: int) -> int:
        """Mark all unread notifications for a user as read and return count."""
        unread_notifications = (
            Notification.query.filter_by(user_id=user_id)
            .filter(Notification.delivery_status != NotificationStatus.READ)
            .all()
        )

        for notification in unread_notifications:
            self._mark_notification_read(notification)

        db.session.commit()
        return len(unread_notifications)

    def delete_notification_for_user(self, notification_id: int, user_id: int) -> None:
        """Delete a user-owned notification."""
        notification = Notification.query.filter_by(
            id=notification_id,
            user_id=user_id,
        ).first()
        if not notification:
            raise NotFoundError("Notification not found")

        db.session.delete(notification)
        db.session.commit()

    def create_default_preferences(self, user_id: int):
        """Ensure user has default notification preference rows and return a view object."""
        existing_count = NotificationPreference.query.filter_by(user_id=user_id).count()
        if existing_count == 0:
            for group_types in self.NOTIFICATION_TYPE_GROUPS.values():
                for type_value in group_types:
                    for channel in self._default_channels_for_type(type_value):
                        self._ensure_preference_row(user_id, type_value, channel)
            db.session.commit()

        return self.get_notification_preferences_view(user_id)

    def get_notification_preferences_view(self, user_id: int):
        """Return notification preferences as a serializer-friendly object."""
        rows = NotificationPreference.query.filter_by(user_id=user_id, is_enabled=True).all()
        mapped = self._map_preferences(rows)
        all_types = self._all_managed_types()
        delivery_telegram_setting = self.get_delivery_telegram_status_updates_setting(user_id)

        channel_enabled = {
            NotificationChannel.EMAIL.value: False,
            NotificationChannel.SMS.value: False,
            NotificationChannel.PUSH.value: False,
            NotificationChannel.IN_APP.value: False,
            NotificationChannel.TELEGRAM.value: False,
        }
        for channels in mapped.values():
            for channel_value in channels:
                channel_enabled[channel_value] = True

        def _group_enabled(group_name: str) -> bool:
            group_types = self.NOTIFICATION_TYPE_GROUPS[group_name]
            for type_value in group_types:
                if mapped.get(type_value):
                    return True
            return False

        return SimpleNamespace(
            user_id=user_id,
            email_enabled=channel_enabled[NotificationChannel.EMAIL.value],
            sms_enabled=channel_enabled[NotificationChannel.SMS.value],
            push_enabled=channel_enabled[NotificationChannel.PUSH.value],
            in_app_enabled=channel_enabled[NotificationChannel.IN_APP.value],
            telegram_enabled=channel_enabled[NotificationChannel.TELEGRAM.value],
            order_notifications=_group_enabled("order"),
            delivery_notifications=_group_enabled("delivery"),
            payment_notifications=_group_enabled("payment"),
            promotion_notifications=_group_enabled("promotion"),
            system_notifications=_group_enabled("system"),
            loyalty_notifications=_group_enabled("loyalty"),
            security_notifications=_group_enabled("security"),
            reminder_notifications=_group_enabled("reminder"),
            quiet_hours_enabled=False,
            quiet_hours_start=None,
            quiet_hours_end=None,
            digest_enabled=False,
            digest_frequency="weekly",
            updated_at=datetime.now(timezone.utc),
            delivery_telegram_status_updates_enabled=delivery_telegram_setting[
                "delivery_telegram_status_updates_enabled"
            ],
            _mapped_preferences=mapped,
            _all_types=all_types,
        )

    def update_notification_preferences_for_user(self, user_id: int, payload: Dict[str, Any]):
        """Update notification preferences from API payload and return current view."""
        self._map_preferences(NotificationPreference.query.filter_by(user_id=user_id, is_enabled=True).all())
        all_types = self._all_managed_types()

        channel_flags = {
            NotificationChannel.EMAIL: payload.get("email_enabled"),
            NotificationChannel.SMS: payload.get("sms_enabled"),
            NotificationChannel.PUSH: payload.get("push_enabled"),
            NotificationChannel.IN_APP: payload.get("in_app_enabled"),
            NotificationChannel.TELEGRAM: payload.get("telegram_enabled"),
        }
        for channel, enabled in channel_flags.items():
            if enabled is None:
                continue
            for type_value in all_types:
                if enabled:
                    self._ensure_preference_row(user_id, type_value, channel)
                else:
                    NotificationPreference.query.filter_by(
                        user_id=user_id,
                        notification_type=type_value,
                        channel=channel,
                    ).delete()

        category_flag_map = {
            "order_notifications": "order",
            "order_updates": "order",
            "delivery_notifications": "delivery",
            "delivery_updates": "delivery",
            "payment_notifications": "payment",
            "payment_updates": "payment",
            "promotion_notifications": "promotion",
            "marketing_emails": "promotion",
            "promotional_sms": "promotion",
            "system_notifications": "system",
            "system_alerts": "system",
            "loyalty_notifications": "loyalty",
            "loyalty_updates": "loyalty",
            "security_notifications": "security",
            "reminder_notifications": "reminder",
            "subscription_updates": "subscription",
        }
        for flag_key, group_name in category_flag_map.items():
            enabled = payload.get(flag_key)
            if enabled is None:
                continue

            group_types = self.NOTIFICATION_TYPE_GROUPS[group_name]
            if not enabled:
                NotificationPreference.query.filter(
                    NotificationPreference.user_id == user_id,
                    NotificationPreference.notification_type.in_(group_types),
                ).delete(synchronize_session=False)
                continue

            preferred_channels = self._enabled_channels_from_payload(payload)
            if not preferred_channels:
                preferred_channels = None
            for type_value in group_types:
                channels_to_apply = preferred_channels or self._default_channels_for_type(type_value)
                for channel in channels_to_apply:
                    self._ensure_preference_row(user_id, type_value, channel)

        delivery_telegram_status_updates_enabled = payload.get("delivery_telegram_status_updates_enabled")
        if delivery_telegram_status_updates_enabled is not None:
            if not isinstance(delivery_telegram_status_updates_enabled, bool):
                raise ValidationError("delivery_telegram_status_updates_enabled must be a boolean")
            self._set_delivery_telegram_status_updates_row(
                user_id=user_id,
                enabled=delivery_telegram_status_updates_enabled,
            )

        db.session.commit()
        return self.get_notification_preferences_view(user_id)

    def get_delivery_telegram_status_updates_setting(self, user_id: int) -> Dict[str, Any]:
        """Get effective Telegram delivery-status notification setting for a user."""
        normalized_user_id = self._coerce_user_id(user_id)
        user = User.query.get(normalized_user_id)
        if not user:
            raise NotFoundError("User not found")

        row = (
            NotificationPreference.query.filter_by(
                user_id=normalized_user_id,
                notification_type=self.DELIVERY_TELEGRAM_STATUS_UPDATES_PREF_KEY,
                channel=NotificationChannel.TELEGRAM,
            )
            .order_by(
                NotificationPreference.updated_at.desc(),
                NotificationPreference.created_at.desc(),
                NotificationPreference.id.desc(),
            )
            .first()
        )
        enabled = True if row is None else bool(row.is_enabled)
        source = "default" if row is None else "explicit"
        updated_at = row.updated_at or row.created_at if row else None

        return {
            "delivery_telegram_status_updates_enabled": enabled,
            "delivery_telegram_status_updates_source": source,
            "telegram_connected": bool(getattr(user, "telegram_id", None)),
            "bot_active": bool(getattr(user, "is_bot_active", False)),
            "updated_at": updated_at.isoformat() if updated_at else None,
        }

    def set_delivery_telegram_status_updates_setting(
        self,
        user_id: int,
        enabled: bool,
        *,
        source: str = "user",
        actor_user_id: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Persist Telegram delivery-status setting with optional admin audit trail."""
        normalized_user_id = self._coerce_user_id(user_id)

        if not isinstance(enabled, bool):
            raise ValidationError("delivery_telegram_status_updates_enabled must be a boolean")
        if source not in {"user", "admin"}:
            raise ValidationError("source must be either user or admin")

        reason_text = (reason or "").strip()
        if source == "admin":
            if not reason_text:
                raise ValidationError("Reason is required for admin updates")

        current = self.get_delivery_telegram_status_updates_setting(normalized_user_id)
        self._set_delivery_telegram_status_updates_row(user_id=normalized_user_id, enabled=enabled)
        db.session.commit()

        updated = self.get_delivery_telegram_status_updates_setting(normalized_user_id)
        if source == "admin":
            try:
                from business_app.models.audit import AuditEventType, AuditSeverity
                from business_app.utils.audit_logger import audit_logger

                audit_logger.log_event(
                    event_type=AuditEventType.USER_UPDATED,
                    action="admin_update_delivery_telegram_notification_setting",
                    severity=AuditSeverity.MEDIUM,
                    resource_type="user",
                    resource_id=str(normalized_user_id),
                    description="Admin updated Telegram delivery notification setting",
                    old_values={
                        "delivery_telegram_status_updates_enabled": current["delivery_telegram_status_updates_enabled"],
                        "delivery_telegram_status_updates_source": current["delivery_telegram_status_updates_source"],
                    },
                    new_values={
                        "delivery_telegram_status_updates_enabled": updated["delivery_telegram_status_updates_enabled"],
                        "delivery_telegram_status_updates_source": updated["delivery_telegram_status_updates_source"],
                    },
                    additional_data={
                        "reason": reason_text,
                        "actor_user_id": actor_user_id,
                        "setting_key": self.DELIVERY_TELEGRAM_STATUS_UPDATES_PREF_KEY,
                    },
                    success=True,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to write admin notification setting audit log: user_id=%s actor_user_id=%s error=%s",
                    normalized_user_id,
                    actor_user_id,
                    exc,
                )

        return updated

    def register_push_token_for_user(
        self,
        user_id: int,
        token: str,
        platform: str,
        device_id: Optional[str] = None,
    ) -> None:
        """Register or update a push token for a user."""
        if platform not in ["ios", "android", "web"]:
            raise ValidationError("Invalid platform")

        existing = PushNotificationToken.query.filter_by(token=token).first()
        if existing:
            existing.user_id = user_id
            existing.is_active = True
            existing.updated_at = datetime.now(timezone.utc)
        else:
            db.session.add(
                PushNotificationToken(
                    user_id=user_id,
                    token=token,
                    platform=platform,
                    device_id=device_id,
                    is_active=True,
                )
            )
        db.session.commit()

    def unregister_push_token_for_user(self, user_id: int, token: str) -> None:
        """Deactivate a push token for a user."""
        push_token = PushNotificationToken.query.filter_by(user_id=user_id, token=token).first()
        if push_token:
            push_token.is_active = False
            push_token.updated_at = datetime.now(timezone.utc)
            db.session.commit()

    def get_active_templates(self, category: Optional[str] = None) -> List[NotificationTemplate]:
        """Get active notification templates, optionally filtered by type/category."""
        query = NotificationTemplate.query.filter_by(is_active=True)
        if category:
            query = query.filter_by(notification_type=category)
        return query.order_by(NotificationTemplate.notification_type, NotificationTemplate.name).all()

    def send_test_notification_from_template(
        self,
        user_id: int,
        template_id: int,
        channel: str,
        test_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Send test notification based on template and return latest notification id."""
        template = NotificationTemplate.query.filter_by(id=template_id, is_active=True).first()
        if not template:
            raise NotFoundError("Template not found")

        try:
            notification_type = NotificationType(template.notification_type)
        except ValueError as exc:
            raise ValidationError("Template has invalid notification type") from exc

        try:
            channel_enum = NotificationChannel(channel)
        except ValueError as exc:
            raise ValidationError("Invalid channel value") from exc

        self.send_notification(
            user_id=user_id,
            notification_type=notification_type,
            channels=[channel_enum],
            template_data=test_data or {},
        )

        created = (
            Notification.query.filter_by(
                user_id=user_id,
                notification_type=notification_type.value,
                channel=channel_enum,
            )
            .order_by(Notification.created_at.desc())
            .first()
        )
        return {"notification_id": created.id if created else None}

    def get_notification_statistics_for_user(self, user_id: int, period: str = "month") -> Dict[str, Any]:
        """Get notification statistics for a user over a period."""
        now = datetime.now(timezone.utc)
        if period == "week":
            start_date = now - timedelta(weeks=1)
        elif period == "month":
            start_date = now - timedelta(days=30)
        elif period == "quarter":
            start_date = now - timedelta(days=90)
        elif period == "year":
            start_date = now - timedelta(days=365)
        else:
            start_date = now - timedelta(days=30)

        base_query = Notification.query.filter_by(user_id=user_id).filter(Notification.created_at >= start_date)

        total_notifications = base_query.count()
        read_notifications = base_query.filter_by(delivery_status=NotificationStatus.READ).count()
        unread_notifications = total_notifications - read_notifications

        notifications_by_type = {}
        type_stats = (
            db.session.query(Notification.notification_type, func.count(Notification.id))
            .filter_by(user_id=user_id)
            .filter(Notification.created_at >= start_date)
            .group_by(Notification.notification_type)
            .all()
        )
        for type_value, count in type_stats:
            notifications_by_type[type_value] = count

        notifications_by_channel = {}
        channel_stats = (
            db.session.query(Notification.channel, func.count(Notification.id))
            .filter_by(user_id=user_id)
            .filter(Notification.created_at >= start_date)
            .group_by(Notification.channel)
            .all()
        )
        for channel_value, count in channel_stats:
            key = channel_value.value if hasattr(channel_value, "value") else str(channel_value)
            notifications_by_channel[key] = count

        daily_stats = (
            db.session.query(
                func.date(Notification.created_at).label("date"),
                func.count(Notification.id).label("count"),
            )
            .filter_by(user_id=user_id)
            .filter(Notification.created_at >= start_date)
            .group_by(func.date(Notification.created_at))
            .all()
        )
        daily_notifications = {date_obj.isoformat(): count for date_obj, count in daily_stats}

        return {
            "period": period,
            "statistics": {
                "total_notifications": total_notifications,
                "read_notifications": read_notifications,
                "unread_notifications": unread_notifications,
                "read_rate": (
                    round((read_notifications / total_notifications * 100), 2) if total_notifications > 0 else 0
                ),
                "notifications_by_type": notifications_by_type,
                "notifications_by_channel": notifications_by_channel,
                "daily_trend": daily_notifications,
            },
        }

    def get_user_notification_channels(self, user_id: int) -> Dict[str, Any]:
        """Get available channels for a user."""
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")

        push_tokens = PushNotificationToken.query.filter_by(user_id=user_id, is_active=True).all()

        return {
            "email": {
                "available": bool(user.email and user.email_verified),
                "address": user.email if user.email_verified else None,
                "verified": user.email_verified,
            },
            "sms": {
                "available": bool(user.phone and user.phone_verified),
                "number": user.phone if user.phone_verified else None,
                "verified": user.phone_verified,
            },
            "push": {
                "available": len(push_tokens) > 0,
                "devices": [
                    {
                        "platform": token.platform,
                        "device_id": token.device_id,
                        "registered_at": token.created_at.isoformat(),
                    }
                    for token in push_tokens
                ],
            },
            "telegram": {
                "available": bool(getattr(user, "telegram_id", None)),
                "chat_id": getattr(user, "telegram_id", None),
            },
        }

    def queue_bulk_notification(
        self,
        sender_id: int,
        user_ids: List[int],
        template_code: str,
        template_data: Optional[Dict[str, Any]],
        channels: List[str],
    ) -> Dict[str, Any]:
        """Validate and queue bulk notifications (admin-only)."""
        sender = User.query.get(sender_id)
        if not sender or not sender.is_admin:
            raise ForbiddenError("Admin access required")

        if not isinstance(user_ids, list) or len(user_ids) > 1000:
            raise ValidationError("Invalid user_ids or too many recipients (max 1000)")
        if not isinstance(channels, list) or len(channels) == 0:
            raise ValidationError("channels must be a non-empty list")

        template = NotificationTemplate.query.filter_by(
            name=template_code,
            is_active=True,
        ).first()
        if not template:
            raise NotFoundError("Template not found")

        normalized_channels: List[str] = []
        for channel in channels:
            try:
                normalized_channels.append(NotificationChannel(channel).value)
            except ValueError as exc:
                raise ValidationError("Invalid channel value") from exc

        from business_app.tasks.notification_tasks import send_bulk_notification_task

        task = send_bulk_notification_task.delay(
            notification_type=template.notification_type,
            recipient_ids=user_ids,
            template_data=template_data or {},
            channels=normalized_channels,
        )
        return {"task_id": task.id, "recipient_count": len(user_ids)}

    def get_delivery_reports_paginated(
        self,
        requester_id: int,
        page: int,
        per_page: int,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get paginated delivery reports for notifications (admin-only)."""
        requester = User.query.get(requester_id)
        if not requester or not requester.is_admin:
            raise ForbiddenError("Admin access required")

        query = Notification.query

        if start_date:
            try:
                query = query.filter(Notification.created_at >= datetime.fromisoformat(start_date))
            except ValueError as exc:
                raise ValidationError("Invalid start_date format") from exc

        if end_date:
            try:
                query = query.filter(Notification.created_at <= datetime.fromisoformat(end_date))
            except ValueError as exc:
                raise ValidationError("Invalid end_date format") from exc

        if channel:
            try:
                query = query.filter_by(channel=NotificationChannel(channel))
            except ValueError as exc:
                raise ValidationError("Invalid channel value") from exc

        query = query.order_by(Notification.created_at.desc())
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        total_sent = query.count()
        delivered = query.filter_by(delivery_status=NotificationStatus.DELIVERED).count()
        failed = query.filter_by(delivery_status=NotificationStatus.FAILED).count()
        pending = query.filter_by(delivery_status=NotificationStatus.PENDING).count()

        reports = []
        for notif in pagination.items:
            delivered_at = None
            if isinstance(notif.extra_data, dict):
                delivered_at = notif.extra_data.get("delivered_at")
            reports.append(
                {
                    "id": notif.id,
                    "user_id": notif.user_id,
                    "channel": notif.channel.value if hasattr(notif.channel, "value") else notif.channel,
                    "status": (
                        notif.delivery_status.value
                        if hasattr(notif.delivery_status, "value")
                        else notif.delivery_status
                    ),
                    "created_at": notif.created_at.isoformat(),
                    "sent_at": notif.sent_at.isoformat() if notif.sent_at else None,
                    "delivered_at": delivered_at,
                    "error_message": notif.failure_reason,
                }
            )

        return {
            "items": reports,
            "page": page,
            "per_page": per_page,
            "total": pagination.total,
            "summary": {
                "total_sent": total_sent,
                "delivered": delivered,
                "failed": failed,
                "pending": pending,
                "delivery_rate": round((delivered / total_sent * 100), 2) if total_sent > 0 else 0,
            },
        }

    def get_admin_notification_templates_paginated(
        self,
        requester_id: int,
        page: int,
        per_page: int,
        search: Optional[str] = None,
        notification_type: Optional[str] = None,
        channel: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Get paginated admin notification templates."""
        self._require_admin_user(requester_id)

        query = NotificationTemplate.query
        normalized_type = self._normalize_notification_type_filter(notification_type, allow_empty=True)
        normalized_channel = self._normalize_campaign_channel(channel, allow_empty=True)

        if normalized_type:
            query = query.filter_by(notification_type=normalized_type)
        if normalized_channel:
            query = query.filter_by(channel=normalized_channel)
        if is_active is not None:
            query = query.filter_by(is_active=bool(is_active))
        if search:
            search_term = f"%{search.strip()}%"
            query = query.filter(
                NotificationTemplate.name.ilike(search_term)
                | NotificationTemplate.subject.ilike(search_term)
                | NotificationTemplate.content.ilike(search_term)
            )

        pagination = query.order_by(
            NotificationTemplate.notification_type.asc(),
            NotificationTemplate.channel.asc(),
            NotificationTemplate.id.desc(),
        ).paginate(page=page, per_page=per_page, error_out=False)

        return {
            "items": [self._serialize_admin_notification_template(template) for template in pagination.items],
            "page": page,
            "per_page": per_page,
            "total": pagination.total,
        }

    def get_admin_notification_template_detail(self, requester_id: int, template_id: int) -> Dict[str, Any]:
        """Get one admin notification template."""
        self._require_admin_user(requester_id)
        template = self._get_notification_template_or_404(template_id)
        return self._serialize_admin_notification_template(template, include_translations=True)

    def create_admin_notification_template(self, requester_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create an admin notification template."""
        self._require_admin_user(requester_id)
        normalized_payload = self._normalize_admin_notification_template_payload(payload)

        existing = NotificationTemplate.query.filter_by(
            notification_type=normalized_payload["notification_type"],
            channel=normalized_payload["channel"],
        ).first()
        if existing:
            raise ConflictError(
                f'Template already exists for {normalized_payload["notification_type"]} on {normalized_payload["channel"]}'  # noqa: E501
            )

        template = NotificationTemplate(
            name=normalized_payload["name"],
            notification_type=normalized_payload["notification_type"],
            channel=normalized_payload["channel"],
            subject=normalized_payload["subject"],
            content=normalized_payload["content"],
            is_active=normalized_payload["is_active"],
        )
        db.session.add(template)
        db.session.flush()

        if normalized_payload["translations"]:
            template.set_translations(normalized_payload["translations"])

        db.session.commit()
        return self._serialize_admin_notification_template(template, include_translations=True)

    def update_admin_notification_template(
        self,
        requester_id: int,
        template_id: int,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Update an admin notification template."""
        self._require_admin_user(requester_id)
        template = self._get_notification_template_or_404(template_id)
        normalized_payload = self._normalize_admin_notification_template_payload(payload, partial=True)

        next_notification_type = normalized_payload.get("notification_type", template.notification_type)
        next_channel = normalized_payload.get("channel", template.channel)

        duplicate = (
            NotificationTemplate.query.filter_by(
                notification_type=next_notification_type,
                channel=next_channel,
            )
            .filter(NotificationTemplate.id != template.id)
            .first()
        )
        if duplicate:
            raise ConflictError(f"Template already exists for {next_notification_type} on {next_channel}")

        for field in ("name", "notification_type", "channel", "subject", "content", "is_active"):
            if field in normalized_payload:
                setattr(template, field, normalized_payload[field])

        if "translations" in normalized_payload:
            template.set_translations(normalized_payload["translations"])

        db.session.commit()
        return self._serialize_admin_notification_template(template, include_translations=True)

    def delete_admin_notification_template(
        self, requester_id: int, template_id: int, *, reactivate: bool = False
    ) -> Dict[str, Any]:
        """Soft deactivate or reactivate an admin notification template."""
        self._require_admin_user(requester_id)
        template = self._get_notification_template_or_404(template_id)
        template.is_active = bool(reactivate)
        db.session.commit()
        return self._serialize_admin_notification_template(template, include_translations=True)

    def preview_admin_notification_template(
        self,
        requester_id: int,
        template_id: int,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Render a notification template preview."""
        self._require_admin_user(requester_id)
        template = self._get_notification_template_or_404(template_id)
        variables = (payload or {}).get("variables") or {}
        if not isinstance(variables, dict):
            raise ValidationError("variables must be an object")
        language = str((payload or {}).get("language") or "en").strip() or "en"

        subject = (
            template.get_translated("subject", language) if hasattr(template, "get_translated") else template.subject
        )
        content = (
            template.get_translated("content", language) if hasattr(template, "get_translated") else template.content
        )

        return {
            "template_id": template.id,
            "template_name": template.name,
            "notification_type": template.notification_type,
            "channel": template.channel,
            "language": language,
            "subject": self._render_template(subject or "", variables, language),
            "content": self._render_template(content or "", variables, language),
            "variables_used": sorted(variables.keys()),
        }

    def test_send_admin_notification_template(
        self,
        requester_id: int,
        template_id: int,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Send a template test notification to the requesting admin."""
        admin_user = self._require_admin_user(requester_id)
        template = self._get_notification_template_or_404(template_id)
        test_data = (payload or {}).get("variables") or {}
        if not isinstance(test_data, dict):
            raise ValidationError("variables must be an object")

        try:
            notification_type = NotificationType(template.notification_type)
        except ValueError as exc:
            raise ValidationError("Template has invalid notification type") from exc

        result = self.send_notification(
            user_id=admin_user.id,
            notification_type=notification_type,
            channels=[NotificationChannel(template.channel)],
            template_data={
                **self._build_notification_campaign_template_data(
                    NotificationCampaign(
                        name="Test", notification_type=template.notification_type, channel=template.channel
                    ),
                    admin_user,
                ),
                **test_data,
                "title": test_data.get("title") or template.subject or template.name,
                "message": test_data.get("message") or template.content,
            },
            template_override=template,
        )

        created = (
            Notification.query.filter_by(
                user_id=admin_user.id,
                notification_type=template.notification_type,
                channel=NotificationChannel(template.channel),
            )
            .order_by(Notification.created_at.desc())
            .first()
        )
        return {
            "notification_id": created.id if created else None,
            "channel": template.channel,
            "result": result.get(template.channel, {}),
        }

    def get_admin_notification_types(self, requester_id: int) -> List[Dict[str, Any]]:
        """Get supported notification type metadata."""
        self._require_admin_user(requester_id)

        existing_types = {
            row[0] for row in db.session.query(NotificationTemplate.notification_type).distinct().all() if row[0]
        }
        all_types = {notification_type.value for notification_type in NotificationType}
        all_types.update(self._all_managed_types())

        return [
            {
                "value": type_name,
                "label": type_name.replace("_", " ").title(),
                "category": self._notification_type_category(type_name),
                "in_use": type_name in existing_types,
            }
            for type_name in sorted(all_types)
        ]

    def get_admin_notification_channels(self, requester_id: int) -> List[Dict[str, Any]]:
        """Get supported channel metadata for the admin console."""
        self._require_admin_user(requester_id)
        return [
            {
                "value": NotificationChannel.EMAIL.value,
                "label": "Email",
                "requires_subject": True,
                "icon": "email",
                "available": True,
            },
            {
                "value": NotificationChannel.TELEGRAM.value,
                "label": "Telegram",
                "requires_subject": False,
                "icon": "telegram",
                "available": True,
            },
            {
                "value": NotificationChannel.IN_APP.value,
                "label": "In-App Notification",
                "requires_subject": False,
                "icon": "inbox",
                "available": True,
            },
            {
                "value": NotificationChannel.PUSH.value,
                "label": "Push Notification",
                "requires_subject": False,
                "icon": "notifications",
                "available": False,
            },
        ]

    def get_admin_notification_segments(self, requester_id: int) -> List[Dict[str, Any]]:
        """Get available user segments for custom campaign targeting."""
        self._require_admin_user(requester_id)
        segments = UserSegment.query.filter_by(is_active=True).order_by(UserSegment.name.asc()).all()
        return [
            {
                "id": segment.id,
                "name": segment.name,
                "description": segment.description,
                "user_count": segment.user_count,
                "criteria": segment.criteria,
            }
            for segment in segments
        ]

    def get_notification_campaigns_paginated(
        self,
        requester_id: int,
        page: int,
        per_page: int,
        search: Optional[str] = None,
        status: Optional[str] = None,
        channel: Optional[str] = None,
        target_audience: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get paginated notification campaigns (admin-only)."""
        self._require_admin_user(requester_id)

        query = NotificationCampaign.query
        normalized_status = self._normalize_campaign_status(status, allow_empty=True)
        normalized_channel = self._normalize_campaign_channel(channel, allow_empty=True)
        normalized_audience = self._normalize_campaign_audience(target_audience, allow_empty=True)

        if normalized_status:
            query = query.filter_by(status=normalized_status)
        if normalized_channel:
            query = query.filter_by(channel=normalized_channel)
        if normalized_audience:
            query = query.filter_by(target_audience=normalized_audience)
        if search:
            search_term = f"%{search.strip()}%"
            query = query.filter(
                NotificationCampaign.name.ilike(search_term)
                | NotificationCampaign.subject_override.ilike(search_term)
                | NotificationCampaign.content_override.ilike(search_term)
            )
        if start_date:
            query = query.filter(NotificationCampaign.created_at >= self._parse_campaign_datetime(start_date))
        if end_date:
            query = query.filter(NotificationCampaign.created_at <= self._parse_campaign_datetime(end_date))

        pagination = query.order_by(
            NotificationCampaign.created_at.desc(),
            NotificationCampaign.id.desc(),
        ).paginate(page=page, per_page=per_page, error_out=False)

        return {
            "items": [self._serialize_notification_campaign(campaign) for campaign in pagination.items],
            "page": page,
            "per_page": per_page,
            "total": pagination.total,
        }

    def create_notification_campaign(self, sender_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create a draft notification campaign."""
        sender = self._require_admin_user(sender_id)
        normalized_payload = self._normalize_notification_campaign_payload(payload)

        campaign = NotificationCampaign(
            name=normalized_payload["name"],
            template_id=normalized_payload.get("template_id"),
            notification_type=normalized_payload["notification_type"],
            channel=normalized_payload["channel"],
            subject_override=normalized_payload.get("subject_override"),
            content_override=normalized_payload.get("content_override"),
            target_audience=normalized_payload["target_audience"],
            target_segment_id=normalized_payload.get("target_segment_id"),
            specific_user_ids=normalized_payload.get("specific_user_ids", []),
            status="draft",
            priority=normalized_payload["priority"],
            scheduled_at=normalized_payload.get("scheduled_at"),
            created_by_user_id=sender.id,
            updated_by_user_id=sender.id,
        )
        db.session.add(campaign)
        db.session.commit()

        return self._serialize_notification_campaign_detail(campaign)

    def get_notification_campaign_detail(self, requester_id: int, campaign_id: int) -> Dict[str, Any]:
        """Get one notification campaign with delivery summary."""
        self._require_admin_user(requester_id)
        campaign = self._get_notification_campaign_or_404(campaign_id)
        return self._serialize_notification_campaign_detail(campaign)

    def update_notification_campaign(
        self,
        sender_id: int,
        campaign_id: int,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Update a draft or scheduled notification campaign."""
        sender = self._require_admin_user(sender_id)
        campaign = self._get_notification_campaign_or_404(campaign_id)
        self._assert_campaign_editable(campaign)

        normalized_payload = self._normalize_notification_campaign_payload(
            payload,
            require_schedule=False,
        )
        campaign.name = normalized_payload["name"]
        campaign.template_id = normalized_payload.get("template_id")
        campaign.notification_type = normalized_payload["notification_type"]
        campaign.channel = normalized_payload["channel"]
        campaign.subject_override = normalized_payload.get("subject_override")
        campaign.content_override = normalized_payload.get("content_override")
        campaign.target_audience = normalized_payload["target_audience"]
        campaign.target_segment_id = normalized_payload.get("target_segment_id")
        campaign.specific_user_ids = normalized_payload.get("specific_user_ids", [])
        campaign.priority = normalized_payload["priority"]
        campaign.scheduled_at = normalized_payload.get("scheduled_at")
        campaign.updated_by_user_id = sender.id

        if campaign.status == "scheduled":
            campaign.recipient_ids_snapshot = []
            campaign.recipient_count = 0
            campaign.celery_task_id = None
            campaign.queued_at = None
            campaign.started_at = None
            campaign.completed_at = None

        db.session.commit()
        return self._serialize_notification_campaign_detail(campaign)

    def delete_notification_campaign(self, sender_id: int, campaign_id: int) -> None:
        """Delete a draft or cancelled notification campaign."""
        self._require_admin_user(sender_id)
        campaign = self._get_notification_campaign_or_404(campaign_id)
        if campaign.status not in {"draft", "cancelled"}:
            raise ConflictError("Only draft or cancelled campaigns can be deleted")

        db.session.delete(campaign)
        db.session.commit()

    def duplicate_notification_campaign(self, sender_id: int, campaign_id: int) -> Dict[str, Any]:
        """Duplicate a notification campaign into a draft."""
        sender = self._require_admin_user(sender_id)
        source = self._get_notification_campaign_or_404(campaign_id)

        duplicate = NotificationCampaign(
            name=f"{source.name} (Copy)",
            template_id=source.template_id,
            notification_type=source.notification_type,
            channel=source.channel,
            subject_override=source.subject_override,
            content_override=source.content_override,
            target_audience=source.target_audience,
            target_segment_id=source.target_segment_id,
            specific_user_ids=list(source.specific_user_ids or []),
            status="draft",
            priority=source.priority,
            created_by_user_id=sender.id,
            updated_by_user_id=sender.id,
        )
        db.session.add(duplicate)
        db.session.commit()
        return self._serialize_notification_campaign_detail(duplicate)

    def queue_notification_campaign(self, sender_id: int, campaign_id: int, send_now: bool) -> Dict[str, Any]:
        """Queue a notification campaign for immediate or scheduled execution."""
        self._require_admin_user(sender_id)
        campaign = self._get_notification_campaign_or_404(campaign_id)
        self._assert_campaign_editable(campaign)

        recipient_ids = self._resolve_notification_campaign_recipient_ids(campaign)
        if not recipient_ids:
            raise ValidationError("No recipients matched the selected audience")

        scheduled_at = None if send_now else campaign.scheduled_at
        if not send_now and not scheduled_at:
            raise ValidationError("scheduled_at is required to schedule a campaign")

        from business_app.tasks.notification_tasks import execute_notification_campaign_task

        apply_async_kwargs = {}
        if scheduled_at:
            apply_async_kwargs["eta"] = scheduled_at

        task = execute_notification_campaign_task.apply_async(args=[campaign.id], **apply_async_kwargs)

        campaign.recipient_ids_snapshot = recipient_ids
        campaign.recipient_count = len(recipient_ids)
        campaign.celery_task_id = task.id
        campaign.queued_at = datetime.now(timezone.utc)
        campaign.started_at = None
        campaign.completed_at = None
        campaign.cancelled_at = None
        campaign.last_error = None
        campaign.status = "sending" if send_now else "scheduled"
        db.session.commit()

        return self._serialize_notification_campaign_detail(campaign)

    def cancel_notification_campaign(self, sender_id: int, campaign_id: int) -> Dict[str, Any]:
        """Cancel a scheduled or queued campaign."""
        self._require_admin_user(sender_id)
        campaign = self._get_notification_campaign_or_404(campaign_id)
        if campaign.status not in {"scheduled", "sending"}:
            raise ConflictError("Only scheduled or sending campaigns can be cancelled")

        if campaign.celery_task_id:
            try:
                from business_app.tasks.celery_app import celery

                celery.control.revoke(campaign.celery_task_id, terminate=False)
            except Exception as exc:
                logger.warning("Failed to revoke notification campaign task %s: %s", campaign.celery_task_id, exc)

        campaign.status = "cancelled"
        campaign.cancelled_at = datetime.now(timezone.utc)
        campaign.last_error = None
        db.session.commit()
        return self._serialize_notification_campaign_detail(campaign)

    def _mark_notification_read(self, notification: Notification) -> None:
        """Mark a notification as read using delivery status and metadata."""
        notification.delivery_status = NotificationStatus.READ
        # JSON columns are not always mutation-tracked; assign a copied dict.
        source = notification.extra_data if isinstance(notification.extra_data, dict) else {}
        extra_data = dict(source)
        extra_data["read_at"] = datetime.now(timezone.utc).isoformat()
        notification.extra_data = extra_data

    def _all_managed_types(self) -> List[str]:
        """Get a de-duplicated list of managed notification types."""
        all_types = []
        for values in self.NOTIFICATION_TYPE_GROUPS.values():
            all_types.extend(values)
        return sorted(set(all_types))

    def _notification_type_category(self, notification_type: str) -> str:
        """Resolve a display category from the canonical notification type."""
        for category, values in self.NOTIFICATION_TYPE_GROUPS.items():
            if notification_type in values:
                return category
        return "general"

    def _get_notification_template_or_404(self, template_id: int) -> NotificationTemplate:
        """Get one notification template or raise not found."""
        template = NotificationTemplate.query.get(template_id)
        if not template:
            raise NotFoundError("Notification template not found")
        return template

    def _normalize_notification_type_filter(
        self,
        notification_type: Optional[str],
        *,
        allow_empty: bool = False,
    ) -> Optional[str]:
        """Normalize a notification type filter or payload field."""
        if notification_type in (None, ""):
            if allow_empty:
                return None
            raise ValidationError("notification_type is required")

        normalized = str(notification_type).strip().lower()
        try:
            return NotificationType(normalized).value
        except ValueError as exc:
            raise ValidationError("Invalid notification_type value") from exc

    def _normalize_admin_notification_template_payload(
        self,
        payload: Dict[str, Any],
        *,
        partial: bool = False,
    ) -> Dict[str, Any]:
        """Validate and normalize admin template payloads."""
        if not isinstance(payload, dict):
            raise ValidationError("Invalid template payload")

        normalized: Dict[str, Any] = {}

        if not partial or "name" in payload:
            name = str(payload.get("name") or "").strip()
            if not name:
                raise ValidationError("Template name is required")
            normalized["name"] = name

        if not partial or "notification_type" in payload:
            normalized["notification_type"] = self._normalize_notification_type_filter(payload.get("notification_type"))

        if not partial or "channel" in payload:
            normalized["channel"] = self._normalize_campaign_channel(payload.get("channel"))

        if not partial or "subject" in payload:
            normalized["subject"] = (payload.get("subject") or "").strip() or None

        if not partial or "content" in payload:
            content = (payload.get("content") or "").strip()
            if not content:
                raise ValidationError("Template content is required")
            normalized["content"] = content

        channel_value = normalized.get("channel")
        if channel_value == NotificationChannel.EMAIL.value and not normalized.get("subject"):
            raise ValidationError("subject is required for email templates")

        if "is_active" in payload or not partial:
            normalized["is_active"] = bool(payload.get("is_active", True))

        if "translations" in payload:
            translations = payload.get("translations") or {}
            if not isinstance(translations, dict):
                raise ValidationError("translations must be an object")
            normalized["translations"] = translations
        elif not partial:
            normalized["translations"] = {}

        return normalized

    def _serialize_admin_notification_template(
        self,
        template: NotificationTemplate,
        *,
        include_translations: bool = False,
    ) -> Dict[str, Any]:
        """Serialize a notification template for admin responses."""
        usage_count = NotificationCampaign.query.filter_by(template_id=template.id).count()
        payload = template.to_dict(language="en", include_all_translations=include_translations)
        payload.update(
            {
                "category": self._notification_type_category(template.notification_type),
                "usage_count": usage_count,
                "description": payload.get("description") or payload.get("subject") or "",
            }
        )
        if include_translations:
            translations: Dict[str, Dict[str, Any]] = {}
            for field_name in getattr(template, "_translatable_fields", []):
                field_translations = template.get_all_translations(field_name)
                for language, value in (field_translations or {}).items():
                    translations.setdefault(language, {})
                    translations[language][field_name] = value
            payload["translations"] = translations
        return payload

    def _normalize_notification_campaign_payload(
        self,
        payload: Dict[str, Any],
        *,
        require_schedule: bool = False,
    ) -> Dict[str, Any]:
        """Validate and normalize notification campaign payloads."""
        if not isinstance(payload, dict):
            raise ValidationError("Invalid campaign payload")

        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValidationError("Campaign name is required")

        notification_type = str(payload.get("notification_type") or "").strip()
        try:
            notification_type = NotificationType(notification_type).value
        except ValueError as exc:
            raise ValidationError("Invalid notification_type value") from exc

        priority = self._normalize_campaign_priority(payload.get("priority"))
        target_audience = self._normalize_campaign_audience(payload.get("target_audience"))
        channel = self._normalize_campaign_channel(payload.get("channel"))

        template_id = payload.get("template_id")
        if template_id is not None:
            try:
                template_id = int(template_id)
            except (TypeError, ValueError) as exc:
                raise ValidationError("template_id must be an integer") from exc
            template = NotificationTemplate.query.get(template_id)
            if not template or not template.is_active:
                raise NotFoundError("Template not found")
            if template.notification_type != notification_type:
                raise ValidationError("Template notification_type does not match campaign notification_type")
            if template.channel != channel:
                raise ValidationError("Template channel does not match campaign channel")

        subject_override = (payload.get("subject") or payload.get("subject_override") or "").strip() or None
        content_override = (payload.get("content") or payload.get("content_override") or "").strip() or None
        if template_id is None and not content_override:
            raise ValidationError("Message content is required when no template is selected")
        if channel == NotificationChannel.EMAIL.value and template_id is None and not subject_override:
            raise ValidationError("Subject is required for email campaigns without a template")

        target_segment_id = payload.get("target_segment_id")
        if target_segment_id is not None:
            try:
                target_segment_id = int(target_segment_id)
            except (TypeError, ValueError) as exc:
                raise ValidationError("target_segment_id must be an integer") from exc
            segment = UserSegment.query.get(target_segment_id)
            if not segment or not segment.is_active:
                raise NotFoundError("Target segment not found")

        specific_user_ids = payload.get("specific_user_ids")
        if specific_user_ids is None:
            specific_user_ids = []
        if not isinstance(specific_user_ids, list):
            raise ValidationError("specific_user_ids must be a list when provided")
        normalized_user_ids = []
        for user_id in specific_user_ids:
            try:
                normalized_user_ids.append(int(user_id))
            except (TypeError, ValueError) as exc:
                raise ValidationError("specific_user_ids must contain integers") from exc

        if target_audience == "custom_segment" and not target_segment_id and not normalized_user_ids:
            raise ValidationError("custom_segment campaigns require target_segment_id or specific_user_ids")
        if target_audience != "custom_segment":
            target_segment_id = None
            normalized_user_ids = []

        scheduled_at = payload.get("scheduled_at")
        scheduled_for = self._parse_campaign_datetime(scheduled_at) if scheduled_at else None
        if require_schedule and not scheduled_for:
            raise ValidationError("scheduled_at is required")

        return {
            "name": name,
            "template_id": template_id,
            "notification_type": notification_type,
            "channel": channel,
            "subject_override": subject_override,
            "content_override": content_override,
            "target_audience": target_audience,
            "target_segment_id": target_segment_id,
            "specific_user_ids": sorted(set(normalized_user_ids)),
            "priority": priority,
            "scheduled_at": scheduled_for,
        }

    def _normalize_campaign_channel(self, channel: Optional[str], allow_empty: bool = False) -> Optional[str]:
        """Normalize admin UI campaign channels to supported values."""
        if channel in (None, ""):
            if allow_empty:
                return None
            raise ValidationError("Channel is required")

        normalized = str(channel).strip().lower()
        # "sms" and its legacy alias "phone" are deliberately absent: SMS is
        # OTP-only, so a campaign can never be blasted over it.
        supported_channels = {
            NotificationChannel.EMAIL.value,
            NotificationChannel.TELEGRAM.value,
            NotificationChannel.IN_APP.value,
        }
        if normalized not in supported_channels:
            raise ValidationError("Invalid channel value")
        return normalized

    def _normalize_campaign_audience(
        self,
        target_audience: Optional[str],
        allow_empty: bool = False,
    ) -> Optional[str]:
        """Normalize supported campaign audiences."""
        if target_audience in (None, ""):
            if allow_empty:
                return None
            raise ValidationError("Target audience is required")

        normalized = str(target_audience).strip().lower()
        if normalized not in self.NOTIFICATION_CAMPAIGN_AUDIENCES:
            raise ValidationError("Invalid target audience")
        return normalized

    def _normalize_campaign_priority(self, priority: Optional[str]) -> str:
        """Normalize campaign priority."""
        priority_value = str(priority or Priority.NORMAL.value).strip().lower()
        priority_aliases = {"medium": Priority.NORMAL.value}
        normalized = priority_aliases.get(priority_value, priority_value)
        if normalized not in {Priority.LOW.value, Priority.NORMAL.value, Priority.HIGH.value, Priority.URGENT.value}:
            raise ValidationError("Invalid priority value")
        return normalized

    def _normalize_campaign_status(self, status: Optional[str], allow_empty: bool = False) -> Optional[str]:
        """Normalize supported campaign statuses."""
        if status in (None, ""):
            if allow_empty:
                return None
            raise ValidationError("Status is required")

        normalized = str(status).strip().lower()
        if normalized not in self.NOTIFICATION_CAMPAIGN_STATUSES:
            raise ValidationError("Invalid campaign status")
        return normalized

    def _parse_campaign_datetime(self, value: Any) -> datetime:
        """Parse optional campaign scheduling values."""
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValidationError("Invalid scheduled_at format") from exc
        raise ValidationError("Invalid scheduled_at format")

    def execute_notification_campaign(self, campaign_id: int) -> Dict[str, Any]:
        """Execute a queued notification campaign."""
        campaign = self._get_notification_campaign_or_404(campaign_id)
        if campaign.status == "cancelled":
            return {"campaign_id": campaign.id, "status": campaign.status, "skipped": True}

        if campaign.status not in {"scheduled", "sending"}:
            raise ConflictError("Campaign is not queued for execution")

        recipient_ids = list(campaign.recipient_ids_snapshot or [])
        if not recipient_ids:
            recipient_ids = self._resolve_notification_campaign_recipient_ids(campaign)
            campaign.recipient_ids_snapshot = recipient_ids
            campaign.recipient_count = len(recipient_ids)

        campaign.status = "sending"
        campaign.started_at = datetime.now(timezone.utc)
        campaign.last_error = None
        db.session.commit()

        successful_count = 0
        failed_count = 0
        errors: List[Dict[str, Any]] = []
        channel_enum = NotificationChannel(campaign.channel)

        for recipient_id in recipient_ids:
            db.session.refresh(campaign)
            if campaign.status == "cancelled":
                break

            user = User.query.get(recipient_id)
            if not user:
                failed_count += 1
                errors.append({"user_id": recipient_id, "error": "User not found"})
                continue

            template_data = self._build_notification_campaign_template_data(campaign, user)
            template_override = self._build_notification_campaign_template_override(
                campaign, getattr(user, "preferred_language", "en")
            )
            subject_preview, content_preview = self._render_campaign_preview_content(
                campaign,
                template_data,
                getattr(user, "preferred_language", "en"),
            )
            template_data["title"] = subject_preview or campaign.name
            template_data["message"] = content_preview or campaign.name

            result = self.send_notification(
                user_id=user.id,
                notification_type=NotificationType(campaign.notification_type),
                channels=[channel_enum],
                template_data=template_data,
                priority=campaign.priority,
                template_override=template_override,
                campaign_id=campaign.id,
            )
            channel_result = result.get(campaign.channel, {})
            if channel_result.get("success"):
                successful_count += 1
            else:
                failed_count += 1
                errors.append({"user_id": user.id, "error": channel_result.get("error", "Notification failed")})

        campaign.completed_at = datetime.now(timezone.utc)
        campaign.last_error = json.dumps(errors[:20]) if errors else None
        if campaign.status == "cancelled":
            campaign.completed_at = campaign.completed_at
        else:
            campaign.status = "failed" if successful_count == 0 else ("sent" if failed_count == 0 else "failed")
        db.session.commit()

        return {
            "campaign_id": campaign.id,
            "status": campaign.status,
            "successful_count": successful_count,
            "failed_count": failed_count,
            "errors": errors,
        }

    def _require_admin_user(self, user_id: Any) -> User:
        """Ensure the requester is an active admin."""
        normalized_user_id = self._coerce_user_id(user_id)
        user = User.query.get(normalized_user_id)
        if not user or not user.is_admin:
            raise ForbiddenError("Admin access required")
        return user

    def _get_notification_campaign_or_404(self, campaign_id: int) -> NotificationCampaign:
        """Get one notification campaign or raise not found."""
        campaign = NotificationCampaign.query.get(campaign_id)
        if not campaign:
            raise NotFoundError("Notification campaign not found")
        return campaign

    def _assert_campaign_editable(self, campaign: NotificationCampaign) -> None:
        """Ensure a campaign can still be edited."""
        if campaign.status not in {"draft", "scheduled"}:
            raise ConflictError("Only draft or scheduled campaigns can be edited")
        if campaign.status == "scheduled" and campaign.started_at:
            raise ConflictError("Campaign has already started sending")

    def _resolve_notification_campaign_recipient_ids(self, campaign: NotificationCampaign) -> List[int]:
        """Resolve campaign recipients and return a stable sorted list of ids."""
        if campaign.target_audience == "all_customers":
            rows = User.query.filter(User.role == UserRole.CUSTOMER).with_entities(User.id).all()
            return sorted({row.id for row in rows})

        if campaign.target_audience == "active_customers":
            rows = (
                db.session.query(User.id)
                .join(Order, Order.user_id == User.id)
                .filter(User.role == UserRole.CUSTOMER)
                .distinct()
                .all()
            )
            return sorted({row.id for row in rows})

        if campaign.target_audience == "new_customers":
            rows = (
                User.query.filter(
                    User.role == UserRole.CUSTOMER,
                    User.created_at >= datetime.now(timezone.utc) - timedelta(days=30),
                )
                .with_entities(User.id)
                .all()
            )
            return sorted({row.id for row in rows})

        if campaign.target_audience == "loyalty_members":
            rows = db.session.query(LoyaltyPoints.user_id).distinct().all()
            return sorted({row.user_id for row in rows})

        if campaign.target_audience == "custom_segment":
            explicit_ids = {int(user_id) for user_id in (campaign.specific_user_ids or [])}
            if explicit_ids:
                return sorted(explicit_ids)
            if campaign.target_segment_id:
                segment = UserSegment.query.get(campaign.target_segment_id)
                if not segment or not segment.is_active:
                    raise NotFoundError("Target segment not found")
                return self._resolve_user_segment_recipient_ids(segment)

        raise ValidationError("Unable to resolve campaign recipients")

    def _resolve_user_segment_recipient_ids(self, segment: UserSegment) -> List[int]:
        """Resolve ids for a user segment criteria subset."""
        criteria = segment.criteria if isinstance(segment.criteria, dict) else {}
        remaining_keys = set(criteria.keys())
        query = User.query.filter(User.role == UserRole.CUSTOMER)

        if "user_ids" in criteria:
            user_ids = criteria.get("user_ids") or []
            return sorted({int(user_id) for user_id in user_ids})

        simple_filters = {
            "status": User.status,
            "preferred_language": User.preferred_language,
            "registration_source": User.registration_source,
            "registration_method": User.registration_method,
        }
        for key, column in simple_filters.items():
            if key in criteria:
                query = query.filter(column == criteria[key])
                remaining_keys.discard(key)

        boolean_filters = {
            "is_verified": User.is_verified,
            "is_premium": User.is_premium,
        }
        for key, column in boolean_filters.items():
            if key in criteria:
                query = query.filter(column.is_(bool(criteria[key])))
                remaining_keys.discard(key)

        if "created_after" in criteria:
            query = query.filter(User.created_at >= self._parse_campaign_datetime(criteria["created_after"]))
            remaining_keys.discard("created_after")
        if "created_before" in criteria:
            query = query.filter(User.created_at <= self._parse_campaign_datetime(criteria["created_before"]))
            remaining_keys.discard("created_before")

        if "has_orders" in criteria or "min_order_count" in criteria or "max_order_count" in criteria:
            order_count_subquery = (
                db.session.query(
                    Order.user_id.label("user_id"),
                    func.count(Order.id).label("order_count"),
                )
                .group_by(Order.user_id)
                .subquery()
            )
            query = query.outerjoin(order_count_subquery, order_count_subquery.c.user_id == User.id)
            if "has_orders" in criteria:
                if criteria["has_orders"]:
                    query = query.filter(func.coalesce(order_count_subquery.c.order_count, 0) > 0)
                else:
                    query = query.filter(func.coalesce(order_count_subquery.c.order_count, 0) == 0)
                remaining_keys.discard("has_orders")
            if "min_order_count" in criteria:
                query = query.filter(
                    func.coalesce(order_count_subquery.c.order_count, 0) >= int(criteria["min_order_count"])
                )
                remaining_keys.discard("min_order_count")
            if "max_order_count" in criteria:
                query = query.filter(
                    func.coalesce(order_count_subquery.c.order_count, 0) <= int(criteria["max_order_count"])
                )
                remaining_keys.discard("max_order_count")

        unsupported_keys = remaining_keys - {"user_ids"}
        if unsupported_keys:
            raise ValidationError(
                f'Unsupported segment criteria for notification campaigns: {", ".join(sorted(unsupported_keys))}'
            )

        rows = query.with_entities(User.id).distinct().all()
        return sorted({row.id for row in rows})

    def _build_notification_campaign_template_data(
        self,
        campaign: NotificationCampaign,
        user: User,
    ) -> Dict[str, Any]:
        """Build campaign template data for one recipient."""
        return {
            "campaign_name": campaign.name,
            "user_id": user.id,
            "user_name": user.full_name or user.email or user.phone or f"User {user.id}",
            "user_email": user.email,
            "user_phone": user.phone,
            "company_name": self.company_name,
            "company_phone": self.company_phone,
            "company_email": self.company_email,
        }

    def _build_notification_campaign_template_override(
        self,
        campaign: NotificationCampaign,
        language: str,
    ):
        """Build a template-like object honoring campaign overrides."""
        template = campaign.template
        subject = campaign.subject_override
        content = campaign.content_override

        if template and not subject:
            subject = (
                template.get_translated("subject", language)
                if hasattr(template, "get_translated")
                else template.subject
            )
        if template and not content:
            content = (
                template.get_translated("content", language)
                if hasattr(template, "get_translated")
                else template.content
            )

        if not content:
            content = ""

        return SimpleNamespace(
            name=campaign.name,
            notification_type=campaign.notification_type,
            channel=campaign.channel,
            subject=subject,
            content=content,
            get_translated=lambda field_name, _language: subject if field_name == "subject" else content,
        )

    def _render_campaign_preview_content(
        self,
        campaign: NotificationCampaign,
        template_data: Dict[str, Any],
        language: str,
    ) -> tuple:
        """Render campaign subject/content previews."""
        template_override = self._build_notification_campaign_template_override(campaign, language)
        subject = template_override.get_translated("subject", language)
        content = template_override.get_translated("content", language)
        return (
            self._render_template(subject or "", template_data, language),
            self._render_template(content or "", template_data, language),
        )

    def _serialize_notification_campaign(self, campaign: NotificationCampaign) -> Dict[str, Any]:
        """Serialize campaign summary for admin list views."""
        sent_count = self._count_campaign_notifications(
            campaign.id,
            statuses={NotificationStatus.SENT.value, NotificationStatus.DELIVERED.value, NotificationStatus.READ.value},
        )
        failed_count = self._count_campaign_notifications(
            campaign.id,
            statuses={NotificationStatus.FAILED.value},
        )
        return {
            "id": campaign.id,
            "name": campaign.name,
            "template_id": campaign.template_id,
            "notification_type": campaign.notification_type,
            "category": self._notification_type_category(campaign.notification_type),
            "channel": campaign.channel,
            "subject": campaign.subject_override or (campaign.template.subject if campaign.template else ""),
            "content": campaign.content_override or (campaign.template.content if campaign.template else ""),
            "status": campaign.status,
            "priority": campaign.priority,
            "target_audience": campaign.target_audience,
            "target_segment_id": campaign.target_segment_id,
            "recipient_count": campaign.recipient_count or len(campaign.recipient_ids_snapshot or []),
            "sent_count": sent_count,
            "failed_count": failed_count,
            "scheduled_at": campaign.scheduled_at.isoformat() if campaign.scheduled_at else None,
            "queued_at": campaign.queued_at.isoformat() if campaign.queued_at else None,
            "started_at": campaign.started_at.isoformat() if campaign.started_at else None,
            "completed_at": campaign.completed_at.isoformat() if campaign.completed_at else None,
            "cancelled_at": campaign.cancelled_at.isoformat() if campaign.cancelled_at else None,
            "created_at": campaign.created_at.isoformat() if campaign.created_at else None,
            "updated_at": campaign.updated_at.isoformat() if campaign.updated_at else None,
        }

    def _serialize_notification_campaign_detail(self, campaign: NotificationCampaign) -> Dict[str, Any]:
        """Serialize campaign detail including delivery summary and recent activity."""
        payload = self._serialize_notification_campaign(campaign)
        payload.update(
            {
                "specific_user_ids": list(campaign.specific_user_ids or []),
                "recipient_ids_snapshot": list(campaign.recipient_ids_snapshot or []),
                "target_segment": (
                    {
                        "id": campaign.target_segment.id,
                        "name": campaign.target_segment.name,
                        "description": campaign.target_segment.description,
                        "user_count": campaign.target_segment.user_count,
                    }
                    if campaign.target_segment
                    else None
                ),
                "template": (
                    self._serialize_admin_notification_template(campaign.template) if campaign.template else None
                ),
                "created_by_user_id": campaign.created_by_user_id,
                "updated_by_user_id": campaign.updated_by_user_id,
                "celery_task_id": campaign.celery_task_id,
                "last_error": campaign.last_error,
                "summary": self._get_campaign_delivery_summary(campaign.id),
                "recent_notifications": self._get_recent_campaign_notifications(campaign.id),
            }
        )
        return payload

    def _count_campaign_notifications(self, campaign_id: int, statuses: set) -> int:
        """Count campaign notifications in a status set."""
        return (
            Notification.query.filter(Notification.campaign_id == campaign_id)
            .filter(Notification.delivery_status.in_(list(statuses)))
            .count()
        )

    def _get_campaign_delivery_summary(self, campaign_id: int) -> Dict[str, Any]:
        """Return delivery summary for one campaign."""
        total = Notification.query.filter(Notification.campaign_id == campaign_id).count()
        sent = self._count_campaign_notifications(
            campaign_id,
            {NotificationStatus.SENT.value, NotificationStatus.DELIVERED.value, NotificationStatus.READ.value},
        )
        delivered = self._count_campaign_notifications(
            campaign_id, {NotificationStatus.DELIVERED.value, NotificationStatus.READ.value}
        )
        failed = self._count_campaign_notifications(campaign_id, {NotificationStatus.FAILED.value})
        pending = self._count_campaign_notifications(campaign_id, {NotificationStatus.PENDING.value})
        return {
            "total": total,
            "sent": sent,
            "delivered": delivered,
            "failed": failed,
            "pending": pending,
            "delivery_rate": round((delivered / total * 100), 2) if total > 0 else 0,
        }

    def _get_recent_campaign_notifications(self, campaign_id: int) -> List[Dict[str, Any]]:
        """Return recent notification rows for a campaign."""
        notifications = (
            Notification.query.filter(Notification.campaign_id == campaign_id)
            .order_by(Notification.created_at.desc())
            .limit(20)
            .all()
        )
        items = []
        for notification in notifications:
            user = notification.user
            items.append(
                {
                    "id": notification.id,
                    "user_id": notification.user_id,
                    "user_name": user.full_name if user else "",
                    "user_email": user.email if user else None,
                    "user_phone": user.phone if user else None,
                    "channel": (
                        notification.channel.value if hasattr(notification.channel, "value") else notification.channel
                    ),
                    "status": (
                        notification.delivery_status.value
                        if hasattr(notification.delivery_status, "value")
                        else notification.delivery_status
                    ),
                    "title": notification.title,
                    "message": notification.message,
                    "failure_reason": notification.failure_reason,
                    "created_at": notification.created_at.isoformat() if notification.created_at else None,
                    "sent_at": notification.sent_at.isoformat() if notification.sent_at else None,
                }
            )
        return items

    def _map_preferences(self, rows: List[NotificationPreference]) -> Dict[str, set]:
        """Map preference rows to type->set(channel_value)."""
        mapped: Dict[str, set] = {}
        for row in rows:
            type_key = row.notification_type
            if type_key not in mapped:
                mapped[type_key] = set()
            channel_value = row.channel.value if hasattr(row.channel, "value") else str(row.channel)
            mapped[type_key].add(channel_value)
        return mapped

    def _ensure_preference_row(self, user_id: int, notification_type: str, channel: NotificationChannel) -> None:
        """Ensure one enabled preference row exists for user/type/channel."""
        existing = NotificationPreference.query.filter_by(
            user_id=user_id,
            notification_type=notification_type,
            channel=channel,
        ).first()
        if existing:
            existing.is_enabled = True
            return

        db.session.add(
            NotificationPreference(
                user_id=user_id,
                notification_type=notification_type,
                channel=channel,
                is_enabled=True,
            )
        )

    def _set_delivery_telegram_status_updates_row(self, user_id: int, enabled: bool) -> None:
        """Upsert explicit Telegram delivery-status updates preference row."""
        normalized_user_id = self._coerce_user_id(user_id)

        existing_rows = (
            NotificationPreference.query.filter_by(
                user_id=normalized_user_id,
                notification_type=self.DELIVERY_TELEGRAM_STATUS_UPDATES_PREF_KEY,
                channel=NotificationChannel.TELEGRAM,
            )
            .order_by(NotificationPreference.id.asc())
            .all()
        )

        if existing_rows:
            primary_row = existing_rows[0]
            primary_row.is_enabled = enabled

            for duplicate_row in existing_rows[1:]:
                db.session.delete(duplicate_row)
            return

        db.session.add(
            NotificationPreference(
                user_id=normalized_user_id,
                notification_type=self.DELIVERY_TELEGRAM_STATUS_UPDATES_PREF_KEY,
                channel=NotificationChannel.TELEGRAM,
                is_enabled=enabled,
            )
        )

    @staticmethod
    def _coerce_user_id(user_id: Any) -> int:
        """Normalize user id inputs coming from JWT identity values."""
        try:
            return int(user_id)
        except (TypeError, ValueError):
            raise ValidationError("Invalid user id")

    def _enabled_channels_from_payload(self, payload: Dict[str, Any]) -> List[NotificationChannel]:
        """Extract globally enabled channels from update payload."""
        mapping = {
            "email_enabled": NotificationChannel.EMAIL,
            "sms_enabled": NotificationChannel.SMS,
            "push_enabled": NotificationChannel.PUSH,
            "in_app_enabled": NotificationChannel.IN_APP,
            "telegram_enabled": NotificationChannel.TELEGRAM,
        }
        result = []
        for key, channel in mapping.items():
            if payload.get(key) is True:
                result.append(channel)
        return result

    def _default_channels_for_type(self, notification_type: str) -> List[NotificationChannel]:
        """Default channels for a notification type."""
        defaults = {
            NotificationType.ORDER_CONFIRMATION.value: [NotificationChannel.EMAIL],
            NotificationType.ORDER_STATUS_UPDATE.value: [NotificationChannel.TELEGRAM],
            NotificationType.ORDER_UPDATE.value: [NotificationChannel.TELEGRAM],
            NotificationType.ORDER_EDITED.value: [NotificationChannel.TELEGRAM],
            NotificationType.DELIVERY_UPDATE.value: [NotificationChannel.TELEGRAM],
            NotificationType.DELIVERY_REMINDER.value: [NotificationChannel.TELEGRAM],
            NotificationType.PAYMENT_CONFIRMATION.value: [NotificationChannel.EMAIL],
            NotificationType.SUBSCRIPTION_REMINDER.value: [NotificationChannel.EMAIL],
            NotificationType.SUBSCRIPTION_CREATED.value: [NotificationChannel.EMAIL, NotificationChannel.TELEGRAM],
            NotificationType.SUBSCRIPTION_RENEWAL.value: [NotificationChannel.EMAIL],
            NotificationType.SUBSCRIPTION_CANCELLED.value: [NotificationChannel.EMAIL],
            NotificationType.SUBSCRIPTION_CANCELLATION_SCHEDULED.value: [NotificationChannel.EMAIL],
            NotificationType.PROMOTIONAL.value: [NotificationChannel.EMAIL],
            NotificationType.SYSTEM.value: [NotificationChannel.EMAIL],
            NotificationType.SYSTEM_ALERT.value: [NotificationChannel.EMAIL],
            NotificationType.SECURITY.value: [NotificationChannel.EMAIL],
            NotificationType.EMAIL_VERIFICATION.value: [NotificationChannel.EMAIL],
            NotificationType.PASSWORD_RESET.value: [NotificationChannel.EMAIL],
            NotificationType.LOYALTY_REWARD.value: [NotificationChannel.EMAIL, NotificationChannel.TELEGRAM],
            NotificationType.REWARD_REDEEMED.value: [NotificationChannel.EMAIL],
            NotificationType.LOYALTY_TIER_UPGRADE.value: [NotificationChannel.EMAIL, NotificationChannel.TELEGRAM],
            NotificationType.LOYALTY_POINTS_EXPIRED.value: [
                NotificationChannel.EMAIL,
                NotificationChannel.TELEGRAM,
            ],
        }
        return defaults.get(notification_type, [NotificationChannel.EMAIL])

    @staticmethod
    def _status_value(status: Any) -> str:
        """Normalize enum-or-string status values to plain strings."""
        return status.value if hasattr(status, "value") else str(status)

    _LOYALTY_REASON_LABELS = {
        "welcome_bonus": {
            "uz": "Xush kelibsiz bonusi",
            "ru": "Приветственный бонус",
            "en": "Welcome bonus",
        },
        "birthday_bonus": {
            "uz": "Tug'ilgan kun bonusi",
            "ru": "Бонус на день рождения",
            "en": "Birthday bonus",
        },
        "purchase": {
            "uz": "Xarid",
            "ru": "Покупка",
            "en": "Purchase",
        },
        "referral": {
            "uz": "Referal",
            "ru": "Реферал",
            "en": "Referral",
        },
        "streak_bonus": {
            "uz": "Streak bonusi",
            "ru": "Бонус за серию",
            "en": "Streak bonus",
        },
        "consecutive_streak_bonus": {
            "uz": "Ketma-ket streak bonusi",
            "ru": "Бонус за последовательную серию",
            "en": "Consecutive streak bonus",
        },
        "surprise_reward": {
            "uz": "Kutilmagan mukofot",
            "ru": "Сюрприз-награда",
            "en": "Surprise reward",
        },
    }

    _LOYALTY_REASON_FALLBACK = {
        "uz": "AquaCoins mukofoti",
        "ru": "Награда AquaCoins",
        "en": "AquaCoins reward",
    }

    @staticmethod
    def _loyalty_reason_label(reason: str, language: str) -> str:
        """Localized human label for an AquaCoins accrual reason.

        Covers every current LoyaltyActionType.value; unknown / future
        reasons fall back to a generic 'AquaCoins reward' so a new bonus
        type never produces a blank or broken message.
        """
        lang = language if language in ("uz", "ru", "en") else "uz"
        labels = NotificationService._LOYALTY_REASON_LABELS.get(reason or "")
        if labels:
            return labels.get(lang) or labels.get("uz")
        fallback = NotificationService._LOYALTY_REASON_FALLBACK
        return fallback.get(lang) or fallback["uz"]

    @staticmethod
    def _loyalty_tier_label(tier_config_id: Optional[int], tier_name: str, language: str) -> str:
        """Localized tier name.

        LoyaltyTierConfig is @translatable("name") — it is the single source of
        truth for what a tier is called in each language, so read it rather
        than keeping a second copy of Bronze/Silver/Gold/Platinum here. An
        admin-created tier with no translations (or a config we can no longer
        resolve) falls back to the raw stored name, which is still readable.
        """
        if tier_config_id:
            from business_app.models.loyalty import LoyaltyTierConfig

            config = LoyaltyTierConfig.query.get(tier_config_id)
            if config:
                translated = config.get_translated("name", language)
                if translated:
                    return translated
                return config.name
        return tier_name

    @staticmethod
    def _unrendered_placeholders(content: str) -> List[str]:
        """Placeholder names still literal in a rendered message, in order."""
        if not content:
            return []
        return _PLACEHOLDER_RE.findall(content)

    def _guard_unrendered_placeholders(
        self, notification_type, channel: str, content: str, user_id: Optional[int]
    ) -> Optional[Dict[str, Any]]:
        """Backstop: never show a customer a raw ``{placeholder}``.

        Returns a skip-result for courtesy notification types, or None to let
        the send proceed. Always logs, so a leak in any type is visible.
        """
        leaked = self._unrendered_placeholders(content)
        if not leaked:
            return None

        notification_type_value = self._status_value(notification_type)
        strict = notification_type_value in _PLACEHOLDER_STRICT_TYPES
        logger.error(
            "Unrendered template placeholders: user_id=%s notification_type=%s channel=%s " "placeholders=%s action=%s",
            user_id,
            notification_type_value,
            channel,
            leaked,
            "skipped" if strict else "sent_anyway",
        )
        if not strict:
            return None
        return {
            "success": False,
            "skipped": True,
            "reason": "unrendered_placeholders",
            "placeholders": leaked,
        }

    @staticmethod
    def _user_has_connected_telegram(user: User) -> bool:
        """Return True when the customer has an active linked Telegram bot."""
        return bool(getattr(user, "telegram_id", None) and getattr(user, "is_bot_active", False))

    def _resolve_loyalty_award_channels(self, user: User) -> List[NotificationChannel]:
        """Channels for an AquaCoins award notification.

        Business rule (local to this notification type, does NOT change the
        global LOYALTY_REWARD default): Telegram when the customer has a
        connected bot, otherwise email. Never both, never SMS.
        """
        if self._user_has_connected_telegram(user):
            return [NotificationChannel.TELEGRAM]
        return [NotificationChannel.EMAIL]

    def _should_force_delivery_status_telegram(self, status_value: str) -> bool:
        """Statuses that must include Telegram for connected users."""
        return status_value in {
            DeliveryStatus.IN_TRANSIT.value,
            DeliveryStatus.ARRIVED.value,
        }

    def _resolve_delivery_status_channels(self, user: User, status_value: str) -> List[NotificationChannel]:
        """Resolve channels for a customer delivery-status event.

        Business rule: delivery updates never use SMS. Only customer-facing
        milestones (in_transit / arrived) notify the customer — via Telegram
        when a bot is connected, otherwise via email, otherwise not at all.
        """
        if not self._should_force_delivery_status_telegram(status_value):
            # delivered / pending / failed / etc. — no customer notification.
            logger.info(
                "No customer notification for non-milestone delivery status: user_id=%s status=%s",
                user.id,
                status_value,
            )
            return []

        telegram_enabled = self.get_delivery_telegram_status_updates_setting(user.id)[
            "delivery_telegram_status_updates_enabled"
        ]
        if telegram_enabled and self._user_has_connected_telegram(user):
            return [NotificationChannel.TELEGRAM]

        if getattr(user, "email", None):
            logger.info(
                "Delivery status update routed to email (no connected bot): user_id=%s status=%s",
                user.id,
                status_value,
            )
            return [NotificationChannel.EMAIL]

        logger.info(
            "Delivery status update has no deliverable channel: user_id=%s status=%s",
            user.id,
            status_value,
        )
        return []

    def _extract_delivery_status_code(self, template_data: Dict[str, Any]) -> Optional[str]:
        """Extract normalized delivery status from template payload."""
        if not template_data:
            return None

        for field in ("delivery_status_code", "event_type", "delivery_status", "order_status"):
            raw_value = template_data.get(field)
            normalized_value = self._normalize_delivery_status_code(raw_value)
            if normalized_value:
                return normalized_value

        return None

    def _normalize_delivery_status_code(self, raw_value: Any) -> Optional[str]:
        """Normalize raw status payload into delivery-style status code."""
        if raw_value is None:
            return None

        normalized_value = self._status_value(raw_value).strip().lower()
        if not normalized_value:
            return None

        normalized_value = normalized_value.replace("-", "_").replace(" ", "_")
        if normalized_value == "intransit":
            normalized_value = DeliveryStatus.IN_TRANSIT.value

        return normalized_value

    @staticmethod
    def _normalize_language_code(language: Optional[str]) -> str:
        """Normalize locale-like language values (e.g. uz-UZ, uz_UZ) to base code."""
        raw_language = str(language or "en").strip().lower().replace("_", "-")
        if not raw_language:
            return "en"
        return raw_language.split("-", 1)[0]

    def _get_translation_exact_language(self, key: str, language: str) -> Optional[str]:
        """Fetch translation for an exact language only (no cross-language fallback)."""
        translation = Translation.query.filter_by(
            key=key,
            language=language,
            is_active=True,
        ).first()
        if not translation or not translation.value:
            return None
        return translation.value

    def _get_localized_delivery_status_label(self, status_value: str, language: str) -> str:
        """Resolve a customer-facing localized label for delivery status notifications."""
        normalized_language = self._normalize_language_code(language)
        normalized_status = self._normalize_delivery_status_code(status_value) or self._status_value(status_value)
        translation_key = f"notification.delivery_status.{normalized_status}"

        exact_label = self._get_translation_exact_language(translation_key, normalized_language)
        if exact_label:
            return exact_label

        fallback_key = f"api.delivery.{normalized_status}"
        fallback_exact_label = self._get_translation_exact_language(fallback_key, normalized_language)
        if fallback_exact_label:
            return fallback_exact_label

        bundled_label = self.DELIVERY_STATUS_LABEL_FALLBACKS.get(normalized_language, {}).get(normalized_status)
        if bundled_label:
            return bundled_label

        label = get_translation(translation_key, normalized_language)
        if label and label != translation_key:
            return label

        fallback_label = get_translation(fallback_key, normalized_language)
        if fallback_label and fallback_label != fallback_key:
            return fallback_label

        return normalized_status.replace("_", " ").title()

    def _build_delivery_status_template_data(
        self,
        *,
        delivery: Delivery,
        history: DeliveryStatusHistory,
        language: str,
    ) -> Dict[str, Any]:
        """Build notification template data from the immutable delivery status event."""
        status_value = self._status_value(history.new_status)

        return {
            "tracking_code": delivery.tracking_number,
            "order_number": delivery.order.order_number if delivery.order else "",
            "delivery_status": self._get_localized_delivery_status_label(status_value, language),
            "delivery_status_code": status_value,
            "estimated_delivery": (
                delivery.estimated_delivery_time.isoformat() if delivery.estimated_delivery_time else None
            ),
            "event_type": status_value,
            "delivery_id": delivery.id,
            "order_id": delivery.order.id if delivery.order else None,
            "history_id": history.id,
            # Company contact details so the email/Telegram body renders real
            # values instead of leaving `{company_phone}` placeholders verbatim.
            "company_name": self.company_name,
            "company_phone": self.company_phone,
            "company_email": self.company_email,
        }

    def _is_payment_order_delivered(self, payment: Payment) -> bool:
        """Return True when payment is attached to an order already marked as delivered."""
        order = getattr(payment, "order", None)
        if not order:
            return False

        order_status = self._normalize_delivery_status_code(getattr(order, "status", None))
        if order_status == DeliveryStatus.DELIVERED.value:
            return True

        delivery = getattr(order, "delivery", None)
        delivery_status = self._normalize_delivery_status_code(getattr(delivery, "status", None)) if delivery else None
        return delivery_status == DeliveryStatus.DELIVERED.value

    def _get_payment_follow_up_message(self, payment: Payment, language: Optional[str]) -> str:
        """Resolve localized payment follow-up copy based on fulfillment stage."""
        normalized_language = self._normalize_language_code(language)
        message_key = "delivered" if self._is_payment_order_delivered(payment) else "processing"
        language_messages = self.PAYMENT_FOLLOW_UP_MESSAGES.get(
            normalized_language,
            self.PAYMENT_FOLLOW_UP_MESSAGES["en"],
        )
        return language_messages.get(message_key, self.PAYMENT_FOLLOW_UP_MESSAGES["en"][message_key])

    # Private methods for different channels
    def _send_email_notification(
        self,
        user: User,
        notification_type: NotificationType,
        template_data: Dict[str, Any],
        language: str,
        template_override=None,
    ) -> Dict[str, Any]:
        """Send email notification using Brevo API with file-based templates"""
        if not self.brevo_api_key:
            raise ConfigurationError(get_translation("error.configuration.email_not_configured"))

        if not user.email:
            return {"success": False, "error": get_translation("error.validation.no_email_address")}

        # Get notification type string
        notification_type_str = (
            notification_type.value if hasattr(notification_type, "value") else str(notification_type)
        )

        # Add user info to template data
        user_name = f"{user.first_name} {user.last_name}".strip() or user.email
        template_data_with_user = {"user_name": user_name, "user_email": user.email, **template_data}

        # Try file-based templates first
        email_template_service = get_email_template_service()
        rendered = email_template_service.render_notification_email(
            notification_type_str, language, template_data_with_user
        )

        if rendered:
            subject = rendered["subject"]
            content = rendered["content"]
            logger.info(f"Using file-based template for {notification_type_str} in {language}")
        else:
            # Fallback to database templates
            logger.info(f"File template not found, falling back to DB for {notification_type_str}")
            template = template_override or self._get_notification_template(
                notification_type, NotificationChannel.EMAIL, language
            )

            if not template:
                return {"success": False, "error": get_translation("error.template_not_found")}

            # Get translated content (or fallback to default)
            template_subject = (
                template.get_translated("subject", language)
                if hasattr(template, "get_translated")
                else template.subject
            )
            template_content = (
                template.get_translated("content", language)
                if hasattr(template, "get_translated")
                else template.content
            )

            # Render template
            subject = self._render_template(template_subject, template_data_with_user, language)
            content = self._render_template(template_content, template_data_with_user, language)

        skip = self._guard_unrendered_placeholders(
            notification_type,
            NotificationChannel.EMAIL.value,
            f"{subject or ''}\n{content or ''}",
            getattr(user, "id", None),
        )
        if skip:
            return skip

        # Build Brevo API request
        url = "https://api.brevo.com/v3/smtp/email"
        headers = {"accept": "application/json", "api-key": self.brevo_api_key, "content-type": "application/json"}
        payload = {
            "sender": {"name": self.default_sender_name, "email": self.default_sender_email},
            "to": [{"email": user.email, "name": user_name}],
            "subject": subject,
            "htmlContent": content,
        }

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            response.raise_for_status()

            result = response.json()
            return {"success": True, "message_id": result.get("messageId"), "status_code": response.status_code}
        except requests.exceptions.HTTPError as e:
            error_detail = ""
            try:
                error_detail = e.response.json() if e.response else str(e)
            except:  # noqa: E722
                error_detail = str(e)
            logger.error(f"Brevo API error: {error_detail}")
            return {"success": False, "error": f"Email API error: {error_detail}"}
        except Exception as e:
            logger.exception("Email sending failed")
            return {"success": False, "error": str(e)}

    def send_sms_to_phone(
        self,
        phone: str,
        notification_type: NotificationType,
        template_key: str,
        template_data: Dict[str, Any],
        language: str = "uz",
    ) -> Dict[str, Any]:
        """
        Send an OTP SMS to a phone number, without requiring a User object.

        This is the ONLY function in the codebase that reaches the SMS
        provider, and it only sends one-time passcodes — see OTP_SMS_TEMPLATES
        for why the sendable set is a closed allowlist.

        Args:
            phone: Phone number in normalized format (+998XXXXXXXXX)
            notification_type: Type of notification, for logging/audit only
            template_key: A key in OTP_SMS_TEMPLATES (e.g. 'sms.verification.otp');
                anything else is refused without contacting the provider
            template_data: Data to render in template
            language: Language code for template

        Returns:
            Dict with success status and message_id or error
        """
        logger.info(f"send_sms_to_phone started: phone={phone[:4]}***{phone[-4:]}, template_key={template_key}")

        # SSOT gate: SMS carries one-time passcodes and nothing else. This is
        # the only function that reaches the provider, so this check is the
        # single chokepoint that guarantees it — no caller can bypass it.
        if template_key not in OTP_SMS_TEMPLATE_KEYS:
            logger.warning(
                "SMS blocked: %s is not an OTP template and SMS is OTP-only. Use Telegram or email.",
                template_key,
            )
            return {"success": False, "skipped": True, "reason": "sms_is_otp_only"}

        if not self.eskiz_client:
            logger.error("send_sms_to_phone error: Eskiz SMS not configured")
            return {"success": False, "error": "SMS service not configured"}

        if not phone:
            logger.error("send_sms_to_phone error: No phone number provided")
            return {"success": False, "error": "No phone number provided"}

        # Texts come from the moderated allowlist above, never from the DB.
        # A DB-backed text would let an admin edit their way into an
        # unmoderated string, which the provider rejects at send time.
        texts = OTP_SMS_TEMPLATES[template_key]
        content = texts.get(language) or texts.get("en")

        if not content:
            logger.error(f"send_sms_to_phone error: no moderated text for key {template_key}")
            return {"success": False, "error": f"SMS template not found: {template_key}"}

        # Render template with data
        try:
            rendered_content = self._render_template(content, template_data, language)
        except Exception:
            logger.exception("Template rendering failed")
            rendered_content = content  # Use unrendered template as fallback

        logger.info(f"send_sms_to_phone rendered content: {rendered_content[:50]}...")

        try:
            # Clean phone number (Eskiz expects format like 998901234567)
            clean_phone = phone.replace("+", "").replace(" ", "").replace("-", "")

            # Send SMS via Eskiz
            response = self.eskiz_client.send_sms(
                mobile_phone=clean_phone, message=rendered_content, from_whom=self.eskiz_from
            )

            # Check if SMS was sent successfully
            if response and hasattr(response, "status"):
                # Eskiz "waiting"/"pending" mean accepted and queued by the
                # provider — only genuine error statuses are failures.
                if response.status in ("success", "waiting", "pending"):
                    logger.info(
                        f"SMS accepted by provider (status={response.status}) for {clean_phone[:3]}***{clean_phone[-4:]}. Message ID: {getattr(response, 'id', 'N/A')}"  # noqa: E501
                    )
                    return {
                        "success": True,
                        "message_id": getattr(response, "id", None),
                        "phone": clean_phone,
                        "provider_status": response.status,
                    }
                else:
                    error_msg = getattr(response, "message", "Unknown error from SMS provider")
                    logger.error(f"Eskiz SMS failed: status={response.status}, message={error_msg}")
                    return {
                        "success": False,
                        "error": f"SMS provider returned status: {response.status}",
                        "details": error_msg,
                    }
            else:
                logger.warning(f"Eskiz SMS returned unexpected response format: {response}")
                return {"success": False, "error": "Unexpected response from SMS provider"}

        except Exception as e:
            logger.exception("Eskiz SMS error")
            return {"success": False, "error": str(e)}

    def send_staff_telegram_message(
        self, user: User, message: str, *, language: Optional[str] = None
    ) -> Dict[str, Any]:
        """Send a one-off Telegram message via staff bot token."""
        if not message:
            return {"success": False, "error": "Message content is required"}

        template = SimpleNamespace(
            subject="Staff bot message",
            content=message,
            get_translated=lambda field_name, _language: ("Staff bot message" if field_name == "subject" else message),
        )

        try:
            return self._send_telegram_notification(
                user=user,
                notification_type=NotificationType.SYSTEM_ALERT,
                template_data={},
                language=language or getattr(user, "preferred_language", "en") or "en",
                template_override=template,
                bot_token=self.staff_telegram_bot_token,
            )
        except ConfigurationError as exc:
            logger.warning(
                "Staff Telegram notification skipped: user_id=%s reason=%s",
                getattr(user, "id", None),
                exc,
            )
            return {"success": False, "error": str(exc)}

    def send_user_telegram_message(self, user: User, message: str, *, language: Optional[str] = None) -> Dict[str, Any]:
        """Send a one-off free-text Telegram message to a customer via the customer bot token."""
        if not message:
            return {"success": False, "error": "Message content is required"}

        safe = html.escape(message)
        template = SimpleNamespace(
            subject="Support message",
            content=safe,
            get_translated=lambda field_name, _language: ("Support message" if field_name == "subject" else safe),
        )

        try:
            return self._send_telegram_notification(
                user=user,
                notification_type=NotificationType.SYSTEM_ALERT,
                template_data={},
                language=language or getattr(user, "preferred_language", "en") or "en",
                template_override=template,
                bot_token=self.telegram_bot_token,
            )
        except ConfigurationError as exc:
            logger.warning(
                "Customer Telegram message skipped: user_id=%s reason=%s",
                getattr(user, "id", None),
                exc,
            )
            return {"success": False, "error": str(exc)}

    # Telegram refuses sendPhoto over 10 MB. base.py's default MAX_CONTENT_LENGTH
    # is 16 MB, so on dev/staging an image in between passes our gate and would
    # fail at Telegram — send those as documents instead. Spec §5.
    #
    # Production hardcodes MAX_CONTENT_LENGTH to 10 MB (config/production.py),
    # matching this exact constant, so an image ever reaching this branch above
    # 10 MB is currently impossible in prod — Werkzeug's body-size gate 413s it
    # first. The branch stays correct dead code for prod as configured today,
    # and becomes live the moment that cap is ever raised; don't assume it runs.
    TELEGRAM_MAX_PHOTO_BYTES = 10 * 1024 * 1024

    def send_user_telegram_media(
        self, user: User, file_bytes: bytes, filename: str, mime_type: str, caption: str = None
    ) -> Dict[str, Any]:
        """Stream an admin upload straight to Telegram; persist nothing locally.

        Returns the file_id Telegram assigns so the same bytes can be read back
        through the support attachment proxy.
        """
        if not self.telegram_bot_token:
            return {"success": False, "error": "Telegram bot token is not configured"}
        telegram_chat_id = getattr(user, "telegram_id", None)
        if not telegram_chat_id:
            return {"success": False, "error": get_translation("error.validation.no_telegram_id")}

        mime_type = mime_type or "application/octet-stream"
        is_photo = mime_type.startswith("image/") and len(file_bytes) <= self.TELEGRAM_MAX_PHOTO_BYTES
        if is_photo:
            method, field, message_type = "sendPhoto", "photo", "photo"
        elif mime_type.startswith("video/"):
            method, field, message_type = "sendVideo", "video", "video"
        else:
            method, field, message_type = "sendDocument", "document", "document"

        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/{method}"
        data = {"chat_id": telegram_chat_id}
        if caption:
            data["caption"] = caption

        try:
            response = requests.post(
                url,
                data=data,
                files={field: (filename, file_bytes, mime_type)},
                timeout=60,
            )
            result = response.json()
        except (requests.RequestException, ValueError) as exc:
            scrubbed = scrub_bot_token(str(exc), self.telegram_bot_token)
            logger.warning("Telegram media send failed: user_id=%s error=%s", getattr(user, "id", None), scrubbed)
            # Report the routing decision even on failure, so a failed PHOTO
            # send isn't mis-recorded as a DOCUMENT by the caller.
            return {"success": False, "error": scrubbed, "message_type": message_type}

        if response.status_code >= 400 or not result.get("ok", False):
            description = result.get("description") or f"Telegram API error (status={response.status_code})"
            logger.warning(
                "Telegram media send rejected: user_id=%s method=%s description=%s",
                getattr(user, "id", None),
                method,
                description,
            )
            return {"success": False, "error": description, "message_type": message_type}

        sent = result.get("result", {}) or {}
        if message_type == "photo":
            sizes = sent.get("photo") or []
            file_id = sizes[-1].get("file_id") if sizes else None
        else:
            file_id = (sent.get(field) or {}).get("file_id")

        if not file_id:
            # A successfully-recorded SENT row with no file_id can never render
            # through the attachment read proxy — silent today, so at least
            # make it visible in the logs.
            logger.warning(
                "Telegram accepted the media send but returned no file_id: user_id=%s method=%s",
                getattr(user, "id", None),
                method,
            )

        return {
            "success": True,
            "message_id": sent.get("message_id"),
            "file_id": file_id,
            "message_type": message_type,
        }

    def send_user_telegram_location(self, user: User, latitude: float, longitude: float) -> Dict[str, Any]:
        """Send a map pin to a customer."""
        if not self.telegram_bot_token:
            return {"success": False, "error": "Telegram bot token is not configured"}
        telegram_chat_id = getattr(user, "telegram_id", None)
        if not telegram_chat_id:
            return {"success": False, "error": get_translation("error.validation.no_telegram_id")}

        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendLocation"
        try:
            response = requests.post(
                url,
                json={"chat_id": telegram_chat_id, "latitude": latitude, "longitude": longitude},
                timeout=15,
            )
            result = response.json()
        except (requests.RequestException, ValueError) as exc:
            return {"success": False, "error": scrub_bot_token(str(exc), self.telegram_bot_token)}

        if response.status_code >= 400 or not result.get("ok", False):
            return {"success": False, "error": result.get("description") or "Telegram rejected the location"}
        return {"success": True, "message_id": result.get("result", {}).get("message_id")}

    def _send_telegram_notification(
        self,
        user: User,
        notification_type: NotificationType,
        template_data: Dict[str, Any],
        language: str,
        template_override=None,
        bot_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send Telegram notification"""
        effective_bot_token = bot_token or self.telegram_bot_token
        if not effective_bot_token:
            raise ConfigurationError(get_translation("error.configuration.telegram_not_configured"))

        notification_type_value = self._status_value(notification_type)
        if notification_type_value == NotificationType.DELIVERY_UPDATE.value:
            delivery_status_code = self._extract_delivery_status_code(template_data or {})
            if not delivery_status_code or not self._should_force_delivery_status_telegram(delivery_status_code):
                logger.info(
                    "Skipped Telegram delivery update: user_id=%s status=%s reason=status_not_allowed",
                    getattr(user, "id", None),
                    delivery_status_code or "unknown",
                )
                return {
                    "success": True,
                    "skipped": True,
                    "reason": "delivery_status_not_allowed",
                }

        # Get user's Telegram ID (serves as chat ID for direct messages)
        telegram_chat_id = getattr(user, "telegram_id", None)
        if not telegram_chat_id:
            return {"success": False, "error": get_translation("error.validation.no_telegram_id")}

        # Get template
        template = template_override or self._get_notification_template(
            notification_type, NotificationChannel.TELEGRAM, language
        )

        if not template:
            return {"success": False, "error": get_translation("error.template_not_found")}

        # Get translated content (or fallback to default)
        template_content = (
            template.get_translated("content", language) if hasattr(template, "get_translated") else template.content
        )

        # Render template
        content = self._render_template(template_content, template_data, language)
        if notification_type_value == NotificationType.DELIVERY_UPDATE.value:
            content = self._strip_driver_info_from_delivery_message(content)

        skip = self._guard_unrendered_placeholders(
            notification_type, NotificationChannel.TELEGRAM.value, content, getattr(user, "id", None)
        )
        if skip:
            return skip

        # Send via Telegram Bot API
        url = f"https://api.telegram.org/bot{effective_bot_token}/sendMessage"
        payload = {"chat_id": telegram_chat_id, "text": content, "parse_mode": "HTML"}

        try:
            response = requests.post(url, json=payload, timeout=15)
            try:
                result = response.json()
            except ValueError:
                result = {}

            if response.status_code >= 400 or not result.get("ok", False):
                description = (
                    result.get("description")
                    or result.get("error")
                    or f"Telegram API error (status={response.status_code})"
                )
                logger.warning(
                    "Telegram notification rejected: user_id=%s notification_type=%s status=%s description=%s",
                    getattr(user, "id", None),
                    self._status_value(notification_type),
                    response.status_code,
                    description,
                )
                return {
                    "success": False,
                    "error": description,
                    "status_code": response.status_code,
                }

            return {"success": True, "message_id": result.get("result", {}).get("message_id")}
        except requests.RequestException as e:
            logger.warning(
                "Telegram notification failed: user_id=%s notification_type=%s error=%s",
                getattr(user, "id", None),
                self._status_value(notification_type),
                e,
            )
            return {"success": False, "error": str(e)}

    @staticmethod
    def _strip_driver_info_from_delivery_message(content: str) -> str:
        """Remove any driver-identifying lines from delivery Telegram messages."""
        if not content:
            return content

        cleaned_lines: List[str] = []
        for line in content.splitlines():
            stripped_line = line.strip()
            normalized_line = re.sub(r"<[^>]+>", "", stripped_line).lower()
            remove_line = False

            if "{driver_name}" in normalized_line or "{driver_phone}" in normalized_line:
                remove_line = True
            elif any(token in normalized_line for token in ("driver", "haydovchi", "водитель")):
                remove_line = True
            elif normalized_line.startswith("📞"):
                remove_line = True
            elif re.match(r"^(phone|telefon|телефон)\s*[:\-]", normalized_line):
                remove_line = True

            if not remove_line:
                cleaned_lines.append(line)

        cleaned_content = "\n".join(cleaned_lines)
        cleaned_content = re.sub(r"\n{3,}", "\n\n", cleaned_content).strip()
        return cleaned_content

    def _send_push_notification(
        self,
        user: User,
        notification_type: NotificationType,
        template_data: Dict[str, Any],
        language: str,
        template_override=None,
    ) -> Dict[str, Any]:
        """Send push notification"""
        # Push notification implementation would depend on your chosen service
        # (Firebase, OneSignal, etc.) - placeholder for now
        return {"success": False, "error": get_translation("error.push_not_implemented")}

    def _send_in_app_notification(
        self,
        user: User,
        notification_type: NotificationType,
        template_data: Dict[str, Any],
        language: str,
        template_override=None,
    ) -> Dict[str, Any]:
        """Create an in-app notification row without an external provider."""
        template = template_override or self._get_notification_template(
            notification_type, NotificationChannel.IN_APP, language
        )
        if not template:
            return {"success": False, "error": get_translation("error.template_not_found")}

        template_subject = (
            template.get_translated("subject", language) if hasattr(template, "get_translated") else template.subject
        )
        template_content = (
            template.get_translated("content", language) if hasattr(template, "get_translated") else template.content
        )

        title = self._render_template(
            template_subject or notification_type.value.replace("_", " ").title(), template_data, language
        )
        content = self._render_template(template_content or "", template_data, language)

        skip = self._guard_unrendered_placeholders(
            notification_type,
            NotificationChannel.IN_APP.value,
            f"{title}\n{content}",
            getattr(user, "id", None),
        )
        if skip:
            return skip

        return {
            "success": True,
            "title": title,
            "content": content,
            "message_id": f"in-app:{uuid.uuid4()}",
        }

    def _get_user_preferred_channels(
        self, user_id: int, notification_type: NotificationType
    ) -> List[NotificationChannel]:
        """Get user's preferred notification channels for a type"""
        notification_type_val = (
            notification_type.value if hasattr(notification_type, "value") else str(notification_type)
        )
        preferences = NotificationPreference.query.filter_by(
            user_id=user_id, notification_type=notification_type_val, is_enabled=True
        ).all()

        if preferences:
            return [NotificationChannel(pref.channel) for pref in preferences]

        # Default preferences if none set
        default_channels = {
            NotificationType.ORDER_CONFIRMATION: [NotificationChannel.EMAIL],
            NotificationType.ORDER_STATUS_UPDATE: [NotificationChannel.TELEGRAM],
            NotificationType.ORDER_EDITED: [NotificationChannel.TELEGRAM],
            NotificationType.DELIVERY_UPDATE: [NotificationChannel.TELEGRAM],
            NotificationType.PAYMENT_CONFIRMATION: [NotificationChannel.EMAIL],
            NotificationType.SUBSCRIPTION_REMINDER: [NotificationChannel.EMAIL],
            NotificationType.PROMOTIONAL: [NotificationChannel.EMAIL],
            NotificationType.SYSTEM: [NotificationChannel.EMAIL],
            NotificationType.LOYALTY_REWARD: [NotificationChannel.EMAIL, NotificationChannel.TELEGRAM],
            NotificationType.REWARD_REDEEMED: [NotificationChannel.EMAIL],
            NotificationType.LOYALTY_TIER_UPGRADE: [NotificationChannel.EMAIL, NotificationChannel.TELEGRAM],
            NotificationType.LOYALTY_POINTS_EXPIRED: [NotificationChannel.EMAIL, NotificationChannel.TELEGRAM],
        }

        return default_channels.get(notification_type, [NotificationChannel.EMAIL])

    def _get_notification_template(
        self, notification_type: NotificationType, channel: NotificationChannel, language: str
    ) -> Optional[NotificationTemplate]:
        """Get notification template"""
        # NotificationTemplate uses TranslatableMixin, so we don't filter by language
        # Instead, we get the template and then retrieve translated content
        notification_type_val = (
            notification_type.value if hasattr(notification_type, "value") else str(notification_type)
        )
        channel_val = channel.value if hasattr(channel, "value") else str(channel)

        template = NotificationTemplate.query.filter_by(
            notification_type=notification_type_val, channel=channel_val, is_active=True
        ).first()

        if template:
            return template

        fallback_template = self._build_default_notification_template(notification_type_val, channel_val)
        if fallback_template:
            logger.warning(
                "Notification template missing in DB; using built-in fallback: notification_type=%s channel=%s",
                notification_type_val,
                channel_val,
            )
            return fallback_template

        return None

    def _build_default_notification_template(self, notification_type: str, channel: str):
        """Build an in-memory fallback template from bundled defaults."""
        template_config = DEFAULT_TEMPLATES.get((notification_type, channel))
        if not template_config:
            return None

        translations = template_config.get("translations", {})
        default_translation = translations.get("uz", {})

        def _get_translated(field_name: str, language: str):
            language_translation = translations.get(language, {})
            if field_name in language_translation:
                return language_translation[field_name]
            if field_name in default_translation:
                return default_translation[field_name]
            return None

        return SimpleNamespace(
            name=template_config.get("name"),
            notification_type=notification_type,
            channel=channel,
            subject=default_translation.get("subject"),
            content=default_translation.get("content", ""),
            is_active=True,
            get_translated=_get_translated,
        )

    def _render_template(self, template: str, data: Dict[str, Any], language: str) -> str:
        """Render template with data"""
        if not template:
            return ""

        try:
            # Simple template rendering - replace placeholders
            rendered = template
            data = data or {}

            # Replace data placeholders
            for key, value in data.items():
                placeholder = f"{{{key}}}"
                brace_placeholder = f"{{{{{key}}}}}"
                rendered = rendered.replace(placeholder, str(value))
                rendered = rendered.replace(brace_placeholder, str(value))

            # Replace translation placeholders
            import re

            translation_pattern = r"\{\{([^}]+)\}\}"
            matches = re.findall(translation_pattern, rendered)

            for match in matches:
                translation = get_translation(match, language)
                rendered = rendered.replace(f"{{{{{match}}}}}", translation)

            return rendered

        except Exception:
            logger.exception("Template rendering failed")
            return template

    def _create_notification_record(
        self,
        user_id: int,
        notification_type: NotificationType,
        channels: List[NotificationChannel],
        template_data: Dict[str, Any],
        results: Dict[str, Any],
        campaign_id: Optional[int] = None,
    ):
        """Persist notification audit rows using a dedicated session.

        A separate session is used so this write is committed independently
        of the caller's transaction. This prevents the notification audit
        commit from inadvertently committing (or rolling back) the main
        request session when send_notification is called mid-transaction
        (e.g. during delivery processing).
        """
        try:
            from sqlalchemy.orm import Session as _Session

            user = User.query.get(user_id)
            payload = template_data or {}

            # Ensure extra_data is JSON-serializable (ORM objects, datetimes,
            # Decimals can arrive in template payloads).
            safe_payload = _json_safe(payload)

            notification_type_value = (
                notification_type.value if hasattr(notification_type, "value") else str(notification_type)
            )

            records = []
            # Create a notification record for each channel
            for channel in channels:
                channel_value = channel.value if hasattr(channel, "value") else str(channel)
                result = results.get(channel_value, {})
                if result.get("skipped"):
                    logger.info(
                        "Skipping notification audit row for skipped channel send: user_id=%s channel=%s notification_type=%s reason=%s",  # noqa: E501
                        user_id,
                        channel_value,
                        notification_type_value,
                        result.get("reason"),
                    )
                    continue

                # Extract message from template_data or use a default
                message = payload.get("message", payload.get("otp_code", "Notification sent"))
                title = payload.get("title", notification_type_value.replace("_", " ").title())

                records.append(
                    Notification(
                        user_id=user_id,
                        notification_type=notification_type_value,
                        channel=channel_value,
                        title=title,
                        message=str(message),
                        is_sent=result.get("success", False),
                        sent_at=datetime.now(timezone.utc) if result.get("success") else None,
                        delivery_status="sent" if result.get("success") else "failed",
                        failure_reason=result.get("error") if not result.get("success") else None,
                        recipient_phone=(
                            getattr(user, "phone", None) if channel_value == NotificationChannel.SMS.value else None
                        ),
                        recipient_email=(
                            getattr(user, "email", None) if channel_value == NotificationChannel.EMAIL.value else None
                        ),
                        recipient_telegram_id=(
                            getattr(user, "telegram_id", None)
                            if channel_value == NotificationChannel.TELEGRAM.value
                            else None
                        ),
                        campaign_id=campaign_id,
                        order_id=safe_payload.get("order_id"),
                        delivery_id=safe_payload.get("delivery_id"),
                        extra_data=safe_payload,
                    )
                )

            with _Session(db.engine) as notif_session:
                notif_session.add_all(records)
                notif_session.commit()

        except Exception:
            logger.exception("Failed to create notification record")


def _json_safe(value, _seen=None):
    """Recursively convert a template payload into JSON-serializable types.

    Payloads can carry ORM objects (e.g. UserAddress), datetimes and Decimals;
    the notifications.extra_data column is JSON, so anything non-primitive must
    be converted before INSERT. Cycles degrade to str() instead of recursing.
    """
    from decimal import Decimal

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Decimal):
        return float(value)
    if _seen is None:
        _seen = set()
    if id(value) in _seen:
        return str(value)
    if isinstance(value, dict):
        _seen.add(id(value))
        return {str(k): _json_safe(v, _seen) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        _seen.add(id(value))
        return [_json_safe(v, _seen) for v in value]
    if hasattr(value, "isoformat"):  # datetime / date / time
        return value.isoformat()
    if hasattr(value, "to_dict"):
        _seen.add(id(value))
        try:
            return _json_safe(value.to_dict(), _seen)
        except Exception:
            return str(value)
    return str(value)


# Default notification templates
# Structure: (notification_type, channel) -> {
#     'name': template name,
#     'translations': {lang: {'subject': ..., 'content': ...}}
# }
# The 'uz' language content is stored as default in the model fields
# Other language translations are stored via TranslatableMixin
DEFAULT_TEMPLATES = {
    # Order confirmation - Email
    ("order_confirmation", "email"): {
        "name": "order_confirmation_email",
        "translations": {
            "uz": {
                "subject": "Buyurtma tasdiqlandi - {{company_name}}",
                "content": """<h2>Buyurtma tasdiqlandi!</h2>
<p>#{order_number} raqamli buyurtmangiz uchun rahmat.</p>
<p><strong>Buyurtma tafsilotlari:</strong></p>
<p><strong>Jami: {order_total} so'm</strong></p>
<p><strong>Yetkazib berish manzili:</strong> {delivery_address}</p>
<p>Buyurtmangiz tayyorlanayotganda va yetkazib berilayotganda sizga xabar beramiz.</p>""",
            },
            "en": {
                "subject": "Order Confirmation - {{company_name}}",
                "content": """<h2>Order Confirmed!</h2>
<p>Thank you for your order #{order_number}.</p>
<p><strong>Order Details:</strong></p>
<p><strong>Total: {order_total} UZS</strong></p>
<p><strong>Delivery Address:</strong> {delivery_address}</p>
<p>We'll notify you when your order is being prepared and out for delivery.</p>""",
            },
            "ru": {
                "subject": "Подтверждение заказа - {{company_name}}",
                "content": """<h2>Заказ подтвержден!</h2>
<p>Спасибо за ваш заказ #{order_number}.</p>
<p><strong>Детали заказа:</strong></p>
<p><strong>Итого: {order_total} сум</strong></p>
<p><strong>Адрес доставки:</strong> {delivery_address}</p>
<p>Мы уведомим вас, когда ваш заказ будет готовиться и доставляться.</p>""",
            },
        },
    },
    # Delivery update - Telegram
    ("delivery_update", "telegram"): {
        "name": "delivery_update_telegram",
        "translations": {
            "uz": {
                "content": """🚚 <b>Yetkazib berish yangiligi</b>

Buyurtma: #{order_number}
Holati: {delivery_status}
Kuzatish: {tracking_code}
"""
            },
            "en": {
                "content": """🚚 <b>Delivery Update</b>

Order: #{order_number}
Status: {delivery_status}
Tracking: {tracking_code}
"""
            },
            "ru": {
                "content": """🚚 <b>Обновление доставки</b>

Заказ: #{order_number}
Статус: {delivery_status}
Отслеживание: {tracking_code}
"""
            },
        },
    },
    # Payment confirmation - Email
    ("payment_confirmation", "email"): {
        "name": "payment_confirmation_email",
        "translations": {
            "uz": {
                "subject": "To'lov tasdiqlandi - {{company_name}}",
                "content": """<h2>To'lov qabul qilindi</h2>
<p>#{order_number} raqamli buyurtmangiz uchun to'lovni muvaffaqiyatli qabul qildik.</p>
<p><strong>To'lov tafsilotlari:</strong></p>
<ul>
    <li>Summa: {payment_amount} so'm</li>
    <li>Usul: {payment_method}</li>
    <li>Havola: {payment_reference}</li>
</ul>
<p>{payment_follow_up_message}</p>""",
            },
            "en": {
                "subject": "Payment Confirmation - {{company_name}}",
                "content": """<h2>Payment Received</h2>
<p>We have successfully received your payment for order #{order_number}.</p>
<p><strong>Payment Details:</strong></p>
<ul>
    <li>Amount: {payment_amount} UZS</li>
    <li>Method: {payment_method}</li>
    <li>Reference: {payment_reference}</li>
</ul>
<p>{payment_follow_up_message}</p>""",
            },
            "ru": {
                "subject": "Подтверждение оплаты - {{company_name}}",
                "content": """<h2>Оплата получена</h2>
<p>Мы успешно получили вашу оплату за заказ #{order_number}.</p>
<p><strong>Детали оплаты:</strong></p>
<ul>
    <li>Сумма: {payment_amount} сум</li>
    <li>Способ: {payment_method}</li>
    <li>Ссылка: {payment_reference}</li>
</ul>
<p>{payment_follow_up_message}</p>""",
            },
        },
    },
    # Payment confirmation - Telegram
    ("payment_confirmation", "telegram"): {
        "name": "payment_confirmation_telegram",
        "translations": {
            "uz": {
                "content": """✅ <b>To'lov tasdiqlandi!</b>

Buyurtma: #{order_number}
Summa: {payment_amount} so'm
Usul: {payment_method}

{payment_follow_up_message}

Xaridingiz uchun rahmat!"""
            },
            "en": {
                "content": """✅ <b>Payment Confirmed!</b>

Order: #{order_number}
Amount: {payment_amount} UZS
Method: {payment_method}

{payment_follow_up_message}

Thank you for your purchase!"""
            },
            "ru": {
                "content": """✅ <b>Оплата подтверждена!</b>

Заказ: #{order_number}
Сумма: {payment_amount} сум
Способ: {payment_method}

{payment_follow_up_message}

Спасибо за покупку!"""
            },
        },
    },
    # Late Click debit credited as prepaid balance - Email
    ("payment_late_debit_credited", "email"): {
        "name": "payment_late_debit_credited_email",
        "translations": {
            "uz": {
                "subject": "To'lov oldindan to'lov sifatida hisobga olindi - {{company_name}}",
                "content": "Buyurtma {order_number} uchun {amount} so'mlik karta to'lovingiz buyurtma allaqachon to'langanidan keyin keldi. Ushbu summa hisobingizga oldindan to'lov sifatida qo'shildi va keyingi buyurtmangizga avtomatik qo'llaniladi.",  # noqa: E501
            },
            "en": {
                "subject": "Payment credited as prepaid balance - {{company_name}}",
                "content": "Your card payment of {amount} UZS for order {order_number} arrived after the order was already settled. The amount has been added to your account as prepaid credit and will apply to your next order automatically.",  # noqa: E501
            },
            "ru": {
                "subject": "Платёж зачислен как предоплата - {{company_name}}",
                "content": "Ваш платёж картой на {amount} сум за заказ {order_number} поступил после того, как заказ уже был оплачен. Сумма зачислена на ваш счёт как предоплата и автоматически применится к следующему заказу.",  # noqa: E501
            },
        },
    },
    # Late Click debit credited as prepaid balance - Telegram
    ("payment_late_debit_credited", "telegram"): {
        "name": "payment_late_debit_credited_telegram",
        "translations": {
            "uz": {
                "content": "Buyurtma {order_number} uchun {amount} so'mlik karta to'lovingiz buyurtma allaqachon to'langanidan keyin keldi. Ushbu summa hisobingizga oldindan to'lov sifatida qo'shildi va keyingi buyurtmangizga avtomatik qo'llaniladi.",  # noqa: E501
            },
            "en": {
                "content": "Your card payment of {amount} UZS for order {order_number} arrived after the order was already settled. The amount has been added to your account as prepaid credit and will apply to your next order automatically.",  # noqa: E501
            },
            "ru": {
                "content": "Ваш платёж картой на {amount} сум за заказ {order_number} поступил после того, как заказ уже был оплачен. Сумма зачислена на ваш счёт как предоплата и автоматически применится к следующему заказу.",  # noqa: E501
            },
        },
    },
    # Online payment auto-cancelled by reconciliation - Email
    ("payment_autocancel_retry", "email"): {
        "name": "payment_autocancel_retry_email",
        "translations": {
            "uz": {
                "subject": "Onlayn to'lovingiz bekor qilindi - {{company_name}}",
                "content": "Buyurtma {order_number} uchun onlayn to'lovingizni tasdiqlay olmadik, shuning uchun u bekor qilindi. Hech qanday summa yechilmadi. Buyurtmangizdan qayta to'lashingiz yoki boshqa to'lov usulini tanlashingiz mumkin.",  # noqa: E501
            },
            "en": {
                "subject": "Your online payment was cancelled - {{company_name}}",
                "content": "We couldn't confirm your online payment for order {order_number}, so it was cancelled. Nothing was charged. You can pay again from your order, or choose another payment method.",  # noqa: E501
            },
            "ru": {
                "subject": "Ваша онлайн-оплата отменена - {{company_name}}",
                "content": "Мы не смогли подтвердить вашу онлайн-оплату заказа {order_number}, поэтому она была отменена. Средства не списаны. Вы можете оплатить заказ повторно или выбрать другой способ оплаты.",  # noqa: E501
            },
        },
    },
    # Online payment auto-cancelled by reconciliation - Telegram
    ("payment_autocancel_retry", "telegram"): {
        "name": "payment_autocancel_retry_telegram",
        "translations": {
            "uz": {
                "content": "Buyurtma {order_number} uchun onlayn to'lovingizni tasdiqlay olmadik, shuning uchun u bekor qilindi. Hech qanday summa yechilmadi. Buyurtmangizdan qayta to'lashingiz yoki boshqa to'lov usulini tanlashingiz mumkin.",  # noqa: E501
            },
            "en": {
                "content": "We couldn't confirm your online payment for order {order_number}, so it was cancelled. Nothing was charged. You can pay again from your order, or choose another payment method.",  # noqa: E501
            },
            "ru": {
                "content": "Мы не смогли подтвердить вашу онлайн-оплату заказа {order_number}, поэтому она была отменена. Средства не списаны. Вы можете оплатить заказ повторно или выбрать другой способ оплаты.",  # noqa: E501
            },
        },
    },
    # Loyalty reward - Email
    ("loyalty_reward", "email"): {
        "name": "loyalty_reward_email",
        "translations": {
            "uz": {
                "subject": "AquaCoins yangiligi - {{company_name}}",
                "content": """<h2>AquaCoins yangiligi</h2>
<p>Tabriklaymiz! Siz {points} AquaCoins qo'lga kiritdingiz.</p>
<p>Joriy balansingiz va mavjud mukofotlarni ko'rish uchun hisobingizga tashrif buyuring.</p>""",
            },
            "en": {
                "subject": "AquaCoins Update - {{company_name}}",
                "content": """<h2>AquaCoins Update</h2>
<p>Congratulations! You've earned {points} AquaCoins.</p>
<p>Visit your account to see your current balance and available rewards.</p>""",
            },
            "ru": {
                "subject": "Обновление AquaCoins - {{company_name}}",
                "content": """<h2>Обновление AquaCoins</h2>
<p>Поздравляем! Вы заработали {points} AquaCoins.</p>
<p>Посетите свой аккаунт, чтобы увидеть текущий баланс и доступные награды.</p>""",
            },
        },
    },
    # Loyalty reward - Telegram (reason-aware, balance included)
    ("loyalty_reward", "telegram"): {
        "name": "loyalty_reward_telegram",
        "translations": {
            "uz": {
                "content": """🪙 <b>AquaCoins qo'shildi!</b>

Sabab: {reason_label}
Siz {points} AquaCoins qo'lga kiritdingiz.
Joriy balans: {balance} AquaCoins""",
            },
            "ru": {
                "content": """🪙 <b>AquaCoins начислены!</b>

Причина: {reason_label}
Вы заработали {points} AquaCoins.
Текущий баланс: {balance} AquaCoins""",
            },
            "en": {
                "content": """🪙 <b>AquaCoins earned!</b>

Reason: {reason_label}
You've earned {points} AquaCoins.
Current balance: {balance} AquaCoins""",
            },
        },
    },
    # Loyalty tier upgrade - Telegram (its own copy; must never reuse the
    # "AquaCoins earned" template, whose {points}/{balance} this event cannot fill)
    ("loyalty_tier_upgrade", "telegram"): {
        "name": "loyalty_tier_upgrade_telegram",
        "translations": {
            "uz": {
                "content": """🎉 <b>Tabriklaymiz!</b>

Siz {tier_label} darajasiga ko'tarildingiz.
Joriy balans: {balance} AquaCoins""",
            },
            "ru": {
                "content": """🎉 <b>Поздравляем!</b>

Вы достигли уровня {tier_label}.
Текущий баланс: {balance} AquaCoins""",
            },
            "en": {
                "content": """🎉 <b>Congratulations!</b>

You've reached the {tier_label} tier.
Current balance: {balance} AquaCoins""",
            },
        },
    },
    # Loyalty tier upgrade - Email
    ("loyalty_tier_upgrade", "email"): {
        "name": "loyalty_tier_upgrade_email",
        "translations": {
            "uz": {
                "subject": "Yangi daraja: {tier_label} - {{company_name}}",
                "content": """<h2>Tabriklaymiz!</h2>
<p>Siz {tier_label} darajasiga ko'tarildingiz.</p>
<p>Joriy balans: <strong>{balance} AquaCoins</strong></p>""",
            },
            "ru": {
                "subject": "Новый уровень: {tier_label} - {{company_name}}",
                "content": """<h2>Поздравляем!</h2>
<p>Вы достигли уровня {tier_label}.</p>
<p>Текущий баланс: <strong>{balance} AquaCoins</strong></p>""",
            },
            "en": {
                "subject": "New tier: {tier_label} - {{company_name}}",
                "content": """<h2>Congratulations!</h2>
<p>You've reached the {tier_label} tier.</p>
<p>Current balance: <strong>{balance} AquaCoins</strong></p>""",
            },
        },
    },
    # Loyalty points expired - Telegram
    ("loyalty_points_expired", "telegram"): {
        "name": "loyalty_points_expired_telegram",
        "translations": {
            "uz": {
                "content": """⏳ <b>AquaCoins muddati tugadi</b>

{points} AquaCoins muddati tugadi.
Joriy balans: {balance} AquaCoins""",
            },
            "ru": {
                "content": """⏳ <b>Срок AquaCoins истёк</b>

Сгорело {points} AquaCoins.
Текущий баланс: {balance} AquaCoins""",
            },
            "en": {
                "content": """⏳ <b>AquaCoins expired</b>

{points} AquaCoins have expired.
Current balance: {balance} AquaCoins""",
            },
        },
    },
    # Loyalty points expired - Email
    ("loyalty_points_expired", "email"): {
        "name": "loyalty_points_expired_email",
        "translations": {
            "uz": {
                "subject": "AquaCoins muddati tugadi - {{company_name}}",
                "content": """<h2>AquaCoins muddati tugadi</h2>
<p>{points} AquaCoins muddati tugadi.</p>
<p>Joriy balans: <strong>{balance} AquaCoins</strong></p>""",
            },
            "ru": {
                "subject": "Срок AquaCoins истёк - {{company_name}}",
                "content": """<h2>Срок AquaCoins истёк</h2>
<p>Сгорело {points} AquaCoins.</p>
<p>Текущий баланс: <strong>{balance} AquaCoins</strong></p>""",
            },
            "en": {
                "subject": "AquaCoins expired - {{company_name}}",
                "content": """<h2>AquaCoins expired</h2>
<p>{points} AquaCoins have expired.</p>
<p>Current balance: <strong>{balance} AquaCoins</strong></p>""",
            },
        },
    },
    # Delivery delay alert - Email (sent to admins/managers by
    # monitor_delivery_delays; default channel routing for this ad-hoc
    # notification type falls back to email since it has no NotificationType
    # enum member / preference mapping).
    ("delivery_delay_alert", "email"): {
        "name": "delivery_delay_alert_email",
        "translations": {
            "uz": {
                "subject": "Yetkazib berish kechikmoqda - {{company_name}}",
                "content": """<h2>Yetkazib berish kechikmoqda</h2>
<p>#{order_number} buyurtma (yetkazib berish #{delivery_id}) kutilgan vaqtdan {delay_minutes} daqiqa kechikmoqda.</p>
<p><strong>Haydovchi:</strong> {driver_name}</p>
<p><strong>Mijoz telefoni:</strong> {customer_phone}</p>
<p>Iltimos, holatni tekshiring va zarur bo'lsa mijoz bilan bog'laning.</p>""",
            },
            "en": {
                "subject": "Delivery Delay Alert - {{company_name}}",
                "content": """<h2>Delivery Delay Alert</h2>
<p>Order #{order_number} (delivery #{delivery_id}) is {delay_minutes} minutes overdue.</p>
<p><strong>Driver:</strong> {driver_name}</p>
<p><strong>Customer phone:</strong> {customer_phone}</p>
<p>Please review the delivery status and contact the customer if needed.</p>""",
            },
            "ru": {
                "subject": "Задержка доставки - {{company_name}}",
                "content": """<h2>Задержка доставки</h2>
<p>Заказ #{order_number} (доставка #{delivery_id}) просрочен на {delay_minutes} минут.</p>
<p><strong>Водитель:</strong> {driver_name}</p>
<p><strong>Телефон клиента:</strong> {customer_phone}</p>
<p>Пожалуйста, проверьте статус доставки и при необходимости свяжитесь с клиентом.</p>""",
            },
        },
    },
}


def seed_notification_templates():
    """
    Seed default notification templates with multilingual support.
    Uses TranslatableMixin for translations storage.
    """
    for (notification_type, channel), template_config in DEFAULT_TEMPLATES.items():
        # Check if template already exists
        existing = NotificationTemplate.query.filter_by(notification_type=notification_type, channel=channel).first()

        translations = template_config.get("translations", {})
        # Use Uzbek as the default/base language
        uz_translation = translations.get("uz", {})

        if not existing:
            # Create new template with Uzbek as default content
            template = NotificationTemplate(
                name=template_config["name"],
                notification_type=notification_type,
                channel=channel,
                subject=uz_translation.get("subject", ""),
                content=uz_translation.get("content", ""),
                is_active=True,
            )
            db.session.add(template)
            db.session.flush()  # Get the ID for setting translations
        else:
            template = existing
            # Update base content if needed
            template.subject = uz_translation.get("subject", template.subject or "")
            template.content = uz_translation.get("content", template.content or "")

        # Set translations for all languages using TranslatableMixin
        for lang, lang_translations in translations.items():
            if "subject" in lang_translations:
                template.set_translated("subject", lang_translations["subject"], lang)
            if "content" in lang_translations:
                template.set_translated("content", lang_translations["content"], lang)

    db.session.commit()
    logger.info(f"Seeded {len(DEFAULT_TEMPLATES)} notification templates with translations")
