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
from business_app.models.order import Order
from business_app.models.delivery import Delivery
from business_app.models.payment import Payment
from business_app.models.subscription import Subscription
from business_app.services.notification_service import NotificationService
from business_app.utils.constants import NotificationType, NotificationChannel
from business_app import db

logger = get_task_logger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_loyalty_notification_task(self, user_id: int, event_type: str, data: Dict[str, Any]):
    """Send loyalty program notification"""
    try:
        logger.info(f"Sending loyalty notification for user {user_id}, event: {event_type}")
        
        notification_service = NotificationService()
        result = notification_service.send_loyalty_notification(user_id, event_type, data)
        
        logger.info(f"Loyalty notification sent successfully for user {user_id}")
        return result
        
    except Exception as exc:
        logger.error(f"Failed to send loyalty notification: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def notify_driver_assignment_task(self, delivery_id: int):
    """Notify driver about new delivery assignment"""
    try:
        logger.info(f"Notifying driver about delivery assignment {delivery_id}")
        
        delivery = Delivery.query.get(delivery_id)
        if not delivery or not delivery.driver:
            logger.error(f"Delivery {delivery_id} not found or no driver assigned")
            return {'success': False, 'error': 'Delivery or driver not found'}
        
        notification_service = NotificationService()
        
        template_data = {
            'delivery_id': delivery.id,
            'tracking_code': delivery.tracking_code,
            'order_number': delivery.order.order_number,
            'customer_name': f"{delivery.order.user.first_name} {delivery.order.user.last_name}",
            'delivery_address': delivery.delivery_address_street,
            'customer_phone': delivery.order.user.phone,
            'estimated_delivery_time': delivery.estimated_delivery_time.isoformat() if delivery.estimated_delivery_time else None
        }
        
        result = notification_service.send_notification(
            delivery.driver_id,
            NotificationType.DELIVERY_UPDATE,
            [NotificationChannel.SMS, NotificationChannel.TELEGRAM],
            template_data
        )
        
        logger.info(f"Driver notification sent successfully for delivery {delivery_id}")
        return result
        
    except Exception as exc:
        logger.error(f"Failed to notify driver: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def notify_delivery_cancellation_task(self, delivery_id: int):
    """Notify about delivery cancellation"""
    try:
        logger.info(f"Sending delivery cancellation notification for delivery {delivery_id}")
        
        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            logger.error(f"Delivery {delivery_id} not found")
            return {'success': False, 'error': 'Delivery not found'}
        
        notification_service = NotificationService()
        
        template_data = {
            'order_number': delivery.order.order_number,
            'tracking_code': delivery.tracking_code,
            'cancellation_reason': delivery.cancellation_reason or 'No reason provided',
            'customer_service_phone': current_app.config['COMPANY_PHONE']
        }
        
        # Notify customer
        customer_result = notification_service.send_notification(
            delivery.order.user_id,
            NotificationType.DELIVERY_UPDATE,
            None,
            template_data
        )
        
        # Notify driver if assigned
        driver_result = {}
        if delivery.driver_id:
            driver_result = notification_service.send_notification(
                delivery.driver_id,
                NotificationType.DELIVERY_UPDATE,
                [NotificationChannel.SMS, NotificationChannel.TELEGRAM],
                template_data
            )
        
        logger.info(f"Delivery cancellation notifications sent for delivery {delivery_id}")
        return {
            'customer_notification': customer_result,
            'driver_notification': driver_result
        }
        
    except Exception as exc:
        logger.error(f"Failed to send delivery cancellation notification: {exc}")
        raise self.retry(exc=exc)


@shared_task
def send_bulk_promotional_notification(user_ids: List[int], campaign_data: Dict[str, Any]):
    """Send bulk promotional notifications"""
    try:
        logger.info(f"Sending bulk promotional notification to {len(user_ids)} users")
        
        notification_service = NotificationService()
        
        results = {
            'total_users': len(user_ids),
            'successful': 0,
            'failed': 0,
            'errors': []
        }
        
        for user_id in user_ids:
            try:
                result = notification_service.send_notification(
                    user_id,
                    NotificationType.PROMOTIONAL,
                    None,
                    campaign_data
                )
                
                if any(r.get('success') for r in result.values()):
                    results['successful'] += 1
                else:
                    results['failed'] += 1
                    
            except Exception as e:
                results['failed'] += 1
                results['errors'].append({
                    'user_id': user_id,
                    'error': str(e)
                })
        
        logger.info(f"Bulk promotional notification completed: {results['successful']} successful, {results['failed']} failed")
        return results
        
    except Exception as e:
        logger.error(f"Bulk promotional notification failed: {e}")
        return {'error': str(e)}


@shared_task
def send_daily_delivery_reminders():
    """Send daily delivery reminders to customers"""
    try:
        logger.info("Sending daily delivery reminders")
        
        # Get deliveries scheduled for today that haven't been delivered
        today = datetime.now(timezone.utc).date()
        tomorrow = today + timedelta(days=1)
        
        deliveries = Delivery.query.filter(
            Delivery.estimated_delivery_time >= today,
            Delivery.estimated_delivery_time < tomorrow,
            Delivery.status.in_(['pending', 'assigned', 'picked_up', 'in_transit'])
        ).all()
        
        notification_service = NotificationService()
        sent_count = 0
        
        for delivery in deliveries:
            try:
                template_data = {
                    'order_number': delivery.order.order_number,
                    'tracking_code': delivery.tracking_code,
                    'estimated_delivery_time': delivery.estimated_delivery_time.strftime('%H:%M'),
                    'delivery_address': delivery.delivery_address_street
                }
                
                notification_service.send_notification(
                    delivery.order.user_id,
                    NotificationType.DELIVERY_UPDATE,
                    [NotificationChannel.SMS],
                    template_data
                )
                
                sent_count += 1
                
            except Exception as e:
                logger.error(f"Failed to send delivery reminder for delivery {delivery.id}: {e}")
                continue
        
        logger.info(f"Sent {sent_count} delivery reminders")
        return {'sent_count': sent_count}
        
    except Exception as e:
        logger.error(f"Failed to send daily delivery reminders: {e}")
        return {'error': str(e)}


@shared_task
def cleanup_old_notifications():
    """Clean up old notification records"""
    try:
        logger.info("Cleaning up old notifications")
        
        # Delete notifications older than 6 months
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=180)
        
        deleted_count = Notification.query.filter(
            Notification.created_at < cutoff_date
        ).delete()
        
        db.session.commit()
        
        logger.info(f"Cleaned up {deleted_count} old notification records")
        return {'deleted_count': deleted_count}
        
    except Exception as e:
        logger.error(f"Failed to clean up old notifications: {e}")
        db.session.rollback()
        return {'error': str(e)}


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_emergency_notification(self, user_ids: List[int], message: str, channels: List[str] = None):
    """Send emergency notification to specified users"""
    try:
        logger.info(f"Sending emergency notification to {len(user_ids)} users")
        
        if channels is None:
            channels = ['sms', 'email', 'telegram']
        
        notification_channels = [NotificationChannel(ch) for ch in channels]
        notification_service = NotificationService()
        
        results = []
        
        for user_id in user_ids:
            try:
                result = notification_service.send_notification(
                    user_id,
                    NotificationType.SYSTEM_ALERT,
                    notification_channels,
                    {'emergency_message': message, 'priority': 'urgent'}
                )
                results.append({'user_id': user_id, 'result': result})
                
            except Exception as e:
                logger.error(f"Failed to send emergency notification to user {user_id}: {e}")
                results.append({'user_id': user_id, 'error': str(e)})
        
        logger.info(f"Emergency notification completed for {len(user_ids)} users")
        return results
        
    except Exception as exc:
        logger.error(f"Emergency notification failed: {exc}")
        raise self.retry(exc=exc)


@shared_task
def process_notification_analytics():
    """Process notification analytics and generate reports"""
    try:
        logger.info("Processing notification analytics")
        
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=1)
        
        # Get notification metrics
        total_notifications = Notification.query.filter(
            Notification.created_at.between(start_date, end_date)
        ).count()
        
        # Success rate by channel
        from sqlalchemy import func, case
        channel_stats = db.session.query(
            Notification.channels,
            func.count(Notification.id),
            func.avg(
                case(
                    [(Notification.status == 'sent', 1)],
                    else_=0
                ).label('success_rate')
            )
        ).filter(
            Notification.created_at.between(start_date, end_date)
        ).group_by(Notification.channels).all()
        
        # Notification type breakdown
        type_breakdown = db.session.query(
            Notification.notification_type,
            func.count(Notification.id)
        ).filter(
            Notification.created_at.between(start_date, end_date)
        ).group_by(Notification.notification_type).all()
        
        analytics_data = {
            'date': start_date.date().isoformat(),
            'total_notifications': total_notifications,
            'channel_stats': [
                {
                    'channels': channels,
                    'count': count,
                    'success_rate': float(success_rate or 0)
                }
                for channels, count, success_rate in channel_stats
            ],
            'type_breakdown': [
                {
                    'type': notification_type,
                    'count': count
                }
                for notification_type, count in type_breakdown
            ]
        }
        
        # Store analytics data
        from business_app.services.analytics_service import AnalyticsService
        analytics_service = AnalyticsService()
        analytics_service.store_notification_analytics(analytics_data)
        
        logger.info("Notification analytics processed successfully")
        return analytics_data
        
    except Exception as e:
        logger.error(f"Failed to process notification analytics: {e}")
        return {'error': str(e)}


@shared_task(bind=True, max_retries=2)
def send_scheduled_notification(self, user_id: int, notification_type: str, 
                               template_data: Dict[str, Any], scheduled_time: str):
    """Send scheduled notification at specified time"""
    try:
        # Parse scheduled time
        scheduled_datetime = datetime.fromisoformat(scheduled_time)
        
        # Check if it's time to send
        if datetime.now(timezone.utc) < scheduled_datetime:
            # Reschedule for later
            eta = scheduled_datetime
            return self.retry(countdown=(eta - datetime.now(timezone.utc)).total_seconds())
        
        logger.info(f"Sending scheduled notification for user {user_id}")
        
        notification_service = NotificationService()
        notification_type_enum = NotificationType(notification_type)
        
        result = notification_service.send_notification(
            user_id,
            notification_type_enum,
            None,
            template_data
        )
        
        logger.info(f"Scheduled notification sent successfully for user {user_id}")
        return result
        
    except Exception as exc:
        logger.error(f"Failed to send scheduled notification: {exc}")
        raise self.retry(exc=exc)


@shared_task
def update_notification_preferences_bulk(user_preferences: List[Dict[str, Any]]):
    """Update notification preferences for multiple users"""
    try:
        logger.info(f"Updating notification preferences for {len(user_preferences)} users")
        
        notification_service = NotificationService()
        updated_count = 0
        
        for user_pref in user_preferences:
            try:
                user_id = user_pref['user_id']
                preferences = user_pref['preferences']
                
                success = notification_service.update_notification_preferences(user_id, preferences)
                if success:
                    updated_count += 1
                    
            except Exception as e:
                logger.error(f"Failed to update preferences for user {user_pref.get('user_id')}: {e}")
                continue
        
        logger.info(f"Updated notification preferences for {updated_count} users")
        return {'updated_count': updated_count}
        
    except Exception as e:
        logger.error(f"Bulk notification preferences update failed: {e}")
        return {'error': str(e)}


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
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


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_delivery_update_task(self, delivery_id: int, status: str):
    """Send delivery status update notification"""
    try:
        logger.info(f"Sending delivery update for delivery {delivery_id}, status: {status}")
        
        notification_service = NotificationService()
        result = notification_service.send_delivery_notification(delivery_id, status)
        
        logger.info(f"Delivery update sent successfully for delivery {delivery_id}")
        return result
        
    except Exception as exc:
        logger.error(f"Failed to send delivery update: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_payment_confirmation_task(self, payment_id: int):
    """Send payment confirmation notification"""
    try:
        logger.info(f"Sending payment confirmation for payment {payment_id}")
        
        notification_service = NotificationService()
        result = notification_service.send_payment_notification(payment_id)
        
        logger.info(f"Payment confirmation sent successfully for payment {payment_id}")
        return result
        
    except Exception as exc:
        logger.error(f"Failed to send payment confirmation: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_verification_email_task(self, user_id: int, verification_token: str):
    """Send email verification notification"""
    try:
        logger.info(f"Sending email verification for user {user_id}")
        
        user = User.query.get(user_id)
        if not user:
            logger.error(f"User {user_id} not found")
            return {'success': False, 'error': 'User not found'}
        
        notification_service = NotificationService()
        
        template_data = {
            'user_name': f"{user.first_name} {user.last_name}",
            'verification_token': verification_token,
            'verification_code': verification_token,  # Alias for template compatibility
            'verification_url': f"{current_app.config['COMPANY_WEBSITE']}/verify-email?token={verification_token}",
            'company_name': current_app.config['COMPANY_NAME']
        }
        
        result = notification_service.send_notification(
            user_id,
            NotificationType.EMAIL_VERIFICATION,
            [NotificationChannel.EMAIL],
            template_data
        )

        logger.info(f"Email verification sent successfully for user {user_id}")
        return result
        
    except Exception as exc:
        logger.error(f"Failed to send email verification: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
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
            return {'success': False, 'error': 'User not found'}
        
        # Use provided phone or user's phone
        target_phone = phone_number or user.phone
        if not target_phone:
            logger.error(f"No phone number available for user {user_id}")
            return {'success': False, 'error': 'No phone number available'}
        
        notification_service = NotificationService()
        
        template_data = {
            'user_name': user.first_name,
            'otp_code': otp_code,
            'phone_number': target_phone,
            'company_name': current_app.config['COMPANY_NAME']
        }
        
        result = notification_service.send_notification(
            user_id,
            NotificationType.SYSTEM,
            [NotificationChannel.SMS],
            template_data
        )
        
        logger.info(f"SMS verification sent successfully for user {user_id} to {target_phone}")
        return result
        
    except Exception as exc:
        logger.error(f"Failed to send SMS verification: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_password_reset_email_task(self, user_id: int, reset_token: str):
    """Send password reset email"""
    try:
        logger.info(f"Sending password reset email for user {user_id}")
        
        user = User.query.get(user_id)
        if not user:
            logger.error(f"User {user_id} not found")
            return {'success': False, 'error': 'User not found'}
        
        notification_service = NotificationService()
        
        template_data = {
            'user_name': f"{user.first_name} {user.last_name}",
            'reset_token': reset_token,
            'reset_url': f"{current_app.config['COMPANY_WEBSITE']}/reset-password/{reset_token}",
            'company_name': current_app.config['COMPANY_NAME'],
            'expiry_hours': 24
        }
        
        result = notification_service.send_notification(
            user_id,
            NotificationType.PASSWORD_RESET,
            [NotificationChannel.EMAIL],
            template_data
        )

        logger.info(f"Password reset email sent successfully for user {user_id}")
        return result
        
    except Exception as exc:
        logger.error(f"Failed to send password reset email: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_subscription_confirmation_task(self, subscription_id: int):
    """Send subscription confirmation notification"""
    try:
        logger.info(f"Sending subscription confirmation for subscription {subscription_id}")
        
        notification_service = NotificationService()
        result = notification_service.send_subscription_notification(subscription_id, 'confirmed')
        
        logger.info(f"Subscription confirmation sent successfully for subscription {subscription_id}")
        return result
        
    except Exception as exc:
        logger.error(f"Failed to send subscription confirmation: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
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


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_bulk_notification_task(self, notification_type: str, recipient_ids: List[int], 
                                template_data: Dict[str, Any] = None, channel: str = 'email'):
    """Send bulk notifications to multiple recipients"""
    try:
        logger.info(f"Starting bulk notification send: {notification_type} to {len(recipient_ids)} recipients")
        
        template_data = template_data or {}
        
        notification_service = NotificationService()
        results = []
        
        for recipient_id in recipient_ids:
            try:
                result = notification_service.send_notification(
                    recipient_id,
                    notification_type,
                    channel=channel,
                    template_data=template_data
                )
                results.append({
                    'recipient_id': recipient_id,
                    'success': result.get('success', False),
                    'notification_id': result.get('notification_id')
                })
            except Exception as e:
                logger.error(f"Failed to send notification to recipient {recipient_id}: {e}")
                results.append({
                    'recipient_id': recipient_id,
                    'success': False,
                    'error': str(e)
                })
        
        successful_sends = sum(1 for r in results if r['success'])
        failed_sends = len(results) - successful_sends
        
        logger.info(f"Bulk notification completed: {successful_sends} successful, {failed_sends} failed")
        
        return {
            'total_recipients': len(recipient_ids),
            'successful_sends': successful_sends,
            'failed_sends': failed_sends,
            'results': results
        }
        
    except Exception as exc:
        logger.error(f"Bulk notification task failed: {exc}")
        raise self.retry(exc=exc)
