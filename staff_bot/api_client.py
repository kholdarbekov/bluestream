"""
API client for communicating with the business application (staff endpoints)
"""
import httpx
import hashlib
import hmac
import json
import logging
from typing import Dict, Any, Optional
from dataclasses import dataclass
import asyncio
from datetime import datetime, timezone
import os

from staff_bot.config import config

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
    error_code: Optional[str] = None


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
            verify=self._resolve_verify_config(),
            follow_redirects=True,
        )

    @staticmethod
    def _extract_response_error(payload: Any, default_message: str) -> tuple[str, Optional[str]]:
        """Normalize backend error payload into message and error code."""
        if isinstance(payload, dict):
            details = payload.get('details') or {}
            error_message = payload.get('message') or payload.get('error') or default_message
            error_code = payload.get('error_code') or details.get('error_code')
            return error_message, error_code
        return default_message, None

    def _log_unsuccessful_response(
        self,
        endpoint: str,
        response: httpx.Response,
        payload: Any,
        error_message: str,
        error_code: Optional[str],
    ) -> None:
        """Emit structured diagnostics for non-2xx backend responses."""
        logger.warning(
            "Staff API request failed: method=%s endpoint=%s status=%s error_code=%s error=%s payload_type=%s",
            response.request.method,
            endpoint,
            response.status_code,
            error_code,
            error_message,
            type(payload).__name__,
        )

    async def start(self) -> None:
        """Initialize the persistent HTTP client. Idempotent."""
        if self._client is None:
            self._client = self._build_http_client()

    async def aclose(self) -> None:
        """Close the persistent HTTP client. Called on bot shutdown."""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                logger.debug("Failed to close persistent API client", exc_info=True)
            finally:
                self._client = None

    async def __aenter__(self):
        # The client is shared across handlers and owned by the bot lifecycle
        # (started in post_init, closed in post_shutdown). Repeated `async with
        # api_client as client:` calls are safe and reuse the same underlying
        # httpx.AsyncClient — no per-request TLS handshake, no race on _client.
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # Do NOT close here — the client is shared across all handlers.
        # Closing on exit would (a) tear down a connection still in use by
        # a concurrent handler that entered `async with` after us, and
        # (b) force the next caller to pay the TLS handshake cost again.
        return None

    async def _make_request(
        self, method: str, endpoint: str,
        token: str = None, data: Dict = None,
        params: Dict = None,
        headers: Dict = None,
        sign: bool = False
    ) -> APIResponse:
        """Make HTTP request with retry logic and circuit breaker."""
        if not self._circuit_breaker.allow_request():
            return APIResponse(success=False, error="Service temporarily unavailable")

        request_headers = {'Content-Type': 'application/json'}
        if token:
            request_headers['Authorization'] = f'Bearer {token}'
        if headers:
            request_headers.update(headers)

        # Signed requests (currently: staff login) must send exactly the
        # bytes we sign — httpx `json=` serializes internally, so we can't
        # guarantee byte-equality with it. Pre-serialize the body ourselves
        # and send it via `content=` so the signed bytes == the bytes on the
        # wire == backend's `request.get_data()`.
        signed_body = None
        if sign and data is not None:
            secret = getattr(getattr(config, "security", None), "webhook_secret", None)
            if secret:
                signed_body = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                request_headers['Content-Type'] = 'application/json'
                request_headers['X-Bot-Webhook-Signature'] = hmac.new(
                    secret.encode("utf-8"), signed_body, hashlib.sha256
                ).hexdigest()
            else:
                logger.error("sign=True but config.security.webhook_secret is unset; login will 401")

        total_attempts = max(1, self.max_retries)
        client = self._client
        owns_client = False
        if client is None:
            client = self._build_http_client()
            owns_client = True

        try:
            for attempt in range(total_attempts):
                try:
                    if signed_body is not None:
                        response = await client.request(
                            method=method,
                            url=endpoint,
                            content=signed_body,
                            params=params,
                            headers=request_headers
                        )
                    else:
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
                        # Use a sentinel to distinguish "data key absent" from "data: null".
                        # The backend serializes with exclude_none=True, so when data IS null
                        # the "data" key is omitted entirely.  Without the sentinel,
                        # payload.get('data', payload) would fall back to the whole payload dict
                        # (truthy) and every "no data" response would look like a real result.
                        _MISSING = object()
                        raw_data = payload.get('data', _MISSING) if isinstance(payload, dict) else _MISSING
                        response_data = None if raw_data is _MISSING else raw_data
                        return APIResponse(
                            success=True,
                            data=response_data,
                            status_code=response.status_code
                        )
                    elif response.status_code == 401:
                        self._log_unsuccessful_response(
                            endpoint,
                            response,
                            payload,
                            "Authentication failed",
                            'STAFF_AUTH_REQUIRED',
                        )
                        return APIResponse(
                            success=False,
                            error="Authentication failed",
                            status_code=401,
                            error_code='STAFF_AUTH_REQUIRED',
                        )
                    elif response.status_code == 403:
                        error_message, error_code = self._extract_response_error(payload, "Access denied")
                        self._log_unsuccessful_response(
                            endpoint,
                            response,
                            payload,
                            error_message,
                            error_code,
                        )
                        return APIResponse(
                            success=False,
                            error=error_message,
                            status_code=403,
                            error_code=error_code,
                        )
                    elif response.status_code == 404:
                        error_message, error_code = self._extract_response_error(payload, "Not found")
                        self._log_unsuccessful_response(
                            endpoint,
                            response,
                            payload,
                            error_message,
                            error_code,
                        )
                        return APIResponse(
                            success=False,
                            error=error_message,
                            status_code=404,
                            error_code=error_code,
                        )
                    elif response.status_code == 409:
                        error_data = payload if isinstance(payload, dict) else {}
                        error_message, error_code = self._extract_response_error(error_data, 'Conflict')
                        self._log_unsuccessful_response(
                            endpoint,
                            response,
                            payload,
                            error_message,
                            error_code,
                        )
                        return APIResponse(
                            success=False,
                            error=error_message,
                            status_code=409,
                            data=error_data,
                            error_code=error_code,
                        )
                    else:
                        error_data = payload if isinstance(payload, dict) else {}
                        error_message, error_code = self._extract_response_error(
                            error_data,
                            f'HTTP {response.status_code}',
                        )
                        self._log_unsuccessful_response(
                            endpoint,
                            response,
                            payload,
                            error_message,
                            error_code,
                        )
                        return APIResponse(
                            success=False,
                            error=error_message,
                            status_code=response.status_code,
                            error_code=error_code,
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
            data=payload,
            sign=True
        )

    async def refresh_token(self, refresh_token: str) -> APIResponse:
        """Refresh JWT token.

        Returns the full APIResponse (rather than just `data`) so callers
        can tell an explicit auth failure (status 401/403) apart from a
        transport blip (no status / 5xx) — the former invalidates the
        cached session, the latter should keep it and let the user retry.
        """
        return await self._make_request(
            'POST',
            f'{config.business_api.auth_endpoint}/refresh',
            # Staff refresh endpoint currently authorizes via refresh-token JWT.
            token=refresh_token,
            # Keep body for backwards compatibility with alternate backend implementations.
            data={'refresh_token': refresh_token}
        )

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

    async def get_failed_deliveries(self, token: str) -> APIResponse:
        """Operator: list FAILED deliveries available for re-dispatch"""
        return await self._make_request(
            'GET',
            f'{config.business_api.delivery_endpoint}/failed',
            token=token
        )

    async def redispatch_delivery(self, token: str, delivery_id: int) -> APIResponse:
        """Operator: re-dispatch a FAILED delivery back to the pool"""
        return await self._make_request(
            'POST',
            f'{config.business_api.delivery_endpoint}/redispatch/{delivery_id}',
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

    async def update_driver_location(
        self, token: str,
        latitude: float, longitude: float
    ) -> APIResponse:
        """Update the driver's own current location (driver-level, no delivery
        required). Used for route-optimization purposes — accepts any one-shot
        or live location share. The backend re-runs route optimization on the
        spot and returns the freshly sorted active-deliveries payload so the
        bot can render the new sequence in one round-trip."""
        return await self._make_request(
            'POST',
            f'{config.business_api.delivery_endpoint}/me/location',
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

    async def optimize_route(self, token: str) -> APIResponse:
        """Manually re-run route optimization for the driver's active set.

        Returns the freshly sorted active-deliveries payload (same shape as
        get_active_deliveries) so the bot can edit-in-place.
        """
        return await self._make_request(
            'POST',
            f'{config.business_api.delivery_endpoint}/optimize-route',
            token=token,
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

    async def search_clients(self, token: str, query: str, search_type: str = 'phone') -> APIResponse:
        """Search for clients"""
        return await self._make_request(
            'GET',
            f'{config.business_api.operator_endpoint}/users/search',
            token=token,
            params={'q': query, 'type': search_type}
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

    async def get_operator_payment_methods(self, token: str, user_id: int) -> APIResponse:
        """Get debt-aware payment methods for an operator-created client order."""
        return await self._make_request(
            'GET',
            f'{config.business_api.operator_endpoint}/users/{user_id}/payment-methods',
            token=token,
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
            '/api/v1/products/',
            token=token
        )

    async def get_user_addresses(self, token: str, user_id: int) -> APIResponse:
        """Get addresses for a specific user"""
        return await self._make_request(
            'GET',
            f'{config.business_api.operator_endpoint}/users/{user_id}/addresses',
            token=token
        )

    async def get_customer_cod_statement(self, token: str, customer_id: int) -> APIResponse:
        """Get COD statement for a customer in staff flows."""
        return await self._make_request(
            'GET',
            f'/api/v1/staff/customers/{customer_id}/cod-statement',
            token=token,
        )

    async def search_customers(
        self,
        token: str,
        query_text: str,
        *,
        search_type: str = 'phone',
        only_with_open_cod: bool = True,
    ) -> APIResponse:
        """Search customers for COD collection workflows."""
        return await self._make_request(
            'GET',
            '/api/v1/staff/customers/search',
            token=token,
            params={
                'q': query_text,
                'type': search_type,
                'only_with_open_cod': str(only_with_open_cod).lower(),
            },
        )

    async def get_cod_debtors(self, token: str, *, page: int = 1, per_page: int = 10) -> APIResponse:
        """List customers with outstanding COD debt (paginated)."""
        return await self._make_request(
            'GET',
            '/api/v1/staff/customers/with-open-cod',
            token=token,
            params={'page': page, 'per_page': per_page},
        )

    async def record_cash_collection(self, token: str, payload: Dict) -> APIResponse:
        """Record a COD cash collection event."""
        return await self._make_request(
            'POST',
            '/api/v1/staff/cash-collections',
            token=token,
            data=payload,
        )

    async def get_reconciliation_session(self, token: str) -> APIResponse:
        """Get the driver's open reconciliation session."""
        return await self._make_request(
            'GET',
            '/api/v1/staff/reconciliation/session',
            token=token,
        )

    async def submit_reconciliation_session(self, token: str, payload: Dict) -> APIResponse:
        """Submit the driver's reconciliation session."""
        return await self._make_request(
            'POST',
            '/api/v1/staff/reconciliation/session/submit',
            token=token,
            data=payload,
        )

    # --- Try-out Operations ---

    async def create_tryout(self, token: str, payload: Dict) -> APIResponse:
        return await self._make_request(
            'POST',
            '/api/v1/staff/tryouts',
            token=token,
            data=payload,
        )

    async def get_tryout_task_pool(self, token: str) -> APIResponse:
        return await self._make_request(
            'GET',
            '/api/v1/staff/tryout-tasks/pool',
            token=token,
        )

    async def accept_tryout_task(self, token: str, task_id: int) -> APIResponse:
        return await self._make_request(
            'POST',
            f'/api/v1/staff/tryout-tasks/{task_id}/accept',
            token=token,
        )

    async def get_active_tryout_tasks(self, token: str) -> APIResponse:
        return await self._make_request(
            'GET',
            '/api/v1/staff/tryout-tasks/active',
            token=token,
        )

    async def get_active_tryouts(self, token: str) -> APIResponse:
        return await self._make_request(
            'GET',
            '/api/v1/staff/tryouts/active',
            token=token,
        )

    async def get_tryout_details(self, token: str, tryout_id: int) -> APIResponse:
        return await self._make_request(
            'GET',
            f'/api/v1/staff/tryouts/{tryout_id}',
            token=token,
        )

    async def complete_tryout_handoff(self, token: str, task_id: int, payload: Dict = None) -> APIResponse:
        return await self._make_request(
            'POST',
            f'/api/v1/staff/tryout-tasks/{task_id}/complete-handoff',
            token=token,
            data=payload or {},
        )

    async def record_tryout_pickup(self, token: str, task_id: int, payload: Dict) -> APIResponse:
        return await self._make_request(
            'POST',
            f'/api/v1/staff/tryout-tasks/{task_id}/record-pickup',
            token=token,
            data=payload,
        )

    async def get_tryout_history(self, token: str) -> APIResponse:
        return await self._make_request(
            'GET',
            '/api/v1/staff/tryouts/history',
            token=token,
        )

    async def reverse_geocode_address(self, token: str, latitude: float, longitude: float) -> APIResponse:
        return await self._make_request(
            'POST',
            '/api/v1/addresses/reverse-geocode',
            token=token,
            data={'latitude': latitude, 'longitude': longitude},
        )

    # --- Bottle Tracking ---

    async def get_customer_bottle_summary(self, token: str, customer_id: int) -> APIResponse:
        return await self._make_request(
            'GET',
            f'/api/v1/staff/bottles/customer/{customer_id}/summary',
            token=token,
        )

    async def get_customer_bottle_addresses(self, token: str, customer_id: int) -> APIResponse:
        return await self._make_request(
            'GET',
            f'/api/v1/staff/bottles/customer/{customer_id}/addresses',
            token=token,
        )

    async def record_bottle_collection(self, token: str, data: dict) -> APIResponse:
        return await self._make_request(
            'POST',
            '/api/v1/staff/bottles/collection',
            token=token,
            data=data,
        )

    async def create_bottle_fine(self, token: str, data: dict) -> APIResponse:
        return await self._make_request(
            'POST',
            '/api/v1/staff/bottles/fine',
            token=token,
            data=data,
        )

    # --- Bottle Session endpoints ---

    async def open_bottle_session(self, token: str, bottles_loaded: int, notes: str = None) -> APIResponse:
        data = {'bottles_loaded': bottles_loaded}
        if notes:
            data['notes'] = notes
        return await self._make_request('POST', '/api/v1/staff/bottles/session/open', token=token, data=data)

    async def get_current_bottle_session(self, token: str) -> APIResponse:
        return await self._make_request('GET', '/api/v1/staff/bottles/session/current', token=token)

    async def close_bottle_session(self, token: str, bottles_returned: int, notes: str = None) -> APIResponse:
        data = {'bottles_returned_to_warehouse': bottles_returned}
        if notes:
            data['notes'] = notes
        return await self._make_request('POST', '/api/v1/staff/bottles/session/close', token=token, data=data)

    async def get_my_bottle_sessions(self, token: str, page: int = 1, per_page: int = 10) -> APIResponse:
        return await self._make_request(
            'GET', '/api/v1/staff/bottles/sessions', token=token,
            params={'page': page, 'per_page': per_page},
        )

    # --- Co-driver session membership endpoints ---

    async def get_joinable_bottle_sessions(self, token: str) -> APIResponse:
        return await self._make_request('GET', '/api/v1/staff/bottles/sessions/joinable', token=token)

    async def join_bottle_session(self, token: str, session_id: int) -> APIResponse:
        return await self._make_request(
            'POST', '/api/v1/staff/bottles/session/join', token=token,
            data={'session_id': session_id},
        )

    async def leave_bottle_session(self, token: str) -> APIResponse:
        return await self._make_request('POST', '/api/v1/staff/bottles/session/leave', token=token)

    async def get_current_session_membership(self, token: str) -> APIResponse:
        return await self._make_request('GET', '/api/v1/staff/bottles/session/membership', token=token)

    # --- Bottle Transfer endpoints ---

    async def get_pending_bottle_transfers(self, token: str) -> APIResponse:
        return await self._make_request('GET', '/api/v1/staff/bottles/transfers/pending', token=token)

    async def initiate_bottle_transfer(self, token: str, receiver_driver_id: int, quantity: int, notes: str = None) -> APIResponse:
        data = {'receiver_driver_id': receiver_driver_id, 'quantity': quantity}
        if notes:
            data['notes'] = notes
        return await self._make_request('POST', '/api/v1/staff/bottles/transfers', token=token, data=data)

    async def confirm_bottle_transfer(self, token: str, transfer_id: int, confirmed_quantity: int, notes: str = None) -> APIResponse:
        data = {'confirmed_quantity': confirmed_quantity}
        if notes:
            data['notes'] = notes
        return await self._make_request(
            'POST', f'/api/v1/staff/bottles/transfers/{transfer_id}/confirm', token=token, data=data
        )

    async def get_drivers_available_to_invite(self, token: str) -> APIResponse:
        return await self._make_request(
            'GET', '/api/v1/staff/bottles/sessions/available-drivers', token=token
        )

    async def invite_driver_to_session(self, token: str, member_driver_id: int) -> APIResponse:
        return await self._make_request(
            'POST', '/api/v1/staff/bottles/session/invite', token=token,
            data={'member_driver_id': member_driver_id},
        )


# Global API client instance
api_client = StaffAPIClient()
