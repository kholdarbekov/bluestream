"""Google Routes API (computeRoutes) — traffic-aware NEXT-LEG ETA only.

One number per call: the ETA of the leg the driver is about to drive
(spec 8.2). Billing: `computeRoutes` bills per REQUEST; `computeRouteMatrix`
bills per ELEMENT — this module must NEVER call the matrix endpoint. At
~6 drivers x ~15 legs/day ≈ 2,700 requests/month this stays inside Google's
5,000/month free Pro allowance.

Degrades silently: any failure (unconfigured key, 4xx/5xx, timeout,
malformed body) returns None and the caller falls back to the OSRM
duration. No exception ever escapes this module.

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

from business_app.utils.http_client import RetryConfig, request_with_retry

logger = logging.getLogger(__name__)

Point = Tuple[float, float]

_GOOGLE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
_FIELD_MASK = "routes.duration,routes.distanceMeters"

# How often a Google Routes failure (bad key, non-entitled key, transport
# error, malformed body, ...) may log at WARNING. The condition itself is
# worth one alert per window; repeating it on every single next-leg lookup
# (every optimize/active-list call, per driver) is just noise. Per-process,
# in-memory — resets on worker restart/deploy, which is fine: the first
# failure after a restart re-announces the condition once.
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
            "google_routes_leg_failed (%s) — falling back to OSRM/matrix duration; "
            "further occurrences suppressed for %.0fs",
            detail,
            _LOG_THROTTLE_SECONDS,
        )
    else:
        logger.debug("google_routes_leg_failed (%s) — falling back (suppressed)", detail)


def get_traffic_aware_leg(origin: Point, destination: Point) -> Optional[Dict[str, float]]:
    """TRAFFIC_AWARE duration/distance for ONE leg, or None.

    Returns {"duration_minutes": float, "distance_km": float} on success.
    Never raises — any failure (unconfigured key, HTTP error, transport
    exception, malformed response) returns None so the caller falls back to
    the OSRM/matrix duration.
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
