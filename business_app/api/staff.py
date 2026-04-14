"""
Staff API endpoints for the Water Business Platform.
Handles staff authentication, delivery operations, and operator actions.
"""
from flask import Blueprint, request, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity

from business_app.services.staff_service import StaffService
from business_app.utils.service_factory import get_corporate_contract_service
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
        cod_projection = StaffService.get_cod_collection_projection(order)

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
            'payment_status': (
                order.payment.status.value
                if order and getattr(order, 'payment', None) and hasattr(order.payment.status, 'value')
                else None
            ),
            'amount_collected': float(order.payment.amount_collected or 0) if order and getattr(order, 'payment', None) else 0,
            'outstanding_amount': float(order.payment.outstanding_amount or 0) if order and getattr(order, 'payment', None) else 0,
            'cod_reserved_prepayment_amount': cod_projection['cod_reserved_prepayment_amount'],
            'expected_cash_to_collect': cod_projection['expected_cash_to_collect'],
            'item_count': len(order.order_items) if order and order.order_items else 0,
            'items': order_items,
            'delivery_notes': order.delivery_notes or '',
            'delivery_instructions': address.delivery_instructions or '' if address else '',
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
        cod_projection = StaffService.get_cod_collection_projection(order)

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
            'order_id': order.id if order else None,
            'customer_id': order.user_id if order else None,
            'order_number': order.order_number if order else None,
            'status': delivery.status.value if hasattr(delivery.status, 'value') else delivery.status,
            'customer_name': f"{order.user.first_name} {order.user.last_name or ''}".strip() if order and order.user else '',
            'customer_phone': order.user.phone if order and order.user else '',
            'address': get_address_line(address),
            'district': address.district if address else '',
            'total_amount': float(order.total_amount) if order and order.total_amount else 0,
            'payment_method': order.payment_method.value if order and order.payment_method else 'cash',
            'payment_status': (
                order.payment.status.value
                if order and getattr(order, 'payment', None) and hasattr(order.payment.status, 'value')
                else None
            ),
            'amount_collected': float(order.payment.amount_collected or 0) if order and getattr(order, 'payment', None) else 0,
            'outstanding_amount': float(order.payment.outstanding_amount or 0) if order and getattr(order, 'payment', None) else 0,
            'cod_reserved_prepayment_amount': cod_projection['cod_reserved_prepayment_amount'],
            'expected_cash_to_collect': cod_projection['expected_cash_to_collect'],
            'items': item_list,
            'delivery_notes': order.delivery_notes or '',
            'delivery_instructions': address.delivery_instructions or '' if address else '',
            # Destination coordinates (order address)
            'destination_latitude': address.latitude if address else None,
            'destination_longitude': address.longitude if address else None,
            # Driver's last known delivery coordinates (origin candidate)
            'current_location_lat': delivery.current_location_lat,
            'current_location_lng': delivery.current_location_lng,
            # Returnable bottle info
            'expected_returnable_bottles': sum(
                float(oi.product.returnable_bottles_per_unit or 0) * (oi.quantity or 0)
                for oi in (order.order_items or [])
                if oi.product and oi.product.tracks_returnable_bottles
            ) if order else 0,
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
        cod_projection = StaffService.get_cod_collection_projection(order)
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
            'payment_status': (
                order.payment.status.value
                if order and getattr(order, 'payment', None) and hasattr(order.payment.status, 'value')
                else None
            ),
            'amount_collected': float(order.payment.amount_collected or 0) if order and getattr(order, 'payment', None) else 0,
            'outstanding_amount': float(order.payment.outstanding_amount or 0) if order and getattr(order, 'payment', None) else 0,
            'cod_reserved_prepayment_amount': cod_projection['cod_reserved_prepayment_amount'],
            'expected_cash_to_collect': cod_projection['expected_cash_to_collect'],
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


@staff_bp.route('/cash-collections', methods=['POST'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver')
def record_cash_collection():
    """Record a standalone COD cash collection."""
    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    customer_id = data.get('customer_id')
    amount = data.get('amount')
    if customer_id is None:
        raise ValidationError("customer_id is required", error_code='STAFF_CUSTOMER_ID_REQUIRED')
    if amount is None:
        raise ValidationError("amount is required", error_code='STAFF_COLLECTION_AMOUNT_REQUIRED')

    from business_app.services.cash_collection_service import CashCollectionService
    from business_app.services.driver_reconciliation_service import DriverReconciliationService

    source = data.get('source')
    if not source:
        source = 'next_delivery' if data.get('delivery_id') else 'standalone_meeting'

    event = CashCollectionService().post_collection(
        customer_id=customer_id,
        amount=amount,
        source=source,
        collector_user_id=current_user_id,
        recorded_by_user_id=current_user_id,
        order_id=data.get('order_id'),
        delivery_id=data.get('delivery_id'),
        notes=data.get('notes'),
        proof_data=data.get('proof_data') or {},
        occurred_at=data.get('occurred_at'),
        manual_allocations=data.get('manual_allocations'),
        allocation_mode=data.get('allocation_mode', 'auto'),
        idempotency_key=data.get('idempotency_key'),
    )

    session_payload = None
    if event.driver_cash_session_id:
        session_payload = DriverReconciliationService().get_session_detail(event.driver_cash_session_id)

    return success_response({
        'cash_collection_event': event.to_dict(),
        'driver_cash_session': session_payload,
    }, status_code=201)


@staff_bp.route('/reconciliation/session', methods=['GET'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver')
def get_reconciliation_session():
    """Get the driver's open reconciliation session for a business date."""
    current_user_id = get_jwt_identity()
    business_date = request.args.get('business_date')

    from business_app.services.driver_reconciliation_service import DriverReconciliationService

    session = DriverReconciliationService().get_open_session_for_driver(
        current_user_id,
        business_date=business_date,
    )
    payload = DriverReconciliationService().get_session_detail(session.id)
    return success_response(payload)


@staff_bp.route('/reconciliation/session/submit', methods=['POST'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver')
def submit_reconciliation_session():
    """Submit end-of-day driver reconciliation."""
    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    from business_app.services.driver_reconciliation_service import DriverReconciliationService

    session = DriverReconciliationService().submit_session(
        driver_user_id=current_user_id,
        declared_cash=data.get('declared_cash'),
        notes=data.get('notes'),
        business_date=data.get('business_date'),
        submitted_by_user_id=current_user_id,
    )
    payload = DriverReconciliationService().get_session_detail(session.id)
    return success_response(payload)


@staff_bp.route('/reconciliation/transfers', methods=['POST'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver')
def create_reconciliation_transfer():
    """Create a checkpoint custody transfer for the driver's reconciliation session."""
    current_user_id = int(get_jwt_identity())
    data = request.get_json() or {}
    declared_transfer_cash = data.get('declared_transfer_cash')
    if declared_transfer_cash is None:
        raise ValidationError(
            "declared_transfer_cash is required",
            error_code='STAFF_DECLARED_TRANSFER_CASH_REQUIRED',
        )

    from business_app.services.driver_reconciliation_service import DriverReconciliationService
    from business_app.services.driver_cash_custody_service import DriverCashCustodyService

    business_date = data.get('business_date')
    session = DriverReconciliationService().get_open_session_for_driver(
        current_user_id,
        business_date=business_date,
    )
    transfer = DriverCashCustodyService().create_transfer(
        session_id=session.id,
        driver_user_id=current_user_id,
        declared_transfer_cash=declared_transfer_cash,
        notes=data.get('notes'),
        transfer_metadata=data.get('transfer_metadata') or {},
    )
    payload = DriverReconciliationService().get_session_detail(session.id)
    payload['created_transfer'] = transfer.to_dict()
    return success_response(payload, status_code=201)


@staff_bp.route('/customers/<int:customer_id>/cod-statement', methods=['GET'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver', 'operator')
def get_staff_customer_cod_statement(customer_id):
    """Get COD receivable statement for a customer in staff workflows."""
    from business_app.services.cash_collection_service import CashCollectionService

    statement = CashCollectionService().get_customer_cod_statement(customer_id)
    return success_response(statement)


@staff_bp.route('/customers/search', methods=['GET'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver', 'operator')
def search_customers_for_cod_collection():
    """Search customers for COD collection workflows."""
    query = request.args.get('q', '')
    search_type = request.args.get('type', 'phone')
    only_with_open_cod = request.args.get('only_with_open_cod', 'true').lower() != 'false'

    items = StaffService.search_customers_for_cod_collection(
        query,
        search_type,
        only_with_open_cod=only_with_open_cod,
    )
    return success_response({'items': items, 'total': len(items)})


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
            'city': addr.city,
            'district': addr.district,
            'latitude': addr.latitude,
            'longitude': addr.longitude,
        })

    return success_response({'items': items, 'total': len(items)})


@staff_bp.route('/operator/users/<int:user_id>/payment-methods', methods=['GET'])
@handle_api_exception
@jwt_required()
@require_staff_roles('operator')
def get_client_payment_methods(user_id):
    """Get debt-aware payment methods for an operator-created client order."""
    return success_response(StaffService.get_client_payment_methods(user_id))


@staff_bp.route('/operator/users/<int:user_id>/corporate-balance', methods=['GET'])
@handle_api_exception
@jwt_required()
@require_staff_roles('operator')
def get_client_corporate_balance(user_id):
    """Get active corporate contract balances for a client user."""
    service = get_corporate_contract_service()
    contract_balances = service.get_active_contract_balances_for_user(user_id)

    if not contract_balances:
        return success_response({
            'user_id': user_id,
            'has_active_contracts': False,
            'contracts': [],
        })

    return success_response({
        'user_id': user_id,
        'has_active_contracts': True,
        'contracts': contract_balances,
    })


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


# --- Bottle Tracking ---

@staff_bp.route('/bottles/customer/<int:user_id>/summary', methods=['GET'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver', 'operator')
def get_customer_bottle_summary(user_id):
    """Get customer's bottle summary (balances across addresses)."""
    from business_app.services.bottle_tracking_service import BottleTrackingService
    service = BottleTrackingService()
    summary = service.get_customer_summary(user_id)
    return success_response(summary)


@staff_bp.route('/bottles/customer/<int:user_id>/addresses', methods=['GET'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver', 'operator')
def get_customer_bottle_addresses(user_id):
    """Get customer addresses with bottle balances."""
    from business_app.services.bottle_tracking_service import BottleTrackingService
    service = BottleTrackingService()
    balances = service.get_customer_balances(user_id)
    return success_response([
        {
            'address_id': b.address_id,
            'address_title': b.address.title if b.address else None,
            'full_address': b.address.full_address if b.address else None,
            'balance': float(b.balance or 0),
            'bottle_balance_id': b.id,
        }
        for b in balances
    ])


@staff_bp.route('/bottles/collection', methods=['POST'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver', 'operator')
def record_bottle_collection():
    """Record standalone bottle collection by driver."""
    from business_app import db
    from business_app.services.bottle_tracking_service import BottleTrackingService

    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    customer_id = data.get('customer_id')
    address_id = data.get('address_id')
    quantity = data.get('quantity')
    notes = data.get('notes')

    if not customer_id or not address_id or not quantity:
        raise ValidationError("customer_id, address_id, and quantity are required")

    service = BottleTrackingService()
    entry = service.record_standalone_collection(
        user_id=customer_id,
        address_id=address_id,
        quantity=quantity,
        actor_user_id=current_user_id,
        notes=notes,
    )
    db.session.commit()

    balance = service.get_balance(customer_id, address_id)
    return success_response({
        'ledger_entry_id': entry.id,
        'quantity_collected': float(abs(entry.quantity)),
        'remaining_balance': float(balance.balance) if balance else 0,
    })


@staff_bp.route('/bottles/fine', methods=['POST'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver', 'operator')
def create_bottle_fine_staff():
    """Driver/operator creates a manual fine for missing bottles."""
    from business_app import db
    from business_app.services.bottle_tracking_service import BottleTrackingService

    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    customer_id = data.get('customer_id')
    bottle_balance_id = data.get('bottle_balance_id')
    quantity = data.get('quantity')
    fine_amount = data.get('fine_amount')
    notes = data.get('notes')

    if not all([customer_id, bottle_balance_id, quantity, fine_amount]):
        raise ValidationError("customer_id, bottle_balance_id, quantity, and fine_amount are required")

    service = BottleTrackingService()
    fine = service.issue_fine(
        user_id=customer_id,
        bottle_balance_id=bottle_balance_id,
        quantity=quantity,
        fine_amount=fine_amount,
        actor_user_id=current_user_id,
        notes=notes,
    )
    db.session.commit()
    return success_response(fine.to_dict())


@staff_bp.route('/bottles/load', methods=['POST'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver')
def record_bottles_loaded():
    """[DEPRECATED] Shim — delegates to session open endpoint for backward compatibility."""
    from business_app import db
    from business_app.serializers.bottle_serializers import (
        DriverBottleSessionOpenRequest,
        serialize_bottle_session,
    )
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from pydantic import ValidationError as PydanticValidationError
    from business_app.utils.api_responses import validation_error_response

    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    try:
        payload = DriverBottleSessionOpenRequest(**data)
    except PydanticValidationError as exc:
        return validation_error_response(exc.errors())

    service = BottleTrackingService()
    session = service.open_bottle_session(
        current_user_id,
        payload.bottles_loaded,
        actor_user_id=current_user_id,
        notes=payload.notes,
    )
    db.session.commit()
    return success_response(serialize_bottle_session(session))


@staff_bp.route('/bottles/return-to-warehouse', methods=['POST'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver')
def record_bottles_returned_to_warehouse():
    """[DEPRECATED] Shim — delegates to session close endpoint for backward compatibility."""
    from business_app import db
    from business_app.serializers.bottle_serializers import (
        DriverBottleSessionCloseRequest,
        serialize_bottle_session,
    )
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from pydantic import ValidationError as PydanticValidationError
    from business_app.utils.api_responses import validation_error_response

    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    try:
        payload = DriverBottleSessionCloseRequest(**data)
    except PydanticValidationError as exc:
        return validation_error_response(exc.errors())

    service = BottleTrackingService()
    session = service.close_bottle_session(
        current_user_id,
        payload.bottles_returned_to_warehouse,
        actor_user_id=current_user_id,
        notes=payload.notes,
    )
    db.session.commit()
    return success_response(serialize_bottle_session(session))


@staff_bp.route('/bottles/my-accountability', methods=['GET'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver')
def get_my_bottle_accountability():
    """Get driver's current open session or most recent closed session."""
    from business_app.serializers.bottle_serializers import serialize_bottle_session
    from business_app.services.bottle_tracking_service import BottleTrackingService

    current_user_id = get_jwt_identity()
    service = BottleTrackingService()

    # Return open session first; fall back to most recent closed one
    open_session = service.get_open_session(current_user_id)
    if open_session:
        return success_response(serialize_bottle_session(open_session))

    result = service.get_driver_sessions(current_user_id, page=1, per_page=1)
    items = result.get("items", [])
    return success_response(serialize_bottle_session(items[0]) if items else {})


# --- Session endpoints ---

@staff_bp.route('/bottles/session/open', methods=['POST'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver')
def open_bottle_session():
    """Driver opens a new trip session by loading bottles from the warehouse."""
    from business_app import db
    from business_app.serializers.bottle_serializers import (
        DriverBottleSessionOpenRequest,
        serialize_bottle_session,
    )
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from pydantic import ValidationError as PydanticValidationError
    from business_app.utils.api_responses import validation_error_response

    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    try:
        payload = DriverBottleSessionOpenRequest(**data)
    except PydanticValidationError as exc:
        return validation_error_response(exc.errors())

    service = BottleTrackingService()
    session = service.open_bottle_session(
        current_user_id,
        payload.bottles_loaded,
        actor_user_id=current_user_id,
        notes=payload.notes,
    )
    db.session.commit()
    return success_response(serialize_bottle_session(session), status_code=201)


@staff_bp.route('/bottles/session/current', methods=['GET'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver')
def get_current_bottle_session():
    """Get driver's current open session, or null if none."""
    from business_app.serializers.bottle_serializers import serialize_bottle_session
    from business_app.services.bottle_tracking_service import BottleTrackingService

    current_user_id = get_jwt_identity()
    service = BottleTrackingService()
    session = service.get_open_session(current_user_id)
    return success_response(serialize_bottle_session(session) if session else None)


@staff_bp.route('/bottles/session/close', methods=['POST'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver')
def close_bottle_session():
    """Driver closes the active session by returning bottles to the warehouse."""
    from business_app import db
    from business_app.serializers.bottle_serializers import (
        DriverBottleSessionCloseRequest,
        serialize_bottle_session,
    )
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from pydantic import ValidationError as PydanticValidationError
    from business_app.utils.api_responses import validation_error_response

    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    try:
        payload = DriverBottleSessionCloseRequest(**data)
    except PydanticValidationError as exc:
        return validation_error_response(exc.errors())

    service = BottleTrackingService()
    session = service.close_bottle_session(
        current_user_id,
        payload.bottles_returned_to_warehouse,
        actor_user_id=current_user_id,
        notes=payload.notes,
    )
    db.session.commit()
    return success_response(serialize_bottle_session(session))


@staff_bp.route('/bottles/sessions', methods=['GET'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver')
def list_my_bottle_sessions():
    """Get paginated session history for the calling driver."""
    from business_app.serializers.bottle_serializers import serialize_bottle_session
    from business_app.services.bottle_tracking_service import BottleTrackingService

    current_user_id = get_jwt_identity()
    service = BottleTrackingService()
    result = service.get_driver_sessions(
        current_user_id,
        page=request.args.get('page', 1, type=int),
        per_page=request.args.get('per_page', 20, type=int),
        status=request.args.get('status'),
    )
    return success_response({
        'items': [serialize_bottle_session(s) for s in result['items']],
        'total': result['total'],
        'page': result['page'],
        'per_page': result['per_page'],
        'pages': result['pages'],
    })


# --- Transfer endpoints ---

@staff_bp.route('/bottles/transfers/pending', methods=['GET'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver')
def get_pending_bottle_transfers():
    """Get transfers pending confirmation by the calling driver (receiver inbox)."""
    from business_app.serializers.bottle_serializers import serialize_bottle_transfer
    from business_app.services.bottle_tracking_service import BottleTrackingService

    current_user_id = get_jwt_identity()
    service = BottleTrackingService()
    transfers = service.get_pending_transfers_for_driver(current_user_id)
    return success_response([serialize_bottle_transfer(t) for t in transfers])


@staff_bp.route('/bottles/transfers', methods=['POST'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver')
def initiate_bottle_transfer():
    """Driver initiates a mid-route bottle transfer to another driver."""
    from business_app import db
    from business_app.serializers.bottle_serializers import (
        DriverBottleTransferCreateRequest,
        serialize_bottle_transfer,
    )
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from pydantic import ValidationError as PydanticValidationError
    from business_app.utils.api_responses import validation_error_response

    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    try:
        payload = DriverBottleTransferCreateRequest(**data)
    except PydanticValidationError as exc:
        return validation_error_response(exc.errors())

    service = BottleTrackingService()
    transfer = service.initiate_bottle_transfer(
        sender_driver_id=current_user_id,
        receiver_driver_id=payload.receiver_driver_id,
        declared_quantity=payload.quantity,
        notes=payload.notes,
    )
    db.session.commit()
    return success_response(serialize_bottle_transfer(transfer), status_code=201)


@staff_bp.route('/bottles/transfers/<int:transfer_id>/confirm', methods=['POST'])
@handle_api_exception
@jwt_required()
@require_staff_roles('delivery_driver')
def confirm_bottle_transfer(transfer_id: int):
    """Receiver confirms (or disputes) a pending bottle transfer."""
    from business_app import db
    from business_app.serializers.bottle_serializers import (
        DriverBottleTransferConfirmRequest,
        serialize_bottle_transfer,
    )
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from pydantic import ValidationError as PydanticValidationError
    from business_app.utils.api_responses import validation_error_response

    current_user_id = get_jwt_identity()
    data = request.get_json() or {}

    try:
        payload = DriverBottleTransferConfirmRequest(**data)
    except PydanticValidationError as exc:
        return validation_error_response(exc.errors())

    service = BottleTrackingService()
    transfer = service.confirm_bottle_transfer(
        transfer_id=transfer_id,
        receiver_driver_id=current_user_id,
        confirmed_quantity=payload.confirmed_quantity,
        notes=payload.notes,
    )
    db.session.commit()
    return success_response(serialize_bottle_transfer(transfer))
