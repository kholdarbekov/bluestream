"""
Centralized validation helpers to reduce duplicate validation logic across API endpoints
"""

from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, UTC
from flask import request
from flask_jwt_extended import get_jwt_identity

from business_app.utils.exceptions import ValidationError


class PaginationValidator:
    """Validator for pagination parameters"""

    @staticmethod
    def validate_pagination_params(
        default_page: int = 1, default_per_page: int = 20, max_per_page: int = 50
    ) -> Tuple[int, int]:
        """
        Validate and extract pagination parameters from request

        Args:
            default_page: Default page number
            default_per_page: Default items per page
            max_per_page: Maximum allowed items per page

        Returns:
            Tuple of (page, per_page)

        Raises:
            ValidationError: If pagination parameters are invalid
        """
        try:
            page = int(request.args.get("page", default_page))
            per_page = min(int(request.args.get("per_page", default_per_page)), max_per_page)
        except ValueError:
            raise ValidationError("Invalid pagination parameters. Page and per_page must be integers.")

        if page < 1:
            raise ValidationError("Page number must be positive")

        if per_page < 1:
            raise ValidationError("Items per page must be positive")

        return page, per_page


class DateValidator:
    """Validator for date parameters"""

    @staticmethod
    def validate_date_range(
        start_date_str: Optional[str] = None, end_date_str: Optional[str] = None, allow_future: bool = True
    ) -> Tuple[Optional[datetime], Optional[datetime]]:
        """
        Validate and parse date range parameters

        Args:
            start_date_str: Start date in ISO format
            end_date_str: End date in ISO format
            allow_future: Whether to allow future dates

        Returns:
            Tuple of (start_date, end_date) as datetime objects

        Raises:
            ValidationError: If date parameters are invalid
        """
        start_date = None
        end_date = None

        if start_date_str:
            try:
                start_date = datetime.fromisoformat(start_date_str)
            except ValueError:
                raise ValidationError("Invalid start_date format. Use ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")

        if end_date_str:
            try:
                end_date = datetime.fromisoformat(end_date_str)
            except ValueError:
                raise ValidationError("Invalid end_date format. Use ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")

        # Validate date range logic
        if start_date and end_date and start_date > end_date:
            raise ValidationError("Start date cannot be after end date")

        # Check future dates if not allowed
        if not allow_future:
            now = datetime.now(UTC)
            if start_date and start_date > now:
                raise ValidationError("Start date cannot be in the future")
            if end_date and end_date > now:
                raise ValidationError("End date cannot be in the future")

        return start_date, end_date

    @staticmethod
    def validate_single_date(date_str: str, field_name: str = "date", allow_future: bool = True) -> datetime:
        """
        Validate and parse a single date parameter

        Args:
            date_str: Date string in ISO format
            field_name: Name of the field for error messages
            allow_future: Whether to allow future dates

        Returns:
            Parsed datetime object

        Raises:
            ValidationError: If date is invalid
        """
        try:
            parsed_date = datetime.fromisoformat(date_str)
        except ValueError:
            raise ValidationError(f"Invalid {field_name} format. Use ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")

        if not allow_future and parsed_date > datetime.now(UTC):
            raise ValidationError(f"{field_name.title()} cannot be in the future")

        return parsed_date


class StatusValidator:
    """Validator for status enum parameters"""

    @staticmethod
    def validate_status_enum(status_str: Optional[str], enum_class, field_name: str = "status") -> Optional[Any]:
        """
        Validate status parameter against an enum

        Args:
            status_str: Status string value
            enum_class: Enum class to validate against
            field_name: Name of the field for error messages

        Returns:
            Enum value if valid, None if status_str is None

        Raises:
            ValidationError: If status is invalid
        """
        if not status_str:
            return None

        try:
            return enum_class(status_str)
        except ValueError:
            valid_values = [e.value for e in enum_class]
            raise ValidationError(
                f"Invalid {field_name} value. Valid options: {', '.join(valid_values)}",
                details={"valid_values": valid_values, "received": status_str},
            )


class RequestDataValidator:
    """Validator for request data"""

    @staticmethod
    def get_current_user_id() -> int:
        """
        Get current user ID from JWT token

        Returns:
            Current user ID

        Raises:
            ValidationError: If no valid JWT token found
        """
        user_id = get_jwt_identity()
        if not user_id:
            raise ValidationError("Authentication required")
        return user_id

    @staticmethod
    def validate_required_fields(data: Dict[str, Any], required_fields: List[str]) -> None:
        """
        Validate that required fields are present in request data

        Args:
            data: Request data dictionary
            required_fields: List of required field names

        Raises:
            ValidationError: If any required fields are missing
        """
        missing_fields = []
        for field in required_fields:
            if field not in data or data[field] is None:
                missing_fields.append(field)

        if missing_fields:
            raise ValidationError(
                f"Missing required fields: {', '.join(missing_fields)}", details={"missing_fields": missing_fields}
            )

    @staticmethod
    def validate_non_empty_string(
        value: Any, field_name: str, min_length: int = 1, max_length: Optional[int] = None
    ) -> str:
        """
        Validate that a value is a non-empty string

        Args:
            value: Value to validate
            field_name: Name of the field for error messages
            min_length: Minimum string length
            max_length: Maximum string length (optional)

        Returns:
            Stripped string value

        Raises:
            ValidationError: If value is not a valid string
        """
        if not isinstance(value, str):
            raise ValidationError(f"{field_name} must be a string")

        value = value.strip()

        if len(value) < min_length:
            raise ValidationError(f"{field_name} must be at least {min_length} characters long")

        if max_length and len(value) > max_length:
            raise ValidationError(f"{field_name} must be no more than {max_length} characters long")

        return value

    @staticmethod
    def validate_positive_integer(value: Any, field_name: str, min_value: int = 1) -> int:
        """
        Validate that a value is a positive integer

        Args:
            value: Value to validate
            field_name: Name of the field for error messages
            min_value: Minimum allowed value

        Returns:
            Integer value

        Raises:
            ValidationError: If value is not a valid positive integer
        """
        try:
            int_value = int(value)
        except (ValueError, TypeError):
            raise ValidationError(f"{field_name} must be an integer")

        if int_value < min_value:
            raise ValidationError(f"{field_name} must be at least {min_value}")

        return int_value

    @staticmethod
    def validate_coordinates(lat: Any, lng: Any) -> Tuple[float, float]:
        """
        Validate latitude and longitude coordinates

        Args:
            lat: Latitude value
            lng: Longitude value

        Returns:
            Tuple of (latitude, longitude) as floats

        Raises:
            ValidationError: If coordinates are invalid
        """
        try:
            lat_float = float(lat)
            lng_float = float(lng)
        except (ValueError, TypeError):
            raise ValidationError("Latitude and longitude must be valid numbers")

        if not (-90 <= lat_float <= 90):
            raise ValidationError("Latitude must be between -90 and 90 degrees")

        if not (-180 <= lng_float <= 180):
            raise ValidationError("Longitude must be between -180 and 180 degrees")

        return lat_float, lng_float


class FilterValidator:
    """Validator for common filter patterns"""

    @staticmethod
    def build_date_filter_query(
        query, date_field, start_date: Optional[datetime] = None, end_date: Optional[datetime] = None
    ):
        """
        Apply date range filters to a SQLAlchemy query

        Args:
            query: SQLAlchemy query object
            date_field: Database field to filter on
            start_date: Start date filter
            end_date: End date filter

        Returns:
            Modified query object
        """
        if start_date:
            query = query.filter(date_field >= start_date)

        if end_date:
            query = query.filter(date_field <= end_date)

        return query

    @staticmethod
    def build_status_filter_query(query, status_field, status_value: Optional[Any] = None):
        """
        Apply status filter to a SQLAlchemy query

        Args:
            query: SQLAlchemy query object
            status_field: Database field to filter on
            status_value: Status value to filter by

        Returns:
            Modified query object
        """
        if status_value:
            query = query.filter(status_field == status_value)

        return query


class PaginationHelper:
    """Helper for building consistent pagination responses"""

    @staticmethod
    def build_pagination_response(items: List[Any], pagination, item_serializer=None) -> Dict[str, Any]:
        """
        Build a standardized pagination response

        Args:
            items: List of items from pagination
            pagination: SQLAlchemy pagination object
            item_serializer: Function to serialize individual items

        Returns:
            Dictionary with items and pagination metadata
        """
        if item_serializer:
            serialized_items = [item_serializer(item) for item in items]
        else:
            # Assume items have a to_dict method or are already serializable
            serialized_items = [item.to_dict() if hasattr(item, "to_dict") else item for item in items]

        return {
            "items": serialized_items,
            "pagination": {
                "page": pagination.page,
                "pages": pagination.pages,
                "per_page": pagination.per_page,
                "total": pagination.total,
                "has_next": pagination.has_next,
                "has_prev": pagination.has_prev,
            },
        }


# Convenience functions for common validation scenarios
def validate_list_request_params(
    default_per_page: int = 20,
    max_per_page: int = 50,
    allow_status_filter: bool = True,
    status_enum=None,
    allow_date_filter: bool = True,
    allow_future_dates: bool = True,
) -> Dict[str, Any]:
    """
    Validate common parameters for list endpoints

    Args:
        default_per_page: Default items per page
        max_per_page: Maximum items per page
        allow_status_filter: Whether to validate status parameter
        status_enum: Enum class for status validation
        allow_date_filter: Whether to validate date range parameters
        allow_future_dates: Whether to allow future dates

    Returns:
        Dictionary with validated parameters
    """
    # Validate pagination
    page, per_page = PaginationValidator.validate_pagination_params(
        default_per_page=default_per_page, max_per_page=max_per_page
    )

    result = {"page": page, "per_page": per_page, "user_id": RequestDataValidator.get_current_user_id()}

    # Validate status filter
    if allow_status_filter and status_enum:
        status_str = request.args.get("status")
        result["status"] = StatusValidator.validate_status_enum(status_str, status_enum)

    # Validate date filters
    if allow_date_filter:
        start_date_str = request.args.get("start_date")
        end_date_str = request.args.get("end_date")
        start_date, end_date = DateValidator.validate_date_range(start_date_str, end_date_str, allow_future_dates)
        result["start_date"] = start_date
        result["end_date"] = end_date

    return result
