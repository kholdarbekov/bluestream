"""
User Addresses API endpoints
Manages user delivery addresses with geocoding support
"""
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import desc

from business_app.models.user import User, UserAddress
from business_app.utils.error_handlers import handle_api_exception
from business_app.utils.exceptions import ValidationError, NotFoundError, ForbiddenError
from business_app.utils.decorators import validate_json, rate_limit
from business_app.utils.api_responses import (
    success_response, error_response, created_response,
    not_found_response, validation_error_response, forbidden_response
)
from business_app.utils.translations import get_translation
from business_app.utils.constants import TASHKENT_DISTRICTS, get_district_name, get_all_districts
from shared.constants import TASHKENT_BOUNDS, TASHKENT_COORDINATES, is_within_tashkent, get_geo_config
from business_app import db

addresses_bp = Blueprint('addresses', __name__)


@addresses_bp.route('/', methods=['GET'])
@jwt_required()
@handle_api_exception
def get_user_addresses():
    """Get all addresses for current user"""
    user_id = get_jwt_identity()

    addresses = UserAddress.query.filter_by(user_id=user_id).order_by(
        desc(UserAddress.is_default), desc(UserAddress.created_at)
    ).all()

    return success_response(data={
        'addresses': [addr.to_dict() for addr in addresses]
    })


@addresses_bp.route('/<int:address_id>', methods=['GET'])
@jwt_required()
@handle_api_exception
def get_address(address_id):
    """Get specific address"""
    user_id = get_jwt_identity()

    address = UserAddress.query.filter_by(id=address_id, user_id=user_id).first()

    if not address:
        return not_found_response(message='Address not found')

    return success_response(data={'address': address.to_dict()})


@addresses_bp.route('/', methods=['POST'])
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
    if not data.get('full_address') and not (data.get('latitude') and data.get('longitude')):
        return validation_error_response(
            errors={'address': 'Either full_address or coordinates (latitude/longitude) are required'}
        )

    # Check if this should be default
    is_default = data.get('is_default', False)

    # If setting as default, unset other defaults
    if is_default:
        UserAddress.query.filter_by(user_id=user_id, is_default=True).update(
            {'is_default': False}
        )

    # If user has no addresses, make this the default
    existing_count = UserAddress.query.filter_by(user_id=user_id).count()
    if existing_count == 0:
        is_default = True

    # Create address with correct field mapping
    address = UserAddress(
        user_id=user_id,
        title=data.get('title'),
        full_address=data.get('full_address', ''),
        street_address=data.get('street_address'),
        city=data.get('city', 'Tashkent'),
        district=data.get('district'),
        postal_code=data.get('postal_code'),
        country=data.get('country', 'Uzbekistan'),
        latitude=data.get('latitude'),
        longitude=data.get('longitude'),
        is_default=is_default,
        is_business=data.get('is_business', False),
        delivery_instructions=data.get('delivery_instructions'),
        landmark=data.get('landmark'),
        floor_number=data.get('floor_number'),
        apartment_number=data.get('apartment_number')
    )

    db.session.add(address)
    db.session.commit()

    current_app.logger.info(f"User {user_id} created new address {address.id}")

    return created_response(
        data={'address': address.to_dict()},
        message='Address created successfully'
    )


@addresses_bp.route('/<int:address_id>', methods=['PUT'])
@jwt_required()
@handle_api_exception
def update_address(address_id):
    """Update existing address"""
    user_id = get_jwt_identity()
    data = request.get_json()

    address = UserAddress.query.filter_by(id=address_id, user_id=user_id).first()

    if not address:
        return not_found_response(message='Address not found')

    # Update fields with correct mapping
    if 'title' in data:
        address.title = data['title']
    if 'full_address' in data:
        address.full_address = data['full_address']
    if 'street_address' in data:
        address.street_address = data['street_address']
    if 'city' in data:
        address.city = data['city']
    if 'district' in data:
        address.district = data['district']
    if 'postal_code' in data:
        address.postal_code = data['postal_code']
    if 'country' in data:
        address.country = data['country']
    if 'latitude' in data:
        address.latitude = data['latitude']
    if 'longitude' in data:
        address.longitude = data['longitude']
    if 'is_business' in data:
        address.is_business = data['is_business']
    if 'delivery_instructions' in data:
        address.delivery_instructions = data['delivery_instructions']
    if 'landmark' in data:
        address.landmark = data['landmark']
    if 'floor_number' in data:
        address.floor_number = data['floor_number']
    if 'apartment_number' in data:
        address.apartment_number = data['apartment_number']

    # Handle default flag
    if 'is_default' in data and data['is_default']:
        # Unset other defaults
        UserAddress.query.filter_by(user_id=user_id, is_default=True).update(
            {'is_default': False}
        )
        address.is_default = True

    db.session.commit()

    current_app.logger.info(f"User {user_id} updated address {address_id}")

    return success_response(
        data={'address': address.to_dict()},
        message='Address updated successfully'
    )


@addresses_bp.route('/<int:address_id>', methods=['DELETE'])
@jwt_required()
@handle_api_exception
def delete_address(address_id):
    """Delete address"""
    user_id = get_jwt_identity()

    address = UserAddress.query.filter_by(id=address_id, user_id=user_id).first()

    if not address:
        return not_found_response(message='Address not found')

    # Don't allow deleting if it's the only address
    address_count = UserAddress.query.filter_by(user_id=user_id).count()
    if address_count == 1:
        return validation_error_response(
            errors={'address': 'Cannot delete your only address'}
        )

    # If deleting default address, set another as default
    if address.is_default:
        other_address = UserAddress.query.filter(
            UserAddress.user_id == user_id,
            UserAddress.id != address_id
        ).first()
        if other_address:
            other_address.is_default = True

    db.session.delete(address)
    db.session.commit()

    current_app.logger.info(f"User {user_id} deleted address {address_id}")

    return success_response(message=get_translation('api.addresses.success.deleted'))


@addresses_bp.route('/<int:address_id>/set-default', methods=['POST'])
@jwt_required()
@handle_api_exception
def set_default_address(address_id):
    """Set address as default"""
    user_id = get_jwt_identity()

    address = UserAddress.query.filter_by(id=address_id, user_id=user_id).first()

    if not address:
        return not_found_response(message='Address not found')

    # Unset other defaults
    UserAddress.query.filter_by(user_id=user_id, is_default=True).update(
        {'is_default': False}
    )

    # Set this as default
    address.is_default = True
    db.session.commit()

    current_app.logger.info(f"User {user_id} set address {address_id} as default")

    return success_response(
        data={'address': address.to_dict()},
        message='Default address updated'
    )


# ============================================================================
# Geocoding Endpoints
# ============================================================================

@addresses_bp.route('/geocode', methods=['POST'])
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

    address = data.get('address')
    if not address:
        return validation_error_response(errors={'address': 'Address string is required'})

    from business_app.services.maps_service import MapsService
    maps_service = MapsService()

    try:
        result = maps_service.geocode_address(address, city='Tashkent')

        if result and result.get('latitude') and result.get('longitude'):
            return success_response(data={
                'latitude': result.get('latitude'),
                'longitude': result.get('longitude'),
                'formatted_address': result.get('formatted_address', address)
            })
        else:
            return error_response(
                message='Could not geocode the address. Please try with more specific details.',
                status_code=404
            )
    except Exception as e:
        current_app.logger.error(f"Geocoding failed for user {user_id}: {e}")
        return error_response(
            message='Geocoding service temporarily unavailable',
            status_code=503
        )


@addresses_bp.route('/reverse-geocode', methods=['POST'])
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

    latitude = data.get('latitude')
    longitude = data.get('longitude')

    if latitude is None or longitude is None:
        return validation_error_response(
            errors={'coordinates': 'Both latitude and longitude are required'}
        )

    # Validate coordinates are within Tashkent bounds
    if not is_within_tashkent(latitude, longitude):
        return validation_error_response(
            errors={'coordinates': 'Coordinates are outside the supported delivery area (Tashkent)'}
        )

    from business_app.services.maps_service import MapsService
    maps_service = MapsService()

    try:
        result = maps_service.reverse_geocode(latitude, longitude)

        # Try to extract district from address components
        district = None
        formatted_address = result.get('formatted_address', '')

        if 'address_components' in result:
            components = result['address_components']
            # Try different component types for district
            for component in components if isinstance(components, list) else []:
                types = component.get('types', [])
                if 'sublocality_level_1' in types or 'administrative_area_level_2' in types:
                    district = component.get('long_name')
                    break

        return success_response(data={
            'formatted_address': formatted_address,
            'district': district,
            'city': 'Tashkent',
            'country': 'Uzbekistan'
        })
    except Exception as e:
        current_app.logger.error(f"Reverse geocoding failed for user {user_id}: {e}")
        return error_response(
            message='Geocoding service temporarily unavailable',
            status_code=503
        )


@addresses_bp.route('/districts', methods=['GET'])
@handle_api_exception
def get_districts():
    """Get list of supported districts with translations

    This is a public endpoint - districts are public reference data.

    Query params:
    - lang: Language code (en, uz, ru) - default: en

    Returns:
    - districts: List of {key, name} objects
    """
    language = request.args.get('lang', 'en')

    # Validate language
    if language not in ['en', 'uz', 'ru']:
        language = 'en'

    districts = get_all_districts(language)

    return success_response(data={
        'districts': districts,
        'region': 'tashkent_city',
        'region_name': {
            'en': 'Tashkent City',
            'uz': 'Toshkent shahri',
            'ru': 'Город Ташкент'
        }.get(language, 'Tashkent City')
    })


@addresses_bp.route('/geo-config', methods=['GET'])
@handle_api_exception
def get_geo_configuration():
    """Get geographic configuration for map-based address selection

    This is a public endpoint - geo configuration is public reference data.

    Query params:
    - lang: Language code (en, uz, ru) - default: en

    Returns:
    - center: Tashkent city center coordinates {latitude, longitude}
    - bounds: Service area boundary {min_lat, max_lat, min_lng, max_lng}
    - districts: List of districts with keys and localized names
    """
    language = request.args.get('lang', 'en')

    # Validate language
    if language not in ['en', 'uz', 'ru']:
        language = 'en'

    config = get_geo_config(language)

    return success_response(data={
        'center': config['center'],
        'bounds': config['bounds'],
        'polygon': config.get('polygon'),
        'districts': config['districts'],
        'region': 'tashkent_city',
        'region_name': {
            'en': 'Tashkent City',
            'uz': 'Toshkent shahri',
            'ru': 'Город Ташкент'
        }.get(language, 'Tashkent City')
    })
