"""
Session Management API endpoints
"""

import logging
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from flasgger import swag_from

from business_app.services.auth_service import AuthService
from business_app.services.session_cleanup_service import SessionCleanupService
from business_app.utils.decorators import admin_required, rate_limit, validate_json
from business_app.utils.helpers import paginate_query
from business_app.models.user import UserSession, User

session_management_bp = Blueprint("session_management", __name__)
logger = logging.getLogger(__name__)


@session_management_bp.route("/sessions", methods=["GET"])
@jwt_required()
@rate_limit(100, 3600)  # 100 requests per hour
@swag_from(
    {
        "tags": ["Session Management"],
        "summary": "Get user sessions",
        "description": "Get all active sessions for the current user",
        "parameters": [
            {"name": "page", "in": "query", "type": "integer", "default": 1, "description": "Page number"},
            {"name": "per_page", "in": "query", "type": "integer", "default": 10, "description": "Sessions per page"},
        ],
        "responses": {
            200: {
                "description": "User sessions retrieved successfully",
                "schema": {
                    "type": "object",
                    "properties": {
                        "sessions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "integer"},
                                    "session_token": {"type": "string"},
                                    "device_info": {"type": "string"},
                                    "ip_address": {"type": "string"},
                                    "expires_at": {"type": "string"},
                                    "is_active": {"type": "boolean"},
                                    "last_activity": {"type": "string"},
                                    "is_current": {"type": "boolean"},
                                },
                            },
                        },
                        "pagination": {
                            "type": "object",
                            "properties": {
                                "page": {"type": "integer"},
                                "pages": {"type": "integer"},
                                "per_page": {"type": "integer"},
                                "total": {"type": "integer"},
                            },
                        },
                    },
                },
            },
            401: {"description": "Unauthorized"},
            429: {"description": "Rate limit exceeded"},
        },
    }
)
def get_user_sessions():
    """Get all sessions for the current user"""
    try:
        user_id = get_jwt_identity()
        current_jti = get_jwt().get("jti")

        page = request.args.get("page", 1, type=int)
        per_page = min(request.args.get("per_page", 10, type=int), 50)

        # Get paginated sessions
        sessions_query = UserSession.query.filter_by(user_id=user_id).order_by(UserSession.last_activity.desc())

        pagination = paginate_query(sessions_query, page, per_page)

        sessions_data = []
        for session in pagination.items:
            session_data = session.to_dict()
            # Mark current session
            session_data["is_current"] = session.session_token == current_jti
            # Don't expose full session token for security
            session_data["session_token"] = session.session_token[-8:] + "..."
            sessions_data.append(session_data)

        return (
            jsonify(
                {
                    "sessions": sessions_data,
                    "pagination": {
                        "page": pagination.page,
                        "pages": pagination.pages,
                        "per_page": pagination.per_page,
                        "total": pagination.total,
                    },
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"Error getting user sessions: {e}")
        return jsonify({"error": "Failed to retrieve sessions"}), 500


@session_management_bp.route("/sessions/<int:session_id>", methods=["DELETE"])
@jwt_required()
@rate_limit(50, 3600)  # 50 requests per hour
@swag_from(
    {
        "tags": ["Session Management"],
        "summary": "Revoke a session",
        "description": "Revoke a specific session for the current user",
        "parameters": [
            {
                "name": "session_id",
                "in": "path",
                "type": "integer",
                "required": True,
                "description": "Session ID to revoke",
            }
        ],
        "responses": {
            200: {"description": "Session revoked successfully"},
            400: {"description": "Cannot revoke current session"},
            404: {"description": "Session not found"},
            401: {"description": "Unauthorized"},
            429: {"description": "Rate limit exceeded"},
        },
    }
)
def revoke_session(session_id):
    """Revoke a specific session"""
    try:
        user_id = get_jwt_identity()
        current_jti = get_jwt().get("jti")

        # Find the session
        session = UserSession.query.filter_by(id=session_id, user_id=user_id, is_active=True).first()

        if not session:
            return jsonify({"error": "Session not found"}), 404

        # Prevent revoking current session
        if session.session_token == current_jti:
            return jsonify({"error": "Cannot revoke current session"}), 400

        # Revoke the session
        auth_service = AuthService()
        auth_service._end_user_session(user_id, f"temp_token_with_jti_{session.session_token}")

        # Blacklist the token if possible with proper expiry
        from business_app.services.token_service import TokenService
        from datetime import timedelta
        from flask import current_app

        token_service = TokenService()
        # Default to access token expiry for session tokens
        default_expires = current_app.config.get("JWT_ACCESS_TOKEN_EXPIRES", timedelta(hours=1))
        token_service.blacklist_token(session.session_token, expires_delta=default_expires)

        logger.info(f"Session {session_id} revoked for user {user_id}")
        return jsonify({"message": "Session revoked successfully"}), 200

    except Exception as e:
        logger.error(f"Error revoking session {session_id}: {e}")
        return jsonify({"error": "Failed to revoke session"}), 500


@session_management_bp.route("/sessions/revoke-all", methods=["POST"])
@jwt_required()
@rate_limit(10, 3600)  # 10 requests per hour
@swag_from(
    {
        "tags": ["Session Management"],
        "summary": "Revoke all other sessions",
        "description": "Revoke all sessions except the current one",
        "responses": {
            200: {"description": "All other sessions revoked successfully"},
            401: {"description": "Unauthorized"},
            429: {"description": "Rate limit exceeded"},
        },
    }
)
def revoke_all_sessions():
    """Revoke all sessions except the current one"""
    try:
        user_id = get_jwt_identity()

        auth_service = AuthService()
        result = auth_service.cleanup_user_sessions(user_id, exclude_current=True)

        logger.info(f"All other sessions revoked for user {user_id}: {result}")
        return (
            jsonify(
                {
                    "message": "All other sessions revoked successfully",
                    "revoked_count": result.get("user_sessions_cleaned", 0),
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"Error revoking all sessions for user: {e}")
        return jsonify({"error": "Failed to revoke sessions"}), 500


# Admin endpoints for session management
@session_management_bp.route("/admin/sessions", methods=["GET"])
@jwt_required()
@admin_required
@rate_limit(100, 3600)  # 100 requests per hour
@swag_from(
    {
        "tags": ["Admin - Session Management"],
        "summary": "Get all system sessions",
        "description": "Get all sessions in the system (admin only)",
        "parameters": [
            {"name": "page", "in": "query", "type": "integer", "default": 1, "description": "Page number"},
            {"name": "per_page", "in": "query", "type": "integer", "default": 20, "description": "Sessions per page"},
            {
                "name": "active_only",
                "in": "query",
                "type": "boolean",
                "default": False,
                "description": "Show only active sessions",
            },
        ],
        "responses": {
            200: {"description": "System sessions retrieved successfully"},
            401: {"description": "Unauthorized"},
            403: {"description": "Admin access required"},
            429: {"description": "Rate limit exceeded"},
        },
    }
)
def get_all_sessions():
    """Get all sessions in the system (admin only)"""
    try:
        page = request.args.get("page", 1, type=int)
        per_page = min(request.args.get("per_page", 20, type=int), 100)
        active_only = request.args.get("active_only", False, type=bool)

        # Build query
        sessions_query = UserSession.query.join(User).order_by(UserSession.last_activity.desc())

        if active_only:
            sessions_query = sessions_query.filter(UserSession.is_active == True)

        pagination = paginate_query(sessions_query, page, per_page)

        sessions_data = []
        for session in pagination.items:
            session_data = session.to_dict()
            # Add user info
            session_data["user"] = {
                "id": session.user.id,
                "email": session.user.email,
                "first_name": session.user.first_name,
                "last_name": session.user.last_name,
                "status": session.user.status,
            }
            # Mask session token for security
            session_data["session_token"] = session.session_token[-8:] + "..."
            sessions_data.append(session_data)

        return (
            jsonify(
                {
                    "sessions": sessions_data,
                    "pagination": {
                        "page": pagination.page,
                        "pages": pagination.pages,
                        "per_page": pagination.per_page,
                        "total": pagination.total,
                    },
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"Error getting all sessions: {e}")
        return jsonify({"error": "Failed to retrieve sessions"}), 500


@session_management_bp.route("/admin/cleanup", methods=["POST"])
@jwt_required()
@admin_required
@rate_limit(5, 3600)  # 5 requests per hour
@validate_json(["action"])
@swag_from(
    {
        "tags": ["Admin - Session Management"],
        "summary": "Trigger session cleanup",
        "description": "Trigger various session cleanup operations (admin only)",
        "parameters": [
            {
                "name": "body",
                "in": "body",
                "required": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["sessions", "users", "orphaned", "full"],
                            "description": "Type of cleanup to perform",
                        },
                        "batch_size": {"type": "integer", "default": 1000, "description": "Batch size for processing"},
                    },
                    "required": ["action"],
                },
            }
        ],
        "responses": {
            200: {"description": "Cleanup completed successfully"},
            400: {"description": "Invalid action"},
            401: {"description": "Unauthorized"},
            403: {"description": "Admin access required"},
            429: {"description": "Rate limit exceeded"},
        },
    }
)
def trigger_cleanup():
    """Trigger session cleanup operations (admin only)"""
    try:
        data = request.get_json()
        action = data["action"]
        batch_size = data.get("batch_size", 1000)

        service = SessionCleanupService()

        if action == "sessions":
            results = service.cleanup_expired_sessions(batch_size)
        elif action == "users":
            results = service.cleanup_inactive_users(batch_size)
        elif action == "orphaned":
            results = service.cleanup_orphaned_data()
        elif action == "full":
            results = service.full_cleanup(batch_size)
        else:
            return jsonify({"error": "Invalid action"}), 400

        logger.info(f"Admin triggered {action} cleanup: {results}")
        return jsonify({"message": f"{action.title()} cleanup completed", "results": results}), 200

    except Exception as e:
        logger.error(f"Error in admin cleanup: {e}")
        return jsonify({"error": "Cleanup failed"}), 500


@session_management_bp.route("/admin/stats", methods=["GET"])
@jwt_required()
@admin_required
@rate_limit(50, 3600)  # 50 requests per hour
@swag_from(
    {
        "tags": ["Admin - Session Management"],
        "summary": "Get session statistics",
        "description": "Get comprehensive session and user statistics (admin only)",
        "responses": {
            200: {"description": "Statistics retrieved successfully"},
            401: {"description": "Unauthorized"},
            403: {"description": "Admin access required"},
            429: {"description": "Rate limit exceeded"},
        },
    }
)
def get_session_stats():
    """Get session and user statistics (admin only)"""
    try:
        service = SessionCleanupService()
        stats = service.get_cleanup_statistics()

        return (
            jsonify(
                {
                    "statistics": stats,
                    "timestamp": (
                        UserSession.query.first().created_at.isoformat() if UserSession.query.first() else None
                    ),
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"Error getting session stats: {e}")
        return jsonify({"error": "Failed to retrieve statistics"}), 500
