"""
Admin API endpoints for the Water Business Platform
This file should be placed in business_app/api/admin.py
"""
from flask import Blueprint, request, jsonify, current_app, g
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import and_, or_, desc, func, text
from datetime import datetime, UTC, timedelta

from business_app.models.user import User, UserAddress
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product, ProductCategory
from business_app.models.payment import Payment
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.subscription import Subscription
from business_app.models.loyalty import LoyaltyProgram, LoyaltyReward
from business_app.models.notification import NotificationTemplate
from business_app.models.analytics import PromotionalCampaign, UserSegment
# from business_app.services.admin_service import AdminService
from business_app.utils.service_factory import get_notification_service
from business_app.serializers.admin_serializers import (
    serialize_user_admin, serialize_order_admin, serialize_product_admin,
    serialize_delivery_person_admin, generate_admin_dashboard_data,
    UserAdminSchema, OrderAdminSchema, ProductAdminSchema, AdminDashboardSchema
)
# from business_app.services.file_storage_service import FileStorageService
from business_app.utils.decorators import (
    validate_json, admin_required, super_admin_required, 
    manager_or_higher_required, staff_or_higher_required, validate_admin_action, rate_limit
)
from business_app.utils.query_optimization import (
    get_users_with_stats, get_orders_with_details, QueryOptimizer, 
    PaginationOptimizer, AggregationOptimizer
)
from business_app.services.inventory_service import get_inventory_service, InventoryOperationType
from business_app.utils.constants import UserRole, SubscriptionStatus, OrderStatus, DeliveryStatus, UserStatus
# from business_app.tasks.admin_tasks import send_bulk_email_task, generate_report_task
from business_app import db

admin_bp = Blueprint('admin', __name__)



@admin_bp.route('/dashboard', methods=['GET'])
@jwt_required()
@staff_or_higher_required
def get_admin_dashboard():
    """Get admin dashboard metrics"""
    try:
        # Get key metrics for admin dashboard
        now = datetime.now(UTC)
        today = now.date()
        yesterday = today - timedelta(days=1)
        last_week = now - timedelta(days=7)
        last_month = now - timedelta(days=30)
        
        # User metrics
        total_users = User.query.count()
        new_users_today = User.query.filter(
            func.date(User.created_at) == today
        ).count()
        new_users_week = User.query.filter(
            User.created_at >= last_week
        ).count()
        active_users = User.query.filter_by(status=UserStatus.ACTIVE.value).count()
        
        # Order metrics
        total_orders = Order.query.count()
        orders_today = Order.query.filter(
            func.date(Order.created_at) == today
        ).count()
        pending_orders = Order.query.filter_by(status=OrderStatus.PENDING.value).count()
        revenue_today = db.session.query(func.sum(Order.total_amount)).filter(
            func.date(Order.created_at) == today
        ).scalar() or 0
        revenue_month = db.session.query(func.sum(Order.total_amount)).filter(
            Order.created_at >= last_month
        ).scalar() or 0
        
        # Product metrics
        total_products = Product.query.filter_by(is_active=True).count()
        low_stock_products = Product.query.filter(
            and_(Product.stock_quantity <= Product.min_stock_level, Product.track_inventory == True)
        ).count()
        
        # Delivery metrics
        active_deliveries = Delivery.query.filter(
            Delivery.status.in_([DeliveryStatus.ASSIGNED.value, DeliveryStatus.PICKED_UP.value, DeliveryStatus.IN_TRANSIT.value])
        ).count()
        failed_deliveries_today = Delivery.query.filter(
            and_(
                Delivery.status == DeliveryStatus.FAILED.value,
                func.date(Delivery.created_at) == today
            )
        ).count()
        
        # Subscription metrics
        active_subscriptions = Subscription.query.filter_by(is_active=True).count()
        subscription_revenue_month = db.session.query(func.sum(Subscription.billing_amount)).filter(
            Subscription.status == SubscriptionStatus.ACTIVE.value
        ).scalar() or 0
        
        dashboard_data = {
            'users': {
                'total': total_users,
                'new_today': new_users_today,
                'new_this_week': new_users_week,
                'active': active_users
            },
            'orders': {
                'total': total_orders,
                'today': orders_today,
                'pending': pending_orders,
                'revenue_today': revenue_today,
                'revenue_month': revenue_month
            },
            'products': {
                'total': total_products,
                'low_stock': low_stock_products
            },
            'delivery': {
                'active_deliveries': active_deliveries,
                'failed_today': failed_deliveries_today
            },
            'subscriptions': {
                'active': active_subscriptions,
                'monthly_revenue': subscription_revenue_month
            }
        }
        
        return jsonify({
            'dashboard': dashboard_data,
            'timestamp': now.isoformat()
        })
        
    except Exception as e:
        current_app.logger.error(f"Get admin dashboard error: {e}")
        return jsonify({'error': 'Failed to get admin dashboard'}), 500


@admin_bp.route('/users', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_users'])
def get_users():
    """Get users with filtering and pagination"""
    try:
        # Get query parameters
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 50)), 100)
        search = request.args.get('search', '').strip()
        role = request.args.get('role')
        status = request.args.get('status')
        sort_by = request.args.get('sort_by', 'created_at')
        sort_order = request.args.get('sort_order', 'desc')
        
        # Build query
        query = User.query
        
        # Apply search filter
        if search:
            search_term = f"%{search}%"
            query = query.filter(or_(
                User.first_name.ilike(search_term),
                User.last_name.ilike(search_term),
                User.email.ilike(search_term),
                User.phone.ilike(search_term)
            ))
        
        # Apply role filter
        if role:
            try:
                user_role = UserRole(role)
                query = query.filter_by(role=user_role)
            except ValueError:
                return jsonify({'error': 'Invalid role value'}), 400
        
        # Apply status filter
        if status:
            try:
                user_status = UserStatus(status)
                query = query.filter_by(status=user_status)
            except ValueError:
                return jsonify({'error': 'Invalid status value'}), 400
        
        # Apply sorting
        if sort_by == 'name':
            order_field = User.first_name
        elif sort_by == 'email':
            order_field = User.email
        elif sort_by == 'created_at':
            order_field = User.created_at
        elif sort_by == 'last_login':
            order_field = User.last_login
        else:
            order_field = User.created_at
        
        if sort_order == 'desc':
            order_field = order_field.desc()
        
        query = query.order_by(order_field)
        
        # Apply eager loading for user list optimization
        query = get_users_with_stats(query)
        
        # Paginate with optimized query
        pagination = PaginationOptimizer.optimize_paginated_query(
            query, page, per_page, eager_load_strategy='user_admin_list'
        )
        
        # Get user statistics efficiently
        user_ids = [user.id for user in pagination.items]
        user_statistics = AggregationOptimizer.get_user_statistics(user_ids)
        
        # Serialize users with statistics
        users_data = []
        for user in pagination.items:
            user_data = serialize_user_admin(user, include_statistics=True)
            user_stats = user_statistics.get(user.id, {})
            user_data.update({
                'order_count': user_stats.get('order_count', 0),
                'total_spent': user_stats.get('total_spent', 0),
                'last_order_date': user_stats.get('last_order_date'),
                'delivery_success_rate': (
                    user_stats.get('successful_deliveries', 0) / max(user_stats.get('delivery_count', 1), 1) * 100
                    if user_stats.get('delivery_count', 0) > 0 else 0
                )
            })
            users_data.append(user_data)
        
        return jsonify({
            'users': users_data,
            'pagination': {
                'page': page,
                'pages': pagination.pages,
                'per_page': per_page,
                'total': pagination.total,
                'has_next': pagination.has_next,
                'has_prev': pagination.has_prev
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Get users error: {e}")
        return jsonify({'error': 'Failed to get users'}), 500


@admin_bp.route('/users/<int:user_id>', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_users'])
def get_user_details(user_id):
    """Get detailed user information"""
    try:
        user = User.query.get(user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404
        
        # Get user's orders
        recent_orders = Order.query.filter_by(user_id=user_id).order_by(
            Order.created_at.desc()
        ).limit(10).all()
        
        # Get user's addresses
        addresses = UserAddress.query.filter_by(user_id=user_id).all()
        
        # Get user statistics
        total_orders = Order.query.filter_by(user_id=user_id).count()
        total_spent = db.session.query(func.sum(Order.total_amount)).filter_by(
            user_id=user_id
        ).scalar() or 0
        
        user_details = {
            'user': serialize_user_admin(user),
            'statistics': {
                'total_orders': total_orders,
                'total_spent': total_spent,
                'avg_order_value': total_spent / total_orders if total_orders > 0 else 0
            },
            'recent_orders': [
                serialize_order_admin(order) for order in recent_orders
            ],
            'addresses': [
                {
                    'id': addr.id,
                    'label': addr.label,
                    'address_line_1': addr.address_line_1,
                    'city': addr.city,
                    'is_default': addr.is_default
                }
                for addr in addresses
            ]
        }
        
        return jsonify(user_details)
        
    except Exception as e:
        current_app.logger.error(f"Get user details error: {e}")
        return jsonify({'error': 'Failed to get user details'}), 500


@admin_bp.route('/users/<int:user_id>/status', methods=['PUT'])
@jwt_required()
@manager_or_higher_required
@validate_json(['status'])
def update_user_status(user_id):
    """Update user status"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        
        user = User.query.get(user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404
        
        new_status = data.get('status')
        reason = data.get('reason', '')
        
        # Prevent privilege escalation - operators cannot modify admin/manager accounts
        current_user = g.current_user
        if (current_user.role == UserRole.OPERATOR and 
            user.role in [UserRole.ADMIN, UserRole.MANAGER]):
            return jsonify({'error': 'Insufficient permissions to modify this user'}), 403
        
        # Prevent self-modification of critical status
        if current_user_id == user_id and new_status in ['banned', 'suspended']:
            return jsonify({'error': 'Cannot suspend or ban your own account'}), 400
        
        try:
            user_status = UserStatus(new_status)
        except ValueError:
            return jsonify({'error': 'Invalid status value'}), 400
        
        old_status = user.status
        user.status = user_status
        user.updated_at = datetime.now(UTC)
        
        # Log the status change (placeholder until admin_service is implemented)
        # admin_service.log_admin_action(
        #     admin_id=current_user_id,
        #     action='user_status_changed',
        #     target_type='user',
        #     target_id=user_id,
        #     details=f"Status changed from {old_status.value} to {new_status}. Reason: {reason}"
        # )
        
        db.session.commit()
        
        # Send notification to user if status changed to suspended/banned
        if user_status in [UserStatus.SUSPENDED, UserStatus.BANNED]:
            get_notification_service().send_notification(
                user_id,
                'account_status_changed',
                template_data={
                    'status': new_status,
                    'reason': reason
                }
            )
        
        return jsonify({
            'message': 'User status updated successfully',
            'user': serialize_user_admin(user)
        })
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update user status error: {e}")
        return jsonify({'error': 'Failed to update user status'}), 500


@admin_bp.route('/orders', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_orders', 'manage_orders'])
def get_orders():
    """Get orders with filtering and pagination"""
    try:
        # Get query parameters
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 50)), 100)
        status = request.args.get('status')
        search = request.args.get('search', '').strip()
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        sort_by = request.args.get('sort_by', 'created_at')
        sort_order = request.args.get('sort_order', 'desc')
        
        # Build query
        query = Order.query
        
        # Apply filters
        if status:
            try:
                order_status = OrderStatus(status)
                query = query.filter_by(status=order_status)
            except ValueError:
                return jsonify({'error': 'Invalid status value'}), 400
        
        if search:
            search_term = f"%{search}%"
            query = query.join(User).filter(or_(
                Order.order_number.ilike(search_term),
                User.first_name.ilike(search_term),
                User.last_name.ilike(search_term),
                User.phone.ilike(search_term)
            ))
        
        if start_date:
            try:
                start_dt = datetime.fromisoformat(start_date)
                query = query.filter(Order.created_at >= start_dt)
            except ValueError:
                return jsonify({'error': 'Invalid start_date format'}), 400
        
        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date)
                query = query.filter(Order.created_at <= end_dt)
            except ValueError:
                return jsonify({'error': 'Invalid end_date format'}), 400
        
        # Apply sorting
        if sort_by == 'total_amount':
            order_field = Order.total_amount
        elif sort_by == 'customer':
            order_field = User.first_name
            query = query.join(User)
        else:
            order_field = Order.created_at
        
        if sort_order == 'desc':
            order_field = order_field.desc()
        
        query = query.order_by(order_field)
        
        # Apply eager loading for orders with full details
        query = get_orders_with_details(query)
        
        # Paginate with optimized query
        pagination = PaginationOptimizer.optimize_paginated_query(
            query, page, per_page, eager_load_strategy='order_admin_detail'
        )
        
        # Get order statistics efficiently
        order_ids = [order.id for order in pagination.items]
        order_statistics = AggregationOptimizer.get_order_statistics(order_ids)
        
        # Serialize orders with statistics
        orders_data = []
        for order in pagination.items:
            order_data = serialize_order_admin(order)
            order_stats = order_statistics.get(order.id, {})
            order_data.update({
                'item_count': order_stats.get('item_count', 0),
                'total_quantity': order_stats.get('total_quantity', 0),
                'payment_count': order_stats.get('payment_count', 0),
                'last_payment_date': order_stats.get('last_payment_date')
            })
            orders_data.append(order_data)
        
        return jsonify({
            'orders': orders_data,
            'pagination': {
                'page': page,
                'pages': pagination.pages,
                'per_page': per_page,
                'total': pagination.total,
                'has_next': pagination.has_next,
                'has_prev': pagination.has_prev
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Get orders error: {e}")
        return jsonify({'error': 'Failed to get orders'}), 500


@admin_bp.route('/orders/<int:order_id>/status', methods=['PUT'])
@jwt_required()
@validate_admin_action(['manage_orders', 'update_orders'])
@validate_json(['status'])
def update_order_status(order_id):
    """Update order status"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        
        order = Order.query.get(order_id)
        if not order:
            return jsonify({'error': 'Order not found'}), 404
        
        new_status = data.get('status')
        notes = data.get('notes', '')
        
        try:
            order_status = OrderStatus(new_status)
        except ValueError:
            return jsonify({'error': 'Invalid status value'}), 400
        
        # Placeholder implementation until admin_service is implemented
        old_status = order.status
        order.status = order_status
        order.updated_at = datetime.now(UTC)
        db.session.commit()
        success = True
        
        if success:
            return jsonify({
                'message': 'Order status updated successfully',
                'order': serialize_order_admin(order)
            })
        else:
            return jsonify({'error': 'Failed to update order status'}), 500
        
    except Exception as e:
        current_app.logger.error(f"Update order status error: {e}")
        return jsonify({'error': 'Failed to update order status'}), 500


@admin_bp.route('/products', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_products', 'manage_products'])
def get_products_admin():
    """Get products for admin management"""
    try:
        # Get query parameters
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 50)), 100)
        search = request.args.get('search', '').strip()
        category_id = request.args.get('category_id', type=int)
        is_active = request.args.get('is_active', type=bool)
        low_stock_only = request.args.get('low_stock_only', type=bool, default=False)
        
        # Build query
        query = Product.query
        
        # Apply filters
        if search:
            search_term = f"%{search}%"
            query = query.filter(or_(
                Product.name.ilike(search_term),
                Product.sku.ilike(search_term),
                Product.description.ilike(search_term)
            ))
        
        if category_id:
            query = query.filter_by(category_id=category_id)
        
        if is_active is not None:
            query = query.filter_by(is_active=is_active)
        
        if low_stock_only:
            query = query.filter(
                and_(
                    Product.track_inventory == True,
                    Product.stock_quantity <= Product.min_stock_level
                )
            )
        
        # Order by name
        query = query.order_by(Product.name)
        
        # Paginate
        pagination = query.paginate(
            page=page, per_page=per_page, error_out=False
        )
        
        return jsonify({
            'products': [
                serialize_product_admin(product) for product in pagination.items
            ],
            'pagination': {
                'page': page,
                'pages': pagination.pages,
                'per_page': per_page,
                'total': pagination.total,
                'has_next': pagination.has_next,
                'has_prev': pagination.has_prev
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Get products admin error: {e}")
        return jsonify({'error': 'Failed to get products'}), 500


@admin_bp.route('/products/<int:product_id>/stock', methods=['PUT'])
@jwt_required()
@validate_admin_action(['manage_products'])
@validate_json(['stock_quantity'])
def update_product_stock(product_id):
    """Update product stock"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        
        product = Product.query.get(product_id)
        if not product:
            return jsonify({'error': 'Product not found'}), 404
        
        new_stock = data.get('stock_quantity')
        adjustment_reason = data.get('reason', 'Manual adjustment')
        
        if not isinstance(new_stock, int) or new_stock < 0:
            return jsonify({'error': 'Invalid stock quantity'}), 400
        
        old_stock = product.stock_quantity
        product.stock_quantity = new_stock
        product.updated_at = datetime.now(UTC)
        
        # Log stock adjustment (placeholder until admin_service is implemented)
        # admin_service.log_admin_action(
        #     admin_id=current_user_id,
        #     action='stock_adjusted',
        #     target_type='product',
        #     target_id=product_id,
        #     details=f"Stock changed from {old_stock} to {new_stock}. Reason: {adjustment_reason}"
        # )
        
        db.session.commit()
        
        return jsonify({
            'message': 'Product stock updated successfully',
            'product': serialize_product_admin(product)
        })
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update product stock error: {e}")
        return jsonify({'error': 'Failed to update product stock'}), 500


@admin_bp.route('/delivery-personnel', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_delivery', 'manage_delivery'])
def get_delivery_personnel():
    """Get delivery personnel"""
    try:
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 50)), 100)
        is_active = request.args.get('is_active', type=bool)
        search = request.args.get('search', '').strip()
        
        # Build query
        query = DeliveryPerson.query
        
        # Apply filters
        if is_active is not None:
            query = query.filter_by(is_active=is_active)
        
        if search:
            search_term = f"%{search}%"
            query = query.filter(or_(
                DeliveryPerson.full_name.ilike(search_term),
                DeliveryPerson.phone.ilike(search_term),
                DeliveryPerson.vehicle_number.ilike(search_term)
            ))
        
        # Order by name
        query = query.order_by(DeliveryPerson.full_name)
        
        # Paginate
        pagination = query.paginate(
            page=page, per_page=per_page, error_out=False
        )
        
        return jsonify({
            'delivery_personnel': [
                serialize_delivery_person_admin(person) for person in pagination.items
            ],
            'pagination': {
                'page': page,
                'pages': pagination.pages,
                'per_page': per_page,
                'total': pagination.total,
                'has_next': pagination.has_next,
                'has_prev': pagination.has_prev
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Get delivery personnel error: {e}")
        return jsonify({'error': 'Failed to get delivery personnel'}), 500


@admin_bp.route('/campaigns', methods=['GET'])
@jwt_required()
@admin_required
def get_promotional_campaigns():
    """Get promotional campaigns"""
    try:
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 50)), 100)
        is_active = request.args.get('is_active', type=bool)
        
        # Build query
        query = PromotionalCampaign.query
        
        if is_active is not None:
            query = query.filter_by(is_active=is_active)
        
        # Order by creation date (newest first)
        query = query.order_by(PromotionalCampaign.created_at.desc())
        
        # Paginate
        pagination = query.paginate(
            page=page, per_page=per_page, error_out=False
        )
        
        campaigns_data = []
        for campaign in pagination.items:
            campaign_data = {
                'id': campaign.id,
                'name': campaign.name,
                'description': campaign.description,
                'promo_code': campaign.promo_code,
                'discount_type': campaign.discount_type,
                'discount_value': campaign.discount_value,
                'min_order_value': campaign.min_order_value,
                'usage_limit': campaign.usage_limit,
                'usage_count': campaign.usage_count,
                'is_active': campaign.is_active,
                'start_date': campaign.start_date.isoformat() if campaign.start_date else None,
                'end_date': campaign.end_date.isoformat() if campaign.end_date else None,
                'created_at': campaign.created_at.isoformat()
            }
            campaigns_data.append(campaign_data)
        
        return jsonify({
            'campaigns': campaigns_data,
            'pagination': {
                'page': page,
                'pages': pagination.pages,
                'per_page': per_page,
                'total': pagination.total,
                'has_next': pagination.has_next,
                'has_prev': pagination.has_prev
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Get promotional campaigns error: {e}")
        return jsonify({'error': 'Failed to get campaigns'}), 500


@admin_bp.route('/reports/generate', methods=['POST'])
@jwt_required()
@rate_limit(max_requests=5, window_seconds=1800, per='user')  # 5 reports per 30 minutes per user
@admin_required
@validate_json(['report_type'])
def generate_report():
    """Generate administrative report"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        
        report_type = data.get('report_type')
        date_range = data.get('date_range', {})
        filters = data.get('filters', {})
        format_type = data.get('format', 'pdf')  # pdf, excel, csv
        
        # Validate report type
        valid_reports = [
            'sales_summary', 'customer_report', 'product_performance',
            'delivery_report', 'financial_summary', 'user_activity'
        ]
        
        if report_type not in valid_reports:
            return jsonify({'error': 'Invalid report type'}), 400
        
        # Generate report asynchronously (placeholder until task is implemented)
        # task = generate_report_task.delay(
        #     report_type=report_type,
        #     date_range=date_range,
        #     filters=filters,
        #     format_type=format_type,
        #     requested_by=current_user_id
        # )
        task_id = 'placeholder_task_id'
        
        return jsonify({
            'message': 'Report generation started',
            'task_id': task_id,
            'report_type': report_type
        })
        
    except Exception as e:
        current_app.logger.error(f"Generate report error: {e}")
        return jsonify({'error': 'Failed to generate report'}), 500


@admin_bp.route('/bulk-actions', methods=['POST'])
@jwt_required()
@rate_limit(max_requests=10, window_seconds=600, per='user')  # 10 bulk actions per 10 minutes per user
@manager_or_higher_required
@validate_json(['action', 'target_type', 'target_ids'])
def perform_bulk_action():
    """Perform bulk actions"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        
        action = data.get('action')
        target_type = data.get('target_type')
        target_ids = data.get('target_ids')
        parameters = data.get('parameters', {})
        
        # Validate inputs
        if not isinstance(target_ids, list) or len(target_ids) > 1000:
            return jsonify({'error': 'Invalid target_ids or too many items (max 1000)'}), 400
        
        valid_actions = {
            'user': ['activate', 'deactivate', 'suspend', 'send_email'],
            'order': ['cancel', 'confirm', 'ship'],
            'product': ['activate', 'deactivate', 'update_stock']
        }
        
        if target_type not in valid_actions or action not in valid_actions[target_type]:
            return jsonify({'error': 'Invalid action for target type'}), 400
        
        # Perform bulk action (placeholder until service is implemented)
        # result = admin_service.perform_bulk_action(
        #     action=action,
        #     target_type=target_type,
        #     target_ids=target_ids,
        #     parameters=parameters,
        #     admin_id=current_user_id
        # )
        result = {'success': len(target_ids), 'failed': 0, 'message': 'Bulk action placeholder'}
        
        return jsonify({
            'message': f'Bulk action {action} completed',
            'results': result
        })
        
    except Exception as e:
        current_app.logger.error(f"Perform bulk action error: {e}")
        return jsonify({'error': 'Failed to perform bulk action'}), 500


@admin_bp.route('/system-settings', methods=['GET'])
@jwt_required()
@super_admin_required
def get_system_settings():
    """Get system settings"""
    try:
        # settings = admin_service.get_system_settings()  # Placeholder until service is implemented
        settings = {'placeholder': 'System settings not yet implemented'}
        return jsonify({'settings': settings})
        
    except Exception as e:
        current_app.logger.error(f"Get system settings error: {e}")
        return jsonify({'error': 'Failed to get system settings'}), 500


@admin_bp.route('/system-settings', methods=['PUT'])
@jwt_required()
@super_admin_required
@validate_json(['settings'])
def update_system_settings():
    """Update system settings"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        
        settings = data.get('settings')
        
        # Update settings
        # success = admin_service.update_system_settings(settings, current_user_id)  # Placeholder until service is implemented
        success = True  # Placeholder
        
        if success:
            return jsonify({'message': 'System settings updated successfully'})
        else:
            return jsonify({'error': 'Failed to update system settings'}), 500
        
    except Exception as e:
        current_app.logger.error(f"Update system settings error: {e}")
        return jsonify({'error': 'Failed to update system settings'}), 500


@admin_bp.route('/audit-logs', methods=['GET'])
@jwt_required()
@admin_required
def get_audit_logs():
    """Get audit logs"""
    try:
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 50)), 100)
        action = request.args.get('action')
        admin_id = request.args.get('admin_id', type=int)
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        
        # logs = admin_service.get_audit_logs(  # Placeholder until service is implemented
        #     page=page,
        #     per_page=per_page,
        #     action=action,
        #     admin_id=admin_id,
        #     start_date=start_date,
        #     end_date=end_date
        # )
        logs = {'logs': [], 'pagination': {'page': page, 'total': 0}}
        
        return jsonify(logs)
        
    except Exception as e:
        current_app.logger.error(f"Get audit logs error: {e}")
        return jsonify({'error': 'Failed to get audit logs'}), 500


@admin_bp.route('/send-announcement', methods=['POST'])
@jwt_required()
@rate_limit(max_requests=3, window_seconds=3600, per='user')  # 3 announcements per hour per user
@manager_or_higher_required
@validate_json(['title', 'message'])
def send_announcement():
    """Send announcement to users"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        
        title = data.get('title')
        message = data.get('message')
        target_users = data.get('target_users', 'all')  # all, active, segment_id
        channels = data.get('channels', ['email', 'push'])
        
        # Send announcement asynchronously (placeholder until task is implemented)
        # task = send_bulk_email_task.delay(
        #     subject=title,
        #     message=message,
        #     target_users=target_users,
        #     channels=channels,
        #     sender_id=current_user_id
        # )
        task_id = 'placeholder_announcement_task'
        
        return jsonify({
            'message': 'Announcement queued for sending',
            'task_id': task_id
        })
        
    except Exception as e:
        current_app.logger.error(f"Send announcement error: {e}")
        return jsonify({'error': 'Failed to send announcement'}), 500


@admin_bp.route('/inventory/<int:product_id>/status', methods=['GET'])
@jwt_required()
@validate_admin_action(['manage_products', 'view_products'])
def get_inventory_status(product_id):
    """Get detailed inventory status for a product"""
    try:
        inventory_status = get_inventory_service().get_inventory_status(product_id)
        return jsonify({
            'success': True,
            'data': inventory_status
        })
        
    except Exception as e:
        current_app.logger.error(f"Get inventory status error: {e}")
        return jsonify({'error': 'Failed to get inventory status'}), 500


@admin_bp.route('/inventory/<int:product_id>/adjust', methods=['POST'])
@jwt_required()
@validate_admin_action(['manage_products'])
@validate_json(['quantity_change', 'operation_type', 'reason'])
def adjust_inventory(product_id):
    """Manually adjust inventory levels"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        
        quantity_change = int(data['quantity_change'])
        operation_type_str = data['operation_type']
        reason = data['reason']
        
        # Validate operation type
        try:
            operation_type = InventoryOperationType(operation_type_str)
        except ValueError:
            return jsonify({
                'error': f'Invalid operation type. Must be one of: {[op.value for op in InventoryOperationType]}'
            }), 400
        
        # Validate quantity change
        if quantity_change == 0:
            return jsonify({'error': 'Quantity change cannot be zero'}), 400
        
        if abs(quantity_change) > 10000:
            return jsonify({'error': 'Quantity change too large (max 10000)'}), 400
        
        # Perform adjustment
        result = get_inventory_service().adjust_inventory(
            product_id=product_id,
            quantity_change=quantity_change,
            operation_type=operation_type,
            reason=reason,
            user_id=current_user_id
        )
        
        if result['success']:
            return jsonify({
                'success': True,
                'message': 'Inventory adjusted successfully',
                'data': result
            })
        else:
            return jsonify({
                'success': False,
                'error': result.get('reason', 'Adjustment failed')
            }), 400
            
    except Exception as e:
        current_app.logger.error(f"Adjust inventory error: {e}")
        return jsonify({'error': 'Failed to adjust inventory'}), 500


@admin_bp.route('/inventory/check-availability', methods=['POST'])
@jwt_required()
@validate_admin_action(['view_products', 'manage_products'])
@validate_json(['items'])
def check_inventory_availability():
    """Check inventory availability for multiple products"""
    try:
        data = request.get_json()
        items = data['items']
        
        # Validate items structure
        for item in items:
            if 'product_id' not in item or 'quantity' not in item:
                return jsonify({'error': 'Each item must have product_id and quantity'}), 400
        
        # Check availability
        availability_results = get_inventory_service().check_multiple_products_availability(items)
        
        # Format response
        results = []
        for result in availability_results:
            product = Product.query.get(result.product_id)
            results.append({
                'product_id': result.product_id,
                'product_name': product.name if product else 'Unknown',
                'requested_quantity': result.requested_quantity,
                'available_quantity': result.available_quantity,
                'reserved_quantity': result.reserved_quantity,
                'is_available': result.is_available,
                'reason': result.reason
            })
        
        return jsonify({
            'success': True,
            'data': {
                'results': results,
                'all_available': all(r.is_available for r in availability_results)
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Check inventory availability error: {e}")
        return jsonify({'error': 'Failed to check inventory availability'}), 500


@admin_bp.route('/inventory/reservations/<int:order_id>', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_orders', 'manage_orders'])
def get_order_reservations(order_id):
    """Get inventory reservations for an order"""
    try:
        # This would require extending the inventory service to get reservations by order
        # For now, return basic order information
        order = Order.query.get(order_id)
        if not order:
            return jsonify({'error': 'Order not found'}), 404
        
        # Get inventory status for each item in the order
        reservations = []
        for item in order.items:
            inventory_status = get_inventory_service().get_inventory_status(item.product_id)
            reservations.append({
                'product_id': item.product_id,
                'product_name': item.product.name,
                'quantity': item.quantity,
                'current_stock': inventory_status['current_stock'],
                'available_quantity': inventory_status['available_quantity'],
                'reserved_quantity': inventory_status['reserved_quantity']
            })
        
        return jsonify({
            'success': True,
            'data': {
                'order_id': order_id,
                'order_status': order.status.value,
                'reservations': reservations
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Get order reservations error: {e}")
        return jsonify({'error': 'Failed to get order reservations'}), 500


@admin_bp.route('/inventory/reservations/<int:order_id>/release', methods=['POST'])
@jwt_required()
@validate_admin_action(['manage_orders'])
def release_order_reservations(order_id):
    """Manually release inventory reservations for an order"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json() or {}
        reason = data.get('reason', 'Manual release by admin')
        
        # Check if order exists
        order = Order.query.get(order_id)
        if not order:
            return jsonify({'error': 'Order not found'}), 404
        
        # Release reservations
        result = get_inventory_service().release_reservations(order_id)
        
        if result['success']:
            # Log the manual release
            from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity
            audit_logger.log_event(
                event_type=AuditEventType.INVENTORY_UPDATED,
                action="inventory_reservations_manually_released",
                severity=AuditSeverity.HIGH,
                resource_type="order_inventory",
                resource_id=str(order_id),
                description=f"Admin manually released inventory reservations for order {order_id}",
                additional_data={
                    'order_id': order_id,
                    'released_by_user_id': current_user_id,
                    'reason': reason
                }
            )
            
            return jsonify({
                'success': True,
                'message': 'Reservations released successfully',
                'data': result
            })
        else:
            return jsonify({
                'success': False,
                'error': result.get('reason', 'Failed to release reservations')
            }), 400
            
    except Exception as e:
        current_app.logger.error(f"Release order reservations error: {e}")
        return jsonify({'error': 'Failed to release reservations'}), 500


@admin_bp.route('/backup', methods=['POST'])
@jwt_required()
@rate_limit(max_requests=2, window_seconds=3600, per='user')  # 2 backups per hour per user
@super_admin_required
def create_backup():
    """Create system backup"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json() or {}
        
        backup_type = data.get('type', 'full')  # full, incremental
        include_files = data.get('include_files', True)
        
        # Create backup asynchronously (placeholder until service is implemented)
        # backup_result = admin_service.create_backup(
        #     backup_type=backup_type,
        #     include_files=include_files,
        #     requested_by=current_user_id
        # )
        backup_result = {'backup_id': 'placeholder_backup_id'}
        
        return jsonify({
            'message': 'Backup creation started',
            'backup_id': backup_result['backup_id']
        })
        
    except Exception as e:
        current_app.logger.error(f"Create backup error: {e}")
        return jsonify({'error': 'Failed to create backup'}), 500