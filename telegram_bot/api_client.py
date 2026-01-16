"""
API client for communicating with the business application
"""
import httpx
import logging
from typing import Dict, Any, Optional, List, Union
from dataclasses import dataclass
import asyncio
from datetime import datetime, timedelta
import ssl
import os

from config import config
from database import db_manager

logger = logging.getLogger('api_client')


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
                          user_token: Optional[str] = None) -> APIResponse:
        """Make HTTP request with retry logic"""
        url = self._get_url(endpoint)
        
        logger.info(f"=== HTTP REQUEST DEBUG ===")
        logger.info(f"Method: {method.upper()}")
        logger.info(f"URL: {url}")
        logger.info(f"Endpoint: {endpoint}")
        
        # Set up headers
        request_headers = headers or {}
        if user_token:
            request_headers['Authorization'] = f'Bearer {user_token}'
            logger.info(f"Authorization header added with token: {user_token[:20]}...")
        
        logger.info(f"Request headers: {request_headers}")
        if data:
            logger.info(f"Request data: {data}")
        if params:
            logger.info(f"Request params: {params}")
        
        for attempt in range(self.max_retries + 1):
            try:
                logger.info(f"Making HTTP request (attempt {attempt + 1}/{self.max_retries + 1})...")
                
                if method.upper() == 'GET':
                    response = await self._client.get(url, headers=request_headers, params=params)
                elif method.upper() == 'POST':
                    response = await self._client.post(url, headers=request_headers, json=data, params=params)
                elif method.upper() == 'PUT':
                    response = await self._client.put(url, headers=request_headers, json=data, params=params)
                elif method.upper() == 'PATCH':
                    response = await self._client.patch(url, headers=request_headers, json=data, params=params)
                elif method.upper() == 'DELETE':
                    response = await self._client.delete(url, headers=request_headers, params=params)
                else:
                    raise ValueError(f"Unsupported HTTP method: {method}")
                
                logger.info(f"Response received - Status code: {response.status_code}")
                logger.info(f"Response headers: {dict(response.headers)}")
                
                # Handle response
                if response.status_code < 400:
                    try:
                        response_data = response.json()
                        logger.info(f"Response JSON data: {response_data}")
                    except Exception as json_error:
                        response_data = response.text
                        logger.info(f"Response text data: {response_data}")
                        logger.warning(f"Failed to parse JSON: {json_error}")
                    
                    logger.info("=== HTTP REQUEST SUCCESS ===")
                    return APIResponse(
                        success=True,
                        data=response_data,
                        status_code=response.status_code
                    )
                else:
                    error_msg = f"HTTP {response.status_code}"
                    try:
                        error_data = response.json()
                        error_msg = error_data.get('message', error_msg)
                        logger.error(f"Error response JSON: {error_data}")
                    except Exception as json_error:
                        error_text = response.text
                        logger.error(f"Error response text: {error_text}")
                        logger.warning(f"Failed to parse error JSON: {json_error}")
                    
                    logger.error(f"=== HTTP REQUEST FAILED - Status {response.status_code} ===")
                    logger.error(f"Error message: {error_msg}")
                    
                    return APIResponse(
                        success=False,
                        error=error_msg,
                        status_code=response.status_code
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
            
            logger.info(f"Response received - Success: {response.success}, Status: {response.status_code}")
            if response.success:
                # The response has nested data structure: response.data['data']['access_token']
                data = response.data.get('data', {})
                token = data.get('access_token')
                refresh_token = data.get('refresh_token')
                expires_in = data.get('expires_in', 3600)
                if token:
                    logger.info(f"Access token received: {token[:20]}...")
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
    
    async def refresh_token(self, refresh_token: str) -> Optional[Dict[str, Any]]:
        """
        Refresh access token using refresh token.
        
        Args:
            refresh_token: Valid refresh token
            
        Returns:
            Dict with new access_token and expires_in, or None if failed
        """
        try:
            logger.info("Attempting to refresh access token")
            response = await self._make_request(
                'POST',
                '/api/v1/auth/refresh',
                data={'refresh_token': refresh_token}
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
                'timestamp': datetime.now().isoformat(),
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
                          search: Optional[str] = None, page: int = 1) -> APIResponse:
        """Get products list"""
        params = {'page': page}
        if category:
            params['category_id'] = category
        if search:
            params['search'] = search

        return await self._make_request('GET', '/api/v1/products',
                                       user_token=user_token, params=params)
    
    async def get_product(self, user_token: str, product_id: int) -> APIResponse:
        """Get single product details"""
        return await self._make_request('GET', f'/api/v1/products/{product_id}', 
                                       user_token=user_token)
    
    async def get_product_categories(self, user_token: str) -> APIResponse:
        """Get product categories"""
        return await self._make_request('GET', '/api/v1/products/categories', 
                                       user_token=user_token)
    
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
        return await self._make_request('PUT', f'/api/v1/orders/{order_id}/cancel', 
                                       user_token=user_token)
    
    async def track_order(self, user_token: str, order_id: int) -> APIResponse:
        """Get order tracking information with status timeline"""
        return await self._make_request('GET', f'/api/v1/orders/{order_id}/track', 
                                       user_token=user_token)
    
    # Payment methods
    async def get_payment_methods(self, user_token: str) -> APIResponse:
        """Get user's payment methods"""
        return await self._make_request('GET', '/api/v1/payments/methods', 
                                       user_token=user_token)
    
    async def create_payment(self, user_token: str, payment_data: Dict) -> APIResponse:
        """Create payment for order"""
        return await self._make_request('POST', '/api/v1/payments', 
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

    async def send_phone_verification(self, user_token: str, phone: str) -> APIResponse:
        """Send phone verification SMS with OTP"""
        return await self._make_request('POST', '/api/v1/auth/send-otp',
                                       user_token=user_token, data={'phone': phone})

    async def verify_phone_otp(self, user_token: str, otp: str) -> APIResponse:
        """Verify phone number with OTP code"""
        return await self._make_request('POST', '/api/v1/auth/verify-phone',
                                       user_token=user_token, data={'otp': otp})

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
    
    async def get_loyalty_history(self, user_token: str) -> APIResponse:
        """Get loyalty points history"""
        return await self._make_request('GET', '/api/v1/loyalty/history', 
                                       user_token=user_token)
    
    async def get_loyalty_rewards(self, user_token: str) -> APIResponse:
        """Get available loyalty rewards"""
        return await self._make_request('GET', '/api/v1/loyalty/rewards', 
                                       user_token=user_token)
    
    async def redeem_reward(self, user_token: str, reward_id: int) -> APIResponse:
        """Redeem loyalty reward"""
        return await self._make_request('POST', f'/api/v1/loyalty/rewards/{reward_id}/redeem', 
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

    # Telegram Payment methods
    async def record_telegram_payment(self, user_token: str, payment_data: Dict) -> APIResponse:
        """
        Record successful Telegram payment in backend.

        Args:
            user_token: User authentication token
            payment_data: Payment details including:
                - order_id: Order ID
                - amount: Payment amount in UZS
                - currency: Currency code (UZS)
                - payment_method: 'payme'
                - telegram_payment_charge_id: Telegram's payment ID
                - provider_payment_charge_id: Payme's payment ID
                - status: 'completed'

        Returns:
            APIResponse with payment record
        """
        return await self._make_request('POST', '/api/v1/payments/telegram',
                                       user_token=user_token, data=payment_data)

    async def update_order_payment_status(self, user_token: str, order_id: int,
                                          status: str, payment_data: Dict = None) -> APIResponse:
        """
        Update order payment status.

        Args:
            user_token: User authentication token
            order_id: Order ID to update
            status: New payment status ('pending', 'paid', 'failed')
            payment_data: Optional additional payment data

        Returns:
            APIResponse with updated order
        """
        data = {'payment_status': status}
        if payment_data:
            data['payment_data'] = payment_data
        return await self._make_request('PATCH', f'/api/v1/orders/{order_id}/payment-status',
                                       user_token=user_token, data=data)

    async def get_order_for_validation(self, user_token: str, order_id: int) -> APIResponse:
        """
        Get minimal order data for pre-checkout validation.

        This endpoint should be optimized for fast response (< 2 seconds)
        as pre-checkout queries must be answered within 10 seconds.

        Args:
            user_token: User authentication token
            order_id: Order ID to validate

        Returns:
            APIResponse with order validation data (id, status, total_amount, user_id)
        """
        return await self._make_request('GET', f'/api/v1/orders/{order_id}/validate',
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


# Global API client instance
api_client = BusinessAPIClient()