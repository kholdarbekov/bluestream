from flask import Blueprint, request, jsonify, current_app, g
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import and_, or_, desc, func
from datetime import datetime, UTC, timedelta

from business_app.models.order import Order, OrderItem
from business_app.models.cart import Cart, CartItem
from business_app.models.product import Product
from business_app.models.user import User, UserAddress
from business_app.models.delivery import Delivery, DeliveryTimeSlot
from business_app.models.analytics import PromotionalCampaign
from business_app.utils.service_factory import (
    get_order_service, get_payment_service, get_delivery_service,
    get_notification_service, get_analytics_service, get_cart_service
)
from business_app.utils.helpers import get_current_language
from business_app.utils.translations import get_translation
from business_app.serializers.order_serializers import (
    serialize_order, serialize_order_item, serialize_order_delivery, serialize_order_payment,
    serialize_order_statistics, serialize_cart_estimate, serialize_delivery_slot,
    OrderSchema, OrderItemSchema, CreateOrderRequest, UpdateOrderRequest,
    OrderFeedbackRequest, CartEstimateRequest, DeliverySlotSchema
)
from business_app.utils.decorators import validate_json, validate_order_input, validate_query_params, rate_limit, require_verification
from business_app.utils.constants import OrderStatus, PaymentMethod, DeliveryStatus, UserRole
from business_app.utils.validation_helpers import (
    validate_list_request_params, FilterValidator, PaginationHelper,
    DateValidator, StatusValidator, RequestDataValidator
)
from business_app.utils.error_handlers import handle_api_exception, create_success_response
from business_app.utils.exceptions import ValidationError
from business_app.utils.api_responses import (
    success_response, error_response, paginated_response, created_response,
    not_found_response, validation_error_response, forbidden_response,
    conflict_response, internal_error_response
)
from business_app.tasks.delivery_tasks import auto_assign_delivery_task
from business_app import db
from pydantic import ValidationError as PydanticValidationError

cart_bp = Blueprint('cart', __name__)


@cart_bp.route('/', methods=['GET'])
@jwt_required()
@handle_api_exception
def get_cart():
    """Get current user's cart"""
    user_id = get_jwt_identity()
    cart_service = get_cart_service()
    cart = cart_service.get_cart_by_user_id(user_id)
    if cart:
        cart_data = cart.to_dict()
    else:
        cart_data = None
    return create_success_response(
        data={'cart': cart_data}
    )

@cart_bp.route('/items', methods=['POST'])
@jwt_required()
@handle_api_exception
def add_cart_item():
    """Add item to cart"""
    user_id = get_jwt_identity()
    data = request.get_json()
    
    cart_service = get_cart_service()
    cart = cart_service.add_item_to_cart(
        user_id,
        data.get('product_id'),
        data.get('quantity', 1)
    )
    
    return create_success_response(
        data={'cart': cart.to_dict()}
    )

@cart_bp.route('/items/<int:product_id>', methods=['PUT'])
@jwt_required()
@handle_api_exception
def update_cart_item(product_id):
    """Update item quantity in cart"""
    user_id = get_jwt_identity()
    data = request.get_json()
    
    cart_service = get_cart_service()
    cart = cart_service.update_item_quantity(
        user_id,
        product_id,
        data.get('quantity', 1)
    )
    
    return create_success_response(
        data={'cart': cart.to_dict()}
    )

@cart_bp.route('/items/<int:product_id>', methods=['DELETE'])
@jwt_required()
@handle_api_exception
def remove_cart_item(product_id):
    """Remove item from cart"""
    user_id = get_jwt_identity()
    
    cart_service = get_cart_service()
    cart = cart_service.remove_item_from_cart(
        user_id,
        product_id
    )
    
    return create_success_response(
        data={'cart': cart.to_dict() if cart else None}
    )

@cart_bp.route('/clear', methods=['POST'])
@jwt_required()
@handle_api_exception
def clear_cart():
    """Clear all items from cart"""
    user_id = get_jwt_identity()

    cart_service = get_cart_service()
    cart_service.clear_cart(user_id)

    return create_success_response(
        data={'message': 'Cart cleared successfully'}
    )


@cart_bp.route('/sync', methods=['POST'])
@jwt_required()
@handle_api_exception
def sync_cart():
    """
    Sync localStorage cart to database
    Used when user logs in with items in localStorage
    """
    user_id = get_jwt_identity()
    data = request.get_json()

    local_cart_items = data.get('cart_items', [])

    if not isinstance(local_cart_items, list):
        return validation_error_response(
            errors={'cart_items': 'Cart items must be a list'}
        )

    cart_service = get_cart_service()
    cart = cart_service.sync_cart_from_local(user_id, local_cart_items)

    return create_success_response(
        data={
            'cart': cart.to_dict() if cart else None,
            'message': 'Cart synchronized successfully'
        }
    )


@cart_bp.route('/estimate', methods=['POST'])
@jwt_required()
@handle_api_exception
def get_cart_estimate():
    """
    Get cart price estimate with delivery fees and discounts
    """
    user_id = get_jwt_identity()
    data = request.get_json()

    cart_items = data.get('cart_items', [])
    delivery_address_id = data.get('delivery_address_id')
    delivery_date = data.get('delivery_date')
    delivery_time_slot = data.get('delivery_time_slot')
    loyalty_points_used = data.get('loyalty_points_used', 0)
    promo_code = data.get('promo_code')

    cart_service = get_cart_service()
    estimate = cart_service.calculate_cart_estimate(
        user_id=user_id,
        items=cart_items,
        delivery_address_id=delivery_address_id,
        delivery_date=delivery_date,
        delivery_time_slot=delivery_time_slot,
        loyalty_points_used=loyalty_points_used,
        promo_code=promo_code
    )

    return create_success_response(data={'estimate': estimate})


@cart_bp.route('/validate', methods=['POST'])
@jwt_required()
@handle_api_exception
def validate_cart():
    """
    Validate cart before checkout
    Checks inventory, pricing, and minimum order requirements
    """
    user_id = get_jwt_identity()
    data = request.get_json()

    cart_items = data.get('cart_items', [])

    cart_service = get_cart_service()
    validation_result = cart_service.prepare_cart_for_checkout(
        user_id=user_id,
        items=cart_items
    )

    return create_success_response(
        data={
            'valid': validation_result.get('ready_for_checkout', False),
            'items': [
                {
                    'product_id': item['product_id'],
                    'product_name': item['product'].name,
                    'quantity': item['quantity'],
                    'unit_price': item['unit_price'],
                    'subtotal': item['subtotal']
                }
                for item in validation_result.get('items', [])
            ],
            'subtotal': validation_result.get('subtotal', 0),
            'warnings': validation_result.get('warnings', [])
        }
    )

