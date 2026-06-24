"""
API client for communicating with the business application
"""
import httpx
import logging
import uuid
from typing import Dict, Any, Optional, List, Union
from dataclasses import dataclass
import asyncio
from datetime import datetime, timedelta, timezone
import ssl
import os

from config import config
from database import db_manager

logger = logging.getLogger('api_client')


class CircuitBreaker:
    """
    Simple circuit breaker to fail fast when the backend is down.

    States:
      CLOSED   – requests flow normally
      OPEN     – requests fail immediately (backend presumed down)
      HALF_OPEN – one probe request is allowed through to test recovery
    """
    CLOSED = 'closed'
    OPEN = 'open'
    HALF_OPEN = 'half_open'

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout  # seconds before OPEN -> HALF_OPEN
        self._state = self.CLOSED
        self._failure_count = 0
        self._last_failure_time: Optional[datetime] = None

    @property
    def state(self) -> str:
        if self._state == self.OPEN and self._last_failure_time:
            elapsed = (datetime.now(timezone.utc) - self._last_failure_time).total_seconds()
            if elapsed >= self.recovery_timeout:
                self._state = self.HALF_OPEN
        return self._state

    def allow_request(self) -> bool:
        """Return True if a request should be attempted."""
        return self.state != self.OPEN

    def record_success(self):
        """Record a successful request – reset to CLOSED."""
        self._failure_count = 0
        self._state = self.CLOSED

    def record_failure(self):
        """Record a failed request – open circuit after threshold."""
        self._failure_count += 1
        self._last_failure_time = datetime.now(timezone.utc)
        if self._failure_count >= self.failure_threshold:
            self._state = self.OPEN
            logger.warning(
                f"Circuit breaker OPEN after {self._failure_count} consecutive failures. "
                f"Failing fast for {self.recovery_timeout}s."
            )


@dataclass
class APIResponse:
    """API response wrapper"""
    success: bool
    data: Any = None
    error: Optional[str] = None
    status_code: Optional[int] = None


class BusinessAPIClient:
    """Client for business application API"""

    def __init__(self):
        self.base_url = config.business_api.base_url
        self.timeout = config.business_api.timeout
        self.max_retries = config.business_api.max_retries
        self.retry_delay = config.business_api.retry_delay
        self._client = None
        self._circuit_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)

    async def __aenter__(self):
        """Async context manager entry"""
        # SSL verification configuration
        ssl_verify = config.business_api.ssl_verify
        ssl_cert_path = config.business_api.ssl_cert_path

        # Log SSL configuration for debugging
        logger.info(f"SSL verification: {'enabled' if ssl_verify else 'disabled'}")
        if ssl_cert_path:
            logger.info(f"Custom SSL certificate path: {ssl_cert_path}")

        # Configure SSL verification with enhanced security
        if ssl_verify:
            if ssl_cert_path:
                # Validate custom certificate path exists
                if not os.path.exists(ssl_cert_path):
                    logger.error(f"SSL certificate file not found: {ssl_cert_path}")
                    raise FileNotFoundError(f"SSL certificate file not found: {ssl_cert_path}")

                # Use custom certificate if provided
                verify_config = ssl_cert_path
                logger.info(f"Using custom SSL certificate: {ssl_cert_path}")
            else:
                # Use system default certificates with enhanced validation
                verify_config = True
                logger.info("Using system default SSL certificates")
        else:
            # Only disable SSL verification if explicitly configured (not recommended)
            verify_config = False
            logger.warning(
                "⚠️  SSL verification is DISABLED. This is a CRITICAL SECURITY RISK and should " +
                "only be used in development environments with self-signed certificates. " +
                "Enable SSL verification in production by setting BUSINESS_API_SSL_VERIFY=true"
            )

            # Additional warning for production-like URLs
            if any(domain in self.base_url.lower() for domain in ['https://', '.com', '.org', '.net']):
                logger.error(
                    "🚨 SECURITY ALERT: SSL verification is disabled for a production-like URL. " +
                    "This creates a serious man-in-the-middle attack vulnerability!"
                )

        # Create HTTP client with enhanced security configuration
        try:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                verify=verify_config,
                follow_redirects=True,
                # Additional security headers
                headers={
                    'User-Agent': 'BlueStream-TelegramBot/1.0',
                    'Accept': 'application/json',
                    'Content-Type': 'application/json'
                },
                # Additional security configurations
                limits=httpx.Limits(
                    max_keepalive_connections=10,
                    max_connections=20,
                    keepalive_expiry=30.0
                )
            )

            # Test SSL connection if verification is enabled
            if ssl_verify and self.base_url.startswith('https://'):
                await self._test_ssl_connection()

        except Exception as e:
            logger.error(f"Failed to initialize HTTP client with SSL configuration: {e}")
            raise
        return self

    async def _test_ssl_connection(self):
        """
        Test SSL connection to verify certificate validity
        """
        try:
            logger.info("Testing SSL connection to business API...")
            # Make a simple HEAD request to test the connection
            test_response = await self._client.head(self.base_url)
            logger.info(f"SSL connection test successful. Status: {test_response.status_code}")
        except httpx.ConnectError as e:
            error_str = str(e).lower()
            if 'ssl' in error_str or 'certificate' in error_str:
                logger.error(f"SSL certificate validation failed: {e}")
                logger.error(
                    "This could indicate an invalid certificate, expired certificate, " +
                    "or man-in-the-middle attack. Check your SSL configuration."
                )
                raise
            else:
                logger.warning(f"Could not connect to business API for SSL test: {e}")
                # Don't raise for connection errors during testing, as service might not be ready
        except Exception as e:
            logger.warning(f"SSL connection test failed with unexpected error: {e}")
            # Don't raise for other errors during testing

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        if self._client:
            await self._client.aclose()

    def _get_url(self, endpoint: str) -> str:
        """Build full URL for endpoint"""
        return f"{self.base_url.rstrip('/')}{endpoint}"

    async def _make_request(self, method: str, endpoint: str,
                          headers: Optional[Dict] = None,
                          data: Optional[Dict] = None,
                          params: Optional[Dict] = None,
                          user_token: Optional[str] = None,
                          language: Optional[str] = None,
                          token_manager=None,
                          telegram_id: Optional[int] = None) -> APIResponse:
        """Make HTTP request with retry logic and circuit breaker"""
        # Circuit breaker: fail fast if backend is presumed down
        if not self._circuit_breaker.allow_request():
            logger.warning(f"Circuit breaker OPEN – failing fast for {method.upper()} {endpoint}")
            return APIResponse(
                success=False,
                error="Service temporarily unavailable (circuit breaker open)"
            )

        url = self._get_url(endpoint)

        logger.debug(f"HTTP {method.upper()} {url}")

        # Set up headers
        request_headers = headers or {}
        # Distributed tracing: generate request ID for correlation across services
        request_id = f"bot-{uuid.uuid4().hex[:12]}"
        request_headers['X-Request-ID'] = request_id

        if user_token:
            request_headers['Authorization'] = f'Bearer {user_token}'

        if language:
            request_headers['Accept-Language'] = language

        logger.debug(f"Request headers: {dict((k, '***' if k == 'Authorization' else v) for k, v in request_headers.items())}")
        if data:
            logger.debug(f"Request data: {data}")
        if params:
            logger.debug(f"Request params: {params}")

        for attempt in range(self.max_retries + 1):
            try:
                logger.debug(f"HTTP request attempt {attempt + 1}/{self.max_retries + 1}")
                request_kwargs = {
                    'headers': request_headers,
                    'params': params,
                    # Keep bot requests stateless and prevent any cookie-based auth bleed.
                    'cookies': {}
                }

                if method.upper() == 'GET':
                    response = await self._client.get(url, **request_kwargs)
                elif method.upper() == 'POST':
                    response = await self._client.post(url, json=data, **request_kwargs)
                elif method.upper() == 'PUT':
                    response = await self._client.put(url, json=data, **request_kwargs)
                elif method.upper() == 'PATCH':
                    response = await self._client.patch(url, json=data, **request_kwargs)
                elif method.upper() == 'DELETE':
                    response = await self._client.delete(url, **request_kwargs)
                else:
                    raise ValueError(f"Unsupported HTTP method: {method}")

                logger.info(f"HTTP {method.upper()} {endpoint} -> {response.status_code}")
                logger.debug(f"Response headers: {dict(response.headers)}")

                # Any HTTP response means backend is reachable – record success
                self._circuit_breaker.record_success()

                # Handle response
                if response.status_code < 400:
                    try:
                        response_data = response.json()
                        logger.debug(f"Response data: {response_data}")
                    except Exception as json_error:
                        response_data = response.text
                        logger.debug(f"Response text: {response_data}")
                        logger.warning(f"Failed to parse JSON: {json_error}")
                    return APIResponse(
                        success=True,
                        data=response_data,
                        status_code=response.status_code
                    )
                else:
                    error_msg = f"HTTP {response.status_code}"
                    error_data = None
                    try:
                        error_data = response.json()
                        error_msg = error_data.get('message', error_msg)
                        logger.debug(f"Error response data: {error_data}")
                    except Exception as json_error:
                        error_text = response.text
                        logger.debug(f"Error response text: {error_text}")

                    logger.warning(f"HTTP {method.upper()} {endpoint} failed: {response.status_code} - {error_msg}")

                    # Invalidate cached tokens on 401 (stale/revoked token)
                    if response.status_code == 401 and token_manager and telegram_id:
                        logger.info(f"Auth failure (401) for user {telegram_id}, invalidating cached tokens")
                        try:
                            await token_manager.invalidate_tokens(telegram_id)
                        except Exception as inv_err:
                            logger.warning(f"Failed to invalidate tokens: {inv_err}")

                    # Surface the full error body so callers can read structured
                    # fields (e.g. `cancelled_order_id` on Asl belgisi 503).
                    return APIResponse(
                        success=False,
                        error=error_msg,
                        status_code=response.status_code,
                        data=error_data,
                    )

            except httpx.ConnectError as e:
                error_str = str(e).lower()
                # Check if this is an SSL-related error
                if 'ssl' in error_str or 'certificate' in error_str:
                    logger.error(f"SSL certificate error (attempt {attempt + 1}): {e}")
                    logger.error(
                        "SSL certificate validation failed. This could indicate:\n" +
                        "1. Invalid or expired SSL certificate\n" +
                        "2. Self-signed certificate (set BUSINESS_API_SSL_VERIFY=false for development)\n" +
                        "3. Certificate hostname mismatch\n" +
                        "4. Potential man-in-the-middle attack\n" +
                        "Please verify the server's SSL certificate."
                    )
                    # Don't retry SSL errors as they're unlikely to resolve
                    self._circuit_breaker.record_failure()
                    return APIResponse(
                        success=False,
                        error=f"SSL certificate validation failed: {str(e)}"
                    )
                else:
                    logger.error(f"Connection error (attempt {attempt + 1}): {e}")
                    if attempt < self.max_retries:
                        retry_delay = self.retry_delay * (attempt + 1)
                        logger.info(f"Retrying connection in {retry_delay} seconds...")
                        await asyncio.sleep(retry_delay)
                    else:
                        self._circuit_breaker.record_failure()
                        logger.error("=== CONNECTION FAILED - MAX RETRIES REACHED ===")
                        return APIResponse(
                            success=False,
                            error=f"Connection failed after {self.max_retries} retries: {str(e)}"
                        )

            except httpx.TimeoutException as e:
                logger.error(f"Request timeout (attempt {attempt + 1}): {e}")
                if attempt < self.max_retries:
                    retry_delay = self.retry_delay * (attempt + 1)
                    logger.info(f"Retrying after timeout in {retry_delay} seconds...")
                    await asyncio.sleep(retry_delay)
                else:
                    self._circuit_breaker.record_failure()
                    logger.error("=== REQUEST TIMEOUT - MAX RETRIES REACHED ===")
                    return APIResponse(
                        success=False,
                        error=f"Request timed out after {self.max_retries} retries"
                    )

            except Exception as e:
                logger.error(f"API request exception (attempt {attempt + 1}): {e}")
                logger.error(f"Exception type: {type(e)}")
                import traceback
                logger.error(f"Traceback: {traceback.format_exc()}")

                if attempt < self.max_retries:
                    retry_delay = self.retry_delay * (attempt + 1)
                    logger.info(f"Retrying in {retry_delay} seconds...")
                    await asyncio.sleep(retry_delay)
                else:
                    self._circuit_breaker.record_failure()
                    logger.error("=== HTTP REQUEST FAILED - MAX RETRIES REACHED ===")
                    return APIResponse(
                        success=False,
                        error=str(e)
                    )

    # Authentication methods
    async def authenticate_user(self, telegram_id: int, user_data: dict = None) -> Optional[str]:
        """Get JWT token for telegram user with enhanced audit logging"""
        try:
            logger.info(f"=== API CLIENT AUTH DEBUG START for user {telegram_id} ===")

            # Prepare authentication data
            auth_data = {'telegram_id': telegram_id}
            logger.info(f"Base auth_data: {auth_data}")

            # Add user information if provided
            if user_data:
                if 'username' in user_data:
                    auth_data['username'] = user_data['username']
                if 'first_name' in user_data:
                    auth_data['first_name'] = user_data['first_name']
                if 'last_name' in user_data:
                    auth_data['last_name'] = user_data['last_name']
                logger.info(f"Final auth_data with user info: {auth_data}")
            else:
                logger.warning("No user_data provided to authenticate_user")

            logger.info("Making POST request to /api/v1/auth/telegram-login")
            response = await self._make_request(
                'POST',
                '/api/v1/auth/telegram-login',
                data=auth_data
            )

            logger.debug(f"Auth response - Success: {response.success}, Status: {response.status_code}")
            if response.success:
                # The response has nested data structure: response.data['data']['access_token']
                data = response.data.get('data', {})
                token = data.get('access_token')
                refresh_token = data.get('refresh_token')
                expires_in = data.get('expires_in', 3600)
                if token:
                    logger.info(f"Authentication successful for telegram_id {telegram_id}")
                    # Return both tokens for caching
                    return {
                        'access_token': token,
                        'refresh_token': refresh_token,
                        'expires_in': expires_in
                    }
                else:
                    logger.error("No access_token in successful response")
                    logger.error(f"Response data: {response.data}")
                    # Log authentication failure to audit system
                    await self._log_authentication_failure(
                        telegram_id=str(telegram_id),
                        failure_reason='missing_access_token',
                        response_data=response.data
                    )
                    return None
            else:
                logger.error(f"Failed to authenticate telegram user {telegram_id}")
                logger.error(f"Error: {response.error}")
                logger.error(f"Status code: {response.status_code}")

                # Log authentication failure to audit system
                await self._log_authentication_failure(
                    telegram_id=str(telegram_id),
                    failure_reason=f"api_error_{response.status_code}",
                    error_message=response.error,
                    status_code=response.status_code

                )
                return None

        except Exception as e:
            logger.error(f"Authentication error for user {telegram_id}: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")

            # Log authentication failure to audit system
            await self._log_authentication_failure(
                telegram_id=str(telegram_id),
                failure_reason='system_exception',
                error_message=str(e)
            )
            return None
        finally:
            logger.info(f"=== API CLIENT AUTH DEBUG END for user {telegram_id} ===")

    async def refresh_token(
        self,
        refresh_token: str,
        telegram_id: Optional[int] = None,
        token_manager=None,
    ) -> Optional[Dict[str, Any]]:
        """
        Refresh access token using refresh token.

        Args:
            refresh_token: Valid refresh token
            telegram_id: Telegram user ID.
            token_manager: TokenManager instance. When both this and telegram_id
                are provided, a 401 from /auth/refresh triggers cached-token
                invalidation in `_make_request`, so a stale refresh token
                (e.g. for a merged/deleted user) is cleared instead of being
                retried on every call.

        Returns:
            Dict with new access_token and expires_in, or None if failed
        """
        try:
            logger.info("Attempting to refresh access token")
            response = await self._make_request(
                'POST',
                '/api/v1/auth/refresh',
                data={'refresh_token': refresh_token},
                telegram_id=telegram_id,
                token_manager=token_manager,
            )

            if response.success:
                data = response.data.get('data', {})
                token = data.get('access_token')
                if token:
                    logger.info("Access token refreshed successfully")
                    return {
                        'access_token': token,
                        'expires_in': data.get('expires_in', 3600)
                    }

            logger.warning(f"Token refresh failed: {response.error}")
            return None

        except Exception as e:
            logger.error(f"Token refresh error: {e}")
            return None

    async def _log_authentication_failure(self, telegram_id: str, failure_reason: str,
                                        error_message: str = None, status_code: int = None,
                                        response_data: dict = None):
        """Log authentication failure to database audit system"""
        try:
            # Check if database connection is available
            if not db_manager.is_connected:
                logger.warning("Database not connected, skipping audit log")
                return

            # Prepare additional data
            additional_data = {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'api_client': 'telegram_bot',
                'auth_method': 'telegram_login'
            }

            if error_message:
                additional_data['error_message'] = error_message
            if status_code:
                additional_data['status_code'] = status_code
            if response_data:
                additional_data['response_data'] = response_data

            # Determine if this is suspicious
            is_suspicious = False
            if failure_reason in ['system_exception', 'api_error_500', 'missing_access_token']:
                is_suspicious = True
            elif status_code and status_code >= 500:
                is_suspicious = True

            # Log to audit system using SQL function
            query = """
            SELECT log_failed_authentication(
                NULL,  -- attempted_email
                NULL,  -- attempted_phone
                $1,    -- attempted_telegram_id
                NULL,  -- ip_address (not available in bot context)
                $2,    -- user_agent
                $3,    -- failure_reason
                $4     -- is_suspicious
            )
            """

            await db_manager.execute(
                query,
                telegram_id,
                'BlueStream-TelegramBot/1.0',
                failure_reason,
                is_suspicious
            )

            logger.info(f"Authentication failure logged to audit system for telegram_id: {telegram_id}")

        except Exception as audit_error:
            logger.error(f"Failed to log authentication failure to audit system: {audit_error}")
            # Don't raise - audit logging should not break the main flow

    async def register_telegram_user(self, telegram_id: int, user_data: Dict) -> APIResponse:
        """Register new telegram user"""
        data = {
            'telegram_id': telegram_id,
            **user_data
        }
        return await self._make_request('POST', '/api/v1/auth/telegram-register', data=data)

    # Product methods
    async def get_products(self, user_token: str, category: Optional[str] = None,
                          search: Optional[str] = None, page: int = 1,
                          per_page: int = 20,
                          language: Optional[str] = None) -> APIResponse:
        """Get products list"""
        params = {'page': page, 'per_page': per_page}
        if category:
            params['category_id'] = category
        if search:
            params['search'] = search

        return await self._make_request('GET', '/api/v1/products',
                                       user_token=user_token, params=params,
                                       language=language)

    async def get_product(self, user_token: str, product_id: int, language: Optional[str] = None) -> APIResponse:
        """Get single product details"""
        return await self._make_request('GET', f'/api/v1/products/{product_id}',
                                       user_token=user_token,
                                       language=language)

    async def get_product_categories(self, user_token: str, language: Optional[str] = None) -> APIResponse:
        """Get product categories"""
        return await self._make_request('GET', '/api/v1/products/categories',
                                       user_token=user_token,
                                       language=language)

    async def get_category(self, user_token: str, category_id: int, language: Optional[str] = None) -> APIResponse:
        """Get specific category details"""
        return await self._make_request('GET', f'/api/v1/products/categories/{category_id}',
                                       user_token=user_token,
                                       language=language)

    # Cart methods
    async def get_cart(self, user_token: str) -> APIResponse:
        """Get user's cart"""
        return await self._make_request('GET', '/api/v1/cart',
                                       user_token=user_token)

    async def add_to_cart(self, user_token: str, product_id: int, quantity: int) -> APIResponse:
        """Add item to cart"""
        data = {
            'product_id': product_id,
            'quantity': quantity
        }
        return await self._make_request('POST', '/api/v1/cart/items',
                                       user_token=user_token, data=data)

    async def update_cart_item(self, user_token: str, product_id: int, quantity: int) -> APIResponse:
        """Update cart item quantity"""
        data = {
            'quantity': quantity
        }
        return await self._make_request('PUT', f'/api/v1/cart/items/{product_id}',
                                       user_token=user_token, data=data)
    async def remove_cart_item(self, user_token: str, product_id: int) -> APIResponse:
        """Remove item from cart"""
        return await self._make_request('DELETE', f'/api/v1/cart/items/{product_id}',
                                       user_token=user_token)

    async def clear_cart(self, user_token: str) -> APIResponse:
        """Clear user's cart"""
        return await self._make_request('POST', '/api/v1/cart/clear',
                                       user_token=user_token)

    # Order methods
    async def create_order(self, user_token: str, order_data: Dict) -> APIResponse:
        """Create new order"""
        return await self._make_request('POST', '/api/v1/orders',
                                       user_token=user_token, data=order_data)

    async def get_user_orders(self, user_token: str, status: Optional[str] = None) -> APIResponse:
        """Get user's orders"""
        params = {}
        if status:
            params['status'] = status

        return await self._make_request('GET', '/api/v1/orders/',
                                       user_token=user_token, params=params)

    async def get_order(self, user_token: str, order_id: int) -> APIResponse:
        """Get specific order details"""
        return await self._make_request('GET', f'/api/v1/orders/{order_id}',
                                       user_token=user_token)

    async def cancel_order(self, user_token: str, order_id: int) -> APIResponse:
        """Cancel order"""
        return await self._make_request('POST', f'/api/v1/orders/{order_id}/cancel',
                                       user_token=user_token)

    async def track_order(self, user_token: str, order_id: int) -> APIResponse:
        """Get order tracking information with status timeline"""
        return await self._make_request('GET', f'/api/v1/orders/{order_id}/track',
                                       user_token=user_token)

    async def get_quick_reorder_suggestions(self, user_token: str,
                                            limit: int = 3,
                                            period_days: int = 90) -> APIResponse:
        """Get habitual product+quantity suggestions for Quick Order."""
        return await self._make_request(
            'GET', '/api/v1/orders/quick-reorder',
            user_token=user_token,
            params={'limit': limit, 'period_days': period_days},
        )

    async def retry_order_with_cash(self, user_token: str, order_id: int) -> APIResponse:
        """Switch a tax-committee-cancelled order to cash payment.

        Used by the Asl belgisi rescue flow. Bypasses the COD active-debt
        cap server-side; see business_app/services/order_service.py
        retry_cancelled_order_with_cash for the security boundary.
        """
        return await self._make_request(
            'POST', f'/api/v1/orders/{order_id}/retry-with-cash',
            user_token=user_token,
        )

    # Payment methods
    async def get_payment_methods(self, user_token: str) -> APIResponse:
        """Get user's payment methods"""
        return await self._make_request('GET', '/api/v1/payments/methods',
                                       user_token=user_token)

    async def create_payment(self, user_token: str, payment_data: Dict) -> APIResponse:
        """Create payment for order"""
        return await self._make_request('POST', '/api/v1/payments/create',
                                       user_token=user_token, data=payment_data)

    async def get_payment_status(self, user_token: str, payment_id: str) -> APIResponse:
        """Get payment status"""
        return await self._make_request('GET', f'/api/v1/payments/{payment_id}',
                                       user_token=user_token)

    # Delivery methods
    async def get_delivery_slots(self, user_token: str, address_id: int) -> APIResponse:
        """Get available delivery time slots"""
        return await self._make_request('GET', f'/api/v1/delivery/slots/{address_id}',
                                       user_token=user_token)

    async def track_delivery(self, user_token: str, order_id: int) -> APIResponse:
        """Track order delivery"""
        return await self._make_request('GET', f'/api/v1/delivery/track/{order_id}',
                                       user_token=user_token)

    # User profile methods
    async def get_user_profile(self, user_token: str) -> APIResponse:
        """Get user profile"""
        return await self._make_request('GET', '/api/v1/auth/profile',
                                       user_token=user_token)

    async def update_user_profile(self, user_token: str, profile_data: Dict) -> APIResponse:
        """Update user profile"""
        return await self._make_request('PUT', '/api/v1/auth/profile',
                                       user_token=user_token, data=profile_data)

    async def get_notification_preferences(self, user_token: str) -> APIResponse:
        """Get user notification preferences."""
        return await self._make_request(
            'GET',
            '/api/v1/notifications/preferences',
            user_token=user_token,
        )

    async def update_notification_preferences(self, user_token: str, payload: Dict) -> APIResponse:
        """Update user notification preferences."""
        return await self._make_request(
            'PUT',
            '/api/v1/notifications/preferences',
            user_token=user_token,
            data=payload,
        )

    async def send_phone_verification(self, user_token: str, phone: str) -> APIResponse:
        """Send phone verification SMS with OTP"""
        return await self._make_request('POST', '/api/v1/auth/send-otp',
                                       user_token=user_token, data={'phone': phone})

    async def verify_phone_otp(self, user_token: str, otp: str) -> APIResponse:
        """Verify phone number with OTP code"""
        return await self._make_request('POST', '/api/v1/auth/verify-phone',
                                       user_token=user_token, data={'otp': otp})

    async def record_support_message(self, user_token: str, content: str) -> APIResponse:
        """Persist an inbound free-text customer message as a support message."""
        return await self._make_request(
            'POST', '/api/v1/support/messages',
            user_token=user_token, data={'content': content}
        )

    async def get_user_addresses(self, user_token: str) -> APIResponse:
        """Get user addresses"""
        return await self._make_request('GET', '/api/v1/auth/addresses',
                                       user_token=user_token)

    async def add_user_address(self, user_token: str, address_data: Dict) -> APIResponse:
        """Add new address"""
        return await self._make_request('POST', '/api/v1/auth/addresses',
                                       user_token=user_token, data=address_data)

    async def update_user_address(self, user_token: str, address_id: int, address_data: Dict) -> APIResponse:
        """Update existing address"""
        return await self._make_request('PUT', f'/api/v1/auth/addresses/{address_id}',
                                       user_token=user_token, data=address_data)

    async def delete_user_address(self, user_token: str, address_id: int) -> APIResponse:
        """Delete address"""
        return await self._make_request('DELETE', f'/api/v1/auth/addresses/{address_id}',
                                       user_token=user_token)

    async def set_default_address(self, user_token: str, address_id: int) -> APIResponse:
        """Set address as default"""
        return await self._make_request('PATCH', f'/api/v1/auth/addresses/{address_id}/set-default',
                                       user_token=user_token)

    async def geocode_address(self, user_token: str, address: str,
                             hint_lat: float = None, hint_lon: float = None) -> APIResponse:
        """Geocode an address string to coordinates

        Args:
            user_token: User authentication token
            address: Address string to geocode
            hint_lat: Optional latitude hint for better results
            hint_lon: Optional longitude hint for better results

        Returns:
            APIResponse with latitude, longitude, and formatted_address
        """
        data = {'address': address}
        if hint_lat is not None:
            data['hint_lat'] = hint_lat
        if hint_lon is not None:
            data['hint_lon'] = hint_lon
        return await self._make_request('POST', '/api/v1/addresses/geocode',
                                       user_token=user_token, data=data)

    async def reverse_geocode(self, user_token: str, latitude: float, longitude: float) -> APIResponse:
        """Reverse geocode coordinates to address

        Args:
            user_token: User authentication token
            latitude: GPS latitude coordinate
            longitude: GPS longitude coordinate

        Returns:
            APIResponse with formatted_address, district, city, country
        """
        data = {'latitude': latitude, 'longitude': longitude}
        return await self._make_request('POST', '/api/v1/addresses/reverse-geocode',
                                       user_token=user_token, data=data)

    async def get_districts(self, user_token: str, language: str = 'en') -> APIResponse:
        """Get list of supported districts

        Args:
            user_token: User authentication token
            language: Language code (en, uz, ru)

        Returns:
            APIResponse with districts list and region info
        """
        return await self._make_request('GET', f'/api/v1/addresses/districts?lang={language}',
                                       user_token=user_token)

    # Subscription methods
    async def get_user_subscriptions(self, user_token: str) -> APIResponse:
        """Get user subscriptions"""
        return await self._make_request('GET', '/api/v1/subscriptions',
                                       user_token=user_token)

    async def create_subscription(self, user_token: str, subscription_data: Dict) -> APIResponse:
        """Create new subscription"""
        return await self._make_request('POST', '/api/v1/subscriptions',
                                       user_token=user_token, data=subscription_data)

    async def update_subscription(self, user_token: str, subscription_id: int,
                                update_data: Dict) -> APIResponse:
        """Update subscription"""
        return await self._make_request('PUT', f'/api/v1/subscriptions/{subscription_id}',
                                       user_token=user_token, data=update_data)

    async def pause_subscription(self, user_token: str, subscription_id: int) -> APIResponse:
        """Pause subscription"""
        return await self._make_request('POST', f'/api/v1/subscriptions/{subscription_id}/pause',
                                       user_token=user_token)

    async def resume_subscription(self, user_token: str, subscription_id: int) -> APIResponse:
        """Resume subscription"""
        return await self._make_request('POST', f'/api/v1/subscriptions/{subscription_id}/resume',
                                       user_token=user_token)

    async def get_subscription(self, user_token: str, subscription_id: int) -> APIResponse:
        """Get specific subscription details"""
        return await self._make_request('GET', f'/api/v1/subscriptions/{subscription_id}',
                                       user_token=user_token)

    async def cancel_subscription(self, user_token: str, subscription_id: int,
                                 cancel_data: Dict = None) -> APIResponse:
        """Cancel subscription"""
        return await self._make_request('POST', f'/api/v1/subscriptions/{subscription_id}/cancel',
                                       user_token=user_token, data=cancel_data or {})

    async def get_subscription_items(self, user_token: str, subscription_id: int) -> APIResponse:
        """Get subscription items"""
        return await self._make_request('GET', f'/api/v1/subscriptions/{subscription_id}/items',
                                       user_token=user_token)

    async def add_subscription_item(self, user_token: str, subscription_id: int,
                                   item_data: Dict) -> APIResponse:
        """Add item to subscription"""
        return await self._make_request('POST', f'/api/v1/subscriptions/{subscription_id}/items',
                                       user_token=user_token, data=item_data)

    async def update_subscription_item(self, user_token: str, subscription_id: int,
                                      item_id: int, item_data: Dict) -> APIResponse:
        """Update subscription item"""
        return await self._make_request('PUT', f'/api/v1/subscriptions/{subscription_id}/items/{item_id}',
                                       user_token=user_token, data=item_data)

    async def remove_subscription_item(self, user_token: str, subscription_id: int,
                                      item_id: int) -> APIResponse:
        """Remove item from subscription"""
        return await self._make_request('DELETE', f'/api/v1/subscriptions/{subscription_id}/items/{item_id}',
                                       user_token=user_token)

    async def get_billing_history(self, user_token: str, subscription_id: int) -> APIResponse:
        """Get subscription billing history"""
        return await self._make_request('GET', f'/api/v1/subscriptions/{subscription_id}/billing-history',
                                       user_token=user_token)

    async def get_subscription_logs(self, user_token: str, subscription_id: int) -> APIResponse:
        """Get subscription activity logs"""
        return await self._make_request('GET', f'/api/v1/subscriptions/{subscription_id}/logs',
                                       user_token=user_token)

    async def get_subscription_templates(self, user_token: str) -> APIResponse:
        """Get predefined subscription templates"""
        return await self._make_request('GET', '/api/v1/subscriptions/templates',
                                       user_token=user_token)

    async def preview_subscription(self, user_token: str, preview_data: Dict) -> APIResponse:
        """Preview subscription cost before creating"""
        return await self._make_request('POST', '/api/v1/subscriptions/preview',
                                       user_token=user_token, data=preview_data)

    async def get_subscription_statistics(self, user_token: str) -> APIResponse:
        """Get user subscription statistics"""
        return await self._make_request('GET', '/api/v1/subscriptions/statistics',
                                       user_token=user_token)

    async def skip_next_delivery(self, user_token: str, subscription_id: int,
                                skip_data: Dict = None) -> APIResponse:
        """Skip next delivery for subscription"""
        return await self._make_request('POST', f'/api/v1/subscriptions/{subscription_id}/skip-next-delivery',
                                       user_token=user_token, data=skip_data or {})

    async def change_payment_method(self, user_token: str, subscription_id: int,
                                   payment_data: Dict) -> APIResponse:
        """Change subscription payment method"""
        return await self._make_request('POST', f'/api/v1/subscriptions/{subscription_id}/change-payment-method',
                                       user_token=user_token, data=payment_data)

    async def retry_billing(self, user_token: str, subscription_id: int) -> APIResponse:
        """Retry failed billing for subscription"""
        return await self._make_request('POST', f'/api/v1/subscriptions/{subscription_id}/retry-billing',
                                       user_token=user_token)

    # Loyalty methods
    async def get_loyalty_points(self, user_token: str) -> APIResponse:
        """Get user's loyalty points balance"""
        return await self._make_request('GET', '/api/v1/loyalty/points',
                                       user_token=user_token)

    async def get_loyalty_history(self, user_token: str, page: int = 1, per_page: int = 10) -> APIResponse:
        """Get loyalty points history (paginated)."""
        return await self._make_request('GET', '/api/v1/loyalty/history',
                                       params={'page': page, 'per_page': per_page},
                                       user_token=user_token)

    async def get_loyalty_rewards(self, user_token: str) -> APIResponse:
        """Get available loyalty rewards"""
        return await self._make_request('GET', '/api/v1/loyalty/rewards',
                                       user_token=user_token)

    async def get_referral_info(self, user_token: str) -> APIResponse:
        """Get the user's referral code, link, and stats"""
        return await self._make_request('GET', '/api/v1/loyalty/referral',
                                       user_token=user_token)

    # Analytics methods (admin only)
    async def get_analytics_overview(self, user_token: str) -> APIResponse:
        """Get analytics overview (admin)"""
        return await self._make_request('GET', '/api/v1/analytics/overview',
                                       user_token=user_token)

    async def get_order_analytics(self, user_token: str, period: str = 'week') -> APIResponse:
        """Get order analytics (admin)"""
        return await self._make_request('GET', f'/api/v1/analytics/orders?period={period}',
                                       user_token=user_token)

    # Authentication methods
    async def logout_current_session(self, user_token: str) -> APIResponse:
        """Logout from current session"""
        return await self._make_request('POST', '/api/v1/auth/logout',
                                       user_token=user_token)

    async def logout_all_sessions(self, user_token: str) -> APIResponse:
        """Logout from all sessions"""
        return await self._make_request('POST', '/api/v1/auth/logout-all',
                                       user_token=user_token)

    async def get_user_sessions(self, user_token: str) -> APIResponse:
        """Get all user sessions"""
        return await self._make_request('GET', '/api/v1/auth/sessions',
                                       user_token=user_token)

    async def revoke_session(self, user_token: str, session_id: str) -> APIResponse:
        """Revoke a specific session"""
        return await self._make_request('DELETE', f'/api/v1/auth/sessions/{session_id}',
                                       user_token=user_token)

    # ==================== Account Linking ====================

    async def check_phone_availability(self, telegram_id: int, phone: str) -> APIResponse:
        """
        Check if a phone number is available for registration or needs linking.

        Args:
            telegram_id: Telegram user ID
            phone: Phone number to check

        Returns:
            APIResponse with available, can_link, and existing_user_masked
        """
        return await self._make_request(
            'POST',
            '/api/v1/auth/check-phone-availability',
            data={'telegram_id': telegram_id, 'phone': phone}
        )

    async def link_phone_send_otp(self, telegram_id: int, phone: str) -> APIResponse:
        """
        Send OTP to phone for account linking.

        Args:
            telegram_id: Telegram user ID
            phone: Phone number to send OTP to

        Returns:
            APIResponse with phone_masked on success
        """
        return await self._make_request(
            'POST',
            '/api/v1/auth/link-phone-account/send-otp',
            data={'telegram_id': telegram_id, 'phone': phone}
        )

    async def link_phone_verify(self, telegram_id: int, otp: str) -> APIResponse:
        """
        Verify OTP and link accounts.

        Args:
            telegram_id: Telegram user ID
            otp: 6-digit OTP code

        Returns:
            APIResponse with user data and tokens on success
        """
        return await self._make_request(
            'POST',
            '/api/v1/auth/link-phone-account/verify',
            data={'telegram_id': telegram_id, 'otp': otp}
        )


    async def get_my_bottle_balances(self, user_token: str) -> 'APIResponse':
        """Get current user's bottle balances across all addresses."""
        return await self._make_request('GET', '/api/v1/orders/bottles/my-balances',
                                        user_token=user_token)

    async def get_my_bottle_ledger(self, user_token: str, address_id: int,
                                    page: int = 1, per_page: int = 10) -> 'APIResponse':
        """Get current user's bottle ledger for a specific address."""
        return await self._make_request('GET',
                                        f'/api/v1/orders/bottles/my-ledger/{address_id}',
                                        params={'page': page, 'per_page': per_page},
                                        user_token=user_token)


# Global API client instance
api_client = BusinessAPIClient()
