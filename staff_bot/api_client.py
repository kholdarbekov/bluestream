"""
API client for communicating with the business application (staff endpoints)
"""
import httpx
import logging
from typing import Dict, Any, Optional
from dataclasses import dataclass
import asyncio
from datetime import datetime, timezone
import os

from config import config

logger = logging.getLogger('api_client')


class CircuitBreaker:
    """Simple circuit breaker to fail fast when the backend is down."""
    CLOSED = 'closed'
    OPEN = 'open'
    HALF_OPEN = 'half_open'

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
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
        return self.state != self.OPEN

    def record_success(self):
        self._failure_count = 0
        self._state = self.CLOSED

    def record_failure(self):
        self._failure_count += 1
        self._last_failure_time = datetime.now(timezone.utc)
        if self._failure_count >= self.failure_threshold:
            self._state = self.OPEN
            logger.warning(
                f"Circuit breaker OPEN after {self._failure_count} failures. "
                f"Failing fast for {self.recovery_timeout}s."
            )


@dataclass
class APIResponse:
    """API response wrapper"""
    success: bool
    data: Any = None
    error: Optional[str] = None
    status_code: Optional[int] = None


class StaffAPIClient:
    """Client for business application staff API endpoints"""

    def __init__(self):
        self.base_url = config.business_api.base_url
        self.timeout = config.business_api.timeout
        self.max_retries = config.business_api.max_retries
        self.retry_delay = config.business_api.retry_delay
        self._client = None
        self._circuit_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)

    def _resolve_verify_config(self):
        """Resolve SSL verification configuration for httpx client."""
        ssl_verify = config.business_api.ssl_verify
        ssl_cert_path = config.business_api.ssl_cert_path

        if ssl_verify and ssl_cert_path:
            if not os.path.exists(ssl_cert_path):
                raise FileNotFoundError(f"SSL certificate file not found: {ssl_cert_path}")
            return ssl_cert_path
        if ssl_verify:
            return True
        return False

    def _build_http_client(self) -> httpx.AsyncClient:
        """Create a configured HTTP client instance."""
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            verify=self._resolve_verify_config()
        )

    async def __aenter__(self):
        self._client = self._build_http_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _make_request(
        self, method: str, endpoint: str,
        token: str = None, data: Dict = None,
        params: Dict = None,
        headers: Dict = None
    ) -> APIResponse:
        """Make HTTP request with retry logic and circuit breaker."""
        if not self._circuit_breaker.allow_request():
            return APIResponse(success=False, error="Service temporarily unavailable")

        request_headers = {'Content-Type': 'application/json'}
        if token:
            request_headers['Authorization'] = f'Bearer {token}'
        if headers:
            request_headers.update(headers)

        total_attempts = max(1, self.max_retries)
        client = self._client
        owns_client = False
        if client is None:
            client = self._build_http_client()
            owns_client = True

        try:
            for attempt in range(total_attempts):
                try:
                    response = await client.request(
                        method=method,
                        url=endpoint,
                        json=data,
                        params=params,
                        headers=request_headers
                    )

                    self._circuit_breaker.record_success()

                    try:
                        payload = response.json() if response.content else {}
                    except ValueError:
                        payload = {}

                    if response.status_code in (200, 201):
                        # Unwrap standardized API response shape: {success, data, ...}
                        data = payload.get('data', payload) if isinstance(payload, dict) else payload
                        return APIResponse(
                            success=True,
                            data=data,
                            status_code=response.status_code
                        )
                    elif response.status_code == 401:
                        return APIResponse(
                            success=False,
                            error="Authentication failed",
                            status_code=401
                        )
                    elif response.status_code == 403:
                        return APIResponse(
                            success=False,
                            error="Access denied",
                            status_code=403
                        )
                    elif response.status_code == 404:
                        return APIResponse(
                            success=False,
                            error="Not found",
                            status_code=404
                        )
                    elif response.status_code == 409:
                        error_data = payload if isinstance(payload, dict) else {}
                        return APIResponse(
                            success=False,
                            error=error_data.get('message') or error_data.get('error', 'Conflict'),
                            status_code=409,
                            data=error_data
                        )
                    else:
                        error_data = payload if isinstance(payload, dict) else {}
                        return APIResponse(
                            success=False,
                            error=error_data.get('message') or error_data.get('error', f'HTTP {response.status_code}'),
                            status_code=response.status_code
                        )

                except httpx.TimeoutException:
                    logger.warning(f"Request timeout (attempt {attempt + 1}/{total_attempts}): {endpoint}")
                    if attempt < total_attempts - 1:
                        await asyncio.sleep(self.retry_delay * (attempt + 1))
                except httpx.ConnectError as e:
                    logger.error(f"Connection error: {e}")
                    self._circuit_breaker.record_failure()
                    if attempt < total_attempts - 1:
                        await asyncio.sleep(self.retry_delay * (attempt + 1))
                except Exception as e:
                    logger.error(f"Request error: {e}")
                    self._circuit_breaker.record_failure()
                    break
        finally:
            if owns_client:
                try:
                    await client.aclose()
                except Exception:
                    logger.debug("Failed to close temporary API client", exc_info=True)

        return APIResponse(success=False, error="Request failed after retries")

    # --- Staff Authentication ---

    async def staff_login(self, telegram_id: int, invite_token: str = None) -> APIResponse:
        """Staff login: pre-bound Telegram ID or one-time invite-token binding."""
        payload = {'telegram_id': str(telegram_id)}
        if invite_token:
            payload['invite_token'] = invite_token
        return await self._make_request(
            'POST',
            f'{config.business_api.auth_endpoint}/login',
            data=payload
        )

    async def refresh_token(self, refresh_token: str) -> Optional[Dict]:
        """Refresh JWT token"""
        response = await self._make_request(
            'POST',
            f'{config.business_api.auth_endpoint}/refresh',
            # Staff refresh endpoint currently authorizes via refresh-token JWT.
            token=refresh_token,
            # Keep body for backwards compatibility with alternate backend implementations.
            data={'refresh_token': refresh_token}
        )
        return response.data if response.success else None

    # --- Delivery Operations ---

    async def get_order_pool(self, token: str, filters: Dict = None) -> APIResponse:
        """Get unassigned orders available for pickup"""
        return await self._make_request(
            'GET',
            f'{config.business_api.delivery_endpoint}/pool',
            token=token,
            params=filters
        )

    async def accept_order(self, token: str, delivery_id: int) -> APIResponse:
        """Accept/pick an order from the pool"""
        return await self._make_request(
            'POST',
            f'{config.business_api.delivery_endpoint}/accept/{delivery_id}',
            token=token
        )

    async def update_delivery_status(
        self, token: str, delivery_id: int,
        status: str, metadata: Dict = None
    ) -> APIResponse:
        """Update delivery status"""
        data = {'status': status}
        if metadata:
            data['metadata'] = metadata
        return await self._make_request(
            'PUT',
            f'{config.business_api.delivery_endpoint}/{delivery_id}/status',
            token=token,
            data=data
        )

    async def update_location(
        self, token: str, delivery_id: int,
        latitude: float, longitude: float
    ) -> APIResponse:
        """Update delivery person's live location"""
        return await self._make_request(
            'POST',
            f'{config.business_api.delivery_endpoint}/{delivery_id}/location',
            token=token,
            data={'latitude': latitude, 'longitude': longitude}
        )

    async def get_active_deliveries(self, token: str) -> APIResponse:
        """Get my active deliveries"""
        return await self._make_request(
            'GET',
            f'{config.business_api.delivery_endpoint}/active',
            token=token
        )

    async def get_delivery_history(self, token: str, params: Dict = None) -> APIResponse:
        """Get my delivery history"""
        return await self._make_request(
            'GET',
            f'{config.business_api.delivery_endpoint}/history',
            token=token,
            params=params
        )

    async def get_delivery_stats(self, token: str, params: Dict = None) -> APIResponse:
        """Get my performance stats"""
        return await self._make_request(
            'GET',
            f'{config.business_api.delivery_endpoint}/stats',
            token=token,
            params=params
        )

    # --- Operator Operations ---

    async def create_client_user(self, token: str, user_data: Dict) -> APIResponse:
        """Create a new client user"""
        return await self._make_request(
            'POST',
            f'{config.business_api.operator_endpoint}/users',
            token=token,
            data=user_data
        )

    async def search_clients(self, token: str, query: str) -> APIResponse:
        """Search for clients"""
        return await self._make_request(
            'GET',
            f'{config.business_api.operator_endpoint}/users/search',
            token=token,
            params={'q': query}
        )

    async def create_order_for_client(self, token: str, order_data: Dict) -> APIResponse:
        """Create order for a client (operator flow)"""
        return await self._make_request(
            'POST',
            f'{config.business_api.operator_endpoint}/orders',
            token=token,
            data=order_data
        )

    async def get_recent_operator_orders(self, token: str) -> APIResponse:
        """Get recent orders created by this operator"""
        return await self._make_request(
            'GET',
            f'{config.business_api.operator_endpoint}/orders/recent',
            token=token
        )

    async def add_client_address(self, token: str, user_id: int, address_data: Dict) -> APIResponse:
        """Add address for a client"""
        return await self._make_request(
            'POST',
            f'{config.business_api.operator_endpoint}/users/{user_id}/addresses',
            token=token,
            data=address_data
        )

    async def mark_order_preparing(self, token: str, order_id: int) -> APIResponse:
        """Mark order as preparing"""
        return await self._make_request(
            'PUT',
            f'/api/v1/staff/orders/{order_id}/preparing',
            token=token
        )

    # --- Shared Operations ---

    async def get_products(self, token: str) -> APIResponse:
        """Get available products (for operator order creation)"""
        return await self._make_request(
            'GET',
            '/api/v1/products',
            token=token
        )

    async def get_user_addresses(self, token: str, user_id: int) -> APIResponse:
        """Get addresses for a specific user"""
        return await self._make_request(
            'GET',
            f'{config.business_api.operator_endpoint}/users/{user_id}/addresses',
            token=token
        )


# Global API client instance
api_client = StaffAPIClient()
