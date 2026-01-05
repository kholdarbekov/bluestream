"""
Notification service for the Water Business Platform
Handles SMS, Email, Telegram, and Push notifications
"""
import json
import logging
from celery.utils.log import get_task_logger
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from flask import current_app
import requests
from eskiz_sms import EskizSMS

from business_app.models.notification import Notification, NotificationTemplate, NotificationPreference
from business_app.models.user import User
from business_app.models.order import Order
from business_app.models.delivery import Delivery
from business_app.models.payment import Payment
from business_app.models.subscription import Subscription
from business_app.utils.exceptions import NotificationError, ConfigurationError
from business_app.utils.constants import NotificationType, NotificationChannel
from business_app.utils.translations import get_translation
from business_app.services.email_template_service import get_email_template_service
from business_app import db

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
        
        return self.send_notification(order.user_id, notification_type, None, template_data)
    
    def send_delivery_notification(self, delivery_id: int, event_type: str) -> Dict[str, Any]:
        """Send delivery-related notification"""
        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            raise NotificationError(get_translation('error.not_found'))
        
        template_data = {
            'tracking_code': delivery.tracking_code,
            'order_number': delivery.order.order_number,
            'delivery_status': delivery.status.value,
            'estimated_delivery': delivery.estimated_delivery_time.isoformat() if delivery.estimated_delivery_time else None,
            'driver_name': f"{delivery.driver.first_name} {delivery.driver.last_name}" if delivery.driver else None,
            'driver_phone': delivery.driver.phone if delivery.driver else None
        }
        
        return self.send_notification(
            delivery.order.user_id,
            NotificationType.DELIVERY_UPDATE,
            None,
            template_data
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
                                 data: Dict[str, Any]) -> Dict[str, Any]:
        """Send loyalty program notification"""
        template_data = {
            'event_type': event_type,
            **data
        }
        
        return self.send_notification(
            user_id,
            NotificationType.LOYALTY_REWARD,
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
        logger.info(f"_send_sms_notification started {user=}, {notification_type=}, {template_data=}, {language=}")
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
    
    def _send_telegram_notification(self, user: User, notification_type: NotificationType,
                                   template_data: Dict[str, Any], language: str) -> Dict[str, Any]:
        """Send Telegram notification"""
        if not self.telegram_bot_token:
            raise ConfigurationError(get_translation('error.configuration.telegram_not_configured'))

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
            return {'success': False, 'error': str(e)}
    
    def _send_push_notification(self, user: User, notification_type: NotificationType,
                               template_data: Dict[str, Any], language: str) -> Dict[str, Any]:
        """Send push notification"""
        # Push notification implementation would depend on your chosen service
        # (Firebase, OneSignal, etc.) - placeholder for now
        return {'success': False, 'error': get_translation('error.push_not_implemented')}
    
    def _get_user_preferred_channels(self, user_id: int, 
                                   notification_type: NotificationType) -> List[NotificationChannel]:
        """Get user's preferred notification channels for a type"""
        preferences = NotificationPreference.query.filter_by(
            user_id=user_id,
            notification_type=notification_type.value,
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
            NotificationType.LOYALTY_REWARD: [NotificationChannel.EMAIL, NotificationChannel.TELEGRAM]
        }
        
        return default_channels.get(notification_type, [NotificationChannel.EMAIL])
    
    def _get_notification_template(self, notification_type: NotificationType,
                                 channel: NotificationChannel, language: str) -> Optional[NotificationTemplate]:
        """Get notification template"""
        # NotificationTemplate uses TranslatableMixin, so we don't filter by language
        # Instead, we get the template and then retrieve translated content
        template = NotificationTemplate.query.filter_by(
            notification_type=notification_type.value,
            channel=channel.value,
            is_active=True
        ).first()

        return template
    
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
            # Create a notification record for each channel
            for channel in channels:
                result = results.get(channel.value, {})

                # Extract message from template_data or use a default
                message = template_data.get('message', template_data.get('otp_code', 'Notification sent'))
                title = template_data.get('title', notification_type.value.replace('_', ' ').title())

                notification = Notification(
                    user_id=user_id,
                    notification_type=notification_type.value,
                    channel=channel.value,
                    title=title,
                    message=str(message),
                    is_sent=result.get('success', False),
                    sent_at=datetime.now(timezone.utc) if result.get('success') else None,
                    delivery_status='sent' if result.get('success') else 'failed',
                    failure_reason=result.get('error') if not result.get('success') else None,
                    extra_data=template_data
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

Haydovchi: {driver_name}
Telefon: {driver_phone}'''
            },
            'en': {
                'content': '''🚚 <b>Delivery Update</b>

Order: #{order_number}
Status: {delivery_status}
Tracking: {tracking_code}

Driver: {driver_name}
Phone: {driver_phone}'''
            },
            'ru': {
                'content': '''🚚 <b>Обновление доставки</b>

Заказ: #{order_number}
Статус: {delivery_status}
Отслеживание: {tracking_code}

Водитель: {driver_name}
Телефон: {driver_phone}'''
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