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
    get_notification_service, get_analytics_service
)
from business_app.serializers.order_serializers import (
    serialize_order, serialize_order_item, serialize_order_delivery, serialize_order_payment,
    serialize_order_statistics, serialize_cart_estimate, serialize_delivery_slot,
    OrderSchema, OrderItemSchema, CreateOrderRequest, UpdateOrderRequest,
    OrderFeedbackRequest, CartEstimateRequest, DeliverySlotSchema
)
from business_app.utils.decorators import validate_json, validate_order_input, validate_query_params, rate_limit
from business_app.utils.constants import OrderStatus, PaymentMethod, DeliveryStatus, UserRole
from business_app.utils.validation_helpers import (
    validate_list_request_params, FilterValidator, PaginationHelper,
    DateValidator, StatusValidator, RequestDataValidator
)
from business_app.utils.error_handlers import handle_api_exception, create_success_response
from business_app.utils.exceptions import ValidationError
from business_app.tasks.delivery_tasks import auto_assign_delivery_task
from business_app import db

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
    
    # Build standardized pagination response
    response_data = PaginationHelper.build_pagination_response(
        pagination.items, pagination, serialize_order
    )
    
    return create_success_response(
        data={'orders': response_data['items'], 'pagination': response_data['pagination']},
        message='Orders retrieved successfully'
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
            return jsonify({'error': 'Order not found'}), 404
        
        # Get delivery information
        delivery_info = None
        if order.delivery:
            delivery = order.delivery
            delivery_info = {
                'tracking_number': delivery.tracking_number,
                'status': delivery.status.value,
                'estimated_delivery_time': delivery.estimated_delivery_time.isoformat() if delivery.estimated_delivery_time else None,
                'actual_delivery_time': delivery.actual_delivery_time.isoformat() if delivery.actual_delivery_time else None,
                'current_location': {
                    'lat': delivery.current_location_lat,
                    'lng': delivery.current_location_lng,
                    'last_update': delivery.last_location_update.isoformat() if delivery.last_location_update else None
                } if delivery.current_location_lat and delivery.current_location_lng else None,
                'driver': {
                    'name': delivery.delivery_person.full_name,
                    'phone': delivery.delivery_person.phone
                } if delivery.delivery_person else None,
                'delivery_attempts': delivery.delivery_attempts,
                'failed_reason': delivery.failed_delivery_reason
            }
        
        # Get order timeline
        timeline = get_order_service().get_order_timeline(order_id)
        
        return jsonify({
            'order': serialize_order(order, include_items=True, include_delivery=True),
            'delivery': delivery_info,
            'timeline': timeline
        })
        
    except Exception as e:
        current_app.logger.error(f"Track order error: {e}")
        return jsonify({'error': 'Failed to track order'}), 500


@orders_bp.route('/emergency', methods=['POST'])
@jwt_required()
@rate_limit(max_requests=3, window_seconds=3600, per='user')  # 3 emergency orders per hour per user
@validate_order_input('emergency_order')
def create_emergency_order():
    """Create an emergency order with priority delivery"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        
        user = User.query.get(current_user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404
        
        # Authorization check: Emergency orders are only available to premium users
        # or users with special authorization
        if not user.is_premium and user.role not in [UserRole.ADMIN, UserRole.MANAGER, UserRole.OPERATOR]:
            current_app.logger.warning(
                f"Unauthorized emergency order attempt by user {user.id} ({user.email}). "
                f"User role: {user.role}, is_premium: {user.is_premium}"
            )
            return jsonify({
                'error': 'Emergency orders are only available to premium customers',
                'code': 'PREMIUM_REQUIRED'
            }), 403
        
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
            return jsonify({
                'error': 'Maximum emergency orders limit reached for today (3 orders)',
                'code': 'EMERGENCY_LIMIT_EXCEEDED'
            }), 429
        
        # Emergency orders have additional fee
        emergency_fee = 10000  # 10,000 UZS emergency fee
        
        # Create order with emergency flag
        order_data = {
            'user_id': current_user_id,
            'delivery_address_id': data.get('delivery_address_id'),
            'delivery_notes': data.get('delivery_notes'),
            'is_urgent': True,
            'payment_method': data.get('payment_method'),
            'order_source': data.get('source', 'web'),
            'emergency_fee': emergency_fee
        }
        
        # Set delivery for within 2 hours
        emergency_delivery_time = datetime.now(UTC) + timedelta(hours=2)
        order_data['delivery_date'] = emergency_delivery_time.date()
        order_data['delivery_time_slot'] = 'emergency'
        
        items_data = data.get('items', [])
        order = get_order_service().create_order(order_data, items_data)
        
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
        
        return jsonify({
            'message': 'Emergency order created successfully',
            'order': serialize_order(order, include_items=True),
            'emergency_fee': emergency_fee,
            'estimated_delivery_time': emergency_delivery_time.isoformat()
        }), 201
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create emergency order error: {e}")
        return jsonify({'error': 'Failed to create emergency order'}), 500


@orders_bp.route('/quick-reorder', methods=['GET'])
@jwt_required()
def get_quick_reorder_suggestions():
    """Get quick reorder suggestions based on order history"""
    try:
        current_user_id = get_jwt_identity()
        limit = min(int(request.args.get('limit', 5)), 10)
        
        # Get user's most frequent orders from last 3 months
        three_months_ago = datetime.now(UTC) - timedelta(days=90)
        
        frequent_items = db.session.query(
            OrderItem.product_id,
            Product.name,
            Product.current_price,
            Product.image_urls,
            func.sum(OrderItem.quantity).label('total_quantity'),
            func.count(OrderItem.id).label('order_count'),
            func.max(Order.created_at).label('last_ordered')
        ).join(Order).join(Product).filter(
            Order.user_id == current_user_id,
            Order.created_at >= three_months_ago,
            Order.status.in_([OrderStatus.DELIVERED, OrderStatus.CONFIRMED])
        ).group_by(
            OrderItem.product_id, Product.name, Product.current_price, Product.image_urls
        ).order_by(
            desc('order_count'), desc('total_quantity')
        ).limit(limit).all()
        
        suggestions = []
        for item in frequent_items:
            suggestions.append({
                'product_id': item.product_id,
                'name': item.name,
                'current_price': item.current_price,
                'image_url': item.image_urls[0] if item.image_urls else None,
                'suggested_quantity': min(int(item.total_quantity / item.order_count), 10),
                'order_frequency': item.order_count,
                'last_ordered': item.last_ordered.isoformat()
            })
        
        return jsonify({
            'quick_reorder_suggestions': suggestions
        })
        
    except Exception as e:
        current_app.logger.error(f"Get quick reorder suggestions error: {e}")
        return jsonify({'error': 'Failed to get suggestions'}), 500


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
                product_counter[item.product_name] += item.quantity
        
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
        
        return jsonify({
            'period': period,
            'statistics': {
                'total_orders': total_orders,
                'total_spent': total_spent,
                'average_order_value': round(avg_order_value, 2),
                'orders_by_status': status_counts,
                'top_products': top_products,
                'monthly_spending_trend': monthly_spending
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Get order statistics error: {e}")
        return jsonify({'error': 'Failed to get statistics'}), 500


@orders_bp.route('/<int:order_id>/feedback', methods=['POST'])
@jwt_required()
@validate_order_input('order_feedback')
def submit_order_feedback(order_id):
    """Submit feedback for a completed order"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        
        order = Order.query.filter_by(
            id=order_id, 
            user_id=current_user_id
        ).first()
        
        if not order:
            return jsonify({'error': 'Order not found'}), 404
        
        if order.status != OrderStatus.DELIVERED:
            return jsonify({'error': 'Can only provide feedback for delivered orders'}), 400
        
        rating = data.get('rating')
        if not isinstance(rating, int) or rating < 1 or rating > 5:
            return jsonify({'error': 'Rating must be between 1 and 5'}), 400
        
        # Update delivery with customer feedback
        if order.delivery:
            order.delivery.customer_rating = rating
            order.delivery.customer_feedback = data.get('comment')
            db.session.commit()
        
        # Track feedback for analytics
        get_analytics_service().track_order_feedback(order_id, rating, data.get('comment'))
        
        return jsonify({'message': 'Feedback submitted successfully'})
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Submit order feedback error: {e}")
        return jsonify({'error': 'Failed to submit feedback'}), 500


@orders_bp.route('/', methods=['POST'])
@jwt_required()
@validate_order_input('create_order')
def create_order():
    """Create a new order"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        
        user = User.query.get(current_user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404
        
        # Validate order items
        items_data = data.get('items', [])
        if not items_data:
            return jsonify({'error': 'Order must contain at least one item'}), 400
        
        # Create order using service
        order_data = {
            'user_id': current_user_id,
            'delivery_address_id': data.get('delivery_address_id'),
            'delivery_date': data.get('delivery_date'),
            'delivery_time_slot': data.get('delivery_time_slot'),
            'delivery_notes': data.get('delivery_notes'),
            'is_urgent': data.get('is_urgent', False),
            'payment_method': data.get('payment_method'),
            'loyalty_points_used': data.get('loyalty_points_used', 0),
            'promo_code': data.get('promo_code'),
            'order_source': data.get('source', 'web')
        }
        
        order = get_order_service().create_order(order_data, items_data)
        
        # Create delivery record if delivery details provided
        if order.delivery_date and order.delivery_time_slot:
            delivery = get_delivery_service().create_delivery(order)
            
            # Auto-assign delivery driver
            auto_assign_delivery_task.delay(delivery.id)
        
        # Send order confirmation
        get_notification_service().send_notification(
            user.id,
            'order_created',
            template_data={
                'order_number': order.order_number,
                'total_amount': order.total_amount
            }
        )
        
        return jsonify({
            'message': 'Order created successfully',
            'order': serialize_order(order, include_items=True)
        }), 201
        
    except ValueError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create order error: {e}")
        return jsonify({'error': 'Failed to create order'}), 500


@orders_bp.route('/<int:order_id>/cancel', methods=['POST'])
@jwt_required()
def cancel_order(order_id):
    """Cancel an order"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json() or {}
        
        order = Order.query.filter_by(
            id=order_id, 
            user_id=current_user_id
        ).first()
        
        if not order:
            return jsonify({'error': 'Order not found'}), 404
        
        if not order.can_be_cancelled():
            return jsonify({'error': 'Order cannot be cancelled at this stage'}), 400
        
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
        
        return jsonify({
            'message': 'Order cancelled successfully',
            'order': serialize_order(order)
        })
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Cancel order error: {e}")
        return jsonify({'error': 'Failed to cancel order'}), 500


@orders_bp.route('/cart/estimate', methods=['POST'])
@jwt_required()
@validate_order_input('cart_estimate')
def estimate_cart():
    """Estimate cart total with discounts and delivery fee"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        
        user = User.query.get(current_user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404
        
        items_data = data.get('items', [])
        delivery_address_id = data.get('delivery_address_id')
        delivery_date = data.get('delivery_date')
        delivery_time_slot = data.get('delivery_time_slot')
        loyalty_points_used = data.get('loyalty_points_used', 0)
        promo_code = data.get('promo_code')
        
        # Calculate cart estimate
        estimate = get_order_service().calculate_cart_estimate(
            user_id=current_user_id,
            items=items_data,
            delivery_address_id=delivery_address_id,
            delivery_date=delivery_date,
            delivery_time_slot=delivery_time_slot,
            loyalty_points_used=loyalty_points_used,
            promo_code=promo_code
        )
        
        return jsonify(estimate)
        
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f"Estimate cart error: {e}")
        return jsonify({'error': 'Failed to estimate cart'}), 500


@orders_bp.route('/delivery-slots', methods=['GET'])
@jwt_required()
def get_delivery_slots():
    """Get available delivery time slots"""
    try:
        delivery_date = request.args.get('delivery_date')
        
        if not delivery_date:
            return jsonify({'error': 'delivery_date parameter is required'}), 400
        
        try:
            target_date = datetime.fromisoformat(delivery_date).date()
        except ValueError:
            return jsonify({'error': 'Invalid delivery_date format'}), 400
        
        # Get available time slots
        slots = get_delivery_service().get_available_time_slots(target_date)
        
        return jsonify({
            'delivery_date': delivery_date,
            'available_slots': [
                serialize_delivery_slot(slot, target_date) for slot in slots
            ]
        })
        
    except Exception as e:
        current_app.logger.error(f"Get delivery slots error: {e}")
        return jsonify({'error': 'Failed to get delivery slots'}), 500


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
        
        campaign = PromotionalCampaign.query.filter_by(
            promo_code=promo_code,
            is_active=True
        ).first()
        
        if not campaign:
            return jsonify({'error': 'Invalid promo code'}), 400
        
        if not campaign.is_valid():
            return jsonify({'error': 'Promo code has expired or reached usage limit'}), 400
        
        if not campaign.can_be_used_by_customer(current_user_id):
            return jsonify({'error': 'You have already used this promo code'}), 400
        
        if campaign.min_order_value and cart_total < campaign.min_order_value:
            return jsonify({
                'error': f'Minimum order value is {campaign.min_order_value} for this promo code'
            }), 400
        
        # Calculate discount
        discount = get_order_service().calculate_promo_discount(campaign, cart_total)
        
        return jsonify({
            'valid': True,
            'campaign': {
                'name': campaign.name,
                'description': campaign.description,
                'discount_type': campaign.discount_type,
                'discount_value': campaign.discount_value,
                'min_order_value': campaign.min_order_value
            },
            'discount_amount': discount,
            'max_discount': campaign.max_discount_amount
        })
        
    except Exception as e:
        current_app.logger.error(f"Validate promo code error: {e}")
        return jsonify({'error': 'Failed to validate promo code'}), 500


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
            return jsonify({'error': 'Original order not found'}), 404
        
        # Create new order based on original
        new_order = get_order_service().repeat_order(original_order)
        
        return jsonify({
            'message': 'Order repeated successfully',
            'order': serialize_order(new_order, include_items=True)
        }), 201
        
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Repeat order error: {e}")
        return jsonify({'error': 'Failed to repeat order'}), 500


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
            return jsonify({'error': 'Order not found'}), 404
        
        # Get delivery tracking information
        delivery_info = None
        if order.delivery:
            delivery = order.delivery
            delivery_info = {
                'tracking_number': delivery.tracking_number,
                'status': delivery.status.value,
                'estimated_delivery_time': delivery.estimated_delivery_time.isoformat() if delivery.estimated_delivery_time else None,
                'actual_delivery_time': delivery.actual_delivery_time.isoformat() if delivery.actual_delivery_time else None,
                'current_location': {
                    'lat': delivery.current_location_lat,
                    'lng': delivery.current_location_lng,
                    'last_update': delivery.last_location_update.isoformat() if delivery.last_location_update else None
                } if delivery.current_location_lat and delivery.current_location_lng else None,
                'driver': {
                    'name': delivery.delivery_person.full_name,
                    'phone': delivery.delivery_person.phone,
                    'vehicle_number': delivery.delivery_person.vehicle_number
                } if delivery.delivery_person else None,
                'delivery_attempts': delivery.delivery_attempts,
                'failed_reason': delivery.failed_delivery_reason,
                'special_instructions': delivery.special_instructions
            }
        
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
        
        return jsonify({
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
        })
        
    except Exception as e:
        current_app.logger.error(f"Track order error: {e}")
        return jsonify({'error': 'Failed to track order'}), 500


@orders_bp.route('/bulk-action', methods=['POST'])
@jwt_required()
@rate_limit(max_requests=5, window_seconds=300, per='user')  # 5 bulk actions per 5 minutes per user
@validate_order_input('bulk_action')
def bulk_order_action():
    """Perform bulk action on multiple orders"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        
        user = User.query.get(current_user_id)
        if not user or not user.is_admin:
            return jsonify({'error': 'Admin access required'}), 403
        
        action = data.get('action')
        order_ids = data.get('order_ids')
        
        if not isinstance(order_ids, list) or len(order_ids) > 100:
            return jsonify({'error': 'Invalid order_ids or too many orders (max 100)'}), 400
        
        valid_actions = ['confirm', 'cancel', 'mark_priority', 'assign_delivery']
        if action not in valid_actions:
            return jsonify({'error': f'Invalid action. Valid actions: {valid_actions}'}), 400
        
        # Process bulk action
        results = get_order_service().perform_bulk_action(action, order_ids, current_user_id)
        
        return jsonify({
            'message': f'Bulk action {action} completed',
            'results': results
        })
        
    except Exception as e:
        current_app.logger.error(f"Bulk order action error: {e}")
        return jsonify({'error': 'Failed to perform bulk action'}), 500


@orders_bp.route('/export', methods=['POST'])
@jwt_required()
@rate_limit(max_requests=3, window_seconds=600, per='user')  # 3 exports per 10 minutes per user
@validate_order_input('export')
def export_orders():
    """Export orders to CSV/Excel"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        
        user = User.query.get(current_user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404
        
        # Regular users can only export their own orders
        if not user.is_admin:
            filters = {'user_id': current_user_id}
        else:
            filters = data.get('filters', {})
        
        format_type = data.get('format', 'csv')  # csv, excel
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        
        if format_type not in ['csv', 'excel']:
            return jsonify({'error': 'Invalid format. Use csv or excel'}), 400
        
        # Generate export
        export_result = get_order_service().export_orders(
            format_type=format_type,
            filters=filters,
            start_date=start_date,
            end_date=end_date,
            user_id=current_user_id
        )
        
        return jsonify({
            'message': 'Export generated successfully',
            'download_url': export_result['download_url'],
            'file_size': export_result['file_size'],
            'expires_at': export_result['expires_at'].isoformat()
        })
        
    except Exception as e:
        current_app.logger.error(f"Export orders error: {e}")
        return jsonify({'error': 'Failed to export orders'}), 500


@orders_bp.route('/subscription', methods=['POST'])
@jwt_required()
@validate_order_input('subscription_order')
def create_subscription_order():
    """Create a recurring subscription order"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        
        user = User.query.get(current_user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404
        
        items_data = data.get('items', [])
        frequency = data.get('frequency')  # weekly, biweekly, monthly
        
        if frequency not in ['weekly', 'biweekly', 'monthly']:
            return jsonify({'error': 'Invalid frequency. Use weekly, biweekly, or monthly'}), 400
        
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
        
        return jsonify({
            'message': 'Subscription order created successfully',
            'subscription': {
                'id': subscription.id,
                'frequency': subscription.frequency,
                'status': subscription.status,
                'next_delivery_date': subscription.next_delivery_date.isoformat() if subscription.next_delivery_date else None,
                'created_at': subscription.created_at.isoformat()
            }
        }), 201
        
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create subscription order error: {e}")
        return jsonify({'error': 'Failed to create subscription order'}), 500


@orders_bp.route('/schedule', methods=['POST'])
@jwt_required()
@validate_order_input('scheduled_order')
def schedule_order():
    """Schedule an order for future delivery"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        
        user = User.query.get(current_user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404
        
        scheduled_date = data.get('scheduled_date')
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_date)
            if scheduled_dt <= datetime.now(UTC):
                return jsonify({'error': 'Scheduled date must be in the future'}), 400
        except ValueError:
            return jsonify({'error': 'Invalid scheduled_date format'}), 400
        
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
        
        return jsonify({
            'message': 'Order scheduled successfully',
            'order': serialize_order(order),
            'scheduled_for': scheduled_dt.isoformat()
        }), 201
        
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Schedule order error: {e}")
        return jsonify({'error': 'Failed to schedule order'}), 500