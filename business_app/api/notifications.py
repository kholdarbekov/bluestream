"""
Notifications API endpoints for the Water Business Platform
This file should be placed in business_app/api/notifications.py
"""

from flask import Blueprint, current_app, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from business_app.serializers.notification_serializers import (
    serialize_notification,
    serialize_notification_preferences,
    serialize_notification_template,
)
from business_app.utils.api_responses import (
    error_response,
    forbidden_response,
    internal_error_response,
    not_found_response,
    paginated_response,
    success_response,
)
from business_app.utils.decorators import cache_response, rate_limit, validate_json
from business_app.utils.exceptions import ForbiddenError, NotFoundError, ValidationError
from business_app.utils.service_factory import get_notification_service
from business_app.utils.translations import get_translation

notifications_bp = Blueprint("notifications", __name__)


@notifications_bp.route("/", methods=["GET"])
@jwt_required()
def get_notifications():
    """Get user notifications with pagination"""
    try:
        current_user_id = get_jwt_identity()

        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 20)), 50)
        status = request.args.get("status")
        notification_type = request.args.get("type")
        channel = request.args.get("channel")

        unread_only_raw = request.args.get("unread_only")
        unread_only = (
            str(unread_only_raw).lower() in {"1", "true", "yes", "on"} if unread_only_raw is not None else False
        )

        notifications_data = get_notification_service().get_user_notifications_paginated(
            user_id=current_user_id,
            page=page,
            per_page=per_page,
            status=status,
            notification_type=notification_type,
            channel=channel,
            unread_only=unread_only,
        )

        return paginated_response(
            items=[serialize_notification(notif) for notif in notifications_data["items"]],
            page=notifications_data["page"],
            per_page=notifications_data["per_page"],
            total=notifications_data["total"],
            additional_meta={"unread_count": notifications_data["unread_count"]},
        )

    except ValidationError as e:
        return error_response(e.message, status_code=400)
    except ValueError:
        return error_response("Invalid pagination value", status_code=400)
    except Exception as e:
        current_app.logger.error(f"Get notifications error: {e}")
        return internal_error_response("Failed to get notifications")


@notifications_bp.route("/<int:notification_id>", methods=["GET"])
@jwt_required()
def get_notification(notification_id):
    """Get specific notification details"""
    try:
        current_user_id = get_jwt_identity()
        notification = get_notification_service().get_notification_for_user(
            notification_id=notification_id,
            user_id=current_user_id,
            mark_as_read=True,
        )

        return success_response(data={"notification": serialize_notification(notification)})

    except NotFoundError as e:
        return not_found_response(e.message)
    except Exception as e:
        current_app.logger.error(f"Get notification error: {e}")
        return internal_error_response("Failed to get notification")


@notifications_bp.route("/<int:notification_id>/mark-read", methods=["POST"])
@jwt_required()
def mark_notification_read(notification_id):
    """Mark a notification as read"""
    try:
        current_user_id = get_jwt_identity()
        get_notification_service().mark_notification_read(
            notification_id=notification_id,
            user_id=current_user_id,
        )

        return success_response(message=get_translation("api.notifications.success.marked_read"))

    except NotFoundError as e:
        return not_found_response(e.message)
    except Exception as e:
        current_app.logger.error(f"Mark notification read error: {e}")
        return internal_error_response("Failed to mark notification as read")


@notifications_bp.route("/mark-all-read", methods=["POST"])
@jwt_required()
def mark_all_notifications_read():
    """Mark all notifications as read"""
    try:
        current_user_id = get_jwt_identity()
        marked_count = get_notification_service().mark_all_notifications_read(current_user_id)

        return success_response(message=f"{marked_count} notifications marked as read")

    except Exception as e:
        current_app.logger.error(f"Mark all notifications read error: {e}")
        return internal_error_response("Failed to mark all notifications as read")


@notifications_bp.route("/<int:notification_id>/delete", methods=["DELETE"])
@jwt_required()
def delete_notification(notification_id):
    """Delete a notification"""
    try:
        current_user_id = get_jwt_identity()
        get_notification_service().delete_notification_for_user(
            notification_id=notification_id,
            user_id=current_user_id,
        )

        return success_response(message=get_translation("api.notifications.success.deleted"))

    except NotFoundError as e:
        return not_found_response(e.message)
    except Exception as e:
        current_app.logger.error(f"Delete notification error: {e}")
        return internal_error_response("Failed to delete notification")


@notifications_bp.route("/preferences", methods=["GET"])
@jwt_required()
def get_notification_preferences():
    """Get user's notification preferences"""
    try:
        current_user_id = get_jwt_identity()
        preferences = get_notification_service().create_default_preferences(current_user_id)

        return success_response(data={"preferences": serialize_notification_preferences(preferences)})

    except Exception as e:
        current_app.logger.error(f"Get notification preferences error: {e}")
        return internal_error_response("Failed to get notification preferences")


@notifications_bp.route("/preferences", methods=["PUT"])
@jwt_required()
@validate_json()
def update_notification_preferences():
    """Update user's notification preferences"""
    try:
        current_user_id = get_jwt_identity()
        payload = request.get_json() or {}

        preferences = get_notification_service().update_notification_preferences_for_user(
            user_id=current_user_id,
            payload=payload,
        )

        return success_response(
            data={"preferences": serialize_notification_preferences(preferences)},
            message="Notification preferences updated successfully",
        )

    except ValidationError as e:
        return error_response(e.message, status_code=400)
    except Exception as e:
        current_app.logger.error(f"Update notification preferences error: {e}")
        return internal_error_response("Failed to update notification preferences")


@notifications_bp.route("/push-token", methods=["POST"])
@jwt_required()
@validate_json(["token", "platform"])
def register_push_token():
    """Register or update push notification token"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json() or {}

        get_notification_service().register_push_token_for_user(
            user_id=current_user_id,
            token=data.get("token"),
            platform=data.get("platform"),
            device_id=data.get("device_id"),
        )

        return success_response(message=get_translation("api.notifications.success.push_registered"))

    except ValidationError as e:
        return error_response(e.message, status_code=400)
    except Exception as e:
        current_app.logger.error(f"Register push token error: {e}")
        return internal_error_response("Failed to register push token")


@notifications_bp.route("/push-token", methods=["DELETE"])
@jwt_required()
@validate_json(["token"])
def unregister_push_token():
    """Unregister push notification token"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json() or {}

        get_notification_service().unregister_push_token_for_user(
            current_user_id,
            data.get("token"),
        )

        return success_response(message=get_translation("api.notifications.success.push_unregistered"))

    except Exception as e:
        current_app.logger.error(f"Unregister push token error: {e}")
        return internal_error_response("Failed to unregister push token")


@notifications_bp.route("/templates", methods=["GET"])
@cache_response(3600)
def get_notification_templates():
    """Get available notification templates"""
    try:
        _ = request.args.get("language", "uz")
        category = request.args.get("category")

        templates = get_notification_service().get_active_templates(category=category)

        return success_response(
            data={"templates": [serialize_notification_template(template) for template in templates]}
        )

    except Exception as e:
        current_app.logger.error(f"Get notification templates error: {e}")
        return internal_error_response("Failed to get notification templates")


@notifications_bp.route("/test", methods=["POST"])
@jwt_required()
@validate_json(["template_id"])
@rate_limit(5, 300)
def send_test_notification():
    """Send a test notification"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json() or {}

        template_id = data.get("template_id")
        channel = data.get("channel", "push")
        test_data = data.get("test_data", {})

        try:
            template_id = int(template_id)
        except (TypeError, ValueError):
            return error_response("template_id must be an integer", status_code=400)

        result = get_notification_service().send_test_notification_from_template(
            user_id=current_user_id,
            template_id=template_id,
            channel=channel,
            test_data=test_data,
        )

        return success_response(
            data=result,
            message="Test notification sent successfully",
        )

    except ValidationError as e:
        return error_response(e.message, status_code=400)
    except NotFoundError as e:
        return not_found_response(e.message)
    except Exception as e:
        current_app.logger.error(f"Send test notification error: {e}")
        return internal_error_response("Failed to send test notification")


@notifications_bp.route("/statistics", methods=["GET"])
@jwt_required()
def get_notification_statistics():
    """Get user's notification statistics"""
    try:
        current_user_id = get_jwt_identity()
        period = request.args.get("period", "month")

        stats = get_notification_service().get_notification_statistics_for_user(
            user_id=current_user_id,
            period=period,
        )
        return success_response(data=stats)

    except Exception as e:
        current_app.logger.error(f"Get notification statistics error: {e}")
        return internal_error_response("Failed to get notification statistics")


@notifications_bp.route("/channels", methods=["GET"])
@jwt_required()
def get_notification_channels():
    """Get user's available notification channels"""
    try:
        current_user_id = get_jwt_identity()
        channels = get_notification_service().get_user_notification_channels(current_user_id)

        return success_response(data={"channels": channels})

    except NotFoundError as e:
        return not_found_response(e.message)
    except Exception as e:
        current_app.logger.error(f"Get notification channels error: {e}")
        return internal_error_response("Failed to get notification channels")


@notifications_bp.route("/bulk-send", methods=["POST"])
@jwt_required()
@validate_json(["user_ids", "template_code"])
@rate_limit(1, 3600)
def send_bulk_notification():
    """Send bulk notification (admin only)"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json() or {}

        result = get_notification_service().queue_bulk_notification(
            sender_id=current_user_id,
            user_ids=data.get("user_ids"),
            template_code=data.get("template_code"),
            template_data=data.get("template_data", {}),
            channels=data.get("channels", ["push", "email"]),
        )

        return success_response(
            data=result,
            message="Bulk notification queued successfully",
        )

    except ValidationError as e:
        return error_response(e.message, status_code=400)
    except NotFoundError as e:
        return not_found_response(e.message)
    except ForbiddenError as e:
        return forbidden_response(e.message)
    except Exception as e:
        current_app.logger.error(f"Send bulk notification error: {e}")
        return internal_error_response("Failed to send bulk notification")


@notifications_bp.route("/delivery-reports", methods=["GET"])
@jwt_required()
def get_delivery_reports():
    """Get notification delivery reports"""
    try:
        current_user_id = get_jwt_identity()

        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 50)), 100)
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        channel = request.args.get("channel")

        reports_data = get_notification_service().get_delivery_reports_paginated(
            requester_id=current_user_id,
            page=page,
            per_page=per_page,
            start_date=start_date,
            end_date=end_date,
            channel=channel,
        )

        return paginated_response(
            items=reports_data["items"],
            page=reports_data["page"],
            per_page=reports_data["per_page"],
            total=reports_data["total"],
            additional_meta={"summary": reports_data["summary"]},
        )

    except ValidationError as e:
        return error_response(e.message, status_code=400)
    except ForbiddenError as e:
        return forbidden_response(e.message)
    except ValueError:
        return error_response("Invalid pagination value", status_code=400)
    except Exception as e:
        current_app.logger.error(f"Get delivery reports error: {e}")
        return internal_error_response("Failed to get delivery reports")
