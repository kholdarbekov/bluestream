"""
Staff API endpoints for the Water Business Platform.
Handles staff authentication, delivery operations, and operator actions.
"""
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, create_access_token, create_refresh_token
from datetime import timedelta

from business_app.services.staff_service import StaffService
from business_app.utils.error_handlers import handle_api_exception
from business_app.utils.api_responses import success_response, error_response
from business_app.utils.exceptions import ValidationError, NotFoundError, ForbiddenError, ConflictError
from business_app.utils.decorators import validate_json, require_roles
from business_app.utils.constants import UserRole

staff_bp = Blueprint('staff', __name__)


def _address_line(address) -> str:
    """Return a display-safe primary address line across schema variants."""
    if not address:
        return ''
    return (
        getattr(address, 'full_address', None)
        or getattr(address, 'street_address', None)
        or getattr(address, 'address_line_1', None)
        or ''
    )


def _address_label(address) -> str:
    """Return address label/title across schema variants."""
    if not address:
        return 'Address'
    return getattr(address, 'title', None) or getattr(address, 'label', None) or 'Address'


# --- Staff Authentication ---

@staff_bp.route('/auth/login', methods=['POST'])
@handle_api_exception
def staff_login():
    """Staff login: pre-bound telegram_id or one-time invite-token binding."""
    data = request.get_json()
    if not data:
        raise ValidationError("Request body is required")

    telegram_id = data.get('telegram_id')
    invite_token = data.get('invite_token')

    if not telegram_id:
        raise ValidationError("telegram_id is required")

    result = StaffService.authenticate_and_link_staff(
        telegram_id=str(telegram_id),
        invite_token=invite_token,
    )
    return success_response(result, status_code=200)


@staff_bp.route('/auth/refresh', methods=['POST'])
@handle_api_exception
@jwt_required(refresh=True)
def staff_refresh_token():
    """Refresh JWT access token"""
    current_user_id = get_jwt_identity()
    from business_app.models.user import User
    user = User.query.get(current_user_id)

    if not user:
        raise NotFoundError("User not found")

    access_token = create_access_token(
        identity=user.id,
        additional_claims={
            'role': user.role.value if hasattr(user.role, 'value') else user.role,
            'staff_roles': user.staff_roles or [],
        },
        expires_delta=timedelta(hours=24)
    )

    return success_response({
        'access_token': access_token,
        'expires_in': 86400,
    })


# --- Delivery Operations ---

@staff_bp.route('/delivery/pool', methods=['GET'])
@handle_api_exception
@jwt_required()
def get_order_pool():
    """Get unassigned orders available for pickup"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    order_id = request.args.get('order_id', type=int)
    delivery_id = request.args.get('delivery_id', type=int)
    include_assigned = request.args.get('include_assigned', 'false').lower() == 'true'

    pool = StaffService.get_delivery_pool({
        'page': page,
        'per_page': per_page,
        'order_id': order_id,
        'delivery_id': delivery_id,
        'include_assigned': include_assigned,
    })

    items = []
    for delivery in pool.get('items', []):
        order = delivery.order
        address = order.delivery_address if order else None
        assignee = delivery.delivery_person if delivery else None

        order_items = []
        if order and order.order_items:
            for oi in order.order_items:
                order_items.append({
                    'product_name': oi.product.name if oi.product else 'Unknown',
                    'quantity': oi.quantity,
                    'unit_price': float(oi.unit_price) if oi.unit_price else 0,
                    'total_price': float(oi.total_price) if oi.total_price else 0,
                })

        order_status = order.status.value if order and hasattr(order.status, 'value') else (order.status if order else None)
        delivery_status = (
            delivery.status.value
            if delivery and hasattr(delivery.status, 'value')
            else (delivery.status if delivery else None)
        )

        items.append({
            'delivery_id': delivery.id,
            'order_id': order.id if order else None,
            'order_number': order.order_number if order else 'N/A',
            'status': order_status,
            'delivery_status': delivery_status,
            'customer_name': f"{order.user.first_name} {order.user.last_name or ''}".strip() if order and order.user else '',
            'customer_phone': order.user.phone if order and order.user else '',
            'district': address.district if address else '',
            'address': _address_line(address),
            'total_amount': float(order.total_amount) if order and order.total_amount else 0,
            'payment_method': order.payment_method.value if order and order.payment_method else 'cash',
            'item_count': len(order.order_items) if order and order.order_items else 0,
            'items': order_items,
            'delivery_notes': order.delivery_notes or '',
            'time_slot': order.delivery_time_slot if order else '',
            'created_at': order.created_at.isoformat() if order and order.created_at else None,
            'delivery_person_id': delivery.delivery_person_id,
            'delivery_person_name': assignee.full_name if assignee else '',
        })

    return success_response({
        'items': items,
        'pagination': pool.get('pagination', {
            'page': page,
            'per_page': per_page,
            'total': len(items),
            'pages': 1,
        }),
    })


@staff_bp.route('/delivery/accept/<int:delivery_id>', methods=['POST'])
@handle_api_exception
@jwt_required()
def accept_order(delivery_id):
    """Accept/pick an order from the pool (with row locking)"""
    current_user_id = get_jwt_identity()
    delivery = StaffService.accept_order(delivery_id, current_user_id)

    return success_response({
        'delivery_id': delivery.id,
        'status': delivery.status.value if hasattr(delivery.status, 'value') else delivery.status,
        'message': 'Order accepted successfully',
    })


@staff_bp.route('/delivery/<int:delivery_id>/status', methods=['PUT'])
@handle_api_exception
@jwt_required()
def update_delivery_status(delivery_id):
    """Update delivery status with validation"""
    current_user_id = get_jwt_identity()
    data = request.get_json()

    if not data or 'status' not in data:
        raise ValidationError("status field is required")

    metadata = data.get('metadata', {})
    delivery = StaffService.update_delivery_status(
        delivery_id, data['status'], current_user_id, metadata
    )

    return success_response({
        'delivery_id': delivery.id,
        'status': delivery.status.value if hasattr(delivery.status, 'value') else delivery.status,
        'message': 'Status updated successfully',
    })


@staff_bp.route('/delivery/<int:delivery_id>/location', methods=['POST'])
@handle_api_exception
@jwt_required()
def update_location(delivery_id):
    """Update delivery person's live location"""
    data = request.get_json()
    if not data:
        raise ValidationError("Request body is required")

    lat = data.get('latitude')
    lng = data.get('longitude')

    if lat is None or lng is None:
        raise ValidationError("latitude and longitude are required")

    delivery = StaffService.update_delivery_location(delivery_id, lat, lng)

    return success_response({'message': 'Location updated'})


@staff_bp.route('/delivery/active', methods=['GET'])
@handle_api_exception
@jwt_required()
def get_active_deliveries():
    """Get my active deliveries"""
    current_user_id = get_jwt_identity()
    deliveries = StaffService.get_active_deliveries(current_user_id)

    items = []
    for delivery in deliveries:
        order = delivery.order
        address = order.delivery_address if order else None

        item_list = []
        if order and order.order_items:
            for oi in order.order_items:
                item_list.append({
                    'product_name': oi.product.name if oi.product else 'Unknown',
                    'quantity': oi.quantity,
                    'unit_price': float(oi.unit_price) if oi.unit_price else 0,
                })

        items.append({
            'delivery_id': delivery.id,
            'order_number': order.order_number if order else 'N/A',
            'status': delivery.status.value if hasattr(delivery.status, 'value') else delivery.status,
            'customer_name': f"{order.user.first_name} {order.user.last_name or ''}".strip() if order and order.user else '',
            'customer_phone': order.user.phone if order and order.user else '',
            'address': _address_line(address),
            'district': address.district if address else '',
            'total_amount': float(order.total_amount) if order and order.total_amount else 0,
            'payment_method': order.payment_method.value if order and order.payment_method else 'cash',
            'items': item_list,
            'delivery_notes': order.delivery_notes or '',
            # Destination coordinates (order address)
            'destination_latitude': address.latitude if address else None,
            'destination_longitude': address.longitude if address else None,
            # Driver's last known delivery coordinates (origin candidate)
            'current_location_lat': delivery.current_location_lat,
            'current_location_lng': delivery.current_location_lng,
        })

    return success_response({'items': items, 'total': len(items)})


@staff_bp.route('/delivery/history', methods=['GET'])
@handle_api_exception
@jwt_required()
def get_delivery_history():
    """Get my delivery history"""
    current_user_id = get_jwt_identity()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)

    result = StaffService.get_delivery_history(current_user_id, page, per_page)

    items = []
    for delivery in result['items']:
        order = delivery.order
        items.append({
            'delivery_id': delivery.id,
            'order_number': order.order_number if order else 'N/A',
            'status': delivery.status.value if hasattr(delivery.status, 'value') else delivery.status,
            'customer_name': f"{order.user.first_name} {order.user.last_name or ''}".strip() if order and order.user else '',
            'total_amount': float(order.total_amount) if order and order.total_amount else 0,
            'delivered_at': delivery.delivered_at.isoformat() if delivery.delivered_at else None,
            'cash_collected': float(delivery.cash_collected) if delivery.cash_collected else None,
        })

    return success_response({
        'items': items,
        'pagination': result['pagination'],
    })


@staff_bp.route('/delivery/stats', methods=['GET'])
@handle_api_exception
@jwt_required()
def get_delivery_stats():
    """Get my delivery performance stats"""
    current_user_id = get_jwt_identity()
    period = request.args.get('period', 'month')

    stats = StaffService.get_delivery_stats(current_user_id, period)
    return success_response(stats)


# --- Operator Operations ---

@staff_bp.route('/operator/users', methods=['POST'])
@handle_api_exception
@jwt_required()
def create_client_user():
    """Create a new client user (operator)"""
    current_user_id = get_jwt_identity()
    data = request.get_json()

    if not data:
        raise ValidationError("Request body is required")

    user = StaffService.create_client_user(current_user_id, data)

    return success_response({
        'id': user.id,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'phone': user.phone,
    }, status_code=201)


@staff_bp.route('/operator/users/search', methods=['GET'])
@handle_api_exception
@jwt_required()
def search_clients():
    """Search for client users"""
    query = request.args.get('q', '')
    search_type = request.args.get('type', 'phone')

    users = StaffService.search_users(query, search_type)

    items = []
    for user in users:
        items.append({
            'id': user.id,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'phone': user.phone,
            'address_count': len(user.addresses) if hasattr(user, 'addresses') and user.addresses else 0,
        })

    return success_response({'items': items, 'total': len(items)})


@staff_bp.route('/operator/orders', methods=['POST'])
@handle_api_exception
@jwt_required()
def create_order_for_client():
    """Create order on behalf of client (phone order)"""
    current_user_id = get_jwt_identity()
    data = request.get_json()

    if not data:
        raise ValidationError("Request body is required")

    client_id = data.get('client_id')
    if not client_id:
        raise ValidationError("client_id is required")

    order = StaffService.create_phone_order(current_user_id, client_id, data)

    return success_response({
        'id': order.id,
        'order_number': order.order_number,
        'status': order.status.value if hasattr(order.status, 'value') else order.status,
        'total_amount': float(order.total_amount) if order.total_amount else 0,
    }, status_code=201)


@staff_bp.route('/operator/orders/recent', methods=['GET'])
@handle_api_exception
@jwt_required()
def get_recent_operator_orders():
    """Get recent orders created by this operator"""
    current_user_id = get_jwt_identity()

    from business_app.models.order import Order
    orders = Order.query.filter_by(
        created_by_staff_id=current_user_id
    ).order_by(Order.created_at.desc()).limit(20).all()

    items = []
    for order in orders:
        items.append({
            'id': order.id,
            'order_number': order.order_number,
            'status': order.status.value if hasattr(order.status, 'value') else order.status,
            'total_amount': float(order.total_amount) if order.total_amount else 0,
            'customer_name': f"{order.user.first_name} {order.user.last_name or ''}".strip() if order.user else '',
            'created_at': order.created_at.isoformat() if order.created_at else None,
        })

    return success_response({'items': items, 'total': len(items)})


@staff_bp.route('/operator/users/<int:user_id>/addresses', methods=['POST'])
@handle_api_exception
@jwt_required()
def add_client_address(user_id):
    """Add address for a client"""
    data = request.get_json()
    if not data:
        raise ValidationError("Request body is required")

    from business_app.models.user import User, UserAddress
    from business_app import db

    user = User.query.get(user_id)
    if not user:
        raise NotFoundError("User not found")

    address = UserAddress(
        user_id=user_id,
        title=data.get('label', data.get('title', 'Home')),
        full_address=data.get('full_address', data.get('address_line_1', '')),
        street_address=data.get('street_address', data.get('address_line_1')),
        city=data.get('city', 'Tashkent'),
        district=data.get('district'),
        latitude=data.get('latitude'),
        longitude=data.get('longitude'),
        delivery_instructions=data.get('delivery_notes', data.get('delivery_instructions')),
    )
    db.session.add(address)
    db.session.commit()

    return success_response({
        'id': address.id,
        'label': _address_label(address),
        'full_address': _address_line(address),
        'address_line_1': _address_line(address),  # Backward-compatible alias for bot payloads
    }, status_code=201)


@staff_bp.route('/operator/users/<int:user_id>/addresses', methods=['GET'])
@handle_api_exception
@jwt_required()
def get_client_addresses(user_id):
    """Get addresses for a client"""
    from business_app.models.user import User, UserAddress

    user = User.query.get(user_id)
    if not user:
        raise NotFoundError("User not found")

    addresses = UserAddress.query.filter_by(user_id=user_id).all()

    items = []
    for addr in addresses:
        display_address = _address_line(addr)
        items.append({
            'id': addr.id,
            'label': _address_label(addr),
            'full_address': display_address,
            'address_line_1': display_address,  # Backward-compatible alias for bot payloads
            'city': addr.city,
            'district': addr.district,
            'latitude': addr.latitude,
            'longitude': addr.longitude,
        })

    return success_response({'items': items, 'total': len(items)})


# --- Shared Staff Operations ---

@staff_bp.route('/orders/<int:order_id>/preparing', methods=['PUT'])
@handle_api_exception
@jwt_required()
def mark_order_preparing(order_id):
    """Mark order as preparing"""
    current_user_id = get_jwt_identity()
    order = StaffService.mark_order_preparing(order_id, current_user_id)

    return success_response({
        'id': order.id,
        'order_number': order.order_number,
        'status': order.status.value if hasattr(order.status, 'value') else order.status,
        'message': 'Order marked as preparing',
    })
