"""Unit tests: the staff-bot transport must not auto-retry a request that may
already have been applied (L1 of the 2026-08-03 retry-safety fix).

RFC 9110 §9.2.2: a non-idempotent request MUST NOT be retried automatically
once it may have reached the server. That binds the *automatic* retrier — the
loop in `StaffAPIClient._make_request` — not a driver who deliberately taps
again. Before this fix `staff_bot/api_client.py` caught `httpx.TimeoutException`
and `httpx.ConnectError` and re-sent **every** verb, so a POST whose bytes were
delivered and whose response was lost became two ledger rows / two fines.

The decision is split by FAILURE PHASE, not by exception name:

* never delivered  -> ConnectTimeout, PoolTimeout, ConnectError, ProxyError
                      (safe to re-send ANY verb)
* ambiguous        -> ReadTimeout, WriteTimeout, ReadError, WriteError,
                      RemoteProtocolError (re-send only `RETRY_SAFE_METHODS`)

⚠️ The subclass-ordering trap these tests exist to catch: all four `*Timeout`
classes ARE subclasses of `httpx.TimeoutException` (measured on the installed
httpx 0.28.1, see `.superpowers/sdd/2026-08-03-retry-safety/VERIFIED-FACTS.md`
§1), so an `except httpx.TimeoutException` clause placed ABOVE the
never-delivered tuple silently swallows the safe connect-phase cases and the
whole fix degrades into a no-op for `ConnectTimeout`/`PoolTimeout`.
`test_connect_timeout_on_a_post_still_retries_despite_being_a_timeoutexception`
is the pin for exactly that ordering.

There is **zero** other coverage of this loop: the only integration harness that
looks like it exercises retries — `_Bridge._request` in
`tests/integration/test_staff_bot_place_full_e2e.py` — hard-codes its double
send and never calls `StaffAPIClient._make_request`, so it cannot see a
regression here. This file is the whole safety net.
"""

import asyncio

import httpx
import pytest

from staff_bot import api_client as api_client_module
from staff_bot.api_client import (
    RETRY_SAFE_METHODS,
    TRANSPORT_AMBIGUOUS_ERROR_CODE,
    APIResponse,
    StaffAPIClient,
)


# --- Harness ---------------------------------------------------------------


class _RaisingHTTPClient:
    """Stands in for `httpx.AsyncClient`, raising a chosen transport error.

    Records every send so a test can assert the exact number of attempts the
    retry loop made — which is the whole point: "did this POST go out once, or
    three times?".
    """

    def __init__(self, exc_class, message="boom"):
        self.exc_class = exc_class
        self.message = message
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        raise self.exc_class(self.message)


def _client_raising(exc_class, max_retries=3):
    """A `StaffAPIClient` whose transport always raises `exc_class`."""
    client = StaffAPIClient()
    client.max_retries = max_retries
    client.retry_delay = 0  # never actually sleep in a unit test
    fake_http = _RaisingHTTPClient(exc_class)
    client._client = fake_http
    return client, fake_http


def _send(client, method, endpoint="/api/v1/staff/bottles/collect"):
    return asyncio.run(client._make_request(method, endpoint))


# Never delivered: the request provably never left us / never completed a
# connection, so re-sending ANY verb is safe.
NEVER_DELIVERED = [
    pytest.param(httpx.ConnectTimeout, id="ConnectTimeout"),
    pytest.param(httpx.PoolTimeout, id="PoolTimeout"),
    pytest.param(httpx.ConnectError, id="ConnectError"),
    pytest.param(httpx.ProxyError, id="ProxyError"),
]

# Ambiguous: the request may already be applied on the server.
AMBIGUOUS = [
    pytest.param(httpx.ReadTimeout, id="ReadTimeout"),
    pytest.param(httpx.WriteTimeout, id="WriteTimeout"),
    pytest.param(httpx.ReadError, id="ReadError"),
    pytest.param(httpx.WriteError, id="WriteError"),
    pytest.param(httpx.RemoteProtocolError, id="RemoteProtocolError"),
]


# --- The policy constants (source-level pins) -------------------------------


@pytest.mark.unit
class TestRetryPolicyConstants:
    def test_put_must_stay_retry_safe(self):
        """PUT must remain in `RETRY_SAFE_METHODS`. Dropping it re-opens BUG 15.

        `staff_bot/handlers/delivery/status_update.py:331` is a
        retry-DEPENDENT compensator: it reaches its success path when the
        SECOND attempt of an at-door completion returns
        `STAFF_INVALID_STATUS_TRANSITION`, and only then clears the delivery
        cash flow (`:340`). If PUT stopped being retried, the first ambiguous
        failure would `break` with `error_code=None`, the driver would be told
        an already-DELIVERED, already-billed order FAILED, and
        `pending_delivery_cash_flow` would stay armed.

        Retrying PUT is safe on VERIFIED server-side grounds, not RFC faith:
        * `business_app/services/staff_service.py:1162-1167` raises
          `STAFF_INVALID_STATUS_TRANSITION` BEFORE the `try:` at `:1198` that
          holds every write, including `delivery_attempts += 1` at `:1223`; and
          no status lists itself as an allowed next in
          `shared/status_transitions.py:30-49`, so a replay does ZERO writes.
        * `staff_service.py:1701-1705` guards `mark_order_preparing` the same
          way, with the customer notification (`:1716-1719`) on the success
          path only.

        This assertion is the ONLY guard: the integration test that covers the
        at-door PUT (`tests/integration/test_staff_bot_place_full_e2e.py:3467`)
        cannot catch a regression here, because its `_Bridge._request` harness
        hard-codes the double send and never calls `_make_request`.
        """
        assert "PUT" in RETRY_SAFE_METHODS

    def test_retry_safe_methods_are_verb_only_with_no_post_exception(self):
        """Owner RULING 1: the rule is a pure function of the HTTP method.

        `RETRY_SAFE_METHODS` is exactly {GET, HEAD, PUT} — no POST allow-list,
        no per-endpoint exception table, so no future endpoint can opt into
        automatic POST retry by accident.

        ALL 25 POST call sites in `api_client.py` lose automatic retry. For 21
        that IS the fix. Four lose a retry that was doing useful work, and the
        owner accepted that trade knowingly: session/open's friendly 409,
        reverse-geocode, optimize-route and me/location. Three of the other 21
        carry money or inventory and now rely on a manual driver re-tap when a
        response is lost — /staff/cash-collections,
        /staff/reconciliation/session/submit and /staff/bottles/session/close.
        """
        assert set(RETRY_SAFE_METHODS) == {"GET", "HEAD", "PUT"}
        assert "POST" not in RETRY_SAFE_METHODS
        assert not hasattr(api_client_module, "RETRY_SAFE_POST_ENDPOINTS")

    def test_phase_tuples_do_not_overlap_and_are_ordered_most_specific_first(self):
        """The never-delivered leaves must not be reachable via the ambiguous bases.

        Python's except ladder is first-match-wins, so the never-delivered
        LEAF classes have to be caught first; this pins that each of them is
        genuinely a subclass of something in the ambiguous tuple (i.e. the
        ordering is load-bearing, not decorative) for the timeout pair, and
        that no ambiguous class is a member of the never-delivered tuple.
        """
        never = api_client_module.NEVER_DELIVERED_ERRORS
        ambiguous = api_client_module.AMBIGUOUS_PHASE_ERRORS

        assert httpx.ConnectTimeout in never and httpx.PoolTimeout in never
        assert httpx.ConnectError in never and httpx.ProxyError in never
        # The trap, restated as an assertion: these two ARE TimeoutExceptions.
        assert issubclass(httpx.ConnectTimeout, httpx.TimeoutException)
        assert issubclass(httpx.PoolTimeout, httpx.TimeoutException)
        assert httpx.TimeoutException in ambiguous
        # ...and no ambiguous leaf sneaks into the never-delivered tuple.
        for ambiguous_leaf in (
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
        ):
            assert not issubclass(ambiguous_leaf, tuple(never))


# --- Connect phase: safe to retry any verb ---------------------------------


@pytest.mark.unit
class TestNeverDeliveredFailuresRetryAnyVerb:
    @pytest.mark.parametrize("exc_class", NEVER_DELIVERED)
    def test_a_post_is_retried_max_retries_times(self, exc_class):
        client, fake_http = _client_raising(exc_class)

        response = _send(client, "POST")

        assert len(fake_http.calls) == 3, (
            f"{exc_class.__name__} never reached the server; a POST must still "
            "use the full retry budget"
        )
        assert isinstance(response, APIResponse)
        assert response.success is False
        assert response.error == "Request failed after retries"

    def test_connect_timeout_on_a_post_still_retries_despite_being_a_timeoutexception(self):
        """The subclass-ordering trap, pinned.

        `httpx.ConnectTimeout` IS an `httpx.TimeoutException`. If the ambiguous
        clause (whose tuple contains the base `TimeoutException`) is ordered
        above the never-delivered tuple, this connect-phase failure is
        misclassified as ambiguous and the POST breaks after ONE send. A naive
        reordering of the except ladder ships as a silent no-op; this test is
        what turns that into a red suite.
        """
        client, fake_http = _client_raising(httpx.ConnectTimeout)

        response = _send(client, "POST")

        assert len(fake_http.calls) == 3
        assert response.error_code is None

    @pytest.mark.parametrize("exc_class", NEVER_DELIVERED)
    def test_exhaustion_is_not_labelled_ambiguous(self, exc_class):
        """A never-delivered exhaustion must NOT claim the write may be applied."""
        client, _ = _client_raising(exc_class)

        response = _send(client, "POST")

        assert response.error_code is None


# --- Ambiguous phase: retry only verified-idempotent verbs ------------------


@pytest.mark.unit
class TestAmbiguousFailuresDoNotRetryNonIdempotentVerbs:
    @pytest.mark.parametrize("exc_class", AMBIGUOUS)
    def test_a_post_is_sent_exactly_once(self, exc_class):
        client, fake_http = _client_raising(exc_class)

        response = _send(client, "POST")

        assert len(fake_http.calls) == 1, (
            f"{exc_class.__name__} may already have been applied server-side; "
            "re-sending the POST is how one driver tap becomes two ledger rows"
        )
        assert response.success is False

    @pytest.mark.parametrize("exc_class", AMBIGUOUS)
    def test_the_terminal_response_is_labelled_transport_ambiguous(self, exc_class):
        client, _ = _client_raising(exc_class)

        response = _send(client, "POST")

        assert response.error_code == TRANSPORT_AMBIGUOUS_ERROR_CODE
        assert TRANSPORT_AMBIGUOUS_ERROR_CODE == "TRANSPORT_AMBIGUOUS"

    def test_a_put_is_still_retried_after_an_ambiguous_failure(self):
        """The at-door delivery-completion path. See the PUT pin above."""
        client, fake_http = _client_raising(httpx.ReadTimeout)

        _send(client, "PUT", "/api/v1/staff/delivery/7/status")

        assert len(fake_http.calls) == 3

    def test_a_get_is_still_retried_after_an_ambiguous_failure(self):
        client, fake_http = _client_raising(httpx.ReadTimeout)

        _send(client, "GET", "/api/v1/staff/deliveries")

        assert len(fake_http.calls) == 3

    def test_a_lowercase_verb_is_normalised_before_the_decision(self):
        """`.upper()` is defensive, not cosmetic: httpx normalises the verb
        inside `httpx.Request.__init__`, which happens AFTER this loop has
        already made its retry decision."""
        client, fake_http = _client_raising(httpx.ReadTimeout)

        _send(client, "get", "/api/v1/staff/deliveries")

        assert len(fake_http.calls) == 3


# --- Deterministic (non-transport) errors still fail fast -------------------


@pytest.mark.unit
class TestDeterministicErrorsFailFast:
    @pytest.mark.parametrize(
        "exc_class",
        [
            pytest.param(httpx.UnsupportedProtocol, id="UnsupportedProtocol"),
            pytest.param(httpx.LocalProtocolError, id="LocalProtocolError"),
            pytest.param(TypeError, id="TypeError"),
        ],
    )
    def test_a_deterministic_error_is_sent_once_and_not_labelled_ambiguous(self, exc_class):
        client, fake_http = _client_raising(exc_class)

        response = _send(client, "GET", "/api/v1/staff/deliveries")

        assert len(fake_http.calls) == 1
        assert response.success is False
        assert response.error_code is None


# --- Circuit-breaker accounting --------------------------------------------
#
# RULING 3's operative instruction — never add `record_failure()` to the TIMEOUT
# path — is held by `test_a_timeout_does_not_trip_the_breaker` below, and that
# guard must not be weakened.
#
# The class is NOT named "…IsUnchanged", because the breaker's behaviour is not
# unchanged. The RULE is unchanged; five classes' attempt COUNTS moved as a
# consequence of RULING 1's phase split. Under HEAD, `ProxyError`, `ReadError`,
# `WriteError`, `RemoteProtocolError` and `CloseError` fell into the generic
# `except Exception` clause (`api_client.py:334-337`), which recorded ONCE and
# then broke. They now sit in the phase sets and therefore RETRY, recording
# three times per request. Since `_failure_count` is cumulative and only
# `record_success()` resets it (`:116-118`) against `failure_threshold=5`
# (`:150`), the breaker now OPENS after 2 failing requests instead of 5.
#
# The owner accepted that delta knowingly (2026-08-03): nothing unsafe is
# replayed — `ProxyError` never reached the server, and the other four retry
# only on `RETRY_SAFE_METHODS` — and a breaker that opens sooner fails fast
# against a dead backend. The pins below nail the delta in the direction it
# actually moved, so a future reader does not mistake it for a regression.


@pytest.mark.unit
class TestCircuitBreakerAccounting:
    @pytest.mark.parametrize(
        "exc_class",
        [
            pytest.param(httpx.ConnectTimeout, id="ConnectTimeout-never-delivered"),
            pytest.param(httpx.PoolTimeout, id="PoolTimeout-never-delivered"),
            pytest.param(httpx.ReadTimeout, id="ReadTimeout-ambiguous"),
            pytest.param(httpx.WriteTimeout, id="WriteTimeout-ambiguous"),
        ],
    )
    def test_a_timeout_does_not_trip_the_breaker(self, exc_class):
        """RULING 3: leave the breaker exactly as it was.

        Before this fix `except httpx.TimeoutException` recorded NO breaker
        failure. It still records none — for connect-phase and ambiguous-phase
        timeouts alike. Do not "improve" this by adding `record_failure()` to
        the timeout path.
        """
        client, _ = _client_raising(exc_class)

        _send(client, "POST")

        assert client._circuit_breaker._failure_count == 0
        assert client._circuit_breaker.state == client._circuit_breaker.CLOSED

    def test_connect_error_still_records_one_failure_per_attempt(self):
        """`httpx.ConnectError` recorded a failure per attempt before the fix
        (`api_client.py:329-333`) and must keep doing so."""
        client, fake_http = _client_raising(httpx.ConnectError)

        _send(client, "POST")

        assert len(fake_http.calls) == 3
        assert client._circuit_breaker._failure_count == 3

    def test_a_non_timeout_ambiguous_error_on_a_post_still_records_exactly_one(self):
        """`httpx.ReadError` fell through to the generic `except Exception`
        clause before the fix, which recorded a failure and broke.

        On a **POST** the count is unchanged at 1, because POST is not in
        `RETRY_SAFE_METHODS`, so an ambiguous failure still breaks after one
        send. That is a coincidence of the verb, NOT a general property — see
        `test_a_non_timeout_ambiguous_error_on_a_get_now_records_three` for the
        case that actually moved.
        """
        client, fake_http = _client_raising(httpx.ReadError)

        _send(client, "POST")

        assert len(fake_http.calls) == 1
        assert client._circuit_breaker._failure_count == 1

    def test_proxy_error_on_a_post_now_sends_and_records_three_times(self):
        """ACCEPTED DELTA (owner ruling 2026-08-03) — pinned, not lamented.

        `httpx.ProxyError` is the one class that changed on EVERY verb,
        including POST: under HEAD it hit the generic `except Exception` clause
        (1 send, 1 record, then break); it is now classified NEVER-DELIVERED —
        a failed CONNECT tunnel means the request never reached the server — so
        re-sending any verb is safe and it retries the full 3 attempts.

        If this ever needs undoing, the fix is to make `ProxyError` break again;
        do NOT cap records at one per request, because `ConnectError` already
        recorded 3 per request under HEAD and a cap would introduce a new
        divergence in order to remove one.
        """
        client, fake_http = _client_raising(httpx.ProxyError)

        _send(client, "POST")

        assert len(fake_http.calls) == 3
        assert client._circuit_breaker._failure_count == 3

    def test_a_non_timeout_ambiguous_error_on_a_get_now_records_three(self):
        """ACCEPTED DELTA — the backend-restart case.

        `httpx.RemoteProtocolError` (server closed the connection mid-response,
        e.g. a rolling restart) also fell into the generic clause under HEAD:
        1 record, then break. GET is in `RETRY_SAFE_METHODS`, so it now retries
        and records once per attempt. With `failure_threshold=5`, two such
        requests open the breaker where five were needed before.
        """
        client, fake_http = _client_raising(httpx.RemoteProtocolError)

        _send(client, "GET")

        assert len(fake_http.calls) == 3
        assert client._circuit_breaker._failure_count == 3
