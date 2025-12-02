"""
Standardized API Response Helpers
Provides type-safe, consistent response formats for all API endpoints
"""
from typing import Any, Dict, List, Optional, Union
from flask import jsonify
from pydantic import BaseModel, Field, ConfigDict


class APIResponse(BaseModel):
    """Standard API response model"""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    success: bool = Field(..., description="Whether the request was successful")
    message: Optional[str] = Field(None, description="Human-readable message")
    data: Optional[Any] = Field(None, description="Response data payload")
    errors: Optional[List[str]] = Field(None, description="List of error messages")
    meta: Optional[Dict[str, Any]] = Field(None, description="Metadata (pagination, etc.)")


class PaginationMeta(BaseModel):
    """Pagination metadata"""
    page: int = Field(..., ge=1, description="Current page number")
    per_page: int = Field(..., ge=1, le=100, description="Items per page")
    total: int = Field(..., ge=0, description="Total number of items")
    pages: int = Field(..., ge=0, description="Total number of pages")
    has_next: bool = Field(..., description="Whether there is a next page")
    has_prev: bool = Field(..., description="Whether there is a previous page")


def success_response(
    data: Any = None,
    message: str = None,
    meta: Dict[str, Any] = None,
    status_code: int = 200
):
    """
    Create a successful API response

    Args:
        data: Response data payload
        message: Optional success message
        meta: Optional metadata (pagination, etc.)
        status_code: HTTP status code (default: 200)

    Returns:
        Flask response tuple (json, status_code)

    Example:
        return success_response(
            data={'order': order_data},
            message='Order created successfully',
            status_code=201
        )
    """
    response = APIResponse(
        success=True,
        message=message,
        data=data,
        meta=meta
    )
    return jsonify(response.model_dump(exclude_none=True)), status_code


def error_response(
    message: str,
    errors: Union[List[str], str, None] = None,
    status_code: int = 400,
    data: Any = None
):
    """
    Create an error API response

    Args:
        message: Main error message
        errors: Additional error details (can be string or list)
        status_code: HTTP status code (default: 400)
        data: Optional data to include with error

    Returns:
        Flask response tuple (json, status_code)

    Example:
        return error_response(
            message='Validation failed',
            errors=['Email is required', 'Phone number invalid'],
            status_code=400
        )
    """
    # Convert single error string to list
    if isinstance(errors, str):
        errors = [errors]

    response = APIResponse(
        success=False,
        message=message,
        errors=errors,
        data=data
    )
    return jsonify(response.model_dump(exclude_none=True)), status_code


def paginated_response(
    items: List[Any],
    page: int,
    per_page: int,
    total: int,
    message: str = None,
    additional_meta: Dict[str, Any] = None
):
    """
    Create a paginated API response

    Args:
        items: List of items for current page
        page: Current page number
        per_page: Items per page
        total: Total number of items
        message: Optional message
        additional_meta: Additional metadata to include

    Returns:
        Flask response tuple (json, status_code)

    Example:
        return paginated_response(
            items=orders,
            page=1,
            per_page=20,
            total=150,
            message='Orders retrieved successfully'
        )
    """
    import math

    pages = math.ceil(total / per_page) if per_page > 0 else 0

    pagination = PaginationMeta(
        page=page,
        per_page=per_page,
        total=total,
        pages=pages,
        has_next=page < pages,
        has_prev=page > 1
    )

    meta = pagination.model_dump()

    # Add any additional metadata
    if additional_meta:
        meta.update(additional_meta)

    return success_response(
        data={'items': items},
        message=message,
        meta=meta,
        status_code=200
    )


def created_response(data: Any = None, message: str = 'Resource created successfully'):
    """
    Create a 201 Created response

    Args:
        data: Created resource data
        message: Success message

    Returns:
        Flask response tuple (json, 201)
    """
    return success_response(data=data, message=message, status_code=201)


def no_content_response():
    """
    Create a 204 No Content response

    Returns:
        Flask response tuple ('', 204)
    """
    return '', 204


def not_found_response(message: str = 'Resource not found', resource_type: str = None):
    """
    Create a 404 Not Found response

    Args:
        message: Error message
        resource_type: Type of resource that wasn't found

    Returns:
        Flask response tuple (json, 404)
    """
    if resource_type:
        message = f"{resource_type} not found"

    return error_response(message=message, status_code=404)


def unauthorized_response(message: str = 'Authentication required'):
    """
    Create a 401 Unauthorized response

    Args:
        message: Error message

    Returns:
        Flask response tuple (json, 401)
    """
    return error_response(message=message, status_code=401)


def forbidden_response(message: str = 'Access forbidden'):
    """
    Create a 403 Forbidden response

    Args:
        message: Error message

    Returns:
        Flask response tuple (json, 403)
    """
    return error_response(message=message, status_code=403)


def validation_error_response(errors: Union[List[str], Dict[str, Any], str]):
    """
    Create a 400 Bad Request response for validation errors

    Args:
        errors: Validation error details

    Returns:
        Flask response tuple (json, 400)

    Example:
        # From Pydantic ValidationError
        try:
            data = CreateOrderRequest(**request.get_json())
        except ValidationError as e:
            return validation_error_response(e.errors())
    """
    error_list = []

    if isinstance(errors, list):
        # Handle Pydantic validation errors
        for error in errors:
            if isinstance(error, dict):
                field = ' -> '.join(str(loc) for loc in error.get('loc', []))
                msg = error.get('msg', 'Validation error')
                error_list.append(f"{field}: {msg}" if field else msg)
            else:
                error_list.append(str(error))
    elif isinstance(errors, dict):
        # Handle dictionary of field errors
        for field, msg in errors.items():
            error_list.append(f"{field}: {msg}")
    else:
        error_list = [str(errors)]

    return error_response(
        message='Validation failed',
        errors=error_list,
        status_code=400
    )


def conflict_response(message: str = 'Resource already exists'):
    """
    Create a 409 Conflict response

    Args:
        message: Error message

    Returns:
        Flask response tuple (json, 409)
    """
    return error_response(message=message, status_code=409)


def internal_error_response(message: str = 'Internal server error', error_id: str = None):
    """
    Create a 500 Internal Server Error response

    Args:
        message: Error message
        error_id: Optional error tracking ID

    Returns:
        Flask response tuple (json, 500)
    """
    meta = {'error_id': error_id} if error_id else None

    response = APIResponse(
        success=False,
        message=message,
        meta=meta
    )
    return jsonify(response.model_dump(exclude_none=True)), 500


# Export all response functions
__all__ = [
    'APIResponse',
    'PaginationMeta',
    'success_response',
    'error_response',
    'paginated_response',
    'created_response',
    'no_content_response',
    'not_found_response',
    'unauthorized_response',
    'forbidden_response',
    'validation_error_response',
    'conflict_response',
    'internal_error_response'
]
