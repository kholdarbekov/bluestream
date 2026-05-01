"""Fake payment-gateway helpers for the TST-001 payment matrix.

Used exclusively by ``tests/integration/test_payment_matrix.py``. Two
responsibilities:

1.  Build properly-signed inbound webhook bodies for Click + Payme so the
    real ``WebhookSignatureVerifier`` accepts them.
2.  Provide strict-scripting fakes for outbound calls (Payme JSON-RPC,
    Click merchant API) when a test exercises a path that calls the
    gateway. Strict scripting means an unscripted call raises
    ``UnscriptedGatewayCall`` so silent test drift is impossible.

Why fakes instead of ``responses`` / ``requests-mock``: outbound paths are
already structured around two seams — ``PaymeProvider._payme_request`` and
``business_app.utils.http_client.request_with_retry`` (for Click). Patching
those seams keeps fakes fast (no socket layer), keyed on semantic identifiers
(method/action/txn_id) rather than URLs, and decoupled from network-library
internals.

Fiscalization (OFD) is intentionally out of scope — see TST-011.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import MagicMock


class UnscriptedGatewayCall(AssertionError):
    """Raised when a test calls a fake gateway without scripting a response.

    Subclasses ``AssertionError`` so it surfaces as a test failure rather than
    a silent error path swallowed by an ``except Exception`` block somewhere.
    """


# --------------------------------------------------------------------------- #
# Inbound webhook body builders
# --------------------------------------------------------------------------- #

def make_click_signature(payload: Dict[str, Any], secret_key: str) -> str:
    """Compute the Click MD5 sign-string the way the provider does.

    Mirrors the verification logic in
    ``ClickPaymentProviderService.verify_signature``: the canonical fields
    differ for prepare (action=0) and complete (action=1+).
    """
    action = str(payload.get('action', '0'))
    if action == '0':
        material = (
            f"{payload['click_trans_id']}{payload['service_id']}"
            f"{secret_key}{payload['merchant_trans_id']}"
            f"{payload['amount']}{payload['action']}{payload['sign_time']}"
        )
    else:
        material = (
            f"{payload['click_trans_id']}{payload['service_id']}"
            f"{secret_key}{payload['merchant_trans_id']}"
            f"{payload['merchant_prepare_id']}{payload['amount']}"
            f"{payload['action']}{payload['sign_time']}"
        )
    return hashlib.md5(material.encode('utf-8')).hexdigest()


def make_click_webhook_form(
    *,
    action: str,
    click_trans_id: str,
    merchant_trans_id: str,
    amount: str,
    secret_key: str,
    service_id: str = '55',
    merchant_prepare_id: Optional[str] = None,
    error: int = 0,
    error_note: str = '',
    click_paydoc_id: Optional[str] = None,
    sign_time: Optional[str] = None,
    nonce: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Return a Click webhook form body that passes signature + replay checks.

    ``action`` is ``'0'`` for prepare and ``'1'`` for complete. ``sign_time``
    and ``nonce`` default to fresh values so the replay window passes.
    """
    sign_time = sign_time or str(int(time.time()))
    # Stamp a unique nonce by default so successive calls don't trip the
    # WebhookSignatureVerifier replay guard. Tests intending to replay
    # identical bodies should pass ``nonce='fixed'`` explicitly.
    body: Dict[str, str] = {
        'click_trans_id': str(click_trans_id),
        'service_id': str(service_id),
        'merchant_trans_id': str(merchant_trans_id),
        'amount': str(amount),
        'action': str(action),
        'sign_time': sign_time,
        'error': str(error),
        'error_note': error_note,
    }
    if merchant_prepare_id is not None:
        body['merchant_prepare_id'] = str(merchant_prepare_id)
    if click_paydoc_id is None and str(action) == '1':
        # Click's protocol requires click_paydoc_id in Complete callbacks.
        # Default it deterministically per (trans_id, sign_time) so existing
        # tests stay protocol-compliant without explicit boilerplate.
        click_paydoc_id = f"paydoc-{click_trans_id}-{sign_time}"
    if click_paydoc_id is not None:
        body['click_paydoc_id'] = str(click_paydoc_id)
    if extra:
        body.update({str(k): str(v) for k, v in extra.items()})

    body['sign_string'] = make_click_signature(body, secret_key)
    # The verifier's replay guard reads timestamp + nonce off the form body
    # as a fallback to headers. Surfacing them keeps the path the same as
    # production traffic where Click does not actually send X-Nonce.
    body['timestamp'] = sign_time
    if nonce is None:
        nonce = f"click-test-{click_trans_id}-{action}-{sign_time}-{uuid.uuid4().hex[:8]}"
    body['nonce'] = nonce
    return body


def make_payme_webhook_body(
    *,
    method: str,
    params: Dict[str, Any],
    request_id: int = 1,
    timestamp: Optional[int] = None,
    nonce: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a Payme JSON-RPC 2.0 envelope.

    Payme signature verification uses Basic-auth headers, not a body field.
    Tests should patch ``PaymeProvider.verify_payme_signature`` to ``True``
    rather than try to forge ``X-Auth`` — the matrix tests the protocol
    handler, not the auth scheme (which has its own unit tests).
    """
    body: Dict[str, Any] = {
        'jsonrpc': '2.0',
        'id': request_id,
        'method': method,
        'params': dict(params),
    }
    body['timestamp'] = timestamp if timestamp is not None else int(time.time())
    # Fresh nonce by default — real gateway retries supply new nonces; PAY-002
    # idempotency keys on params.id, not nonce. Tests that want a true replay
    # of identical bodies should pass ``nonce='fixed'`` explicitly.
    body['nonce'] = nonce or f"payme-test-{method}-{request_id}-{uuid.uuid4().hex}"
    return body


# --------------------------------------------------------------------------- #
# Outbound call fakes (strict scripting)
# --------------------------------------------------------------------------- #

@dataclass
class _ScriptedResponse:
    """A queued response for a strict-scripting fake.

    ``payload`` is returned verbatim if ``raise_exc`` is ``None``; otherwise
    the exception instance is raised. ``label`` is for diagnostics only.
    """

    label: str
    payload: Any = None
    raise_exc: Optional[BaseException] = None


@dataclass
class FakePayme:
    """Strict fake for ``PaymeProvider._payme_request``.

    Scripts are keyed by the JSON-RPC ``method`` name. Each ``script(...)``
    appends one queued response. Every call pops the head of the queue.
    Calling without a scripted response raises ``UnscriptedGatewayCall``.
    """

    queues: Dict[str, List[_ScriptedResponse]] = field(default_factory=dict)
    calls: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)

    def script(
        self,
        method: str,
        *,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
        raise_exc: Optional[BaseException] = None,
        label: str = '',
    ) -> 'FakePayme':
        """Queue a Payme response for ``method``.

        Pass exactly one of ``result``, ``error``, or ``raise_exc``. Returns
        ``self`` for chaining.
        """
        if sum(x is not None for x in (result, error, raise_exc)) != 1:
            raise ValueError(
                "FakePayme.script: pass exactly one of result, error, raise_exc"
            )
        if raise_exc is not None:
            payload = None
        elif error is not None:
            payload = {'error': error}
        else:
            payload = {'result': result}
        self.queues.setdefault(method, []).append(
            _ScriptedResponse(
                label=label or f"{method}.{len(self.queues.get(method, []))}",
                payload=payload,
                raise_exc=raise_exc,
            )
        )
        return self

    def __call__(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append((method, dict(params)))
        queue = self.queues.get(method)
        if not queue:
            raise UnscriptedGatewayCall(
                f"FakePayme: no scripted response for method={method!r}; "
                f"params={params!r}. Call .script({method!r}, ...) in the test."
            )
        scripted = queue.pop(0)
        if scripted.raise_exc is not None:
            raise scripted.raise_exc
        return scripted.payload

    def assert_drained(self) -> None:
        """Fail the test if any scripted responses were never consumed."""
        leftovers = {m: [r.label for r in q] for m, q in self.queues.items() if q}
        if leftovers:
            raise AssertionError(
                f"FakePayme: scripted responses never consumed: {leftovers}"
            )


@dataclass
class FakeClickMerchant:
    """Strict fake for ``request_with_retry`` when called from Click.

    Click's outbound merchant API goes through
    ``business_app.utils.http_client.request_with_retry``. The fake replaces
    that callable; scripts are keyed by ``(method, url_substring)`` so a
    single test can script different responses per endpoint (status, refund,
    fiscalization, etc.) without coupling to full URLs.
    """

    queues: Dict[Tuple[str, str], List[_ScriptedResponse]] = field(default_factory=dict)
    calls: List[Dict[str, Any]] = field(default_factory=list)

    def script(
        self,
        *,
        method: str,
        url_contains: str,
        json_body: Optional[Dict[str, Any]] = None,
        raise_exc: Optional[BaseException] = None,
        status_code: int = 200,
        label: str = '',
    ) -> 'FakeClickMerchant':
        """Queue a Click outbound response.

        Pass either ``json_body`` (returned as the parsed JSON body of the
        ``requests.Response``) or ``raise_exc`` (raised inside
        ``request_with_retry``).
        """
        if (json_body is None) == (raise_exc is None):
            raise ValueError(
                "FakeClickMerchant.script: pass exactly one of json_body or raise_exc"
            )
        key = (method.upper(), url_contains)
        payload = None
        if json_body is not None:
            payload = (status_code, json_body)
        self.queues.setdefault(key, []).append(
            _ScriptedResponse(
                label=label or f"{method.upper()} ~{url_contains}",
                payload=payload,
                raise_exc=raise_exc,
            )
        )
        return self

    def __call__(self, **kwargs):
        method = (kwargs.get('method') or 'GET').upper()
        url = kwargs.get('url') or ''
        self.calls.append({'method': method, 'url': url, **{k: v for k, v in kwargs.items() if k not in ('method', 'url')}})

        # Find the most-specific matching scripted queue for this call. Most
        # specific = longest url_contains substring. Falls back to an exact
        # method+empty match if a test wants a global default.
        candidate: Optional[Tuple[str, str]] = None
        for (m, sub), queue in self.queues.items():
            if m != method or not queue:
                continue
            if sub and sub not in url:
                continue
            if candidate is None or len(sub) > len(candidate[1]):
                candidate = (m, sub)
        if candidate is None:
            raise UnscriptedGatewayCall(
                f"FakeClickMerchant: no scripted response for "
                f"method={method} url={url!r}. Call .script(method={method!r}, "
                f"url_contains=..., json_body=...) in the test."
            )

        scripted = self.queues[candidate].pop(0)
        if scripted.raise_exc is not None:
            raise scripted.raise_exc

        status_code, body = scripted.payload
        # Build a minimal ``requests.Response``-shaped object — the production
        # code only ever calls ``.raise_for_status()``, ``.json()``, and reads
        # ``.status_code`` / ``.text``.
        response = MagicMock(name='FakeClickResponse')
        response.status_code = status_code
        response.text = json.dumps(body)
        response.json.return_value = body
        if status_code >= 400:
            from requests import HTTPError
            response.raise_for_status.side_effect = HTTPError(
                f"{status_code} Client Error", response=response
            )
        else:
            response.raise_for_status.return_value = None
        return response

    def assert_drained(self) -> None:
        leftovers = {f"{m} ~{s}": [r.label for r in q] for (m, s), q in self.queues.items() if q}
        if leftovers:
            raise AssertionError(
                f"FakeClickMerchant: scripted responses never consumed: {leftovers}"
            )


# --------------------------------------------------------------------------- #
# Test config helpers
# --------------------------------------------------------------------------- #

# Fixed test secrets so signature builders + the verifier agree on the
# material. Tests should set these on ``app.config`` via fixture override.
TEST_CLICK_SHOP_SECRET_KEY = 'test-click-shop-secret'
TEST_CLICK_SHOP_SERVICE_ID = '55'
TEST_PAYME_MERCHANT_ID = 'test-payme-merchant'
TEST_PAYME_SECRET_KEY = 'test-payme-secret'


def apply_test_provider_secrets(app) -> None:
    """Set the per-provider config the fakes assume.

    The TestingConfig deliberately leaves payment secrets unset (they're
    pulled from Docker secrets in real envs); without these, the verifier
    rejects every webhook with a missing-key error before the matrix runs.
    """
    app.config['CLICK_SHOP_SECRET_KEY'] = TEST_CLICK_SHOP_SECRET_KEY
    app.config['CLICK_SHOP_SERVICE_ID'] = TEST_CLICK_SHOP_SERVICE_ID
    app.config['CLICK_SECRET_KEY'] = TEST_CLICK_SHOP_SECRET_KEY
    app.config['CLICK_SERVICE_ID'] = TEST_CLICK_SHOP_SERVICE_ID
    app.config['CLICK_MERCHANT_ID'] = 'test-click-merchant'
    app.config['CLICK_SHOP_MERCHANT_ID'] = 'test-click-merchant'
    app.config['PAYME_MERCHANT_ID'] = TEST_PAYME_MERCHANT_ID
    app.config['PAYME_SECRET_KEY'] = TEST_PAYME_SECRET_KEY
    # Empty allowlist => any source IP. Keeps the matrix from depending on
    # a fixture that sets request.remote_addr.
    app.config['CLICK_CALLBACK_ALLOWLIST'] = []
    app.config['PAYME_WEBHOOK_IPS'] = []


__all__ = [
    'UnscriptedGatewayCall',
    'make_click_signature',
    'make_click_webhook_form',
    'make_payme_webhook_body',
    'FakePayme',
    'FakeClickMerchant',
    'TEST_CLICK_SHOP_SECRET_KEY',
    'TEST_PAYME_SECRET_KEY',
    'apply_test_provider_secrets',
]
