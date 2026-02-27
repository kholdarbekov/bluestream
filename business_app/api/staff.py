"""
Staff API endpoints for the Water Business Platform.
Handles staff authentication, delivery operations, and operator actions.
"""
from flask import Blueprint, request, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity

from business_app.services.staff_service import StaffService
from business_app.utils.address_helpers import get_address_label, get_address_line
from business_app.utils.decorators import require_staff_roles
from business_app.utils.error_handlers import handle_api_exception
from business_app.utils.api_responses import success_response
from business_app.utils.exceptions import ValidationError

staff_bp = Blueprint('staff', __name__)

# --- Staff Authentication ---

@staff_bp.route('/auth/login', methods=['POST'])
@handle_api_exception
def staff_login():
    """Staff login: pre-bound telegram_id or one-time invite-token binding."""
    data = request.get_json()
    if not data:
        raise ValidationError("Request body is required", error_code='STAFF_REQUEST_BODY_REQUIRED')

    telegram_id = data.get('telegram_id')
    invite_token = data.get('invite_token')

    if not telegram_id:
        raise ValidationError("telegram_id is required", error_code='STAFF_TELEGRAM_ID_REQUIRED')

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
    auth_header = request.headers.get('Authorization', '')
    refresh_token = None
    if auth_header.startswith('Bearer '):
        refresh_token = auth_header[7:].strip()

    if not refresh_token:
        refresh_cookie_name = current_app.config.get('JWT_REFRESH_COOKIE_NAME', 'refresh_token_cookie')
        refresh_token = request.cookies.get(refresh_cookie_name)

    if not refresh_token:
        raise ValidationError("Refresh token is required", error_code='STAFF_REFRESH_TOKEN_REQUIRED')

    from business_app.services.token_service import TokenService
    token_service = TokenService()
    refreshed = token_service.refresh_access_token(refresh_token)

    return success_response({
        'access_token': refreshed['access_token'],
        'expires_in': refreshed.get('expires_in', 3600),
    })


# --- Delivery Operations ---

@staff_bp.route('/delivery/pool', methods=['GET'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver', 'operator')
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
                    'product_name': oi.product.name if oi.product else '',
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
            'order_number': order.order_number if order else None,
            'status': order_status,
            'delivery_status': delivery_status,
            'customer_name': f"{order.user.first_name} {order.user.last_name or ''}".strip() if order and order.user else '',
            'customer_phone': order.user.phone if order and order.user else '',
            'district': address.district if address else '',
            'address': get_address_line(address),
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
@require_staff_roles('delivery_driver')
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
@require_staff_roles('delivery_driver')
def update_delivery_status(delivery_id):
    """Update delivery status with validation"""
    current_user_id = get_jwt_identity()
    data = request.get_json()

    if not data or 'status' not in data:
        raise ValidationError("status field is required", error_code='STAFF_STATUS_REQUIRED')

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
@require_staff_roles('delivery_driver')
def update_location(delivery_id):
    """Update delivery person's live location"""
    data = request.get_json()
    if not data:
        raise ValidationError("Request body is required", error_code='STAFF_REQUEST_BODY_REQUIRED')

    lat = data.get('latitude')
    lng = data.get('longitude')

    if lat is None or lng is None:
        raise ValidationError("latitude and longitude are required", error_code='STAFF_COORDINATES_REQUIRED')

    delivery = StaffService.update_delivery_location(delivery_id, lat, lng)

    return success_response({'message': 'Location updated'})


@staff_bp.route('/delivery/active', methods=['GET'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver')
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
                    'product_name': oi.product.name if oi.product else '',
                    'quantity': oi.quantity,
                    'unit_price': float(oi.unit_price) if oi.unit_price else 0,
                })

        items.append({
            'delivery_id': delivery.id,
            'order_number': order.order_number if order else None,
            'status': delivery.status.value if hasattr(delivery.status, 'value') else delivery.status,
            'customer_name': f"{order.user.first_name} {order.user.last_name or ''}".strip() if order and order.user else '',
            'customer_phone': order.user.phone if order and order.user else '',
            'address': get_address_line(address),
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
@require_staff_roles('delivery_driver')
def get_delivery_history():
    """Get my delivery history"""
    current_user_id = get_jwt_identity()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)

    result = StaffService.get_delivery_history(current_user_id, page, per_page)

    items = []
    for delivery in result['items']:
        order = delivery.order
        address = order.delivery_address if order else None
        items.append({
            'delivery_id': delivery.id,
            'order_number': order.order_number if order else None,
            'status': delivery.status.value if hasattr(delivery.status, 'value') else delivery.status,
            'customer_name': f"{order.user.first_name} {order.user.last_name or ''}".strip() if order and order.user else '',
            'total_amount': float(order.total_amount) if order and order.total_amount else 0,
            'district': address.district if address else '',
            'delivered_at': delivery.delivered_at.isoformat() if delivery.delivered_at else None,
            'updated_at': delivery.updated_at.isoformat() if delivery.updated_at else None,
            'cash_collected': float(delivery.cash_collected) if delivery.cash_collected else None,
        })

    return success_response({
        'items': items,
        'pagination': result['pagination'],
    })


@staff_bp.route('/delivery/stats', methods=['GET'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver')
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
@require_staff_roles('operator')
def create_client_user():
    """Create a new client user (operator)"""
    current_user_id = get_jwt_identity()
    data = request.get_json()

    if not data:
        raise ValidationError("Request body is required", error_code='STAFF_REQUEST_BODY_REQUIRED')

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
@require_staff_roles('operator')
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
            'order_count': len(user.orders) if hasattr(user, 'orders') and user.orders else 0,
        })

    return success_response({'items': items, 'total': len(items)})


@staff_bp.route('/operator/orders', methods=['POST'])
@handle_api_exception
@jwt_required()
@require_staff_roles('operator')
def create_order_for_client():
    """Create order on behalf of client (phone order)"""
    current_user_id = get_jwt_identity()
    data = request.get_json()

    if not data:
        raise ValidationError("Request body is required", error_code='STAFF_REQUEST_BODY_REQUIRED')

    client_id = data.get('client_id')
    if not client_id:
        raise ValidationError("client_id is required", error_code='STAFF_CLIENT_ID_REQUIRED')

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
@require_staff_roles('operator')
def get_recent_operator_orders():
    """Get recent orders created by this operator"""
    current_user_id = get_jwt_identity()
    orders = StaffService.get_recent_operator_orders(current_user_id, limit=20)

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
@require_staff_roles('operator')
def add_client_address(user_id):
    """Add address for a client"""
    data = request.get_json()
    if not data:
        raise ValidationError("Request body is required", error_code='STAFF_REQUEST_BODY_REQUIRED')
    address = StaffService.add_client_address(user_id, data)

    return success_response({
        'id': address.id,
        'label': get_address_label(address),
        'full_address': get_address_line(address),
        'address_line_1': get_address_line(address),  # Backward-compatible alias for bot payloads
    }, status_code=201)


@staff_bp.route('/operator/users/<int:user_id>/addresses', methods=['GET'])
@handle_api_exception
@jwt_required()
@require_staff_roles('operator')
def get_client_addresses(user_id):
    """Get addresses for a client"""
    addresses = StaffService.get_client_addresses(user_id)

    items = []
    for addr in addresses:
        display_address = get_address_line(addr)
        items.append({
            'id': addr.id,
            'label': get_address_label(addr),
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
@require_staff_roles('delivery_driver', 'operator')
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
