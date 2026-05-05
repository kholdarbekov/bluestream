"""
Maps service for the Water Business Platform
Supports Google Maps, Yandex Maps, and OpenStreetMap
"""

import logging
import requests
from typing import Dict, Any, List, Tuple
from flask import current_app

from business_app.utils.distance_matrix import get_distance_matrix as _get_distance_matrix
from business_app.utils.exceptions import ExternalServiceError, ConfigurationError, ProviderUnavailableError
from business_app.utils.helpers import calculate_distance
from business_app.utils.http_client import RetryConfig, request_with_retry

logger = logging.getLogger(__name__)


class MapsService:
    """Service for map-related operations"""

    def __init__(self):
        self.provider = current_app.config.get("MAPS_PROVIDER", "google").lower()
        self.google_api_key = current_app.config.get("GOOGLE_MAPS_API_KEY")
        self.yandex_api_key = current_app.config.get("YANDEX_MAPS_API_KEY")

        # API endpoints
        self.google_geocoding_url = "https://maps.googleapis.com/maps/api/geocode/json"
        self.google_directions_url = "https://maps.googleapis.com/maps/api/directions/json"
        self.google_places_url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"

        self.yandex_geocoding_url = "https://geocode-maps.yandex.ru/1.x/"
        self.yandex_routing_url = "https://api.routing.yandex.net/v2/route"

        self.osm_nominatim_url = "https://nominatim.openstreetmap.org"
        self.osm_routing_url = "https://router.project-osrm.org/route/v1/driving"

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

    def validate_coordinates(self, latitude: float, longitude: float) -> bool:
        """
        Validate if coordinates are within Tashkent delivery area

        Args:
            latitude: Latitude coordinate
            longitude: Longitude coordinate

        Returns:
            True if coordinates are valid for delivery
        """
        # Tashkent approximate bounds
        tashkent_bounds = {"north": 41.4, "south": 41.1, "east": 69.4, "west": 69.1}

        return (
            tashkent_bounds["south"] <= latitude <= tashkent_bounds["north"]
            and tashkent_bounds["west"] <= longitude <= tashkent_bounds["east"]
        )

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

        response = requests.get(self.google_geocoding_url, params=params)
        response.raise_for_status()

        data = response.json()
        if data["status"] != "OK" or not data["results"]:
            raise ExternalServiceError("Address not found")

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

        response = requests.get(self.google_geocoding_url, params=params)
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

        response = requests.get(self.google_directions_url, params=params)
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
            "polyline": route["overview_polyline"]["points"],
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

        response = requests.get(self.google_places_url, params=params)
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

        response = requests.get(self.yandex_geocoding_url, params=params)
        response.raise_for_status()

        data = response.json()
        collection = data["response"]["GeoObjectCollection"]

        if not collection["featureMember"]:
            raise ExternalServiceError("Address not found")

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
                "polyline": None,
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
                "polyline": None,
                "fallback": True,
            }

        data = response.json()
        route = data.get("route") or {}
        distance_m = (route.get("distance") or {}).get("value", 0)
        duration_s = (route.get("duration") or {}).get("value", 0)
        duration_traffic_s = (route.get("duration_in_traffic") or {}).get("value")

        return {
            "distance_km": distance_m / 1000.0,
            "duration_minutes": duration_s / 60.0,
            "duration_in_traffic_minutes": (duration_traffic_s / 60.0) if duration_traffic_s else None,
            "estimated_arrival": None,
            "polyline": route.get("geometry"),
        }

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
        response = requests.get(f"{self.osm_nominatim_url}/search", params=params, headers=headers)
        response.raise_for_status()

        data = response.json()
        if not data:
            raise ExternalServiceError("Address not found")

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
        response = requests.get(f"{self.osm_nominatim_url}/reverse", params=params, headers=headers)
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
        """Get route using OSRM"""
        coords = f"{start_lon},{start_lat};{end_lon},{end_lat}"

        if waypoints:
            waypoint_coords = ";".join([f"{lon},{lat}" for lat, lon in waypoints])
            coords = f"{start_lon},{start_lat};{waypoint_coords};{end_lon},{end_lat}"

        url = f"{self.osm_routing_url}/{coords}"
        params = {"overview": "full", "geometries": "polyline", "steps": "true"}

        response = requests.get(url, params=params)
        response.raise_for_status()

        data = response.json()
        if data["code"] != "Ok" or not data["routes"]:
            raise ExternalServiceError("Route not found")

        route = data["routes"][0]

        return {
            "distance_km": route["distance"] / 1000,
            "duration_minutes": route["duration"] / 60,
            "polyline": route["geometry"],
            "steps": [
                {
                    "instruction": step["maneuver"]["instruction"],
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
