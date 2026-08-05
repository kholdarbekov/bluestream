"""
API client for communicating with the business application (staff endpoints)
"""
import httpx
import hashlib
import hmac
import json
import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
import asyncio
from datetime import datetime, timezone
import os

from staff_bot.config import config

logger = logging.getLogger('api_client')


# --- Retry policy: classify by FAILURE PHASE, not by exception name ---------
#
# RFC 9110 §9.2.2: a non-idempotent request MUST NOT be retried automatically
# once it may have reached the server. That binds the AUTOMATIC retrier — the
# loop in `_make_request` — not the driver who deliberately taps again. nginx
# removed transparent POST retry in 1.9.13 for exactly this reason; traefik
# #8990, dotnet/aspire #4631 and varnish-cache #4413 all landed the same rule.
#
# httpx 0.28.1 hierarchy, MEASURED (see
# .superpowers/sdd/2026-08-03-retry-safety/VERIFIED-FACTS.md §1) — there is NO
# base class meaning "the request never left us", so the safe set is enumerated
# by LEAF class and MUST be caught FIRST: `ConnectTimeout` and `PoolTimeout` are
# `TimeoutException` subclasses, so an `except httpx.TimeoutException` above
# this tuple would swallow the safe cases together with the unsafe ones.
# `ConnectError` is a `NetworkError` — disjoint from `TimeoutException` — which
# is why it needs naming here explicitly.
NEVER_DELIVERED_ERRORS = (
    httpx.ConnectTimeout,   # TCP/TLS handshake never completed
    httpx.PoolTimeout,      # never even got a connection out of the pool
    httpx.ConnectError,     # DNS failure / refused / unreachable
    httpx.ProxyError,       # CONNECT tunnel never established
)

# Read/write phase. `ReadTimeout` means the request WAS delivered and the
# response was lost. `WriteTimeout`/`WriteError` mean bytes may be partially on
# the wire. `RemoteProtocolError` means the server closed mid-exchange. All are
# ambiguous or worse, and re-sending a POST here is how one driver tap becomes
# two ledger rows. Listed AFTER the tuple above; the bases here would otherwise
# capture its leaves.
AMBIGUOUS_PHASE_ERRORS = (
    httpx.TimeoutException,      # what is LEFT: ReadTimeout, WriteTimeout
    httpx.NetworkError,          # ReadError, WriteError, CloseError
    httpx.RemoteProtocolError,
)

# `error_code` stamped on the terminal APIResponse when the loop gave up
# BECAUSE the failure was ambiguous (as opposed to a deterministic client-side
# error). It is deliberately absent from `BaseHandler.API_ERROR_CODE_KEY_MAP`,
# so today every handler renders it exactly as it renders any other transport
# failure. Note the resolution path: `_resolve_api_error_message`
# (`staff_bot/handlers/base.py:276-287`) falls THROUGH an unmapped `error_code`
# to `API_ERROR_MESSAGE_KEY_MAP`, which keys on the `error` STRING — and every
# terminal give-up carries "Request failed after retries" (:475), mapped at
# `base.py:102` to `staff.error.api.service_unavailable`. The "unexpected"
# default at `base.py:306` is unreachable on this path. That matters: the
# un-warned driver is told "service unavailable, please try later", which is
# actively WRONG advice when the write may already have landed. Its jobs are to
# make the "the request
# MAY already be applied" case greppable in the staff_bot logs, and to give the
# driver-facing "check the bottle statement before repeating" warning something
# to key on.
TRANSPORT_AMBIGUOUS_ERROR_CODE = "TRANSPORT_AMBIGUOUS"

# The only verbs this client may re-send after an ambiguous failure.
#
# PUT IS INCLUDED, and that is a verified claim, not an RFC assumption. The two
# PUTs this client issues are guarded state-machine transitions whose guard runs
# BEFORE any write:
#   * PUT /staff/delivery/{id}/status  -> business_app/services/staff_service.py
#     :1162-1167 raises STAFF_INVALID_STATUS_TRANSITION, and NO status in
#     shared/status_transitions.py:30-49 lists itself as an allowed next, so
#     every replay is refused before the `try:` at :1198. `delivery_attempts`
#     (:1223) lives inside that block and cannot be reached by a replay.
#   * PUT /staff/orders/{id}/preparing -> staff_service.py:1701-1705 raises
#     STAFF_ORDER_STATUS_INVALID_FOR_PREPARING before any write, and the
#     customer-facing Telegram notification at :1716-1719 fires only on success.
# Replay of either is zero writes + a deterministic 400, and
# `staff_bot/handlers/delivery/status_update.py:317-340` DEPENDS on that 400:
# it is the idempotent acknowledgement that stops a completed at-door delivery
# from being reported to the driver as a failure. Removing PUT from this set
# re-opens that bug.
#
# POST is excluded, with NO per-endpoint exception list (owner ruling): the
# rule stays a pure function of the verb so no future endpoint can opt into
# automatic POST retry by accident.
#
# ALL 25 POST call sites in this file lose automatic retry. For 21 of them that
# IS the fix — replaying them is exactly the double-write this change exists to
# stop. Four lose a retry that was doing useful work, and the owner accepted
# that trade knowingly:
#   * /staff/bottles/session/open   — its friendly 409 now needs a driver re-tap
#   * /addresses/reverse-geocode    — driver redoes the step by hand
#   * {delivery}/optimize-route     — ditto
#   * {delivery}/me/location        — ditto
# Three of the other 21 carry money or inventory and now rely on a manual
# re-tap when a response is lost — /staff/cash-collections (:754),
# /staff/reconciliation/session/submit (:771) and /staff/bottles/session/close
# (:899). Unlike session/open, session/close has no friendly-conflict arm
# (`bottle_collection.py:1332-1334` vs `:1240-1246`), so a lost response there
# surfaces as a generic error. That is the intended behaviour — an auto-retried
# money POST is the bug — but it is the sharp edge of this ruling, not a
# footnote.
RETRY_SAFE_METHODS = frozenset({"GET", "HEAD", "PUT"})


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

    @staticmethod
    def _may_retry_after_ambiguous_failure(method: str) -> bool:
        """True only for verbs that are idempotent by HTTP semantics AND
        verified idempotent server-side (see RETRY_SAFE_METHODS).

        `.upper()` is defensive, not cosmetic: httpx normalises the verb inside
        `httpx.Request.__init__`, but that happens AFTER this loop has already
        made its retry decision, so a future lowercase caller would otherwise
        fall silently into the "unknown verb" bucket.
        """
        return str(method).upper() in RETRY_SAFE_METHODS

    @staticmethod
    def _records_circuit_failure(exc: BaseException) -> bool:
        """Preserve the breaker's PRE-EXISTING accounting, verbatim.

        Before the phase split, `except httpx.TimeoutException` recorded NO
        breaker failure while `except httpx.ConnectError` and the generic
        `except Exception` both did — i.e. "every non-timeout transport failure
        records, every timeout does not". That invariant is reproduced here
        rather than changed: the phase split governs the RETRY decision only.

        Owner ruling: do NOT add `record_failure()` to the timeout path as an
        "improvement". The smaller diff and zero new outage-time behaviour were
        chosen deliberately over the extra breaker signal.
        """
        return not isinstance(exc, httpx.TimeoutException)

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
        # Sticky: once any attempt failed in a phase where the request may have
        # been applied, the terminal response says so — even if a later attempt
        # fails in a provably-never-delivered phase.
        ambiguous = False
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

                except NEVER_DELIVERED_ERRORS as exc:
                    # Connect phase: no connection, or no connection out of the
                    # pool. The request provably never reached the server, so
                    # re-sending ANY verb is safe.
                    logger.warning(
                        "Connect-phase failure (%s) on attempt %d/%d: %s %s",
                        exc.__class__.__name__, attempt + 1, total_attempts,
                        method, endpoint,
                    )
                    if self._records_circuit_failure(exc):
                        self._circuit_breaker.record_failure()
                    if attempt < total_attempts - 1:
                        await asyncio.sleep(self.retry_delay * (attempt + 1))
                except AMBIGUOUS_PHASE_ERRORS as exc:
                    # Read/write phase: the request may already be applied.
                    # Retry idempotent verbs only (RFC 9110 §9.2.2).
                    ambiguous = True
                    logger.warning(
                        "Ambiguous-phase failure (%s) on attempt %d/%d: %s %s",
                        exc.__class__.__name__, attempt + 1, total_attempts,
                        method, endpoint,
                    )
                    if self._records_circuit_failure(exc):
                        self._circuit_breaker.record_failure()
                    if not self._may_retry_after_ambiguous_failure(method):
                        logger.warning(
                            "Not retrying non-idempotent %s %s after an ambiguous "
                            "transport failure; the request may already be applied.",
                            method, endpoint,
                        )
                        break
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

        return APIResponse(
            success=False,
            error="Request failed after retries",
            error_code=TRANSPORT_AMBIGUOUS_ERROR_CODE if ambiguous else None,
        )

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

    async def get_operator_payment_methods(
        self, token: str, user_id: int, delivery_address_id: Optional[int] = None
    ) -> APIResponse:
        """Get debt-aware payment methods for an operator-created client order.

        ``delivery_address_id`` is optional and, when supplied, makes the
        backend evaluate the COD cap's PLACE arm as well as the person arm
        (business_app/api/staff.py — ``get_client_payment_methods``). Without it
        ``payment_restrictions`` can never carry ``restriction_scope == 'place'``
        and the operator would relay a coworker's debt as the customer's own.
        """
        params = {'delivery_address_id': delivery_address_id} if delivery_address_id else None
        return await self._make_request(
            'GET',
            f'{config.business_api.operator_endpoint}/users/{user_id}/payment-methods',
            token=token,
            params=params,
        )

    async def get_operator_order_estimate(
        self, token: str, user_id: int, items: List[Dict]
    ) -> APIResponse:
        """Price a basket FOR THE CLIENT — the only money the operator screen renders.

        POST, but READ-ONLY: the backend creates nothing (business_app/api/
        staff.py — ``get_client_order_estimate`` -> ``StaffService.
        estimate_phone_order``). It is a POST because the basket is a structured
        body, not a query string.

        ``get_products`` prices for whoever holds the token, and here that is the
        OPERATOR — so a corporate-contract client's screen showed the generic
        price against a contract charge. Never render catalogue money to an
        operator; render THIS.
        """
        return await self._make_request(
            'POST',
            f'{config.business_api.operator_endpoint}/users/{user_id}/order-estimate',
            token=token,
            data={'items': items},
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
