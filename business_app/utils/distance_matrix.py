"""
Distance / duration matrix wrapper for route optimization.

Returns a dict keyed by (i, j) origin/destination index pairs containing
`{"distance_km": float, "duration_minutes": float}`. The diagonal (i == j) is
always 0/0.

Provider fallback chain (best to last-resort):
  1. HERE Matrix Routing v8 — traffic-aware, generous free tier, primary
  2. Yandex Distance Matrix    — traffic-aware, requires paid Yandex tariff
  3. Yandex Routing pairwise   — traffic-aware, same Yandex tariff
  4. OSRM /table               — real road, no traffic, no API key
  5. Haversine                 — straight-line, last resort

Each level is tried only if the previous one is misconfigured or returns
an error. All external calls go through the shared `request_with_retry`
helper (per-host circuit breaker + retries) and successful results are
cached in Redis. Haversine results are deliberately not cached so we keep
retrying real providers.

Cache key is built from the rounded (lat, lng) tuples — coordinates are
rounded to 5 decimal places (~1m precision) so trivial GPS drift still hits
the cache.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Dict, List, Optional, Tuple

import redis
from flask import current_app

from business_app.utils.exceptions import ProviderUnavailableError
from business_app.utils.helpers import calculate_distance
from business_app.utils.http_client import RetryConfig, request_with_retry

logger = logging.getLogger(__name__)

Point = Tuple[float, float]
Matrix = Dict[Tuple[int, int], Dict[str, float]]

# HERE Matrix Routing API v8 — traffic-aware, free tier 250k req/month.
# Per HERE docs: POST JSON, auth via ?apiKey=... query param. The sync mode
# (?async=false) returns the matrix in the response body; async mode is for
# matrices >100x100 which we never approach (typical N is 3-15).
_HERE_MATRIX_URL = "https://matrix.router.hereapi.com/v8/matrix"

_YANDEX_MATRIX_URL = "https://api.routing.yandex.net/v2/distancematrix"
_YANDEX_ROUTE_URL = "https://api.routing.yandex.net/v2/route"
# OSRM public demo endpoint — open-source road-network routing, no API key
# needed. Used as a real-road fallback when neither HERE nor Yandex is
# available, so the optimizer is never reduced to straight-line estimates.
_OSRM_TABLE_URL = "https://router.project-osrm.org/table/v1/driving"

_TRAFFIC_TTL_DEFAULT = 1800  # 30 min
_STATIC_TTL_DEFAULT = 86400  # 24 h
_AVG_CITY_SPEED_KMH = 25.0  # Tashkent baseline for Haversine fallback


def _round_point(p: Point) -> Point:
    return (round(p[0], 5), round(p[1], 5))


def _cache_key(points: List[Point], traffic: bool) -> str:
    rounded = [_round_point(p) for p in points]
    payload = json.dumps({"pts": rounded, "traffic": traffic}, sort_keys=True).encode()
    digest = hashlib.sha256(payload).hexdigest()[:16]
    return f"distance_matrix:v1:{digest}"


def _redis() -> Optional[redis.Redis]:
    try:
        return redis.from_url(current_app.config["REDIS_URL"])
    except (redis.RedisError, KeyError, RuntimeError):
        return None


def _matrix_from_cache(key: str) -> Optional[Matrix]:
    client = _redis()
    if client is None:
        return None
    try:
        raw = client.get(key)
    except redis.RedisError as exc:
        logger.warning("Redis GET failed for %s: %s", key, exc)
        return None
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return {tuple(map(int, k.split(","))): v for k, v in decoded.items()}


def _matrix_to_cache(key: str, matrix: Matrix, ttl: int) -> None:
    client = _redis()
    if client is None:
        return
    try:
        encoded = json.dumps({f"{i},{j}": v for (i, j), v in matrix.items()})
        client.setex(key, ttl, encoded)
    except (redis.RedisError, TypeError, ValueError) as exc:
        logger.warning("Redis SETEX failed for %s: %s", key, exc)


def _here_matrix(points: List[Point], api_key: str, traffic: bool) -> Matrix:
    """Single POST to HERE Matrix Routing API v8 (sync mode).

    Per HERE docs: coordinates are `{"lat": ..., "lng": ...}` objects;
    response carries `matrix.travelTimes` (seconds) and `matrix.distances`
    (metres) as flat row-major arrays of length numOrigins×numDestinations.
    Auth is `?apiKey=...` (camelCase). Traffic is taken into account when
    `departureTime` is set to a real timestamp; we send "any" when caller
    explicitly disables traffic.

    Region: HERE requires a `regionDefinition` for traffic-aware queries.
    We fit a circle to the input points (centroid + max radius + padding)
    so the request stays under their region size limits.
    """
    n = len(points)
    if n == 0:
        return {}

    coord_objs = [{"lat": lat, "lng": lng} for lat, lng in points]

    # Centroid + radius for the region. HERE caps traffic-aware regions at
    # ~400 km diameter; clamp our radius so we never violate that, with a
    # 5 km padding around the bounding sphere of the points.
    centroid_lat = sum(p[0] for p in points) / n
    centroid_lng = sum(p[1] for p in points) / n
    max_km_from_centroid = max(
        (calculate_distance(centroid_lat, centroid_lng, p[0], p[1]) for p in points),
        default=0.0,
    )
    radius_m = int(min(max(max_km_from_centroid + 5.0, 5.0), 200.0) * 1000)

    body = {
        "origins": coord_objs,
        "destinations": coord_objs,
        "regionDefinition": {
            "type": "circle",
            "center": {"lat": centroid_lat, "lng": centroid_lng},
            "radius": radius_m,
        },
        "matrixAttributes": ["travelTimes", "distances"],
        "transportMode": "car",
        "routingMode": "fast",
    }
    if traffic:
        # ISO 8601 with offset; HERE uses this to pick live or predicted
        # traffic. We use UTC.
        from datetime import datetime, timezone

        body["departureTime"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    else:
        # Time-independent calculation, no traffic.
        body["departureTime"] = "any"

    response = request_with_retry(
        method="POST",
        url=_HERE_MATRIX_URL,
        timeout_seconds=15,
        retry_config=RetryConfig(max_retries=2, backoff_base_seconds=0.5),
        circuit_key="here_matrix",
        params={"async": "false", "apiKey": api_key},
        json=body,
        headers={"Content-Type": "application/json"},
    )
    if response.status_code >= 400:
        raise ProviderUnavailableError(
            f"HERE matrix returned {response.status_code}: {response.text[:200]}",
            provider="here_matrix",
        )

    data = response.json()
    matrix_data = data.get("matrix") or {}
    travel_times = matrix_data.get("travelTimes") or []
    distances = matrix_data.get("distances") or []
    error_codes = matrix_data.get("errorCodes") or []

    if not travel_times or not distances or len(travel_times) != n * n or len(distances) != n * n:
        raise ProviderUnavailableError(
            f"HERE matrix response shape unexpected: travelTimes={len(travel_times)} "
            f"distances={len(distances)} expected={n * n}",
            provider="here_matrix",
        )

    # Reshape flat row-major (per HERE docs: k = numDest * i + j) into our
    # dict[(i, j)] form. Per HERE's error-code table, 0 = OK, others mean
    # the cell wasn't computable — backfill with Haversine for those.
    matrix: Matrix = {}
    for i in range(n):
        for j in range(n):
            k = n * i + j
            err = error_codes[k] if k < len(error_codes) else 0
            t = travel_times[k]
            d = distances[k]
            if err != 0 or t < 0 or d < 0:
                km = calculate_distance(points[i][0], points[i][1], points[j][0], points[j][1])
                matrix[(i, j)] = {
                    "distance_km": km,
                    "duration_minutes": (km / _AVG_CITY_SPEED_KMH) * 60.0,
                }
            else:
                matrix[(i, j)] = {
                    "distance_km": d / 1000.0,
                    "duration_minutes": t / 60.0,
                }
    return matrix


def _osrm_table_matrix(points: List[Point]) -> Matrix:
    """Build a real-road matrix via OSRM's /table endpoint.

    OSRM returns durations (seconds) by default and distances (metres) when
    `annotations=distance,duration` is requested. The result is a real-road
    network calculation — *not* straight-line — so it's a valid substitute
    for Yandex when Yandex is unavailable or its key isn't authorised for
    routing. Note: no traffic awareness (free-flow only).

    Coordinate convention: OSRM uses "lon,lat" pairs joined by ";".
    """
    coord_str = ";".join(f"{lng},{lat}" for lat, lng in points)
    response = request_with_retry(
        method="GET",
        url=f"{_OSRM_TABLE_URL}/{coord_str}",
        timeout_seconds=15,
        retry_config=RetryConfig(max_retries=2, backoff_base_seconds=0.5),
        circuit_key="osrm_table",
        params={"annotations": "distance,duration"},
    )
    if response.status_code >= 400:
        raise ProviderUnavailableError(
            f"OSRM table returned {response.status_code}: {response.text[:200]}",
            provider="osrm_table",
        )
    data = response.json()
    if data.get("code") != "Ok":
        raise ProviderUnavailableError(
            f"OSRM table error: {data.get('code')} {data.get('message', '')}",
            provider="osrm_table",
        )
    distances = data.get("distances") or []  # metres, NxN
    durations = data.get("durations") or []  # seconds, NxN
    n = len(points)
    if len(distances) != n or len(durations) != n:
        raise ProviderUnavailableError(
            f"OSRM table size mismatch: expected {n}x{n}",
            provider="osrm_table",
        )
    matrix: Matrix = {}
    for i in range(n):
        for j in range(n):
            d_m = distances[i][j] if j < len(distances[i]) else None
            t_s = durations[i][j] if j < len(durations[i]) else None
            if d_m is None or t_s is None:
                # Unreachable cell — fall back to Haversine for this pair.
                km = calculate_distance(points[i][0], points[i][1], points[j][0], points[j][1])
                matrix[(i, j)] = {
                    "distance_km": km,
                    "duration_minutes": (km / _AVG_CITY_SPEED_KMH) * 60.0,
                }
            else:
                matrix[(i, j)] = {
                    "distance_km": d_m / 1000.0,
                    "duration_minutes": t_s / 60.0,
                }
    return matrix


def _haversine_matrix(points: List[Point]) -> Matrix:
    matrix: Matrix = {}
    n = len(points)
    for i in range(n):
        for j in range(n):
            if i == j:
                matrix[(i, j)] = {"distance_km": 0.0, "duration_minutes": 0.0}
                continue
            km = calculate_distance(points[i][0], points[i][1], points[j][0], points[j][1])
            minutes = (km / _AVG_CITY_SPEED_KMH) * 60.0
            matrix[(i, j)] = {"distance_km": km, "duration_minutes": minutes}
    return matrix


def _yandex_matrix(points: List[Point], api_key: str, traffic: bool) -> Matrix:
    """Single-call Yandex Distance Matrix request. Raises on non-2xx.

    Coordinate convention: Yandex Distance Matrix expects "lat,lon" pairs
    joined by "|" (NOT "lon,lat"). The API key must be authorised for the
    Routing API tariff — a Geocoder-only key returns 401/403 here.
    """
    coord_str = "|".join(f"{lat},{lng}" for lat, lng in points)
    params = {
        "apikey": api_key,
        "origins": coord_str,
        "destinations": coord_str,
        "mode": "driving",
    }
    if traffic:
        # Yandex requires Unix timestamp (uint32 seconds), not the literal
        # string "now" — passing "now" yields 400 "Expected uint32".
        params["departure_time"] = int(time.time())

    response = request_with_retry(
        method="GET",
        url=_YANDEX_MATRIX_URL,
        timeout_seconds=15,
        retry_config=RetryConfig(max_retries=2, backoff_base_seconds=0.5),
        circuit_key="yandex_matrix",
        params=params,
    )
    if response.status_code >= 400:
        raise ProviderUnavailableError(
            f"Yandex matrix returned {response.status_code}: {response.text[:200]}",
            provider="yandex_matrix",
        )

    data = response.json()
    rows = data.get("rows") or data.get("matrix") or []
    if not rows:
        raise ProviderUnavailableError("Yandex matrix response missing rows", provider="yandex_matrix")

    matrix: Matrix = {}
    for i, row in enumerate(rows):
        elements = row.get("elements") or row
        for j, el in enumerate(elements):
            distance_m = (el.get("distance") or {}).get("value")
            duration_s = (el.get("duration") or {}).get("value")
            if traffic and el.get("duration_in_traffic"):
                duration_s = el["duration_in_traffic"].get("value", duration_s)
            if distance_m is None or duration_s is None:
                # Element unreachable — substitute Haversine for this pair.
                km = calculate_distance(points[i][0], points[i][1], points[j][0], points[j][1])
                matrix[(i, j)] = {
                    "distance_km": km,
                    "duration_minutes": (km / _AVG_CITY_SPEED_KMH) * 60.0,
                }
                continue
            matrix[(i, j)] = {
                "distance_km": distance_m / 1000.0,
                "duration_minutes": duration_s / 60.0,
            }
    return matrix


def _yandex_pairwise(points: List[Point], api_key: str, traffic: bool) -> Tuple[Matrix, int, int]:
    """Fallback when the matrix endpoint is unavailable: N² calls to /route.

    Returns `(matrix, success_count, total_pairs)` so the outer wrapper can
    decide whether the result is good enough to call "yandex_pairwise" or
    whether to mark it as `haversine` (when most/all pairs failed). Per-cell
    failures are still backfilled with Haversine so the matrix is well-formed
    regardless.
    """
    matrix: Matrix = {}
    n = len(points)
    success = 0
    total_pairs = 0
    for i in range(n):
        for j in range(n):
            if i == j:
                matrix[(i, j)] = {"distance_km": 0.0, "duration_minutes": 0.0}
                continue
            total_pairs += 1
            params = {
                "apikey": api_key,
                # Yandex routing waypoints are "lat,lon" joined by "|".
                "waypoints": f"{points[i][0]},{points[i][1]}|{points[j][0]},{points[j][1]}",
                "mode": "driving",
            }
            if traffic:
                params["departure_time"] = int(time.time())
            try:
                response = request_with_retry(
                    method="GET",
                    url=_YANDEX_ROUTE_URL,
                    timeout_seconds=10,
                    retry_config=RetryConfig(max_retries=1, backoff_base_seconds=0.3),
                    circuit_key="yandex_route",
                    params=params,
                )
                if response.status_code >= 400:
                    raise ValueError(f"status={response.status_code} body={response.text[:120]}")
                data = response.json()
                route = data.get("route") or {}
                distance_m = (route.get("distance") or {}).get("value", 0)
                duration_s = (route.get("duration") or {}).get("value", 0)
                if not distance_m or not duration_s:
                    raise ValueError("route missing distance/duration")
                matrix[(i, j)] = {
                    "distance_km": distance_m / 1000.0,
                    "duration_minutes": duration_s / 60.0,
                }
                success += 1
            except Exception as exc:
                logger.warning("Yandex pairwise route failed for (%d,%d): %s", i, j, exc)
                km = calculate_distance(points[i][0], points[i][1], points[j][0], points[j][1])
                matrix[(i, j)] = {
                    "distance_km": km,
                    "duration_minutes": (km / _AVG_CITY_SPEED_KMH) * 60.0,
                }
    return matrix, success, total_pairs


def get_distance_matrix(
    points: List[Point],
    *,
    traffic: bool = True,
    provider: Optional[str] = None,
    use_cache: bool = True,
) -> Tuple[Matrix, str]:
    """Return distance/duration matrix for `points` and the source label.

    Args:
        points: list of (lat, lng) tuples; the matrix is square N×N.
        traffic: when True, request traffic-aware durations (shorter cache TTL).
        provider: override `MAPS_PROVIDER` config (mainly for tests).
        use_cache: skip Redis lookup when False (forces a fresh call).

    Returns:
        (matrix, source) where source ∈ {"yandex_matrix", "yandex_pairwise",
        "haversine", "cache"}.
    """
    if not points:
        return {}, "empty"
    if len(points) == 1:
        return {(0, 0): {"distance_km": 0.0, "duration_minutes": 0.0}}, "trivial"

    cache_key = _cache_key(points, traffic)
    if use_cache:
        cached = _matrix_from_cache(cache_key)
        if cached is not None:
            return cached, "cache"

    provider = (provider or current_app.config.get("MAPS_PROVIDER", "google")).lower()
    here_key = current_app.config.get("HERE_MAPS_API_KEY")
    yandex_key = current_app.config.get("YANDEX_MAPS_API_KEY")

    matrix: Optional[Matrix] = None
    source = "haversine"

    # Fallback chain (best to last-resort):
    #   1. HERE Matrix v8       — traffic-aware, generous free tier
    #   2. Yandex Distance Matrix — traffic-aware, paid tariff required
    #   3. Yandex Routing pairwise — traffic-aware, same paid tariff
    #   4. OSRM /table           — real road, no traffic, no API key
    #   5. Haversine             — straight-line, last resort
    # Each tier is tried only when the previous one is misconfigured or
    # returned an unrecoverable error. Haversine in production indicates
    # the operator should fix one of the upstream providers.
    if here_key:
        try:
            matrix = _here_matrix(points, here_key, traffic)
            source = "here_matrix"
        except ProviderUnavailableError as exc:
            logger.warning("HERE matrix unavailable, trying Yandex: %s", exc)
            matrix = None
        except Exception as exc:  # noqa: BLE001
            # Defensive catch — network errors that bypass the retry layer,
            # parse errors on malformed responses, etc. Always fall through
            # to the next provider rather than crashing the optimizer.
            logger.warning("HERE matrix unexpected error, trying Yandex: %s", exc)
            matrix = None

    if matrix is None and provider == "yandex" and yandex_key:
        try:
            matrix = _yandex_matrix(points, yandex_key, traffic)
            source = "yandex_matrix"
        except ProviderUnavailableError as exc:
            logger.warning("Yandex matrix unavailable, trying pairwise: %s", exc)
            try:
                pairwise_matrix, success, total = _yandex_pairwise(points, yandex_key, traffic)
                # Only call this "yandex_pairwise" if a clear majority of pairs
                # actually returned real road data. Otherwise the per-cell
                # Haversine backfill dominates and we should be honest about it.
                if total > 0 and success / total >= 0.5:
                    matrix = pairwise_matrix
                    source = "yandex_pairwise"
                else:
                    logger.warning(
                        "Yandex pairwise mostly failed (%d/%d pairs ok) — trying OSRM",
                        success,
                        total,
                    )
                    matrix = None
            except ProviderUnavailableError as exc2:
                logger.warning("Yandex pairwise circuit open: %s", exc2)
                matrix = None

    # Try OSRM if Yandex didn't produce a result. OSRM is open-source and
    # works without an API key. Distance comes back accurate (real road
    # network) but durations are FREE-FLOW (no traffic model). We deliberately
    # do NOT apply any synthetic traffic adjustment — that would dress
    # heuristic guessing as data. OSRM ETAs will under-estimate real travel
    # during rush hour; the only honest fix is paid Yandex routing access.
    if matrix is None:
        try:
            matrix = _osrm_table_matrix(points)
            source = "osrm_table"
        except ProviderUnavailableError as exc:
            logger.warning("OSRM table unavailable, falling back to Haversine: %s", exc)
            matrix = None
        except Exception as exc:  # noqa: BLE001
            logger.warning("OSRM table unexpected error, falling back to Haversine: %s", exc)
            matrix = None

    # Last resort. Haversine straight-line distance is wrong by 25-40% in
    # dense urban areas — an explicit warning so this never goes unnoticed
    # in production.
    if matrix is None:
        logger.warning(
            "distance_matrix using HAVERSINE fallback (real-road providers all unavailable) "
            "n=%d — sequencing will be approximate; ETA/km in UI should be suppressed.",
            len(points),
        )
        matrix = _haversine_matrix(points)
        source = "haversine"

    ttl = (
        current_app.config.get("MATRIX_CACHE_TTL_TRAFFIC_SECONDS", _TRAFFIC_TTL_DEFAULT)
        if traffic
        else current_app.config.get("MATRIX_CACHE_TTL_STATIC_SECONDS", _STATIC_TTL_DEFAULT)
    )
    if source != "haversine":
        # Don't cache the Haversine fallback — we want to retry real providers
        # next time. Yandex and OSRM results are cached normally.
        _matrix_to_cache(cache_key, matrix, ttl)

    logger.info(
        "distance_matrix_built source=%s n=%d traffic=%s cache_key=%s",
        source,
        len(points),
        traffic,
        cache_key,
    )
    return matrix, source
