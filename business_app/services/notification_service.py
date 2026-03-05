"""
Notification service for the Water Business Platform
Handles SMS, Email, Telegram, and Push notifications
"""
import json
import logging
import re
from celery.utils.log import get_task_logger
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from typing import Dict, Any, List, Optional
from flask import current_app
import requests
from eskiz_sms import EskizSMS

from business_app.models.notification import (
    Notification,
    NotificationTemplate,
    NotificationPreference,
    PushNotificationToken,
)
from business_app.models.user import User
from business_app.models.order import Order
from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.payment import Payment
from business_app.models.subscription import Subscription
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
    DeliveryStatus,
)
from business_app.utils.translations import get_translation
from business_app.services.email_template_service import get_email_template_service
from business_app import db
from sqlalchemy import func

# Use standard logging that works in both Flask and Celery contexts
# logger = logging.getLogger(__name__)
logger = get_task_logger(__name__)

# Configure logger to ensure it outputs in both Flask and Celery
# if not logger.handlers:
#     handler = logging.StreamHandler()
#     handler.setFormatter(logging.Formatter(
#         '[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
#     ))
#     logger.addHandler(handler)
#     logger.setLevel(logging.INFO)
#     logger.propagate = True


class NotificationService:
    """Service for handling notifications across multiple channels"""

    NOTIFICATION_TYPE_GROUPS = {
        'order': [
            NotificationType.ORDER_CONFIRMATION.value,
            NotificationType.ORDER_STATUS_UPDATE.value,
            NotificationType.ORDER_UPDATE.value,
        ],
        'delivery': [
            NotificationType.DELIVERY_UPDATE.value,
            NotificationType.DELIVERY_REMINDER.value,
        ],
        'payment': [NotificationType.PAYMENT_CONFIRMATION.value],
        'promotion': [NotificationType.PROMOTIONAL.value],
        'system': [
            NotificationType.SYSTEM.value,
            NotificationType.SYSTEM_ALERT.value,
            NotificationType.EMAIL_VERIFICATION.value,
            NotificationType.PASSWORD_RESET.value,
        ],
        'loyalty': [
            NotificationType.LOYALTY_REWARD.value,
            NotificationType.REWARD_REDEEMED.value,
        ],
        'security': [NotificationType.SECURITY.value],
        'reminder': [NotificationType.SUBSCRIPTION_REMINDER.value],
        'subscription': [
            NotificationType.SUBSCRIPTION_CREATED.value,
            NotificationType.SUBSCRIPTION_RENEWAL.value,
            NotificationType.SUBSCRIPTION_CANCELLED.value,
            NotificationType.SUBSCRIPTION_CANCELLATION_SCHEDULED.value,
            NotificationType.SUBSCRIPTION_REMINDER.value,
        ],
    }
    
    def __init__(self):
        # Email configuration (Brevo)
        self.brevo_api_key = current_app.config.get('BREVO_API_KEY')
        self.default_sender_email = current_app.config.get('BREVO_SENDER_EMAIL') or current_app.config.get('MAIL_DEFAULT_SENDER')
        self.default_sender_name = current_app.config.get('BREVO_SENDER_NAME') or current_app.config.get('COMPANY_NAME', 'Bluestream')

        # SMS configuration (Eskiz)
        self.eskiz_email = current_app.config.get('ESKIZ_EMAIL')
        self.eskiz_password = current_app.config.get('ESKIZ_PASSWORD')
        self.eskiz_from = current_app.config.get('ESKIZ_FROM', '4546')
        
        # Telegram configuration
        self.telegram_bot_token = current_app.config.get('TELEGRAM_BOT_TOKEN')
        
        # Company information
        self.company_name = current_app.config.get('COMPANY_NAME', 'Aqua Element')
        self.company_phone = current_app.config.get('COMPANY_PHONE')
        self.company_email = current_app.config.get('COMPANY_EMAIL')
        
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
        logger.info(f"DEBUG: Initializing Eskiz SMS - email: {self.eskiz_email}, has_password: {bool(self.eskiz_password)}")
        if self.eskiz_email and self.eskiz_password:
            try:
                logger.info("DEBUG: Creating EskizSMS client instance...")
                self.eskiz_client = EskizSMS(
                    email=self.eskiz_email,
                    password=self.eskiz_password,
                    save_token=True,
                    env_file_path='.env'
                )
                logger.info(f"DEBUG: Eskiz SMS client initialized successfully: {type(self.eskiz_client)}")
            except Exception as e:
                logger.error(f"Failed to initialize Eskiz SMS client: {e}", exc_info=True)
                self.eskiz_client = None
        else:
            logger.warning(f"DEBUG: Eskiz credentials missing - email: {bool(self.eskiz_email)}, password: {bool(self.eskiz_password)}")
            self.eskiz_client = None
    
    def send_notification(self, user_id: int, notification_type: NotificationType,
                         channels: List[NotificationChannel] = None,
                         template_data: Dict[str, Any] = None,
                         priority: str = 'normal') -> Dict[str, Any]:
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
            raise NotificationError(get_translation('error.not_found'))
        
        # Get user's notification preferences
        if channels is None:
            channels = self._get_user_preferred_channels(user_id, notification_type)
        
        # Get user's language preference
        user_language = getattr(user, 'preferred_language', 'en')
        
        results = {}
        
        for channel in channels:
            try:
                if channel == NotificationChannel.EMAIL:
                    result = self._send_email_notification(
                        user, notification_type, template_data, user_language
                    )
                elif channel == NotificationChannel.SMS:
                    result = self._send_sms_notification(
                        user, notification_type, template_data, user_language
                    )
                elif channel == NotificationChannel.TELEGRAM:
                    result = self._send_telegram_notification(
                        user, notification_type, template_data, user_language
                    )
                elif channel == NotificationChannel.PUSH:
                    result = self._send_push_notification(
                        user, notification_type, template_data, user_language
                    )
                else:
                    result = {'success': False, 'error': f'Unsupported channel: {channel.value}'}
                
                results[channel.value] = result
                
            except Exception as e:
                logger.error(f"Failed to send {channel.value} notification: {e}")
                results[channel.value] = {'success': False, 'error': str(e)}
        
        # Create notification record
        self._create_notification_record(
            user_id, notification_type, channels, template_data, results
        )
        
        return results
    
    def send_bulk_notification(self, user_ids: List[int], notification_type: NotificationType,
                              template_data: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send notification to multiple users"""
        results = {
            'successful': 0,
            'failed': 0,
            'errors': []
        }
        
        for user_id in user_ids:
            try:
                self.send_notification(user_id, notification_type, None, template_data)
                results['successful'] += 1
            except Exception as e:
                results['failed'] += 1
                results['errors'].append({
                    'user_id': user_id,
                    'error': str(e)
                })
        
        return results
    
    def send_order_notification(self, order_id: int, event_type: str) -> Dict[str, Any]:
        """Send order-related notification"""
        order = Order.query.get(order_id)
        if not order:
            raise NotificationError(get_translation('error.not_found'))
        
        # Map event types to notification types
        event_mapping = {
            'order_created': NotificationType.ORDER_CONFIRMATION,
            'status_changed_confirmed': NotificationType.ORDER_STATUS_UPDATE,
            'status_changed_preparing': NotificationType.ORDER_STATUS_UPDATE,
            'status_changed_out_for_delivery': NotificationType.DELIVERY_UPDATE,
            'status_changed_delivered': NotificationType.DELIVERY_UPDATE,
            'status_changed_cancelled': NotificationType.ORDER_STATUS_UPDATE
        }
        
        notification_type = event_mapping.get(event_type, NotificationType.ORDER_STATUS_UPDATE)
        
        template_data = {
            'order_number': order.order_number,
            'order_status': order.status.value,
            'order_total': order.total_amount,
            'delivery_address': order.delivery_address.street_address if order.delivery_address else None,
            'estimated_delivery': order.estimated_delivery_time.isoformat() if hasattr(order, 'estimated_delivery_time') and order.estimated_delivery_time else None,
            'items': [
                {
                    'name': item.product.name if item.product else 'Unknown',
                    'quantity': item.quantity,
                    'price': item.total_price
                }
                for item in order.order_items
            ]
        }

        if notification_type == NotificationType.DELIVERY_UPDATE:
            order_status_value = self._status_value(order.status)
            delivery = Delivery.query.filter_by(order_id=order.id).first()
            language = getattr(order.user, 'preferred_language', 'en') if order.user else 'en'
            template_data.update(
                {
                    'delivery_status_code': order_status_value,
                    'delivery_status': self._get_localized_delivery_status_label(order_status_value, language),
                    'tracking_code': (
                        delivery.tracking_number if delivery and getattr(delivery, 'tracking_number', None) else ''
                    ),
                }
            )
        
        return self.send_notification(order.user_id, notification_type, None, template_data)
    
    def send_delivery_notification(self, delivery_id: int, event_type: str) -> Dict[str, Any]:
        """Send delivery-related notification"""
        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            raise NotificationError(get_translation('error.not_found'))

        template_data = {
            'tracking_code': delivery.tracking_number,
            'order_number': delivery.order.order_number if delivery.order else '',
            'delivery_status': delivery.status.value,
            'estimated_delivery': delivery.estimated_delivery_time.isoformat() if delivery.estimated_delivery_time else None,
            'event_type': event_type,
        }
        
        return self.send_notification(
            delivery.order.user_id,
            NotificationType.DELIVERY_UPDATE,
            None,
            template_data
        )

    def send_delivery_status_change_notification(self, history_id: int) -> Dict[str, Any]:
        """Send delivery-status notification from a committed history event snapshot."""
        history = DeliveryStatusHistory.query.get(history_id)
        if not history:
            logger.warning("Delivery status history %s not found; skipping notification", history_id)
            return {'success': False, 'error': 'Delivery status history not found'}

        delivery = Delivery.query.get(history.delivery_id)
        if not delivery:
            logger.warning(
                "Delivery %s not found for delivery status history %s; skipping notification",
                history.delivery_id,
                history_id,
            )
            return {'success': False, 'error': 'Delivery not found'}

        order = delivery.order
        if not order:
            logger.warning(
                "Order missing for delivery %s (history %s); skipping notification",
                delivery.id,
                history_id,
            )
            return {'success': False, 'error': 'Order not found'}

        user = User.query.get(order.user_id)
        if not user:
            logger.warning(
                "User %s not found for delivery %s (history %s); skipping notification",
                order.user_id,
                delivery.id,
                history_id,
            )
            return {'success': False, 'error': 'User not found'}

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

        language = getattr(user, 'preferred_language', 'en') or 'en'
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
    
    def send_payment_notification(self, payment_id: int) -> Dict[str, Any]:
        """Send payment confirmation notification via Telegram (if user has telegram) or email"""
        payment = Payment.query.get(payment_id)
        if not payment:
            raise NotificationError(get_translation('error.not_found'))

        user = User.query.get(payment.user_id)
        if not user:
            raise NotificationError(get_translation('error.not_found'))

        template_data = {
            'order_number': payment.order.order_number if payment.order else 'N/A',
            'payment_amount': payment.amount,
            'payment_method': payment.payment_method.value if payment.payment_method else 'unknown',
            'payment_reference': payment.payment_id  # Use payment_id as reference
        }

        # Determine channels: use Telegram if user has telegram_id, otherwise email
        channels = []
        if user.telegram_id:
            channels.append(NotificationChannel.TELEGRAM)
        else:
            channels.append(NotificationChannel.EMAIL)

        return self.send_notification(
            payment.user_id,
            NotificationType.PAYMENT_CONFIRMATION,
            channels,
            template_data
        )
    
    def send_subscription_notification(self, subscription_id: int, event_type: str) -> Dict[str, Any]:
        """Send subscription-related notification"""
        subscription = Subscription.query.get(subscription_id)
        if not subscription:
            raise NotificationError(get_translation('error.not_found'))
        
        template_data = {
            'subscription_id': subscription.id,
            'plan_name': subscription.plan.name if subscription.plan else 'Standard',
            'frequency': subscription.frequency.value,
            'total_amount': subscription.total_amount,
            'next_billing_date': subscription.next_billing_date.isoformat() if subscription.next_billing_date else None,
            'event_type': event_type
        }
        
        return self.send_notification(
            subscription.user_id,
            NotificationType.SUBSCRIPTION_REMINDER,
            None,
            template_data
        )
    
    def send_loyalty_notification(self, user_id: int, event_type: str,
                                 data: Dict[str, Any], 
                                 notification_type: NotificationType = None) -> Dict[str, Any]:
        """Send loyalty program notification
        
        Args:
            user_id: User to notify
            event_type: Type of loyalty event (earned, redeemed, etc.)
            data: Template data
            notification_type: Notification type to use (defaults to LOYALTY_REWARD)
        """
        template_data = {
            'event_type': event_type,
            **data
        }
        
        # Use provided notification type or default to LOYALTY_REWARD
        notif_type = notification_type if notification_type else NotificationType.LOYALTY_REWARD
        
        return self.send_notification(
            user_id,
            notif_type,
            None,
            template_data
        )
    
    def update_notification_preferences(self, user_id: int,
                                      preferences: Dict[str, Any]) -> bool:
        """Update user's notification preferences"""
        try:
            for notification_type, channels in preferences.items():
                # Remove existing preferences for this type
                NotificationPreference.query.filter_by(
                    user_id=user_id,
                    notification_type=notification_type
                ).delete()
                
                # Add new preferences
                for channel, enabled in channels.items():
                    if enabled:
                        preference = NotificationPreference(
                            user_id=user_id,
                            notification_type=notification_type,
                            channel=channel,
                            is_enabled=True
                        )
                        db.session.add(preference)
            
            db.session.commit()
            return True
            
        except Exception as e:
            logger.error(f"Failed to update notification preferences: {e}")
            db.session.rollback()
            return False
    
    def get_notification_preferences(self, user_id: int) -> Dict[str, Any]:
        """Get user's notification preferences"""
        preferences = NotificationPreference.query.filter_by(
            user_id=user_id,
            is_enabled=True
        ).all()
        
        result = {}
        for pref in preferences:
            if pref.notification_type not in result:
                result[pref.notification_type] = []
            result[pref.notification_type].append(pref.channel)
        
        return result
    
    def create_notification_template(self, name: str, notification_type: NotificationType,
                                   channel: NotificationChannel, language: str,
                                   subject: str = None, content: str = None) -> NotificationTemplate:
        """Create notification template"""
        template = NotificationTemplate(
            name=name,
            notification_type=notification_type,
            channel=channel,
            language=language,
            subject=subject,
            content=content,
            is_active=True
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
            'items': pagination.items,
            'page': page,
            'per_page': per_page,
            'total': pagination.total,
            'unread_count': unread_count,
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
            order_notifications=_group_enabled('order'),
            delivery_notifications=_group_enabled('delivery'),
            payment_notifications=_group_enabled('payment'),
            promotion_notifications=_group_enabled('promotion'),
            system_notifications=_group_enabled('system'),
            loyalty_notifications=_group_enabled('loyalty'),
            security_notifications=_group_enabled('security'),
            reminder_notifications=_group_enabled('reminder'),
            quiet_hours_enabled=False,
            quiet_hours_start=None,
            quiet_hours_end=None,
            digest_enabled=False,
            digest_frequency='weekly',
            updated_at=datetime.now(timezone.utc),
            _mapped_preferences=mapped,
            _all_types=all_types,
        )

    def update_notification_preferences_for_user(self, user_id: int, payload: Dict[str, Any]):
        """Update notification preferences from API payload and return current view."""
        mapped = self._map_preferences(
            NotificationPreference.query.filter_by(user_id=user_id, is_enabled=True).all()
        )
        all_types = self._all_managed_types()

        channel_flags = {
            NotificationChannel.EMAIL: payload.get('email_enabled'),
            NotificationChannel.SMS: payload.get('sms_enabled'),
            NotificationChannel.PUSH: payload.get('push_enabled'),
            NotificationChannel.IN_APP: payload.get('in_app_enabled'),
            NotificationChannel.TELEGRAM: payload.get('telegram_enabled'),
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
            'order_notifications': 'order',
            'order_updates': 'order',
            'delivery_notifications': 'delivery',
            'delivery_updates': 'delivery',
            'payment_notifications': 'payment',
            'payment_updates': 'payment',
            'promotion_notifications': 'promotion',
            'marketing_emails': 'promotion',
            'promotional_sms': 'promotion',
            'system_notifications': 'system',
            'system_alerts': 'system',
            'loyalty_notifications': 'loyalty',
            'loyalty_updates': 'loyalty',
            'security_notifications': 'security',
            'reminder_notifications': 'reminder',
            'subscription_updates': 'subscription',
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

        db.session.commit()
        return self.get_notification_preferences_view(user_id)

    def register_push_token_for_user(
        self,
        user_id: int,
        token: str,
        platform: str,
        device_id: Optional[str] = None,
    ) -> None:
        """Register or update a push token for a user."""
        if platform not in ['ios', 'android', 'web']:
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
        return {'notification_id': created.id if created else None}

    def get_notification_statistics_for_user(self, user_id: int, period: str = 'month') -> Dict[str, Any]:
        """Get notification statistics for a user over a period."""
        now = datetime.now(timezone.utc)
        if period == 'week':
            start_date = now - timedelta(weeks=1)
        elif period == 'month':
            start_date = now - timedelta(days=30)
        elif period == 'quarter':
            start_date = now - timedelta(days=90)
        elif period == 'year':
            start_date = now - timedelta(days=365)
        else:
            start_date = now - timedelta(days=30)

        base_query = Notification.query.filter_by(user_id=user_id).filter(
            Notification.created_at >= start_date
        )

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
            key = channel_value.value if hasattr(channel_value, 'value') else str(channel_value)
            notifications_by_channel[key] = count

        daily_stats = (
            db.session.query(
                func.date(Notification.created_at).label('date'),
                func.count(Notification.id).label('count'),
            )
            .filter_by(user_id=user_id)
            .filter(Notification.created_at >= start_date)
            .group_by(func.date(Notification.created_at))
            .all()
        )
        daily_notifications = {date_obj.isoformat(): count for date_obj, count in daily_stats}

        return {
            'period': period,
            'statistics': {
                'total_notifications': total_notifications,
                'read_notifications': read_notifications,
                'unread_notifications': unread_notifications,
                'read_rate': round((read_notifications / total_notifications * 100), 2) if total_notifications > 0 else 0,
                'notifications_by_type': notifications_by_type,
                'notifications_by_channel': notifications_by_channel,
                'daily_trend': daily_notifications,
            },
        }

    def get_user_notification_channels(self, user_id: int) -> Dict[str, Any]:
        """Get available channels for a user."""
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")

        push_tokens = PushNotificationToken.query.filter_by(user_id=user_id, is_active=True).all()

        return {
            'email': {
                'available': bool(user.email and user.email_verified),
                'address': user.email if user.email_verified else None,
                'verified': user.email_verified,
            },
            'sms': {
                'available': bool(user.phone and user.phone_verified),
                'number': user.phone if user.phone_verified else None,
                'verified': user.phone_verified,
            },
            'push': {
                'available': len(push_tokens) > 0,
                'devices': [
                    {
                        'platform': token.platform,
                        'device_id': token.device_id,
                        'registered_at': token.created_at.isoformat(),
                    }
                    for token in push_tokens
                ],
            },
            'telegram': {
                'available': bool(getattr(user, 'telegram_id', None)),
                'chat_id': getattr(user, 'telegram_id', None),
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
        return {'task_id': task.id, 'recipient_count': len(user_ids)}

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
                delivered_at = notif.extra_data.get('delivered_at')
            reports.append(
                {
                    'id': notif.id,
                    'user_id': notif.user_id,
                    'channel': notif.channel.value if hasattr(notif.channel, 'value') else notif.channel,
                    'status': notif.delivery_status.value if hasattr(notif.delivery_status, 'value') else notif.delivery_status,
                    'created_at': notif.created_at.isoformat(),
                    'sent_at': notif.sent_at.isoformat() if notif.sent_at else None,
                    'delivered_at': delivered_at,
                    'error_message': notif.failure_reason,
                }
            )

        return {
            'items': reports,
            'page': page,
            'per_page': per_page,
            'total': pagination.total,
            'summary': {
                'total_sent': total_sent,
                'delivered': delivered,
                'failed': failed,
                'pending': pending,
                'delivery_rate': round((delivered / total_sent * 100), 2) if total_sent > 0 else 0,
            },
        }

    def _mark_notification_read(self, notification: Notification) -> None:
        """Mark a notification as read using delivery status and metadata."""
        notification.delivery_status = NotificationStatus.READ
        # JSON columns are not always mutation-tracked; assign a copied dict.
        source = notification.extra_data if isinstance(notification.extra_data, dict) else {}
        extra_data = dict(source)
        extra_data['read_at'] = datetime.now(timezone.utc).isoformat()
        notification.extra_data = extra_data

    def _all_managed_types(self) -> List[str]:
        """Get a de-duplicated list of managed notification types."""
        all_types = []
        for values in self.NOTIFICATION_TYPE_GROUPS.values():
            all_types.extend(values)
        return sorted(set(all_types))

    def _map_preferences(self, rows: List[NotificationPreference]) -> Dict[str, set]:
        """Map preference rows to type->set(channel_value)."""
        mapped: Dict[str, set] = {}
        for row in rows:
            type_key = row.notification_type
            if type_key not in mapped:
                mapped[type_key] = set()
            channel_value = row.channel.value if hasattr(row.channel, 'value') else str(row.channel)
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

    def _enabled_channels_from_payload(self, payload: Dict[str, Any]) -> List[NotificationChannel]:
        """Extract globally enabled channels from update payload."""
        mapping = {
            'email_enabled': NotificationChannel.EMAIL,
            'sms_enabled': NotificationChannel.SMS,
            'push_enabled': NotificationChannel.PUSH,
            'in_app_enabled': NotificationChannel.IN_APP,
            'telegram_enabled': NotificationChannel.TELEGRAM,
        }
        result = []
        for key, channel in mapping.items():
            if payload.get(key) is True:
                result.append(channel)
        return result

    def _default_channels_for_type(self, notification_type: str) -> List[NotificationChannel]:
        """Default channels for a notification type."""
        defaults = {
            NotificationType.ORDER_CONFIRMATION.value: [NotificationChannel.EMAIL, NotificationChannel.SMS],
            NotificationType.ORDER_STATUS_UPDATE.value: [NotificationChannel.SMS, NotificationChannel.TELEGRAM],
            NotificationType.ORDER_UPDATE.value: [NotificationChannel.SMS, NotificationChannel.TELEGRAM],
            NotificationType.DELIVERY_UPDATE.value: [NotificationChannel.SMS, NotificationChannel.TELEGRAM],
            NotificationType.DELIVERY_REMINDER.value: [NotificationChannel.SMS, NotificationChannel.TELEGRAM],
            NotificationType.PAYMENT_CONFIRMATION.value: [NotificationChannel.EMAIL],
            NotificationType.SUBSCRIPTION_REMINDER.value: [NotificationChannel.EMAIL],
            NotificationType.SUBSCRIPTION_CREATED.value: [NotificationChannel.EMAIL, NotificationChannel.TELEGRAM],
            NotificationType.SUBSCRIPTION_RENEWAL.value: [NotificationChannel.EMAIL],
            NotificationType.SUBSCRIPTION_CANCELLED.value: [NotificationChannel.EMAIL],
            NotificationType.SUBSCRIPTION_CANCELLATION_SCHEDULED.value: [NotificationChannel.EMAIL],
            NotificationType.PROMOTIONAL.value: [NotificationChannel.EMAIL],
            NotificationType.SYSTEM.value: [NotificationChannel.EMAIL, NotificationChannel.SMS],
            NotificationType.SYSTEM_ALERT.value: [NotificationChannel.EMAIL, NotificationChannel.SMS],
            NotificationType.SECURITY.value: [NotificationChannel.EMAIL, NotificationChannel.SMS],
            NotificationType.EMAIL_VERIFICATION.value: [NotificationChannel.EMAIL],
            NotificationType.PASSWORD_RESET.value: [NotificationChannel.EMAIL, NotificationChannel.SMS],
            NotificationType.LOYALTY_REWARD.value: [NotificationChannel.EMAIL, NotificationChannel.TELEGRAM],
            NotificationType.REWARD_REDEEMED.value: [NotificationChannel.EMAIL],
        }
        return defaults.get(notification_type, [NotificationChannel.EMAIL])

    @staticmethod
    def _status_value(status: Any) -> str:
        """Normalize enum-or-string status values to plain strings."""
        return status.value if hasattr(status, 'value') else str(status)

    @staticmethod
    def _user_has_connected_telegram(user: User) -> bool:
        """Return True when the customer has an active linked Telegram bot."""
        return bool(getattr(user, 'telegram_id', None) and getattr(user, 'is_bot_active', False))

    def _should_force_delivery_status_telegram(self, status_value: str) -> bool:
        """Statuses that must include Telegram for connected users."""
        return status_value in {
            DeliveryStatus.IN_TRANSIT.value,
            DeliveryStatus.ARRIVED.value,
        }

    def _resolve_delivery_status_channels(self, user: User, status_value: str) -> List[NotificationChannel]:
        """Resolve channels for a delivery status event with Telegram override rules."""
        channels = list(self._get_user_preferred_channels(user.id, NotificationType.DELIVERY_UPDATE))
        deduped = {
            self._status_value(channel): channel
            for channel in channels
        }

        if self._should_force_delivery_status_telegram(status_value):
            if self._user_has_connected_telegram(user):
                deduped[NotificationChannel.TELEGRAM.value] = NotificationChannel.TELEGRAM
                logger.info(
                    "Forced Telegram delivery notification enabled: user_id=%s status=%s history_rule=connected_bot",
                    user.id,
                    status_value,
                )
            else:
                logger.info(
                    "Skipped forced Telegram delivery notification: user_id=%s status=%s reason=bot_not_connected",
                    user.id,
                    status_value,
                )
        else:
            if deduped.pop(NotificationChannel.TELEGRAM.value, None) is not None:
                logger.info(
                    "Removed Telegram from non-target delivery notification: user_id=%s status=%s",
                    user.id,
                    status_value,
                )

        return list(deduped.values())

    def _extract_delivery_status_code(self, template_data: Dict[str, Any]) -> Optional[str]:
        """Extract normalized delivery status from template payload."""
        if not template_data:
            return None

        for field in ('delivery_status_code', 'event_type', 'delivery_status', 'order_status'):
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

        normalized_value = normalized_value.replace('-', '_').replace(' ', '_')
        if normalized_value == 'intransit':
            normalized_value = DeliveryStatus.IN_TRANSIT.value

        return normalized_value

    def _get_localized_delivery_status_label(self, status_value: str, language: str) -> str:
        """Resolve a customer-facing localized label for delivery status notifications."""
        translation_key = f'notification.delivery_status.{status_value}'
        label = get_translation(translation_key, language)
        if label and label != translation_key:
            return label

        fallback_key = f'api.delivery.{status_value}'
        fallback_label = get_translation(fallback_key, language)
        if fallback_label and fallback_label != fallback_key:
            return fallback_label

        return status_value.replace('_', ' ').title()

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
            'tracking_code': delivery.tracking_number,
            'order_number': delivery.order.order_number if delivery.order else '',
            'delivery_status': self._get_localized_delivery_status_label(status_value, language),
            'delivery_status_code': status_value,
            'estimated_delivery': (
                delivery.estimated_delivery_time.isoformat()
                if delivery.estimated_delivery_time else None
            ),
            'event_type': status_value,
            'delivery_id': delivery.id,
            'order_id': delivery.order.id if delivery.order else None,
            'history_id': history.id,
        }
    
    # Private methods for different channels
    def _send_email_notification(self, user: User, notification_type: NotificationType,
                                template_data: Dict[str, Any], language: str) -> Dict[str, Any]:
        """Send email notification using Brevo API with file-based templates"""
        if not self.brevo_api_key:
            raise ConfigurationError(get_translation('error.configuration.email_not_configured'))

        if not user.email:
            return {'success': False, 'error': get_translation('error.validation.no_email_address')}

        # Get notification type string
        notification_type_str = notification_type.value if hasattr(notification_type, 'value') else str(notification_type)

        # Add user info to template data
        user_name = f"{user.first_name} {user.last_name}".strip() or user.email
        template_data_with_user = {
            'user_name': user_name,
            'user_email': user.email,
            **template_data
        }

        # Try file-based templates first
        email_template_service = get_email_template_service()
        rendered = email_template_service.render_notification_email(
            notification_type_str,
            language,
            template_data_with_user
        )

        if rendered:
            subject = rendered['subject']
            content = rendered['content']
            logger.info(f"Using file-based template for {notification_type_str} in {language}")
        else:
            # Fallback to database templates
            logger.info(f"File template not found, falling back to DB for {notification_type_str}")
            template = self._get_notification_template(
                notification_type, NotificationChannel.EMAIL, language
            )

            if not template:
                return {'success': False, 'error': get_translation('error.template_not_found')}

            # Get translated content (or fallback to default)
            template_subject = template.get_translated('subject', language) if hasattr(template, 'get_translated') else template.subject
            template_content = template.get_translated('content', language) if hasattr(template, 'get_translated') else template.content

            # Render template
            subject = self._render_template(template_subject, template_data_with_user, language)
            content = self._render_template(template_content, template_data_with_user, language)

        # Build Brevo API request
        url = 'https://api.brevo.com/v3/smtp/email'
        headers = {
            'accept': 'application/json',
            'api-key': self.brevo_api_key,
            'content-type': 'application/json'
        }
        payload = {
            'sender': {
                'name': self.default_sender_name,
                'email': self.default_sender_email
            },
            'to': [
                {
                    'email': user.email,
                    'name': user_name
                }
            ],
            'subject': subject,
            'htmlContent': content
        }

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            response.raise_for_status()

            result = response.json()
            return {
                'success': True,
                'message_id': result.get('messageId'),
                'status_code': response.status_code
            }
        except requests.exceptions.HTTPError as e:
            error_detail = ''
            try:
                error_detail = e.response.json() if e.response else str(e)
            except:
                error_detail = str(e)
            logger.error(f"Brevo API error: {error_detail}")
            return {'success': False, 'error': f"Email API error: {error_detail}"}
        except Exception as e:
            logger.error(f"Email sending failed: {e}")
            return {'success': False, 'error': str(e)}
    
    def _send_sms_notification(self, user: User, notification_type: NotificationType,
                              template_data: Dict[str, Any], language: str) -> Dict[str, Any]:
        """Send SMS notification using Eskiz"""
        logger.info(
            "_send_sms_notification started user=%s, notification_type=%s, template_data=%s, language=%s",
            user,
            notification_type,
            template_data,
            language,
        )
        if not self.eskiz_client:
            logger.error(f"_send_sms_notification error Eskiz SMS not configured")
            raise ConfigurationError(get_translation('error.configuration.sms_not_configured'))

        if not user.phone:
            logger.error(f"_send_sms_notification error User has no phone number, user.phone={user.phone}")
            return {'success': False, 'error': get_translation('error.validation.no_phone_number')}

        # Get template
        template = self._get_notification_template(
            notification_type, NotificationChannel.SMS, language
        )

        if not template:
            logger.error(f"_send_sms_notification error SMS template not found")
            return {'success': False, 'error': get_translation('error.template_not_found')}

        # Get translated content (or fallback to default)
        template_content = template.get_translated('content', language) if hasattr(template, 'get_translated') else template.content
        logger.info(f"_send_sms_notification template_content: {template_content}")
        # Render template
        content = self._render_template(template_content, template_data, language)
        logger.info(f"_send_sms_notification rendered content: {content}")

        try:
            # Clean phone number (Eskiz expects format like 998901234567)
            phone = user.phone.replace('+', '').replace(' ', '').replace('-', '')

            # Send SMS via Eskiz
            response = self.eskiz_client.send_sms(
                mobile_phone=phone,
                message=content,
                from_whom=self.eskiz_from
            )

            # Check if SMS was sent successfully
            # Eskiz returns Response object with status field
            if response and hasattr(response, 'status'):
                if response.status == 'success':
                    logger.info(f"SMS sent successfully to {phone}. Message ID: {getattr(response, 'id', 'N/A')}")
                    return {
                        'success': True,
                        'message_id': getattr(response, 'id', None),
                        'phone': phone,
                        'response': response
                    }
                else:
                    # SMS service returned an error status
                    error_msg = getattr(response, 'message', 'Unknown error from SMS provider')
                    logger.error(f"Eskiz SMS failed for {phone}: status={response.status}, message={error_msg}")
                    return {
                        'success': False,
                        'error': f"SMS provider returned status: {response.status}",
                        'details': error_msg
                    }
            else:
                # Unexpected response format
                logger.warning(f"Eskiz SMS returned unexpected response format: {response}")
                return {
                    'success': False,
                    'error': 'Unexpected response from SMS provider',
                    'response': response
                }

        except Exception as e:
            logger.error(f"Eskiz SMS error: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    def send_sms_to_phone(
        self,
        phone: str,
        notification_type: NotificationType,
        template_key: str,
        template_data: Dict[str, Any],
        language: str = 'uz'
    ) -> Dict[str, Any]:
        """
        Send SMS to a phone number without requiring a User object.

        Used for sending OTPs during registration when user doesn't exist yet.

        Args:
            phone: Phone number in normalized format (+998XXXXXXXXX)
            notification_type: Type of notification for template lookup
            template_key: Specific template key (e.g., 'sms.registration.otp')
            template_data: Data to render in template
            language: Language code for template

        Returns:
            Dict with success status and message_id or error
        """
        logger.info(f"send_sms_to_phone started: phone={phone[:4]}***{phone[-4:]}, template_key={template_key}")

        if not self.eskiz_client:
            logger.error("send_sms_to_phone error: Eskiz SMS not configured")
            return {'success': False, 'error': 'SMS service not configured'}

        if not phone:
            logger.error("send_sms_to_phone error: No phone number provided")
            return {'success': False, 'error': 'No phone number provided'}

        # Get template by key from translation system
        from business_app.utils.translations import get_translation

        # Try to get SMS content from translation system with the specific key
        # content = get_translation(template_key, language=language, default=None)
        content = None

        if not content:
            # Fallback templates for phone registration
            fallback_templates = {
                'sms.registration.otp': {
                    'uz': "Bluestream: Ro'yxatdan o'tish kodi: {otp_code}. Kod 3 daqiqa amal qiladi.",
                    'ru': "Bluestream: Код регистрации: {otp_code}. Код действителен 3 минуты.",
                    'en': "Bluestream: Your registration code: {otp_code}. Valid for 3 minutes."
                },
                'sms.verification.otp': {
                    'uz': "Aqua Element platformasida telefon raqamingizni tasdiqlash uchun kod: {otp_code}",
                    'ru': "Код для подтверждения вашего номера телефона на платформе Aqua Element: {otp_code}",
                    'en': "Code to verify your phone number on the Aqua Element platform: {otp_code}"
                },
                'sms.welcome': {
                    'uz': "Bluestream'ga xush kelibsiz, {first_name}! Buyurtma berish uchun ilovamizdan foydalaning.",
                    'ru': "Добро пожаловать в Bluestream, {first_name}! Используйте наше приложение для заказов.",
                    'en': "Welcome to Bluestream, {first_name}! Use our app to place orders."
                }
            }

            if template_key in fallback_templates:
                content = fallback_templates[template_key].get(language, fallback_templates[template_key].get('en'))
            else:
                logger.error(f"send_sms_to_phone error: No template found for key {template_key}")
                return {'success': False, 'error': f'SMS template not found: {template_key}'}

        # Render template with data
        try:
            rendered_content = self._render_template(content, template_data, language)
        except Exception as e:
            logger.error(f"Template rendering failed: {e}")
            rendered_content = content  # Use unrendered template as fallback

        logger.info(f"send_sms_to_phone rendered content: {rendered_content[:50]}...")

        try:
            # Clean phone number (Eskiz expects format like 998901234567)
            clean_phone = phone.replace('+', '').replace(' ', '').replace('-', '')

            # Send SMS via Eskiz
            response = self.eskiz_client.send_sms(
                mobile_phone=clean_phone,
                message=rendered_content,
                from_whom=self.eskiz_from
            )

            # Check if SMS was sent successfully
            if response and hasattr(response, 'status'):
                if response.status == 'success':
                    logger.info(f"SMS sent successfully to {clean_phone[:3]}***{clean_phone[-4:]}. Message ID: {getattr(response, 'id', 'N/A')}")
                    return {
                        'success': True,
                        'message_id': getattr(response, 'id', None),
                        'phone': clean_phone
                    }
                else:
                    error_msg = getattr(response, 'message', 'Unknown error from SMS provider')
                    logger.error(f"Eskiz SMS failed: status={response.status}, message={error_msg}")
                    return {
                        'success': False,
                        'error': f"SMS provider returned status: {response.status}",
                        'details': error_msg
                    }
            else:
                logger.warning(f"Eskiz SMS returned unexpected response format: {response}")
                return {
                    'success': False,
                    'error': 'Unexpected response from SMS provider'
                }

        except Exception as e:
            logger.error(f"Eskiz SMS error: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    def _send_telegram_notification(self, user: User, notification_type: NotificationType,
                                   template_data: Dict[str, Any], language: str) -> Dict[str, Any]:
        """Send Telegram notification"""
        if not self.telegram_bot_token:
            raise ConfigurationError(get_translation('error.configuration.telegram_not_configured'))

        notification_type_value = self._status_value(notification_type)
        if notification_type_value == NotificationType.DELIVERY_UPDATE.value:
            delivery_status_code = self._extract_delivery_status_code(template_data or {})
            if not delivery_status_code or not self._should_force_delivery_status_telegram(delivery_status_code):
                logger.info(
                    "Skipped Telegram delivery update: user_id=%s status=%s reason=status_not_allowed",
                    getattr(user, 'id', None),
                    delivery_status_code or 'unknown',
                )
                return {
                    'success': True,
                    'skipped': True,
                    'reason': 'delivery_status_not_allowed',
                }

        # Get user's Telegram ID (serves as chat ID for direct messages)
        telegram_chat_id = getattr(user, 'telegram_id', None)
        if not telegram_chat_id:
            return {'success': False, 'error': get_translation('error.validation.no_telegram_id')}
        
        # Get template
        template = self._get_notification_template(
            notification_type, NotificationChannel.TELEGRAM, language
        )

        if not template:
            return {'success': False, 'error': get_translation('error.template_not_found')}

        # Get translated content (or fallback to default)
        template_content = template.get_translated('content', language) if hasattr(template, 'get_translated') else template.content

        # Render template
        content = self._render_template(template_content, template_data, language)
        if notification_type_value == NotificationType.DELIVERY_UPDATE.value:
            content = self._strip_driver_info_from_delivery_message(content)
        
        # Send via Telegram Bot API
        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        payload = {
            'chat_id': telegram_chat_id,
            'text': content,
            'parse_mode': 'HTML'
        }
        
        try:
            response = requests.post(url, json=payload)
            response.raise_for_status()
            
            result = response.json()
            return {
                'success': result.get('ok', False),
                'message_id': result.get('result', {}).get('message_id')
            }
        except Exception as e:
            logger.warning(
                "Telegram notification failed: user_id=%s notification_type=%s error=%s",
                getattr(user, 'id', None),
                self._status_value(notification_type),
                e,
            )
            return {'success': False, 'error': str(e)}

    @staticmethod
    def _strip_driver_info_from_delivery_message(content: str) -> str:
        """Remove any driver-identifying lines from delivery Telegram messages."""
        if not content:
            return content

        cleaned_lines: List[str] = []
        for line in content.splitlines():
            stripped_line = line.strip()
            normalized_line = re.sub(r'<[^>]+>', '', stripped_line).lower()
            remove_line = False

            if '{driver_name}' in normalized_line or '{driver_phone}' in normalized_line:
                remove_line = True
            elif any(token in normalized_line for token in ('driver', 'haydovchi', 'водитель')):
                remove_line = True
            elif normalized_line.startswith('📞'):
                remove_line = True
            elif re.match(r'^(phone|telefon|телефон)\s*[:\-]', normalized_line):
                remove_line = True

            if not remove_line:
                cleaned_lines.append(line)

        cleaned_content = '\n'.join(cleaned_lines)
        cleaned_content = re.sub(r'\n{3,}', '\n\n', cleaned_content).strip()
        return cleaned_content
    
    def _send_push_notification(self, user: User, notification_type: NotificationType,
                               template_data: Dict[str, Any], language: str) -> Dict[str, Any]:
        """Send push notification"""
        # Push notification implementation would depend on your chosen service
        # (Firebase, OneSignal, etc.) - placeholder for now
        return {'success': False, 'error': get_translation('error.push_not_implemented')}
    
    def _get_user_preferred_channels(self, user_id: int, 
                                   notification_type: NotificationType) -> List[NotificationChannel]:
        """Get user's preferred notification channels for a type"""
        notification_type_val = notification_type.value if hasattr(notification_type, 'value') else str(notification_type)
        preferences = NotificationPreference.query.filter_by(
            user_id=user_id,
            notification_type=notification_type_val,
            is_enabled=True
        ).all()
        
        if preferences:
            return [NotificationChannel(pref.channel) for pref in preferences]
        
        # Default preferences if none set
        default_channels = {
            NotificationType.ORDER_CONFIRMATION: [NotificationChannel.EMAIL, NotificationChannel.SMS],
            NotificationType.ORDER_STATUS_UPDATE: [NotificationChannel.SMS, NotificationChannel.TELEGRAM],
            NotificationType.DELIVERY_UPDATE: [NotificationChannel.SMS, NotificationChannel.TELEGRAM],
            NotificationType.PAYMENT_CONFIRMATION: [NotificationChannel.EMAIL],
            NotificationType.SUBSCRIPTION_REMINDER: [NotificationChannel.EMAIL],
            NotificationType.PROMOTIONAL: [NotificationChannel.EMAIL],
            NotificationType.SYSTEM: [NotificationChannel.EMAIL, NotificationChannel.SMS],
            NotificationType.LOYALTY_REWARD: [NotificationChannel.EMAIL, NotificationChannel.TELEGRAM],
            NotificationType.REWARD_REDEEMED: [NotificationChannel.EMAIL]
        }
        
        return default_channels.get(notification_type, [NotificationChannel.EMAIL])
    
    def _get_notification_template(self, notification_type: NotificationType,
                                 channel: NotificationChannel, language: str) -> Optional[NotificationTemplate]:
        """Get notification template"""
        # NotificationTemplate uses TranslatableMixin, so we don't filter by language
        # Instead, we get the template and then retrieve translated content
        notification_type_val = notification_type.value if hasattr(notification_type, 'value') else str(notification_type)
        channel_val = channel.value if hasattr(channel, 'value') else str(channel)
        
        template = NotificationTemplate.query.filter_by(
            notification_type=notification_type_val,
            channel=channel_val,
            is_active=True
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

        translations = template_config.get('translations', {})
        default_translation = translations.get('uz', {})

        def _get_translated(field_name: str, language: str):
            language_translation = translations.get(language, {})
            if field_name in language_translation:
                return language_translation[field_name]
            if field_name in default_translation:
                return default_translation[field_name]
            return None

        return SimpleNamespace(
            name=template_config.get('name'),
            notification_type=notification_type,
            channel=channel,
            subject=default_translation.get('subject'),
            content=default_translation.get('content', ''),
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
            
            # Replace data placeholders
            for key, value in data.items():
                placeholder = f"{{{key}}}"
                rendered = rendered.replace(placeholder, str(value))
            
            # Replace translation placeholders
            import re
            translation_pattern = r'\{\{([^}]+)\}\}'
            matches = re.findall(translation_pattern, rendered)
            
            for match in matches:
                translation = get_translation(match, language)
                rendered = rendered.replace(f"{{{{{match}}}}}", translation)
            
            return rendered
            
        except Exception as e:
            logger.error(f"Template rendering failed: {e}")
            return template
    
    def _create_notification_record(self, user_id: int, notification_type: NotificationType,
                                  channels: List[NotificationChannel], template_data: Dict[str, Any],
                                  results: Dict[str, Any]):
        """Create notification record in database"""
        try:
            user = User.query.get(user_id)
            payload = template_data or {}
            notification_type_value = (
                notification_type.value if hasattr(notification_type, 'value') else str(notification_type)
            )

            # Create a notification record for each channel
            for channel in channels:
                channel_value = channel.value if hasattr(channel, 'value') else str(channel)
                result = results.get(channel_value, {})
                if result.get('skipped'):
                    logger.info(
                        "Skipping notification audit row for skipped channel send: user_id=%s channel=%s notification_type=%s reason=%s",
                        user_id,
                        channel_value,
                        notification_type_value,
                        result.get('reason'),
                    )
                    continue

                # Extract message from template_data or use a default
                message = payload.get('message', payload.get('otp_code', 'Notification sent'))
                title = payload.get('title', notification_type_value.replace('_', ' ').title())

                notification = Notification(
                    user_id=user_id,
                    notification_type=notification_type_value,
                    channel=channel_value,
                    title=title,
                    message=str(message),
                    is_sent=result.get('success', False),
                    sent_at=datetime.now(timezone.utc) if result.get('success') else None,
                    delivery_status='sent' if result.get('success') else 'failed',
                    failure_reason=result.get('error') if not result.get('success') else None,
                    recipient_phone=(
                        getattr(user, 'phone', None)
                        if channel_value == NotificationChannel.SMS.value else None
                    ),
                    recipient_email=(
                        getattr(user, 'email', None)
                        if channel_value == NotificationChannel.EMAIL.value else None
                    ),
                    recipient_telegram_id=(
                        getattr(user, 'telegram_id', None)
                        if channel_value == NotificationChannel.TELEGRAM.value else None
                    ),
                    order_id=payload.get('order_id'),
                    delivery_id=payload.get('delivery_id'),
                    extra_data=payload,
                )

                db.session.add(notification)

            db.session.commit()

        except Exception as e:
            logger.error(f"Failed to create notification record: {e}")
            db.session.rollback()


# Default notification templates
# Structure: (notification_type, channel) -> {
#     'name': template name,
#     'translations': {lang: {'subject': ..., 'content': ...}}
# }
# The 'uz' language content is stored as default in the model fields
# Other language translations are stored via TranslatableMixin
DEFAULT_TEMPLATES = {
    # Order confirmation - Email
    ('order_confirmation', 'email'): {
        'name': 'order_confirmation_email',
        'translations': {
            'uz': {
                'subject': 'Buyurtma tasdiqlandi - {{company_name}}',
                'content': '''<h2>Buyurtma tasdiqlandi!</h2>
<p>#{order_number} raqamli buyurtmangiz uchun rahmat.</p>
<p><strong>Buyurtma tafsilotlari:</strong></p>
<p><strong>Jami: {order_total} so'm</strong></p>
<p><strong>Yetkazib berish manzili:</strong> {delivery_address}</p>
<p>Buyurtmangiz tayyorlanayotganda va yetkazib berilayotganda sizga xabar beramiz.</p>'''
            },
            'en': {
                'subject': 'Order Confirmation - {{company_name}}',
                'content': '''<h2>Order Confirmed!</h2>
<p>Thank you for your order #{order_number}.</p>
<p><strong>Order Details:</strong></p>
<p><strong>Total: {order_total} UZS</strong></p>
<p><strong>Delivery Address:</strong> {delivery_address}</p>
<p>We'll notify you when your order is being prepared and out for delivery.</p>'''
            },
            'ru': {
                'subject': 'Подтверждение заказа - {{company_name}}',
                'content': '''<h2>Заказ подтвержден!</h2>
<p>Спасибо за ваш заказ #{order_number}.</p>
<p><strong>Детали заказа:</strong></p>
<p><strong>Итого: {order_total} сум</strong></p>
<p><strong>Адрес доставки:</strong> {delivery_address}</p>
<p>Мы уведомим вас, когда ваш заказ будет готовиться и доставляться.</p>'''
            }
        }
    },

    # Order confirmation - SMS
    ('order_confirmation', 'sms'): {
        'name': 'order_confirmation_sms',
        'translations': {
            'uz': {
                'content': 'Buyurtma #{order_number} tasdiqlandi! Jami: {order_total} so\'m. Yetkazib berish haqida xabar beramiz. {{company_name}}ni tanlaganingiz uchun rahmat!'
            },
            'en': {
                'content': 'Order #{order_number} confirmed! Total: {order_total} UZS. We\'ll update you on delivery progress. Thank you for choosing {{company_name}}!'
            },
            'ru': {
                'content': 'Заказ #{order_number} подтвержден! Сумма: {order_total} сум. Уведомим о доставке. Спасибо за выбор {{company_name}}!'
            }
        }
    },

    # Delivery update - SMS
    ('delivery_update', 'sms'): {
        'name': 'delivery_update_sms',
        'translations': {
            'uz': {
                'content': 'Yetkazib berish: #{order_number} buyurtmangiz {delivery_status}. Kuzatish: {tracking_code}. Savollar? {company_phone} ga qo\'ng\'iroq qiling'
            },
            'en': {
                'content': 'Delivery Update: Your order #{order_number} is {delivery_status}. Track: {tracking_code}. Questions? Call {company_phone}'
            },
            'ru': {
                'content': 'Обновление доставки: Ваш заказ #{order_number} {delivery_status}. Отслеживание: {tracking_code}. Вопросы? {company_phone}'
            }
        }
    },

    # Delivery update - Telegram
    ('delivery_update', 'telegram'): {
        'name': 'delivery_update_telegram',
        'translations': {
            'uz': {
                'content': '''🚚 <b>Yetkazib berish yangiligi</b>

Buyurtma: #{order_number}
Holati: {delivery_status}
Kuzatish: {tracking_code}
'''
            },
            'en': {
                'content': '''🚚 <b>Delivery Update</b>

Order: #{order_number}
Status: {delivery_status}
Tracking: {tracking_code}
'''
            },
            'ru': {
                'content': '''🚚 <b>Обновление доставки</b>

Заказ: #{order_number}
Статус: {delivery_status}
Отслеживание: {tracking_code}
'''
            }
        }
    },

    # Payment confirmation - Email
    ('payment_confirmation', 'email'): {
        'name': 'payment_confirmation_email',
        'translations': {
            'uz': {
                'subject': 'To\'lov tasdiqlandi - {{company_name}}',
                'content': '''<h2>To'lov qabul qilindi</h2>
<p>#{order_number} raqamli buyurtmangiz uchun to'lovni muvaffaqiyatli qabul qildik.</p>
<p><strong>To'lov tafsilotlari:</strong></p>
<ul>
    <li>Summa: {payment_amount} so'm</li>
    <li>Usul: {payment_method}</li>
    <li>Havola: {payment_reference}</li>
</ul>
<p>Buyurtmangiz qayta ishlanmoqda.</p>'''
            },
            'en': {
                'subject': 'Payment Confirmation - {{company_name}}',
                'content': '''<h2>Payment Received</h2>
<p>We have successfully received your payment for order #{order_number}.</p>
<p><strong>Payment Details:</strong></p>
<ul>
    <li>Amount: {payment_amount} UZS</li>
    <li>Method: {payment_method}</li>
    <li>Reference: {payment_reference}</li>
</ul>
<p>Your order is now being processed.</p>'''
            },
            'ru': {
                'subject': 'Подтверждение оплаты - {{company_name}}',
                'content': '''<h2>Оплата получена</h2>
<p>Мы успешно получили вашу оплату за заказ #{order_number}.</p>
<p><strong>Детали оплаты:</strong></p>
<ul>
    <li>Сумма: {payment_amount} сум</li>
    <li>Способ: {payment_method}</li>
    <li>Ссылка: {payment_reference}</li>
</ul>
<p>Ваш заказ обрабатывается.</p>'''
            }
        }
    },

    # Payment confirmation - Telegram
    ('payment_confirmation', 'telegram'): {
        'name': 'payment_confirmation_telegram',
        'translations': {
            'uz': {
                'content': '''✅ <b>To'lov tasdiqlandi!</b>

Buyurtma: #{order_number}
Summa: {payment_amount} so'm
Usul: {payment_method}

Buyurtmangiz qayta ishlanmoqda. Yetkazib berishga tayyor bo'lganda xabar beramiz.

Xaridingiz uchun rahmat!'''
            },
            'en': {
                'content': '''✅ <b>Payment Confirmed!</b>

Order: #{order_number}
Amount: {payment_amount} UZS
Method: {payment_method}

Your order is now being processed. We'll notify you when it's ready for delivery.

Thank you for your purchase!'''
            },
            'ru': {
                'content': '''✅ <b>Оплата подтверждена!</b>

Заказ: #{order_number}
Сумма: {payment_amount} сум
Способ: {payment_method}

Ваш заказ обрабатывается. Мы уведомим вас, когда он будет готов к доставке.

Спасибо за покупку!'''
            }
        }
    },

    # Loyalty reward - Email
    ('loyalty_reward', 'email'): {
        'name': 'loyalty_reward_email',
        'translations': {
            'uz': {
                'subject': 'Sodiqlik ballari yangiligi - {{company_name}}',
                'content': '''<h2>Sodiqlik ballari yangiligi</h2>
<p>Tabriklaymiz! Siz {points} sodiqlik ballini qo'lga kiritdingiz.</p>
<p>Joriy balansingiz va mavjud mukofotlarni ko'rish uchun hisobingizga tashrif buyuring.</p>'''
            },
            'en': {
                'subject': 'Loyalty Points Update - {{company_name}}',
                'content': '''<h2>Loyalty Points Update</h2>
<p>Congratulations! You've earned {points} loyalty points.</p>
<p>Visit your account to see your current balance and available rewards.</p>'''
            },
            'ru': {
                'subject': 'Обновление баллов лояльности - {{company_name}}',
                'content': '''<h2>Обновление баллов лояльности</h2>
<p>Поздравляем! Вы заработали {points} баллов лояльности.</p>
<p>Посетите свой аккаунт, чтобы увидеть текущий баланс и доступные награды.</p>'''
            }
        }
    }
}


def seed_notification_templates():
    """
    Seed default notification templates with multilingual support.
    Uses TranslatableMixin for translations storage.
    """
    for (notification_type, channel), template_config in DEFAULT_TEMPLATES.items():
        # Check if template already exists
        existing = NotificationTemplate.query.filter_by(
            notification_type=notification_type,
            channel=channel
        ).first()

        translations = template_config.get('translations', {})
        # Use Uzbek as the default/base language
        uz_translation = translations.get('uz', {})

        if not existing:
            # Create new template with Uzbek as default content
            template = NotificationTemplate(
                name=template_config['name'],
                notification_type=notification_type,
                channel=channel,
                subject=uz_translation.get('subject', ''),
                content=uz_translation.get('content', ''),
                is_active=True
            )
            db.session.add(template)
            db.session.flush()  # Get the ID for setting translations
        else:
            template = existing
            # Update base content if needed
            template.subject = uz_translation.get('subject', template.subject or '')
            template.content = uz_translation.get('content', template.content or '')

        # Set translations for all languages using TranslatableMixin
        for lang, lang_translations in translations.items():
            if 'subject' in lang_translations:
                template.set_translated('subject', lang_translations['subject'], lang)
            if 'content' in lang_translations:
                template.set_translated('content', lang_translations['content'], lang)

    db.session.commit()
    logger.info(f"Seeded {len(DEFAULT_TEMPLATES)} notification templates with translations")
