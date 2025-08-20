"""
Enhanced Swagger/OpenAPI configuration for BlueStream API
Provides comprehensive API documentation with schemas, security, and examples
"""
from flasgger import LazyString


def get_swagger_template():
    """Get comprehensive Swagger template with all schemas and configurations"""
    return {
        "swagger": "2.0",
        "info": {
            "title": "BlueStream Water Delivery Platform API",
            "version": "1.0.0",
            "description": """
# BlueStream Water Delivery Platform API

Complete REST API for the BlueStream water delivery platform. This API provides endpoints for:

- **Authentication & Authorization**: User registration, login, JWT token management
- **User Management**: Profile management, address management, preferences
- **Product Catalog**: Browse products, categories, pricing, availability
- **Order Management**: Create orders, track status, order history
- **Payment Processing**: Multiple payment methods, refunds, transaction history
- **Delivery Management**: Schedule deliveries, track drivers, delivery zones
- **Loyalty Program**: Points, rewards, referrals
- **Subscriptions**: Recurring orders, subscription management
- **Notifications**: Real-time updates, preferences
- **Analytics**: Order statistics, user insights (admin only)

## Authentication

The API uses JWT (JSON Web Tokens) for authentication. Include the token in the Authorization header:

```
Authorization: Bearer <your_jwt_token>
```

## Rate Limiting

API endpoints are rate-limited to prevent abuse:
- Authentication endpoints: 10-20 requests per hour
- General API endpoints: 1000 requests per hour
- Admin endpoints: 500 requests per hour

## Error Handling

All API responses follow a consistent format:

```json
{
  "success": boolean,
  "message": "Human readable message",
  "data": {}, // Present on success
  "errors": [], // Present on validation errors
  "error_code": "ERROR_CODE" // Present on specific errors
}
```

## Versioning

The current API version is v1. All endpoints are prefixed with `/api/v1`.

## Support

For API support, contact: support@bluestream.uz
            """,
            "termsOfService": "https://bluestream.uz/terms",
            "contact": {
                "name": "BlueStream API Support",
                "url": "https://bluestream.uz/support",
                "email": "api-support@bluestream.uz"
            },
            "license": {
                "name": "Proprietary",
                "url": "https://bluestream.uz/license"
            }
        },
        "host": LazyString(lambda: "api.bluestream.uz"),
        "basePath": "/api/v1",
        "schemes": ["https", "http"],
        "consumes": ["application/json"],
        "produces": ["application/json"],
        "securityDefinitions": {
            "Bearer": {
                "type": "apiKey",
                "name": "Authorization",
                "in": "header",
                "description": "JWT Authorization header. Format: 'Bearer {token}'"
            },
            "AdminRole": {
                "type": "apiKey",
                "name": "X-Admin-Token",
                "in": "header",
                "description": "Admin access token for administrative endpoints"
            }
        },
        "security": [
            {"Bearer": []}
        ],
        "tags": [
            {
                "name": "Authentication",
                "description": "User authentication and authorization endpoints"
            },
            {
                "name": "Users",
                "description": "User profile and account management"
            },
            {
                "name": "Products",
                "description": "Product catalog and inventory management"
            },
            {
                "name": "Orders",
                "description": "Order creation, management and tracking"
            },
            {
                "name": "Payments",
                "description": "Payment processing and transaction management"
            },
            {
                "name": "Delivery",
                "description": "Delivery scheduling and tracking"
            },
            {
                "name": "Loyalty",
                "description": "Loyalty program and rewards management"
            },
            {
                "name": "Subscriptions",
                "description": "Recurring order subscriptions"
            },
            {
                "name": "Notifications",
                "description": "Push notifications and alerts"
            },
            {
                "name": "Analytics",
                "description": "Business analytics and reporting (Admin only)"
            },
            {
                "name": "Admin",
                "description": "Administrative endpoints (Admin only)"
            }
        ],
        "definitions": {
            "User": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "example": 123,
                        "description": "Unique user identifier"
                    },
                    "email": {
                        "type": "string",
                        "format": "email",
                        "example": "user@example.com",
                        "description": "User's email address"
                    },
                    "phone": {
                        "type": "string",
                        "example": "+998901234567",
                        "description": "User's phone number"
                    },
                    "first_name": {
                        "type": "string",
                        "example": "John",
                        "description": "User's first name"
                    },
                    "last_name": {
                        "type": "string",
                        "example": "Doe",
                        "description": "User's last name"
                    },
                    "date_of_birth": {
                        "type": "string",
                        "format": "date",
                        "example": "1990-01-01",
                        "description": "User's date of birth"
                    },
                    "gender": {
                        "type": "string",
                        "enum": ["male", "female"],
                        "example": "male",
                        "description": "User's gender"
                    },
                    "role": {
                        "type": "string",
                        "enum": ["customer", "admin", "delivery_driver", "support"],
                        "example": "customer",
                        "description": "User's role in the system"
                    },
                    "status": {
                        "type": "string",
                        "enum": ["active", "inactive", "suspended"],
                        "example": "active",
                        "description": "User's account status"
                    },
                    "email_verified": {
                        "type": "boolean",
                        "example": True,
                        "description": "Whether user's email is verified"
                    },
                    "phone_verified": {
                        "type": "boolean",
                        "example": False,
                        "description": "Whether user's phone is verified"
                    },
                    "loyalty_points": {
                        "type": "integer",
                        "example": 150,
                        "description": "User's current loyalty points"
                    },
                    "created_at": {
                        "type": "string",
                        "format": "date-time",
                        "example": "2024-01-01T12:00:00Z",
                        "description": "Account creation timestamp"
                    }
                }
            },
            "Product": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "example": 1,
                        "description": "Unique product identifier"
                    },
                    "name": {
                        "type": "string",
                        "example": "Pure Water 19L",
                        "description": "Product name"
                    },
                    "description": {
                        "type": "string",
                        "example": "Premium pure water in 19L bottle",
                        "description": "Product description"
                    },
                    "category": {
                        "type": "string",
                        "example": "water",
                        "description": "Product category"
                    },
                    "size": {
                        "type": "string",
                        "enum": ["small", "medium", "large"],
                        "example": "large",
                        "description": "Product size"
                    },
                    "volume": {
                        "type": "number",
                        "format": "decimal",
                        "example": 19.00,
                        "description": "Product volume"
                    },
                    "volume_unit": {
                        "type": "string",
                        "example": "L",
                        "description": "Volume unit (L, ML, etc.)"
                    },
                    "base_price": {
                        "type": "number",
                        "format": "decimal",
                        "example": 15000.00,
                        "description": "Base price in UZS"
                    },
                    "stock_quantity": {
                        "type": "integer",
                        "example": 100,
                        "description": "Available stock quantity"
                    },
                    "is_active": {
                        "type": "boolean",
                        "example": True,
                        "description": "Whether product is active and available"
                    },
                    "image_url": {
                        "type": "string",
                        "format": "uri",
                        "example": "https://cdn.bluestream.uz/products/water-19l.jpg",
                        "description": "Product image URL"
                    }
                }
            },
            "Order": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "example": 12345,
                        "description": "Unique order identifier"
                    },
                    "order_number": {
                        "type": "string",
                        "example": "ORD-2024-001234",
                        "description": "Human-readable order number"
                    },
                    "user_id": {
                        "type": "integer",
                        "example": 123,
                        "description": "ID of the user who placed the order"
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "confirmed", "processing", "out_for_delivery", "delivered", "cancelled"],
                        "example": "confirmed",
                        "description": "Current order status"
                    },
                    "items": {
                        "type": "array",
                        "items": {
                            "$ref": "#/definitions/OrderItem"
                        },
                        "description": "List of items in the order"
                    },
                    "subtotal": {
                        "type": "number",
                        "format": "decimal",
                        "example": 30000.00,
                        "description": "Subtotal amount in UZS"
                    },
                    "delivery_fee": {
                        "type": "number",
                        "format": "decimal",
                        "example": 3000.00,
                        "description": "Delivery fee in UZS"
                    },
                    "discount_amount": {
                        "type": "number",
                        "format": "decimal",
                        "example": 2000.00,
                        "description": "Total discount amount in UZS"
                    },
                    "total_amount": {
                        "type": "number",
                        "format": "decimal",
                        "example": 31000.00,
                        "description": "Final total amount in UZS"
                    },
                    "delivery_address": {
                        "$ref": "#/definitions/Address"
                    },
                    "delivery_time_slot": {
                        "$ref": "#/definitions/TimeSlot"
                    },
                    "notes": {
                        "type": "string",
                        "example": "Please call before delivery",
                        "description": "Special delivery instructions"
                    },
                    "created_at": {
                        "type": "string",
                        "format": "date-time",
                        "example": "2024-01-01T12:00:00Z",
                        "description": "Order creation timestamp"
                    },
                    "estimated_delivery": {
                        "type": "string",
                        "format": "date-time",
                        "example": "2024-01-01T16:00:00Z",
                        "description": "Estimated delivery time"
                    }
                }
            },
            "OrderItem": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "example": 1,
                        "description": "Order item identifier"
                    },
                    "product_id": {
                        "type": "integer",
                        "example": 1,
                        "description": "Product identifier"
                    },
                    "product": {
                        "$ref": "#/definitions/Product"
                    },
                    "quantity": {
                        "type": "integer",
                        "minimum": 1,
                        "example": 2,
                        "description": "Quantity ordered"
                    },
                    "unit_price": {
                        "type": "number",
                        "format": "decimal",
                        "example": 15000.00,
                        "description": "Price per unit in UZS"
                    },
                    "total_price": {
                        "type": "number",
                        "format": "decimal",
                        "example": 30000.00,
                        "description": "Total price for this item in UZS"
                    }
                }
            },
            "Address": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "example": 1,
                        "description": "Address identifier"
                    },
                    "address_line1": {
                        "type": "string",
                        "example": "123 Main Street, Apt 4B",
                        "description": "Primary address line"
                    },
                    "address_line2": {
                        "type": "string",
                        "example": "Near Metro Station",
                        "description": "Secondary address line (optional)"
                    },
                    "city": {
                        "type": "string",
                        "example": "Tashkent",
                        "description": "City name"
                    },
                    "district": {
                        "type": "string",
                        "example": "Chilanzar",
                        "description": "District or area"
                    },
                    "postal_code": {
                        "type": "string",
                        "example": "100000",
                        "description": "Postal code"
                    },
                    "latitude": {
                        "type": "number",
                        "format": "float",
                        "example": 41.2995,
                        "description": "GPS latitude coordinate"
                    },
                    "longitude": {
                        "type": "number",
                        "format": "float",
                        "example": 69.2401,
                        "description": "GPS longitude coordinate"
                    },
                    "is_default": {
                        "type": "boolean",
                        "example": True,
                        "description": "Whether this is the default address"
                    }
                }
            },
            "TimeSlot": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "example": 1,
                        "description": "Time slot identifier"
                    },
                    "name": {
                        "type": "string",
                        "example": "Morning",
                        "description": "Time slot name"
                    },
                    "time_range": {
                        "type": "string",
                        "example": "09:00-12:00",
                        "description": "Time range for delivery"
                    },
                    "is_available": {
                        "type": "boolean",
                        "example": True,
                        "description": "Whether this slot is available"
                    }
                }
            },
            "Payment": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "example": 1,
                        "description": "Payment identifier"
                    },
                    "order_id": {
                        "type": "integer",
                        "example": 12345,
                        "description": "Associated order ID"
                    },
                    "payment_method": {
                        "type": "string",
                        "enum": ["card", "cash", "payme", "click"],
                        "example": "card",
                        "description": "Payment method used"
                    },
                    "provider": {
                        "type": "string",
                        "example": "uzcard",
                        "description": "Payment provider"
                    },
                    "amount": {
                        "type": "number",
                        "format": "decimal",
                        "example": 31000.00,
                        "description": "Payment amount in UZS"
                    },
                    "currency": {
                        "type": "string",
                        "example": "UZS",
                        "description": "Payment currency"
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "processing", "completed", "failed", "refunded"],
                        "example": "completed",
                        "description": "Payment status"
                    },
                    "transaction_id": {
                        "type": "string",
                        "example": "TXN-ABC123456",
                        "description": "External transaction ID"
                    },
                    "created_at": {
                        "type": "string",
                        "format": "date-time",
                        "example": "2024-01-01T12:00:00Z",
                        "description": "Payment creation timestamp"
                    }
                }
            },
            "Tokens": {
                "type": "object",
                "properties": {
                    "access_token": {
                        "type": "string",
                        "example": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
                        "description": "JWT access token"
                    },
                    "refresh_token": {
                        "type": "string",
                        "example": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
                        "description": "JWT refresh token"
                    },
                    "token_type": {
                        "type": "string",
                        "example": "Bearer",
                        "description": "Token type"
                    },
                    "expires_in": {
                        "type": "integer",
                        "example": 3600,
                        "description": "Token expiration time in seconds"
                    }
                }
            },
            "Error": {
                "type": "object",
                "properties": {
                    "success": {
                        "type": "boolean",
                        "example": False,
                        "description": "Always false for errors"
                    },
                    "message": {
                        "type": "string",
                        "example": "Validation failed",
                        "description": "Human-readable error message"
                    },
                    "error_code": {
                        "type": "string",
                        "example": "VALIDATION_ERROR",
                        "description": "Machine-readable error code"
                    },
                    "errors": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {
                                    "type": "string",
                                    "example": "email",
                                    "description": "Field that caused the error"
                                },
                                "message": {
                                    "type": "string",
                                    "example": "Invalid email format",
                                    "description": "Specific error message"
                                }
                            }
                        },
                        "description": "Detailed error information"
                    }
                }
            },
            "Success": {
                "type": "object",
                "properties": {
                    "success": {
                        "type": "boolean",
                        "example": True,
                        "description": "Always true for successful responses"
                    },
                    "message": {
                        "type": "string",
                        "example": "Operation completed successfully",
                        "description": "Human-readable success message"
                    },
                    "data": {
                        "type": "object",
                        "description": "Response data (varies by endpoint)"
                    }
                }
            },
            "Pagination": {
                "type": "object",
                "properties": {
                    "page": {
                        "type": "integer",
                        "example": 1,
                        "description": "Current page number"
                    },
                    "per_page": {
                        "type": "integer",
                        "example": 20,
                        "description": "Items per page"
                    },
                    "total_pages": {
                        "type": "integer",
                        "example": 5,
                        "description": "Total number of pages"
                    },
                    "total_items": {
                        "type": "integer",
                        "example": 100,
                        "description": "Total number of items"
                    },
                    "has_next": {
                        "type": "boolean",
                        "example": True,
                        "description": "Whether there is a next page"
                    },
                    "has_prev": {
                        "type": "boolean",
                        "example": False,
                        "description": "Whether there is a previous page"
                    }
                }
            }
        },
        "responses": {
            "BadRequest": {
                "description": "Bad request - validation error",
                "schema": {
                    "$ref": "#/definitions/Error"
                }
            },
            "Unauthorized": {
                "description": "Unauthorized - authentication required",
                "schema": {
                    "$ref": "#/definitions/Error"
                }
            },
            "Forbidden": {
                "description": "Forbidden - insufficient permissions",
                "schema": {
                    "$ref": "#/definitions/Error"
                }
            },
            "NotFound": {
                "description": "Resource not found",
                "schema": {
                    "$ref": "#/definitions/Error"
                }
            },
            "Conflict": {
                "description": "Conflict - resource already exists",
                "schema": {
                    "$ref": "#/definitions/Error"
                }
            },
            "TooManyRequests": {
                "description": "Rate limit exceeded",
                "schema": {
                    "$ref": "#/definitions/Error"
                }
            },
            "InternalServerError": {
                "description": "Internal server error",
                "schema": {
                    "$ref": "#/definitions/Error"
                }
            }
        },
        "parameters": {
            "PageParameter": {
                "name": "page",
                "in": "query",
                "type": "integer",
                "minimum": 1,
                "default": 1,
                "description": "Page number for pagination"
            },
            "PerPageParameter": {
                "name": "per_page",
                "in": "query",
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 20,
                "description": "Number of items per page"
            },
            "SortParameter": {
                "name": "sort",
                "in": "query",
                "type": "string",
                "description": "Sort field and direction (e.g., 'created_at:desc')"
            },
            "SearchParameter": {
                "name": "search",
                "in": "query",
                "type": "string",
                "description": "Search query string"
            }
        }
    }


def get_swagger_config():
    """Get Swagger UI configuration"""
    return {
        "headers": [],
        "specs": [
            {
                "endpoint": 'apispec_1',
                "route": '/apispec_1.json',
                "rule_filter": lambda rule: True,
                "model_filter": lambda tag: True,
            }
        ],
        "static_url_path": "/flasgger_static",
        "swagger_ui": True,
        "specs_route": "/docs",
        "doc_dir": "./docs/swagger/",
        "swagger_ui_config": {
            "docExpansion": "list",
            "defaultModelsExpandDepth": 3,
            "defaultModelExpandDepth": 3,
            "displayRequestDuration": True,
            "filter": True,
            "showExtensions": True,
            "showCommonExtensions": True,
            "tryItOutEnabled": True,
            "requestInterceptor": "(request) => { request.credentials = 'include'; return request; }",
            "supportedSubmitMethods": ["get", "post", "put", "delete", "patch"],
            "validatorUrl": None,  # Disable validator
            "layout": "BaseLayout",
            "deepLinking": True,
            "displayOperationId": False,
            "defaultModelRendering": "example",
            "showRequestHeaders": True,
            "showResponseHeaders": True,
            "tagsSorter": "alpha",
            "operationsSorter": "alpha"
        }
    }