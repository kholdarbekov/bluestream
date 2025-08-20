"""
Pydantic Helper Functions for Flask API Integration
"""
from functools import wraps
from typing import Type, Any, Dict, Optional
from flask import request, jsonify
from pydantic import BaseModel, ValidationError


def validate_json_with_model(model_class: Type[BaseModel]):
    """
    Decorator to validate JSON request data using a Pydantic model
    
    Usage:
    @validate_json_with_model(CreateOrderRequest)
    def create_order():
        # request.validated_json contains the validated data
        validated_data = request.validated_json
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not request.is_json:
                return jsonify({'error': 'Content-Type must be application/json'}), 400
            
            try:
                json_data = request.get_json()
                if json_data is None:
                    return jsonify({'error': 'Invalid JSON data'}), 400
                
                # Validate using Pydantic model
                validated_data = model_class(**json_data)
                
                # Add validated data to request object for easy access
                request.validated_json = validated_data
                request.validated_dict = validated_data.model_dump()
                
                return f(*args, **kwargs)
                
            except ValidationError as e:
                # Extract field errors for better user experience
                errors = []
                for error in e.errors():
                    field = '.'.join(str(x) for x in error['loc'])
                    message = error['msg']
                    errors.append(f"{field}: {message}")
                
                return jsonify({
                    'error': 'Validation failed',
                    'validation_errors': errors,
                    'details': e.errors()
                }), 400
            except Exception as e:
                return jsonify({'error': f'Request processing failed: {str(e)}'}), 400
        
        return decorated_function
    return decorator


def serialize_response(data: Any, model_class: Optional[Type[BaseModel]] = None) -> Dict[str, Any]:
    """
    Serialize response data using a Pydantic model
    
    Args:
        data: The data to serialize (can be model instance, dict, or list)
        model_class: Optional Pydantic model class to use for validation
        
    Returns:
        Serialized data dictionary
    """
    if model_class:
        try:
            if isinstance(data, list):
                return [model_class.model_validate(item).model_dump() for item in data]
            else:
                return model_class.model_validate(data).model_dump()
        except Exception as e:
            # Fallback to regular serialization if model validation fails
            pass
    
    # Handle different data types
    if hasattr(data, 'model_dump'):
        return data.model_dump()
    elif isinstance(data, list):
        return [serialize_item(item) for item in data]
    elif isinstance(data, dict):
        return data
    else:
        # Try to convert to dict if it has attributes
        if hasattr(data, '__dict__'):
            return {k: v for k, v in data.__dict__.items() if not k.startswith('_')}
        return data


def serialize_item(item: Any) -> Any:
    """Helper function to serialize individual items"""
    if hasattr(item, 'model_dump'):
        return item.model_dump()
    elif hasattr(item, '__dict__'):
        return {k: v for k, v in item.__dict__.items() if not k.startswith('_')}
    else:
        return item


def create_success_response(
    message: str,
    data: Optional[Any] = None,
    status_code: int = 200,
    **extra_fields
) -> tuple:
    """
    Create a standardized success response
    
    Args:
        message: Success message
        data: Optional data to include
        status_code: HTTP status code
        **extra_fields: Additional fields to include in response
        
    Returns:
        Tuple of (response_dict, status_code)
    """
    response = {
        'success': True,
        'message': message,
        **extra_fields
    }
    
    if data is not None:
        response['data'] = serialize_response(data)
    
    return jsonify(response), status_code


def create_error_response(
    error: str,
    status_code: int = 400,
    validation_errors: Optional[list] = None,
    **extra_fields
) -> tuple:
    """
    Create a standardized error response
    
    Args:
        error: Error message
        status_code: HTTP status code
        validation_errors: Optional list of validation errors
        **extra_fields: Additional fields to include in response
        
    Returns:
        Tuple of (response_dict, status_code)
    """
    response = {
        'success': False,
        'error': error,
        **extra_fields
    }
    
    if validation_errors:
        response['validation_errors'] = validation_errors
    
    return jsonify(response), status_code


def paginated_response(
    items: list,
    pagination_obj,
    item_serializer: Optional[Type[BaseModel]] = None,
    **extra_fields
) -> Dict[str, Any]:
    """
    Create a paginated response with consistent structure
    
    Args:
        items: List of items to serialize
        pagination_obj: Flask-SQLAlchemy pagination object
        item_serializer: Optional Pydantic model for item serialization
        **extra_fields: Additional fields to include
        
    Returns:
        Dictionary with items and pagination info
    """
    serialized_items = serialize_response(items, item_serializer) if item_serializer else items
    
    return {
        'items': serialized_items,
        'pagination': {
            'page': pagination_obj.page,
            'pages': pagination_obj.pages,
            'per_page': pagination_obj.per_page,
            'total': pagination_obj.total,
            'has_next': pagination_obj.has_next,
            'has_prev': pagination_obj.has_prev
        },
        **extra_fields
    }


def handle_model_errors(func):
    """
    Decorator to handle common model-related errors
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ValidationError as e:
            errors = []
            for error in e.errors():
                field = '.'.join(str(x) for x in error['loc'])
                message = error['msg']
                errors.append(f"{field}: {message}")
            
            return create_error_response(
                'Validation failed',
                status_code=400,
                validation_errors=errors
            )
        except ValueError as e:
            return create_error_response(str(e), status_code=400)
        except Exception as e:
            return create_error_response(f'Internal server error: {str(e)}', status_code=500)
    
    return wrapper


# Additional helper functions for common patterns

def extract_filters_from_request(filter_model: Type[BaseModel]) -> Dict[str, Any]:
    """
    Extract and validate filter parameters from request args using a Pydantic model
    
    Args:
        filter_model: Pydantic model defining expected filters
        
    Returns:
        Dictionary of validated filters
    """
    try:
        # Convert request args to dict, handling multiple values
        args_dict = {}
        for key, value in request.args.items():
            # Handle boolean values
            if value.lower() in ('true', 'false'):
                args_dict[key] = value.lower() == 'true'
            # Handle numeric values
            elif value.isdigit():
                args_dict[key] = int(value)
            else:
                args_dict[key] = value
        
        # Validate using the provided model
        validated_filters = filter_model(**args_dict)
        return validated_filters.model_dump(exclude_none=True)
        
    except ValidationError:
        # Return empty dict if validation fails - let the endpoint handle it
        return {}


def serialize_database_model(model_instance, schema_class: Type[BaseModel]) -> Dict[str, Any]:
    """
    Serialize a database model instance using a Pydantic schema
    
    Args:
        model_instance: SQLAlchemy model instance
        schema_class: Pydantic schema class
        
    Returns:
        Serialized dictionary
    """
    try:
        return schema_class.model_validate(model_instance).model_dump()
    except Exception:
        # Fallback to basic serialization
        return {
            attr: getattr(model_instance, attr)
            for attr in dir(model_instance)
            if not attr.startswith('_') and not callable(getattr(model_instance, attr))
        }