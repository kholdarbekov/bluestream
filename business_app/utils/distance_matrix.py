"""
Distance / duration matrix wrapper for route optimization.

Returns a dict keyed by (i, j) origin/destination index pairs containing
`{"distance_km": float, "duration_minutes": float}`. The diagonal (i == j) is
always 0/0.

Provider fallback chain (best to last-resort):
  1. Self-hosted OSRM (OSRM_BASE_URL)         — real road, free-flow, primary
  2. HERE Matrix v8 / Yandex Distance Matrix  — LEGACY_MATRIX_PROVIDERS_ENABLED
     only; both keys are un-entitled in production (403/401) so this tier is
     off the hot path by default. Code kept for the day entitlements exist.
  3. OSRM public demo server                  — OSRM_PUBLIC_FALLBACK_ENABLED
     only (emergency; demo usage policy forbids production use)
  4. Haversine                                — straight-line, last resort

Each level is tried only if the previous one is misconfigured or returns
an error. All external calls go through the shared `request_with_retry`
helper (per-host circuit breaker + retries).

Successful results are cached in Redis as TWO tiers (spec 8.3): a static
stop↔stop sub-matrix keyed on the SORTED stop coordinates (24 h TTL when
free-flow — stable all day regardless of driver movement or stop ordering;
the shorter MATRIX_CACHE_TTL_TRAFFIC_SECONDS when the payload actually came
from a traffic-aware provider, see `_TRAFFIC_AWARE_SOURCES`) and a small
live origin row/column keyed on the driver's rounded position (short TTL).
Haversine results are deliberately not cached so we keep retrying real
providers. Coordinates are rounded to 5 decimal places (~1 m) inside keys.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

import redis
from flask import current_app

from business_app.utils.exceptions import ProviderUnavailableError
from business_app.utils.helpers import calculate_distance
from business_app.utils.http_client import RetryConfig, get_circuit_breaker, request_with_retry

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
# OSRM public demo endpoint. EMERGENCY fallback only, behind
# OSRM_PUBLIC_FALLBACK_ENABLED — its usage policy forbids production use.
# The primary tier is the self-hosted instance at OSRM_BASE_URL.
_OSRM_DEMO_BASE_URL = "https://router.project-osrm.org"

# Self-hosted OSRM is a same-network / LAN service, not an internet API. Its
# retry policy is tuned tighter than the other tiers (15s timeout / 2
# retries, sized for HERE/Yandex/the public demo) but NOT on the timeout —
# on a dev box where OSRM_BASE_URL points nowhere (default
# "http://osrm:5000"; the `osrm` compose service sits behind `--profile
# routing` and is simply not running — see .env.example), Docker's embedded
# DNS returns NXDOMAIN and `requests` raises ConnectionError in single-digit
# milliseconds *regardless of the timeout value* — cutting 15s to 5s buys
# nothing there. The actual dev-box speedup is `max_retries: 2 → 1`, which
# turns ~1.5s of retry backoff into ~0.3s. The timeout stays close to the
# original 15s (5s, not lower) because production is the opposite case: on
# a Pi 5 with `--mmap` and no page-cache preload, a 1.0-CPU/2GB-capped
# container's first `/table` call after a deploy or a page-cache eviction
# faults MLD tiles off disk — that cold request is exactly the post-deploy
# one, and a spurious fall-through to Haversine there is worse than a
# slower call.
_OSRM_SELFHOSTED_TIMEOUT_SECONDS = 5.0
_OSRM_SELFHOSTED_RETRY_CONFIG = RetryConfig(max_retries=1, backoff_base_seconds=0.3, backoff_max_seconds=1.0)
# How often the "self-hosted OSRM unavailable" condition may log at WARNING.
# The condition itself is a real operational fact worth one alert; repeating
# it on every single optimization call (which is what a dev box with the
# `osrm` profile never started would otherwise do, every time a route is
# viewed) is just noise. Per-process, in-memory — resets on worker
# restart/deploy, which is fine: the first call after a restart re-announces
# the condition once.
_OSRM_SELFHOSTED_LOG_THROTTLE_SECONDS = 300.0  # 5 minutes
_osrm_selfhosted_log_lock = threading.Lock()
# `None` means "never logged in this process" — deliberately NOT `0.0`.
# `time.monotonic()` is host/process uptime, not wall clock: right after a
# fresh boot (or a fresh CI VM) it can itself be well under the throttle
# window, so a `0.0` sentinel would make "never logged" indistinguishable
# from "logged at monotonic time ~0" and silently demote the very first
# WARNING after a reboot to DEBUG — precisely the "OSRM didn't come back up"
# moment that most needs to fire.
_osrm_selfhosted_last_logged_at: Optional[float] = None


def _log_osrm_selfhosted_unavailable(detail: str) -> None:
    """WARNING at most once per `_OSRM_SELFHOSTED_LOG_THROTTLE_SECONDS`
    (per process); every other occurrence in the window logs at DEBUG."""
    global _osrm_selfhosted_last_logged_at
    now = time.monotonic()
    with _osrm_selfhosted_log_lock:
        should_warn = (
            _osrm_selfhosted_last_logged_at is None
            or (now - _osrm_selfhosted_last_logged_at) >= _OSRM_SELFHOSTED_LOG_THROTTLE_SECONDS
        )
        if should_warn:
            _osrm_selfhosted_last_logged_at = now
    if should_warn:
        logger.warning(
            "Self-hosted OSRM unavailable (%s) — falling back to the next matrix "
            "tier; further occurrences suppressed for %.0fs",
            detail,
            _OSRM_SELFHOSTED_LOG_THROTTLE_SECONDS,
        )
    else:
        logger.debug("Self-hosted OSRM unavailable (%s) — falling back (suppressed)", detail)


_STATIC_TTL_DEFAULT = 86400  # 24 h — the static stop↔stop tier when free-flow (spec 8.3)
_TRAFFIC_TTL_DEFAULT = 1800  # 30 min — static tier TTL when the payload IS traffic-aware
_LIVE_ORIGIN_TTL_DEFAULT = 120  # live origin row: repeat calls within one optimize cycle
_AVG_CITY_SPEED_KMH = 25.0  # Tashkent baseline for Haversine fallback

# Sources whose durations actually vary with live/predicted traffic. Self-hosted
# and public-demo OSRM are free-flow ALWAYS (no traffic model at all, regardless
# of the `traffic` flag a caller passes) — only these legacy providers, and only
# when LEGACY_MATRIX_PROVIDERS_ENABLED, produce data that can go stale within
# hours rather than a day (task-4 review fix 4).
_TRAFFIC_AWARE_SOURCES = frozenset({"here_matrix", "yandex_matrix", "yandex_pairwise"})

_ZERO_CELL = {"distance_km": 0.0, "duration_minutes": 0.0}

# Shared "is this result honestly real-road data" guard (final review round,
# I2): a matrix where fewer than this fraction of cells came from the
# provider — the rest silently Haversine-backfilled per-cell — must not be
# cached under the provider's own label or trusted as real-road data. Applied
# identically to `_yandex_pairwise` (per-pair fallback) and `_osrm_matrix`
# (per-cell fallback within one /table response) so both dishonesty seams
# close the same way.
_MIN_REAL_CELL_RATIO = 0.5


def _pt_key(p: Point) -> str:
    """Stable string identity for a coordinate at cache precision (~1 m)."""
    return f"{round(p[0], 5):.5f},{round(p[1], 5):.5f}"


def _static_key(stops: List[Point], traffic: bool) -> str:
    """Key of the stop↔stop sub-matrix. SORTED stop identities: immune to both
    origin movement and stop ordering — the fix for the guaranteed miss the
    old whole-matrix key produced with the moving driver as point 0."""
    payload = json.dumps({"stops": sorted(_pt_key(p) for p in stops), "traffic": traffic}).encode()
    return f"distance_matrix:static:v2:{hashlib.sha256(payload).hexdigest()[:16]}"


def _live_key(origin: Point, stops: List[Point], traffic: bool) -> str:
    payload = json.dumps(
        {
            "origin": _pt_key(origin),
            "stops": sorted(_pt_key(p) for p in stops),
            "traffic": traffic,
        }
    ).encode()
    return f"distance_matrix:live:v2:{hashlib.sha256(payload).hexdigest()[:16]}"


def _redis() -> Optional[redis.Redis]:
    try:
        return redis.from_url(current_app.config["REDIS_URL"])
    except (redis.RedisError, KeyError, RuntimeError):
        return None


def _cache_get_json(key: str) -> Optional[dict]:
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
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _cache_set_json(key: str, payload: dict, ttl: int) -> None:
    client = _redis()
    if client is None:
        return
    try:
        client.setex(key, ttl, json.dumps(payload))
    except (redis.RedisError, TypeError, ValueError) as exc:
        logger.warning("Redis SETEX failed for %s: %s", key, exc)


def _cell_or_haversine(d_m, t_s, a: Point, b: Point) -> Dict[str, float]:
    """Provider cell → our cell shape, Haversine-backfilling null cells."""
    if d_m is None or t_s is None:
        km = calculate_distance(a[0], a[1], b[0], b[1])
        return {"distance_km": km, "duration_minutes": (km / _AVG_CITY_SPEED_KMH) * 60.0}
    return {"distance_km": d_m / 1000.0, "duration_minutes": t_s / 60.0}


def _store_split_cache(points: List[Point], traffic: bool, matrix: Matrix, source: str) -> None:
    """Persist a freshly fetched full matrix as the two tiers (spec 8.3).

    The static tier's 24h TTL assumes free-flow data — true for self-hosted
    OSRM (the primary tier, source="osrm_selfhosted"/"osrm_table") which has
    no traffic model regardless of the `traffic` flag a caller passes. It is
    NOT true for `source` in `_TRAFFIC_AWARE_SOURCES` (HERE/Yandex, only
    reachable when `LEGACY_MATRIX_PROVIDERS_ENABLED`): a stop↔stop cell from
    those providers can go stale within hours, not a day — a route solved at
    20:00 must not still be priced on 08:00 rush-hour durations 20 hours
    later with no signal that it happened. Honour
    `MATRIX_CACHE_TTL_TRAFFIC_SECONDS` for the static tier in that case
    (task-4 review fix 4); the live-origin tier's own short TTL already
    protects it regardless of source.
    """
    stops = points[1:]
    skeys = [_pt_key(p) for p in stops]
    static_cells: Dict[str, Dict[str, float]] = {}
    for a, sa in enumerate(skeys):
        for b, sb in enumerate(skeys):
            if a == b or sa == sb:
                continue
            static_cells[f"{sa}|{sb}"] = matrix[(a + 1, b + 1)]
    row = {sa: matrix[(0, a + 1)] for a, sa in enumerate(skeys)}
    # This full-fetch path stores the REAL stop->origin column (unlike
    # `_fetch_origin_row_col`'s partial-fetch path, which mirrors `col` from
    # `row`). See that function's docstring for the invariant that makes the
    # mirror safe on the other path — no consumer costs `matrix[(i, 0)]`, so
    # a genuine value here versus a mirrored one there are interchangeable
    # today.
    col = {sa: matrix[(a + 1, 0)] for a, sa in enumerate(skeys)}
    is_traffic_aware = traffic and source in _TRAFFIC_AWARE_SOURCES
    static_ttl = int(
        current_app.config.get("MATRIX_CACHE_TTL_TRAFFIC_SECONDS", _TRAFFIC_TTL_DEFAULT)
        if is_traffic_aware
        else current_app.config.get("MATRIX_CACHE_TTL_STATIC_SECONDS", _STATIC_TTL_DEFAULT)
    )
    live_ttl = int(current_app.config.get("MATRIX_LIVE_ORIGIN_TTL_SECONDS", _LIVE_ORIGIN_TTL_DEFAULT))
    # `"source"` alongside `cells` lets a later full cache HIT recover which
    # provider actually produced this data — see `get_cached_matrix_source`
    # (final review round, I3). `get_distance_matrix` itself keeps returning
    # the "cache" tier label on a hit (several tests pin that literal); this
    # is a separate, optional lookup a caller can make when it needs real
    # provenance instead of the tier label.
    _cache_set_json(_static_key(stops, traffic), {"cells": static_cells, "source": source}, static_ttl)
    _cache_set_json(_live_key(points[0], stops, traffic), {"row": row, "col": col}, live_ttl)


def _assemble_from_tiers(points: List[Point], static_payload: dict, live_payload: dict) -> Optional[Matrix]:
    """Full N×N matrix from the two tiers, or None when any cell is missing."""
    skeys = [_pt_key(p) for p in points[1:]]
    matrix: Matrix = {(i, i): dict(_ZERO_CELL) for i in range(len(points))}
    row = live_payload.get("row") or {}
    col = live_payload.get("col") or {}
    cells = static_payload.get("cells") or {}
    for a, sa in enumerate(skeys):
        cell_row = row.get(sa)
        cell_col = col.get(sa)
        if cell_row is None or cell_col is None:
            return None
        matrix[(0, a + 1)] = cell_row
        matrix[(a + 1, 0)] = cell_col
        for b, sb in enumerate(skeys):
            if a == b:
                continue
            if sa == sb:
                # Two deliveries at the same (rounded) coordinates.
                matrix[(a + 1, b + 1)] = dict(_ZERO_CELL)
                continue
            cell = cells.get(f"{sa}|{sb}")
            if cell is None:
                return None
            matrix[(a + 1, b + 1)] = cell
    return matrix


def _load_split_cache(points: List[Point], traffic: bool) -> Optional[Matrix]:
    static_payload = _cache_get_json(_static_key(points[1:], traffic))
    if static_payload is None:
        return None
    live_payload = _cache_get_json(_live_key(points[0], points[1:], traffic))
    if live_payload is None:
        return None
    return _assemble_from_tiers(points, static_payload, live_payload)


def get_cached_matrix_source(points: List[Point], traffic: bool) -> Optional[str]:
    """Recover the provider that actually produced a full cache HIT (final
    review round, I3).

    `get_distance_matrix` returns the tier label `"cache"` on a full hit —
    useful for its own `static_tier=hit live_tier=hit` diagnostics, but it
    discards which provider's fetch originally populated the tier, and a
    caller that needs real provenance (e.g. `eta_source`, a field Plan 3
    will consume) must not publish `"cache"` as if it were a provider name.
    `_store_split_cache` stashes the originating `source` under the static
    tier's `"source"` key; this reads it back WITHOUT changing
    `get_distance_matrix`'s own return contract (several tests pin
    `source == "cache"` literally, and changing that would ripple beyond
    this fix's scope).

    Returns None when the static tier is a miss, or when it was written
    before this fix (no `"source"` key) — callers MUST treat None as
    "not recoverable" and say so explicitly, never guess a provider.
    """
    static_payload = _cache_get_json(_static_key(points[1:], traffic))
    if not static_payload:
        return None
    return static_payload.get("source")


def _fetch_origin_row_col(points: List[Point], base_url: str) -> Optional[dict]:
    """Fetch ONLY the origin→stop row from self-hosted OSRM, as a live-tier
    payload {"row": {pt_key: cell}, "col": {...}}.

    One `/table` call with `sources=0` (index into the coordinate list) — the
    cheap complement to a static-tier hit. There is deliberately no second
    `destinations=0` call for the reverse stop→origin column: `col` mirrors
    `row` instead of paying for a second HTTP round-trip that would otherwise
    double the request count on the system's two hottest matrix calls (the
    driver→committed-stop leg and the bot's next-leg ETA, both N=2 with an
    always-moving origin — task-4 review fix 1/2).

    INVARIANT that makes the mirror safe (final review round, I1; corrected
    again in the residuals round — the first correction misattributed the
    reason): every matrix consumer walks an OPEN path that starts at index 0
    and never returns to it, so a cell at `matrix[(i, 0)]` is never COSTED,
    even though `_solve_with_pins` DOES read it (it copies
    `matrix[(free_node, 0)]` into its `local_matrix` sub-problem while
    building the free-node TSP). `_solve_tsp_exact`'s Held-Karp DP and
    `_solve_tsp_heuristic`'s NN+2-opt are open-ended by construction — no
    return edge exists to look up in the first place. `_two_opt_frozen` is
    NOT protected by treating index 0 as a "frozen" position (`frozen_positions`
    never contains 0 — it only ever holds positions >= 1, see
    `_solve_with_pins`'s `frozen = {i + 1 for ...}`); it is protected because
    its reversal loop is `range(1, len(best) - 1)`, so index 0 can never sit
    inside a reversed slice and `path[0]` stays `start_idx` forever — no
    `(v, 0)` edge is ever produced. `col` therefore only needs to satisfy
    shape (same keys as `row`), never value.
    `tests/unit/test_route_optimization_pins.py::TestReturnColumnNeverCosted`
    pins this with a SINGLE asymmetric poisoned cell (a uniform poison across
    every `(i, 0)` would cancel out under a hypothetical closed-tour
    objective too, making the tripwire vacuous); if a future solver change
    ever costs a return-to-origin edge (e.g. a closed-tour objective or a
    `weight=` on a symmetric metric), that test
    trips FIRST — fetch the real column at that point, don't just fix the
    test.

    Same host, same circuit as the self-hosted primary tier, so this reuses
    its SSOT timeout/retry policy (`_OSRM_SELFHOSTED_TIMEOUT_SECONDS` /
    `_OSRM_SELFHOSTED_RETRY_CONFIG`) rather than the internet-tier defaults
    — a same-network service must not wait 15s (task-4 review fix 3).

    Returns None on any failure — including a slow/dead OSRM, an HTTP error,
    an unexpected response shape, or a non-"Ok" `code` — so the caller falls
    back to a full fetch. Failures are logged through the throttled
    `_log_osrm_selfhosted_unavailable` (same helper the primary tier uses)
    instead of an unthrottled `logger.warning`, so a hung/erroring OSRM
    can't flood the log once per call (task-4 review fix 6).
    """
    coord_str = ";".join(f"{lng},{lat}" for lat, lng in points)
    skeys = [_pt_key(p) for p in points[1:]]
    n = len(points)
    try:
        response = request_with_retry(
            method="GET",
            url=f"{base_url}/table/v1/driving/{coord_str}",
            timeout_seconds=_OSRM_SELFHOSTED_TIMEOUT_SECONDS,
            retry_config=_OSRM_SELFHOSTED_RETRY_CONFIG,
            circuit_key="osrm_selfhosted",
            params={"annotations": "distance,duration", "sources": "0"},
        )
        if response.status_code >= 400:
            _log_osrm_selfhosted_unavailable(f"origin-row fetch status={response.status_code}")
            return None
        data = response.json()
        if data.get("code") != "Ok":
            _log_osrm_selfhosted_unavailable(f"origin-row fetch code={data.get('code')}")
            return None
        distances = data.get("distances") or []
        durations = data.get("durations") or []
        if len(distances) != 1 or len(durations) != 1:
            _log_osrm_selfhosted_unavailable("origin-row fetch response shape mismatch")
            return None
        row: Dict[str, Dict[str, float]] = {}
        for j in range(1, n):
            row[skeys[j - 1]] = _cell_or_haversine(distances[0][j], durations[0][j], points[0], points[j])
        # `col` mirrors `row` — see docstring: it IS read (into
        # `_solve_with_pins`'s local sub-matrix) but never costed, because
        # the solver only ever walks an open path pinned at index 0. Never a
        # second HTTP call.
        return {"row": row, "col": dict(row)}
    except Exception as exc:  # noqa: BLE001
        _log_osrm_selfhosted_unavailable(f"origin-row fetch error: {exc}")
        return None


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


def _osrm_matrix(
    points: List[Point],
    *,
    base_url: str,
    circuit_key: str,
    provider_label: str,
    timeout_seconds: float = 15.0,
    retry_config: Optional[RetryConfig] = None,
) -> Tuple[Matrix, int, int]:
    """Build a real-road matrix via an OSRM /table endpoint.

    Serves both the self-hosted instance (base_url=OSRM_BASE_URL,
    circuit_key/provider_label "osrm_selfhosted") and the public demo
    fallback ("osrm_table"). OSRM returns durations (seconds) and, with
    `annotations=distance,duration`, distances (metres). Free-flow only —
    no traffic model. Coordinate convention: "lon,lat" joined by ";".

    `timeout_seconds`/`retry_config` default to the values historically used
    for the (internet-hosted) demo fallback (15s / 2 retries). The
    self-hosted primary caller in `get_distance_matrix` passes
    `_OSRM_SELFHOSTED_TIMEOUT_SECONDS` / `_OSRM_SELFHOSTED_RETRY_CONFIG`
    instead — see those constants for why a same-network service needs a
    much tighter policy.

    Returns `(matrix, real_cells, total_cells)`, mirroring
    `_yandex_pairwise`'s `(matrix, success, total_pairs)` shape (final review
    round, I2): a single OSRM response can carry `null` cells for individual
    unreachable pairs (a disconnected component in the extract, a stop
    snapped onto a private road), each backfilled with Haversine via
    `_cell_or_haversine`. Unlike `_yandex_pairwise`, that backfill previously
    had no ceiling — the caller now applies the same
    `_MIN_REAL_CELL_RATIO` majority guard before trusting/caching this as
    real-road data.
    """
    coord_str = ";".join(f"{lng},{lat}" for lat, lng in points)
    response = request_with_retry(
        method="GET",
        url=f"{base_url}/table/v1/driving/{coord_str}",
        timeout_seconds=timeout_seconds,
        retry_config=retry_config or RetryConfig(max_retries=2, backoff_base_seconds=0.5),
        circuit_key=circuit_key,
        params={"annotations": "distance,duration"},
    )
    if response.status_code >= 400:
        raise ProviderUnavailableError(
            f"OSRM table returned {response.status_code}: {response.text[:200]}",
            provider=provider_label,
        )
    data = response.json()
    if data.get("code") != "Ok":
        raise ProviderUnavailableError(
            f"OSRM table error: {data.get('code')} {data.get('message', '')}",
            provider=provider_label,
        )
    distances = data.get("distances") or []  # metres, NxN
    durations = data.get("durations") or []  # seconds, NxN
    n = len(points)
    if len(distances) != n or len(durations) != n:
        raise ProviderUnavailableError(
            f"OSRM table size mismatch: expected {n}x{n}",
            provider=provider_label,
        )
    matrix: Matrix = {}
    real_cells = 0
    total_cells = 0
    for i in range(n):
        for j in range(n):
            if i == j:
                matrix[(i, j)] = dict(_ZERO_CELL)
                continue
            total_cells += 1
            d_m = distances[i][j] if j < len(distances[i]) else None
            t_s = durations[i][j] if j < len(durations[i]) else None
            if d_m is not None and t_s is not None:
                real_cells += 1
            # Unreachable cell falls back to Haversine for this pair.
            matrix[(i, j)] = _cell_or_haversine(d_m, t_s, points[i], points[j])
    return matrix, real_cells, total_cells


def _yandex_step_metric(raw: object) -> Optional[float]:
    """Extract a numeric value from a Yandex Router API step field.

    Per Yandex's documented Router API response shape (route.legs[].steps[]),
    `length`/`duration` are plain numbers, not `{"value": ...}` wrapper
    objects (unlike Google's Directions API). Handled defensively so a
    response variant that DOES wrap the number doesn't silently zero out —
    accept either shape rather than assuming one.
    """
    if isinstance(raw, dict):
        raw = raw.get("value")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


def yandex_route_totals(route: Dict) -> Tuple[float, float]:
    """Sum per-step distance (metres) and duration (seconds) from a Yandex
    Router API `route` object, across every leg and every step.

    Per Yandex's Router API docs (confirmed during this fix), there is no
    route-level or leg-level distance/duration aggregate — `route.distance`
    and `route.duration` do not exist. Only `route.legs[].steps[]` entries
    carry the real per-step `length` (metres) / `duration` (seconds).
    Mirrors the `route.legs[].steps[]` traversal `_yandex_route_geometry`
    (in `business_app.services.maps_service`) already uses for
    `steps[].polyline.points`.

    Returns (0.0, 0.0) when the route carries no legs/steps or no usable
    numeric fields — callers must treat that as "no data", never invent a
    non-zero number for it.
    """
    total_distance_m = 0.0
    total_duration_s = 0.0
    for leg in route.get("legs") or []:
        for step in leg.get("steps") or []:
            distance_val = _yandex_step_metric(step.get("length"))
            duration_val = _yandex_step_metric(step.get("duration"))
            if distance_val is not None:
                total_distance_m += distance_val
            if duration_val is not None:
                total_duration_s += duration_val
    return total_distance_m, total_duration_s


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
                # `route.distance`/`route.duration` do NOT exist in Yandex's
                # documented Router API response — only the per-step
                # `length`/`duration` inside `route.legs[].steps[]` do. Sum
                # them instead of reading a top-level key that was always
                # absent (see `yandex_route_totals` docstring). The guard
                # below is unchanged: a summed 0 (missing legs/steps/metrics)
                # still raises so the per-cell Haversine fallback runs and a
                # bad Yandex response can't poison the optimiser.
                distance_m, duration_s = yandex_route_totals(route)
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
        traffic: when True, request traffic-aware durations. Also part of both
            cache-tier keys (spec 8.3), so traffic and non-traffic results
            never collide.
        provider: override `MAPS_PROVIDER` config (mainly for tests).
        use_cache: skip Redis lookup when False (forces a fresh call).

    Returns:
        (matrix, source) where source ∈ {"cache", "osrm_selfhosted",
        "here_matrix", "yandex_matrix", "yandex_pairwise", "osrm_table",
        "haversine", "empty", "trivial"}.
    """
    if not points:
        return {}, "empty"
    if len(points) == 1:
        return {(0, 0): {"distance_km": 0.0, "duration_minutes": 0.0}}, "trivial"

    osrm_base_url = (current_app.config.get("OSRM_BASE_URL") or "").rstrip("/")
    # Whether the static-tier Redis GET actually found an entry, independent
    # of whether we went on to exploit it — the fall-through log at the
    # bottom of this function reads this so an OSRM-down incident (static
    # hit, live fetch failed) doesn't get misreported as a static MISS and
    # under-report the static tier's real hit rate (task-4 review fix 5).
    static_tier_hit = False

    if use_cache:
        cached = _load_split_cache(points, traffic)
        if cached is not None:
            logger.info(
                "distance_matrix_built source=cache n=%d traffic=%s static_tier=hit live_tier=hit",
                len(points),
                traffic,
            )
            return cached, "cache"

        # A static-tier hit alone still saves the expensive part: fetch only
        # the origin row/column and splice it onto the cached stop↔stop
        # sub-matrix. This is the movement-proof path (spec 8.3 + §4.2).
        if osrm_base_url:
            static_payload = _cache_get_json(_static_key(points[1:], traffic))
            static_tier_hit = static_payload is not None
            # Guard against a DEGENERATE static tier: with a single stop
            # (N=2, e.g. the driver→committed-stop leg or the bot's next-leg
            # ETA — both moving-origin, both called on nearly every solve/
            # render), `_store_split_cache` never has a cross-stop pair to
            # store, so `cells` is always `{}`. Taking the partial path there
            # buys zero reuse and would still cost its own request on top of
            # whatever the full chain needs — fall straight through to a
            # single full fetch instead (task-4 review fix 2).
            if static_tier_hit and len(points) > 2 and static_payload.get("cells"):
                live_payload = _fetch_origin_row_col(points, osrm_base_url)
                if live_payload is not None:
                    assembled = _assemble_from_tiers(points, static_payload, live_payload)
                    if assembled is not None:
                        live_ttl = int(
                            current_app.config.get("MATRIX_LIVE_ORIGIN_TTL_SECONDS", _LIVE_ORIGIN_TTL_DEFAULT)
                        )
                        _cache_set_json(_live_key(points[0], points[1:], traffic), live_payload, live_ttl)
                        logger.info(
                            "distance_matrix_built source=osrm_selfhosted n=%d traffic=%s "
                            "static_tier=hit live_tier=fetched",
                            len(points),
                            traffic,
                        )
                        return assembled, "osrm_selfhosted"

    provider = (provider or current_app.config.get("MAPS_PROVIDER", "google")).lower()
    here_key = current_app.config.get("HERE_MAPS_API_KEY")
    yandex_key = current_app.config.get("YANDEX_MAPS_API_KEY")
    legacy_enabled = bool(current_app.config.get("LEGACY_MATRIX_PROVIDERS_ENABLED", False))
    demo_enabled = bool(current_app.config.get("OSRM_PUBLIC_FALLBACK_ENABLED", False))

    matrix: Optional[Matrix] = None
    source = "haversine"
    # Populated when an OSRM tier wins, so the fall-through log below can
    # report how many cells were real vs per-cell Haversine backfill even on
    # a result that cleared the `_MIN_REAL_CELL_RATIO` guard (final review
    # round, I2).
    real_cell_counts: Optional[Tuple[int, int]] = None

    # Chain (spec 8.1/8.2), best to last-resort:
    #   1. Self-hosted OSRM        — real road, free-flow, always on when configured
    #   2. HERE / Yandex           — LEGACY_MATRIX_PROVIDERS_ENABLED only (403/401
    #                                 in production today; kept, not deleted)
    #   3. OSRM public demo        — OSRM_PUBLIC_FALLBACK_ENABLED only (emergency)
    #   4. Haversine               — straight-line, never cached
    if osrm_base_url:
        breaker = get_circuit_breaker("osrm_selfhosted")
        if not breaker.allow_request():
            # Circuit already open (e.g. OSRM_BASE_URL points at a hostname
            # that doesn't resolve) — skip the network round-trip entirely
            # rather than pay request_with_retry's own "failing fast" check
            # (and its WARNING log) on every single call.
            _log_osrm_selfhosted_unavailable("circuit open")
        else:
            try:
                osrm_result, real_cells, total_cells = _osrm_matrix(
                    points,
                    base_url=osrm_base_url,
                    circuit_key="osrm_selfhosted",
                    provider_label="osrm_selfhosted",
                    timeout_seconds=_OSRM_SELFHOSTED_TIMEOUT_SECONDS,
                    retry_config=_OSRM_SELFHOSTED_RETRY_CONFIG,
                )
                # Same majority guard as `_yandex_pairwise` below (final
                # review round, I2): a response that is mostly Haversine
                # backfill under the hood must not be cached/trusted as
                # "osrm_selfhosted" real-road data.
                if total_cells > 0 and real_cells / total_cells >= _MIN_REAL_CELL_RATIO:
                    matrix = osrm_result
                    source = "osrm_selfhosted"
                    real_cell_counts = (real_cells, total_cells)
                else:
                    logger.warning(
                        "Self-hosted OSRM mostly missing cells (%d/%d real) — "
                        "falling through to the next matrix tier instead of "
                        "caching a mostly-Haversine result as osrm_selfhosted",
                        real_cells,
                        total_cells,
                    )
                    matrix = None
            except ProviderUnavailableError as exc:
                _log_osrm_selfhosted_unavailable(str(exc))
                matrix = None
            except Exception as exc:  # noqa: BLE001
                _log_osrm_selfhosted_unavailable(str(exc))
                matrix = None

    if matrix is None and legacy_enabled and here_key:
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

    if matrix is None and legacy_enabled and provider == "yandex" and yandex_key:
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
                # Shared with the OSRM tiers' guard below (final review round,
                # residual 3): one constant, not two hardcoded `0.5` literals
                # deciding the same threshold.
                if total > 0 and success / total >= _MIN_REAL_CELL_RATIO:
                    matrix = pairwise_matrix
                    source = "yandex_pairwise"
                    logger.info(
                        "Yandex pairwise succeeded (%d/%d pairs ok) — using "
                        "traffic-aware Yandex routing as the matrix source",
                        success,
                        total,
                    )
                else:
                    logger.warning(
                        "Yandex pairwise mostly failed (%d/%d pairs ok) — falling through",
                        success,
                        total,
                    )
                    matrix = None
            except ProviderUnavailableError as exc2:
                logger.warning("Yandex pairwise circuit open: %s", exc2)
                matrix = None

    # Public OSRM demo server: EMERGENCY fallback only, off by default. Its
    # usage policy forbids production use — this exists so an operator can
    # flip one flag during a self-hosted OSRM outage rather than dropping
    # straight to Haversine, and is expected to be turned off again once the
    # self-hosted instance is healthy.
    if matrix is None and demo_enabled:
        try:
            demo_result, demo_real_cells, demo_total_cells = _osrm_matrix(
                points,
                base_url=_OSRM_DEMO_BASE_URL,
                circuit_key="osrm_table",
                provider_label="osrm_table",
            )
            # Same majority guard as the self-hosted tier above (final
            # review round, I2).
            if demo_total_cells > 0 and demo_real_cells / demo_total_cells >= _MIN_REAL_CELL_RATIO:
                matrix = demo_result
                source = "osrm_table"
                real_cell_counts = (demo_real_cells, demo_total_cells)
            else:
                logger.warning(
                    "OSRM public demo mostly missing cells (%d/%d real) — "
                    "falling back to Haversine instead of caching a "
                    "mostly-Haversine result as osrm_table",
                    demo_real_cells,
                    demo_total_cells,
                )
                matrix = None
        except ProviderUnavailableError as exc:
            logger.warning("OSRM demo unavailable, falling back to Haversine: %s", exc)
            matrix = None
        except Exception as exc:  # noqa: BLE001
            logger.warning("OSRM demo unexpected error, falling back to Haversine: %s", exc)
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

    if source != "haversine":
        # Never cache the Haversine fallback — retry real providers next time.
        _store_split_cache(points, traffic, matrix, source)

    logger.info(
        "distance_matrix_built source=%s n=%d traffic=%s static_tier=%s live_tier=miss " "real_cells=%s",
        source,
        len(points),
        traffic,
        "hit" if static_tier_hit else "miss",
        f"{real_cell_counts[0]}/{real_cell_counts[1]}" if real_cell_counts else "n/a",
    )
    return matrix, source
