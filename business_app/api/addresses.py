"""
User Addresses API endpoints
Manages user delivery addresses with geocoding support
"""

from flask import Blueprint, request, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError

from business_app.models.user import UserAddress
from business_app.utils.error_handlers import handle_api_exception
from business_app.utils.exceptions import NotFoundError
from business_app.utils.api_responses import (
    success_response,
    error_response,
    created_response,
    not_found_response,
    validation_error_response,
)
from business_app.utils.translations import get_translation
from shared.constants import is_within_tashkent, get_geo_config, get_all_districts
from business_app import db

addresses_bp = Blueprint("addresses", __name__)


@addresses_bp.route("/", methods=["GET"])
@jwt_required()
@handle_api_exception
def get_user_addresses():
    """Get all addresses for current user"""
    user_id = get_jwt_identity()

    addresses = (
        UserAddress.query.filter_by(user_id=user_id)
        .order_by(desc(UserAddress.is_default), desc(UserAddress.created_at))
        .all()
    )

    return success_response(data={"addresses": [addr.to_dict() for addr in addresses]})


@addresses_bp.route("/<int:address_id>", methods=["GET"])
@jwt_required()
@handle_api_exception
def get_address(address_id):
    """Get specific address"""
    user_id = get_jwt_identity()

    address = UserAddress.query.filter_by(id=address_id, user_id=user_id).first()

    if not address:
        return not_found_response(message=get_translation("api.addresses.error.not_found"))

    return success_response(data={"address": address.to_dict()})


@addresses_bp.route("/", methods=["POST"])
@jwt_required()
@handle_api_exception
def create_address():
    """Create new address

    Required fields:
    - full_address: Complete address string

    Optional fields:
    - title: Address label (e.g., "Home", "Work")
    - street_address: Street name and number
    - city: City name (default: Tashkent)
    - district: District/region name
    - postal_code: Postal/ZIP code
    - country: Country (default: Uzbekistan)
    - latitude: GPS latitude coordinate
    - longitude: GPS longitude coordinate
    - is_default: Set as default address
    - is_business: Business address flag
    - delivery_instructions: Special delivery instructions
    - landmark: Nearby landmark
    - floor_number: Building floor
    - apartment_number: Apartment/unit number
    """
    user_id = get_jwt_identity()
    data = request.get_json()

    # Validate required field - at minimum need full_address OR latitude/longitude
    if not data.get("full_address") and not (data.get("latitude") and data.get("longitude")):
        return validation_error_response(
            errors={"address": get_translation("api.addresses.error.full_address_or_coordinates_required")}
        )

    # Enforce the delivery-zone SSOT before persisting (see business_app/utils/geo_validation.py)
    latitude, longitude = data.get("latitude"), data.get("longitude")
    if latitude is not None and longitude is not None and not is_within_tashkent(latitude, longitude):
        return validation_error_response(
            errors={"coordinates": get_translation("api.addresses.error.coordinates_outside_supported_area")}
        )

    # Check if this should be default
    is_default = data.get("is_default", False)

    # If setting as default, unset other defaults
    if is_default:
        UserAddress.query.filter_by(user_id=user_id, is_default=True).update({"is_default": False})

    # If user has no addresses, make this the default
    existing_count = UserAddress.query.filter_by(user_id=user_id).count()
    if existing_count == 0:
        is_default = True

    # Create address with correct field mapping
    address = UserAddress(
        user_id=user_id,
        title=data.get("title"),
        full_address=data.get("full_address", ""),
        street_address=data.get("street_address"),
        city=data.get("city", "Tashkent"),
        district=data.get("district"),
        postal_code=data.get("postal_code"),
        country=data.get("country", "Uzbekistan"),
        latitude=data.get("latitude"),
        longitude=data.get("longitude"),
        is_default=is_default,
        is_business=data.get("is_business", False),
        delivery_instructions=data.get("delivery_instructions"),
        landmark=data.get("landmark"),
        floor_number=data.get("floor_number"),
        apartment_number=data.get("apartment_number"),
    )

    db.session.add(address)
    db.session.commit()

    current_app.logger.info(f"User {user_id} created new address {address.id}")

    return created_response(
        data={"address": address.to_dict()}, message=get_translation("api.addresses.success.created")
    )


@addresses_bp.route("/<int:address_id>", methods=["PUT"])
@jwt_required()
@handle_api_exception
def update_address(address_id):
    """Update existing address"""
    user_id = get_jwt_identity()
    data = request.get_json()

    address = UserAddress.query.filter_by(id=address_id, user_id=user_id).first()

    if not address:
        return not_found_response(message=get_translation("api.addresses.error.not_found"))

    # Enforce the delivery-zone SSOT when coordinates are being changed
    if "latitude" in data or "longitude" in data:
        new_lat = data["latitude"] if "latitude" in data else address.latitude
        new_lng = data["longitude"] if "longitude" in data else address.longitude
        if new_lat is not None and new_lng is not None and not is_within_tashkent(new_lat, new_lng):
            return validation_error_response(
                errors={"coordinates": get_translation("api.addresses.error.coordinates_outside_supported_area")}
            )

    # Update fields with correct mapping
    if "title" in data:
        address.title = data["title"]
    if "full_address" in data:
        address.full_address = data["full_address"]
    if "street_address" in data:
        address.street_address = data["street_address"]
    if "city" in data:
        address.city = data["city"]
    if "district" in data:
        address.district = data["district"]
    if "postal_code" in data:
        address.postal_code = data["postal_code"]
    if "country" in data:
        address.country = data["country"]
    if "latitude" in data:
        address.latitude = data["latitude"]
    if "longitude" in data:
        address.longitude = data["longitude"]
    if "is_business" in data:
        address.is_business = data["is_business"]
    if "delivery_instructions" in data:
        address.delivery_instructions = data["delivery_instructions"]
    if "landmark" in data:
        address.landmark = data["landmark"]
    if "floor_number" in data:
        address.floor_number = data["floor_number"]
    if "apartment_number" in data:
        address.apartment_number = data["apartment_number"]

    # Handle default flag
    if "is_default" in data and data["is_default"]:
        # Unset other defaults
        UserAddress.query.filter_by(user_id=user_id, is_default=True).update({"is_default": False})
        address.is_default = True

    db.session.commit()

    current_app.logger.info(f"User {user_id} updated address {address_id}")

    return success_response(
        data={"address": address.to_dict()}, message=get_translation("api.addresses.success.updated")
    )


@addresses_bp.route("/<int:address_id>", methods=["DELETE"])
@jwt_required()
@handle_api_exception
def delete_address(address_id):
    """Delete address"""
    user_id = get_jwt_identity()

    address = UserAddress.query.filter_by(id=address_id, user_id=user_id).first()

    if not address:
        return not_found_response(message=get_translation("api.addresses.error.not_found"))

    # Don't allow deleting if it's the only address
    address_count = UserAddress.query.filter_by(user_id=user_id).count()
    if address_count == 1:
        return validation_error_response(
            errors={"address": get_translation("api.addresses.error.cannot_delete_only_address")}
        )

    from business_app.services.subscription_service import SubscriptionService

    if SubscriptionService.user_has_subscription_using_address(user_id, address_id):
        message = get_translation("api.addresses.error.in_use_by_subscription")
        if message == "api.addresses.error.in_use_by_subscription":
            message = "Cannot delete an address used by subscriptions"
        return validation_error_response(errors={"address": message})

    # If deleting default address, set another as default
    if address.is_default:
        other_address = UserAddress.query.filter(UserAddress.user_id == user_id, UserAddress.id != address_id).first()
        if other_address:
            other_address.is_default = True

    from business_app.utils.transactions import atomic_transaction

    try:
        with atomic_transaction():
            db.session.delete(address)
    except IntegrityError:
        return validation_error_response(errors={"address": "Cannot delete an address referenced by existing records"})

    current_app.logger.info(f"User {user_id} deleted address {address_id}")

    return success_response(message=get_translation("api.addresses.success.deleted"))


@addresses_bp.route("/<int:address_id>/set-default", methods=["POST"])
@jwt_required()
@handle_api_exception
def set_default_address(address_id):
    """Set address as default"""
    user_id = get_jwt_identity()

    address = UserAddress.query.filter_by(id=address_id, user_id=user_id).first()

    if not address:
        return not_found_response(message=get_translation("api.addresses.error.not_found"))

    # Unset other defaults
    UserAddress.query.filter_by(user_id=user_id, is_default=True).update({"is_default": False})

    # Set this as default
    address.is_default = True
    db.session.commit()

    current_app.logger.info(f"User {user_id} set address {address_id} as default")

    return success_response(
        data={"address": address.to_dict()}, message=get_translation("api.addresses.success.default_updated")
    )


# ============================================================================
# Geocoding Endpoints
# ============================================================================


@addresses_bp.route("/geocode", methods=["POST"])
@jwt_required()
@handle_api_exception
def geocode_address():
    """Geocode an address string to coordinates

    Request body:
    - address: Address string to geocode (required)
    - hint_lat: Optional latitude hint for better results
    - hint_lon: Optional longitude hint for better results

    Returns:
    - latitude: Geocoded latitude
    - longitude: Geocoded longitude
    - formatted_address: Formatted address from geocoder
    """
    user_id = get_jwt_identity()
    data = request.get_json()

    address = data.get("address")
    if not address:
        return validation_error_response(
            errors={"address": get_translation("api.addresses.error.address_string_required")}
        )

    from business_app.services.maps_service import MapsService

    maps_service = MapsService()

    try:
        result = maps_service.geocode_address(address, city="Tashkent")

        if result and result.get("latitude") and result.get("longitude"):
            return success_response(
                data={
                    "latitude": result.get("latitude"),
                    "longitude": result.get("longitude"),
                    "formatted_address": result.get("formatted_address", address),
                }
            )
        # Defensive: providers signal no-match by raising NotFoundError, but keep
        # this branch in case a provider ever returns an empty/partial result.
        return error_response(message=get_translation("api.addresses.error.geocode_not_found"), status_code=404)
    except NotFoundError:
        # Expected bad user input (address the geocoder can't resolve) — not a
        # server fault. Log at INFO and return a clean 404, not ERROR/503.
        current_app.logger.info(f"Geocode: address not found for user {user_id}: address={address!r}")
        return error_response(message=get_translation("api.addresses.error.geocode_not_found"), status_code=404)
    except Exception as e:
        current_app.logger.error(f"Geocoding failed for user {user_id}: {e}")
        return error_response(
            message=get_translation("api.addresses.error.geocoding_service_unavailable"), status_code=503
        )


@addresses_bp.route("/reverse-geocode", methods=["POST"])
@jwt_required()
@handle_api_exception
def reverse_geocode():
    """Reverse geocode coordinates to address

    Request body:
    - latitude: GPS latitude (required)
    - longitude: GPS longitude (required)

    Returns:
    - formatted_address: Human-readable address
    - district: Detected district if available
    - city: City name
    - country: Country name
    """
    user_id = get_jwt_identity()
    data = request.get_json()

    latitude = data.get("latitude")
    longitude = data.get("longitude")

    if latitude is None or longitude is None:
        return validation_error_response(
            errors={"coordinates": get_translation("api.addresses.error.coordinates_required")}
        )

    # Validate coordinates are within Tashkent bounds
    if not is_within_tashkent(latitude, longitude):
        return validation_error_response(
            errors={"coordinates": get_translation("api.addresses.error.coordinates_outside_supported_area")}
        )

    from business_app.services.maps_service import MapsService

    maps_service = MapsService()

    try:
        result = maps_service.reverse_geocode(latitude, longitude)

        # Try to extract district from address components
        district = None
        formatted_address = result.get("formatted_address", "")

        if "address_components" in result:
            components = result["address_components"]
            # Try different component types for district
            for component in components if isinstance(components, list) else []:
                types = component.get("types", [])
                if "sublocality_level_1" in types or "administrative_area_level_2" in types:
                    district = component.get("long_name")
                    break

        return success_response(
            data={
                "formatted_address": formatted_address,
                "district": district,
                "city": get_translation("api.addresses.city.tashkent"),
                "country": get_translation("api.addresses.country.uzbekistan"),
            }
        )
    except Exception as e:
        current_app.logger.error(f"Reverse geocoding failed for user {user_id}: {e}")
        return error_response(
            message=get_translation("api.addresses.error.geocoding_service_unavailable"), status_code=503
        )


@addresses_bp.route("/districts", methods=["GET"])
@handle_api_exception
def get_districts():
    """Get list of supported districts with translations

    This is a public endpoint - districts are public reference data.

    Query params:
    - lang: Language code (en, uz, ru) - default: en

    Returns:
    - districts: List of {key, name} objects
    """
    language = request.args.get("lang", "en")

    # Validate language
    if language not in ["en", "uz", "ru"]:
        language = "en"

    districts = get_all_districts(language)

    return success_response(
        data={
            "districts": districts,
            "region": "tashkent_city",
            "region_name": get_translation("api.addresses.region.tashkent_city"),
        }
    )


@addresses_bp.route("/geo-config", methods=["GET"])
@handle_api_exception
def get_geo_configuration():
    """Get geographic configuration for map-based address selection

    This is a public endpoint - geo configuration is public reference data.

    Query params:
    - lang: Language code (en, uz, ru) - default: en

    Returns:
    - center: Tashkent city center coordinates {latitude, longitude} (map-centering hint)
    - polygon: Delivery coverage area as [lat, lng] pairs (single source of truth)
    - districts: List of districts with keys and localized names
    """
    language = request.args.get("lang", "en")

    # Validate language
    if language not in ["en", "uz", "ru"]:
        language = "en"

    config = get_geo_config(language)

    return success_response(
        data={
            "center": config["center"],
            "polygon": config.get("polygon"),
            "districts": config["districts"],
            "region": "tashkent_city",
            "region_name": get_translation("api.addresses.region.tashkent_city"),
        }
    )
