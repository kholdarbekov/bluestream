"""
User Addresses API endpoints
Manages user delivery addresses
"""
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import desc

from business_app.models.user import User, UserAddress
from business_app.utils.error_handlers import handle_api_exception, create_success_response
from business_app.utils.exceptions import ValidationError, NotFoundError, ForbiddenError
from business_app.utils.decorators import validate_json, rate_limit
from business_app.utils.api_responses import (
    success_response, error_response, created_response,
    not_found_response, validation_error_response, forbidden_response
)
from business_app.utils.translations import get_translation
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
@validate_json(['address_line1', 'city'])
@handle_api_exception
def create_address():
    """Create new address"""
    user_id = get_jwt_identity()
    data = request.get_json()

    # Validate required fields
    if not data.get('address_line1') or not data.get('city'):
        return validation_error_response(
            errors={'address': 'Address line 1 and city are required'}
        )

    # Check if this should be default
    is_default = data.get('is_default', False)

    # If setting as default, unset other defaults
    if is_default:
        UserAddress.query.filter_by(user_id=user_id, is_default=True).update(
            {'is_default': False}
        )

    # Create address
    address = UserAddress(
        user_id=user_id,
        label=data.get('label'),
        address_line1=data.get('address_line1'),
        address_line2=data.get('address_line2'),
        city=data.get('city'),
        state=data.get('state'),
        postal_code=data.get('postal_code'),
        country=data.get('country', 'Uzbekistan'),
        phone_number=data.get('phone_number'),
        is_default=is_default,
        delivery_instructions=data.get('delivery_instructions')
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

    # Update fields
    if 'label' in data:
        address.label = data['label']
    if 'address_line1' in data:
        address.address_line1 = data['address_line1']
    if 'address_line2' in data:
        address.address_line2 = data['address_line2']
    if 'city' in data:
        address.city = data['city']
    if 'state' in data:
        address.state = data['state']
    if 'postal_code' in data:
        address.postal_code = data['postal_code']
    if 'country' in data:
        address.country = data['country']
    if 'phone_number' in data:
        address.phone_number = data['phone_number']
    if 'delivery_instructions' in data:
        address.delivery_instructions = data['delivery_instructions']

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
