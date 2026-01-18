"""
Orders API endpoints
This file should be placed in business_app/api/orders.py
"""
from flask import Blueprint, request, jsonify, current_app, g
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import and_, or_, desc, func
from datetime import datetime, UTC, timedelta

from business_app.models.order import Order, OrderItem
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

orders_bp = Blueprint('orders', __name__)


@orders_bp.route('/', methods=['GET'])
@jwt_required()
@handle_api_exception
def get_orders():
    """Get user orders with pagination and filtering"""
    # Validate request parameters using centralized validation
    params = validate_list_request_params(
        default_per_page=20,
        max_per_page=50,
        allow_status_filter=True,
        status_enum=OrderStatus,
        allow_date_filter=True,
        allow_future_dates=True
    )
    
    # Build query
    query = Order.query.filter_by(user_id=params['user_id'])
    
    # Apply filters using centralized filter builders
    query = FilterValidator.build_status_filter_query(
        query, Order.status, params.get('status')
    )
    
    query = FilterValidator.build_date_filter_query(
        query, Order.created_at, params.get('start_date'), params.get('end_date')
    )
    
    # Order by creation date (newest first)
    query = query.order_by(Order.created_at.desc())
    
    # Paginate
    pagination = query.paginate(
        page=params['page'], per_page=params['per_page'], error_out=False
    )
    
    # Build standardized pagination response with order items included
    response_data = PaginationHelper.build_pagination_response(
        pagination.items, pagination, lambda order: serialize_order(order, include_items=True)
    )
    
    return create_success_response(
        data={'orders': response_data['items'], 'pagination': response_data['pagination']},
        message=get_translation('api.orders.list_retrieved')
    )


@orders_bp.route('/<int:order_id>', methods=['GET'])
@jwt_required()
def get_order(order_id):
    """Get specific order details"""
    try:
        current_user_id = get_jwt_identity()

        order = Order.query.filter_by(
            id=order_id,
            user_id=current_user_id
        ).first()

        if not order:
            return not_found_response(message=get_translation('api.orders.not_found'))

        # Get delivery information
        delivery_info = None
        if order.delivery:
            delivery_info = serialize_order_delivery(order.delivery)

        # Get order timeline
        timeline = get_order_service().get_order_timeline(order_id)

        return success_response(
            data={
                'order': serialize_order(order, include_items=True, include_delivery=True),
                'delivery': delivery_info,
                'timeline': timeline
            },
            message=get_translation('api.orders.retrieved')
        )

    except Exception as e:
        current_app.logger.error(f"Get order error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/emergency', methods=['POST'])
@jwt_required()
@rate_limit(max_requests=3, window_seconds=3600, per='user')  # 3 emergency orders per hour per user
def create_emergency_order():
    """Create an emergency order with priority delivery"""
    try:
        current_user_id = get_jwt_identity()

        # Validate request with Pydantic
        try:
            request_data = CreateOrderRequest(**request.get_json())
        except PydanticValidationError as e:
            return validation_error_response(e.errors())

        user = User.query.get(current_user_id)
        if not user:
            return not_found_response(message=get_translation('error.not_found'))

        # Authorization check: Emergency orders are only available to premium users
        # or users with special authorization
        if not user.is_premium and user.role not in [UserRole.ADMIN, UserRole.MANAGER, UserRole.OPERATOR]:
            current_app.logger.warning(
                f"Unauthorized emergency order attempt by user {user.id} ({user.email}). "
                f"User role: {user.role}, is_premium: {user.is_premium}"
            )
            return forbidden_response(
                message=get_translation('error.forbidden')
            )
        
        # Additional rate limiting for emergency orders - max 3 per day per user
        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        today_emergency_orders = Order.query.filter(
            Order.user_id == current_user_id,
            Order.is_urgent == True,
            Order.created_at >= today_start
        ).count()
        
        if today_emergency_orders >= 3:
            current_app.logger.warning(
                f"Emergency order rate limit exceeded by user {user.id} ({user.email}). "
                f"Orders today: {today_emergency_orders}"
            )
            return error_response(
                message=get_translation('error.forbidden'),
                status_code=429
            )

        # Emergency orders have additional fee
        emergency_fee = 10000  # 10,000 UZS emergency fee

        # Create order with emergency flag
        order_data = {
            'user_id': current_user_id,
            'delivery_address_id': request_data.delivery_address_id,
            'delivery_notes': request_data.delivery_notes,
            'is_urgent': True,
            'payment_method': request_data.payment_method,
            'order_source': request_data.source,
            'emergency_fee': emergency_fee
        }

        # Set delivery for within 2 hours
        emergency_delivery_time = datetime.now(UTC) + timedelta(hours=2)
        order_data['delivery_date'] = emergency_delivery_time.date()
        order_data['delivery_time_slot'] = 'emergency'

        order = get_order_service().create_order(order_data, request_data.items)

        # Create priority delivery
        delivery = get_delivery_service().create_emergency_delivery(order)

        # Immediate driver assignment
        auto_assign_delivery_task.apply_async(args=[delivery.id], countdown=30)

        # Send emergency notifications
        get_notification_service().send_notification(
            user.id,
            'emergency_order_created',
            template_data={
                'order_number': order.order_number,
                'estimated_delivery': emergency_delivery_time.strftime('%H:%M')
            }
        )

        # Notify operations team
        get_notification_service().send_notification(
            None,  # Send to all managers
            'emergency_order_alert',
            template_data={
                'order_number': order.order_number,
                'customer_name': user.full_name,
                'customer_phone': user.phone
            },
            channels=['sms', 'telegram']
        )

        return created_response(
            data={
                'order': serialize_order(order, include_items=True),
                'emergency_fee': emergency_fee,
                'estimated_delivery_time': emergency_delivery_time.isoformat()
            },
            message=get_translation('api.orders.created')
        )

    except PydanticValidationError as e:
        return validation_error_response(e.errors())
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create emergency order error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/quick-reorder', methods=['GET'])
@jwt_required()
def get_quick_reorder_suggestions():
    """Get quick reorder suggestions based on order history"""
    try:
        current_user_id = get_jwt_identity()
        limit = min(int(request.args.get('limit', 5)), 10)
        period_days = int(request.args.get('period_days', 90))

        # Use CartService to get quick reorder suggestions
        cart_service = get_cart_service()
        suggestions = cart_service.get_quick_reorder_suggestions(
            user_id=current_user_id,
            limit=limit,
            period_days=period_days
        )

        return success_response(
            data={'quick_reorder_suggestions': suggestions},
            message=get_translation('success.saved')
        )

    except Exception as e:
        current_app.logger.error(f"Get quick reorder suggestions error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/statistics', methods=['GET'])
@jwt_required()
def get_order_statistics():
    """Get user's order statistics"""
    try:
        current_user_id = get_jwt_identity()
        period = request.args.get('period', 'year')  # month, quarter, year, all
        
        # Calculate date range
        now = datetime.now(UTC)
        if period == 'month':
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == 'quarter':
            quarter_start_month = ((now.month - 1) // 3) * 3 + 1
            start_date = now.replace(month=quarter_start_month, day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == 'year':
            start_date = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:  # all time
            start_date = None
        
        # Base query
        query = Order.query.filter_by(user_id=current_user_id)
        if start_date:
            query = query.filter(Order.created_at >= start_date)
        
        # Calculate statistics
        orders = query.all()
        total_orders = len(orders)
        total_spent = sum(order.total_amount for order in orders)
        
        # Orders by status
        status_counts = {}
        for status in OrderStatus:
            status_counts[status.value] = len([o for o in orders if o.status == status])
        
        # Average order value
        avg_order_value = total_spent / total_orders if total_orders > 0 else 0
        
        # Most ordered products
        from collections import Counter
        product_counter = Counter()
        for order in orders:
            for item in order.order_items:
                product_name = item.product.name if item.product else 'Unknown Product'
                product_counter[product_name] += item.quantity
        
        top_products = [
            {'name': name, 'quantity': qty} 
            for name, qty in product_counter.most_common(5)
        ]
        
        # Monthly spending trend (last 12 months)
        monthly_spending = {}
        for i in range(12):
            month_start = (now.replace(day=1) - timedelta(days=32*i)).replace(day=1)
            month_end = (month_start.replace(month=month_start.month % 12 + 1) 
                        if month_start.month < 12 
                        else month_start.replace(year=month_start.year + 1, month=1))
            
            month_orders = [o for o in orders 
                          if month_start <= o.created_at < month_end]
            month_total = sum(o.total_amount for o in month_orders)
            
            monthly_spending[month_start.strftime('%Y-%m')] = month_total
        
        statistics = {
            'total_orders': total_orders,
            'total_spent': total_spent,
            'average_order_value': round(avg_order_value, 2),
            'orders_by_status': status_counts,
            'top_products': top_products,
            'monthly_spending_trend': monthly_spending
        }

        return success_response(
            data={'period': period, 'statistics': statistics},
            message=get_translation('success.saved')
        )

    except Exception as e:
        current_app.logger.error(f"Get order statistics error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/<int:order_id>/feedback', methods=['POST'])
@jwt_required()
def submit_order_feedback(order_id):
    """Submit feedback for a completed order"""
    try:
        current_user_id = get_jwt_identity()

        # Validate request with Pydantic
        try:
            feedback_data = OrderFeedbackRequest(**request.get_json())
        except PydanticValidationError as e:
            return validation_error_response(e.errors())

        order = Order.query.filter_by(
            id=order_id,
            user_id=current_user_id
        ).first()

        if not order:
            return not_found_response(message=get_translation('api.orders.not_found'))

        if order.status != OrderStatus.DELIVERED:
            return error_response(
                message=get_translation('error.forbidden'),
                status_code=400
            )

        # Update delivery with customer feedback
        if order.delivery:
            order.delivery.customer_rating = feedback_data.rating
            order.delivery.customer_feedback = feedback_data.comment
            db.session.commit()

        # Track feedback for analytics
        get_analytics_service().track_order_feedback(order_id, feedback_data.rating, feedback_data.comment)

        return success_response(message=get_translation('success.saved'))

    except PydanticValidationError as e:
        return validation_error_response(e.errors())
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Submit order feedback error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/', methods=['POST'])
@jwt_required()
@require_verification('phone')
def create_order():
    """Create a new order"""
    try:
        current_user_id = get_jwt_identity()

        # Validate request with Pydantic
        try:
            order_request = CreateOrderRequest(**request.get_json())
        except PydanticValidationError as e:
            return validation_error_response(e.errors())

        user = User.query.get(current_user_id)
        if not user:
            return not_found_response(message=get_translation('error.not_found'))

        # Get user address if delivery_address_id provided
        if order_request.delivery_address_id:
            address: UserAddress = UserAddress.query.filter_by(
                id=order_request.delivery_address_id,
                user_id=current_user_id
            ).first()
            if not address:
                return error_response(
                    message=get_translation('api.addresses.not_found'),
                    status_code=400
                )

        # Create order using service
        order_data = {
            "items": order_request.items,
            "delivery_address": {
                'delivery_address_id': order_request.delivery_address_id,
                "street": address.street_address,
                "longitude": address.longitude,
                "latitude": address.latitude,
            },
            'user_id': current_user_id,
            'delivery_date': order_request.delivery_date,
            'delivery_time_slot': order_request.delivery_time_slot,
            'delivery_notes': order_request.delivery_notes,
            'is_urgent': order_request.is_urgent,
            'payment_method': order_request.payment_method,
            'loyalty_points_used': order_request.loyalty_points_used,
            'promo_code': order_request.promo_code,
            'order_source': order_request.source
        }

        order = get_order_service().create_order(current_user_id, order_data)
        current_app.logger.info(f"CREATE ORDER API: Order created successfully: order={order}")

        # Create delivery record if delivery details provided
        if order.delivery_date and order.delivery_time_slot:
            delivery = get_delivery_service().create_delivery(order)

            # Auto-assign delivery driver
            auto_assign_delivery_task.delay(delivery.id)

        current_app.logger.info(f"CREATE ORDER API: order_number={order.order_number}, type={type(order.order_number)}, total_amount={order.total_amount}, type={type(order.total_amount)}")

        # Create payment record for electronic payment methods (payme, click)
        # Note: For on-site card payments (user enters card on our site), we don't need
        # payment links - the payment is processed via cards.create -> cards.verify -> receipts.pay
        if order.payment_method in [PaymentMethod.PAYME, PaymentMethod.CLICK]:
            from business_app.services.payment_service import PaymentService

            payment_service = PaymentService()

            payment = payment_service.create_payment(
                order_id=order.id,
                payment_method=order.payment_method,
                amount=int(order.total_amount)
            )

            current_app.logger.info(f"Payment record created for order {order.id}: payment_id={payment.id}")

        # Send order confirmation
        # get_notification_service().send_notification(
        #     user.id,
        #     'order_created',
        #     template_data={
        #         'order_number': order.order_number,
        #         'total_amount': float(order.total_amount)
        #     }
        # )
        current_app.logger.info(f"CREATE ORDER API: send_notification finished")

        response_data = {
            'order': serialize_order(order, include_items=True)
        }

        return created_response(
            data=response_data,
            message=get_translation('api.orders.created')
        )

    except PydanticValidationError as e:
        db.session.rollback()
        return validation_error_response(e.errors())
    except ValueError as e:
        db.session.rollback()
        return error_response(message=str(e), status_code=400)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create order error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/<int:order_id>/cancel', methods=['POST'])
@jwt_required()
def cancel_order(order_id):
    """Cancel an order"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json(silent=True) or {}

        order = Order.query.filter_by(
            id=order_id,
            user_id=current_user_id
        ).first()

        if not order:
            return not_found_response(message=get_translation('api.orders.not_found'))

        if not order.can_be_cancelled():
            return error_response(
                message=get_translation('api.orders.cannot_cancel'),
                status_code=400
            )

        # Cancel the order
        get_order_service().cancel_order(order_id, reason=data.get('reason'))

        # Send cancellation notification
        get_notification_service().send_notification(
            current_user_id,
            'order_cancelled',
            template_data={
                'order_number': order.order_number,
                'cancellation_reason': data.get('reason', 'Customer request')
            }
        )

        return success_response(
            data={'order': serialize_order(order)},
            message=get_translation('api.orders.cancelled')
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Cancel order error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/cart/estimate', methods=['POST'])
@jwt_required()
def estimate_cart():
    """Estimate cart total with discounts and delivery fee"""
    try:
        current_user_id = get_jwt_identity()

        # Validate request with Pydantic
        try:
            cart_request = CartEstimateRequest(**request.get_json())
        except PydanticValidationError as e:
            return validation_error_response(e.errors())

        user = User.query.get(current_user_id)
        if not user:
            return not_found_response(message=get_translation('error.not_found'))

        # Use CartService to calculate cart estimate
        cart_service = get_cart_service()
        estimate = cart_service.calculate_cart_estimate(
            user_id=current_user_id,
            items=[item.dict() for item in cart_request.items],
            delivery_address_id=cart_request.delivery_address_id,
            delivery_date=cart_request.delivery_date,
            delivery_time_slot=cart_request.delivery_time_slot,
            loyalty_points_used=cart_request.loyalty_points_used,
            promo_code=cart_request.promo_code
        )

        return success_response(
            data=estimate,
            message=get_translation('success.saved')
        )

    except PydanticValidationError as e:
        return validation_error_response(e.errors())
    except ValidationError as e:
        return validation_error_response(errors=str(e))
    except ValueError as e:
        return error_response(message=str(e), status_code=400)
    except Exception as e:
        current_app.logger.error(f"Estimate cart error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/delivery-slots', methods=['GET'])
@jwt_required()
def get_delivery_slots():
    """Get available delivery time slots"""
    try:
        delivery_date = request.args.get('delivery_date')

        if not delivery_date:
            return error_response(
                message=get_translation('error.validation.required_field'),
                status_code=400
            )

        try:
            target_date = datetime.fromisoformat(delivery_date).date()
        except ValueError:
            return error_response(
                message=get_translation('error.validation.invalid_date'),
                status_code=400
            )

        # Get available time slots
        slots = get_delivery_service().get_available_time_slots(target_date)

        return success_response(
            data={
                'delivery_date': delivery_date,
                'available_slots': [
                    serialize_delivery_slot(slot, target_date) for slot in slots
                ]
            },
            message=get_translation('success.saved')
        )

    except Exception as e:
        current_app.logger.error(f"Get delivery slots error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/promo-code/validate', methods=['POST'])
@jwt_required()
@validate_json(['promo_code'])
def validate_promo_code():
    """Validate promotional code"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        promo_code = data.get('promo_code').upper()
        cart_total = data.get('cart_total', 0)

        # Use CartService to validate promo code
        cart_service = get_cart_service()
        validation_result = cart_service.validate_promo_code(
            promo_code=promo_code,
            user_id=current_user_id,
            cart_total=cart_total
        )

        if not validation_result['valid']:
            return error_response(
                message=validation_result['message'],
                status_code=400
            )

        return success_response(
            data=validation_result,
            message=get_translation('success.saved')
        )

    except ValidationError as e:
        return validation_error_response(errors=str(e))
    except Exception as e:
        current_app.logger.error(f"Validate promo code error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/repeat/<int:order_id>', methods=['POST'])
@jwt_required()
def repeat_order(order_id):
    """Repeat a previous order"""
    try:
        current_user_id = get_jwt_identity()

        original_order = Order.query.filter_by(
            id=order_id,
            user_id=current_user_id
        ).first()

        if not original_order:
            return not_found_response(message=get_translation('api.orders.not_found'))

        # Create new order based on original
        new_order = get_order_service().repeat_order(original_order)

        return created_response(
            data={'order': serialize_order(new_order, include_items=True)},
            message=get_translation('api.orders.created')
        )

    except ValueError as e:
        return error_response(message=str(e), status_code=400)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Repeat order error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/<int:order_id>/track', methods=['GET'])
@jwt_required()
def track_order(order_id):
    """Track order status and delivery"""
    try:
        current_user_id = get_jwt_identity()
        
        order = Order.query.filter_by(
            id=order_id, 
            user_id=current_user_id
        ).first()
        
        if not order:
            return not_found_response(message=get_translation('api.orders.not_found'))

        # Get delivery tracking information
        delivery_info = None
        if order.delivery:
            delivery_info = serialize_order_delivery(order.delivery)

        # Get order timeline
        timeline = get_order_service().get_order_timeline(order_id)

        # Calculate estimated time remaining
        time_remaining = None
        if order.delivery and order.delivery.estimated_delivery_time:
            remaining = order.delivery.estimated_delivery_time - datetime.now(UTC)
            if remaining.total_seconds() > 0:
                time_remaining = {
                    'hours': remaining.seconds // 3600,
                    'minutes': (remaining.seconds % 3600) // 60,
                    'total_minutes': remaining.seconds // 60
                }

        return success_response(
            data={
                'order': {
                    'id': order.id,
                    'order_number': order.order_number,
                    'status': order.status.value,
                    'total_amount': order.total_amount,
                    'created_at': order.created_at.isoformat()
                },
                'delivery': delivery_info,
                'timeline': timeline,
                'estimated_time_remaining': time_remaining
            },
            message=get_translation('api.orders.retrieved')
        )

    except Exception as e:
        current_app.logger.error(f"Track order error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/bulk-action', methods=['POST'])
@jwt_required()
@rate_limit(max_requests=5, window_seconds=300, per='user')  # 5 bulk actions per 5 minutes per user
def bulk_order_action():
    """Perform bulk action on multiple orders"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        user = User.query.get(current_user_id)
        if not user or not user.is_admin:
            return forbidden_response(message=get_translation('error.forbidden'))

        action = data.get('action')
        order_ids = data.get('order_ids')

        if not isinstance(order_ids, list) or len(order_ids) > 100:
            return error_response(
                message=get_translation('error.forbidden'),
                status_code=400
            )

        valid_actions = ['confirm', 'cancel', 'mark_priority', 'assign_delivery']
        if action not in valid_actions:
            return error_response(
                message=get_translation('error.forbidden'),
                status_code=400
            )

        # Process bulk action
        results = get_order_service().perform_bulk_action(action, order_ids, current_user_id)

        return success_response(
            data={'results': results},
            message=get_translation('success.updated')
        )

    except Exception as e:
        current_app.logger.error(f"Bulk order action error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/export', methods=['POST'])
@jwt_required()
@rate_limit(max_requests=3, window_seconds=600, per='user')  # 3 exports per 10 minutes per user
def export_orders():
    """Export orders to CSV/Excel"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        user = User.query.get(current_user_id)
        if not user:
            return not_found_response(message=get_translation('error.not_found'))

        # Regular users can only export their own orders
        if not user.is_admin:
            filters = {'user_id': current_user_id}
        else:
            filters = data.get('filters', {})

        format_type = data.get('format', 'csv')  # csv, excel
        start_date = data.get('start_date')
        end_date = data.get('end_date')

        if format_type not in ['csv', 'excel']:
            return error_response(
                message=get_translation('error.forbidden'),
                status_code=400
            )

        # Generate export
        export_result = get_order_service().export_orders(
            format_type=format_type,
            filters=filters,
            start_date=start_date,
            end_date=end_date,
            user_id=current_user_id
        )

        return success_response(
            data={
                'download_url': export_result['download_url'],
                'file_size': export_result['file_size'],
                'expires_at': export_result['expires_at'].isoformat()
            },
            message=get_translation('success.saved')
        )

    except Exception as e:
        current_app.logger.error(f"Export orders error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/subscription', methods=['POST'])
@jwt_required()
def create_subscription_order():
    """Create a recurring subscription order"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        user = User.query.get(current_user_id)
        if not user:
            return not_found_response(message=get_translation('error.not_found'))

        items_data = data.get('items', [])
        frequency = data.get('frequency')  # weekly, biweekly, monthly

        if frequency not in ['weekly', 'biweekly', 'monthly']:
            return error_response(
                message=get_translation('error.forbidden'),
                status_code=400
            )

        # Create subscription order
        subscription_data = {
            'user_id': current_user_id,
            'delivery_address_id': data.get('delivery_address_id'),
            'frequency': frequency,
            'start_date': data.get('start_date'),
            'delivery_time_slot': data.get('delivery_time_slot'),
            'delivery_notes': data.get('delivery_notes'),
            'payment_method': data.get('payment_method'),
            'auto_pay': data.get('auto_pay', True)
        }

        subscription = get_order_service().create_subscription_order(subscription_data, items_data)

        # Send confirmation notification
        get_notification_service().send_notification(
            user.id,
            'subscription_created',
            template_data={
                'subscription_id': subscription.id,
                'frequency': frequency,
                'next_delivery': subscription.next_delivery_date.isoformat() if subscription.next_delivery_date else None
            }
        )

        return created_response(
            data={
                'subscription': {
                    'id': subscription.id,
                    'frequency': subscription.frequency,
                    'status': subscription.status,
                    'next_delivery_date': subscription.next_delivery_date.isoformat() if subscription.next_delivery_date else None,
                    'created_at': subscription.created_at.isoformat()
                }
            },
            message=get_translation('api.subscriptions.created')
        )

    except ValueError as e:
        return error_response(message=str(e), status_code=400)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create subscription order error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/schedule', methods=['POST'])
@jwt_required()
def schedule_order():
    """Schedule an order for future delivery"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        user = User.query.get(current_user_id)
        if not user:
            return not_found_response(message=get_translation('error.not_found'))

        scheduled_date = data.get('scheduled_date')
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_date)
            if scheduled_dt <= datetime.now(UTC):
                return error_response(
                    message=get_translation('error.validation.invalid_date'),
                    status_code=400
                )
        except ValueError:
            return error_response(
                message=get_translation('error.validation.invalid_date'),
                status_code=400
            )

        # Create scheduled order
        order_data = {
            'user_id': current_user_id,
            'delivery_address_id': data.get('delivery_address_id'),
            'delivery_date': scheduled_dt.date(),
            'delivery_time_slot': data.get('delivery_time_slot'),
            'delivery_notes': data.get('delivery_notes'),
            'payment_method': data.get('payment_method'),
            'order_source': data.get('source', 'web'),
            'is_scheduled': True,
            'scheduled_date': scheduled_dt
        }

        items_data = data.get('items', [])
        order = get_order_service().create_scheduled_order(order_data, items_data)

        # Schedule order processing task
        from business_app.tasks.order_tasks import process_scheduled_order_task
        process_scheduled_order_task.apply_async(
            args=[order.id],
            eta=scheduled_dt - timedelta(hours=1)  # Process 1 hour before scheduled time
        )

        return created_response(
            data={
                'order': serialize_order(order),
                'scheduled_for': scheduled_dt.isoformat()
            },
            message=get_translation('api.orders.created')
        )

    except ValueError as e:
        return error_response(message=str(e), status_code=400)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Schedule order error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/statuses', methods=['GET'])
def get_order_statuses():
    """
    Get all available order statuses.
    
    This endpoint provides the single source of truth for order statuses,
    ensuring UI and backend remain synchronized.
    """
    statuses = []
    for status in OrderStatus:
        # Convert enum value to human-readable label
        label = status.value.replace('_', ' ').title()
        statuses.append({
            'value': status.value,
            'label': label
        })
    
    return success_response(
        data={'statuses': statuses},
        message='Order statuses retrieved successfully'
    )