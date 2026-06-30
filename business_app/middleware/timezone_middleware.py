"""
Timezone middleware for handling timezone conversion in API requests/responses
"""

from datetime import datetime, UTC
from flask import request, g
from functools import wraps
import json
import logging

from business_app.utils.timezone_utils import (
    get_user_timezone,
    utc_to_local,
    parse_user_datetime,
    ensure_utc,
)

logger = logging.getLogger(__name__)


class TimezoneMiddleware:
    """Middleware for automatic timezone conversion"""

    def __init__(self, app=None):
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        """Initialize timezone middleware with Flask app"""
        app.before_request(self.before_request)
        app.after_request(self.after_request)

        # Register timezone context processor
        @app.context_processor
        def inject_timezone_context():
            return {"user_timezone": get_user_timezone(), "utc_now": datetime.now(UTC)}

    def before_request(self):
        """Process request before handling"""
        try:
            # Set user timezone in global context
            g.user_timezone = get_user_timezone()

            # Convert datetime strings in request JSON to UTC
            # Only process if there's actual JSON data
            if request.is_json and request.content_length and request.content_length > 0:
                try:
                    json_data = request.get_json(silent=True)
                    if json_data:
                        self._convert_request_datetimes(json_data)
                except Exception as json_error:
                    # Ignore JSON parsing errors (malformed JSON, empty body, etc.)
                    logger.debug(f"Could not parse request JSON: {json_error}")

        except Exception as e:
            logger.warning(f"Error in timezone middleware before_request: {e}")

    def after_request(self, response):
        """Process response after handling"""
        try:
            # Convert UTC datetimes in response to user timezone
            if response.is_json and response.get_json():
                data = response.get_json()
                converted_data = self._convert_response_datetimes(data)
                response.set_data(json.dumps(converted_data))

        except Exception as e:
            logger.warning(f"Error in timezone middleware after_request: {e}")

        return response

    def _convert_request_datetimes(self, data, source_tz=None):
        """
        Convert datetime strings in request data from user timezone to UTC

        Args:
            data: Request data (dict, list, or primitive)
            source_tz: Source timezone (defaults to user timezone)
        """
        if source_tz is None:
            source_tz = getattr(g, "user_timezone", None)

        datetime_fields = [
            "created_at",
            "updated_at",
            "deleted_at",
            "start_time",
            "end_time",
            "scheduled_at",
            "delivery_time",
            "preferred_delivery_time",
            "expires_at",
            "valid_until",
            # date_of_birth is intentionally excluded: it is a pure calendar date
            # with no time component.  Running it through parse_user_datetime()
            # interprets it as wall-clock midnight in the user's local timezone
            # and converts to UTC, which shifts the calendar day backwards for
            # timezones east of UTC (e.g. Asia/Tashkent +05:00 yields the
            # previous day).  Date-only fields must never be tz-shifted.
            "last_login",
            "verified_at",
            "paid_at",
        ]

        if isinstance(data, dict):
            for key, value in data.items():
                if key in datetime_fields and isinstance(value, str):
                    try:
                        # Parse and convert to UTC
                        utc_dt = parse_user_datetime(value, source_tz=source_tz)
                        data[key] = utc_dt.isoformat()
                    except (ValueError, TypeError):
                        # Keep original value if parsing fails
                        pass
                elif isinstance(value, (dict, list)):
                    self._convert_request_datetimes(value, source_tz)

        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    self._convert_request_datetimes(item, source_tz)

    def _convert_response_datetimes(self, data, target_tz=None):
        """
        Convert UTC datetimes in response data to user timezone

        Args:
            data: Response data (dict, list, or primitive)
            target_tz: Target timezone (defaults to user timezone)

        Returns:
            Converted data
        """
        if target_tz is None:
            target_tz = getattr(g, "user_timezone", None)

        datetime_fields = [
            "created_at",
            "updated_at",
            "deleted_at",
            "start_time",
            "end_time",
            "scheduled_at",
            "delivery_time",
            "estimated_delivery_time",
            "expires_at",
            "valid_until",
            "last_login",
            "verified_at",
            "paid_at",
            "delivered_at",
            "confirmed_at",
            "cancelled_at",
        ]

        if isinstance(data, dict):
            converted = {}
            for key, value in data.items():
                if key in datetime_fields and isinstance(value, str):
                    try:
                        # Parse ISO string and convert to user timezone
                        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                        local_dt = utc_to_local(dt, target_tz)
                        converted[key] = local_dt.isoformat()
                    except (ValueError, TypeError):
                        # Keep original value if parsing fails
                        converted[key] = value
                elif isinstance(value, (dict, list)):
                    converted[key] = self._convert_response_datetimes(value, target_tz)
                else:
                    converted[key] = value
            return converted

        elif isinstance(data, list):
            return [self._convert_response_datetimes(item, target_tz) for item in data]

        else:
            return data


def timezone_aware(f):
    """
    Decorator to ensure timezone-aware handling in view functions

    Usage:
        @app.route('/api/orders')
        @timezone_aware
        def get_orders():
            # All datetime operations will be timezone-aware
            pass
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Ensure user timezone is set
        if not hasattr(g, "user_timezone"):
            g.user_timezone = get_user_timezone()

        # Call original function
        result = f(*args, **kwargs)

        return result

    return decorated_function


def convert_model_datetimes_to_user_tz(model_dict, timezone_tz=None):
    """
    Convert model datetime fields to user timezone for API response

    Args:
        model_dict: Dictionary representation of model
        timezone_tz: Target timezone

    Returns:
        dict: Model dict with converted datetimes
    """
    if timezone_tz is None:
        timezone_tz = getattr(g, "user_timezone", None)

    datetime_fields = [
        "created_at",
        "updated_at",
        "deleted_at",
        "last_login",
        "verified_at",
        "paid_at",
        "delivered_at",
        "confirmed_at",
        "cancelled_at",
        "expires_at",
        "valid_until",
        "scheduled_at",
    ]

    converted = model_dict.copy()

    for field in datetime_fields:
        if field in converted and converted[field]:
            try:
                dt = converted[field]
                if isinstance(dt, str):
                    dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))

                # Convert to user timezone
                local_dt = utc_to_local(dt, timezone_tz)
                converted[field] = local_dt.isoformat()

            except (ValueError, TypeError, AttributeError):
                # Keep original value if conversion fails
                pass

    return converted


def prepare_datetime_for_storage(dt_value):
    """
    Prepare datetime value for database storage (ensure UTC)

    Args:
        dt_value: Datetime value (string, datetime, or None)

    Returns:
        datetime: UTC datetime ready for storage
    """
    if dt_value is None:
        return None

    if isinstance(dt_value, str):
        try:
            # Parse user input and convert to UTC
            return parse_user_datetime(dt_value)
        except ValueError:
            return None

    elif isinstance(dt_value, datetime):
        # Ensure UTC timezone
        return ensure_utc(dt_value)

    else:
        return None
