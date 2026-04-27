# Standardized Error Handling System

This document outlines the comprehensive error handling standards implemented for the BlueStream Water Platform APIs.

## Overview

The platform uses a standardized error handling system that provides:
- Consistent error response formats across all APIs
- Comprehensive logging with request context
- Proper HTTP status codes for different error types
- Security-aware error sanitization
- Request tracing for debugging

## Architecture

### Core Components

1. **Error Handlers** (`utils/error_handlers.py`)
   - `handle_api_exception`: Main decorator for API endpoints
   - `ExceptionMapper`: Maps exceptions to HTTP status codes
   - `ErrorResponse`: Builds standardized error responses

2. **Custom Exceptions** (`utils/exceptions.py`)
   - Business-specific exception classes
   - Enhanced with error codes and context details
   - Structured exception hierarchy

3. **Specialized Decorators**
   - `handle_database_exceptions`: For database operations
   - `handle_external_service_exceptions`: For external API calls
   - `handle_file_operation_exceptions`: For file operations

## Usage Patterns

### Basic API Endpoint

```python
from business_app.utils.error_handlers import handle_api_exception, create_success_response
from business_app.utils.exceptions import ValidationError, NotFoundError

@api_bp.route('/resource/<int:resource_id>', methods=['GET'])
@handle_api_exception
def get_resource(resource_id):
    """Get a specific resource"""

    if resource_id <= 0:
        raise ValidationError("Resource ID must be positive")

    resource = Resource.query.get(resource_id)
    if not resource:
        raise NotFoundError("Resource not found",
                          resource_type="Resource",
                          resource_id=str(resource_id))

    return create_success_response(
        data=resource.to_dict(),
        message="Resource retrieved successfully"
    )
```

### Database Operations

```python
@api_bp.route('/resource', methods=['POST'])
@handle_api_exception
@handle_database_exceptions
def create_resource():
    """Create a new resource"""
    data = request.get_json()

    # Validation
    if not data.get('name'):
        raise ValidationError("Name is required")

    # Create resource (database exceptions handled automatically)
    resource = Resource(name=data['name'])
    db.session.add(resource)
    db.session.commit()

    return create_success_response(
        data=resource.to_dict(),
        message="Resource created successfully",
        status_code=201
    )
```

### External Service Calls

```python
@api_bp.route('/notify', methods=['POST'])
@handle_api_exception
@handle_external_service_exceptions('sms_service')
def send_notification():
    """Send notification via external service"""
    data = request.get_json()

    # External service call (exceptions handled automatically)
    result = sms_service.send_message(data['phone'], data['message'])

    return create_success_response(
        data={'message_id': result.id},
        message="Notification sent successfully"
    )
```

## Exception Types

### Business Logic Exceptions

```python
# Validation errors (400)
raise ValidationError("Invalid input data",
                     validation_errors=['Email format invalid'])

# Resource not found (404)
raise NotFoundError("User not found",
                   resource_type="User",
                   resource_id="123")

# Authentication required (401)
raise UnauthorizedError("Authentication required")

# Access forbidden (403)
raise ForbiddenError("Admin access required",
                    required_permission="admin")

# Resource conflict (409)
raise ConflictError("Email already exists",
                   conflict_type="unique_constraint")
```

### Service-Specific Exceptions

```python
# Payment processing (402)
raise PaymentError("Payment declined",
                  payment_gateway="stripe",
                  gateway_error_code="card_declined")

# Delivery operations (422)
raise DeliveryError("Delivery address unreachable",
                   delivery_stage="route_planning")

# External service failures (503)
raise ExternalServiceError("SMS service unavailable",
                          service_name="twilio")

# Rate limiting (429)
raise RateLimitError("Rate limit exceeded",
                    retry_after=60,
                    limit=100)
```

## Response Formats

### Success Response

```json
{
  "success": true,
  "status_code": 200,
  "timestamp": "2024-01-15T10:30:00Z",
  "message": "Operation completed successfully",
  "data": {
    "id": 123,
    "name": "Resource Name"
  },
  "request_id": "req_123456789"
}
```

### Error Response

```json
{
  "error": "VALIDATION_ERROR",
  "message": "Invalid input data",
  "status_code": 400,
  "timestamp": "2024-01-15T10:30:00Z",
  "path": "/api/v1/resource",
  "method": "POST",
  "details": {
    "validation_errors": ["Email format invalid"],
    "field": "email"
  },
  "request_id": "req_123456789"
}
```

## Migration Guidelines

### Step 1: Import New Error Handlers

```python
# Add to API file imports
from business_app.utils.error_handlers import (
    handle_api_exception, create_success_response,
    handle_database_exceptions, handle_external_service_exceptions
)
from business_app.utils.exceptions import (
    ValidationError, NotFoundError, UnauthorizedError,
    ForbiddenError, ConflictError, PaymentError, DeliveryError
)
```

### Step 2: Apply Decorators

```python
# Before
@api_bp.route('/endpoint', methods=['POST'])
@jwt_required()
def endpoint():
    try:
        # ... logic ...
    except Exception as e:
        return jsonify({'error': 'Failed'}), 500

# After
@api_bp.route('/endpoint', methods=['POST'])
@jwt_required()
@handle_api_exception
def endpoint():
    # ... logic ... (exceptions handled automatically)
```

### Step 3: Replace Manual Error Responses

```python
# Before
if not resource:
    return jsonify({'error': 'Resource not found'}), 404

# After
if not resource:
    raise NotFoundError("Resource not found", resource_type="Resource")
```

### Step 4: Use Standardized Success Responses

```python
# Before
return jsonify({'success': True, 'data': result})

# After
return create_success_response(data=result, message="Operation successful")
```

## Security Features

### Request Data Sanitization

The error handler automatically sanitizes sensitive data from logs:

```python
# These fields are automatically redacted in logs
SENSITIVE_KEYS = {
    'password', 'token', 'secret', 'key', 'authorization',
    'credit_card', 'card_number', 'cvv', 'pin', 'ssn'
}
```

### Error Message Security

- Production environments receive generic error messages
- Detailed error information is logged but not exposed to clients
- Stack traces are never sent to clients

## Logging and Monitoring

### Structured Logging

All errors are logged with comprehensive context:

```json
{
  "exception_type": "ValidationError",
  "exception_message": "Invalid email format",
  "endpoint": "register",
  "method": "POST",
  "path": "/api/auth/register",
  "user_id": "user_123",
  "ip_address": "192.168.1.1",
  "user_agent": "Mozilla/5.0...",
  "timestamp": "2024-01-15T10:30:00Z",
  "trace_id": "trace_123456789",
  "request_data": {"email": "[REDACTED]", "name": "John"}
}
```

### Request Tracing

Each request gets a unique trace ID for debugging:

```python
# Automatically added to request context
g.trace_id = str(uuid.uuid4())

# Included in all logs and responses
response['request_id'] = g.trace_id
```

## Best Practices

### Do's

1. **Use specific exceptions** instead of generic Exception
2. **Provide helpful error messages** with context
3. **Include relevant details** in exception details
4. **Let decorators handle exceptions** instead of try-catch blocks
5. **Use create_success_response** for consistent success format

### Don'ts

1. **Don't expose internal errors** to clients
2. **Don't log sensitive data** in error messages
3. **Don't use manual error responses** instead of exceptions
4. **Don't ignore error context** when raising exceptions
5. **Don't mix old and new error handling** in the same endpoint

### Exception Selection Guide

- **ValidationError**: Bad user input, invalid data format
- **NotFoundError**: Resource doesn't exist
- **UnauthorizedError**: Authentication required
- **ForbiddenError**: Permission denied
- **ConflictError**: Resource already exists, state conflict
- **PaymentError**: Payment processing issues
- **DeliveryError**: Delivery-specific problems
- **ExternalServiceError**: Third-party service failures
- **BusinessLogicError**: Business rule violations

## Testing Error Handling

### Unit Tests

```python
def test_validation_error():
    with pytest.raises(ValidationError) as exc_info:
        endpoint_function(invalid_data)

    assert "Invalid input" in str(exc_info.value)
    assert exc_info.value.details['field'] == 'email'

def test_error_response_format():
    response = client.post('/api/resource', json={})

    assert response.status_code == 400
    data = response.get_json()
    assert data['error'] == 'VALIDATION_ERROR'
    assert 'request_id' in data
    assert 'timestamp' in data
```

### Integration Tests

```python
def test_end_to_end_error_handling():
    # Test that errors are properly handled and logged
    response = client.post('/api/resource', json={'invalid': 'data'})

    # Check response format
    assert response.status_code == 400
    data = response.get_json()
    assert data['error'] == 'VALIDATION_ERROR'

    # Check that error was logged (verify with log capture)
    assert 'ValidationError' in captured_logs
```

## Performance Considerations

- **Minimal overhead**: Error handling adds <1ms per request
- **Efficient logging**: Structured logs with minimal serialization
- **Request tracing**: UUID generation is fast and unique
- **Memory usage**: Exception objects are lightweight with context

## Migration Status

The error handling system has been implemented for:

- ✅ Authentication APIs (auth.py) - Uses existing decorators
- 🔄 Delivery APIs (delivery.py) - Partially migrated (2 endpoints)
- ❌ Payment APIs (payments.py) - Not migrated
- ❌ Order APIs (orders.py) - Not migrated
- ❌ Product APIs (products.py) - Not migrated
- ❌ Admin APIs (admin.py) - Not migrated
- ❌ Analytics APIs (analytics.py) - Not migrated
- ❌ Loyalty APIs (loyalty.py) - Not migrated
- ❌ Notification APIs (notifications.py) - Not migrated
- ❌ Subscription APIs (subscriptions.py) - Not migrated

**Estimated remaining effort**: 2-3 days for complete migration

## Next Steps

1. **Complete migration** of remaining API endpoints
2. **Update API documentation** to reflect new error formats
3. **Add monitoring dashboards** for error tracking
4. **Create error handling guides** for new developers
5. **Implement error rate alerting** for production monitoring

This standardized error handling system ensures consistent, secure, and maintainable error management across the entire BlueStream platform.
