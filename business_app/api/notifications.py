"""
Notifications API endpoints for the Water Business Platform
This file should be placed in business_app/api/notifications.py
"""
from flask import Blueprint, request, jsonify, current_app, g
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import and_, or_, desc, func
from datetime import datetime, UTC, timedelta

from business_app.models.notification import (
    Notification,
    NotificationTemplate,
    NotificationPreference,
    PushNotificationToken,
    NotificationChannel
)
from business_app.models.user import User
from business_app.utils.service_factory import get_notification_service
from business_app.utils.api_responses import (
    success_response, error_response, paginated_response, created_response,
    not_found_response, validation_error_response, internal_error_response,
    forbidden_response
)
from business_app.serializers.notification_serializers import (
    serialize_notification, serialize_notification_template, serialize_notification_preferences,
    serialize_bulk_notification, NotificationSchema, SendNotificationRequest,
    CreateTemplateRequest, UpdatePreferencesRequest, NotificationResponseSchema
)
from business_app.utils.decorators import validate_json, rate_limit, cache_response
from business_app.utils.constants import NotificationStatus, NotificationType, NotificationChannelType
from business_app.utils.translations import get_translation
from business_app.tasks.notification_tasks import send_bulk_notification_task
from business_app import db

notifications_bp = Blueprint('notifications', __name__)



@notifications_bp.route('/', methods=['GET'])
@jwt_required()
def get_notifications():
    """Get user notifications with pagination"""
    try:
        current_user_id = get_jwt_identity()

        # Get query parameters
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 20)), 50)
        status = request.args.get('status')
        notification_type = request.args.get('type')
        channel = request.args.get('channel')
        unread_only = request.args.get('unread_only', type=bool, default=False)

        # Build query
        query = Notification.query.filter_by(user_id=current_user_id)

        # Apply filters
        if status:
            try:
                notif_status = NotificationStatus(status)
                query = query.filter_by(status=notif_status)
            except ValueError:
                return error_response('Invalid status value')

        if notification_type:
            try:
                notif_type = NotificationType(notification_type)
                query = query.filter_by(notification_type=notif_type)
            except ValueError:
                return error_response('Invalid notification type')

        if channel:
            try:
                notif_channel = NotificationChannelType(channel)
                query = query.filter_by(channel=notif_channel)
            except ValueError:
                return error_response('Invalid channel value')

        if unread_only:
            query = query.filter_by(is_read=False)

        # Order by creation date (newest first)
        query = query.order_by(Notification.created_at.desc())

        # Paginate
        pagination = query.paginate(
            page=page, per_page=per_page, error_out=False
        )

        # Get unread count
        unread_count = Notification.query.filter_by(
            user_id=current_user_id,
            is_read=False
        ).count()

        # Serialize notifications
        notifications = [
            serialize_notification(notif) for notif in pagination.items
        ]

        return paginated_response(
            items=notifications,
            page=page,
            per_page=per_page,
            total=pagination.total,
            additional_meta={'unread_count': unread_count}
        )

    except Exception as e:
        current_app.logger.error(f"Get notifications error: {e}")
        return internal_error_response('Failed to get notifications')


@notifications_bp.route('/<int:notification_id>', methods=['GET'])
@jwt_required()
def get_notification(notification_id):
    """Get specific notification details"""
    try:
        current_user_id = get_jwt_identity()

        notification = Notification.query.filter_by(
            id=notification_id,
            user_id=current_user_id
        ).first()

        if not notification:
            return not_found_response('Notification not found')

        # Mark as read if not already read
        if not notification.is_read:
            notification.is_read = True
            notification.read_at = datetime.now(UTC)
            db.session.commit()

        return success_response(
            data={'notification': serialize_notification(notification)}
        )

    except Exception as e:
        current_app.logger.error(f"Get notification error: {e}")
        return internal_error_response('Failed to get notification')


@notifications_bp.route('/<int:notification_id>/mark-read', methods=['POST'])
@jwt_required()
def mark_notification_read(notification_id):
    """Mark a notification as read"""
    try:
        current_user_id = get_jwt_identity()

        notification = Notification.query.filter_by(
            id=notification_id,
            user_id=current_user_id
        ).first()

        if not notification:
            return not_found_response('Notification not found')

        if not notification.is_read:
            notification.is_read = True
            notification.read_at = datetime.now(UTC)
            db.session.commit()

        return success_response(message=get_translation('api.notifications.success.marked_read'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Mark notification read error: {e}")
        return internal_error_response('Failed to mark notification as read')


@notifications_bp.route('/mark-all-read', methods=['POST'])
@jwt_required()
def mark_all_notifications_read():
    """Mark all notifications as read"""
    try:
        current_user_id = get_jwt_identity()

        # Update all unread notifications
        unread_notifications = Notification.query.filter_by(
            user_id=current_user_id,
            is_read=False
        ).all()

        for notification in unread_notifications:
            notification.is_read = True
            notification.read_at = datetime.now(UTC)

        db.session.commit()

        return success_response(
            message=f'{len(unread_notifications)} notifications marked as read'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Mark all notifications read error: {e}")
        return internal_error_response('Failed to mark all notifications as read')


@notifications_bp.route('/<int:notification_id>/delete', methods=['DELETE'])
@jwt_required()
def delete_notification(notification_id):
    """Delete a notification"""
    try:
        current_user_id = get_jwt_identity()

        notification = Notification.query.filter_by(
            id=notification_id,
            user_id=current_user_id
        ).first()

        if not notification:
            return not_found_response('Notification not found')

        db.session.delete(notification)
        db.session.commit()

        return success_response(message=get_translation('api.notifications.success.deleted'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete notification error: {e}")
        return internal_error_response('Failed to delete notification')


@notifications_bp.route('/preferences', methods=['GET'])
@jwt_required()
def get_notification_preferences():
    """Get user's notification preferences"""
    try:
        current_user_id = get_jwt_identity()

        # Get or create default preferences
        preferences = NotificationPreference.query.filter_by(
            user_id=current_user_id
        ).first()

        if not preferences:
            preferences = get_notification_service().create_default_preferences(current_user_id)

        return success_response(
            data={'preferences': serialize_notification_preferences(preferences)}
        )

    except Exception as e:
        current_app.logger.error(f"Get notification preferences error: {e}")
        return internal_error_response('Failed to get notification preferences')


@notifications_bp.route('/preferences', methods=['PUT'])
@jwt_required()
@validate_json()
def update_notification_preferences():
    """Update user's notification preferences"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        # Get or create preferences
        preferences = NotificationPreference.query.filter_by(
            user_id=current_user_id
        ).first()

        if not preferences:
            preferences = get_notification_service().create_default_preferences(current_user_id)

        # Update preferences
        updatable_fields = [
            'email_enabled', 'sms_enabled', 'push_enabled', 'telegram_enabled',
            'order_updates', 'delivery_updates', 'payment_updates', 'subscription_updates',
            'loyalty_updates', 'marketing_emails', 'promotional_sms', 'system_alerts',
            'quiet_hours_start', 'quiet_hours_end', 'timezone'
        ]

        for field in updatable_fields:
            if field in data:
                setattr(preferences, field, data[field])

        preferences.updated_at = datetime.now(UTC)
        db.session.commit()

        return success_response(
            data={'preferences': serialize_notification_preferences(preferences)},
            message='Notification preferences updated successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update notification preferences error: {e}")
        return internal_error_response('Failed to update notification preferences')


@notifications_bp.route('/push-token', methods=['POST'])
@jwt_required()
@validate_json(['token', 'platform'])
def register_push_token():
    """Register or update push notification token"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        token = data.get('token')
        platform = data.get('platform')  # 'ios', 'android', 'web'
        device_id = data.get('device_id')

        if platform not in ['ios', 'android', 'web']:
            return error_response('Invalid platform')

        # Check if token already exists
        existing_token = PushNotificationToken.query.filter_by(
            token=token
        ).first()

        if existing_token:
            # Update existing token
            existing_token.user_id = current_user_id
            existing_token.is_active = True
            existing_token.updated_at = datetime.now(UTC)
        else:
            # Create new token record
            push_token = PushNotificationToken(
                user_id=current_user_id,
                token=token,
                platform=platform,
                device_id=device_id,
                is_active=True
            )
            db.session.add(push_token)

        db.session.commit()

        return success_response(message=get_translation('api.notifications.success.push_registered'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Register push token error: {e}")
        return internal_error_response('Failed to register push token')


@notifications_bp.route('/push-token', methods=['DELETE'])
@jwt_required()
@validate_json(['token'])
def unregister_push_token():
    """Unregister push notification token"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        token = data.get('token')

        push_token = PushNotificationToken.query.filter_by(
            user_id=current_user_id,
            token=token
        ).first()

        if push_token:
            push_token.is_active = False
            push_token.updated_at = datetime.now(UTC)
            db.session.commit()

        return success_response(message=get_translation('api.notifications.success.push_unregistered'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Unregister push token error: {e}")
        return internal_error_response('Failed to unregister push token')


@notifications_bp.route('/templates', methods=['GET'])
@cache_response(3600)  # Cache for 1 hour
def get_notification_templates():
    """Get available notification templates"""
    try:
        language = request.args.get('language', 'uz')
        category = request.args.get('category')

        query = NotificationTemplate.query.filter_by(is_active=True)

        if category:
            # Historical query param name retained; map to notification_type.
            query = query.filter_by(notification_type=category)

        templates = query.order_by(
            NotificationTemplate.notification_type,
            NotificationTemplate.name
        ).all()

        return success_response(
            data={
                'templates': [
                    serialize_notification_template(template)
                    for template in templates
                ]
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get notification templates error: {e}")
        return internal_error_response('Failed to get notification templates')


@notifications_bp.route('/test', methods=['POST'])
@jwt_required()
@validate_json(['template_id'])
@rate_limit(5, 300)  # 5 test notifications per 5 minutes
def send_test_notification():
    """Send a test notification"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        template_id = data.get('template_id')
        channel = data.get('channel', 'push')
        test_data = data.get('test_data', {})

        template = NotificationTemplate.query.filter_by(
            id=template_id,
            is_active=True
        ).first()

        if not template:
            return not_found_response('Template not found')

        # Send test notification
        notification = get_notification_service().send_notification(
            user_id=current_user_id,
            template_code=template.code,
            template_data=test_data,
            channels=[channel],
            is_test=True
        )

        return success_response(
            data={'notification_id': notification.id if notification else None},
            message='Test notification sent successfully'
        )

    except Exception as e:
        current_app.logger.error(f"Send test notification error: {e}")
        return internal_error_response('Failed to send test notification')


@notifications_bp.route('/statistics', methods=['GET'])
@jwt_required()
def get_notification_statistics():
    """Get user's notification statistics"""
    try:
        current_user_id = get_jwt_identity()
        period = request.args.get('period', 'month')  # week, month, quarter, year

        # Calculate date range
        now = datetime.now(UTC)
        if period == 'week':
            start_date = now - timedelta(weeks=1)
        elif period == 'month':
            start_date = now - timedelta(days=30)
        elif period == 'quarter':
            start_date = now - timedelta(days=90)
        elif period == 'year':
            start_date = now - timedelta(days=365)
        else:
            start_date = now - timedelta(days=30)  # Default to month

        # Get aggregated statistics using optimized queries
        base_query = Notification.query.filter_by(user_id=current_user_id).filter(
            Notification.created_at >= start_date
        )

        total_notifications = base_query.count()
        read_notifications = base_query.filter_by(is_read=True).count()
        unread_notifications = total_notifications - read_notifications

        # Get notifications by type using database aggregation
        notifications_by_type = {}
        type_stats = db.session.query(
            Notification.notification_type,
            func.count(Notification.id)
        ).filter_by(user_id=current_user_id).filter(
            Notification.created_at >= start_date
        ).group_by(Notification.notification_type).all()

        for notification_type, count in type_stats:
            notifications_by_type[notification_type.value] = count

        # Get notifications by channel using database aggregation
        notifications_by_channel = {}
        channel_stats = db.session.query(
            Notification.channel,
            func.count(Notification.id)
        ).filter_by(user_id=current_user_id).filter(
            Notification.created_at >= start_date
        ).group_by(Notification.channel).all()

        for channel, count in channel_stats:
            notifications_by_channel[channel.value] = count

        # Get daily trend using database aggregation
        daily_stats = db.session.query(
            func.date(Notification.created_at).label('date'),
            func.count(Notification.id).label('count')
        ).filter_by(user_id=current_user_id).filter(
            Notification.created_at >= start_date
        ).group_by(func.date(Notification.created_at)).all()

        # Create daily trend dictionary
        daily_notifications = {}
        for date_obj, count in daily_stats:
            daily_notifications[date_obj.isoformat()] = count

        return success_response(
            data={
                'period': period,
                'statistics': {
                    'total_notifications': total_notifications,
                    'read_notifications': read_notifications,
                    'unread_notifications': unread_notifications,
                    'read_rate': round((read_notifications / total_notifications * 100), 2) if total_notifications > 0 else 0,
                    'notifications_by_type': notifications_by_type,
                    'notifications_by_channel': notifications_by_channel,
                    'daily_trend': daily_notifications
                }
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get notification statistics error: {e}")
        return internal_error_response('Failed to get notification statistics')


@notifications_bp.route('/channels', methods=['GET'])
@jwt_required()
def get_notification_channels():
    """Get user's available notification channels"""
    try:
        current_user_id = get_jwt_identity()

        user = User.query.get(current_user_id)
        if not user:
            return not_found_response('User not found')

        # Get user's push tokens
        push_tokens = PushNotificationToken.query.filter_by(
            user_id=current_user_id,
            is_active=True
        ).all()

        # Check available channels
        channels = {
            'email': {
                'available': bool(user.email and user.email_verified),
                'address': user.email if user.email_verified else None,
                'verified': user.email_verified
            },
            'sms': {
                'available': bool(user.phone and user.phone_verified),
                'number': user.phone if user.phone_verified else None,
                'verified': user.phone_verified
            },
            'push': {
                'available': len(push_tokens) > 0,
                'devices': [
                    {
                        'platform': token.platform,
                        'device_id': token.device_id,
                        'registered_at': token.created_at.isoformat()
                    }
                    for token in push_tokens
                ]
            },
            'telegram': {
                'available': bool(getattr(user, 'telegram_chat_id', None)),
                'chat_id': getattr(user, 'telegram_chat_id', None)
            }
        }

        return success_response(data={'channels': channels})

    except Exception as e:
        current_app.logger.error(f"Get notification channels error: {e}")
        return internal_error_response('Failed to get notification channels')


@notifications_bp.route('/bulk-send', methods=['POST'])
@jwt_required()
@validate_json(['user_ids', 'template_code'])
@rate_limit(1, 3600)  # 1 bulk send per hour
def send_bulk_notification():
    """Send bulk notification (admin only)"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        # Check if user has admin privileges
        user = User.query.get(current_user_id)
        if not user or not user.is_admin:
            return forbidden_response('Admin access required')

        user_ids = data.get('user_ids')
        template_code = data.get('template_code')
        template_data = data.get('template_data', {})
        channels = data.get('channels', ['push', 'email'])

        # Validate user_ids
        if not isinstance(user_ids, list) or len(user_ids) > 1000:
            return error_response('Invalid user_ids or too many recipients (max 1000)')

        # Validate template
        template = NotificationTemplate.query.filter_by(
            code=template_code,
            is_active=True
        ).first()

        if not template:
            return not_found_response('Template not found')

        # Send bulk notification asynchronously
        task = send_bulk_notification_task.delay(
            user_ids=user_ids,
            template_code=template_code,
            template_data=template_data,
            channels=channels,
            sender_id=current_user_id
        )

        return success_response(
            data={
                'task_id': task.id,
                'recipient_count': len(user_ids)
            },
            message='Bulk notification queued successfully'
        )

    except Exception as e:
        current_app.logger.error(f"Send bulk notification error: {e}")
        return internal_error_response('Failed to send bulk notification')


@notifications_bp.route('/delivery-reports', methods=['GET'])
@jwt_required()
def get_delivery_reports():
    """Get notification delivery reports"""
    try:
        current_user_id = get_jwt_identity()

        # Check if user has admin privileges
        user = User.query.get(current_user_id)
        if not user or not user.is_admin:
            return forbidden_response('Admin access required')

        # Get query parameters
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 50)), 100)
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        channel = request.args.get('channel')

        # Build query
        query = Notification.query

        # Apply date filters
        if start_date:
            try:
                start_dt = datetime.fromisoformat(start_date)
                query = query.filter(Notification.created_at >= start_dt)
            except ValueError:
                return error_response('Invalid start_date format')

        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date)
                query = query.filter(Notification.created_at <= end_dt)
            except ValueError:
                return error_response('Invalid end_date format')

        if channel:
            try:
                notif_channel = NotificationChannelType(channel)
                query = query.filter_by(channel=notif_channel)
            except ValueError:
                return error_response('Invalid channel value')

        # Order by creation date (newest first)
        query = query.order_by(Notification.created_at.desc())

        # Paginate
        pagination = query.paginate(
            page=page, per_page=per_page, error_out=False
        )

        # Calculate delivery statistics
        total_sent = query.count()
        delivered = query.filter_by(status=NotificationStatus.DELIVERED).count()
        failed = query.filter_by(status=NotificationStatus.FAILED).count()
        pending = query.filter_by(status=NotificationStatus.PENDING).count()

        # Serialize reports
        reports = [
            {
                'id': notif.id,
                'user_id': notif.user_id,
                'channel': notif.channel.value,
                'status': notif.status.value,
                'created_at': notif.created_at.isoformat(),
                'sent_at': notif.sent_at.isoformat() if notif.sent_at else None,
                'delivered_at': notif.delivered_at.isoformat() if notif.delivered_at else None,
                'error_message': notif.error_message
            }
            for notif in pagination.items
        ]

        return paginated_response(
            items=reports,
            page=page,
            per_page=per_page,
            total=pagination.total,
            additional_meta={
                'summary': {
                    'total_sent': total_sent,
                    'delivered': delivered,
                    'failed': failed,
                    'pending': pending,
                    'delivery_rate': round((delivered / total_sent * 100), 2) if total_sent > 0 else 0
                }
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get delivery reports error: {e}")
        return internal_error_response('Failed to get delivery reports')
