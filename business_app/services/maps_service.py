"""
Maps service for the Water Business Platform
Supports Google Maps, Yandex Maps, and OpenStreetMap
"""

import logging
import requests
from typing import Dict, Any, List, Optional, Tuple
from flask import current_app

from business_app.utils.distance_matrix import _OSRM_DEMO_BASE_URL
from business_app.utils.distance_matrix import get_cached_matrix_source as _get_cached_matrix_source
from business_app.utils.distance_matrix import get_distance_matrix as _get_distance_matrix
from business_app.utils.distance_matrix import yandex_route_totals
from business_app.utils.exceptions import (
    ConfigurationError,
    ExternalServiceError,
    NotFoundError,
    ProviderUnavailableError,
)
from business_app.utils.helpers import calculate_distance
from business_app.utils.http_client import RetryConfig, request_with_retry
from business_app.utils.polyline import decode_polyline

logger = logging.getLogger(__name__)


class MapsService:
    """Service for map-related operations"""

    def __init__(self):
        self.provider = current_app.config.get("MAPS_PROVIDER", "google").lower()
        self.google_api_key = current_app.config.get("GOOGLE_MAPS_API_KEY")
        self.yandex_api_key = current_app.config.get("YANDEX_MAPS_API_KEY")

        # Per-request timeout (seconds) for all outbound map HTTP calls. A bare
        # request with no timeout blocks indefinitely on a stalled connection,
        # which previously hung optimize_driver_route_task past its hard limit.
        self.request_timeout = current_app.config.get("MAPS_REQUEST_TIMEOUT", 10)

        # API endpoints
        self.google_geocoding_url = "https://maps.googleapis.com/maps/api/geocode/json"
        self.google_directions_url = "https://maps.googleapis.com/maps/api/directions/json"
        self.google_places_url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"

        self.yandex_geocoding_url = "https://geocode-maps.yandex.ru/1.x/"
        self.yandex_routing_url = "https://api.routing.yandex.net/v2/route"

        self.osm_nominatim_url = "https://nominatim.openstreetmap.org"

        # OSRM routing base. The tiering policy is NOT redecided here — it is
        # the same one `distance_matrix.py` already owns: our self-hosted
        # engine at OSRM_BASE_URL is the primary tier, and the public demo
        # server is an emergency-only fallback behind
        # OSRM_PUBLIC_FALLBACK_ENABLED (its usage policy forbids production
        # use). This module used to hardcode the demo URL and honour neither
        # setting, which sent the admin dispatch map's road geometry to a
        # third-party box while our own OSRM sat there with the geometry
        # dataset loaded. `None` means "no OSRM route tier is available" and
        # `_osm_get_route` refuses rather than quietly reaching for the demo.
        self.osm_routing_url = self._resolve_osrm_route_base()

    @staticmethod
    def _osrm_step_instruction(step: Dict[str, Any]) -> str:
        """Compose readable step text from an OSRM maneuver.

        OSRM deliberately ships NO `instruction` field — verified against the
        live engine, `maneuver` holds exactly
        `{bearing_after, bearing_before, location, modifier, type}`. Prose is
        the client's job (upstream's own answer is the separate
        osrm-text-instructions package). Reading `maneuver["instruction"]`
        was therefore an unconditional KeyError that made this whole provider
        branch raise on every call; it went unnoticed because production runs
        MAPS_PROVIDER=google|yandex and nothing consumes `get_route()["steps"]`.

        Deliberately plain English and not translated: `steps` currently has
        no consumer, and inventing DB-backed translation keys for text nobody
        renders would be worse than leaving it obvious. Route it through i18n
        the day something actually shows it to a driver.
        """
        maneuver = step.get("maneuver") or {}
        kind = (maneuver.get("type") or "").strip()
        modifier = (maneuver.get("modifier") or "").strip()
        name = (step.get("name") or "").strip()

        head = " ".join(part for part in (kind, modifier) if part).strip()
        if not head:
            head = "continue"
        text = f"{head} onto {name}" if name else head
        return text[:1].upper() + text[1:]

    @staticmethod
    def _resolve_osrm_route_base() -> Optional[str]:
        """Self-hosted first, public demo only if explicitly opted in."""
        base = (current_app.config.get("OSRM_BASE_URL") or "").rstrip("/")
        if base:
            return f"{base}/route/v1/driving"
        if current_app.config.get("OSRM_PUBLIC_FALLBACK_ENABLED"):
            return f"{_OSRM_DEMO_BASE_URL}/route/v1/driving"
        return None

    def geocode_address(self, address: str, city: str = "Tashkent") -> Dict[str, Any]:
        """
        Convert address to coordinates

        Args:
            address: Street address
            city: City name

        Returns:
            Dictionary with coordinates and formatted address
        """
        full_address = f"{address}, {city}, Uzbekistan"

        try:
            if self.provider == "google":
                return self._google_geocode(full_address)
            elif self.provider == "yandex":
                return self._yandex_geocode(full_address)
            else:  # OpenStreetMap
                return self._osm_geocode(full_address)
        except NotFoundError:
            # Expected "no match for this address" — let it propagate as a 404,
            # don't re-wrap it into a 503 ExternalServiceError.
            raise
        except Exception as e:
            raise ExternalServiceError(f"Geocoding failed: {e}")

    def reverse_geocode(self, latitude: float, longitude: float) -> Dict[str, Any]:
        """
        Convert coordinates to address

        Args:
            latitude: Latitude coordinate
            longitude: Longitude coordinate

        Returns:
            Dictionary with address information
        """
        try:
            if self.provider == "google":
                return self._google_reverse_geocode(latitude, longitude)
            elif self.provider == "yandex":
                return self._yandex_reverse_geocode(latitude, longitude)
            else:  # OpenStreetMap
                return self._osm_reverse_geocode(latitude, longitude)
        except Exception as e:
            raise ExternalServiceError(f"Reverse geocoding failed: {e}")

    def get_route(
        self,
        start_lat: float,
        start_lon: float,
        end_lat: float,
        end_lon: float,
        waypoints: List[Tuple[float, float]] = None,
    ) -> Dict[str, Any]:
        """
        Get route between two points

        Args:
            start_lat: Starting latitude
            start_lon: Starting longitude
            end_lat: Ending latitude
            end_lon: Ending longitude
            waypoints: Optional waypoints along the route

        Returns:
            Dictionary with route information
        """
        try:
            if self.provider == "google":
                return self._google_get_route(start_lat, start_lon, end_lat, end_lon, waypoints)
            elif self.provider == "yandex":
                return self._yandex_get_route(start_lat, start_lon, end_lat, end_lon, waypoints)
            else:  # OpenStreetMap/OSRM
                return self._osm_get_route(start_lat, start_lon, end_lat, end_lon, waypoints)
        except Exception as e:
            raise ExternalServiceError(f"Route calculation failed: {e}")

    def calculate_travel_time(
        self, start_lat: float, start_lon: float, end_lat: float, end_lon: float, departure_time: str = None
    ) -> Dict[str, Any]:
        """
        Calculate travel time between two points

        Args:
            start_lat: Starting latitude
            start_lon: Starting longitude
            end_lat: Ending latitude
            end_lon: Ending longitude
            departure_time: Departure time (ISO format)

        Returns:
            Dictionary with travel time information
        """
        route = self.get_route(start_lat, start_lon, end_lat, end_lon)

        return {
            "distance_km": route.get("distance_km", 0),
            "duration_minutes": route.get("duration_minutes", 0),
            "duration_in_traffic_minutes": route.get("duration_in_traffic_minutes"),
            "estimated_arrival": route.get("estimated_arrival"),
        }

    def find_nearby_places(
        self, latitude: float, longitude: float, place_type: str = "establishment", radius: int = 1000
    ) -> List[Dict[str, Any]]:
        """
        Find nearby places

        Args:
            latitude: Search center latitude
            longitude: Search center longitude
            place_type: Type of places to search for
            radius: Search radius in meters

        Returns:
            List of nearby places
        """
        try:
            if self.provider == "google":
                return self._google_find_nearby(latitude, longitude, place_type, radius)
            elif self.provider == "yandex":
                return self._yandex_find_nearby(latitude, longitude, place_type, radius)
            else:  # OpenStreetMap
                return self._osm_find_nearby(latitude, longitude, place_type, radius)
        except Exception as e:
            raise ExternalServiceError(f"Nearby search failed: {e}")

    def get_delivery_zones(self, center_lat: float, center_lon: float) -> List[Dict[str, Any]]:
        """
        Get delivery zones around a center point

        Args:
            center_lat: Center latitude
            center_lon: Center longitude

        Returns:
            List of delivery zones with boundaries
        """
        from business_app.utils.constants import DELIVERY_ZONES

        zones = []
        for zone_name, zone_info in DELIVERY_ZONES.items():
            # Create circular zone
            zone = {
                "name": zone_name,
                "display_name": zone_info["name"],
                "radius_km": zone_info["radius"],
                "delivery_fee": zone_info["fee"],
                "center": {"latitude": center_lat, "longitude": center_lon},
                "boundary_points": self._generate_circle_points(center_lat, center_lon, zone_info["radius"]),
            }
            zones.append(zone)

        return zones

    # Google Maps implementations
    def _google_geocode(self, address: str) -> Dict[str, Any]:
        """Geocode using Google Maps API"""
        if not self.google_api_key:
            raise ConfigurationError("Google Maps API key not configured")

        params = {"address": address, "key": self.google_api_key, "region": "uz"}

        response = requests.get(self.google_geocoding_url, params=params, timeout=self.request_timeout)
        response.raise_for_status()

        data = response.json()
        if data["status"] != "OK" or not data["results"]:
            raise NotFoundError("Address not found")

        result = data["results"][0]
        location = result["geometry"]["location"]

        return {
            "latitude": location["lat"],
            "longitude": location["lng"],
            "formatted_address": result["formatted_address"],
            "address_components": result.get("address_components", []),
            "place_id": result.get("place_id"),
        }

    def _google_reverse_geocode(self, latitude: float, longitude: float) -> Dict[str, Any]:
        """Reverse geocode using Google Maps API"""
        if not self.google_api_key:
            raise ConfigurationError("Google Maps API key not configured")

        params = {"latlng": f"{latitude},{longitude}", "key": self.google_api_key, "language": "en"}

        response = requests.get(self.google_geocoding_url, params=params, timeout=self.request_timeout)
        response.raise_for_status()

        data = response.json()
        if data["status"] != "OK" or not data["results"]:
            raise ExternalServiceError("Location not found")

        result = data["results"][0]

        return {
            "formatted_address": result["formatted_address"],
            "address_components": result.get("address_components", []),
            "place_id": result.get("place_id"),
        }

    def _google_get_route(
        self,
        start_lat: float,
        start_lon: float,
        end_lat: float,
        end_lon: float,
        waypoints: List[Tuple[float, float]] = None,
    ) -> Dict[str, Any]:
        """Get route using Google Directions API"""
        if not self.google_api_key:
            raise ConfigurationError("Google Maps API key not configured")

        params = {
            "origin": f"{start_lat},{start_lon}",
            "destination": f"{end_lat},{end_lon}",
            "key": self.google_api_key,
            "traffic_model": "best_guess",
            "departure_time": "now",
        }

        if waypoints:
            waypoint_str = "|".join([f"{lat},{lon}" for lat, lon in waypoints])
            params["waypoints"] = waypoint_str

        response = requests.get(self.google_directions_url, params=params, timeout=self.request_timeout)
        response.raise_for_status()

        data = response.json()
        if data["status"] != "OK" or not data["routes"]:
            raise ExternalServiceError("Route not found")

        route = data["routes"][0]
        leg = route["legs"][0]

        return {
            "distance_km": leg["distance"]["value"] / 1000,
            "distance_text": leg["distance"]["text"],
            "duration_minutes": leg["duration"]["value"] / 60,
            "duration_text": leg["duration"]["text"],
            "duration_in_traffic_minutes": leg.get("duration_in_traffic", {}).get("value", 0) / 60,
            # `overview_polyline.points` is an ENCODED polyline STRING, always
            # precision 5 for Google's Directions API — decode it here so
            # every `get_route()` caller sees the same normalised
            # `[[lat, lng], ...] | None` shape regardless of provider. This
            # used to be handed back raw under "polyline" and forwarded
            # straight to Leaflet's <Polyline positions={...}> by
            # admin_dispatch.py, which happily accepted a string where it
            # expected an array of coordinate pairs.
            "geometry": decode_polyline(route["overview_polyline"]["points"]),
            "steps": [
                {
                    "instruction": step["html_instructions"],
                    "distance": step["distance"]["text"],
                    "duration": step["duration"]["text"],
                }
                for step in leg["steps"]
            ],
        }

    def _google_find_nearby(
        self, latitude: float, longitude: float, place_type: str, radius: int
    ) -> List[Dict[str, Any]]:
        """Find nearby places using Google Places API"""
        if not self.google_api_key:
            raise ConfigurationError("Google Maps API key not configured")

        params = {
            "location": f"{latitude},{longitude}",
            "radius": radius,
            "type": place_type,
            "key": self.google_api_key,
        }

        response = requests.get(self.google_places_url, params=params, timeout=self.request_timeout)
        response.raise_for_status()

        data = response.json()
        places = []

        for place in data.get("results", []):
            places.append(
                {
                    "name": place["name"],
                    "place_id": place["place_id"],
                    "latitude": place["geometry"]["location"]["lat"],
                    "longitude": place["geometry"]["location"]["lng"],
                    "rating": place.get("rating"),
                    "types": place.get("types", []),
                    "vicinity": place.get("vicinity"),
                }
            )

        return places

    # Yandex Maps implementations
    def _yandex_geocode(self, address: str) -> Dict[str, Any]:
        """Geocode using Yandex Maps API"""
        if not self.yandex_api_key:
            raise ConfigurationError("Yandex Maps API key not configured")

        params = {"geocode": address, "apikey": self.yandex_api_key, "format": "json", "lang": "en"}

        response = requests.get(self.yandex_geocoding_url, params=params, timeout=self.request_timeout)
        response.raise_for_status()

        data = response.json()
        collection = data["response"]["GeoObjectCollection"]

        if not collection["featureMember"]:
            raise NotFoundError("Address not found")

        geo_object = collection["featureMember"][0]["GeoObject"]
        coords = geo_object["Point"]["pos"].split()

        return {
            "latitude": float(coords[1]),
            "longitude": float(coords[0]),
            "formatted_address": geo_object["metaDataProperty"]["GeocoderMetaData"]["text"],
            "precision": geo_object["metaDataProperty"]["GeocoderMetaData"]["precision"],
        }

    def _yandex_reverse_geocode(self, latitude: float, longitude: float) -> Dict[str, Any]:
        """Reverse geocode using Yandex Maps API"""
        return self._yandex_geocode(f"{longitude},{latitude}")

    def _yandex_get_route(
        self,
        start_lat: float,
        start_lon: float,
        end_lat: float,
        end_lon: float,
        waypoints: List[Tuple[float, float]] = None,
    ) -> Dict[str, Any]:
        """Get route using Yandex Routing API (traffic-aware)."""
        import time

        if not self.yandex_api_key:
            raise ConfigurationError("Yandex Maps API key not configured")

        # Yandex expects "lat,lng" pairs joined with "|" — start, intermediates, end.
        coords = [(start_lat, start_lon)]
        if waypoints:
            coords.extend(waypoints)
        coords.append((end_lat, end_lon))
        params = {
            "apikey": self.yandex_api_key,
            "waypoints": "|".join(f"{lat},{lng}" for lat, lng in coords),
            "mode": "driving",
            # Yandex requires Unix timestamp (uint32 seconds), not "now".
            "departure_time": int(time.time()),
        }

        try:
            response = request_with_retry(
                method="GET",
                url=self.yandex_routing_url,
                timeout_seconds=10,
                retry_config=RetryConfig(max_retries=2, backoff_base_seconds=0.5),
                circuit_key="yandex_route",
                params=params,
            )
        except ProviderUnavailableError as exc:
            logger.warning("Yandex routing unavailable, falling back to Haversine: %s", exc)
            distance = calculate_distance(start_lat, start_lon, end_lat, end_lon)
            return {
                "distance_km": distance,
                "duration_minutes": distance * 2.4,  # ~25 km/h city default
                "duration_in_traffic_minutes": None,
                "estimated_arrival": None,
                "geometry": None,
                "fallback": True,
            }

        if response.status_code >= 400:
            logger.warning(
                "Yandex routing returned %d: %s — falling back to Haversine",
                response.status_code,
                response.text[:200],
            )
            distance = calculate_distance(start_lat, start_lon, end_lat, end_lon)
            return {
                "distance_km": distance,
                "duration_minutes": distance * 2.4,
                "duration_in_traffic_minutes": None,
                "estimated_arrival": None,
                "geometry": None,
                "fallback": True,
            }

        data = response.json()
        route = data.get("route") or {}
        # NOTE (Task 12, 2026-08): `route.distance`/`route.duration` do NOT
        # exist in Yandex's documented Router API response (confirmed against
        # yandex.com/maps-api/docs/router-api/response.html) — there is no
        # route-level or leg-level distance/duration aggregate at all. The
        # real per-request totals must be summed from the per-step
        # `length`(m)/`duration`(s) inside `route.legs[].steps[]`, which is
        # exactly what `yandex_route_totals` (in
        # `business_app.utils.distance_matrix`) does, mirroring the
        # `legs[].steps[]` traversal `_yandex_route_geometry` below already
        # uses for `steps[].polyline.points`. `route.duration_in_traffic`
        # (top-level) is not part of the documented shape either — real-time
        # traffic, when requested via `departure_time`, is already reflected
        # in each step's own `duration`; this read is kept only as a
        # defensive no-op (resolves to `None`, same as before) in case some
        # response variant does carry it.
        distance_m, duration_s = yandex_route_totals(route)
        duration_traffic_s = (route.get("duration_in_traffic") or {}).get("value")

        return {
            "distance_km": distance_m / 1000.0,
            "duration_minutes": duration_s / 60.0,
            "duration_in_traffic_minutes": (duration_traffic_s / 60.0) if duration_traffic_s else None,
            "estimated_arrival": None,
            "geometry": self._yandex_route_geometry(route),
        }

    @staticmethod
    def _yandex_route_geometry(route: Dict[str, Any]) -> Optional[List[List[float]]]:
        """Real road geometry from a Yandex Router API `route` object.

        There is no top-level `route.geometry` (unlike this method's own
        pre-fix code assumed, and unlike Google/OSRM's `overview_polyline`).
        Per Yandex's documented response shape, the path lives nested under
        `route.legs[].steps[].polyline.points`, and each `points` entry is
        already a `[latitude, longitude]` pair — Yandex does NOT encode this
        as a compressed string the way Google/OSRM do, so no decoding step is
        needed here, only concatenation across every leg and step in order.

        This is based on Yandex's public Router API docs (fetched during this
        fix: yandex.com/maps-api/docs/router-api/response.html and
        .../examples.html), not a captured live response — this codebase has
        no test Yandex API key to call the real endpoint from. Written
        defensively (every level is `.get(...) or []`) so if the real shape
        turns out to differ in some way this doc reading missed, the result
        is `None` (the existing honest dashed-fallback), never a crash or a
        malformed positions array.
        """
        points: List[List[float]] = []
        for leg in route.get("legs") or []:
            for step in leg.get("steps") or []:
                step_points = (step.get("polyline") or {}).get("points") or []
                for point in step_points:
                    if (
                        isinstance(point, (list, tuple))
                        and len(point) == 2
                        and all(isinstance(coord, (int, float)) for coord in point)
                    ):
                        points.append([float(point[0]), float(point[1])])
        return points or None

    def get_distance_matrix(
        self,
        points: List[Tuple[float, float]],
        traffic: bool = True,
        use_cache: bool = True,
    ) -> Tuple[Dict[Tuple[int, int], Dict[str, float]], str]:
        """Return a distance/duration matrix for `points`.

        Delegates to `business_app.utils.distance_matrix.get_distance_matrix`,
        which handles Yandex matrix → pairwise → Haversine fallback and Redis
        caching. Returns (matrix, source_label).
        """
        return _get_distance_matrix(points, traffic=traffic, provider=self.provider, use_cache=use_cache)

    def get_cached_matrix_source(self, points: List[Tuple[float, float]], traffic: bool = True) -> Optional[str]:
        """Recover the provider that produced a full cache HIT for `points`.

        Delegates to `business_app.utils.distance_matrix.get_cached_matrix_source`
        (final review round, I3). Returns None when not recoverable (miss, or
        a cache entry written before this fix) — callers must not guess.
        """
        return _get_cached_matrix_source(points, traffic=traffic)

    def _yandex_find_nearby(
        self, latitude: float, longitude: float, place_type: str, radius: int
    ) -> List[Dict[str, Any]]:
        """Find nearby places using Yandex API"""
        # Simplified implementation
        return []

    # OpenStreetMap implementations
    def _osm_geocode(self, address: str) -> Dict[str, Any]:
        """Geocode using OpenStreetMap Nominatim"""
        params = {"q": address, "format": "json", "limit": 1, "countrycodes": "uz"}

        headers = {"User-Agent": "WaterBusinessPlatform/1.0"}
        response = requests.get(
            f"{self.osm_nominatim_url}/search", params=params, headers=headers, timeout=self.request_timeout
        )
        response.raise_for_status()

        data = response.json()
        if not data:
            raise NotFoundError("Address not found")

        result = data[0]

        return {
            "latitude": float(result["lat"]),
            "longitude": float(result["lon"]),
            "formatted_address": result["display_name"],
            "osm_id": result.get("osm_id"),
            "place_id": result.get("place_id"),
        }

    def _osm_reverse_geocode(self, latitude: float, longitude: float) -> Dict[str, Any]:
        """Reverse geocode using OpenStreetMap Nominatim"""
        params = {"lat": latitude, "lon": longitude, "format": "json"}

        headers = {"User-Agent": "WaterBusinessPlatform/1.0"}
        response = requests.get(
            f"{self.osm_nominatim_url}/reverse", params=params, headers=headers, timeout=self.request_timeout
        )
        response.raise_for_status()

        data = response.json()

        return {
            "formatted_address": data.get("display_name", ""),
            "address_components": data.get("address", {}),
            "osm_id": data.get("osm_id"),
            "place_id": data.get("place_id"),
        }

    def _osm_get_route(
        self,
        start_lat: float,
        start_lon: float,
        end_lat: float,
        end_lon: float,
        waypoints: List[Tuple[float, float]] = None,
    ) -> Dict[str, Any]:
        """Get route using OSRM (self-hosted; public demo only if opted in)."""
        if not self.osm_routing_url:
            # Refuse loudly rather than silently reaching for the public demo
            # server. The one caller that renders this (the admin dispatch
            # map) already degrades to straight dashed legs when geometry is
            # unavailable, so a hard failure here is visible but harmless.
            raise ExternalServiceError(
                "No OSRM route endpoint available: set OSRM_BASE_URL to the "
                "self-hosted engine, or set OSRM_PUBLIC_FALLBACK_ENABLED=true "
                "to permit the public demo server."
            )

        coords = f"{start_lon},{start_lat};{end_lon},{end_lat}"

        if waypoints:
            waypoint_coords = ";".join([f"{lon},{lat}" for lat, lon in waypoints])
            coords = f"{start_lon},{start_lat};{waypoint_coords};{end_lon},{end_lat}"

        url = f"{self.osm_routing_url}/{coords}"
        # `geometries=polyline` (NOT `polyline6`) — precision 5, identical to
        # Google's overview_polyline encoding. If this is ever changed to
        # `polyline6`, `decode_polyline` below must be called with
        # `precision=6` or every decoded point will be off by 10x.
        params = {"overview": "full", "geometries": "polyline", "steps": "true"}

        response = requests.get(url, params=params, timeout=self.request_timeout)
        response.raise_for_status()

        data = response.json()
        if data["code"] != "Ok" or not data["routes"]:
            raise ExternalServiceError("Route not found")

        route = data["routes"][0]

        return {
            "distance_km": route["distance"] / 1000,
            "duration_minutes": route["duration"] / 60,
            # `route["geometry"]` is OSRM's raw encoded-polyline STRING (see
            # the `params` comment above) — decode it into the same
            # normalised `[[lat, lng], ...] | None` shape every provider now
            # returns under "geometry".
            "geometry": decode_polyline(route["geometry"]),
            "steps": [
                {
                    "instruction": self._osrm_step_instruction(step),
                    "distance": f"{step['distance']} m",
                    "duration": f"{step['duration']} s",
                }
                for leg in route["legs"]
                for step in leg["steps"]
            ],
        }

    def _osm_find_nearby(self, latitude: float, longitude: float, place_type: str, radius: int) -> List[Dict[str, Any]]:
        """Find nearby places using Overpass API"""
        # This would require integration with Overpass API
        # Simplified implementation for now
        return []

    def _generate_circle_points(
        self, center_lat: float, center_lon: float, radius_km: float, num_points: int = 36
    ) -> List[Dict[str, float]]:
        """Generate points around a circle for zone boundaries"""
        import math

        points = []
        for i in range(num_points):
            angle = 2 * math.pi * i / num_points

            # Convert radius from km to degrees (approximation)
            lat_offset = radius_km / 111.0  # 1 degree lat ≈ 111 km
            lon_offset = radius_km / (111.0 * math.cos(math.radians(center_lat)))

            lat = center_lat + lat_offset * math.cos(angle)
            lon = center_lon + lon_offset * math.sin(angle)

            points.append({"latitude": lat, "longitude": lon})

        return points
