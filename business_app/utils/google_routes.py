"""Google Routes API (computeRoutes) — traffic-aware ETA of ONE leg, for the
customer's "driver is on the way" estimate only.

One number per call: the ETA of one driving leg (spec 8.2). Billing:
`computeRoutes` bills per REQUEST; `computeRouteMatrix` bills per ELEMENT —
this module must NEVER call the matrix endpoint.

Owner rule 2026-11-03: Google is paid, so it serves the customer's "driver is
on the way" estimate and nothing else. The driver's route card, rebuilt on
every location update, reads the distance matrix and never calls this module.
Every call passes a DAILY CAP here (`_under_daily_cap`): one Redis counter per
Tashkent day, `GOOGLE_ROUTES_DAILY_CAP` calls (150, so a 31-day month stays
inside Google's 5,000/month free allowance). Over the cap no request is sent.

Degrades silently: any failure (unconfigured key, spent daily cap, 4xx/5xx,
timeout, malformed body) returns None. The caller then shows that tap no
minutes: the customer sees "On the way", and our own engine's time is
recorded only in the hidden shadow row (`DeliveryEtaService.record_on_the_way`).
No exception ever escapes this module. If Redis, the cap's counter,
is unreachable the call still goes ahead (a cap outage must not silence a
working route) and the error is logged at WARNING.

Discoverability: Plan 1 found HERE and Yandex keys silently 403/401-ing for
months because nothing surfaced it. A persistent failure here (e.g. a key
that exists but isn't entitled for Routes API — the exact same failure
shape) logs at WARNING, throttled to once per `_LOG_THROTTLE_SECONDS` per
process so it doesn't turn into per-call noise. `GOOGLE_ROUTES_API_KEY`
being unset is a deliberate, expected skip (not a failure) and is silent.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional, Tuple

from flask import current_app

from business_app import redis_client
from business_app.utils.http_client import RetryConfig, request_with_retry
from business_app.utils.local_windows import local_date
from shared.redis_keyspace import RedisKeyspace

logger = logging.getLogger(__name__)

Point = Tuple[float, float]

_GOOGLE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
_FIELD_MASK = "routes.duration,routes.distanceMeters"

# A day's counter outlives its Tashkent day by a day, so a clock a little off still finds the number it expects;
# Redis then drops it. Set once, on the day's first call.
_CALLS_COUNTER_TTL_SECONDS = 2 * 24 * 60 * 60

# How often a Google Routes failure (bad key, non-entitled key, transport
# error, malformed body, ...) may log at WARNING. The condition itself is
# worth one alert per window; repeating it on every single lookup (every
# "on the way" tap) is just noise. Per-process, in-memory — resets on
# worker restart/deploy, which is fine: the first failure after a restart
# re-announces the condition once.
_LOG_THROTTLE_SECONDS = 300.0  # 5 minutes
_log_lock = threading.Lock()
# `None` means "never logged in this process" — deliberately NOT `0.0`; see
# the identical rationale in `distance_matrix._osrm_selfhosted_last_logged_at`.
_last_logged_at: Optional[float] = None


def _log_failure(detail: str) -> None:
    """WARNING at most once per `_LOG_THROTTLE_SECONDS` (per process); every
    other occurrence in the window logs at DEBUG. This is what makes a
    persistent 403 (an un-entitled key that "exists" but never works)
    discoverable in logs without spamming a WARNING on every request."""
    global _last_logged_at
    now = time.monotonic()
    with _log_lock:
        should_warn = _last_logged_at is None or (now - _last_logged_at) >= _LOG_THROTTLE_SECONDS
        if should_warn:
            _last_logged_at = now
    if should_warn:
        logger.warning(
            "google_routes_leg_failed (%s) — no minutes for this tap, the customer sees 'On the way'; "
            "further occurrences suppressed for %.0fs",
            detail,
            _LOG_THROTTLE_SECONDS,
        )
    else:
        logger.debug("google_routes_leg_failed (%s) — no minutes for this tap (suppressed)", detail)


def _under_daily_cap() -> bool:
    """Count THIS call against today's cap; True when it may be sent.

    The day is the Tashkent day (`local_date()`), so the budget turns over at
    local midnight, not at 05:00 local when the UTC date changes. INCR is
    atomic, so workers racing for the last call of the day cannot both get it;
    the expiry is set in the same MULTI, on the day's first call only
    (`EXPIRE ... NX`), so a counter never lives on without one. The count
    includes the calls this check refuses, which is how a refusal shows when
    the key is read.

    Fails OPEN: a Redis error lets the call through. The cap is a bill guard,
    not a gate, and a cap outage must not silence a working route.
    """
    cap = current_app.config["GOOGLE_ROUTES_DAILY_CAP"]
    try:
        key = RedisKeyspace.google_routes_calls(local_date())
        transaction = redis_client.pipeline(transaction=True)
        transaction.incr(key)
        transaction.expire(key, _CALLS_COUNTER_TTL_SECONDS, nx=True)
        calls_today, _ = transaction.execute()
    except Exception as exc:  # noqa: BLE001 — a cap outage must not silence a working route
        logger.warning("google_routes_cap_unavailable (%s) — letting the call through", exc)
        return True
    if calls_today > cap:
        logger.warning("google_routes_daily_cap_reached calls_today=%s cap=%s — no Google request", calls_today, cap)
        return False
    return True


def get_traffic_aware_leg(origin: Point, destination: Point) -> Optional[Dict[str, float]]:
    """TRAFFIC_AWARE duration/distance for ONE leg, or None.

    Returns {"duration_minutes": float, "distance_km": float} on success.
    The call is counted against the Tashkent day's cap BEFORE the request is
    sent (`_under_daily_cap`); once the day's count is over
    `GOOGLE_ROUTES_DAILY_CAP` nothing is sent and the result is None.
    Never raises — any failure (unconfigured key, spent cap, HTTP error,
    transport exception, malformed response) returns None, and the tap gets no
    minutes: the customer sees "On the way", and the matrix time goes only into
    the hidden shadow row.
    """
    api_key = current_app.config.get("GOOGLE_ROUTES_API_KEY")
    if not api_key:
        return None
    body = {
        "origin": {"location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}},
        "destination": {"location": {"latLng": {"latitude": destination[0], "longitude": destination[1]}}},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
    }
    try:
        if not _under_daily_cap():
            return None
        response = request_with_retry(
            method="POST",
            url=_GOOGLE_ROUTES_URL,
            timeout_seconds=5,
            retry_config=RetryConfig(max_retries=1, backoff_base_seconds=0.3),
            circuit_key="google_routes",
            json=body,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": _FIELD_MASK,
            },
        )
        if response.status_code >= 400:
            _log_failure(f"status={response.status_code} body={response.text[:200]}")
            return None
        routes = (response.json() or {}).get("routes") or []
        if not routes:
            _log_failure("empty routes[] in response")
            return None
        route = routes[0]
        duration_raw = route.get("duration")  # e.g. "540s"
        distance_m = route.get("distanceMeters")
        if not isinstance(duration_raw, str) or not duration_raw.endswith("s") or distance_m is None:
            _log_failure(f"malformed route payload duration={duration_raw!r} distanceMeters={distance_m!r}")
            return None
        return {
            "duration_minutes": float(duration_raw[:-1]) / 60.0,
            "distance_km": float(distance_m) / 1000.0,
        }
    except Exception as exc:  # noqa: BLE001 — silence is the contract (spec 8.2)
        _log_failure(f"error={exc}")
        return None
