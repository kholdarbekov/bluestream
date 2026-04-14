# BlueStream API Documentation

Complete documentation for the BlueStream Water Delivery Platform REST API.

## Overview

The BlueStream API is a RESTful web service that provides access to all platform functionality including user management, product catalog, order processing, payments, and delivery tracking.

## Base Information

- **Base URL**: `https://api.aqua-element.uz/api/v1`
- **Protocol**: HTTPS
- **Authentication**: JWT Bearer Tokens
- **Content Type**: `application/json`
- **Documentation**: Available at `/docs` (Swagger UI)

## Authentication

### JWT Token Authentication

All authenticated endpoints require a JWT token in the Authorization header:

```http
Authorization: Bearer <your_jwt_token>
```

### Obtaining Tokens

1. **Registration**: `POST /auth/register` - Returns access and refresh tokens
2. **Login**: `POST /auth/login` - Returns access and refresh tokens  
3. **Refresh**: `POST /auth/refresh` - Returns new access token

### Token Lifecycle

- **Access Token**: Valid for 1 hour
- **Refresh Token**: Valid for 30 days (7 days in production)
- **Cookie Support**: Tokens are also set as httpOnly cookies for web clients

## Rate Limiting

The API implements rate limiting to prevent abuse:

| Endpoint Type | Limit | Window |
|--------------|-------|---------|
| Authentication | 10-20 requests | 1 hour |
| General API | 1000 requests | 1 hour |
| Admin Endpoints | 500 requests | 1 hour |
| Password Reset | 5 requests | 1 hour |

Rate limit headers are included in responses:
- `X-RateLimit-Limit`: Maximum requests allowed
- `X-RateLimit-Remaining`: Requests remaining in window
- `X-RateLimit-Reset`: Time when limit resets

## Error Handling

All API responses follow a consistent format:

### Success Response Format

```json
{
  "success": true,
  "message": "Operation completed successfully",
  "data": {
    // Response data here
  }
}
```

### Error Response Format

```json
{
  "success": false,
  "message": "Human readable error message",
  "error_code": "MACHINE_READABLE_CODE",
  "errors": [
    {
      "field": "email",
      "message": "Invalid email format"
    }
  ]
}
```

### HTTP Status Codes

| Code | Meaning | Description |
|------|---------|-------------|
| 200 | OK | Request successful |
| 201 | Created | Resource created successfully |
| 400 | Bad Request | Invalid request data |
| 401 | Unauthorized | Authentication required |
| 403 | Forbidden | Insufficient permissions |
| 404 | Not Found | Resource not found |
| 409 | Conflict | Resource already exists |
| 422 | Unprocessable Entity | Validation failed |
| 429 | Too Many Requests | Rate limit exceeded |
| 500 | Internal Server Error | Server error |

### Common Error Codes

| Error Code | Description |
|------------|-------------|
| `VALIDATION_ERROR` | Request validation failed |
| `AUTHENTICATION_REQUIRED` | Valid JWT token required |
| `INSUFFICIENT_PERMISSIONS` | User lacks required permissions |
| `RESOURCE_NOT_FOUND` | Requested resource doesn't exist |
| `RESOURCE_CONFLICT` | Resource already exists |
| `RATE_LIMIT_EXCEEDED` | Too many requests |
| `PAYMENT_FAILED` | Payment processing failed |
| `INSUFFICIENT_STOCK` | Product out of stock |
| `DELIVERY_UNAVAILABLE` | Delivery not available to address |

## API Endpoints

### Authentication Endpoints

#### POST /auth/register
Register a new user account.

**Request:**
```json
{
  "email": "user@example.com",
  "password": "SecurePassword123!",
  "phone": "+998901234567",
  "first_name": "John",
  "last_name": "Doe",
  "date_of_birth": "1990-01-01",
  "gender": "male",
  "referral_code": "ABC123"
}
```

**Response (201):**
```json
{
  "success": true,
  "message": "Registration successful",
  "data": {
    "user": {
      "id": 123,
      "email": "user@example.com",
      "phone": "+998901234567",
      "first_name": "John",
      "last_name": "Doe",
      "status": "active",
      "email_verified": false,
      "phone_verified": false
    },
    "tokens": {
      "access_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
      "refresh_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
      "token_type": "Bearer",
      "expires_in": 3600
    }
  }
}
```

#### POST /auth/login
Authenticate user and return tokens.

**Request:**
```json
{
  "identifier": "user@example.com",
  "password": "SecurePassword123!"
}
```

**Response (200):**
```json
{
  "success": true,
  "message": "Login successful",
  "data": {
    "user": { /* User object */ },
    "tokens": { /* Token object */ }
  }
}
```

#### POST /auth/refresh
Refresh access token using refresh token.

**Request:**
```json
{
  "refresh_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..."
}
```

### Product Endpoints

#### GET /products
Get paginated list of products.

**Query Parameters:**
- `page` (int): Page number (default: 1)
- `per_page` (int): Items per page (default: 20, max: 100)
- `category` (string): Filter by category
- `search` (string): Search in name and description
- `sort` (string): Sort field and direction (e.g., "price:asc", "name:desc")
- `min_price` (number): Minimum price filter
- `max_price` (number): Maximum price filter
- `in_stock` (boolean): Filter by stock availability

**Response (200):**
```json
{
  "success": true,
  "message": "Products retrieved successfully",
  "data": {
    "products": [
      {
        "id": 1,
        "name": "Pure Water 19L",
        "description": "Premium pure water in 19L bottle",
        "category": "water",
        "size": "large",
        "volume": 19.00,
        "volume_unit": "L",
        "base_price": 15000.00,
        "stock_quantity": 100,
        "is_active": true,
        "image_url": "https://cdn.aqua-element.uz/products/water-19l.jpg"
      }
    ],
    "pagination": {
      "page": 1,
      "per_page": 20,
      "total_pages": 5,
      "total_items": 100,
      "has_next": true,
      "has_prev": false
    }
  }
}
```

#### GET /products/{id}
Get detailed information about a specific product.

**Response (200):**
```json
{
  "success": true,
  "message": "Product retrieved successfully",
  "data": {
    "product": {
      "id": 1,
      "name": "Pure Water 19L",
      "description": "Premium pure water in 19L bottle",
      "category": "water",
      "size": "large",
      "volume": 19.00,
      "volume_unit": "L",
      "base_price": 15000.00,
      "stock_quantity": 100,
      "min_stock_level": 10,
      "is_active": true,
      "image_url": "https://cdn.aqua-element.uz/products/water-19l.jpg",
      "created_at": "2024-01-01T12:00:00Z",
      "updated_at": "2024-01-01T12:00:00Z"
    }
  }
}
```

### Order Endpoints

#### POST /orders
Create a new order.

**Authentication:** Required  
**Request:**
```json
{
  "items": [
    {
      "product_id": 1,
      "quantity": 2,
      "unit_price": 15000.00
    }
  ],
  "delivery_address": {
    "address_line1": "123 Main Street, Apt 4B",
    "city": "Tashkent",
    "district": "Chilanzar",
    "latitude": 41.2995,
    "longitude": 69.2401
  },
  "delivery_time_slot_id": 1,
  "notes": "Please call before delivery",
  "payment_method": "card"
}
```

**Response (201):**
```json
{
  "success": true,
  "message": "Order created successfully",
  "data": {
    "order": {
      "id": 12345,
      "order_number": "ORD-2024-001234",
      "status": "pending",
      "items": [ /* Order items */ ],
      "subtotal": 30000.00,
      "delivery_fee": 3000.00,
      "discount_amount": 0.00,
      "total_amount": 33000.00,
      "delivery_address": { /* Address object */ },
      "estimated_delivery": "2024-01-01T16:00:00Z",
      "created_at": "2024-01-01T12:00:00Z"
    }
  }
}
```

#### GET /orders
Get user's orders with pagination and filtering.

**Authentication:** Required  
**Query Parameters:**
- `page` (int): Page number
- `per_page` (int): Items per page
- `status` (string): Filter by order status
- `from_date` (date): Orders from this date
- `to_date` (date): Orders until this date

#### GET /orders/{id}
Get detailed information about a specific order.

**Authentication:** Required  

#### POST /orders/{id}/cancel
Cancel an order.

**Authentication:** Required  
**Request:**
```json
{
  "reason": "Customer requested cancellation",
  "refund_requested": true
}
```

### Payment Endpoints

#### POST /orders/{order_id}/payment
Process payment for an order.

**Authentication:** Required  
**Request:**
```json
{
  "amount": 33000.00,
  "currency": "UZS",
  "payment_method": "card",
  "card_token": "card_token_from_frontend",
  "return_url": "https://app.aqua-element.uz/payment/return",
  "callback_url": "https://api.aqua-element.uz/payments/callback"
}
```

### User Management Endpoints

#### GET /users/profile
Get current user's profile information.

**Authentication:** Required  

#### PUT /users/profile
Update user profile information.

**Authentication:** Required  

#### GET /users/addresses
Get user's saved addresses.

**Authentication:** Required  

#### POST /users/addresses
Add a new address for the user.

**Authentication:** Required  

## Data Models

### User Model
```json
{
  "id": 123,
  "email": "user@example.com",
  "phone": "+998901234567",
  "first_name": "John",
  "last_name": "Doe",
  "date_of_birth": "1990-01-01",
  "gender": "male",
  "role": "customer",
  "status": "active",
  "email_verified": true,
  "phone_verified": false,
  "loyalty_points": 150,
  "created_at": "2024-01-01T12:00:00Z"
}
```

### Product Model
```json
{
  "id": 1,
  "name": "Pure Water 19L",
  "description": "Premium pure water in 19L bottle",
  "category": "water",
  "size": "large",
  "volume": 19.00,
  "volume_unit": "L",
  "base_price": 15000.00,
  "stock_quantity": 100,
  "is_active": true,
  "image_url": "https://cdn.aqua-element.uz/products/water-19l.jpg"
}
```

### Order Model
```json
{
  "id": 12345,
  "order_number": "ORD-2024-001234",
  "user_id": 123,
  "status": "confirmed",
  "items": [
    {
      "id": 1,
      "product_id": 1,
      "quantity": 2,
      "unit_price": 15000.00,
      "total_price": 30000.00
    }
  ],
  "subtotal": 30000.00,
  "delivery_fee": 3000.00,
  "discount_amount": 0.00,
  "total_amount": 33000.00,
  "delivery_address": { /* Address object */ },
  "estimated_delivery": "2024-01-01T16:00:00Z",
  "created_at": "2024-01-01T12:00:00Z"
}
```

## SDKs and Libraries

### JavaScript/Node.js
```javascript
const BlueStreamAPI = require('@bluestream/api-client');

const api = new BlueStreamAPI({
  baseURL: 'https://api.aqua-element.uz/api/v1',
  apiKey: 'your-api-key'
});

// Register user
const user = await api.auth.register({
  email: 'user@example.com',
  password: 'SecurePassword123!',
  phone: '+998901234567',
  first_name: 'John',
  last_name: 'Doe'
});

// Get products
const products = await api.products.list({
  page: 1,
  per_page: 20,
  category: 'water'
});

// Create order
const order = await api.orders.create({
  items: [{ product_id: 1, quantity: 2 }],
  delivery_address: { /* address */ }
});
```

### Python
```python
from bluestream_api import BlueStreamClient

client = BlueStreamClient(
    base_url='https://api.aqua-element.uz/api/v1',
    api_key='your-api-key'
)

# Register user
user = client.auth.register(
    email='user@example.com',
    password='SecurePassword123!',
    phone='+998901234567',
    first_name='John',
    last_name='Doe'
)

# Get products
products = client.products.list(
    page=1,
    per_page=20,
    category='water'
)

# Create order
order = client.orders.create(
    items=[{'product_id': 1, 'quantity': 2}],
    delivery_address={...}
)
```

## Testing

### Using cURL
```bash
# Register user
curl -X POST https://api.aqua-element.uz/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "user@example.com",
    "password": "SecurePassword123!",
    "phone": "+998901234567",
    "first_name": "John",
    "last_name": "Doe"
  }'

# Get products (authenticated)
curl -X GET https://api.aqua-element.uz/api/v1/products \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"

# Create order
curl -X POST https://api.aqua-element.uz/api/v1/orders \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "items": [{"product_id": 1, "quantity": 2}],
    "delivery_address": {...}
  }'
```

### Postman Collection
A complete Postman collection is available at: [BlueStream API Collection](https://api.aqua-element.uz/postman/collection.json)

## Webhooks

The API supports webhooks for real-time notifications:

### Order Status Updates
```json
{
  "event": "order.status_updated",
  "data": {
    "order_id": 12345,
    "old_status": "confirmed",
    "new_status": "out_for_delivery",
    "timestamp": "2024-01-01T14:00:00Z"
  }
}
```

### Payment Status Updates
```json
{
  "event": "payment.completed",
  "data": {
    "payment_id": 67890,
    "order_id": 12345,
    "amount": 33000.00,
    "status": "completed",
    "timestamp": "2024-01-01T12:30:00Z"
  }
}
```

## Best Practices

### 1. Authentication
- Store JWT tokens securely (httpOnly cookies for web, secure storage for mobile)
- Refresh tokens before they expire
- Implement proper logout by invalidating tokens

### 2. Error Handling
- Always check the `success` field in responses
- Handle rate limiting with exponential backoff
- Implement retry logic for transient errors

### 3. Performance
- Use pagination for list endpoints
- Implement caching where appropriate
- Minimize unnecessary API calls

### 4. Security
- Validate all user input on client side
- Use HTTPS for all API calls
- Don't expose sensitive data in URLs

## Changelog

### v1.0.0 (2024-01-01)
- Initial API release
- Authentication and user management
- Product catalog and search
- Order processing and tracking
- Payment integration
- Delivery management

## Support

For API support and questions:
- **Email**: api-support@aqua-element.uz
- **Documentation**: https://api.aqua-element.uz/docs
- **Status Page**: https://status.aqua-element.uz
- **Support Portal**: https://support.aqua-element.uz

## Rate Limits and Quotas

| Plan | Requests/Hour | Burst Limit | Concurrent Connections |
|------|---------------|-------------|----------------------|
| Free | 1,000 | 100 | 10 |
| Basic | 10,000 | 500 | 50 |
| Premium | 100,000 | 1,000 | 100 |
| Enterprise | Unlimited | 5,000 | 500 |

## Legal

- **Terms of Service**: https://aqua-element.uz/terms
- **Privacy Policy**: https://aqua-element.uz/privacy
- **API License**: https://aqua-element.uz/api-license