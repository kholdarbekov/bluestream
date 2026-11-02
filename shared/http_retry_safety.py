"""One retry-safety rule for both bots' HTTP clients (RFC 9110 section 9.2.2).

Imports nothing from business_app: it ships in the bot images.
"""
import httpx

# --- Retry policy: classify by FAILURE PHASE, not by exception name ---------
#
# RFC 9110 §9.2.2: a non-idempotent request MUST NOT be retried automatically
# once it may have reached the server. That binds the AUTOMATIC retrier (the
# retry loop in each bot's api_client `_make_request`), not the person who deliberately taps again. nginx
# removed transparent POST retry in 1.9.13 for exactly this reason; traefik
# #8990, dotnet/aspire #4631 and varnish-cache #4413 all landed the same rule.
#
# httpx 0.28.1 hierarchy, MEASURED (see
# a probe of the installed httpx) — there is NO
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
# ambiguous or worse, and re-sending a POST here is how one tap becomes
# two writes. Listed AFTER the tuple above; the bases here would otherwise
# capture its leaves.
AMBIGUOUS_PHASE_ERRORS = (
    httpx.TimeoutException,      # what is LEFT: ReadTimeout, WriteTimeout
    httpx.NetworkError,          # ReadError, WriteError, CloseError
    httpx.RemoteProtocolError,
)

# The verbs that are idempotent by HTTP semantics and safe for ANY client to replay after an
# ambiguous failure. A client that has verified more verbs idempotent server-side (the staff bot
# adds PUT) builds its own set on top of this one and passes it to `may_retry_automatically`.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD"})


def may_retry_automatically(exc: BaseException, method: str, replayable_methods: frozenset) -> bool:
    """May an automatic retry loop send `method` again after `exc`?

    A request that never left us (NEVER_DELIVERED_ERRORS) can be re-sent with any verb. Anything
    else (a read/write-phase failure, or an exception this rule does not know) may have reached
    the server, so only a verb in `replayable_methods` is re-sent.
    """
    if isinstance(exc, NEVER_DELIVERED_ERRORS):
        return True
    return str(method).upper() in replayable_methods
