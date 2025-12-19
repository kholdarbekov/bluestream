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
from business_app.models.product import Product, ProductCategory, ProductSizeEnum
from business_app.models.payment import Payment, PaymentTransaction
from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryRoute, DeliveryTimeSlot
from business_app.models.subscription import Subscription
from business_app.models.loyalty import LoyaltyProgram, LoyaltyReward, LoyaltyPoints, LoyaltyTransaction
from business_app.models.notification import NotificationTemplate
from business_app.models.analytics import PromotionalCampaign, UserSegment
from business_app.models.review import Review
from business_app.models.audit import AuditLog, AuditEventType, AuditSeverity
# TranslatableContent replaced by unified Translation system
from business_app.models.translation import Translation, TranslationCategory, Language
# from business_app.services.admin_service import AdminService
from business_app.utils.service_factory import get_notification_service
from business_app.services.subscription_service import SubscriptionService
from business_app.services.payment_service import PaymentService
from business_app.services.review_service import ReviewService
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
from business_app.utils.constants import UserRole, SubscriptionStatus, OrderStatus, DeliveryStatus, UserStatus, SubscriptionFrequency
# from business_app.tasks.admin_tasks import send_bulk_email_task, generate_report_task
from business_app import db
from business_app.utils.helpers import get_current_language
from business_app.utils.translations import get_translation
from business_app.utils.api_responses import (
    success_response, error_response, paginated_response, created_response,
    not_found_response, validation_error_response, internal_error_response,
    forbidden_response
)
from business_app.utils.bot_webhook import trigger_translation_reload

admin_bp = Blueprint('admin', __name__)



@admin_bp.route('/dashboard', methods=['GET'])
@jwt_required()
@staff_or_higher_required
def get_admin_dashboard():
    """Get comprehensive admin dashboard with analytics and chart data"""
    try:
        # Parse query parameters for date range
        period = request.args.get('period', 'month')  # day, week, month, year

        now = datetime.now(UTC)
        today = now.date()
        yesterday = today - timedelta(days=1)

        # Calculate date ranges based on period
        if period == 'day':
            current_start = datetime.combine(today, datetime.min.time()).replace(tzinfo=UTC)
            previous_start = datetime.combine(yesterday, datetime.min.time()).replace(tzinfo=UTC)
            previous_end = current_start
            chart_days = 24  # Last 24 hours
        elif period == 'week':
            current_start = now - timedelta(days=7)
            previous_start = now - timedelta(days=14)
            previous_end = current_start
            chart_days = 7
        elif period == 'year':
            current_start = now - timedelta(days=365)
            previous_start = now - timedelta(days=730)
            previous_end = current_start
            chart_days = 12  # 12 months
        else:  # month (default)
            current_start = now - timedelta(days=30)
            previous_start = now - timedelta(days=60)
            previous_end = current_start
            chart_days = 30

        # ======================
        # OVERVIEW METRICS
        # ======================

        # User metrics - current period
        total_users = User.query.count()
        new_users_current = User.query.filter(User.created_at >= current_start).count()
        new_users_previous = User.query.filter(
            and_(User.created_at >= previous_start, User.created_at < previous_end)
        ).count()
        active_users = User.query.filter_by(status=UserStatus.ACTIVE.value).count()

        user_growth = ((new_users_current - new_users_previous) / new_users_previous * 100) if new_users_previous > 0 else 0

        # Order metrics - current period
        total_orders = Order.query.count()
        orders_current = Order.query.filter(Order.created_at >= current_start).count()
        orders_previous = Order.query.filter(
            and_(Order.created_at >= previous_start, Order.created_at < previous_end)
        ).count()
        pending_orders = Order.query.filter_by(status=OrderStatus.PENDING.value).count()

        orders_growth = ((orders_current - orders_previous) / orders_previous * 100) if orders_previous > 0 else 0

        # Revenue metrics - current period
        revenue_current = db.session.query(func.sum(Order.total_amount)).filter(
            Order.created_at >= current_start,
            Order.status != OrderStatus.CANCELLED.value
        ).scalar() or 0

        revenue_previous = db.session.query(func.sum(Order.total_amount)).filter(
            and_(
                Order.created_at >= previous_start,
                Order.created_at < previous_end,
                Order.status != OrderStatus.CANCELLED.value
            )
        ).scalar() or 0

        revenue_growth = ((revenue_current - revenue_previous) / revenue_previous * 100) if revenue_previous > 0 else 0

        avg_order_value_current = revenue_current / orders_current if orders_current > 0 else 0
        avg_order_value_previous = revenue_previous / orders_previous if orders_previous > 0 else 0

        # Product metrics
        total_products = Product.query.filter_by(is_active=True).count()
        low_stock_products = Product.query.filter(
            and_(Product.stock_quantity <= Product.min_stock_level, Product.track_inventory == True)
        ).count()
        out_of_stock = Product.query.filter(
            and_(Product.stock_quantity == 0, Product.track_inventory == True)
        ).count()

        # Delivery metrics
        active_deliveries = Delivery.query.filter(
            Delivery.status.in_([
                DeliveryStatus.ASSIGNED.value,
                DeliveryStatus.PICKED_UP.value,
                DeliveryStatus.IN_TRANSIT.value
            ])
        ).count()

        completed_deliveries_current = Delivery.query.filter(
            and_(
                Delivery.status == DeliveryStatus.DELIVERED.value,
                Delivery.updated_at >= current_start
            )
        ).count()

        failed_deliveries_current = Delivery.query.filter(
            and_(
                Delivery.status == DeliveryStatus.FAILED.value,
                Delivery.updated_at >= current_start
            )
        ).count()

        delivery_success_rate = (completed_deliveries_current / (completed_deliveries_current + failed_deliveries_current) * 100) if (completed_deliveries_current + failed_deliveries_current) > 0 else 0

        # Subscription metrics
        active_subscriptions = Subscription.query.filter_by(status=SubscriptionStatus.ACTIVE.value).count()
        paused_subscriptions = Subscription.query.filter_by(status=SubscriptionStatus.PAUSED.value).count()

        new_subscriptions_current = Subscription.query.filter(
            Subscription.created_at >= current_start
        ).count()

        cancelled_subscriptions_current = Subscription.query.filter(
            and_(
                Subscription.status == SubscriptionStatus.CANCELLED.value,
                Subscription.updated_at >= current_start
            )
        ).count()

        subscription_revenue_current = db.session.query(func.sum(Subscription.billing_amount)).filter(
            Subscription.status == SubscriptionStatus.ACTIVE.value
        ).scalar() or 0

        # Loyalty metrics
        total_loyalty_members = LoyaltyPoints.query.count()
        points_in_circulation = db.session.query(func.sum(LoyaltyPoints.current_balance)).scalar() or 0

        points_earned_current = db.session.query(func.sum(LoyaltyTransaction.points)).filter(
            and_(
                LoyaltyTransaction.created_at >= current_start,
                LoyaltyTransaction.points > 0
            )
        ).scalar() or 0

        points_redeemed_current = abs(db.session.query(func.sum(LoyaltyTransaction.points)).filter(
            and_(
                LoyaltyTransaction.created_at >= current_start,
                LoyaltyTransaction.points < 0
            )
        ).scalar() or 0)

        # ======================
        # CHART DATA
        # ======================

        # Revenue trend chart (daily for month/week, hourly for day, monthly for year)
        revenue_chart = []
        orders_chart = []
        users_chart = []

        if period == 'day':
            # Hourly data for last 24 hours
            for i in range(24):
                hour_start = now - timedelta(hours=24-i)
                hour_end = hour_start + timedelta(hours=1)

                hourly_revenue = db.session.query(func.sum(Order.total_amount)).filter(
                    and_(
                        Order.created_at >= hour_start,
                        Order.created_at < hour_end,
                        Order.status != OrderStatus.CANCELLED.value
                    )
                ).scalar() or 0

                hourly_orders = Order.query.filter(
                    and_(Order.created_at >= hour_start, Order.created_at < hour_end)
                ).count()

                hourly_users = User.query.filter(
                    and_(User.created_at >= hour_start, User.created_at < hour_end)
                ).count()

                revenue_chart.append({
                    'label': hour_start.strftime('%H:00'),
                    'value': float(hourly_revenue)
                })
                orders_chart.append({
                    'label': hour_start.strftime('%H:00'),
                    'value': hourly_orders
                })
                users_chart.append({
                    'label': hour_start.strftime('%H:00'),
                    'value': hourly_users
                })

        elif period == 'year':
            # Monthly data for last 12 months
            for i in range(12):
                month_start = (now - timedelta(days=365)) + timedelta(days=30*i)
                month_end = month_start + timedelta(days=30)

                monthly_revenue = db.session.query(func.sum(Order.total_amount)).filter(
                    and_(
                        Order.created_at >= month_start,
                        Order.created_at < month_end,
                        Order.status != OrderStatus.CANCELLED.value
                    )
                ).scalar() or 0

                monthly_orders = Order.query.filter(
                    and_(Order.created_at >= month_start, Order.created_at < month_end)
                ).count()

                monthly_users = User.query.filter(
                    and_(User.created_at >= month_start, User.created_at < month_end)
                ).count()

                revenue_chart.append({
                    'label': month_start.strftime('%b %Y'),
                    'value': float(monthly_revenue)
                })
                orders_chart.append({
                    'label': month_start.strftime('%b %Y'),
                    'value': monthly_orders
                })
                users_chart.append({
                    'label': month_start.strftime('%b %Y'),
                    'value': monthly_users
                })

        else:
            # Daily data for week/month
            for i in range(chart_days):
                day_start = datetime.combine((today - timedelta(days=chart_days-1-i)), datetime.min.time()).replace(tzinfo=UTC)
                day_end = day_start + timedelta(days=1)

                daily_revenue = db.session.query(func.sum(Order.total_amount)).filter(
                    and_(
                        Order.created_at >= day_start,
                        Order.created_at < day_end,
                        Order.status != OrderStatus.CANCELLED.value
                    )
                ).scalar() or 0

                daily_orders = Order.query.filter(
                    and_(Order.created_at >= day_start, Order.created_at < day_end)
                ).count()

                daily_users = User.query.filter(
                    and_(User.created_at >= day_start, User.created_at < day_end)
                ).count()

                revenue_chart.append({
                    'label': day_start.strftime('%b %d'),
                    'value': float(daily_revenue)
                })
                orders_chart.append({
                    'label': day_start.strftime('%b %d'),
                    'value': daily_orders
                })
                users_chart.append({
                    'label': day_start.strftime('%b %d'),
                    'value': daily_users
                })

        # Order status distribution (pie chart data)
        order_status_distribution = db.session.query(
            Order.status,
            func.count(Order.id).label('count')
        ).filter(
            Order.created_at >= current_start
        ).group_by(Order.status).all()

        order_status_chart = [
            {'label': status.value, 'value': count}
            for status, count in order_status_distribution
        ]

        # Payment method distribution
        payment_method_distribution = db.session.query(
            Payment.payment_method,
            func.count(Payment.id).label('count'),
            func.sum(Payment.amount).label('total')
        ).filter(
            Payment.created_at >= current_start
        ).group_by(Payment.payment_method).all()

        payment_method_chart = [
            {
                'label': method,
                'count': count,
                'total': float(total or 0)
            }
            for method, count, total in payment_method_distribution
        ]

        # ======================
        # TOP PERFORMERS
        # ======================

        # Top 10 products by revenue
        top_products = db.session.query(
            Product.id,
            Product.name,
            func.sum(OrderItem.quantity).label('units_sold'),
            func.sum(OrderItem.unit_price * OrderItem.quantity).label('revenue')
        ).join(OrderItem, OrderItem.product_id == Product.id).join(
            Order, Order.id == OrderItem.order_id
        ).filter(
            and_(
                Order.created_at >= current_start,
                Order.status != OrderStatus.CANCELLED.value
            )
        ).group_by(Product.id, Product.name).order_by(
            func.sum(OrderItem.unit_price * OrderItem.quantity).desc()
        ).limit(10).all()

        top_products_list = [
            {
                'product_id': p.id,
                'product_name': p.name,
                'units_sold': p.units_sold,
                'revenue': float(p.revenue)
            }
            for p in top_products
        ]

        # Top 10 customers by spending
        top_customers = db.session.query(
            User.id,
            func.concat(User.first_name, ' ', User.last_name).label('full_name'),
            User.phone,
            func.count(Order.id).label('order_count'),
            func.sum(Order.total_amount).label('total_spent')
        ).join(Order, Order.user_id == User.id).filter(
            and_(
                Order.created_at >= current_start,
                Order.status != OrderStatus.CANCELLED.value
            )
        ).group_by(User.id, func.concat(User.first_name, ' ', User.last_name), User.phone).order_by(
            func.sum(Order.total_amount).desc()
        ).limit(10).all()

        top_customers_list = [
            {
                'user_id': c.id,
                'name': c.full_name,
                'phone': c.phone,
                'order_count': c.order_count,
                'total_spent': float(c.total_spent)
            }
            for c in top_customers
        ]

        # ======================
        # RECENT ACTIVITY
        # ======================

        recent_orders = Order.query.order_by(Order.created_at.desc()).limit(5).all()
        recent_orders_list = [
            {
                'id': o.id,
                'user_name': o.user.full_name if o.user else 'Unknown',
                'total_amount': float(o.total_amount),
                'status': o.status.value,
                'created_at': o.created_at.isoformat()
            }
            for o in recent_orders
        ]

        recent_users = User.query.order_by(User.created_at.desc()).limit(5).all()
        recent_users_list = [
            {
                'id': u.id,
                'full_name': u.full_name,
                'phone': u.phone,
                'created_at': u.created_at.isoformat()
            }
            for u in recent_users
        ]

        # ======================
        # ALERTS & NOTIFICATIONS
        # ======================

        alerts = []

        if low_stock_products > 0:
            alerts.append({
                'type': 'warning',
                'category': 'inventory',
                'message': f'{low_stock_products} products are running low on stock',
                'action_url': '/admin/products?filter=low_stock'
            })

        if out_of_stock > 0:
            alerts.append({
                'type': 'error',
                'category': 'inventory',
                'message': f'{out_of_stock} products are out of stock',
                'action_url': '/admin/products?filter=out_of_stock'
            })

        if pending_orders > 10:
            alerts.append({
                'type': 'warning',
                'category': 'orders',
                'message': f'{pending_orders} orders are pending processing',
                'action_url': '/admin/orders?status=pending'
            })

        if failed_deliveries_current > 0:
            alerts.append({
                'type': 'error',
                'category': 'delivery',
                'message': f'{failed_deliveries_current} deliveries failed in the current period',
                'action_url': '/admin/deliveries?status=failed'
            })

        # Check for failed payments
        failed_payments = Payment.query.filter(
            and_(
                Payment.status == 'failed',
                Payment.created_at >= current_start
            )
        ).count()

        if failed_payments > 0:
            alerts.append({
                'type': 'warning',
                'category': 'payments',
                'message': f'{failed_payments} payments failed in the current period',
                'action_url': '/admin/payments?status=failed'
            })

        # ======================
        # COMPILE DASHBOARD DATA
        # ======================

        dashboard_data = {
            'overview': {
                'users': {
                    'total': total_users,
                    'new_current_period': new_users_current,
                    'new_previous_period': new_users_previous,
                    'growth_percentage': round(user_growth, 2),
                    'active': active_users
                },
                'orders': {
                    'total': total_orders,
                    'current_period': orders_current,
                    'previous_period': orders_previous,
                    'growth_percentage': round(orders_growth, 2),
                    'pending': pending_orders,
                    'avg_order_value_current': float(avg_order_value_current),
                    'avg_order_value_previous': float(avg_order_value_previous)
                },
                'revenue': {
                    'current_period': float(revenue_current),
                    'previous_period': float(revenue_previous),
                    'growth_percentage': round(revenue_growth, 2),
                    'currency': 'UZS'
                },
                'products': {
                    'total_active': total_products,
                    'low_stock': low_stock_products,
                    'out_of_stock': out_of_stock
                },
                'deliveries': {
                    'active': active_deliveries,
                    'completed_current_period': completed_deliveries_current,
                    'failed_current_period': failed_deliveries_current,
                    'success_rate': round(delivery_success_rate, 2)
                },
                'subscriptions': {
                    'active': active_subscriptions,
                    'paused': paused_subscriptions,
                    'new_current_period': new_subscriptions_current,
                    'cancelled_current_period': cancelled_subscriptions_current,
                    'monthly_revenue': float(subscription_revenue_current)
                },
                'loyalty': {
                    'total_members': total_loyalty_members,
                    'points_in_circulation': points_in_circulation,
                    'points_earned_current_period': points_earned_current,
                    'points_redeemed_current_period': points_redeemed_current
                }
            },
            'charts': {
                'revenue_trend': revenue_chart,
                'orders_trend': orders_chart,
                'users_trend': users_chart,
                'order_status_distribution': order_status_chart,
                'payment_methods': payment_method_chart
            },
            'top_performers': {
                'products': top_products_list,
                'customers': top_customers_list
            },
            'recent_activity': {
                'orders': recent_orders_list,
                'users': recent_users_list
            },
            'alerts': alerts,
            'period': period,
            'date_range': {
                'start': current_start.isoformat(),
                'end': now.isoformat()
            }
        }

        current_app.logger.info(f"Get admin dashboard result: {dashboard_data}")

        return success_response(
            data={
                'dashboard': dashboard_data,
                'timestamp': now.isoformat()
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get admin dashboard error: {e}")
        return internal_error_response('Failed to get admin dashboard')


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
                return validation_error_response('Invalid role value')

        # Apply status filter
        if status:
            try:
                user_status = UserStatus(status)
                query = query.filter_by(status=user_status.value)
            except ValueError:
                return validation_error_response('Invalid status value')
        
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

        return paginated_response(
            items=users_data,
            page=page,
            per_page=per_page,
            total=pagination.total
        )

    except Exception as e:
        current_app.logger.error(f"Get users error: {e}")
        return internal_error_response('Failed to get users')


@admin_bp.route('/users/<int:user_id>', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_users'])
def get_user_details(user_id):
    """Get detailed user information"""
    try:
        user = User.query.get(user_id)
        if not user:
            return not_found_response(resource_type='User')
        
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

        return success_response(data=user_details)

    except Exception as e:
        current_app.logger.error(f"Get user details error: {e}")
        return internal_error_response('Failed to get user details')


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
            return not_found_response(resource_type='User')

        new_status = data.get('status')
        reason = data.get('reason', '')

        # Prevent privilege escalation - operators cannot modify admin/manager accounts
        current_user = g.current_user
        current_role_value = current_user.role.value if hasattr(current_user.role, 'value') else current_user.role
        user_role_value = user.role.value if hasattr(user.role, 'value') else user.role
        if (current_role_value == UserRole.OPERATOR.value and
            user_role_value in [UserRole.ADMIN.value, UserRole.MANAGER.value]):
            return forbidden_response('Insufficient permissions to modify this user')

        # Prevent self-modification of critical status
        if current_user_id == user_id and new_status in ['banned', 'suspended']:
            return validation_error_response('Cannot suspend or ban your own account')

        try:
            user_status = UserStatus(new_status)
        except ValueError:
            return validation_error_response('Invalid status value')
        
        old_status = user.status
        user.status = user_status.value
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
        if user_status.value in [UserStatus.BANNED.value]:
            get_notification_service().send_notification(
                user_id,
                'account_status_changed',
                template_data={
                    'status': new_status,
                    'reason': reason
                }
            )

        return success_response(
            data={'user': serialize_user_admin(user)},
            message='User status updated successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update user status error: {e}")
        return internal_error_response('Failed to update user status')


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
                query = query.filter_by(status=order_status.value)
            except ValueError:
                return validation_error_response('Invalid status value')

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
                return validation_error_response('Invalid start_date format')

        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date)
                query = query.filter(Order.created_at <= end_dt)
            except ValueError:
                return validation_error_response('Invalid end_date format')
        
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
            current_app.logger.info(f"Order: {order}, status: {order.status}, staus.type: {type(order.status)}, status.value: {order.status.value}, status.value.type: {type(order.status.value)}")
            order_data = serialize_order_admin(order)
            current_app.logger.info(f"order_data: {order_data}")
            order_stats = order_statistics.get(order.id, {})
            order_data.update({
                'item_count': order_stats.get('item_count', 0),
                'total_quantity': order_stats.get('total_quantity', 0),
                'payment_count': order_stats.get('payment_count', 0),
                'last_payment_date': order_stats.get('last_payment_date')
            })
            orders_data.append(order_data)

        return paginated_response(
            items=orders_data,
            page=page,
            per_page=per_page,
            total=pagination.total
        )

    except Exception as e:
        current_app.logger.error(f"Get orders error: {e}")
        return internal_error_response('Failed to get orders')


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
            return not_found_response(resource_type='Order')

        new_status = data.get('status')
        notes = data.get('notes', '')

        try:
            order_status = OrderStatus(new_status)
        except ValueError:
            return validation_error_response('Invalid status value')
        
        # Placeholder implementation until admin_service is implemented
        old_status = order.status
        order.status = order_status.value
        order.updated_at = datetime.now(UTC)
        db.session.commit()
        success = True

        if success:
            return success_response(
                data={'order': serialize_order_admin(order)},
                message='Order status updated successfully'
            )
        else:
            return internal_error_response('Failed to update order status')

    except Exception as e:
        current_app.logger.error(f"Update order status error: {e}")
        return internal_error_response('Failed to update order status')


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

        return paginated_response(
            items=[serialize_product_admin(product) for product in pagination.items],
            page=page,
            per_page=per_page,
            total=pagination.total
        )

    except Exception as e:
        current_app.logger.error(f"Get products admin error: {e}")
        return internal_error_response('Failed to get products')


@admin_bp.route('/products', methods=['POST'])
@jwt_required()
@validate_admin_action(['manage_products'])
def create_product():
    """Create a new product"""
    try:
        data = request.get_json()
        current_app.logger.info(f"/admin/products POST data: {data}")
        # Validate required fields
        required_fields = ['name']
        missing_fields = [field for field in required_fields if not data.get(field)]
        if missing_fields:
            return validation_error_response(
                errors={'missing_fields': missing_fields}
            )

        # Map frontend field names to backend field names
        # Frontend sends 'price', backend uses 'base_price'
        base_price = data.get('price') or data.get('base_price')
        if not base_price:
            return validation_error_response(
                errors={'missing_fields': ['price']}
            )

        # Handle category - frontend may send category name or id
        category_id = data.get('category_id')
        if not category_id:
            return validation_error_response(
                errors={'missing_fields': ['category']}
            )

        # Handle size - derive from product name or use default
        size = data.get('size')
        if not size:
            # Try to extract size from product name
            name_lower = data['name'].lower()
            if '19' in name_lower or '19л' in name_lower:
                size = ProductSizeEnum.SIZE_19L
            elif '10' in name_lower or '10л' in name_lower:
                size = ProductSizeEnum.SIZE_10L
            elif '5' in name_lower or '5л' in name_lower:
                size = ProductSizeEnum.SIZE_5L
            else:
                # Default to 1L
                size = ProductSizeEnum.SIZE_19L
        elif isinstance(size, str):
            # Convert string to enum
            try:
                size = ProductSizeEnum(size)
            except ValueError:
                return validation_error_response(
                    errors={'size': f'Size must be one of: {[e.value for e in ProductSizeEnum]}'}
                )

        # Handle status - frontend sends 'active'/'inactive', backend uses is_active boolean
        is_active = data.get('is_active', True)
        if 'status' in data:
            is_active = data['status'] in ['active', True, 'true', 1]

        current_app.logger.info(f"/admin/products POST data.volume: {data.get('volume')}")
        # Create new product
        product = Product(
            name=data['name'],
            description=data.get('description'),
            short_description=data.get('short_description'),
            sku=data.get('sku'),
            base_price=base_price,
            discount_price=data.get('discount_price'),
            category_id=category_id,
            size=size,
            volume=data.get('volume'),
            volume_unit=data.get('volume_unit', 'L'),
            weight=data.get('weight'),
            weight_unit=data.get('weight_unit', 'kg'),
            is_active=is_active,
            is_featured=data.get('is_featured', False),
            requires_prescription=data.get('requires_prescription', False),
            track_inventory=data.get('track_inventory', True),
            stock_quantity=data.get('stock_quantity', 0),
            min_stock_level=data.get('min_stock_level', 0),
            max_stock_level=data.get('max_stock_level', 1000),
            images=data.get('images', []),
            nutrition_facts=data.get('nutrition_facts', {}),
            ingredients=data.get('ingredients'),
            barcode=data.get('barcode'),
            slug=data.get('slug'),
            meta_title=data.get('meta_title'),
            meta_description=data.get('meta_description')
        )

        db.session.add(product)
        db.session.commit()

        return success_response(
            data={'product': serialize_product_admin(product)},
            message='Product created successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create product error: {e}")
        return internal_error_response('Failed to create product')


@admin_bp.route('/products/<int:product_id>', methods=['PUT'])
@jwt_required()
@validate_admin_action(['manage_products'])
def update_product(product_id):
    """Update an existing product"""
    try:
        data = request.get_json()

        product = Product.query.get(product_id)
        if not product:
            return not_found_response(resource_type='Product')

        # Update fields
        if 'name' in data:
            product.name = data['name']
        if 'description' in data:
            product.description = data['description']
        if 'short_description' in data:
            product.short_description = data['short_description']
        if 'sku' in data:
            product.sku = data['sku']

        # Handle price field mapping (frontend sends 'price', backend uses 'base_price')
        if 'price' in data:
            product.base_price = data['price']
        elif 'base_price' in data:
            product.base_price = data['base_price']

        if 'discount_price' in data:
            product.discount_price = data['discount_price']

        # Handle category field mapping (frontend may send category name)
        if 'category' in data and not 'category_id' in data:
            from business_app.models.product import ProductCategory
            category = ProductCategory.query.filter(
                (ProductCategory.name.ilike(data['category'])) |
                (ProductCategory.id == data['category']) if isinstance(data['category'], int) else False
            ).first()
            if category:
                product.category_id = category.id
        elif 'category_id' in data:
            product.category_id = data['category_id']

        # Handle size field
        if 'size' in data:
            from business_app.models.product import ProductSizeEnum
            if isinstance(data['size'], str):
                try:
                    product.size = ProductSizeEnum(data['size'])
                except ValueError:
                    pass  # Keep existing size if invalid
            else:
                product.size = data['size']

        if 'volume' in data:
            product.volume = data['volume']
        if 'volume_unit' in data:
            product.volume_unit = data['volume_unit']
        if 'weight' in data:
            product.weight = data['weight']
        if 'weight_unit' in data:
            product.weight_unit = data['weight_unit']

        # Handle status field mapping (frontend sends 'status': 'active'/'inactive')
        if 'status' in data:
            product.is_active = data['status'] in ['active', True, 'true', 1]
        elif 'is_active' in data:
            product.is_active = data['is_active']

        if 'is_featured' in data:
            product.is_featured = data['is_featured']
        if 'requires_prescription' in data:
            product.requires_prescription = data['requires_prescription']
        if 'track_inventory' in data:
            product.track_inventory = data['track_inventory']
        if 'stock_quantity' in data:
            product.stock_quantity = data['stock_quantity']
        if 'min_stock_level' in data:
            product.min_stock_level = data['min_stock_level']
        if 'max_stock_level' in data:
            product.max_stock_level = data['max_stock_level']
        if 'images' in data:
            product.images = data['images']
        if 'nutrition_facts' in data:
            product.nutrition_facts = data['nutrition_facts']
        if 'ingredients' in data:
            product.ingredients = data['ingredients']
        if 'barcode' in data:
            product.barcode = data['barcode']
        if 'slug' in data:
            product.slug = data['slug']
        if 'meta_title' in data:
            product.meta_title = data['meta_title']
        if 'meta_description' in data:
            product.meta_description = data['meta_description']

        product.updated_at = datetime.now(UTC)
        db.session.commit()

        return success_response(
            data={'product': serialize_product_admin(product)},
            message='Product updated successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update product error: {e}")
        return internal_error_response('Failed to update product')


@admin_bp.route('/products/<int:product_id>', methods=['DELETE'])
@jwt_required()
@validate_admin_action(['manage_products'])
def delete_product(product_id):
    """Delete a product"""
    try:
        product = Product.query.get(product_id)
        if not product:
            return not_found_response(resource_type='Product')

        # Soft delete by setting is_active to False
        product.is_active = False
        product.updated_at = datetime.now(UTC)
        db.session.commit()

        return success_response(
            message='Product deleted successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete product error: {e}")
        return internal_error_response('Failed to delete product')


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
            return not_found_response(resource_type='Product')

        new_stock = data.get('stock_quantity')
        adjustment_reason = data.get('reason', 'Manual adjustment')

        if not isinstance(new_stock, int) or new_stock < 0:
            return validation_error_response('Invalid stock quantity')
        
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

        return success_response(
            data={'product': serialize_product_admin(product)},
            message='Product stock updated successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update product stock error: {e}")
        return internal_error_response('Failed to update product stock')


# ============================================================================
# PRODUCT CATEGORY MANAGEMENT ENDPOINTS
# ============================================================================

@admin_bp.route('/categories', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_products', 'manage_products'])
def get_categories():
    """Get all product categories with filtering and search"""
    try:
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 50)), 100)
        search = request.args.get('search', '').strip()
        is_active = request.args.get('is_active', type=bool)
        sort_by = request.args.get('sort_by', 'sort_order')  # sort_order, name, created_at

        # Build query
        query = ProductCategory.query

        # Apply filters
        if is_active is not None:
            query = query.filter_by(is_active=is_active)

        if search:
            search_term = f"%{search}%"
            query = query.filter(or_(
                ProductCategory.name.ilike(search_term),
                ProductCategory.description.ilike(search_term)
            ))

        # Apply sorting
        if sort_by == 'name':
            query = query.order_by(ProductCategory.name)
        elif sort_by == 'created_at':
            query = query.order_by(ProductCategory.created_at.desc())
        else:  # Default: sort_order
            query = query.order_by(ProductCategory.sort_order, ProductCategory.name)

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize categories with product count
        categories_data = []
        for category in pagination.items:
            product_count = Product.query.filter_by(category_id=category.id, is_active=True).count()
            category_dict = {
                'id': category.id,
                'name': category.name,
                'description': category.description,
                'is_active': category.is_active,
                'sort_order': category.sort_order,
                'icon_url': category.icon_url,
                'product_count': product_count,
                'created_at': category.created_at.isoformat() if category.created_at else None,
                'updated_at': category.updated_at.isoformat() if category.updated_at else None
            }
            categories_data.append(category_dict)

        return paginated_response(
            items=categories_data,
            page=page,
            per_page=per_page,
            total=pagination.total
        )

    except Exception as e:
        current_app.logger.error(f"Get categories error: {e}")
        return internal_error_response('Failed to get categories')


@admin_bp.route('/categories/<int:category_id>', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_products', 'manage_products'])
def get_category(category_id):
    """Get specific category details"""
    try:
        category = ProductCategory.query.get(category_id)

        if not category:
            return not_found_response('Category not found')

        # Get products in this category
        products = Product.query.filter_by(category_id=category_id).all()

        category_data = {
            'id': category.id,
            'name': category.name,
            'description': category.description,
            'is_active': category.is_active,
            'sort_order': category.sort_order,
            'icon_url': category.icon_url,
            'product_count': len(products),
            'active_product_count': len([p for p in products if p.is_active]),
            'created_at': category.created_at.isoformat() if category.created_at else None,
            'updated_at': category.updated_at.isoformat() if category.updated_at else None,
            'products': [{'id': p.id, 'name': p.name, 'sku': p.sku, 'is_active': p.is_active} for p in products[:10]]  # First 10 products
        }

        return success_response(data={'category': category_data})

    except Exception as e:
        current_app.logger.error(f"Get category error: {e}")
        return internal_error_response('Failed to get category')


@admin_bp.route('/categories', methods=['POST'])
@jwt_required()
@validate_admin_action(['manage_products'])
def create_category():
    """Create a new product category"""
    try:
        data = request.get_json()

        # Validate required fields
        if not data.get('name'):
            return validation_error_response('Category name is required')

        # Check if category with same name already exists
        existing = ProductCategory.query.filter_by(name=data['name']).first()
        if existing:
            return validation_error_response('Category with this name already exists')

        # Create new category
        category = ProductCategory(
            name=data['name'],
            description=data.get('description'),
            is_active=data.get('is_active', True),
            sort_order=data.get('sort_order', 0),
            icon_url=data.get('icon_url')
        )

        db.session.add(category)
        db.session.commit()

        current_app.logger.info(f"Category created: {category.name} (ID: {category.id})")

        return success_response(
            data={'category': {
                'id': category.id,
                'name': category.name,
                'description': category.description,
                'is_active': category.is_active,
                'sort_order': category.sort_order,
                'icon_url': category.icon_url
            }},
            message='Category created successfully',
            status_code=201
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create category error: {e}")
        return internal_error_response('Failed to create category')


@admin_bp.route('/categories/<int:category_id>', methods=['PUT'])
@jwt_required()
@validate_admin_action(['manage_products'])
def update_category(category_id):
    """Update a product category"""
    try:
        category = ProductCategory.query.get(category_id)

        if not category:
            return not_found_response('Category not found')

        data = request.get_json()

        # Check if name is being changed to an existing name
        if 'name' in data and data['name'] != category.name:
            existing = ProductCategory.query.filter_by(name=data['name']).first()
            if existing:
                return validation_error_response('Category with this name already exists')

        # Update fields
        if 'name' in data:
            category.name = data['name']
        if 'description' in data:
            category.description = data['description']
        if 'is_active' in data:
            category.is_active = data['is_active']
        if 'sort_order' in data:
            category.sort_order = data['sort_order']
        if 'icon_url' in data:
            category.icon_url = data['icon_url']

        db.session.commit()

        current_app.logger.info(f"Category updated: {category.name} (ID: {category.id})")

        return success_response(
            data={'category': {
                'id': category.id,
                'name': category.name,
                'description': category.description,
                'is_active': category.is_active,
                'sort_order': category.sort_order,
                'icon_url': category.icon_url
            }},
            message='Category updated successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update category error: {e}")
        return internal_error_response('Failed to update category')


@admin_bp.route('/categories/<int:category_id>', methods=['DELETE'])
@jwt_required()
@validate_admin_action(['manage_products'])
def delete_category(category_id):
    """Delete a product category (soft delete by setting inactive)"""
    try:
        category = ProductCategory.query.get(category_id)

        if not category:
            return not_found_response('Category not found')

        # Check if category has products
        product_count = Product.query.filter_by(category_id=category_id).count()

        force_delete = request.args.get('force', 'false').lower() == 'true'

        if product_count > 0 and not force_delete:
            return validation_error_response(
                f'Cannot delete category with {product_count} products. Set force=true to deactivate instead.'
            )

        if force_delete or product_count > 0:
            # Soft delete: just deactivate
            category.is_active = False
            db.session.commit()
            current_app.logger.info(f"Category deactivated: {category.name} (ID: {category.id})")
            return success_response(message=f'Category deactivated (has {product_count} products)')
        else:
            # Hard delete if no products
            category_name = category.name
            db.session.delete(category)
            db.session.commit()
            current_app.logger.info(f"Category deleted: {category_name} (ID: {category_id})")
            return success_response(message=get_translation('api.admin.success.category_deleted'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete category error: {e}")
        return internal_error_response('Failed to delete category')


@admin_bp.route('/categories/<int:category_id>/reorder', methods=['PUT'])
@jwt_required()
@validate_admin_action(['manage_products'])
def reorder_category(category_id):
    """Update category sort order"""
    try:
        category = ProductCategory.query.get(category_id)

        if not category:
            return not_found_response('Category not found')

        data = request.get_json()
        new_sort_order = data.get('sort_order')

        if new_sort_order is None:
            return validation_error_response('sort_order is required')

        category.sort_order = new_sort_order
        db.session.commit()

        return success_response(message=get_translation('api.admin.success.category_order_updated'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Reorder category error: {e}")
        return internal_error_response('Failed to reorder category')


# ==================== DELIVERY TIME SLOT MANAGEMENT ====================

@admin_bp.route('/delivery/time-slots', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_delivery', 'manage_delivery'])
def get_time_slots_admin():
    """Get all delivery time slots with filtering"""
    try:
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 50)), 100)
        is_active = request.args.get('is_active', type=bool)

        # Build query
        query = DeliveryTimeSlot.query

        # Apply filters
        if is_active is not None:
            query = query.filter_by(is_active=is_active)

        # Order by start time
        query = query.order_by(DeliveryTimeSlot.start_time)

        # Paginate
        pagination = query.paginate(
            page=page, per_page=per_page, error_out=False
        )

        # Serialize time slots
        items = []
        for slot in pagination.items:
            items.append({
                'id': slot.id,
                'name': slot.name,
                'start_time': slot.start_time,
                'end_time': slot.end_time,
                'time_range': f"{slot.start_time}-{slot.end_time}",
                'is_active': slot.is_active,
                'max_orders': slot.max_orders,
                'delivery_fee': float(slot.delivery_fee),
                'is_premium': slot.is_premium,
                'premium_fee': float(slot.premium_fee),
                'available_days': slot.available_days
            })

        return paginated_response(
            items=items,
            page=page,
            per_page=per_page,
            total=pagination.total
        )

    except Exception as e:
        current_app.logger.error(f"Get time slots error: {e}")
        return internal_error_response('Failed to get time slots')


@admin_bp.route('/delivery/time-slots/<int:slot_id>', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_delivery', 'manage_delivery'])
def get_time_slot_admin(slot_id):
    """Get a specific time slot"""
    try:
        slot = DeliveryTimeSlot.query.get(slot_id)

        if not slot:
            return not_found_response('Time slot not found')

        return success_response(data={
            'id': slot.id,
            'name': slot.name,
            'start_time': slot.start_time,
            'end_time': slot.end_time,
            'is_active': slot.is_active,
            'max_orders': slot.max_orders,
            'delivery_fee': float(slot.delivery_fee),
            'is_premium': slot.is_premium,
            'premium_fee': float(slot.premium_fee),
            'available_days': slot.available_days
        })

    except Exception as e:
        current_app.logger.error(f"Get time slot error: {e}")
        return internal_error_response('Failed to get time slot')


@admin_bp.route('/delivery/time-slots', methods=['POST'])
@jwt_required()
@validate_admin_action(['manage_delivery'])
def create_time_slot():
    """Create a new delivery time slot"""
    try:
        data = request.get_json()

        # Validate required fields
        required_fields = ['name', 'start_time', 'end_time', 'max_orders', 'delivery_fee']
        for field in required_fields:
            if field not in data:
                return validation_error_response(f'{field} is required')

        # Create time slot
        time_slot = DeliveryTimeSlot(
            name=data['name'],
            start_time=data['start_time'],
            end_time=data['end_time'],
            is_active=data.get('is_active', True),
            max_orders=data['max_orders'],
            delivery_fee=data['delivery_fee'],
            is_premium=data.get('is_premium', False),
            premium_fee=data.get('premium_fee', 0),
            available_days=data.get('available_days', [0, 1, 2, 3, 4, 5, 6])
        )

        db.session.add(time_slot)
        db.session.commit()

        current_app.logger.info(f"Time slot created: {time_slot.name} (ID: {time_slot.id})")

        return created_response(
            message='Time slot created successfully',
            data={'id': time_slot.id}
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create time slot error: {e}")
        return internal_error_response('Failed to create time slot')


@admin_bp.route('/delivery/time-slots/<int:slot_id>', methods=['PUT'])
@jwt_required()
@validate_admin_action(['manage_delivery'])
def update_time_slot(slot_id):
    """Update a delivery time slot"""
    try:
        slot = DeliveryTimeSlot.query.get(slot_id)

        if not slot:
            return not_found_response('Time slot not found')

        data = request.get_json()

        # Update fields
        if 'name' in data:
            slot.name = data['name']
        if 'start_time' in data:
            slot.start_time = data['start_time']
        if 'end_time' in data:
            slot.end_time = data['end_time']
        if 'is_active' in data:
            slot.is_active = data['is_active']
        if 'max_orders' in data:
            slot.max_orders = data['max_orders']
        if 'delivery_fee' in data:
            slot.delivery_fee = data['delivery_fee']
        if 'is_premium' in data:
            slot.is_premium = data['is_premium']
        if 'premium_fee' in data:
            slot.premium_fee = data['premium_fee']
        if 'available_days' in data:
            slot.available_days = data['available_days']

        db.session.commit()

        current_app.logger.info(f"Time slot updated: {slot.name} (ID: {slot_id})")

        return success_response(message=get_translation('api.admin.success.time_slot_updated'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update time slot error: {e}")
        return internal_error_response('Failed to update time slot')


@admin_bp.route('/delivery/time-slots/<int:slot_id>', methods=['DELETE'])
@jwt_required()
@validate_admin_action(['manage_delivery'])
def delete_time_slot(slot_id):
    """Delete a delivery time slot"""
    try:
        slot = DeliveryTimeSlot.query.get(slot_id)

        if not slot:
            return not_found_response('Time slot not found')

        slot_name = slot.name
        db.session.delete(slot)
        db.session.commit()

        current_app.logger.info(f"Time slot deleted: {slot_name} (ID: {slot_id})")

        return success_response(message=get_translation('api.admin.success.time_slot_deleted'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete time slot error: {e}")
        return internal_error_response('Failed to delete time slot')


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

        return paginated_response(
            items=[serialize_delivery_person_admin(person) for person in pagination.items],
            page=page,
            per_page=per_page,
            total=pagination.total
        )

    except Exception as e:
        current_app.logger.error(f"Get delivery personnel error: {e}")
        return internal_error_response('Failed to get delivery personnel')


# ============================================================================
# DELIVERY ROUTE MANAGEMENT ENDPOINTS
# ============================================================================

@admin_bp.route('/delivery-routes', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_delivery', 'manage_delivery'])
def get_delivery_routes():
    """Get delivery routes with filtering and search"""
    try:
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 50)), 100)

        # Filters
        status = request.args.get('status')  # planned, in_progress, completed, cancelled
        delivery_person_id = request.args.get('delivery_person_id', type=int)
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        search = request.args.get('search', '').strip()

        # Build query
        query = DeliveryRoute.query

        # Apply filters
        if status:
            query = query.filter_by(status=status)

        if delivery_person_id:
            query = query.filter_by(delivery_person_id=delivery_person_id)

        if date_from:
            try:
                date_from_dt = datetime.fromisoformat(date_from.replace('Z', '+00:00'))
                query = query.filter(DeliveryRoute.route_date >= date_from_dt)
            except ValueError:
                return validation_error_response('Invalid date_from format')

        if date_to:
            try:
                date_to_dt = datetime.fromisoformat(date_to.replace('Z', '+00:00'))
                query = query.filter(DeliveryRoute.route_date <= date_to_dt)
            except ValueError:
                return validation_error_response('Invalid date_to format')

        if search:
            search_term = f"%{search}%"
            query = query.join(User, DeliveryRoute.delivery_person_id == User.id).filter(or_(
                DeliveryRoute.name.ilike(search_term),
                User.first_name.ilike(search_term),
                User.last_name.ilike(search_term)
            ))

        # Order by date descending
        query = query.order_by(DeliveryRoute.route_date.desc())

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize routes
        routes_data = []
        for route in pagination.items:
            route_dict = route.to_dict()

            # Add order count
            route_dict['order_count'] = len(route.optimized_order) if route.optimized_order else 0

            routes_data.append(route_dict)

        return paginated_response(
            items=routes_data,
            page=page,
            per_page=per_page,
            total=pagination.total
        )

    except Exception as e:
        current_app.logger.error(f"Get delivery routes error: {e}")
        return internal_error_response('Failed to get delivery routes')


@admin_bp.route('/delivery-routes/<int:route_id>', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_delivery', 'manage_delivery'])
def get_delivery_route(route_id):
    """Get detailed delivery route information"""
    try:
        route = DeliveryRoute.query.get(route_id)

        if not route:
            return not_found_response('Delivery route not found')

        route_dict = route.to_dict()

        # Get deliveries for this route
        if route.optimized_order:
            deliveries = Delivery.query.filter(
                Delivery.order_id.in_(route.optimized_order)
            ).all()

            route_dict['deliveries'] = [
                {
                    'id': d.id,
                    'order_id': d.order_id,
                    'tracking_number': d.tracking_number,
                    'status': d.status.value,
                    'scheduled_time_slot': d.scheduled_time_slot,
                    'delivery_address': d.order.delivery_address if d.order else None,
                    'customer_name': d.order.user.full_name if d.order and d.order.user else None,
                    'customer_phone': d.order.user.phone if d.order and d.order.user else None,
                    'delivered_at': d.delivered_at.isoformat() if d.delivered_at else None
                }
                for d in deliveries
            ]
        else:
            route_dict['deliveries'] = []

        return success_response(data={'route': route_dict})

    except Exception as e:
        current_app.logger.error(f"Get delivery route error: {e}")
        return internal_error_response('Failed to get delivery route')


@admin_bp.route('/delivery-routes', methods=['POST'])
@jwt_required()
@validate_admin_action(['manage_delivery'])
@validate_json()
def create_delivery_route():
    """Create new delivery route"""
    try:
        data = request.get_json()

        # Validate required fields
        required_fields = ['name', 'delivery_person_id', 'route_date', 'start_location_lat', 'start_location_lng']
        for field in required_fields:
            if field not in data:
                return validation_error_response(f'Missing required field: {field}')

        # Validate delivery person exists
        delivery_person = User.query.get(data['delivery_person_id'])
        if not delivery_person:
            return not_found_response('Delivery person not found')

        # Parse route date
        try:
            route_date = datetime.fromisoformat(data['route_date'].replace('Z', '+00:00'))
        except ValueError:
            return validation_error_response('Invalid route_date format')

        # Create route
        route = DeliveryRoute(
            name=data['name'],
            delivery_person_id=data['delivery_person_id'],
            route_date=route_date,
            start_location_lat=data['start_location_lat'],
            start_location_lng=data['start_location_lng'],
            optimized_order=data.get('optimized_order', []),
            total_distance_km=data.get('total_distance_km'),
            estimated_duration_minutes=data.get('estimated_duration_minutes'),
            notes=data.get('notes')
        )

        db.session.add(route)
        db.session.commit()

        return success_response(
            data={'route': route.to_dict()},
            message='Delivery route created successfully',
            status_code=201
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create delivery route error: {e}")
        return internal_error_response('Failed to create delivery route')


@admin_bp.route('/delivery-routes/<int:route_id>', methods=['PUT'])
@jwt_required()
@validate_admin_action(['manage_delivery'])
@validate_json()
def update_delivery_route(route_id):
    """Update delivery route"""
    try:
        route = DeliveryRoute.query.get(route_id)

        if not route:
            return not_found_response('Delivery route not found')

        # Don't allow updates to completed routes
        if route.status == 'completed':
            return validation_error_response('Cannot update completed routes')

        data = request.get_json()

        # Update basic fields
        if 'name' in data:
            route.name = data['name']

        if 'delivery_person_id' in data:
            delivery_person = User.query.get(data['delivery_person_id'])
            if not delivery_person:
                return not_found_response('Delivery person not found')
            route.delivery_person_id = data['delivery_person_id']

        if 'route_date' in data:
            try:
                route.route_date = datetime.fromisoformat(data['route_date'].replace('Z', '+00:00'))
            except ValueError:
                return validation_error_response('Invalid route_date format')

        if 'start_location_lat' in data:
            route.start_location_lat = data['start_location_lat']

        if 'start_location_lng' in data:
            route.start_location_lng = data['start_location_lng']

        if 'optimized_order' in data:
            route.optimized_order = data['optimized_order']

        if 'total_distance_km' in data:
            route.total_distance_km = data['total_distance_km']

        if 'estimated_duration_minutes' in data:
            route.estimated_duration_minutes = data['estimated_duration_minutes']

        if 'notes' in data:
            route.notes = data['notes']

        if 'extra_data' in data:
            route.extra_data = data['extra_data']

        db.session.commit()

        return success_response(
            data={'route': route.to_dict()},
            message='Delivery route updated successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update delivery route error: {e}")
        return internal_error_response('Failed to update delivery route')


@admin_bp.route('/delivery-routes/<int:route_id>/status', methods=['PUT'])
@jwt_required()
@validate_admin_action(['manage_delivery'])
@validate_json()
def update_route_status(route_id):
    """Update delivery route status"""
    try:
        route = DeliveryRoute.query.get(route_id)

        if not route:
            return not_found_response('Delivery route not found')

        data = request.get_json()
        new_status = data.get('status')

        if not new_status:
            return validation_error_response('Status is required')

        valid_statuses = ['planned', 'in_progress', 'completed', 'cancelled']
        if new_status not in valid_statuses:
            return validation_error_response(f'Invalid status. Must be one of: {", ".join(valid_statuses)}')

        old_status = route.status

        # Validate status transitions
        if old_status == 'completed' and new_status != 'completed':
            return validation_error_response('Cannot change status of completed route')

        if old_status == 'cancelled' and new_status not in ['planned', 'cancelled']:
            return validation_error_response('Cancelled route can only be set to planned')

        # Update status and timestamps
        route.status = new_status

        if new_status == 'in_progress' and not route.started_at:
            route.started_at = datetime.now(UTC)

        if new_status == 'completed':
            route.completed_at = datetime.now(UTC)

            # Update actual metrics if provided
            if 'actual_distance_km' in data:
                route.actual_distance_km = data['actual_distance_km']

            if 'actual_duration_minutes' in data:
                route.actual_duration_minutes = data['actual_duration_minutes']

            if 'deliveries_completed' in data:
                route.deliveries_completed = data['deliveries_completed']

            if 'deliveries_failed' in data:
                route.deliveries_failed = data['deliveries_failed']

        db.session.commit()

        return success_response(
            data={'route': route.to_dict()},
            message=f'Route status updated to {new_status}'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update route status error: {e}")
        return internal_error_response('Failed to update route status')


@admin_bp.route('/delivery-routes/<int:route_id>', methods=['DELETE'])
@jwt_required()
@validate_admin_action(['manage_delivery'])
def delete_delivery_route(route_id):
    """Delete delivery route"""
    try:
        route = DeliveryRoute.query.get(route_id)

        if not route:
            return not_found_response('Delivery route not found')

        # Don't allow deletion of in-progress or completed routes
        if route.status in ['in_progress', 'completed']:
            return validation_error_response(f'Cannot delete {route.status} routes')

        db.session.delete(route)
        db.session.commit()

        return success_response(message=get_translation('api.admin.success.delivery_route_deleted'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete delivery route error: {e}")
        return internal_error_response('Failed to delete delivery route')


@admin_bp.route('/delivery-routes/analytics', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_delivery'])
def get_delivery_routes_analytics():
    """Get delivery routes analytics"""
    try:
        # Date range
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')

        # Default to last 30 days
        now = datetime.now(UTC)
        start_date = now - timedelta(days=30)
        end_date = now

        if date_from:
            try:
                start_date = datetime.fromisoformat(date_from.replace('Z', '+00:00'))
            except ValueError:
                return validation_error_response('Invalid date_from format')

        if date_to:
            try:
                end_date = datetime.fromisoformat(date_to.replace('Z', '+00:00'))
            except ValueError:
                return validation_error_response('Invalid date_to format')

        # Total routes
        total_routes = DeliveryRoute.query.filter(
            and_(
                DeliveryRoute.route_date >= start_date,
                DeliveryRoute.route_date <= end_date
            )
        ).count()

        # Routes by status
        routes_by_status = db.session.query(
            DeliveryRoute.status,
            func.count(DeliveryRoute.id).label('count')
        ).filter(
            and_(
                DeliveryRoute.route_date >= start_date,
                DeliveryRoute.route_date <= end_date
            )
        ).group_by(DeliveryRoute.status).all()

        status_breakdown = {status: count for status, count in routes_by_status}

        # Average metrics for completed routes
        completed_routes = DeliveryRoute.query.filter(
            and_(
                DeliveryRoute.route_date >= start_date,
                DeliveryRoute.route_date <= end_date,
                DeliveryRoute.status == 'completed'
            )
        ).all()

        total_distance = sum(r.actual_distance_km or 0 for r in completed_routes)
        total_duration = sum(r.actual_duration_minutes or 0 for r in completed_routes)
        total_deliveries = sum(r.deliveries_completed or 0 for r in completed_routes)
        total_failed = sum(r.deliveries_failed or 0 for r in completed_routes)

        avg_distance = total_distance / len(completed_routes) if completed_routes else 0
        avg_duration = total_duration / len(completed_routes) if completed_routes else 0
        avg_deliveries_per_route = total_deliveries / len(completed_routes) if completed_routes else 0
        success_rate = (total_deliveries / (total_deliveries + total_failed) * 100) if (total_deliveries + total_failed) > 0 else 0

        # Top performing delivery personnel
        top_delivery_persons = db.session.query(
            User.id,
            func.concat(User.first_name, ' ', User.last_name).label('full_name'),
            func.count(DeliveryRoute.id).label('route_count'),
            func.sum(DeliveryRoute.deliveries_completed).label('total_deliveries'),
            func.sum(DeliveryRoute.actual_distance_km).label('total_distance')
        ).join(DeliveryRoute, DeliveryRoute.delivery_person_id == User.id).filter(
            and_(
                DeliveryRoute.route_date >= start_date,
                DeliveryRoute.route_date <= end_date,
                DeliveryRoute.status == 'completed'
            )
        ).group_by(User.id, func.concat(User.first_name, ' ', User.last_name)).order_by(
            func.sum(DeliveryRoute.deliveries_completed).desc()
        ).limit(10).all()

        top_performers = [
            {
                'person_id': p.id,
                'person_name': p.full_name,
                'route_count': p.route_count,
                'total_deliveries': p.total_deliveries or 0,
                'total_distance_km': float(p.total_distance or 0)
            }
            for p in top_delivery_persons
        ]

        # Daily route completion trend
        daily_trend = db.session.query(
            func.date(DeliveryRoute.route_date).label('date'),
            func.count(DeliveryRoute.id).label('routes'),
            func.sum(DeliveryRoute.deliveries_completed).label('deliveries')
        ).filter(
            and_(
                DeliveryRoute.route_date >= start_date,
                DeliveryRoute.route_date <= end_date,
                DeliveryRoute.status == 'completed'
            )
        ).group_by(func.date(DeliveryRoute.route_date)).order_by(
            func.date(DeliveryRoute.route_date)
        ).all()

        daily_data = [
            {
                'date': d.date.isoformat(),
                'routes': d.routes,
                'deliveries': d.deliveries or 0
            }
            for d in daily_trend
        ]

        analytics = {
            'summary': {
                'total_routes': total_routes,
                'completed_routes': status_breakdown.get('completed', 0),
                'in_progress_routes': status_breakdown.get('in_progress', 0),
                'planned_routes': status_breakdown.get('planned', 0),
                'cancelled_routes': status_breakdown.get('cancelled', 0)
            },
            'performance': {
                'avg_distance_km': round(avg_distance, 2),
                'avg_duration_minutes': round(avg_duration, 2),
                'avg_deliveries_per_route': round(avg_deliveries_per_route, 2),
                'success_rate': round(success_rate, 2),
                'total_deliveries_completed': total_deliveries,
                'total_deliveries_failed': total_failed
            },
            'status_breakdown': status_breakdown,
            'top_performers': top_performers,
            'daily_trend': daily_data,
            'date_range': {
                'start': start_date.isoformat(),
                'end': end_date.isoformat()
            }
        }

        return success_response(data=analytics)

    except Exception as e:
        current_app.logger.error(f"Get delivery routes analytics error: {e}")
        return internal_error_response('Failed to get delivery routes analytics')


# ============================================================================
# SUBSCRIPTION MANAGEMENT ENDPOINTS
# ============================================================================

@admin_bp.route('/subscriptions', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_orders', 'manage_orders'])
def get_subscriptions():
    """Get all subscriptions with filtering and search"""
    try:
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 50)), 100)
        search = request.args.get('search', '').strip()
        status = request.args.get('status')  # active, paused, cancelled, expired
        user_id = request.args.get('user_id', type=int)
        sort_by = request.args.get('sort_by', 'created_at')  # created_at, next_billing_date, billing_amount

        # Build query
        query = Subscription.query

        # Apply filters
        if status:
            query = query.filter_by(status=status)

        if user_id:
            query = query.filter_by(user_id=user_id)

        if search:
            search_term = f"%{search}%"
            query = query.join(User).filter(or_(
                Subscription.subscription_number.ilike(search_term),
                Subscription.name.ilike(search_term),
                User.first_name.ilike(search_term),
                User.last_name.ilike(search_term),
                User.email.ilike(search_term)
            ))

        # Apply sorting
        if sort_by == 'next_billing_date':
            query = query.order_by(Subscription.next_billing_date.desc())
        elif sort_by == 'billing_amount':
            query = query.order_by(Subscription.billing_amount.desc())
        else:  # Default: created_at
            query = query.order_by(Subscription.created_at.desc())

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize subscriptions
        subscriptions_data = []
        for sub in pagination.items:
            sub_dict = {
                'id': sub.id,
                'subscription_number': sub.subscription_number,
                'user_id': sub.user_id,
                'user_name': f"{sub.user.first_name} {sub.user.last_name or ''}".strip(),
                'user_email': sub.user.email,
                'status': sub.status,
                'name': sub.name,
                'description': sub.description,
                'billing_cycle': sub.billing_cycle.value if hasattr(sub.billing_cycle, 'value') else str(sub.billing_cycle),
                'billing_amount': float(sub.billing_amount),
                'next_billing_date': sub.next_billing_date.isoformat() if sub.next_billing_date else None,
                'delivery_frequency': sub.delivery_frequency.value if hasattr(sub.delivery_frequency, 'value') else str(sub.delivery_frequency),
                'auto_renew': sub.auto_renew,
                'paused_at': sub.paused_at.isoformat() if sub.paused_at else None,
                'pause_reason': sub.pause_reason,
                'resume_date': sub.resume_date.isoformat() if sub.resume_date else None,
                'total_orders_generated': sub.total_orders_generated,
                'total_amount_billed': float(sub.total_amount_billed),
                'items_count': len(sub.subscription_items),
                'created_at': sub.created_at.isoformat() if sub.created_at else None
            }
            subscriptions_data.append(sub_dict)

        return paginated_response(
            items=subscriptions_data,
            page=page,
            per_page=per_page,
            total=pagination.total
        )

    except Exception as e:
        current_app.logger.error(f"Get subscriptions error: {e}")
        return internal_error_response('Failed to get subscriptions')


@admin_bp.route('/subscriptions/<int:subscription_id>', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_orders', 'manage_orders'])
def get_subscription(subscription_id):
    """Get detailed subscription information"""
    try:
        subscription = Subscription.query.get(subscription_id)

        if not subscription:
            return not_found_response('Subscription not found')

        # Get subscription items
        items = []
        for item in subscription.subscription_items:
            items.append({
                'id': item.id,
                'product_id': item.product_id,
                'product_name': item.product.name if item.product else None,
                'quantity': item.quantity,
                'unit_price': float(item.unit_price),
                'subtotal': float(item.subtotal)
            })

        # Get recent orders
        recent_orders = Order.query.filter_by(subscription_id=subscription_id)\
            .order_by(Order.created_at.desc()).limit(10).all()
        orders = [{
            'id': o.id,
            'order_number': o.order_number,
            'status': o.status,
            'total_amount': float(o.total_amount),
            'created_at': o.created_at.isoformat() if o.created_at else None
        } for o in recent_orders]

        # Get subscription logs
        logs = []
        for log in subscription.subscription_logs[-10:]:  # Last 10 logs
            logs.append({
                'id': log.id,
                'action': log.action,
                'details': log.details,
                'created_at': log.created_at.isoformat() if log.created_at else None
            })

        subscription_data = {
            'id': subscription.id,
            'subscription_number': subscription.subscription_number,
            'user': {
                'id': subscription.user.id,
                'name': f"{subscription.user.first_name} {subscription.user.last_name or ''}".strip(),
                'email': subscription.user.email,
                'phone': subscription.user.phone
            },
            'status': subscription.status,
            'name': subscription.name,
            'description': subscription.description,
            'billing_cycle': subscription.billing_cycle.value if hasattr(subscription.billing_cycle, 'value') else str(subscription.billing_cycle),
            'billing_amount': float(subscription.billing_amount),
            'next_billing_date': subscription.next_billing_date.isoformat() if subscription.next_billing_date else None,
            'last_billing_date': subscription.last_billing_date.isoformat() if subscription.last_billing_date else None,
            'delivery_frequency': subscription.delivery_frequency.value if hasattr(subscription.delivery_frequency, 'value') else str(subscription.delivery_frequency),
            'delivery_day_of_week': subscription.delivery_day_of_week,
            'delivery_day_of_month': subscription.delivery_day_of_month,
            'delivery_time_slot': subscription.delivery_time_slot,
            'delivery_address_id': subscription.delivery_address_id,
            'start_date': subscription.start_date.isoformat() if subscription.start_date else None,
            'end_date': subscription.end_date.isoformat() if subscription.end_date else None,
            'auto_renew': subscription.auto_renew,
            'payment_method': subscription.payment_method,
            'auto_payment': subscription.auto_payment,
            'paused_at': subscription.paused_at.isoformat() if subscription.paused_at else None,
            'pause_reason': subscription.pause_reason,
            'resume_date': subscription.resume_date.isoformat() if subscription.resume_date else None,
            'total_orders_generated': subscription.total_orders_generated,
            'total_amount_billed': float(subscription.total_amount_billed),
            'failed_billing_attempts': subscription.failed_billing_attempts,
            'last_successful_billing': subscription.last_successful_billing.isoformat() if subscription.last_successful_billing else None,
            'discount_percentage': subscription.discount_percentage,
            'loyalty_points_multiplier': subscription.loyalty_points_multiplier,
            'items': items,
            'recent_orders': orders,
            'recent_logs': logs,
            'created_at': subscription.created_at.isoformat() if subscription.created_at else None,
            'updated_at': subscription.updated_at.isoformat() if subscription.updated_at else None
        }

        return success_response(data={'subscription': subscription_data})

    except Exception as e:
        current_app.logger.error(f"Get subscription error: {e}")
        return internal_error_response('Failed to get subscription')


@admin_bp.route('/subscriptions/<int:subscription_id>/pause', methods=['POST'])
@jwt_required()
@validate_admin_action(['manage_orders'])
def pause_subscription_admin(subscription_id):
    """Pause a subscription (admin action)"""
    try:
        data = request.get_json() or {}
        pause_reason = data.get('pause_reason', 'Paused by administrator')
        resume_date = data.get('resume_date')

        subscription_service = SubscriptionService()

        # Convert resume_date string to datetime if provided
        resume_dt = None
        if resume_date:
            try:
                resume_dt = datetime.fromisoformat(resume_date.replace('Z', '+00:00'))
            except:
                return validation_error_response('Invalid resume_date format. Use ISO format.')

        # Pause subscription (service handles user_id validation, we pass None for admin override)
        paused_sub = subscription_service.pause_subscription(
            subscription_id=subscription_id,
            user_id=None,  # Admin can pause any subscription
            reason=pause_reason,
            resume_date=resume_dt
        )

        current_app.logger.info(f"Subscription paused by admin: {subscription_id}")

        return success_response(
            data={'subscription_number': paused_sub.subscription_number, 'status': paused_sub.status},
            message='Subscription paused successfully'
        )

    except Exception as e:
        current_app.logger.error(f"Pause subscription error: {e}")
        return internal_error_response(f'Failed to pause subscription: {str(e)}')


@admin_bp.route('/subscriptions/<int:subscription_id>/resume', methods=['POST'])
@jwt_required()
@validate_admin_action(['manage_orders'])
def resume_subscription_admin(subscription_id):
    """Resume a paused subscription (admin action)"""
    try:
        subscription_service = SubscriptionService()

        # Resume subscription
        resumed_sub = subscription_service.resume_subscription(
            subscription_id=subscription_id,
            user_id=None  # Admin can resume any subscription
        )

        current_app.logger.info(f"Subscription resumed by admin: {subscription_id}")

        return success_response(
            data={'subscription_number': resumed_sub.subscription_number, 'status': resumed_sub.status},
            message='Subscription resumed successfully'
        )

    except Exception as e:
        current_app.logger.error(f"Resume subscription error: {e}")
        return internal_error_response(f'Failed to resume subscription: {str(e)}')


@admin_bp.route('/subscriptions/<int:subscription_id>/cancel', methods=['POST'])
@jwt_required()
@validate_admin_action(['manage_orders'])
def cancel_subscription_admin(subscription_id):
    """Cancel a subscription (admin action)"""
    try:
        data = request.get_json() or {}
        cancellation_reason = data.get('cancellation_reason', 'Cancelled by administrator')

        subscription_service = SubscriptionService()

        # Cancel subscription
        cancelled_sub = subscription_service.cancel_subscription(
            subscription_id=subscription_id,
            user_id=None,  # Admin can cancel any subscription
            reason=cancellation_reason
        )

        current_app.logger.info(f"Subscription cancelled by admin: {subscription_id}")

        return success_response(
            data={'subscription_number': cancelled_sub.subscription_number, 'status': cancelled_sub.status},
            message='Subscription cancelled successfully'
        )

    except Exception as e:
        current_app.logger.error(f"Cancel subscription error: {e}")
        return internal_error_response(f'Failed to cancel subscription: {str(e)}')


@admin_bp.route('/subscriptions/<int:subscription_id>/billing/process', methods=['POST'])
@jwt_required()
@validate_admin_action(['manage_orders'])
def process_subscription_billing_admin(subscription_id):
    """Manually trigger billing for a subscription"""
    try:
        subscription_service = SubscriptionService()

        # Process billing
        result = subscription_service.process_subscription_billing(subscription_id)

        current_app.logger.info(f"Subscription billing processed by admin: {subscription_id}")

        return success_response(
            data=result,
            message='Billing processed successfully'
        )

    except Exception as e:
        current_app.logger.error(f"Process subscription billing error: {e}")
        return internal_error_response(f'Failed to process billing: {str(e)}')


@admin_bp.route('/subscriptions/analytics', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_orders'])
def get_subscription_analytics():
    """Get subscription analytics"""
    try:
        # Parse date range
        start_date_str = request.args.get('start_date')
        end_date_str = request.args.get('end_date')

        start_date = None
        end_date = None

        if start_date_str:
            try:
                start_date = datetime.fromisoformat(start_date_str.replace('Z', '+00:00'))
            except:
                return validation_error_response('Invalid start_date format')

        if end_date_str:
            try:
                end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
            except:
                return validation_error_response('Invalid end_date format')

        subscription_service = SubscriptionService()
        analytics = subscription_service.get_subscription_analytics(
            start_date=start_date,
            end_date=end_date
        )

        return success_response(data={'analytics': analytics})

    except Exception as e:
        current_app.logger.error(f"Get subscription analytics error: {e}")
        return internal_error_response('Failed to get subscription analytics')


# ============================================================================
# PAYMENT MANAGEMENT ENDPOINTS
# ============================================================================

@admin_bp.route('/payments', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_orders', 'manage_orders'])
def get_payments():
    """Get all payments with filtering and search"""
    try:
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 50)), 100)
        search = request.args.get('search', '').strip()
        status = request.args.get('status')  # pending, completed, failed, refunded
        payment_method = request.args.get('payment_method')
        user_id = request.args.get('user_id', type=int)
        order_id = request.args.get('order_id', type=int)
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        sort_by = request.args.get('sort_by', 'created_at')  # created_at, amount

        # Build query
        query = Payment.query

        # Apply filters
        if status:
            query = query.filter_by(status=status)

        if payment_method:
            query = query.filter_by(payment_method=payment_method)

        if user_id:
            query = query.filter_by(user_id=user_id)

        if order_id:
            query = query.filter_by(order_id=order_id)

        # Date range filter
        if start_date:
            try:
                start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
                query = query.filter(Payment.created_at >= start_dt)
            except:
                return validation_error_response('Invalid start_date format')

        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
                query = query.filter(Payment.created_at <= end_dt)
            except:
                return validation_error_response('Invalid end_date format')

        # Search
        if search:
            search_term = f"%{search}%"
            query = query.join(User).filter(or_(
                Payment.payment_id.ilike(search_term),
                Payment.provider_transaction_id.ilike(search_term),
                User.first_name.ilike(search_term),
                User.last_name.ilike(search_term),
                User.email.ilike(search_term)
            ))

        # Apply sorting
        if sort_by == 'amount':
            query = query.order_by(Payment.amount.desc())
        else:  # Default: created_at
            query = query.order_by(Payment.created_at.desc())

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize payments
        payments_data = []
        for payment in pagination.items:
            payment_dict = {
                'id': payment.id,
                'payment_id': payment.payment_id,
                'user_id': payment.user_id,
                'user_name': f"{payment.user.first_name} {payment.user.last_name or ''}".strip() if payment.user else None,
                'user_email': payment.user.email if payment.user else None,
                'order_id': payment.order_id,
                'order_number': payment.order.order_number if payment.order else None,
                'subscription_id': payment.subscription_id,
                'amount': float(payment.amount),
                'currency': payment.currency,
                'payment_method': payment.payment_method,
                'status': payment.status,
                'provider_transaction_id': payment.provider_transaction_id,
                'description': payment.description,
                'failure_reason': payment.failure_reason,
                'webhook_processed': payment.webhook_processed,
                'created_at': payment.created_at.isoformat() if payment.created_at else None
            }
            payments_data.append(payment_dict)

        # Calculate summary statistics
        total_amount = db.session.query(func.sum(Payment.amount)).filter(
            Payment.id.in_([p.id for p in pagination.items])
        ).scalar() or 0

        return paginated_response(
            items=payments_data,
            page=page,
            per_page=per_page,
            total=pagination.total,
            additional_meta={'total_amount': float(total_amount)}
        )

    except Exception as e:
        current_app.logger.error(f"Get payments error: {e}")
        return internal_error_response('Failed to get payments')


@admin_bp.route('/payments/<int:payment_id>', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_orders', 'manage_orders'])
def get_payment(payment_id):
    """Get detailed payment information including transactions"""
    try:
        payment = Payment.query.get(payment_id)

        if not payment:
            return not_found_response('Payment not found')

        # Get payment transactions
        transactions = PaymentTransaction.query.filter_by(payment_id=payment_id)\
            .order_by(PaymentTransaction.created_at.desc()).all()

        transaction_data = []
        for txn in transactions:
            transaction_data.append({
                'id': txn.id,
                'transaction_type': txn.transaction_type,
                'amount': float(txn.amount),
                'currency': txn.currency,
                'status': txn.status,
                'provider_transaction_id': txn.provider_transaction_id,
                'provider_reference': txn.provider_reference,
                'success': txn.success,
                'failure_reason': txn.failure_reason,
                'ip_address': txn.ip_address,
                'created_at': txn.created_at.isoformat() if txn.created_at else None
            })

        payment_data = {
            'id': payment.id,
            'payment_id': payment.payment_id,
            'user': {
                'id': payment.user.id,
                'name': f"{payment.user.first_name} {payment.user.last_name or ''}".strip(),
                'email': payment.user.email,
                'phone': payment.user.phone
            } if payment.user else None,
            'order': {
                'id': payment.order.id,
                'order_number': payment.order.order_number,
                'status': payment.order.status,
                'total_amount': float(payment.order.total_amount)
            } if payment.order else None,
            'subscription_id': payment.subscription_id,
            'amount': float(payment.amount),
            'currency': payment.currency,
            'payment_method': payment.payment_method,
            'status': payment.status,
            'provider_transaction_id': payment.provider_transaction_id,
            'provider_data': payment.provider_data,
            'payment_link': payment.payment_link,
            'payment_link_expires_at': payment.payment_link_expires_at.isoformat() if payment.payment_link_expires_at else None,
            'webhook_processed': payment.webhook_processed,
            'webhook_attempts': payment.webhook_attempts,
            'description': payment.description,
            'callback_url': payment.callback_url,
            'failure_reason': payment.failure_reason,
            'transactions': transaction_data,
            'created_at': payment.created_at.isoformat() if payment.created_at else None,
            'updated_at': payment.updated_at.isoformat() if payment.updated_at else None
        }

        return success_response(data={'payment': payment_data})

    except Exception as e:
        current_app.logger.error(f"Get payment error: {e}")
        return internal_error_response('Failed to get payment')


@admin_bp.route('/payments/<int:payment_id>/refund', methods=['POST'])
@jwt_required()
@validate_admin_action(['manage_orders'])
def refund_payment(payment_id):
    """Process a payment refund"""
    try:
        data = request.get_json() or {}
        refund_amount = data.get('amount')
        reason = data.get('reason', 'Refund requested by administrator')

        if not refund_amount:
            return validation_error_response('Refund amount is required')

        payment = Payment.query.get(payment_id)
        if not payment:
            return not_found_response('Payment not found')

        # Validate refund amount
        try:
            refund_amount = int(refund_amount)
            if refund_amount <= 0:
                return validation_error_response('Refund amount must be greater than 0')
            if refund_amount > payment.amount:
                return validation_error_response('Refund amount cannot exceed payment amount')
        except ValueError:
            return validation_error_response('Invalid refund amount')

        # Process refund using payment service
        payment_service = PaymentService()
        success = payment_service.process_refund(
            payment_id=payment_id,
            amount=refund_amount,
            reason=reason
        )

        if success:
            current_app.logger.info(f"Payment refunded by admin: {payment_id}, Amount: {refund_amount}")
            return success_response(
                data={'payment_id': payment.payment_id, 'refund_amount': refund_amount},
                message='Refund processed successfully'
            )
        else:
            return internal_error_response('Refund processing failed')

    except Exception as e:
        current_app.logger.error(f"Refund payment error: {e}")
        return internal_error_response(f'Failed to process refund: {str(e)}')


@admin_bp.route('/payments/<int:payment_id>/status', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_orders'])
def get_payment_status(payment_id):
    """Get current payment status from provider"""
    try:
        payment_service = PaymentService()
        status_data = payment_service.get_payment_status(payment_id)

        return success_response(data={'status': status_data})

    except Exception as e:
        current_app.logger.error(f"Get payment status error: {e}")
        return internal_error_response(f'Failed to get payment status: {str(e)}')


@admin_bp.route('/payments/analytics', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_orders'])
def get_payment_analytics():
    """Get payment analytics and statistics"""
    try:
        # Parse date range
        start_date_str = request.args.get('start_date')
        end_date_str = request.args.get('end_date')

        # Default to last 30 days if no dates provided
        end_date = datetime.now(UTC)
        start_date = end_date - timedelta(days=30)

        if start_date_str:
            try:
                start_date = datetime.fromisoformat(start_date_str.replace('Z', '+00:00'))
            except:
                return validation_error_response('Invalid start_date format')

        if end_date_str:
            try:
                end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
            except:
                return validation_error_response('Invalid end_date format')

        # Query payments in date range
        payments_query = Payment.query.filter(
            Payment.created_at.between(start_date, end_date)
        )

        # Total payments
        total_payments = payments_query.count()

        # Payments by status
        status_counts = db.session.query(
            Payment.status,
            func.count(Payment.id).label('count'),
            func.sum(Payment.amount).label('total_amount')
        ).filter(Payment.created_at.between(start_date, end_date))\
         .group_by(Payment.status).all()

        status_breakdown = {}
        for status, count, total in status_counts:
            status_breakdown[status] = {
                'count': count,
                'total_amount': float(total) if total else 0
            }

        # Payments by method
        method_counts = db.session.query(
            Payment.payment_method,
            func.count(Payment.id).label('count'),
            func.sum(Payment.amount).label('total_amount')
        ).filter(Payment.created_at.between(start_date, end_date))\
         .group_by(Payment.payment_method).all()

        method_breakdown = {}
        for method, count, total in method_counts:
            method_breakdown[method] = {
                'count': count,
                'total_amount': float(total) if total else 0
            }

        # Total revenue
        total_revenue = db.session.query(func.sum(Payment.amount))\
            .filter(Payment.status == 'completed')\
            .filter(Payment.created_at.between(start_date, end_date))\
            .scalar() or 0

        # Refunded amount
        refunded_amount = db.session.query(func.sum(PaymentTransaction.amount))\
            .join(Payment)\
            .filter(PaymentTransaction.transaction_type == 'refund')\
            .filter(PaymentTransaction.success == True)\
            .filter(Payment.created_at.between(start_date, end_date))\
            .scalar() or 0

        # Failed payments
        failed_payments = payments_query.filter(Payment.status == 'failed').count()

        # Success rate
        completed_payments = payments_query.filter(Payment.status == 'completed').count()
        success_rate = (completed_payments / total_payments * 100) if total_payments > 0 else 0

        analytics = {
            'period': {
                'start_date': start_date.isoformat(),
                'end_date': end_date.isoformat()
            },
            'totals': {
                'total_payments': total_payments,
                'completed_payments': completed_payments,
                'failed_payments': failed_payments,
                'total_revenue': float(total_revenue),
                'refunded_amount': float(refunded_amount),
                'net_revenue': float(total_revenue - refunded_amount),
                'success_rate': round(success_rate, 2)
            },
            'by_status': status_breakdown,
            'by_method': method_breakdown
        }

        return success_response(data={'analytics': analytics})

    except Exception as e:
        current_app.logger.error(f"Get payment analytics error: {e}")
        return internal_error_response('Failed to get payment analytics')


@admin_bp.route('/payments/transactions', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_orders'])
def get_payment_transactions():
    """Get all payment transactions with filtering"""
    try:
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 50)), 100)
        transaction_type = request.args.get('transaction_type')  # charge, refund, capture, cancel
        status = request.args.get('status')  # success, failed, pending
        payment_id = request.args.get('payment_id', type=int)

        # Build query
        query = PaymentTransaction.query

        # Apply filters
        if transaction_type:
            query = query.filter_by(transaction_type=transaction_type)

        if status:
            query = query.filter_by(status=status)

        if payment_id:
            query = query.filter_by(payment_id=payment_id)

        # Order by most recent
        query = query.order_by(PaymentTransaction.created_at.desc())

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize transactions
        transactions_data = []
        for txn in pagination.items:
            txn_dict = {
                'id': txn.id,
                'payment_id': txn.payment_id,
                'transaction_type': txn.transaction_type,
                'amount': float(txn.amount),
                'currency': txn.currency,
                'status': txn.status,
                'provider_transaction_id': txn.provider_transaction_id,
                'provider_reference': txn.provider_reference,
                'success': txn.success,
                'failure_reason': txn.failure_reason,
                'ip_address': txn.ip_address,
                'created_at': txn.created_at.isoformat() if txn.created_at else None
            }
            transactions_data.append(txn_dict)

        return paginated_response(
            items=transactions_data,
            page=page,
            per_page=per_page,
            total=pagination.total
        )

    except Exception as e:
        current_app.logger.error(f"Get payment transactions error: {e}")
        return internal_error_response('Failed to get payment transactions')


# ============================================================================
# REVIEW MODERATION ENDPOINTS
# ============================================================================

@admin_bp.route('/reviews', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_products', 'manage_products'])
def get_reviews():
    """Get all reviews with filtering for moderation"""
    try:
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 50)), 100)
        search = request.args.get('search', '').strip()
        is_approved = request.args.get('is_approved', type=bool)
        is_featured = request.args.get('is_featured', type=bool)
        rating = request.args.get('rating', type=int)
        product_id = request.args.get('product_id', type=int)
        user_id = request.args.get('user_id', type=int)
        pending_only = request.args.get('pending_only', 'false').lower() == 'true'
        sort_by = request.args.get('sort_by', 'created_at')  # created_at, rating, helpful_count

        # Build query
        query = Review.query

        # Apply filters
        if pending_only:
            query = query.filter_by(is_approved=False)
        elif is_approved is not None:
            query = query.filter_by(is_approved=is_approved)

        if is_featured is not None:
            query = query.filter_by(is_featured=is_featured)

        if rating:
            query = query.filter_by(rating=rating)

        if product_id:
            query = query.filter_by(product_id=product_id)

        if user_id:
            query = query.filter_by(user_id=user_id)

        # Search
        if search:
            search_term = f"%{search}%"
            query = query.join(User).join(Product).filter(or_(
                Review.title.ilike(search_term),
                Review.comment.ilike(search_term),
                User.first_name.ilike(search_term),
                User.last_name.ilike(search_term),
                Product.name.ilike(search_term)
            ))

        # Apply sorting
        if sort_by == 'rating':
            query = query.order_by(Review.rating.desc())
        elif sort_by == 'helpful_count':
            query = query.order_by(Review.helpful_count.desc())
        else:  # Default: created_at
            query = query.order_by(Review.created_at.desc())

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize reviews
        reviews_data = []
        for review in pagination.items:
            review_dict = {
                'id': review.id,
                'user_id': review.user_id,
                'user_name': f"{review.user.first_name} {review.user.last_name or ''}".strip() if review.user else None,
                'product_id': review.product_id,
                'product_name': review.product.name if review.product else None,
                'order_id': review.order_id,
                'rating': review.rating,
                'title': review.title,
                'comment': review.comment,
                'is_approved': review.is_approved,
                'is_featured': review.is_featured,
                'moderator_notes': review.moderator_notes,
                'helpful_count': review.helpful_count,
                'photos': review.photos,
                'created_at': review.created_at.isoformat() if review.created_at else None
            }
            reviews_data.append(review_dict)

        # Statistics
        total_pending = Review.query.filter_by(is_approved=False).count()
        total_featured = Review.query.filter_by(is_featured=True).count()

        return paginated_response(
            items=reviews_data,
            page=page,
            per_page=per_page,
            total=pagination.total,
            additional_meta={
                'total_pending': total_pending,
                'total_featured': total_featured
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get reviews error: {e}")
        return internal_error_response('Failed to get reviews')


@admin_bp.route('/reviews/<int:review_id>', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_products', 'manage_products'])
def get_review(review_id):
    """Get detailed review information"""
    try:
        review = Review.query.get(review_id)

        if not review:
            return not_found_response('Review not found')

        # Get product info
        product = review.product
        product_info = None
        if product:
            # Get product's average rating
            avg_rating = db.session.query(func.avg(Review.rating))\
                .filter(Review.product_id == product.id, Review.is_approved == True)\
                .scalar() or 0

            product_info = {
                'id': product.id,
                'name': product.name,
                'average_rating': round(float(avg_rating), 2),
                'total_reviews': Review.query.filter_by(product_id=product.id, is_approved=True).count()
            }

        # Get user info
        user = review.user
        user_info = None
        if user:
            user_reviews_count = Review.query.filter_by(user_id=user.id).count()
            user_approved_reviews = Review.query.filter_by(user_id=user.id, is_approved=True).count()

            user_info = {
                'id': user.id,
                'name': f"{user.first_name} {user.last_name or ''}".strip(),
                'email': user.email,
                'phone': user.phone,
                'total_reviews': user_reviews_count,
                'approved_reviews': user_approved_reviews
            }

        review_data = {
            'id': review.id,
            'user': user_info,
            'product': product_info,
            'order_id': review.order_id,
            'rating': review.rating,
            'title': review.title,
            'comment': review.comment,
            'is_approved': review.is_approved,
            'is_featured': review.is_featured,
            'moderator_notes': review.moderator_notes,
            'helpful_count': review.helpful_count,
            'photos': review.photos,
            'created_at': review.created_at.isoformat() if review.created_at else None,
            'updated_at': review.updated_at.isoformat() if review.updated_at else None
        }

        return success_response(data={'review': review_data})

    except Exception as e:
        current_app.logger.error(f"Get review error: {e}")
        return internal_error_response('Failed to get review')


@admin_bp.route('/reviews/<int:review_id>/approve', methods=['POST'])
@jwt_required()
@validate_admin_action(['manage_products'])
def approve_review(review_id):
    """Approve a review"""
    try:
        data = request.get_json() or {}
        moderator_notes = data.get('moderator_notes')

        review_service = ReviewService()

        # Use the moderate_review method
        updated_review = review_service.moderate_review(
            review_id=review_id,
            is_approved=True,
            moderator_notes=moderator_notes,
            admin_user_id=get_jwt_identity()
        )

        current_app.logger.info(f"Review approved by admin: {review_id}")

        return success_response(
            data={'review_id': updated_review.id, 'is_approved': updated_review.is_approved},
            message='Review approved successfully'
        )

    except Exception as e:
        current_app.logger.error(f"Approve review error: {e}")
        return internal_error_response(f'Failed to approve review: {str(e)}')


@admin_bp.route('/reviews/<int:review_id>/reject', methods=['POST'])
@jwt_required()
@validate_admin_action(['manage_products'])
def reject_review(review_id):
    """Reject a review"""
    try:
        data = request.get_json() or {}
        moderator_notes = data.get('moderator_notes', 'Rejected by administrator')

        review_service = ReviewService()

        # Use the moderate_review method
        updated_review = review_service.moderate_review(
            review_id=review_id,
            is_approved=False,
            moderator_notes=moderator_notes,
            admin_user_id=get_jwt_identity()
        )

        current_app.logger.info(f"Review rejected by admin: {review_id}")

        return success_response(
            data={'review_id': updated_review.id, 'is_approved': updated_review.is_approved},
            message='Review rejected successfully'
        )

    except Exception as e:
        current_app.logger.error(f"Reject review error: {e}")
        return internal_error_response(f'Failed to reject review: {str(e)}')


@admin_bp.route('/reviews/<int:review_id>/feature', methods=['POST'])
@jwt_required()
@validate_admin_action(['manage_products'])
def feature_review(review_id):
    """Feature or unfeature a review"""
    try:
        data = request.get_json() or {}
        is_featured = data.get('is_featured', True)

        review = Review.query.get(review_id)
        if not review:
            return not_found_response('Review not found')

        # Only approved reviews can be featured
        if is_featured and not review.is_approved:
            return validation_error_response('Only approved reviews can be featured')

        review.is_featured = is_featured
        db.session.commit()

        current_app.logger.info(f"Review {'featured' if is_featured else 'unfeatured'} by admin: {review_id}")

        return success_response(
            data={'review_id': review.id, 'is_featured': review.is_featured},
            message=f"Review {'featured' if is_featured else 'unfeatured'} successfully"
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Feature review error: {e}")
        return internal_error_response('Failed to feature review')


@admin_bp.route('/reviews/<int:review_id>', methods=['DELETE'])
@jwt_required()
@validate_admin_action(['manage_products'])
def delete_review(review_id):
    """Delete a review"""
    try:
        review_service = ReviewService()
        admin_user_id = get_jwt_identity()

        # The service expects user_id for ownership check, but admin can delete any review
        # We'll need to pass the review's user_id or modify the service
        review = Review.query.get(review_id)
        if not review:
            return not_found_response('Review not found')

        success = review_service.delete_review(
            review_id=review_id,
            user_id=review.user_id,
            is_admin=True
        )

        if success:
            current_app.logger.info(f"Review deleted by admin: {review_id}")
            return success_response(message=get_translation('api.admin.success.review_deleted'))
        else:
            return internal_error_response('Failed to delete review')

    except Exception as e:
        current_app.logger.error(f"Delete review error: {e}")
        return internal_error_response(f'Failed to delete review: {str(e)}')


@admin_bp.route('/reviews/bulk-approve', methods=['POST'])
@jwt_required()
@validate_admin_action(['manage_products'])
def bulk_approve_reviews():
    """Bulk approve multiple reviews"""
    try:
        data = request.get_json()
        review_ids = data.get('review_ids', [])
        moderator_notes = data.get('moderator_notes')

        if not review_ids:
            return validation_error_response('review_ids is required')

        review_service = ReviewService()
        approved_count = 0
        failed_count = 0

        for review_id in review_ids:
            try:
                review_service.moderate_review(
                    review_id=review_id,
                    is_approved=True,
                    moderator_notes=moderator_notes,
                    admin_user_id=get_jwt_identity()
                )
                approved_count += 1
            except Exception as e:
                current_app.logger.error(f"Failed to approve review {review_id}: {e}")
                failed_count += 1

        current_app.logger.info(f"Bulk approval completed: {approved_count} approved, {failed_count} failed")

        return success_response(
            data={'approved': approved_count, 'failed': failed_count},
            message=f'Bulk approval completed: {approved_count} approved, {failed_count} failed'
        )

    except Exception as e:
        current_app.logger.error(f"Bulk approve reviews error: {e}")
        return internal_error_response('Failed to bulk approve reviews')


@admin_bp.route('/reviews/analytics', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_products'])
def get_review_analytics():
    """Get review analytics and statistics"""
    try:
        # Parse date range
        start_date_str = request.args.get('start_date')
        end_date_str = request.args.get('end_date')

        # Default to last 30 days if no dates provided
        end_date = datetime.now(UTC)
        start_date = end_date - timedelta(days=30)

        if start_date_str:
            try:
                start_date = datetime.fromisoformat(start_date_str.replace('Z', '+00:00'))
            except:
                return validation_error_response('Invalid start_date format')

        if end_date_str:
            try:
                end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
            except:
                return validation_error_response('Invalid end_date format')

        # Total reviews
        total_reviews = Review.query.filter(Review.created_at.between(start_date, end_date)).count()

        # Reviews by approval status
        approved_reviews = Review.query.filter(
            Review.created_at.between(start_date, end_date),
            Review.is_approved == True
        ).count()

        pending_reviews = Review.query.filter(
            Review.created_at.between(start_date, end_date),
            Review.is_approved == False
        ).count()

        featured_reviews = Review.query.filter(
            Review.created_at.between(start_date, end_date),
            Review.is_featured == True
        ).count()

        # Average rating
        avg_rating = db.session.query(func.avg(Review.rating))\
            .filter(Review.created_at.between(start_date, end_date))\
            .filter(Review.is_approved == True)\
            .scalar() or 0

        # Reviews by rating
        rating_breakdown = db.session.query(
            Review.rating,
            func.count(Review.id).label('count')
        ).filter(Review.created_at.between(start_date, end_date))\
         .group_by(Review.rating).all()

        rating_counts = {rating: count for rating, count in rating_breakdown}

        # Top reviewed products
        top_products = db.session.query(
            Product.id,
            Product.name,
            func.count(Review.id).label('review_count'),
            func.avg(Review.rating).label('avg_rating')
        ).join(Review)\
         .filter(Review.created_at.between(start_date, end_date))\
         .filter(Review.is_approved == True)\
         .group_by(Product.id, Product.name)\
         .order_by(desc('review_count'))\
         .limit(10).all()

        top_products_data = [{
            'product_id': p.id,
            'product_name': p.name,
            'review_count': p.review_count,
            'average_rating': round(float(p.avg_rating), 2) if p.avg_rating else 0
        } for p in top_products]

        analytics = {
            'period': {
                'start_date': start_date.isoformat(),
                'end_date': end_date.isoformat()
            },
            'totals': {
                'total_reviews': total_reviews,
                'approved_reviews': approved_reviews,
                'pending_reviews': pending_reviews,
                'featured_reviews': featured_reviews,
                'average_rating': round(float(avg_rating), 2),
                'approval_rate': round((approved_reviews / total_reviews * 100), 2) if total_reviews > 0 else 0
            },
            'rating_breakdown': rating_counts,
            'top_products': top_products_data
        }

        return success_response(data={'analytics': analytics})

    except Exception as e:
        current_app.logger.error(f"Get review analytics error: {e}")
        return internal_error_response('Failed to get review analytics')


# ============================================================================
# CAMPAIGN MANAGEMENT ENDPOINTS
# ============================================================================

@admin_bp.route('/campaigns', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_campaigns', 'manage_campaigns'])
def get_promotional_campaigns():
    """
    Get promotional campaigns with filtering

    Query Parameters:
        - page: Page number (default: 1)
        - per_page: Items per page (default: 20)
        - is_active: Filter by active status
        - campaign_type: Filter by type (discount, loyalty_bonus, free_delivery)
        - search: Search in name and description
        - valid_only: Show only currently valid campaigns
    """
    try:
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 20)), 100)

        # Build query
        query = PromotionalCampaign.query

        # Active filter
        is_active = request.args.get('is_active')
        if is_active is not None:
            is_active_bool = is_active.lower() == 'true'
            query = query.filter_by(is_active=is_active_bool)

        # Campaign type filter
        campaign_type = request.args.get('campaign_type')
        if campaign_type:
            query = query.filter_by(campaign_type=campaign_type)

        # Search
        search = request.args.get('search')
        if search:
            query = query.filter(
                or_(
                    PromotionalCampaign.name.ilike(f'%{search}%'),
                    PromotionalCampaign.description.ilike(f'%{search}%'),
                    PromotionalCampaign.promo_code.ilike(f'%{search}%')
                )
            )

        # Valid only filter
        valid_only = request.args.get('valid_only')
        if valid_only and valid_only.lower() == 'true':
            now = datetime.now(UTC)
            query = query.filter(
                PromotionalCampaign.is_active == True,
                PromotionalCampaign.start_date <= now
            ).filter(
                or_(
                    PromotionalCampaign.end_date.is_(None),
                    PromotionalCampaign.end_date >= now
                )
            )

        # Order by creation date (newest first)
        query = query.order_by(PromotionalCampaign.created_at.desc())

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize campaigns
        language = get_current_language()
        campaigns_data = []
        for campaign in pagination.items:
            campaign_data = campaign.to_dict(language=language)
            campaign_data.update({
                'total_uses': campaign.total_uses,
                'total_discount_given': float(campaign.total_discount_given) if campaign.total_discount_given else 0,
                'total_revenue_generated': float(campaign.total_revenue_generated) if campaign.total_revenue_generated else 0
            })
            campaigns_data.append(campaign_data)

        return paginated_response(
            items=campaigns_data,
            total=pagination.total,
            page=page,
            per_page=per_page
        )

    except Exception as e:
        current_app.logger.error(f"Get promotional campaigns error: {e}")
        return internal_error_response('Failed to get campaigns')


@admin_bp.route('/campaigns/<int:campaign_id>', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_campaigns', 'manage_campaigns'])
def get_campaign_detail(campaign_id):
    """Get detailed information about a specific campaign"""
    try:
        campaign = PromotionalCampaign.query.get(campaign_id)

        if not campaign:
            return not_found_response('Campaign not found')

        language = get_current_language()
        campaign_data = campaign.to_dict(language=language, include_all_translations=True)

        # Add usage statistics
        from business_app.models.analytics import CampaignUsage

        usage_stats = {
            'total_uses': campaign.total_uses,
            'total_discount_given': float(campaign.total_discount_given) if campaign.total_discount_given else 0,
            'total_revenue_generated': float(campaign.total_revenue_generated) if campaign.total_revenue_generated else 0,
            'unique_customers': CampaignUsage.query.filter_by(campaign_id=campaign_id).distinct(CampaignUsage.user_id).count(),
            'usage_limit': campaign.usage_limit,
            'usage_limit_per_customer': campaign.usage_limit_per_customer,
            'remaining_uses': campaign.usage_limit - campaign.total_uses if campaign.usage_limit else None
        }

        campaign_data['usage_stats'] = usage_stats

        return success_response(data={'campaign': campaign_data})

    except Exception as e:
        current_app.logger.error(f"Get campaign detail error: {e}")
        return internal_error_response('Failed to get campaign detail')


@admin_bp.route('/campaigns', methods=['POST'])
@jwt_required()
@validate_admin_action(['manage_campaigns'])
@validate_json(['name', 'campaign_type', 'start_date'])
def create_campaign():
    """
    Create a new promotional campaign

    Request Body:
        - name: Campaign name
        - description: Campaign description
        - campaign_type: Type (discount, loyalty_bonus, free_delivery)
        - discount_type: Discount type (percentage, fixed, buy_x_get_y)
        - discount_value: Discount value
        - min_order_value: Minimum order value
        - max_discount_amount: Maximum discount amount
        - start_date: Start date (ISO format)
        - end_date: End date (ISO format, optional)
        - usage_limit: Total usage limit (optional)
        - usage_limit_per_customer: Per customer limit (default: 1)
        - promo_code: Promo code (optional, will be generated if not provided)
        - target_all_customers: Target all customers
        - target_new_customers: Target new customers only
        - target_vip_customers: Target VIP customers only
        - target_segments: List of customer segment IDs
        - is_active: Active status (default: true)
        - translations: Multilingual content
    """
    try:
        data = request.get_json()

        # Validate campaign type
        valid_types = ['discount', 'loyalty_bonus', 'free_delivery']
        campaign_type = data.get('campaign_type')
        if campaign_type not in valid_types:
            return validation_error_response(f'Invalid campaign_type. Must be one of: {", ".join(valid_types)}')

        # Validate discount fields for discount campaigns
        if campaign_type == 'discount':
            if not data.get('discount_type') or not data.get('discount_value'):
                return validation_error_response('discount_type and discount_value required for discount campaigns')

        # Parse dates
        start_date = datetime.fromisoformat(data['start_date'].replace('Z', '+00:00'))
        end_date = None
        if data.get('end_date'):
            end_date = datetime.fromisoformat(data['end_date'].replace('Z', '+00:00'))

        # Generate promo code if not provided
        promo_code = data.get('promo_code')
        if not promo_code and campaign_type == 'discount':
            import random
            import string
            promo_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

        # Check for duplicate promo code
        if promo_code:
            existing = PromotionalCampaign.query.filter_by(promo_code=promo_code).first()
            if existing:
                return validation_error_response(f'Promo code "{promo_code}" already exists')

        # Create campaign
        campaign = PromotionalCampaign(
            name=data.get('name'),
            description=data.get('description'),
            campaign_type=campaign_type,
            discount_type=data.get('discount_type'),
            discount_value=Decimal(str(data.get('discount_value'))) if data.get('discount_value') else None,
            min_order_value=Decimal(str(data.get('min_order_value'))) if data.get('min_order_value') else None,
            max_discount_amount=Decimal(str(data.get('max_discount_amount'))) if data.get('max_discount_amount') else None,
            start_date=start_date,
            end_date=end_date,
            usage_limit=data.get('usage_limit'),
            usage_limit_per_customer=data.get('usage_limit_per_customer', 1),
            promo_code=promo_code,
            target_all_customers=data.get('target_all_customers', False),
            target_new_customers=data.get('target_new_customers', False),
            target_vip_customers=data.get('target_vip_customers', False),
            target_segments=data.get('target_segments', []),
            is_active=data.get('is_active', True)
        )

        db.session.add(campaign)
        db.session.flush()

        # Handle translations
        if data.get('translations'):
            campaign.set_translations(data['translations'])

        db.session.commit()

        language = get_current_language()
        return created_response(
            data={'campaign': campaign.to_dict(language=language)},
            message='Campaign created successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create campaign error: {e}")
        import traceback
        current_app.logger.error(traceback.format_exc())
        return internal_error_response('Failed to create campaign')


@admin_bp.route('/campaigns/<int:campaign_id>', methods=['PUT'])
@jwt_required()
@validate_admin_action(['manage_campaigns'])
def update_campaign(campaign_id):
    """Update an existing campaign"""
    try:
        campaign = PromotionalCampaign.query.get(campaign_id)

        if not campaign:
            return not_found_response('Campaign not found')

        data = request.get_json()

        # Update basic fields
        if 'name' in data:
            campaign.name = data['name']
        if 'description' in data:
            campaign.description = data['description']
        if 'discount_type' in data:
            campaign.discount_type = data['discount_type']
        if 'discount_value' in data:
            campaign.discount_value = Decimal(str(data['discount_value']))
        if 'min_order_value' in data:
            campaign.min_order_value = Decimal(str(data['min_order_value']))
        if 'max_discount_amount' in data:
            campaign.max_discount_amount = Decimal(str(data['max_discount_amount']))
        if 'usage_limit' in data:
            campaign.usage_limit = data['usage_limit']
        if 'usage_limit_per_customer' in data:
            campaign.usage_limit_per_customer = data['usage_limit_per_customer']
        if 'is_active' in data:
            campaign.is_active = data['is_active']
        if 'target_all_customers' in data:
            campaign.target_all_customers = data['target_all_customers']
        if 'target_new_customers' in data:
            campaign.target_new_customers = data['target_new_customers']
        if 'target_vip_customers' in data:
            campaign.target_vip_customers = data['target_vip_customers']
        if 'target_segments' in data:
            campaign.target_segments = data['target_segments']

        # Update dates
        if 'start_date' in data:
            campaign.start_date = datetime.fromisoformat(data['start_date'].replace('Z', '+00:00'))
        if 'end_date' in data:
            campaign.end_date = datetime.fromisoformat(data['end_date'].replace('Z', '+00:00')) if data['end_date'] else None

        # Update promo code (check for duplicates)
        if 'promo_code' in data and data['promo_code'] != campaign.promo_code:
            existing = PromotionalCampaign.query.filter_by(promo_code=data['promo_code']).first()
            if existing:
                return validation_error_response(f'Promo code "{data["promo_code"]}" already exists')
            campaign.promo_code = data['promo_code']

        # Handle translations
        if data.get('translations'):
            campaign.set_translations(data['translations'])

        db.session.commit()

        language = get_current_language()
        return success_response(
            data={'campaign': campaign.to_dict(language=language)},
            message='Campaign updated successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update campaign error: {e}")
        return internal_error_response('Failed to update campaign')


@admin_bp.route('/campaigns/<int:campaign_id>', methods=['DELETE'])
@jwt_required()
@validate_admin_action(['manage_campaigns'])
def delete_campaign(campaign_id):
    """Delete or deactivate a campaign"""
    try:
        campaign = PromotionalCampaign.query.get(campaign_id)

        if not campaign:
            return not_found_response('Campaign not found')

        # Check if campaign has been used
        if campaign.total_uses > 0:
            # Don't delete, just deactivate
            campaign.is_active = False
            db.session.commit()
            return success_response(message=get_translation('api.admin.success.campaign_deactivated'))

        # Safe to delete
        db.session.delete(campaign)
        db.session.commit()

        return success_response(message=get_translation('api.admin.success.campaign_deleted'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete campaign error: {e}")
        return internal_error_response('Failed to delete campaign')


@admin_bp.route('/campaigns/<int:campaign_id>/toggle', methods=['POST'])
@jwt_required()
@validate_admin_action(['manage_campaigns'])
def toggle_campaign(campaign_id):
    """Toggle campaign active status"""
    try:
        campaign = PromotionalCampaign.query.get(campaign_id)

        if not campaign:
            return not_found_response('Campaign not found')

        campaign.is_active = not campaign.is_active
        db.session.commit()

        status = 'activated' if campaign.is_active else 'deactivated'
        return success_response(
            data={'is_active': campaign.is_active},
            message=f'Campaign {status} successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Toggle campaign error: {e}")
        return internal_error_response('Failed to toggle campaign')


@admin_bp.route('/campaigns/<int:campaign_id>/usage', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_campaigns', 'view_reports'])
def get_campaign_usage(campaign_id):
    """
    Get campaign usage history

    Query Parameters:
        - page: Page number
        - per_page: Items per page
    """
    try:
        campaign = PromotionalCampaign.query.get(campaign_id)

        if not campaign:
            return not_found_response('Campaign not found')

        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 20)), 100)

        from business_app.models.analytics import CampaignUsage

        # Get usage records
        usage_query = CampaignUsage.query.filter_by(campaign_id=campaign_id)
        pagination = usage_query.order_by(CampaignUsage.created_at.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )

        usage_data = []
        for usage in pagination.items:
            # Get user info
            user = User.query.get(usage.user_id)
            usage_item = {
                'id': usage.id,
                'user_id': usage.user_id,
                'user_name': user.name if user else None,
                'user_email': user.email if user else None,
                'order_id': usage.order_id,
                'created_at': usage.created_at.isoformat() if usage.created_at else None
            }
            usage_data.append(usage_item)

        return paginated_response(
            items=usage_data,
            total=pagination.total,
            page=page,
            per_page=per_page
        )

    except Exception as e:
        current_app.logger.error(f"Get campaign usage error: {e}")
        return internal_error_response('Failed to get campaign usage')


# ============================================================================
# PRICE RULE MANAGEMENT ENDPOINTS
# ============================================================================

@admin_bp.route('/price-rules', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_products', 'manage_products'])
def get_price_rules():
    """
    Get all price rules with filtering

    Query Parameters:
        - page: Page number (default: 1)
        - per_page: Items per page (default: 20)
        - product_id: Filter by product ID
        - rule_type: Filter by rule type (bulk_discount, vip_discount, etc.)
        - is_active: Filter by active status
        - valid_only: Show only currently valid rules
        - search: Search in name and description
    """
    try:
        from business_app.models.product import PriceRule
        from business_app.utils.constants import PriceRuleType

        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 20)), 100)

        # Build query
        query = PriceRule.query

        # Product filter
        product_id = request.args.get('product_id', type=int)
        if product_id:
            query = query.filter_by(product_id=product_id)

        # Rule type filter
        rule_type = request.args.get('rule_type')
        if rule_type:
            try:
                query = query.filter_by(rule_type=PriceRuleType(rule_type))
            except ValueError:
                return validation_error_response(f'Invalid rule_type: {rule_type}')

        # Active filter
        is_active = request.args.get('is_active')
        if is_active is not None:
            is_active_bool = is_active.lower() == 'true'
            query = query.filter_by(is_active=is_active_bool)

        # Valid only filter
        valid_only = request.args.get('valid_only')
        if valid_only and valid_only.lower() == 'true':
            now = datetime.now(UTC)
            query = query.filter(
                PriceRule.is_active == True
            ).filter(
                or_(
                    PriceRule.valid_from.is_(None),
                    PriceRule.valid_from <= now
                )
            ).filter(
                or_(
                    PriceRule.valid_until.is_(None),
                    PriceRule.valid_until >= now
                )
            )

        # Search
        search = request.args.get('search')
        if search:
            query = query.filter(
                or_(
                    PriceRule.name.ilike(f'%{search}%'),
                    PriceRule.description.ilike(f'%{search}%')
                )
            )

        # Sort by product and creation date
        query = query.order_by(PriceRule.product_id.asc(), PriceRule.created_at.desc())

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize price rules
        language = get_current_language()
        rules_data = []
        for rule in pagination.items:
            rule_dict = rule.to_dict(language=language) if hasattr(rule, 'to_dict') else {
                'id': rule.id,
                'product_id': rule.product_id,
                'rule_type': rule.rule_type.value if rule.rule_type else None,
                'name': rule.name,
                'description': rule.description,
                'min_quantity': rule.min_quantity,
                'max_quantity': rule.max_quantity,
                'min_order_value': float(rule.min_order_value) if rule.min_order_value else None,
                'customer_type': rule.customer_type,
                'discount_type': rule.discount_type,
                'discount_value': float(rule.discount_value) if rule.discount_value else None,
                'is_active': rule.is_active,
                'valid_from': rule.valid_from.isoformat() if rule.valid_from else None,
                'valid_until': rule.valid_until.isoformat() if rule.valid_until else None,
                'created_at': rule.created_at.isoformat() if rule.created_at else None
            }

            # Add product info
            product = Product.query.get(rule.product_id)
            if product:
                rule_dict['product'] = {
                    'id': product.id,
                    'name': product.name,
                    'price': float(product.price) if product.price else None
                }

            rules_data.append(rule_dict)

        return paginated_response(
            items=rules_data,
            total=pagination.total,
            page=page,
            per_page=per_page
        )

    except Exception as e:
        current_app.logger.error(f"Get price rules error: {e}")
        import traceback
        current_app.logger.error(traceback.format_exc())
        return internal_error_response('Failed to get price rules')


@admin_bp.route('/price-rules/<int:rule_id>', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_products', 'manage_products'])
def get_price_rule_detail(rule_id):
    """Get detailed information about a specific price rule"""
    try:
        from business_app.models.product import PriceRule

        rule = PriceRule.query.get(rule_id)

        if not rule:
            return not_found_response('Price rule not found')

        language = get_current_language()
        rule_data = rule.to_dict(language=language, include_all_translations=True) if hasattr(rule, 'to_dict') else {
            'id': rule.id,
            'product_id': rule.product_id,
            'rule_type': rule.rule_type.value if rule.rule_type else None,
            'name': rule.name,
            'description': rule.description,
            'min_quantity': rule.min_quantity,
            'max_quantity': rule.max_quantity,
            'min_order_value': float(rule.min_order_value) if rule.min_order_value else None,
            'customer_type': rule.customer_type,
            'discount_type': rule.discount_type,
            'discount_value': float(rule.discount_value) if rule.discount_value else None,
            'is_active': rule.is_active,
            'valid_from': rule.valid_from.isoformat() if rule.valid_from else None,
            'valid_until': rule.valid_until.isoformat() if rule.valid_until else None,
            'created_at': rule.created_at.isoformat() if rule.created_at else None
        }

        # Add product info
        product = Product.query.get(rule.product_id)
        if product:
            rule_data['product'] = {
                'id': product.id,
                'name': product.name,
                'price': float(product.price) if product.price else None,
                'is_active': product.is_active
            }

        return success_response(data={'price_rule': rule_data})

    except Exception as e:
        current_app.logger.error(f"Get price rule detail error: {e}")
        return internal_error_response('Failed to get price rule detail')


@admin_bp.route('/price-rules', methods=['POST'])
@jwt_required()
@validate_admin_action(['manage_products'])
@validate_json(['product_id', 'rule_type', 'name', 'discount_value'])
def create_price_rule():
    """
    Create a new price rule

    Request Body:
        - product_id: Product ID
        - rule_type: Rule type (bulk_discount, vip_discount, loyalty_discount, seasonal_discount, time_based)
        - name: Rule name
        - description: Rule description
        - min_quantity: Minimum quantity (default: 1)
        - max_quantity: Maximum quantity (optional)
        - min_order_value: Minimum order value (optional)
        - customer_type: Customer type filter (vip, regular, etc.)
        - discount_type: Discount type (percentage or fixed)
        - discount_value: Discount value
        - is_active: Active status (default: true)
        - valid_from: Valid from date (ISO format, optional)
        - valid_until: Valid until date (ISO format, optional)
        - translations: Multilingual content
    """
    try:
        from business_app.models.product import PriceRule
        from business_app.utils.constants import PriceRuleType

        data = request.get_json()

        # Validate product exists
        product = Product.query.get(data.get('product_id'))
        if not product:
            return not_found_response('Product not found')

        # Validate rule type
        valid_rule_types = [t.value for t in PriceRuleType]
        rule_type_str = data.get('rule_type')
        if rule_type_str not in valid_rule_types:
            return validation_error_response(f'Invalid rule_type. Must be one of: {", ".join(valid_rule_types)}')

        rule_type = PriceRuleType(rule_type_str)

        # Validate discount type
        discount_type = data.get('discount_type', 'percentage')
        if discount_type not in ['percentage', 'fixed']:
            return validation_error_response('discount_type must be "percentage" or "fixed"')

        # Create price rule
        rule = PriceRule(
            product_id=data.get('product_id'),
            rule_type=rule_type,
            name=data.get('name'),
            description=data.get('description'),
            min_quantity=data.get('min_quantity', 1),
            max_quantity=data.get('max_quantity'),
            min_order_value=Decimal(str(data.get('min_order_value'))) if data.get('min_order_value') else None,
            customer_type=data.get('customer_type'),
            discount_type=discount_type,
            discount_value=Decimal(str(data.get('discount_value'))),
            is_active=data.get('is_active', True)
        )

        # Set validity dates
        if data.get('valid_from'):
            rule.valid_from = datetime.fromisoformat(data['valid_from'].replace('Z', '+00:00'))
        if data.get('valid_until'):
            rule.valid_until = datetime.fromisoformat(data['valid_until'].replace('Z', '+00:00'))

        db.session.add(rule)
        db.session.flush()

        # Handle translations if supported
        if hasattr(rule, 'set_translations') and data.get('translations'):
            rule.set_translations(data['translations'])

        db.session.commit()

        language = get_current_language()
        rule_data = {
            'id': rule.id,
            'product_id': rule.product_id,
            'rule_type': rule.rule_type.value,
            'name': rule.name,
            'description': rule.description,
            'discount_value': float(rule.discount_value),
            'is_active': rule.is_active
        }

        return created_response(
            data={'price_rule': rule_data},
            message='Price rule created successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create price rule error: {e}")
        import traceback
        current_app.logger.error(traceback.format_exc())
        return internal_error_response('Failed to create price rule')


@admin_bp.route('/price-rules/<int:rule_id>', methods=['PUT'])
@jwt_required()
@validate_admin_action(['manage_products'])
def update_price_rule(rule_id):
    """Update an existing price rule"""
    try:
        from business_app.models.product import PriceRule

        rule = PriceRule.query.get(rule_id)

        if not rule:
            return not_found_response('Price rule not found')

        data = request.get_json()

        # Update basic fields
        if 'name' in data:
            rule.name = data['name']
        if 'description' in data:
            rule.description = data['description']
        if 'min_quantity' in data:
            rule.min_quantity = data['min_quantity']
        if 'max_quantity' in data:
            rule.max_quantity = data['max_quantity']
        if 'min_order_value' in data:
            rule.min_order_value = Decimal(str(data['min_order_value'])) if data['min_order_value'] else None
        if 'customer_type' in data:
            rule.customer_type = data['customer_type']
        if 'discount_type' in data:
            if data['discount_type'] not in ['percentage', 'fixed']:
                return validation_error_response('discount_type must be "percentage" or "fixed"')
            rule.discount_type = data['discount_type']
        if 'discount_value' in data:
            rule.discount_value = Decimal(str(data['discount_value']))
        if 'is_active' in data:
            rule.is_active = data['is_active']

        # Update validity dates
        if 'valid_from' in data:
            rule.valid_from = datetime.fromisoformat(data['valid_from'].replace('Z', '+00:00')) if data['valid_from'] else None
        if 'valid_until' in data:
            rule.valid_until = datetime.fromisoformat(data['valid_until'].replace('Z', '+00:00')) if data['valid_until'] else None

        # Handle translations if supported
        if hasattr(rule, 'set_translations') and data.get('translations'):
            rule.set_translations(data['translations'])

        db.session.commit()

        language = get_current_language()
        rule_data = {
            'id': rule.id,
            'product_id': rule.product_id,
            'name': rule.name,
            'discount_value': float(rule.discount_value),
            'is_active': rule.is_active
        }

        return success_response(
            data={'price_rule': rule_data},
            message='Price rule updated successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update price rule error: {e}")
        return internal_error_response('Failed to update price rule')


@admin_bp.route('/price-rules/<int:rule_id>', methods=['DELETE'])
@jwt_required()
@validate_admin_action(['manage_products'])
def delete_price_rule(rule_id):
    """Delete a price rule"""
    try:
        from business_app.models.product import PriceRule

        rule = PriceRule.query.get(rule_id)

        if not rule:
            return not_found_response('Price rule not found')

        db.session.delete(rule)
        db.session.commit()

        return success_response(message=get_translation('api.admin.success.price_rule_deleted'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete price rule error: {e}")
        return internal_error_response('Failed to delete price rule')


@admin_bp.route('/price-rules/types', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_products', 'manage_products'])
def get_price_rule_types():
    """Get list of available price rule types"""
    try:
        from business_app.utils.constants import PriceRuleType

        rule_types = [
            {
                'value': rule_type.value,
                'label': rule_type.value.replace('_', ' ').title(),
                'description': _get_rule_type_description(rule_type.value)
            }
            for rule_type in PriceRuleType
        ]

        return success_response(data={'rule_types': rule_types})

    except Exception as e:
        current_app.logger.error(f"Get price rule types error: {e}")
        return internal_error_response('Failed to get price rule types')


def _get_rule_type_description(rule_type):
    """Get human-readable description for rule type"""
    descriptions = {
        'bulk_discount': 'Discount based on quantity purchased',
        'vip_discount': 'Special discount for VIP customers',
        'loyalty_discount': 'Discount based on loyalty tier',
        'seasonal_discount': 'Time-limited seasonal promotion',
        'time_based': 'Discount active during specific time periods'
    }
    return descriptions.get(rule_type, '')


@admin_bp.route('/reports/generate', methods=['POST'])
@jwt_required()
@rate_limit(max_requests=5, window_seconds=1800, per='user')  # 5 reports per 30 minutes per user
@validate_admin_action(['view_reports', 'generate_reports'])
@validate_json(['report_type'])
def generate_report():
    """
    Generate administrative report with various formats

    Request Body:
        - report_type: Type of report (sales_summary, customer_report, etc.)
        - date_range: {start_date: ISO, end_date: ISO}
        - filters: Additional filters specific to report type
        - format: Output format (json, csv, excel) - default: json
        - include_charts: Include chart data (default: true)
    """
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        report_type = data.get('report_type')
        date_range = data.get('date_range', {})
        filters = data.get('filters', {})
        format_type = data.get('format', 'json')
        include_charts = data.get('include_charts', True)

        # Validate report type
        valid_reports = [
            'sales_summary', 'customer_report', 'product_performance',
            'delivery_report', 'financial_summary', 'user_activity',
            'inventory_report', 'subscription_report', 'loyalty_report'
        ]

        if report_type not in valid_reports:
            return validation_error_response(f'Invalid report type. Valid types: {", ".join(valid_reports)}')

        # Parse date range
        start_date = date_range.get('start_date')
        end_date = date_range.get('end_date')

        if start_date:
            start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
        else:
            start_dt = datetime.now(UTC) - timedelta(days=30)

        if end_date:
            end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
        else:
            end_dt = datetime.now(UTC)

        # Generate report based on type
        report_data = None

        if report_type == 'sales_summary':
            report_data = _generate_sales_summary_report(start_dt, end_dt, filters)
        elif report_type == 'customer_report':
            report_data = _generate_customer_report(start_dt, end_dt, filters)
        elif report_type == 'product_performance':
            report_data = _generate_product_performance_report(start_dt, end_dt, filters)
        elif report_type == 'delivery_report':
            report_data = _generate_delivery_report(start_dt, end_dt, filters)
        elif report_type == 'financial_summary':
            report_data = _generate_financial_summary_report(start_dt, end_dt, filters)
        elif report_type == 'user_activity':
            report_data = _generate_user_activity_report(start_dt, end_dt, filters)
        elif report_type == 'inventory_report':
            report_data = _generate_inventory_report(start_dt, end_dt, filters)
        elif report_type == 'subscription_report':
            report_data = _generate_subscription_report(start_dt, end_dt, filters)
        elif report_type == 'loyalty_report':
            report_data = _generate_loyalty_report(start_dt, end_dt, filters)

        # Add metadata
        report_data['metadata'] = {
            'report_type': report_type,
            'generated_at': datetime.now(UTC).isoformat(),
            'generated_by': current_user_id,
            'date_range': {
                'start': start_dt.isoformat(),
                'end': end_dt.isoformat()
            },
            'filters': filters,
            'format': format_type
        }

        # Format output
        if format_type == 'csv':
            return _format_report_as_csv(report_data, report_type)
        elif format_type == 'excel':
            return _format_report_as_excel(report_data, report_type)
        else:  # json
            return success_response(
                data={'report': report_data},
                message=f'{report_type} report generated successfully'
            )

    except Exception as e:
        current_app.logger.error(f"Generate report error: {e}")
        import traceback
        current_app.logger.error(traceback.format_exc())
        return internal_error_response('Failed to generate report')


def _generate_sales_summary_report(start_dt, end_dt, filters):
    """Generate sales summary report"""
    # Total orders and revenue
    orders_query = Order.query.filter(
        Order.created_at >= start_dt,
        Order.created_at <= end_dt
    )

    if filters.get('status'):
        orders_query = orders_query.filter_by(status=filters['status'])

    total_orders = orders_query.count()
    total_revenue = db.session.query(
        func.sum(Order.total_amount)
    ).filter(
        Order.created_at >= start_dt,
        Order.created_at <= end_dt,
        Order.status.in_(['delivered', 'completed'])
    ).scalar() or 0

    # Average order value
    avg_order_value = total_revenue / total_orders if total_orders > 0 else 0

    # Orders by status
    orders_by_status = db.session.query(
        Order.status,
        func.count(Order.id),
        func.sum(Order.total_amount)
    ).filter(
        Order.created_at >= start_dt,
        Order.created_at <= end_dt
    ).group_by(Order.status).all()

    status_breakdown = [
        {
            'status': status,
            'count': count,
            'revenue': float(revenue or 0)
        }
        for status, count, revenue in orders_by_status
    ]

    # Top selling products
    top_products = db.session.query(
        Product.id,
        Product.name,
        func.count(OrderItem.id).label('order_count'),
        func.sum(OrderItem.quantity).label('total_quantity'),
        func.sum(OrderItem.total_price).label('total_revenue')
    ).join(
        OrderItem, Product.id == OrderItem.product_id
    ).join(
        Order, OrderItem.order_id == Order.id
    ).filter(
        Order.created_at >= start_dt,
        Order.created_at <= end_dt
    ).group_by(
        Product.id, Product.name
    ).order_by(
        desc('total_revenue')
    ).limit(10).all()

    top_products_list = [
        {
            'product_id': prod_id,
            'product_name': name,
            'order_count': order_count,
            'total_quantity': total_quantity,
            'total_revenue': float(total_revenue)
        }
        for prod_id, name, order_count, total_quantity, total_revenue in top_products
    ]

    # Daily sales trend
    daily_sales = db.session.query(
        func.date(Order.created_at).label('date'),
        func.count(Order.id).label('orders'),
        func.sum(Order.total_amount).label('revenue')
    ).filter(
        Order.created_at >= start_dt,
        Order.created_at <= end_dt
    ).group_by('date').order_by('date').all()

    sales_trend = [
        {
            'date': date.isoformat() if date else None,
            'orders': orders,
            'revenue': float(revenue or 0)
        }
        for date, orders, revenue in daily_sales
    ]

    return {
        'summary': {
            'total_orders': total_orders,
            'total_revenue': float(total_revenue),
            'average_order_value': round(avg_order_value, 2),
            'period_days': (end_dt - start_dt).days
        },
        'status_breakdown': status_breakdown,
        'top_products': top_products_list,
        'sales_trend': sales_trend
    }


def _generate_customer_report(start_dt, end_dt, filters):
    """Generate customer activity report"""
    # New customers
    new_customers = User.query.filter(
        User.created_at >= start_dt,
        User.created_at <= end_dt,
        User.role == UserRole.CUSTOMER
    ).count()

    # Active customers (placed at least one order)
    active_customers = db.session.query(
        func.count(func.distinct(Order.user_id))
    ).filter(
        Order.created_at >= start_dt,
        Order.created_at <= end_dt
    ).scalar() or 0

    # Top customers by revenue
    top_customers = db.session.query(
        User.id,
        User.name,
        User.email,
        func.count(Order.id).label('order_count'),
        func.sum(Order.total_amount).label('total_spent')
    ).join(
        Order, User.id == Order.user_id
    ).filter(
        Order.created_at >= start_dt,
        Order.created_at <= end_dt
    ).group_by(
        User.id, User.name, User.email
    ).order_by(
        desc('total_spent')
    ).limit(20).all()

    top_customers_list = [
        {
            'user_id': user_id,
            'name': name,
            'email': email,
            'order_count': order_count,
            'total_spent': float(total_spent)
        }
        for user_id, name, email, order_count, total_spent in top_customers
    ]

    # Customer acquisition trend
    daily_signups = db.session.query(
        func.date(User.created_at).label('date'),
        func.count(User.id).label('signups')
    ).filter(
        User.created_at >= start_dt,
        User.created_at <= end_dt,
        User.role == UserRole.CUSTOMER
    ).group_by('date').order_by('date').all()

    acquisition_trend = [
        {
            'date': date.isoformat() if date else None,
            'signups': signups
        }
        for date, signups in daily_signups
    ]

    return {
        'summary': {
            'new_customers': new_customers,
            'active_customers': active_customers,
            'total_customers': User.query.filter_by(role=UserRole.CUSTOMER).count()
        },
        'top_customers': top_customers_list,
        'acquisition_trend': acquisition_trend
    }


def _generate_product_performance_report(start_dt, end_dt, filters):
    """Generate product performance report"""
    return _generate_sales_summary_report(start_dt, end_dt, filters)


def _generate_delivery_report(start_dt, end_dt, filters):
    """Generate delivery performance report"""
    # Delivery statistics
    deliveries = Delivery.query.filter(
        Delivery.created_at >= start_dt,
        Delivery.created_at <= end_dt
    )

    total_deliveries = deliveries.count()

    # Deliveries by status
    by_status = db.session.query(
        Delivery.status,
        func.count(Delivery.id)
    ).filter(
        Delivery.created_at >= start_dt,
        Delivery.created_at <= end_dt
    ).group_by(Delivery.status).all()

    status_breakdown = [
        {'status': status, 'count': count}
        for status, count in by_status
    ]

    # On-time delivery rate
    on_time = Delivery.query.filter(
        Delivery.created_at >= start_dt,
        Delivery.created_at <= end_dt,
        Delivery.status == 'delivered',
        Delivery.delivered_at <= Delivery.scheduled_delivery_time
    ).count()

    on_time_rate = (on_time / total_deliveries * 100) if total_deliveries > 0 else 0

    # Top delivery personnel
    top_personnel = db.session.query(
        DeliveryPerson.id,
        DeliveryPerson.name,
        func.count(Delivery.id).label('delivery_count'),
        func.avg(
            func.extract('epoch', Delivery.delivered_at - Delivery.created_at) / 3600
        ).label('avg_delivery_time_hours')
    ).join(
        Delivery, DeliveryPerson.id == Delivery.delivery_person_id
    ).filter(
        Delivery.created_at >= start_dt,
        Delivery.created_at <= end_dt,
        Delivery.status == 'delivered'
    ).group_by(
        DeliveryPerson.id, DeliveryPerson.name
    ).order_by(
        desc('delivery_count')
    ).limit(10).all()

    personnel_list = [
        {
            'person_id': person_id,
            'name': name,
            'delivery_count': delivery_count,
            'avg_delivery_time_hours': round(float(avg_time or 0), 2)
        }
        for person_id, name, delivery_count, avg_time in top_personnel
    ]

    return {
        'summary': {
            'total_deliveries': total_deliveries,
            'on_time_rate': round(on_time_rate, 2),
            'on_time_count': on_time
        },
        'status_breakdown': status_breakdown,
        'top_personnel': personnel_list
    }


def _generate_financial_summary_report(start_dt, end_dt, filters):
    """Generate financial summary report"""
    # Total revenue
    total_revenue = db.session.query(
        func.sum(Payment.amount)
    ).filter(
        Payment.created_at >= start_dt,
        Payment.created_at <= end_dt,
        Payment.status == 'completed'
    ).scalar() or 0

    # Payment method breakdown
    by_method = db.session.query(
        Payment.payment_method,
        func.count(Payment.id),
        func.sum(Payment.amount)
    ).filter(
        Payment.created_at >= start_dt,
        Payment.created_at <= end_dt,
        Payment.status == 'completed'
    ).group_by(Payment.payment_method).all()

    method_breakdown = [
        {
            'method': method,
            'count': count,
            'amount': float(amount or 0)
        }
        for method, count, amount in by_method
    ]

    # Refunds
    total_refunds = db.session.query(
        func.sum(Payment.amount)
    ).filter(
        Payment.created_at >= start_dt,
        Payment.created_at <= end_dt,
        Payment.status == 'refunded'
    ).scalar() or 0

    # Daily revenue
    daily_revenue = db.session.query(
        func.date(Payment.created_at).label('date'),
        func.sum(Payment.amount).label('revenue')
    ).filter(
        Payment.created_at >= start_dt,
        Payment.created_at <= end_dt,
        Payment.status == 'completed'
    ).group_by('date').order_by('date').all()

    revenue_trend = [
        {
            'date': date.isoformat() if date else None,
            'revenue': float(revenue or 0)
        }
        for date, revenue in daily_revenue
    ]

    return {
        'summary': {
            'total_revenue': float(total_revenue),
            'total_refunds': float(total_refunds),
            'net_revenue': float(total_revenue - total_refunds)
        },
        'payment_method_breakdown': method_breakdown,
        'revenue_trend': revenue_trend
    }


def _generate_user_activity_report(start_dt, end_dt, filters):
    """Generate user activity report"""
    # Login events from audit logs
    login_events = AuditLog.query.filter(
        AuditLog.created_at >= start_dt,
        AuditLog.created_at <= end_dt,
        AuditLog.event_type == AuditEventType.LOGIN_SUCCESS
    ).count()

    # Active users
    active_users = db.session.query(
        func.count(func.distinct(AuditLog.user_id))
    ).filter(
        AuditLog.created_at >= start_dt,
        AuditLog.created_at <= end_dt,
        AuditLog.user_id.isnot(None)
    ).scalar() or 0

    # Most active users
    top_active = db.session.query(
        User.id,
        User.name,
        User.email,
        func.count(AuditLog.id).label('activity_count')
    ).join(
        AuditLog, User.id == AuditLog.user_id
    ).filter(
        AuditLog.created_at >= start_dt,
        AuditLog.created_at <= end_dt
    ).group_by(
        User.id, User.name, User.email
    ).order_by(
        desc('activity_count')
    ).limit(20).all()

    active_users_list = [
        {
            'user_id': user_id,
            'name': name,
            'email': email,
            'activity_count': activity_count
        }
        for user_id, name, email, activity_count in top_active
    ]

    return {
        'summary': {
            'total_logins': login_events,
            'active_users': active_users
        },
        'most_active_users': active_users_list
    }


def _generate_inventory_report(start_dt, end_dt, filters):
    """Generate inventory status report"""
    # Get all products with inventory
    products = Product.query.filter_by(is_active=True).all()

    inventory_data = []
    low_stock_items = []

    for product in products:
        stock_level = product.stock_quantity or 0

        item_data = {
            'product_id': product.id,
            'product_name': product.name,
            'current_stock': stock_level,
            'is_active': product.is_active
        }

        inventory_data.append(item_data)

        # Check if low stock (arbitrary threshold)
        if stock_level < 10:
            low_stock_items.append(item_data)

    return {
        'summary': {
            'total_products': len(products),
            'low_stock_count': len(low_stock_items),
            'out_of_stock_count': sum(1 for p in products if (p.stock_quantity or 0) == 0)
        },
        'inventory': inventory_data[:100],  # Limit to 100 for performance
        'low_stock_items': low_stock_items
    }


def _generate_subscription_report(start_dt, end_dt, filters):
    """Generate subscription report"""
    # Active subscriptions
    active_subs = Subscription.query.filter_by(status='active').count()

    # New subscriptions in period
    new_subs = Subscription.query.filter(
        Subscription.created_at >= start_dt,
        Subscription.created_at <= end_dt
    ).count()

    # Subscriptions by status
    by_status = db.session.query(
        Subscription.status,
        func.count(Subscription.id),
        func.sum(Subscription.billing_amount)
    ).group_by(Subscription.status).all()

    status_breakdown = [
        {
            'status': status,
            'count': count,
            'total_value': float(total_value or 0)
        }
        for status, count, total_value in by_status
    ]

    # Monthly recurring revenue
    mrr = db.session.query(
        func.sum(Subscription.billing_amount)
    ).filter_by(
        status='active',
        billing_cycle=SubscriptionFrequency.MONTHLY
    ).scalar() or 0

    return {
        'summary': {
            'active_subscriptions': active_subs,
            'new_subscriptions': new_subs,
            'monthly_recurring_revenue': float(mrr)
        },
        'status_breakdown': status_breakdown
    }


def _generate_loyalty_report(start_dt, end_dt, filters):
    """Generate loyalty program report"""
    # Total loyalty points awarded
    total_points = db.session.query(
        func.sum(User.loyalty_points)
    ).filter_by(role=UserRole.CUSTOMER).scalar() or 0

    # Top loyalty members
    top_members = db.session.query(
        User.id,
        User.name,
        User.email,
        User.loyalty_points
    ).filter(
        User.role == UserRole.CUSTOMER,
        User.loyalty_points > 0
    ).order_by(
        User.loyalty_points.desc()
    ).limit(20).all()

    top_members_list = [
        {
            'user_id': user_id,
            'name': name,
            'email': email,
            'points': points
        }
        for user_id, name, email, points in top_members
    ]

    return {
        'summary': {
            'total_points_in_system': total_points,
            'members_with_points': len(top_members)
        },
        'top_members': top_members_list
    }


def _format_report_as_csv(report_data, report_type):
    """Format report as CSV"""
    import csv
    from io import StringIO

    output = StringIO()

    # Simple CSV formatting - flatten the summary section
    if 'summary' in report_data:
        writer = csv.DictWriter(output, fieldnames=report_data['summary'].keys())
        writer.writeheader()
        writer.writerow(report_data['summary'])

    csv_data = output.getvalue()

    return success_response(data={
        'report': csv_data,
        'format': 'csv',
        'metadata': report_data.get('metadata', {})
    })


def _format_report_as_excel(report_data, report_type):
    """Format report as Excel (simplified JSON for now)"""
    # For full Excel support, you would use openpyxl or xlsxwriter
    # This is a placeholder that returns JSON with Excel metadata
    return success_response(data={
        'report': report_data,
        'format': 'excel',
        'note': 'Excel format requires openpyxl library - returning JSON format'
    })


@admin_bp.route('/bulk-actions', methods=['POST'])
@jwt_required()
@rate_limit(max_requests=10, window_seconds=600, per='user')  # 10 bulk actions per 10 minutes per user
@validate_admin_action(['manage_users', 'manage_orders', 'manage_products'])
@validate_json(['action', 'target_type', 'target_ids'])
def perform_bulk_action():
    """
    Perform bulk actions on multiple entities

    Request Body:
        - action: Action to perform
        - target_type: Type of entities (user, order, product, review, subscription)
        - target_ids: List of entity IDs (max 1000)
        - parameters: Additional parameters for the action
        - reason: Reason for the action (required for some actions)
    """
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        action = data.get('action')
        target_type = data.get('target_type')
        target_ids = data.get('target_ids')
        parameters = data.get('parameters', {})
        reason = data.get('reason', f'Bulk action by admin {current_user_id}')

        # Validate inputs
        if not isinstance(target_ids, list) or len(target_ids) == 0:
            return validation_error_response('target_ids must be a non-empty list')

        if len(target_ids) > 1000:
            return validation_error_response('Maximum 1000 items allowed per bulk action')

        # Define valid actions per target type
        valid_actions = {
            'user': ['activate', 'deactivate', 'suspend', 'delete', 'send_email', 'assign_role'],
            'order': ['cancel', 'confirm', 'process', 'mark_delivered'],
            'product': ['activate', 'deactivate', 'delete', 'update_stock', 'update_price'],
            'review': ['approve', 'reject', 'delete', 'feature'],
            'subscription': ['pause', 'resume', 'cancel'],
            'delivery': ['assign_driver', 'mark_in_transit', 'mark_delivered']
        }

        if target_type not in valid_actions:
            return validation_error_response(f'Invalid target_type. Valid types: {", ".join(valid_actions.keys())}')

        if action not in valid_actions[target_type]:
            return validation_error_response(
                f'Invalid action "{action}" for {target_type}. Valid actions: {", ".join(valid_actions[target_type])}'
            )

        # Perform bulk action based on target type
        result = None

        if target_type == 'user':
            result = _bulk_action_users(action, target_ids, parameters, reason, current_user_id)
        elif target_type == 'order':
            result = _bulk_action_orders(action, target_ids, parameters, reason, current_user_id)
        elif target_type == 'product':
            result = _bulk_action_products(action, target_ids, parameters, reason, current_user_id)
        elif target_type == 'review':
            result = _bulk_action_reviews(action, target_ids, parameters, reason, current_user_id)
        elif target_type == 'subscription':
            result = _bulk_action_subscriptions(action, target_ids, parameters, reason, current_user_id)
        elif target_type == 'delivery':
            result = _bulk_action_deliveries(action, target_ids, parameters, reason, current_user_id)

        # Log bulk action
        from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity

        audit_logger.log_event(
            event_type=AuditEventType.BULK_OPERATION,
            action=f'bulk_{action}_{target_type}',
            severity=AuditSeverity.HIGH,
            resource_type=target_type,
            description=f'Bulk {action} on {result["success_count"]} {target_type}(s)',
            success=True,
            additional_data={
                'action': action,
                'target_type': target_type,
                'total_items': len(target_ids),
                'success_count': result['success_count'],
                'failed_count': result['failed_count'],
                'reason': reason
            }
        )

        return success_response(
            data={'results': result},
            message=f'Bulk action completed: {result["success_count"]} succeeded, {result["failed_count"]} failed'
        )

    except Exception as e:
        current_app.logger.error(f"Perform bulk action error: {e}")
        import traceback
        current_app.logger.error(traceback.format_exc())
        return internal_error_response('Failed to perform bulk action')


def _bulk_action_users(action, user_ids, parameters, reason, admin_id):
    """Perform bulk actions on users"""
    success_count = 0
    failed_count = 0
    errors = []

    for user_id in user_ids:
        try:
            user = User.query.get(user_id)
            if not user:
                failed_count += 1
                errors.append({'user_id': user_id, 'error': 'User not found'})
                continue

            if action == 'activate':
                user.status = 'active'
                user.is_active = True
            elif action == 'deactivate':
                user.status = 'inactive'
                user.is_active = False
            elif action == 'suspend':
                user.status = 'suspended'
                user.is_active = False
            elif action == 'delete':
                # Soft delete - mark as deleted
                user.status = 'deleted'
                user.is_active = False
            elif action == 'assign_role':
                new_role = parameters.get('role')
                if new_role in ['customer', 'admin', 'manager', 'staff']:
                    user.role = new_role
                else:
                    failed_count += 1
                    errors.append({'user_id': user_id, 'error': 'Invalid role'})
                    continue
            elif action == 'send_email':
                # This would integrate with notification service
                # For now, just mark as success
                pass

            db.session.commit()
            success_count += 1

        except Exception as e:
            db.session.rollback()
            failed_count += 1
            errors.append({'user_id': user_id, 'error': str(e)})

    return {
        'success_count': success_count,
        'failed_count': failed_count,
        'errors': errors[:10],  # Limit errors to first 10
        'total_errors': len(errors)
    }


def _bulk_action_orders(action, order_ids, parameters, reason, admin_id):
    """Perform bulk actions on orders"""
    success_count = 0
    failed_count = 0
    errors = []

    for order_id in order_ids:
        try:
            order = Order.query.get(order_id)
            if not order:
                failed_count += 1
                errors.append({'order_id': order_id, 'error': 'Order not found'})
                continue

            if action == 'cancel':
                if order.status in ['pending', 'confirmed']:
                    order.status = 'cancelled'
                    order.cancelled_at = datetime.now(UTC)
                    order.cancellation_reason = reason
                else:
                    failed_count += 1
                    errors.append({'order_id': order_id, 'error': f'Cannot cancel order with status {order.status}'})
                    continue

            elif action == 'confirm':
                if order.status == 'pending':
                    order.status = 'confirmed'
                    order.confirmed_at = datetime.now(UTC)
                else:
                    failed_count += 1
                    errors.append({'order_id': order_id, 'error': f'Cannot confirm order with status {order.status}'})
                    continue

            elif action == 'process':
                if order.status == 'confirmed':
                    order.status = 'processing'
                else:
                    failed_count += 1
                    errors.append({'order_id': order_id, 'error': f'Cannot process order with status {order.status}'})
                    continue

            elif action == 'mark_delivered':
                if order.status in ['processing', 'shipped', 'out_for_delivery']:
                    order.status = 'delivered'
                    order.delivered_at = datetime.now(UTC)
                else:
                    failed_count += 1
                    errors.append({'order_id': order_id, 'error': f'Cannot mark as delivered with status {order.status}'})
                    continue

            db.session.commit()
            success_count += 1

        except Exception as e:
            db.session.rollback()
            failed_count += 1
            errors.append({'order_id': order_id, 'error': str(e)})

    return {
        'success_count': success_count,
        'failed_count': failed_count,
        'errors': errors[:10],
        'total_errors': len(errors)
    }


def _bulk_action_products(action, product_ids, parameters, reason, admin_id):
    """Perform bulk actions on products"""
    success_count = 0
    failed_count = 0
    errors = []

    for product_id in product_ids:
        try:
            product = Product.query.get(product_id)
            if not product:
                failed_count += 1
                errors.append({'product_id': product_id, 'error': 'Product not found'})
                continue

            if action == 'activate':
                product.is_active = True
            elif action == 'deactivate':
                product.is_active = False
            elif action == 'delete':
                # Soft delete
                product.is_active = False
                product.deleted_at = datetime.now(UTC)
            elif action == 'update_stock':
                stock_adjustment = parameters.get('stock_adjustment', 0)
                new_stock = parameters.get('new_stock')

                if new_stock is not None:
                    product.stock_quantity = new_stock
                else:
                    product.stock_quantity = (product.stock_quantity or 0) + stock_adjustment

            elif action == 'update_price':
                new_price = parameters.get('new_price')
                price_adjustment = parameters.get('price_adjustment', 0)

                if new_price is not None:
                    product.price = new_price
                elif price_adjustment != 0:
                    product.price = product.price + price_adjustment

            db.session.commit()
            success_count += 1

        except Exception as e:
            db.session.rollback()
            failed_count += 1
            errors.append({'product_id': product_id, 'error': str(e)})

    return {
        'success_count': success_count,
        'failed_count': failed_count,
        'errors': errors[:10],
        'total_errors': len(errors)
    }


def _bulk_action_reviews(action, review_ids, parameters, reason, admin_id):
    """Perform bulk actions on reviews"""
    success_count = 0
    failed_count = 0
    errors = []

    for review_id in review_ids:
        try:
            review = Review.query.get(review_id)
            if not review:
                failed_count += 1
                errors.append({'review_id': review_id, 'error': 'Review not found'})
                continue

            if action == 'approve':
                review.is_approved = True
                review.moderator_notes = reason
            elif action == 'reject':
                review.is_approved = False
                review.moderator_notes = reason
            elif action == 'delete':
                db.session.delete(review)
            elif action == 'feature':
                review.is_featured = True
                review.is_approved = True

            db.session.commit()
            success_count += 1

        except Exception as e:
            db.session.rollback()
            failed_count += 1
            errors.append({'review_id': review_id, 'error': str(e)})

    return {
        'success_count': success_count,
        'failed_count': failed_count,
        'errors': errors[:10],
        'total_errors': len(errors)
    }


def _bulk_action_subscriptions(action, subscription_ids, parameters, reason, admin_id):
    """Perform bulk actions on subscriptions"""
    success_count = 0
    failed_count = 0
    errors = []

    for subscription_id in subscription_ids:
        try:
            subscription = Subscription.query.get(subscription_id)
            if not subscription:
                failed_count += 1
                errors.append({'subscription_id': subscription_id, 'error': 'Subscription not found'})
                continue

            if action == 'pause':
                if subscription.status == 'active':
                    subscription.status = 'paused'
                    subscription.paused_at = datetime.now(UTC)
                    subscription.pause_reason = reason
                else:
                    failed_count += 1
                    errors.append({'subscription_id': subscription_id, 'error': 'Can only pause active subscriptions'})
                    continue

            elif action == 'resume':
                if subscription.status == 'paused':
                    subscription.status = 'active'
                    subscription.paused_at = None
                    subscription.pause_reason = None
                else:
                    failed_count += 1
                    errors.append({'subscription_id': subscription_id, 'error': 'Can only resume paused subscriptions'})
                    continue

            elif action == 'cancel':
                subscription.status = 'cancelled'
                subscription.end_date = datetime.now(UTC)

            db.session.commit()
            success_count += 1

        except Exception as e:
            db.session.rollback()
            failed_count += 1
            errors.append({'subscription_id': subscription_id, 'error': str(e)})

    return {
        'success_count': success_count,
        'failed_count': failed_count,
        'errors': errors[:10],
        'total_errors': len(errors)
    }


def _bulk_action_deliveries(action, delivery_ids, parameters, reason, admin_id):
    """Perform bulk actions on deliveries"""
    success_count = 0
    failed_count = 0
    errors = []

    for delivery_id in delivery_ids:
        try:
            delivery = Delivery.query.get(delivery_id)
            if not delivery:
                failed_count += 1
                errors.append({'delivery_id': delivery_id, 'error': 'Delivery not found'})
                continue

            if action == 'assign_driver':
                driver_id = parameters.get('driver_id')
                if not driver_id:
                    failed_count += 1
                    errors.append({'delivery_id': delivery_id, 'error': 'driver_id required'})
                    continue

                driver = DeliveryPerson.query.get(driver_id)
                if not driver:
                    failed_count += 1
                    errors.append({'delivery_id': delivery_id, 'error': 'Driver not found'})
                    continue

                delivery.delivery_person_id = driver_id
                delivery.status = 'assigned'

            elif action == 'mark_in_transit':
                delivery.status = 'in_transit'
                delivery.picked_up_at = datetime.now(UTC)

            elif action == 'mark_delivered':
                delivery.status = 'delivered'
                delivery.delivered_at = datetime.now(UTC)

            db.session.commit()
            success_count += 1

        except Exception as e:
            db.session.rollback()
            failed_count += 1
            errors.append({'delivery_id': delivery_id, 'error': str(e)})

    return {
        'success_count': success_count,
        'failed_count': failed_count,
        'errors': errors[:10],
        'total_errors': len(errors)
    }


# ============================================================================
# LOYALTY REWARD MANAGEMENT ENDPOINTS
# ============================================================================

@admin_bp.route('/loyalty/rewards', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_loyalty', 'manage_loyalty'])
def get_loyalty_rewards():
    """
    Get all loyalty rewards with filtering and pagination

    Query Parameters:
        - page: Page number (default: 1)
        - per_page: Items per page (default: 20)
        - program_id: Filter by loyalty program
        - reward_type: Filter by reward type (discount, free_product, free_delivery, voucher)
        - is_active: Filter by active status
        - is_featured: Filter by featured status
        - search: Search in name and description
    """
    try:
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 20)), 100)

        # Build query
        query = LoyaltyReward.query

        # Program filter
        program_id = request.args.get('program_id', type=int)
        if program_id:
            query = query.filter_by(program_id=program_id)

        # Reward type filter
        reward_type = request.args.get('reward_type')
        if reward_type:
            query = query.filter_by(reward_type=reward_type)

        # Active filter
        is_active = request.args.get('is_active')
        if is_active is not None:
            is_active_bool = is_active.lower() == 'true'
            query = query.filter_by(is_active=is_active_bool)

        # Featured filter
        is_featured = request.args.get('is_featured')
        if is_featured is not None:
            is_featured_bool = is_featured.lower() == 'true'
            query = query.filter_by(is_featured=is_featured_bool)

        # Search
        search = request.args.get('search')
        if search:
            query = query.filter(
                or_(
                    LoyaltyReward.name.ilike(f'%{search}%'),
                    LoyaltyReward.description.ilike(f'%{search}%')
                )
            )

        # Sort by sort_order and created_at
        query = query.order_by(LoyaltyReward.sort_order.asc(), LoyaltyReward.created_at.desc())

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize rewards
        language = get_current_language()
        rewards = [reward.to_dict(language=language) for reward in pagination.items]

        return paginated_response(
            items=rewards,
            total=pagination.total,
            page=page,
            per_page=per_page
        )

    except Exception as e:
        current_app.logger.error(f"Get loyalty rewards error: {e}")
        return internal_error_response('Failed to get loyalty rewards')


@admin_bp.route('/loyalty/rewards/<int:reward_id>', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_loyalty', 'manage_loyalty'])
def get_loyalty_reward_detail(reward_id):
    """Get detailed information about a specific loyalty reward"""
    try:
        reward = LoyaltyReward.query.get(reward_id)

        if not reward:
            return not_found_response('Loyalty reward not found')

        language = get_current_language()
        reward_data = reward.to_dict(language=language, include_all_translations=True)

        # Add program info
        if reward.program:
            reward_data['program'] = reward.program.to_dict()

        # Add product info if applicable
        if reward.free_product:
            reward_data['free_product'] = {
                'id': reward.free_product.id,
                'name': reward.free_product.name,
                'price': float(reward.free_product.price) if reward.free_product.price else 0
            }

        # Add redemption statistics
        reward_data['redemption_stats'] = {
            'total_redemptions': reward.redemptions_used,
            'remaining_redemptions': reward.max_redemptions - reward.redemptions_used if reward.max_redemptions else None,
            'is_available': reward.is_active and (not reward.max_redemptions or reward.redemptions_used < reward.max_redemptions)
        }

        return success_response(data={'reward': reward_data})

    except Exception as e:
        current_app.logger.error(f"Get loyalty reward detail error: {e}")
        return internal_error_response('Failed to get loyalty reward detail')


@admin_bp.route('/loyalty/rewards', methods=['POST'])
@jwt_required()
@validate_admin_action(['manage_loyalty'])
@validate_json(['name', 'reward_type', 'points_cost'])
def create_loyalty_reward():
    """
    Create a new loyalty reward

    Request Body:
        - program_id: ID of loyalty program
        - name: Reward name
        - description: Reward description
        - reward_type: Type (discount, free_product, free_delivery, voucher)
        - points_cost: Points required
        - min_order_value: Minimum order value (optional)
        - max_uses_per_user: Max uses per user (default: 1)
        - max_redemptions: Overall limit (optional)
        - discount_type: percentage or fixed (for discount rewards)
        - discount_value: Discount value
        - free_product_id: Product ID (for free_product rewards)
        - voucher_code: Voucher code (for voucher rewards)
        - is_active: Active status (default: true)
        - is_featured: Featured status (default: false)
        - valid_from: Valid from date (ISO format)
        - valid_until: Valid until date (ISO format)
        - applicable_products: List of product IDs
        - applicable_categories: List of category IDs
        - terms_conditions: Terms and conditions
        - image_url: Image URL
        - sort_order: Display order (default: 0)
        - translations: Multilingual content
    """
    try:
        data = request.get_json()

        # Get default program if not specified
        program_id = data.get('program_id')
        if not program_id:
            default_program = LoyaltyProgram.query.filter_by(is_default=True).first()
            if not default_program:
                default_program = LoyaltyProgram.query.filter_by(is_active=True).first()

            if not default_program:
                return validation_error_response('No active loyalty program found')

            program_id = default_program.id

        # Validate program exists
        program = LoyaltyProgram.query.get(program_id)
        if not program:
            return not_found_response('Loyalty program not found')

        # Validate reward type
        reward_type = data.get('reward_type')
        valid_types = ['discount', 'free_product', 'free_delivery', 'voucher']
        if reward_type not in valid_types:
            return validation_error_response(f'Invalid reward_type. Must be one of: {", ".join(valid_types)}')

        # Type-specific validation
        if reward_type == 'discount':
            if not data.get('discount_type') or not data.get('discount_value'):
                return validation_error_response('discount_type and discount_value required for discount rewards')

        if reward_type == 'free_product':
            if not data.get('free_product_id'):
                return validation_error_response('free_product_id required for free_product rewards')

        # Create reward
        reward = LoyaltyReward(
            program_id=program_id,
            name=data.get('name'),
            description=data.get('description'),
            reward_type=reward_type,
            points_cost=data.get('points_cost'),
            min_order_value=Decimal(str(data.get('min_order_value', 0))),
            max_uses_per_user=data.get('max_uses_per_user', 1),
            max_redemptions=data.get('max_redemptions'),
            discount_type=data.get('discount_type'),
            discount_value=Decimal(str(data.get('discount_value'))) if data.get('discount_value') else None,
            free_product_id=data.get('free_product_id'),
            voucher_code=data.get('voucher_code'),
            is_active=data.get('is_active', True),
            is_featured=data.get('is_featured', False),
            applicable_products=data.get('applicable_products', []),
            applicable_categories=data.get('applicable_categories', []),
            terms_conditions=data.get('terms_conditions'),
            image_url=data.get('image_url'),
            sort_order=data.get('sort_order', 0)
        )

        # Set validity dates
        if data.get('valid_from'):
            reward.valid_from = datetime.fromisoformat(data['valid_from'].replace('Z', '+00:00'))
        if data.get('valid_until'):
            reward.valid_until = datetime.fromisoformat(data['valid_until'].replace('Z', '+00:00'))

        db.session.add(reward)
        db.session.flush()

        # Handle translations
        if data.get('translations'):
            reward.set_translations(data['translations'])

        db.session.commit()

        language = get_current_language()
        return created_response(
            data={'reward': reward.to_dict(language=language)},
            message='Loyalty reward created successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create loyalty reward error: {e}")
        import traceback
        current_app.logger.error(traceback.format_exc())
        return internal_error_response('Failed to create loyalty reward')


@admin_bp.route('/loyalty/rewards/<int:reward_id>', methods=['PUT'])
@jwt_required()
@validate_admin_action(['manage_loyalty'])
def update_loyalty_reward(reward_id):
    """Update an existing loyalty reward"""
    try:
        reward = LoyaltyReward.query.get(reward_id)

        if not reward:
            return not_found_response('Loyalty reward not found')

        data = request.get_json()

        # Update basic fields
        if 'name' in data:
            reward.name = data['name']
        if 'description' in data:
            reward.description = data['description']
        if 'points_cost' in data:
            reward.points_cost = data['points_cost']
        if 'min_order_value' in data:
            reward.min_order_value = Decimal(str(data['min_order_value']))
        if 'max_uses_per_user' in data:
            reward.max_uses_per_user = data['max_uses_per_user']
        if 'max_redemptions' in data:
            reward.max_redemptions = data['max_redemptions']
        if 'discount_type' in data:
            reward.discount_type = data['discount_type']
        if 'discount_value' in data:
            reward.discount_value = Decimal(str(data['discount_value']))
        if 'free_product_id' in data:
            reward.free_product_id = data['free_product_id']
        if 'voucher_code' in data:
            reward.voucher_code = data['voucher_code']
        if 'is_active' in data:
            reward.is_active = data['is_active']
        if 'is_featured' in data:
            reward.is_featured = data['is_featured']
        if 'applicable_products' in data:
            reward.applicable_products = data['applicable_products']
        if 'applicable_categories' in data:
            reward.applicable_categories = data['applicable_categories']
        if 'terms_conditions' in data:
            reward.terms_conditions = data['terms_conditions']
        if 'image_url' in data:
            reward.image_url = data['image_url']
        if 'sort_order' in data:
            reward.sort_order = data['sort_order']

        # Update validity dates
        if 'valid_from' in data:
            reward.valid_from = datetime.fromisoformat(data['valid_from'].replace('Z', '+00:00')) if data['valid_from'] else None
        if 'valid_until' in data:
            reward.valid_until = datetime.fromisoformat(data['valid_until'].replace('Z', '+00:00')) if data['valid_until'] else None

        # Handle translations
        if data.get('translations'):
            reward.set_translations(data['translations'])

        db.session.commit()

        language = get_current_language()
        return success_response(
            data={'reward': reward.to_dict(language=language)},
            message='Loyalty reward updated successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update loyalty reward error: {e}")
        return internal_error_response('Failed to update loyalty reward')


@admin_bp.route('/loyalty/rewards/<int:reward_id>', methods=['DELETE'])
@jwt_required()
@validate_admin_action(['manage_loyalty'])
def delete_loyalty_reward(reward_id):
    """Delete a loyalty reward"""
    try:
        reward = LoyaltyReward.query.get(reward_id)

        if not reward:
            return not_found_response('Loyalty reward not found')

        # Check if reward has been redeemed
        if reward.redemptions_used > 0:
            # Don't delete, just deactivate
            reward.is_active = False
            db.session.commit()
            return success_response(message=get_translation('api.admin.success.loyalty_reward_deactivated'))

        # Safe to delete
        db.session.delete(reward)
        db.session.commit()

        return success_response(message=get_translation('api.admin.success.loyalty_reward_deleted'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete loyalty reward error: {e}")
        return internal_error_response('Failed to delete loyalty reward')


@admin_bp.route('/loyalty/programs', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_loyalty', 'manage_loyalty'])
def get_loyalty_programs():
    """Get all loyalty programs"""
    try:
        programs = LoyaltyProgram.query.order_by(LoyaltyProgram.is_default.desc(), LoyaltyProgram.created_at.desc()).all()

        programs_data = [program.to_dict() for program in programs]

        return success_response(data={'programs': programs_data})

    except Exception as e:
        current_app.logger.error(f"Get loyalty programs error: {e}")
        return internal_error_response('Failed to get loyalty programs')


@admin_bp.route('/loyalty/programs', methods=['POST'])
@jwt_required()
@validate_admin_action(['manage_loyalty'])
def create_loyalty_program():
    """Create a new loyalty program"""
    try:
        data = request.get_json()

        # Validate required fields
        if not data.get('name'):
            return validation_error_response('Program name is required')

        # Create new program
        program = LoyaltyProgram(
            name=data['name'],
            description=data.get('description'),
            is_active=data.get('is_active', True),
            is_default=data.get('is_default', False),
            points_per_uzs=data.get('points_per_uzs', 1.0),
            signup_bonus=data.get('signup_bonus', 100),
            referral_bonus=data.get('referral_bonus', 50),
            birthday_bonus=data.get('birthday_bonus', 25),
            points_expiry_days=data.get('points_expiry_days', 365),
            min_redemption_points=data.get('min_redemption_points', 100),
            tier_thresholds=data.get('tier_thresholds', {}),
            tier_multipliers=data.get('tier_multipliers', {}),
            terms_and_conditions=data.get('terms_and_conditions'),
            start_date=data.get('start_date'),
            end_date=data.get('end_date')
        )

        db.session.add(program)
        db.session.commit()

        current_app.logger.info(f"Loyalty program created: {program.name} (ID: {program.id})")

        return success_response(
            data={'program': program.to_dict()},
            message='Loyalty program created successfully',
            status_code=201
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create loyalty program error: {e}")
        return internal_error_response('Failed to create loyalty program')


@admin_bp.route('/loyalty/programs/<int:program_id>', methods=['PUT'])
@jwt_required()
@validate_admin_action(['manage_loyalty'])
def update_loyalty_program(program_id):
    """Update loyalty program settings"""
    try:
        program = LoyaltyProgram.query.get(program_id)

        if not program:
            return not_found_response('Loyalty program not found')

        data = request.get_json()

        # Update fields
        if 'name' in data:
            program.name = data['name']
        if 'description' in data:
            program.description = data['description']
        if 'is_active' in data:
            program.is_active = data['is_active']
        if 'points_per_uzs' in data:
            program.points_per_uzs = data['points_per_uzs']
        if 'signup_bonus' in data:
            program.signup_bonus = data['signup_bonus']
        if 'referral_bonus' in data:
            program.referral_bonus = data['referral_bonus']
        if 'birthday_bonus' in data:
            program.birthday_bonus = data['birthday_bonus']
        if 'points_expiry_days' in data:
            program.points_expiry_days = data['points_expiry_days']
        if 'min_redemption_points' in data:
            program.min_redemption_points = data['min_redemption_points']
        if 'tier_thresholds' in data:
            program.tier_thresholds = data['tier_thresholds']
        if 'tier_multipliers' in data:
            program.tier_multipliers = data['tier_multipliers']
        if 'terms_and_conditions' in data:
            program.terms_and_conditions = data['terms_and_conditions']

        db.session.commit()

        return success_response(
            data={'program': program.to_dict()},
            message='Loyalty program updated successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update loyalty program error: {e}")
        return internal_error_response('Failed to update loyalty program')


@admin_bp.route('/loyalty/programs/<int:program_id>', methods=['DELETE'])
@jwt_required()
@validate_admin_action(['manage_loyalty'])
def delete_loyalty_program(program_id):
    """Delete a loyalty program"""
    try:
        program = LoyaltyProgram.query.get(program_id)

        if not program:
            return not_found_response('Loyalty program not found')

        # Check if this is the default program
        if program.is_default:
            return validation_error_response('Cannot delete the default loyalty program')

        # Check if program has active members
        from business_app.models.loyalty import LoyaltyPoints
        member_count = LoyaltyPoints.query.filter_by(program_id=program_id).count()

        if member_count > 0:
            # Soft delete - just deactivate
            program.is_active = False
            db.session.commit()
            current_app.logger.info(f"Loyalty program deactivated: {program.name} (ID: {program.id})")
            return success_response(message=f'Program deactivated (has {member_count} members)')
        else:
            # Hard delete if no members
            program_name = program.name
            db.session.delete(program)
            db.session.commit()
            current_app.logger.info(f"Loyalty program deleted: {program_name} (ID: {program_id})")
            return success_response(message=get_translation('api.admin.success.loyalty_program_deleted'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete loyalty program error: {e}")
        return internal_error_response('Failed to delete loyalty program')


@admin_bp.route('/loyalty/analytics', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_loyalty', 'view_reports'])
def get_loyalty_analytics():
    """
    Get loyalty program analytics and statistics

    Query Parameters:
        - start_date: Start date (ISO format)
        - end_date: End date (ISO format)
        - program_id: Filter by program ID
    """
    try:
        # Date range
        end_date = request.args.get('end_date')
        if end_date:
            end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
        else:
            end_dt = datetime.now(UTC)

        start_date = request.args.get('start_date')
        if start_date:
            start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
        else:
            start_dt = end_dt - timedelta(days=30)

        program_id = request.args.get('program_id', type=int)

        # Total active members
        from business_app.models.loyalty import LoyaltyPoints

        active_members_query = LoyaltyPoints.query.filter(LoyaltyPoints.current_balance > 0)
        if program_id:
            active_members_query = active_members_query.filter_by(program_id=program_id)

        active_members = active_members_query.count()

        # Total points in circulation
        total_points = db.session.query(
            func.sum(LoyaltyPoints.current_balance)
        ).filter(LoyaltyPoints.current_balance > 0)

        if program_id:
            total_points = total_points.filter(LoyaltyPoints.program_id == program_id)

        total_points_value = total_points.scalar() or 0

        # Redemption statistics
        from business_app.models.loyalty import LoyaltyTransaction

        redemptions = LoyaltyTransaction.query.filter(
            LoyaltyTransaction.transaction_type == 'redeemed',
            LoyaltyTransaction.created_at >= start_dt,
            LoyaltyTransaction.created_at <= end_dt
        )

        total_redemptions = redemptions.count()
        total_redeemed_points = abs(db.session.query(
            func.sum(LoyaltyTransaction.points)
        ).filter(
            LoyaltyTransaction.transaction_type == 'redeemed',
            LoyaltyTransaction.created_at >= start_dt,
            LoyaltyTransaction.created_at <= end_dt,
            LoyaltyTransaction.points < 0
        ).scalar() or 0)

        # Earned points
        earned_points = db.session.query(
            func.sum(LoyaltyTransaction.points)
        ).filter(
            LoyaltyTransaction.transaction_type == 'earned',
            LoyaltyTransaction.created_at >= start_dt,
            LoyaltyTransaction.created_at <= end_dt
        ).scalar() or 0

        # Top rewards by redemptions
        top_rewards = db.session.query(
            LoyaltyReward.id,
            LoyaltyReward.name,
            LoyaltyReward.points_cost,
            LoyaltyReward.redemptions_used
        ).filter(
            LoyaltyReward.redemptions_used > 0
        ).order_by(
            LoyaltyReward.redemptions_used.desc()
        ).limit(10).all()

        top_rewards_list = [
            {
                'reward_id': reward_id,
                'name': name,
                'points_cost': points_cost,
                'redemptions': redemptions_used
            }
            for reward_id, name, points_cost, redemptions_used in top_rewards
        ]

        # Tier distribution
        tier_distribution = db.session.query(
            LoyaltyPoints.current_tier,
            func.count(LoyaltyPoints.id)
        ).group_by(LoyaltyPoints.current_tier).all()

        tier_stats = {
            tier: count for tier, count in tier_distribution
        }

        analytics = {
            'period': {
                'start_date': start_dt.isoformat(),
                'end_date': end_dt.isoformat()
            },
            'active_members': active_members,
            'total_points_in_circulation': total_points_value,
            'period_stats': {
                'points_earned': earned_points,
                'points_redeemed': total_redeemed_points,
                'total_redemptions': total_redemptions,
                'avg_redemption_value': round(total_redeemed_points / total_redemptions, 2) if total_redemptions > 0 else 0
            },
            'top_rewards': top_rewards_list,
            'tier_distribution': tier_stats
        }

        return success_response(data={'analytics': analytics})

    except Exception as e:
        current_app.logger.error(f"Get loyalty analytics error: {e}")
        import traceback
        current_app.logger.error(traceback.format_exc())
        return internal_error_response('Failed to get loyalty analytics')


# ============================================================================
# NOTIFICATION TEMPLATE MANAGEMENT ENDPOINTS
# ============================================================================

@admin_bp.route('/notification-templates', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_notifications', 'manage_notifications'])
def get_notification_templates():
    """
    Get all notification templates with filtering

    Query Parameters:
        - page: Page number (default: 1)
        - per_page: Items per page (default: 20)
        - notification_type: Filter by type
        - channel: Filter by channel (email, sms, push, telegram)
        - is_active: Filter by active status
        - search: Search in name, subject, content
    """
    try:
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 20)), 100)

        # Build query
        query = NotificationTemplate.query

        # Type filter
        notification_type = request.args.get('notification_type')
        if notification_type:
            query = query.filter_by(notification_type=notification_type)

        # Channel filter
        channel = request.args.get('channel')
        if channel:
            query = query.filter_by(channel=channel)

        # Active filter
        is_active = request.args.get('is_active')
        if is_active is not None:
            is_active_bool = is_active.lower() == 'true'
            query = query.filter_by(is_active=is_active_bool)

        # Search
        search = request.args.get('search')
        if search:
            query = query.filter(
                or_(
                    NotificationTemplate.name.ilike(f'%{search}%'),
                    NotificationTemplate.subject.ilike(f'%{search}%'),
                    NotificationTemplate.content.ilike(f'%{search}%')
                )
            )

        # Sort by type and channel
        query = query.order_by(NotificationTemplate.notification_type.asc(), NotificationTemplate.channel.asc())

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize templates
        language = get_current_language()
        templates = [template.to_dict(language=language) for template in pagination.items]

        return paginated_response(
            items=templates,
            total=pagination.total,
            page=page,
            per_page=per_page
        )

    except Exception as e:
        current_app.logger.error(f"Get notification templates error: {e}")
        return internal_error_response('Failed to get notification templates')


@admin_bp.route('/notification-templates/<int:template_id>', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_notifications', 'manage_notifications'])
def get_notification_template_detail(template_id):
    """Get detailed information about a specific notification template"""
    try:
        template = NotificationTemplate.query.get(template_id)

        if not template:
            return not_found_response('Notification template not found')

        language = get_current_language()
        template_data = template.to_dict(language=language, include_all_translations=True)

        return success_response(data={'template': template_data})

    except Exception as e:
        current_app.logger.error(f"Get notification template detail error: {e}")
        return internal_error_response('Failed to get notification template detail')


@admin_bp.route('/notification-templates', methods=['POST'])
@jwt_required()
@validate_admin_action(['manage_notifications'])
@validate_json(['name', 'notification_type', 'channel', 'content'])
def create_notification_template():
    """
    Create a new notification template

    Request Body:
        - name: Template name
        - notification_type: Type (order_update, delivery_reminder, etc.)
        - channel: Channel (email, sms, push, telegram)
        - subject: Email subject (required for email channel)
        - content: Template content with placeholders
        - is_active: Active status (default: true)
        - translations: Multilingual content
    """
    try:
        data = request.get_json()

        # Validate channel
        valid_channels = ['email', 'sms', 'push', 'telegram', 'in_app']
        channel = data.get('channel')
        if channel not in valid_channels:
            return validation_error_response(f'Invalid channel. Must be one of: {", ".join(valid_channels)}')

        # Email templates require subject
        if channel == 'email' and not data.get('subject'):
            return validation_error_response('subject is required for email templates')

        # Check for duplicate template (same type + channel)
        existing = NotificationTemplate.query.filter_by(
            notification_type=data.get('notification_type'),
            channel=channel
        ).first()

        if existing:
            return validation_error_response(f'Template already exists for {data.get("notification_type")} on {channel} channel')

        # Create template
        template = NotificationTemplate(
            name=data.get('name'),
            notification_type=data.get('notification_type'),
            channel=channel,
            subject=data.get('subject'),
            content=data.get('content'),
            is_active=data.get('is_active', True)
        )

        db.session.add(template)
        db.session.flush()

        # Handle translations
        if data.get('translations'):
            template.set_translations(data['translations'])

        db.session.commit()

        language = get_current_language()
        return created_response(
            data={'template': template.to_dict(language=language)},
            message='Notification template created successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create notification template error: {e}")
        import traceback
        current_app.logger.error(traceback.format_exc())
        return internal_error_response('Failed to create notification template')


@admin_bp.route('/notification-templates/<int:template_id>', methods=['PUT'])
@jwt_required()
@validate_admin_action(['manage_notifications'])
def update_notification_template(template_id):
    """Update an existing notification template"""
    try:
        template = NotificationTemplate.query.get(template_id)

        if not template:
            return not_found_response('Notification template not found')

        data = request.get_json()

        # Update fields
        if 'name' in data:
            template.name = data['name']
        if 'subject' in data:
            template.subject = data['subject']
        if 'content' in data:
            template.content = data['content']
        if 'is_active' in data:
            template.is_active = data['is_active']

        # Handle translations
        if data.get('translations'):
            template.set_translations(data['translations'])

        db.session.commit()

        language = get_current_language()
        return success_response(
            data={'template': template.to_dict(language=language)},
            message='Notification template updated successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update notification template error: {e}")
        return internal_error_response('Failed to update notification template')


@admin_bp.route('/notification-templates/<int:template_id>', methods=['DELETE'])
@jwt_required()
@validate_admin_action(['manage_notifications'])
def delete_notification_template(template_id):
    """Delete a notification template"""
    try:
        template = NotificationTemplate.query.get(template_id)

        if not template:
            return not_found_response('Notification template not found')

        # Just deactivate instead of deleting to preserve history
        template.is_active = False
        db.session.commit()

        return success_response(message=get_translation('api.admin.success.template_deactivated'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete notification template error: {e}")
        return internal_error_response('Failed to delete notification template')


@admin_bp.route('/notification-templates/<int:template_id>/preview', methods=['POST'])
@jwt_required()
@validate_admin_action(['view_notifications', 'manage_notifications'])
def preview_notification_template(template_id):
    """
    Preview a notification template with sample data

    Request Body:
        - variables: Dictionary of placeholder values
        - language: Language code (optional)
    """
    try:
        template = NotificationTemplate.query.get(template_id)

        if not template:
            return not_found_response('Notification template not found')

        data = request.get_json() or {}
        variables = data.get('variables', {})
        language = data.get('language') or get_current_language()

        # Get localized content
        template_data = template.to_dict(language=language)

        # Simple placeholder replacement
        preview_subject = template_data.get('subject', '')
        preview_content = template_data.get('content', '')

        for key, value in variables.items():
            placeholder = f'{{{{{key}}}}}'  # {{variable}}
            preview_subject = preview_subject.replace(placeholder, str(value))
            preview_content = preview_content.replace(placeholder, str(value))

        preview = {
            'template_id': template.id,
            'template_name': template_data.get('name'),
            'notification_type': template.notification_type,
            'channel': template.channel,
            'language': language,
            'subject': preview_subject,
            'content': preview_content,
            'variables_used': list(variables.keys())
        }

        return success_response(data={'preview': preview})

    except Exception as e:
        current_app.logger.error(f"Preview notification template error: {e}")
        return internal_error_response('Failed to preview notification template')


@admin_bp.route('/notification-templates/types', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_notifications', 'manage_notifications'])
def get_notification_types():
    """Get list of available notification types"""
    try:
        # Get distinct notification types from templates
        types = db.session.query(NotificationTemplate.notification_type).distinct().all()

        # Common notification types
        common_types = [
            'order_created',
            'order_confirmed',
            'order_shipped',
            'order_delivered',
            'order_cancelled',
            'delivery_reminder',
            'payment_received',
            'payment_failed',
            'subscription_created',
            'subscription_renewed',
            'subscription_expiring',
            'subscription_cancelled',
            'loyalty_points_earned',
            'loyalty_reward_available',
            'welcome',
            'password_reset',
            'account_verified',
            'promotional',
            'announcement'
        ]

        # Combine existing types with common types
        all_types = set([t[0] for t in types] + common_types)

        types_data = [
            {
                'value': type_name,
                'label': type_name.replace('_', ' ').title(),
                'in_use': type_name in [t[0] for t in types]
            }
            for type_name in sorted(all_types)
        ]

        return success_response(data={'types': types_data})

    except Exception as e:
        current_app.logger.error(f"Get notification types error: {e}")
        return internal_error_response('Failed to get notification types')


@admin_bp.route('/notification-templates/channels', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_notifications', 'manage_notifications'])
def get_notification_channels():
    """Get list of available notification channels"""
    try:
        channels = [
            {
                'value': 'email',
                'label': 'Email',
                'requires_subject': True,
                'icon': 'email'
            },
            {
                'value': 'sms',
                'label': 'SMS',
                'requires_subject': False,
                'icon': 'message'
            },
            {
                'value': 'push',
                'label': 'Push Notification',
                'requires_subject': False,
                'icon': 'notifications'
            },
            {
                'value': 'telegram',
                'label': 'Telegram',
                'requires_subject': False,
                'icon': 'telegram'
            },
            {
                'value': 'in_app',
                'label': 'In-App Notification',
                'requires_subject': False,
                'icon': 'inbox'
            }
        ]

        return success_response(data={'channels': channels})

    except Exception as e:
        current_app.logger.error(f"Get notification channels error: {e}")
        return internal_error_response('Failed to get notification channels')


@admin_bp.route('/system-settings', methods=['GET'])
@jwt_required()
@super_admin_required
def get_system_settings():
    """Get system settings"""
    try:
        # Get current system settings from app config and database
        settings = {
            'general': {
                'app_name': current_app.config.get('APP_NAME', 'BlueStream Water Delivery'),
                'timezone': current_app.config.get('TIMEZONE', 'Asia/Tashkent'),
                'default_language': current_app.config.get('DEFAULT_LANGUAGE', 'uz'),
                'supported_languages': current_app.config.get('SUPPORTED_LANGUAGES', ['uz', 'ru', 'en']),
                'maintenance_mode': current_app.config.get('MAINTENANCE_MODE', False)
            },
            'business': {
                'currency': current_app.config.get('CURRENCY', 'UZS'),
                'currency_symbol': current_app.config.get('CURRENCY_SYMBOL', 'сум'),
                'tax_rate': current_app.config.get('TAX_RATE', 0),
                'min_order_amount': current_app.config.get('MIN_ORDER_AMOUNT', 0),
                'free_delivery_threshold': current_app.config.get('FREE_DELIVERY_THRESHOLD', 0),
                'default_delivery_fee': current_app.config.get('DEFAULT_DELIVERY_FEE', 0)
            },
            'orders': {
                'auto_cancel_pending_orders_hours': current_app.config.get('AUTO_CANCEL_PENDING_ORDERS_HOURS', 24),
                'allow_order_cancellation_minutes': current_app.config.get('ALLOW_ORDER_CANCELLATION_MINUTES', 30),
                'max_order_items': current_app.config.get('MAX_ORDER_ITEMS', 50),
                'order_number_prefix': current_app.config.get('ORDER_NUMBER_PREFIX', 'ORD')
            },
            'delivery': {
                'delivery_radius_km': current_app.config.get('DELIVERY_RADIUS_KM', 50),
                'delivery_slots_enabled': current_app.config.get('DELIVERY_SLOTS_ENABLED', True),
                'same_day_delivery_cutoff_hour': current_app.config.get('SAME_DAY_DELIVERY_CUTOFF_HOUR', 14),
                'avg_delivery_time_minutes': current_app.config.get('AVG_DELIVERY_TIME_MINUTES', 60)
            },
            'loyalty': {
                'loyalty_enabled': current_app.config.get('LOYALTY_ENABLED', True),
                'points_per_uzs': current_app.config.get('LOYALTY_POINTS_PER_UZS', 1),
                'signup_bonus_points': current_app.config.get('LOYALTY_SIGNUP_BONUS', 100),
                'referral_bonus_points': current_app.config.get('LOYALTY_REFERRAL_BONUS', 50),
                'points_expiry_days': current_app.config.get('LOYALTY_POINTS_EXPIRY_DAYS', 365)
            },
            'notifications': {
                'sms_enabled': current_app.config.get('SMS_ENABLED', True),
                'email_enabled': current_app.config.get('EMAIL_ENABLED', True),
                'telegram_enabled': current_app.config.get('TELEGRAM_ENABLED', True),
                'push_enabled': current_app.config.get('PUSH_ENABLED', False),
                'order_confirmation_sms': current_app.config.get('ORDER_CONFIRMATION_SMS', True),
                'delivery_reminder_sms': current_app.config.get('DELIVERY_REMINDER_SMS', True)
            },
            'payments': {
                'payment_methods_enabled': current_app.config.get('PAYMENT_METHODS_ENABLED', ['cash', 'card', 'online']),
                'default_payment_method': current_app.config.get('DEFAULT_PAYMENT_METHOD', 'cash'),
                'payment_timeout_minutes': current_app.config.get('PAYMENT_TIMEOUT_MINUTES', 15),
                'auto_refund_failed_deliveries': current_app.config.get('AUTO_REFUND_FAILED_DELIVERIES', True)
            },
            'security': {
                'max_login_attempts': current_app.config.get('MAX_LOGIN_ATTEMPTS', 5),
                'login_lockout_minutes': current_app.config.get('LOGIN_LOCKOUT_MINUTES', 30),
                'password_min_length': current_app.config.get('PASSWORD_MIN_LENGTH', 8),
                'session_timeout_minutes': current_app.config.get('SESSION_TIMEOUT_MINUTES', 1440),
                'require_email_verification': current_app.config.get('REQUIRE_EMAIL_VERIFICATION', False),
                'require_phone_verification': current_app.config.get('REQUIRE_PHONE_VERIFICATION', True)
            },
            'api': {
                'rate_limit_enabled': current_app.config.get('RATE_LIMIT_ENABLED', True),
                'rate_limit_per_minute': current_app.config.get('RATE_LIMIT_PER_MINUTE', 60),
                'api_version': current_app.config.get('API_VERSION', 'v1'),
                'cors_enabled': current_app.config.get('CORS_ENABLED', True)
            },
            'files': {
                'max_upload_size_mb': current_app.config.get('MAX_UPLOAD_SIZE_MB', 10),
                'allowed_image_extensions': current_app.config.get('ALLOWED_IMAGE_EXTENSIONS', ['jpg', 'jpeg', 'png', 'webp']),
                'allowed_document_extensions': current_app.config.get('ALLOWED_DOCUMENT_EXTENSIONS', ['pdf', 'doc', 'docx'])
            }
        }

        return success_response(data={'settings': settings})

    except Exception as e:
        current_app.logger.error(f"Get system settings error: {e}")
        return internal_error_response('Failed to get system settings')


@admin_bp.route('/system-settings', methods=['PUT'])
@jwt_required()
@super_admin_required
@validate_json()
def update_system_settings():
    """Update system settings"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        if not data:
            return validation_error_response('Settings data is required')

        # Track which settings were updated
        updated_settings = []

        # Update settings categories
        categories = ['general', 'business', 'orders', 'delivery', 'loyalty', 'notifications', 'payments', 'security', 'api', 'files']

        for category in categories:
            if category in data:
                category_settings = data[category]

                # Validate and update each setting in the category
                for key, value in category_settings.items():
                    setting_key = f"{category.upper()}_{key.upper()}"

                    # Validate specific settings
                    if category == 'business':
                        if key == 'tax_rate' and (value < 0 or value > 100):
                            return validation_error_response('Tax rate must be between 0 and 100')
                        if key in ['min_order_amount', 'default_delivery_fee'] and value < 0:
                            return validation_error_response(f'{key} cannot be negative')

                    if category == 'orders':
                        if key in ['auto_cancel_pending_orders_hours', 'allow_order_cancellation_minutes', 'max_order_items'] and value < 0:
                            return validation_error_response(f'{key} must be positive')

                    if category == 'delivery':
                        if key == 'delivery_radius_km' and value <= 0:
                            return validation_error_response('Delivery radius must be positive')
                        if key == 'same_day_delivery_cutoff_hour' and (value < 0 or value > 23):
                            return validation_error_response('Cutoff hour must be between 0 and 23')

                    if category == 'loyalty':
                        if key in ['points_per_uzs', 'signup_bonus_points', 'referral_bonus_points'] and value < 0:
                            return validation_error_response(f'{key} cannot be negative')

                    if category == 'security':
                        if key == 'password_min_length' and value < 6:
                            return validation_error_response('Password minimum length must be at least 6')
                        if key in ['max_login_attempts', 'login_lockout_minutes', 'session_timeout_minutes'] and value <= 0:
                            return validation_error_response(f'{key} must be positive')

                    if category == 'api':
                        if key == 'rate_limit_per_minute' and value <= 0:
                            return validation_error_response('Rate limit must be positive')

                    if category == 'files':
                        if key == 'max_upload_size_mb' and value <= 0:
                            return validation_error_response('Max upload size must be positive')

                    # Update config (runtime only - would need env file or database persistence for permanent storage)
                    current_app.config[setting_key] = value
                    updated_settings.append(setting_key)

        # Log the settings update for audit
        from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity

        audit_logger.log_event(
            event_type=AuditEventType.SYSTEM_MAINTENANCE,
            action='update_system_settings',
            severity=AuditSeverity.HIGH,
            resource_type='system_settings',
            description=f'System settings updated: {", ".join(updated_settings)}',
            new_values=data,
            success=True
        )

        return success_response(
            message=f'System settings updated successfully ({len(updated_settings)} settings changed)',
            data={
                'updated_settings': updated_settings,
                'note': 'Settings are updated in runtime. For permanent changes, update environment variables or configuration files.'
            }
        )

    except Exception as e:
        current_app.logger.error(f"Update system settings error: {e}")
        return internal_error_response('Failed to update system settings')


@admin_bp.route('/system-settings/categories', methods=['GET'])
@jwt_required()
@super_admin_required
def get_system_settings_categories():
    """Get list of system settings categories with descriptions"""
    try:
        categories = [
            {
                'key': 'general',
                'name': 'General Settings',
                'description': 'Application name, timezone, language, and maintenance mode'
            },
            {
                'key': 'business',
                'name': 'Business Settings',
                'description': 'Currency, tax rate, minimum order amount, delivery fees'
            },
            {
                'key': 'orders',
                'name': 'Order Settings',
                'description': 'Order cancellation, auto-cancel, maximum items'
            },
            {
                'key': 'delivery',
                'name': 'Delivery Settings',
                'description': 'Delivery radius, time slots, same-day delivery cutoff'
            },
            {
                'key': 'loyalty',
                'name': 'Loyalty Program Settings',
                'description': 'Points earning, bonuses, expiry, and program features'
            },
            {
                'key': 'notifications',
                'name': 'Notification Settings',
                'description': 'SMS, email, Telegram, push notifications configuration'
            },
            {
                'key': 'payments',
                'name': 'Payment Settings',
                'description': 'Payment methods, timeouts, auto-refunds'
            },
            {
                'key': 'security',
                'name': 'Security Settings',
                'description': 'Login attempts, password rules, verification requirements'
            },
            {
                'key': 'api',
                'name': 'API Settings',
                'description': 'Rate limiting, versioning, CORS configuration'
            },
            {
                'key': 'files',
                'name': 'File Upload Settings',
                'description': 'Maximum upload size, allowed file extensions'
            }
        ]

        return success_response(data={'categories': categories})

    except Exception as e:
        current_app.logger.error(f"Get system settings categories error: {e}")
        return internal_error_response('Failed to get system settings categories')


@admin_bp.route('/system-settings/reset', methods=['POST'])
@jwt_required()
@super_admin_required
@validate_json()
def reset_system_settings():
    """Reset system settings to defaults"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        category = data.get('category')

        if category and category not in ['general', 'business', 'orders', 'delivery', 'loyalty', 'notifications', 'payments', 'security', 'api', 'files']:
            return validation_error_response('Invalid category')

        # Define default values
        defaults = {
            'GENERAL_APP_NAME': 'BlueStream Water Delivery',
            'GENERAL_TIMEZONE': 'Asia/Tashkent',
            'GENERAL_DEFAULT_LANGUAGE': 'uz',
            'GENERAL_SUPPORTED_LANGUAGES': ['uz', 'ru', 'en'],
            'GENERAL_MAINTENANCE_MODE': False,
            'BUSINESS_CURRENCY': 'UZS',
            'BUSINESS_CURRENCY_SYMBOL': 'сум',
            'BUSINESS_TAX_RATE': 0,
            'BUSINESS_MIN_ORDER_AMOUNT': 0,
            'BUSINESS_FREE_DELIVERY_THRESHOLD': 0,
            'BUSINESS_DEFAULT_DELIVERY_FEE': 0,
            'ORDERS_AUTO_CANCEL_PENDING_ORDERS_HOURS': 24,
            'ORDERS_ALLOW_ORDER_CANCELLATION_MINUTES': 30,
            'ORDERS_MAX_ORDER_ITEMS': 50,
            'ORDERS_ORDER_NUMBER_PREFIX': 'ORD',
            'DELIVERY_DELIVERY_RADIUS_KM': 50,
            'DELIVERY_DELIVERY_SLOTS_ENABLED': True,
            'DELIVERY_SAME_DAY_DELIVERY_CUTOFF_HOUR': 14,
            'DELIVERY_AVG_DELIVERY_TIME_MINUTES': 60,
            'LOYALTY_LOYALTY_ENABLED': True,
            'LOYALTY_POINTS_PER_UZS': 1,
            'LOYALTY_SIGNUP_BONUS_POINTS': 100,
            'LOYALTY_REFERRAL_BONUS_POINTS': 50,
            'LOYALTY_POINTS_EXPIRY_DAYS': 365,
            'NOTIFICATIONS_SMS_ENABLED': True,
            'NOTIFICATIONS_EMAIL_ENABLED': True,
            'NOTIFICATIONS_TELEGRAM_ENABLED': True,
            'NOTIFICATIONS_PUSH_ENABLED': False,
            'NOTIFICATIONS_ORDER_CONFIRMATION_SMS': True,
            'NOTIFICATIONS_DELIVERY_REMINDER_SMS': True,
            'PAYMENTS_PAYMENT_METHODS_ENABLED': ['cash', 'card', 'online'],
            'PAYMENTS_DEFAULT_PAYMENT_METHOD': 'cash',
            'PAYMENTS_PAYMENT_TIMEOUT_MINUTES': 15,
            'PAYMENTS_AUTO_REFUND_FAILED_DELIVERIES': True,
            'SECURITY_MAX_LOGIN_ATTEMPTS': 5,
            'SECURITY_LOGIN_LOCKOUT_MINUTES': 30,
            'SECURITY_PASSWORD_MIN_LENGTH': 8,
            'SECURITY_SESSION_TIMEOUT_MINUTES': 1440,
            'SECURITY_REQUIRE_EMAIL_VERIFICATION': False,
            'SECURITY_REQUIRE_PHONE_VERIFICATION': True,
            'API_RATE_LIMIT_ENABLED': True,
            'API_RATE_LIMIT_PER_MINUTE': 60,
            'API_API_VERSION': 'v1',
            'API_CORS_ENABLED': True,
            'FILES_MAX_UPLOAD_SIZE_MB': 10,
            'FILES_ALLOWED_IMAGE_EXTENSIONS': ['jpg', 'jpeg', 'png', 'webp'],
            'FILES_ALLOWED_DOCUMENT_EXTENSIONS': ['pdf', 'doc', 'docx']
        }

        reset_settings = []

        # Reset specific category or all settings
        if category:
            prefix = f"{category.upper()}_"
            for key, value in defaults.items():
                if key.startswith(prefix):
                    current_app.config[key] = value
                    reset_settings.append(key)
        else:
            # Reset all settings
            for key, value in defaults.items():
                current_app.config[key] = value
                reset_settings.append(key)

        # Log the reset for audit
        from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity

        audit_logger.log_event(
            event_type=AuditEventType.SYSTEM_MAINTENANCE,
            action='reset_system_settings',
            severity=AuditSeverity.HIGH,
            resource_type='system_settings',
            description=f'System settings reset to defaults: {category if category else "all categories"}',
            success=True
        )

        return success_response(
            message=f'System settings reset to defaults ({len(reset_settings)} settings)',
            data={'reset_settings': reset_settings}
        )

    except Exception as e:
        current_app.logger.error(f"Reset system settings error: {e}")
        return internal_error_response('Failed to reset system settings')


@admin_bp.route('/audit-logs', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_audit_logs', 'super_admin'])
def get_audit_logs():
    """
    Get audit logs with comprehensive filtering

    Query Parameters:
        - page: Page number (default: 1)
        - per_page: Items per page (max: 100, default: 50)
        - event_type: Filter by event type (login_success, order_created, etc.)
        - severity: Filter by severity (low, medium, high, critical)
        - user_id: Filter by user ID
        - resource_type: Filter by resource type (user, order, product, etc.)
        - resource_id: Filter by specific resource ID
        - action: Filter by action name
        - success: Filter by success status (true/false)
        - start_date: Filter logs after this date (ISO format)
        - end_date: Filter logs before this date (ISO format)
        - ip_address: Filter by IP address
        - search: Search in description and action fields
        - sort_by: Sort field (created_at, severity, duration_ms) - default: created_at
        - sort_order: Sort order (asc, desc) - default: desc
    """
    try:
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 50)), 100)

        # Build query
        query = AuditLog.query

        # Event type filter
        event_type = request.args.get('event_type')
        if event_type:
            try:
                query = query.filter_by(event_type=AuditEventType(event_type))
            except ValueError:
                return validation_error_response(f"Invalid event_type: {event_type}")

        # Severity filter
        severity = request.args.get('severity')
        if severity:
            try:
                query = query.filter_by(severity=AuditSeverity(severity))
            except ValueError:
                return validation_error_response(f"Invalid severity: {severity}")

        # User filter
        user_id = request.args.get('user_id', type=int)
        if user_id:
            query = query.filter_by(user_id=user_id)

        # Resource filters
        resource_type = request.args.get('resource_type')
        if resource_type:
            query = query.filter_by(resource_type=resource_type)

        resource_id = request.args.get('resource_id')
        if resource_id:
            query = query.filter_by(resource_id=resource_id)

        # Action filter
        action = request.args.get('action')
        if action:
            query = query.filter(AuditLog.action.ilike(f'%{action}%'))

        # Success filter
        success = request.args.get('success')
        if success is not None:
            success_bool = success.lower() == 'true'
            query = query.filter_by(success=success_bool)

        # Date range filter
        start_date = request.args.get('start_date')
        if start_date:
            try:
                start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
                query = query.filter(AuditLog.created_at >= start_dt)
            except ValueError:
                return validation_error_response("Invalid start_date format. Use ISO format.")

        end_date = request.args.get('end_date')
        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
                query = query.filter(AuditLog.created_at <= end_dt)
            except ValueError:
                return validation_error_response("Invalid end_date format. Use ISO format.")

        # IP address filter
        ip_address = request.args.get('ip_address')
        if ip_address:
            query = query.filter_by(ip_address=ip_address)

        # Search in description and action
        search = request.args.get('search')
        if search:
            query = query.filter(
                or_(
                    AuditLog.description.ilike(f'%{search}%'),
                    AuditLog.action.ilike(f'%{search}%')
                )
            )

        # Sorting
        sort_by = request.args.get('sort_by', 'created_at')
        sort_order = request.args.get('sort_order', 'desc')

        if sort_by == 'severity':
            sort_field = AuditLog.severity
        elif sort_by == 'duration_ms':
            sort_field = AuditLog.duration_ms
        else:
            sort_field = AuditLog.created_at

        if sort_order == 'asc':
            query = query.order_by(sort_field.asc())
        else:
            query = query.order_by(sort_field.desc())

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize logs
        logs = []
        for log in pagination.items:
            log_data = log.to_dict()

            # Add user info if available
            if log.user_id:
                user = User.query.get(log.user_id)
                if user:
                    log_data['user'] = {
                        'id': user.id,
                        'name': user.name,
                        'email': user.email,
                        'role': user.role
                    }

            logs.append(log_data)

        return paginated_response(
            items=logs,
            total=pagination.total,
            page=page,
            per_page=per_page
        )

    except Exception as e:
        current_app.logger.error(f"Get audit logs error: {e}")
        return internal_error_response('Failed to get audit logs')


@admin_bp.route('/audit-logs/<int:log_id>', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_audit_logs', 'super_admin'])
def get_audit_log_detail(log_id):
    """Get detailed information about a specific audit log entry"""
    try:
        log = AuditLog.query.get(log_id)

        if not log:
            return not_found_response('Audit log not found')

        log_data = log.to_dict()

        # Add related information
        if log.user_id:
            user = User.query.get(log.user_id)
            if user:
                log_data['user'] = {
                    'id': user.id,
                    'name': user.name,
                    'email': user.email,
                    'phone': user.phone,
                    'role': user.role,
                    'status': user.status
                }

        # Add related resource information if applicable
        if log.resource_type and log.resource_id:
            if log.resource_type == 'order':
                order = Order.query.get(log.resource_id)
                if order:
                    log_data['resource'] = {
                        'type': 'order',
                        'id': order.id,
                        'order_number': order.order_number,
                        'status': order.status,
                        'total_amount': float(order.total_amount)
                    }
            elif log.resource_type == 'product':
                product = Product.query.get(log.resource_id)
                if product:
                    log_data['resource'] = {
                        'type': 'product',
                        'id': product.id,
                        'name': product.name,
                        'is_active': product.is_active
                    }
            elif log.resource_type == 'user':
                user = User.query.get(log.resource_id)
                if user:
                    log_data['resource'] = {
                        'type': 'user',
                        'id': user.id,
                        'name': user.name,
                        'email': user.email,
                        'role': user.role
                    }

        return success_response(data={'audit_log': log_data})

    except Exception as e:
        current_app.logger.error(f"Get audit log detail error: {e}")
        return internal_error_response('Failed to get audit log detail')


@admin_bp.route('/audit-logs/analytics', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_audit_logs', 'super_admin'])
def get_audit_log_analytics():
    """
    Get analytics and statistics for audit logs

    Query Parameters:
        - start_date: Start date for analytics (ISO format)
        - end_date: End date for analytics (ISO format)
        - period: Time period (day, week, month) - default: month
    """
    try:
        # Date range
        end_date = request.args.get('end_date')
        if end_date:
            end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
        else:
            end_dt = datetime.now(UTC)

        period = request.args.get('period', 'month')
        if period == 'day':
            start_dt = end_dt - timedelta(days=1)
        elif period == 'week':
            start_dt = end_dt - timedelta(days=7)
        else:  # month
            start_dt = end_dt - timedelta(days=30)

        start_date = request.args.get('start_date')
        if start_date:
            start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))

        # Base query for period
        base_query = AuditLog.query.filter(
            AuditLog.created_at >= start_dt,
            AuditLog.created_at <= end_dt
        )

        # Total events
        total_events = base_query.count()

        # Events by severity
        severity_breakdown = db.session.query(
            AuditLog.severity,
            func.count(AuditLog.id)
        ).filter(
            AuditLog.created_at >= start_dt,
            AuditLog.created_at <= end_dt
        ).group_by(AuditLog.severity).all()

        severity_stats = {
            severity.value: count for severity, count in severity_breakdown
        }

        # Events by type
        event_type_breakdown = db.session.query(
            AuditLog.event_type,
            func.count(AuditLog.id)
        ).filter(
            AuditLog.created_at >= start_dt,
            AuditLog.created_at <= end_dt
        ).group_by(AuditLog.event_type).order_by(
            func.count(AuditLog.id).desc()
        ).limit(10).all()

        event_type_stats = {
            event_type.value: count for event_type, count in event_type_breakdown
        }

        # Success vs failure rate
        success_count = base_query.filter_by(success=True).count()
        failure_count = base_query.filter_by(success=False).count()

        # Top users by activity
        top_users = db.session.query(
            AuditLog.user_id,
            User.name,
            User.email,
            User.role,
            func.count(AuditLog.id).label('event_count')
        ).join(
            User, AuditLog.user_id == User.id
        ).filter(
            AuditLog.created_at >= start_dt,
            AuditLog.created_at <= end_dt,
            AuditLog.user_id.isnot(None)
        ).group_by(
            AuditLog.user_id, User.name, User.email, User.role
        ).order_by(
            desc('event_count')
        ).limit(10).all()

        top_users_list = [
            {
                'user_id': user_id,
                'name': name,
                'email': email,
                'role': role,
                'event_count': event_count
            }
            for user_id, name, email, role, event_count in top_users
        ]

        # Security events
        security_event_types = [
            AuditEventType.PERMISSION_DENIED,
            AuditEventType.SUSPICIOUS_ACTIVITY,
            AuditEventType.LOGIN_FAILURE
        ]

        security_events = AuditLog.query.filter(
            AuditLog.created_at >= start_dt,
            AuditLog.created_at <= end_dt,
            AuditLog.event_type.in_(security_event_types)
        ).count()

        # Failed operations by resource type
        failed_operations = db.session.query(
            AuditLog.resource_type,
            func.count(AuditLog.id)
        ).filter(
            AuditLog.created_at >= start_dt,
            AuditLog.created_at <= end_dt,
            AuditLog.success == False
        ).group_by(AuditLog.resource_type).all()

        failed_ops_stats = {
            resource_type: count for resource_type, count in failed_operations if resource_type
        }

        # Average duration by event type (top 10)
        avg_duration = db.session.query(
            AuditLog.event_type,
            func.avg(AuditLog.duration_ms).label('avg_duration')
        ).filter(
            AuditLog.created_at >= start_dt,
            AuditLog.created_at <= end_dt,
            AuditLog.duration_ms.isnot(None)
        ).group_by(AuditLog.event_type).order_by(
            desc('avg_duration')
        ).limit(10).all()

        duration_stats = {
            event_type.value: round(avg_dur, 2) for event_type, avg_dur in avg_duration if avg_dur
        }

        # Events over time (daily breakdown)
        daily_events = db.session.query(
            func.date(AuditLog.created_at).label('date'),
            func.count(AuditLog.id).label('count')
        ).filter(
            AuditLog.created_at >= start_dt,
            AuditLog.created_at <= end_dt
        ).group_by('date').order_by('date').all()

        timeline = [
            {
                'date': date.isoformat() if date else None,
                'count': count
            }
            for date, count in daily_events
        ]

        analytics = {
            'period': {
                'start_date': start_dt.isoformat(),
                'end_date': end_dt.isoformat(),
                'period': period
            },
            'total_events': total_events,
            'success_rate': round((success_count / total_events * 100), 2) if total_events > 0 else 0,
            'failure_rate': round((failure_count / total_events * 100), 2) if total_events > 0 else 0,
            'severity_breakdown': severity_stats,
            'event_type_breakdown': event_type_stats,
            'security_events': security_events,
            'top_users': top_users_list,
            'failed_operations_by_type': failed_ops_stats,
            'average_duration_by_event_type': duration_stats,
            'timeline': timeline
        }

        return success_response(data={'analytics': analytics})

    except Exception as e:
        current_app.logger.error(f"Get audit log analytics error: {e}")
        return internal_error_response('Failed to get audit log analytics')


@admin_bp.route('/audit-logs/export', methods=['POST'])
@jwt_required()
@validate_admin_action(['export_data', 'super_admin'])
def export_audit_logs():
    """
    Export audit logs to CSV/JSON format

    Request Body:
        - format: Export format (csv, json) - default: csv
        - filters: Same filters as get_audit_logs endpoint
        - start_date: Start date (ISO format)
        - end_date: End date (ISO format)
    """
    try:
        from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity

        data = request.get_json() or {}
        export_format = data.get('format', 'csv')
        filters = data.get('filters', {})

        # Build query with filters
        query = AuditLog.query

        # Apply filters (similar to get_audit_logs)
        if filters.get('event_type'):
            query = query.filter_by(event_type=AuditEventType(filters['event_type']))
        if filters.get('severity'):
            query = query.filter_by(severity=AuditSeverity(filters['severity']))
        if filters.get('user_id'):
            query = query.filter_by(user_id=filters['user_id'])
        if filters.get('start_date'):
            start_dt = datetime.fromisoformat(filters['start_date'].replace('Z', '+00:00'))
            query = query.filter(AuditLog.created_at >= start_dt)
        if filters.get('end_date'):
            end_dt = datetime.fromisoformat(filters['end_date'].replace('Z', '+00:00'))
            query = query.filter(AuditLog.created_at <= end_dt)

        # Limit to prevent excessive exports
        query = query.order_by(AuditLog.created_at.desc()).limit(10000)

        logs = query.all()

        # Log the export action
        current_user_id = get_jwt_identity()
        audit_logger.log_event(
            event_type=AuditEventType.DATA_EXPORT,
            action='export_audit_logs',
            severity=AuditSeverity.HIGH,
            resource_type='audit_logs',
            description=f"Exported {len(logs)} audit logs in {export_format} format",
            additional_data={
                'format': export_format,
                'filters': filters,
                'record_count': len(logs)
            }
        )

        if export_format == 'json':
            export_data = [log.to_dict() for log in logs]
            return success_response(data={
                'export': export_data,
                'format': 'json',
                'record_count': len(logs)
            })
        else:
            # CSV format
            import csv
            from io import StringIO

            output = StringIO()
            fieldnames = [
                'event_id', 'event_type', 'severity', 'user_id', 'action',
                'resource_type', 'resource_id', 'success', 'ip_address',
                'description', 'created_at'
            ]

            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()

            for log in logs:
                writer.writerow({
                    'event_id': log.event_id,
                    'event_type': log.event_type.value,
                    'severity': log.severity.value,
                    'user_id': log.user_id,
                    'action': log.action,
                    'resource_type': log.resource_type,
                    'resource_id': log.resource_id,
                    'success': log.success,
                    'ip_address': log.ip_address,
                    'description': log.description,
                    'created_at': log.created_at.isoformat() if log.created_at else ''
                })

            csv_data = output.getvalue()

            return success_response(data={
                'export': csv_data,
                'format': 'csv',
                'record_count': len(logs)
            })

    except Exception as e:
        current_app.logger.error(f"Export audit logs error: {e}")
        return internal_error_response('Failed to export audit logs')


@admin_bp.route('/audit-logs/security-alerts', methods=['GET'])
@jwt_required()
@validate_admin_action(['view_audit_logs', 'super_admin'])
def get_security_alerts():
    """
    Get recent security-related audit log entries

    Query Parameters:
        - hours: Number of hours to look back (default: 24)
        - severity: Minimum severity (low, medium, high, critical) - default: medium
    """
    try:
        hours = int(request.args.get('hours', 24))
        min_severity = request.args.get('severity', 'medium')

        # Calculate time range
        start_dt = datetime.now(UTC) - timedelta(hours=hours)

        # Security event types
        security_event_types = [
            AuditEventType.LOGIN_FAILURE,
            AuditEventType.PERMISSION_DENIED,
            AuditEventType.SUSPICIOUS_ACTIVITY,
            AuditEventType.EMERGENCY_OPERATION,
            AuditEventType.SENSITIVE_DATA_ACCESS
        ]

        # Severity filter
        severity_levels = {
            'low': [AuditSeverity.LOW, AuditSeverity.MEDIUM, AuditSeverity.HIGH, AuditSeverity.CRITICAL],
            'medium': [AuditSeverity.MEDIUM, AuditSeverity.HIGH, AuditSeverity.CRITICAL],
            'high': [AuditSeverity.HIGH, AuditSeverity.CRITICAL],
            'critical': [AuditSeverity.CRITICAL]
        }

        allowed_severities = severity_levels.get(min_severity, severity_levels['medium'])

        # Query security events
        alerts = AuditLog.query.filter(
            AuditLog.created_at >= start_dt,
            AuditLog.event_type.in_(security_event_types),
            AuditLog.severity.in_(allowed_severities)
        ).order_by(AuditLog.created_at.desc()).limit(100).all()

        # Also include all failed operations with high severity
        failed_critical = AuditLog.query.filter(
            AuditLog.created_at >= start_dt,
            AuditLog.success == False,
            AuditLog.severity.in_([AuditSeverity.HIGH, AuditSeverity.CRITICAL])
        ).order_by(AuditLog.created_at.desc()).limit(100).all()

        # Combine and deduplicate
        all_alerts = {alert.id: alert for alert in alerts + failed_critical}

        alerts_data = []
        for alert in sorted(all_alerts.values(), key=lambda x: x.created_at, reverse=True):
            alert_data = alert.to_dict()

            # Add user info
            if alert.user_id:
                user = User.query.get(alert.user_id)
                if user:
                    alert_data['user'] = {
                        'id': user.id,
                        'name': user.name,
                        'email': user.email
                    }

            alerts_data.append(alert_data)

        # Summary statistics
        summary = {
            'total_alerts': len(alerts_data),
            'critical_count': sum(1 for a in alerts_data if a['severity'] == 'critical'),
            'high_count': sum(1 for a in alerts_data if a['severity'] == 'high'),
            'time_range_hours': hours,
            'most_recent': alerts_data[0]['created_at'] if alerts_data else None
        }

        return success_response(data={
            'alerts': alerts_data[:50],  # Limit to 50 most recent
            'summary': summary
        })

    except Exception as e:
        current_app.logger.error(f"Get security alerts error: {e}")
        return internal_error_response('Failed to get security alerts')


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

        return success_response(
            data={'task_id': task_id},
            message='Announcement queued for sending'
        )

    except Exception as e:
        current_app.logger.error(f"Send announcement error: {e}")
        return internal_error_response('Failed to send announcement')


@admin_bp.route('/inventory/<int:product_id>/status', methods=['GET'])
@jwt_required()
@validate_admin_action(['manage_products', 'view_products'])
def get_inventory_status(product_id):
    """Get detailed inventory status for a product"""
    try:
        inventory_status = get_inventory_service().get_inventory_status(product_id)
        return success_response(data=inventory_status)

    except Exception as e:
        current_app.logger.error(f"Get inventory status error: {e}")
        return internal_error_response('Failed to get inventory status')


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
            return validation_error_response(
                f'Invalid operation type. Must be one of: {[op.value for op in InventoryOperationType]}'
            )

        # Validate quantity change
        if quantity_change == 0:
            return validation_error_response('Quantity change cannot be zero')

        if abs(quantity_change) > 10000:
            return validation_error_response('Quantity change too large (max 10000)')
        
        # Perform adjustment
        result = get_inventory_service().adjust_inventory(
            product_id=product_id,
            quantity_change=quantity_change,
            operation_type=operation_type,
            reason=reason,
            user_id=current_user_id
        )

        if result['success']:
            return success_response(
                data=result,
                message='Inventory adjusted successfully'
            )
        else:
            return error_response(
                message=result.get('reason', 'Adjustment failed'),
                status_code=400
            )

    except Exception as e:
        current_app.logger.error(f"Adjust inventory error: {e}")
        return internal_error_response('Failed to adjust inventory')


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
                return validation_error_response('Each item must have product_id and quantity')
        
        # Check availability
        availability_results = get_inventory_service().check_multiple_products_availability(items)
        
        # Format response
        language = get_current_language()
        results = []
        for result in availability_results:
            product = Product.query.get(result.product_id)
            product_name = product.get_translated('name', language) if product else 'Unknown'
            results.append({
                'product_id': result.product_id,
                'product_name': product_name,
                'requested_quantity': result.requested_quantity,
                'available_quantity': result.available_quantity,
                'reserved_quantity': result.reserved_quantity,
                'is_available': result.is_available,
                'reason': result.reason
            })

        return success_response(
            data={
                'results': results,
                'all_available': all(r.is_available for r in availability_results)
            }
        )

    except Exception as e:
        current_app.logger.error(f"Check inventory availability error: {e}")
        return internal_error_response('Failed to check inventory availability')


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
            return not_found_response(resource_type='Order')
        
        # Get inventory status for each item in the order
        language = get_current_language()
        reservations = []
        for item in order.items:
            inventory_status = get_inventory_service().get_inventory_status(item.product_id)
            product_name = item.product.get_translated('name', language) if item.product else 'Unknown'
            reservations.append({
                'product_id': item.product_id,
                'product_name': product_name,
                'quantity': item.quantity,
                'current_stock': inventory_status['current_stock'],
                'available_quantity': inventory_status['available_quantity'],
                'reserved_quantity': inventory_status['reserved_quantity']
            })

        return success_response(
            data={
                'order_id': order_id,
                'order_status': order.status.value,
                'reservations': reservations
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get order reservations error: {e}")
        return internal_error_response('Failed to get order reservations')


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
            return not_found_response(resource_type='Order')
        
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

            return success_response(
                data=result,
                message='Reservations released successfully'
            )
        else:
            return error_response(
                message=result.get('reason', 'Failed to release reservations'),
                status_code=400
            )

    except Exception as e:
        current_app.logger.error(f"Release order reservations error: {e}")
        return internal_error_response('Failed to release reservations')


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

        return success_response(
            data={'backup_id': backup_result['backup_id']},
            message='Backup creation started'
        )

    except Exception as e:
        current_app.logger.error(f"Create backup error: {e}")
        return internal_error_response('Failed to create backup')


# =============================================================================
# TRANSLATION MANAGEMENT ROUTES
# =============================================================================

def parse_entity_key(key):
    """Parse entity key format: EntityType.field.ID"""
    parts = key.split('.')
    if len(parts) == 3:
        try:
            entity_type, field_name, entity_id = parts
            return entity_type, field_name, int(entity_id)
        except ValueError:
            return None, None, None
    return None, None, None

def format_entity_translation(translation):
    """Convert Translation record to entity translation format for API compatibility"""
    entity_type, field_name, entity_id = parse_entity_key(translation.key)
    
    if entity_type and field_name and entity_id:
        return {
            'id': translation.id,
            'entity_type': entity_type,
            'entity_id': entity_id,
            'field_name': field_name,
            'language': translation.language,
            'content': translation.value,
            'is_active': translation.is_active,
            'version': 1,  # For compatibility
            'created_at': translation.created_at.isoformat() if translation.created_at else None,
            'updated_at': translation.updated_at.isoformat() if translation.updated_at else None
        }
    return None

@admin_bp.route('/translations', methods=['GET'])
@jwt_required()
@manager_or_higher_required
def get_translations():
    """Get all translations (both static and entity) with filtering and pagination"""
    try:
        # Get query parameters
        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 50, type=int), 100)
        entity_type = request.args.get('entity_type')
        entity_id = request.args.get('entity_id', type=int)
        field_name = request.args.get('field_name')
        language = request.args.get('language')
        search = request.args.get('search')
        translation_type = request.args.get('type')  # 'static' or 'entity'
        
        # Build base query
        query = Translation.query
        
        # Filter by translation type
        if translation_type == 'static':
            # Static translations have category NOT starting with 'entity_'
            query = query.filter(~Translation.category.like('entity_%'))
        elif translation_type == 'entity' or entity_type or entity_id or field_name:
            # Entity translations have category starting with 'entity_'
            query = query.filter(Translation.category.like('entity_%'))
        
        # Apply filters for entity translations
        if entity_type:
            query = query.filter(Translation.category == f'entity_{entity_type.lower()}')
        if entity_id:
            query = query.filter(Translation.key.like(f'%.%.{entity_id}'))
        if field_name:
            query = query.filter(Translation.key.like(f'%.{field_name}.%'))
        if language:
            query = query.filter(Translation.language == language)
        if search:
            query = query.filter(Translation.value.ilike(f'%{search}%'))
        
        # Order by key and language for consistency
        query = query.order_by(Translation.key, Translation.language)
        
        # Paginate
        pagination = query.paginate(
            page=page, per_page=per_page, error_out=False
        )
        
        # Format results based on category (already filtered by query)
        translations = []
        for item in pagination.items:
            # Check if it's an entity translation based on category
            if item.category and item.category.startswith('entity_'):
                # Entity translation - format with entity structure
                entity_trans = format_entity_translation(item)
                if entity_trans:
                    translations.append(entity_trans)
            else:
                # Static translation - format with standard structure
                translations.append({
                    'id': item.id,
                    'key': item.key,
                    'language': item.language,
                    'value': item.value,
                    'category': item.category,
                    'description': item.description,
                    'is_active': item.is_active,
                    'created_at': item.created_at.isoformat() if item.created_at else None,
                    'updated_at': item.updated_at.isoformat() if item.updated_at else None
                })
        
        # Get statistics - count individual translation records (each key-language pair is one record)
        # Entity translations have category starting with 'entity_'
        total_translation_records = Translation.query.count()
        entity_translation_records = Translation.query.filter(Translation.category.like('entity_%')).count()
        static_translation_records = total_translation_records - entity_translation_records

        # Count unique translatable items (not individual records)
        unique_entity_items = db.session.query(Translation.key).filter(
            Translation.category.like('entity_%')
        ).distinct().count()

        unique_static_keys = db.session.query(Translation.key).filter(
            ~Translation.category.like('entity_%')
        ).distinct().count()

        # Language breakdown
        language_stats = db.session.query(
            Translation.language,
            func.count(Translation.id).label('count')
        ).group_by(Translation.language).all()

        return success_response(
            data={
                'translations': translations,
                'statistics': {
                    'total_records': total_translation_records,
                    'entity_records': entity_translation_records,
                    'static_records': static_translation_records,
                    'unique_entity_items': unique_entity_items,
                    'unique_static_keys': unique_static_keys,
                    'total_unique_items': unique_entity_items + unique_static_keys,
                    'language_stats': [{'language': lang, 'count': count} for lang, count in language_stats],
                    'description': 'Records = individual key-language pairs, Items = unique translatable content'
                }
            },
            meta={
                'page': page,
                'pages': pagination.pages,
                'per_page': per_page,
                'total': pagination.total,
                'has_next': pagination.has_next,
                'has_prev': pagination.has_prev
            }
        )

    except Exception as e:
        current_app.logger.error(f"Error getting translations: {e}")
        return internal_error_response('Failed to get translations')


@admin_bp.route('/translations/<int:translation_id>', methods=['GET'])
@jwt_required()
@manager_or_higher_required
def get_translation_by_id(translation_id):
    """Get a specific translation by ID"""
    try:
        translation = Translation.query.get_or_404(translation_id)
        
        # Check if it's an entity translation
        if '.' in translation.key and len(translation.key.split('.')) == 3:
            result = format_entity_translation(translation)
            if result:
                return success_response(data={'translation': result})

        # Static translation
        return success_response(
            data={
                'translation': {
                    'id': translation.id,
                    'key': translation.key,
                    'language': translation.language,
                    'value': translation.value,
                    'category': translation.category,
                    'description': translation.description,
                    'is_active': translation.is_active,
                    'created_at': translation.created_at.isoformat() if translation.created_at else None,
                    'updated_at': translation.updated_at.isoformat() if translation.updated_at else None
                }
            }
        )

    except Exception as e:
        current_app.logger.error(f"Error getting translation: {e}")
        return not_found_response('Translation not found')


@admin_bp.route('/translations', methods=['POST'])
@jwt_required()
@manager_or_higher_required
@validate_json()
def create_translation():
    """Create a new translation (static or entity)"""
    try:
        data = request.get_json()
        
        # Determine if it's an entity translation or static translation
        if all(field in data for field in ['entity_type', 'entity_id', 'field_name']):
            # Entity translation
            entity_type = data['entity_type']
            entity_id = data['entity_id']
            field_name = data['field_name']
            language = data['language']
            content = data['content']
            
            # Use unified Translation model
            success = Translation.set_entity_translation(
                entity_type=entity_type,
                entity_id=entity_id,
                field_name=field_name,
                language=language,
                value=content,
                user_id=get_jwt_identity()
            )
            
            if success:
                db.session.commit()

                # Trigger bot translation reload if telegram category
                if data.get('category') == 'telegram':
                    trigger_translation_reload()

                return created_response(message='Entity translation created successfully')
            else:
                return internal_error_response('Failed to create entity translation')

        elif all(field in data for field in ['key', 'language', 'value']):
            # Static translation
            existing = Translation.query.filter_by(
                key=data['key'],
                language=data['language']
            ).first()

            if existing:
                return validation_error_response('Translation already exists')

            translation = Translation(
                key=data['key'],
                language=data['language'],
                value=data['value'],
                category=data.get('category', 'general'),
                description=data.get('description'),
                is_active=True,
                created_by=get_jwt_identity(),
                updated_by=get_jwt_identity()
            )

            db.session.add(translation)
        else:
            return validation_error_response('Invalid translation data format')

        db.session.commit()

        # Trigger bot translation reload if telegram category
        if data.get('category') == 'telegram':
            trigger_translation_reload()

        return created_response(message='Translation created successfully')

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create translation error: {e}")
        return internal_error_response('Failed to create translation')


@admin_bp.route('/translations/<int:translation_id>', methods=['PUT'])
@jwt_required()
@manager_or_higher_required
@validate_json()
def update_translation(translation_id):
    """Update an existing translation"""
    try:
        translation = Translation.query.get_or_404(translation_id)
        data = request.get_json()
        
        # Update fields
        if 'content' in data:
            translation.value = data['content']
        if 'value' in data:
            translation.value = data['value']
        if 'category' in data:
            translation.category = data['category']
        if 'description' in data:
            translation.description = data['description']
        if 'is_active' in data:
            translation.is_active = data['is_active']
        
        translation.updated_by = get_jwt_identity()
        translation.updated_at = datetime.now(UTC)

        db.session.commit()

        # Trigger bot translation reload if telegram category
        if translation.category == 'telegram':
            trigger_translation_reload()

        return success_response(message=get_translation('api.admin.success.translation_updated'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update translation error: {e}")
        return internal_error_response('Failed to update translation')


@admin_bp.route('/translations/<int:translation_id>', methods=['DELETE'])
@jwt_required()
@manager_or_higher_required
def delete_translation(translation_id):
    """Delete a translation"""
    try:
        translation = Translation.query.get_or_404(translation_id)

        # Check if it's a telegram translation before deletion
        is_telegram = translation.category == 'telegram'

        db.session.delete(translation)
        db.session.commit()

        # Trigger bot translation reload if telegram category
        if is_telegram:
            trigger_translation_reload()

        return success_response(message=get_translation('api.admin.success.translation_deleted'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete translation error: {e}")
        return internal_error_response('Failed to delete translation')


@admin_bp.route('/translations/entities', methods=['GET'])
@jwt_required()
@manager_or_higher_required
def get_translatable_entities():
    """Get all translatable entities with their available fields"""
    try:
        # Get distinct entity categories (like entity_product, entity_subscription)
        entity_categories = db.session.query(Translation.category).filter(
            Translation.category.like('entity_%')
        ).distinct().all()
        
        entities = []
        for (category,) in entity_categories:
            # Extract entity type from category (entity_product -> Product)
            entity_type = category.replace('entity_', '').title()
            
            # Get translations for this entity category to parse fields and count entities
            translations = db.session.query(Translation.key).filter_by(category=category).distinct().all()
            
            available_fields = set()
            entity_ids = set()
            
            for (key,) in translations:
                # Parse key format: EntityType.field.ID (e.g., Product.name.123)
                key_parts = key.split('.')
                if len(key_parts) == 3:
                    parsed_entity_type, field_name, entity_id = key_parts
                    available_fields.add(field_name)
                    try:
                        entity_ids.add(int(entity_id))
                    except ValueError:
                        continue
            
            entities.append({
                'entity_type': entity_type,
                'available_fields': list(available_fields),
                'entity_count': len(entity_ids)
            })

        return success_response(data={'entities': entities})

    except Exception as e:
        current_app.logger.error(f"Get translatable entities error: {e}")
        return internal_error_response('Failed to fetch translatable entities')


@admin_bp.route('/translations/sync/<entity_type>', methods=['POST'])
@jwt_required()
@manager_or_higher_required
@validate_json()
def sync_entity_translations(entity_type):
    """Sync translations for all entities of a specific type"""
    try:
        data = request.get_json()
        entity_ids = data.get('entity_ids', [])  # Empty list means sync all
        
        # Map entity types to model classes
        entity_models = {
            'Product': Product,
            'ProductCategory': ProductCategory,
            'SubscriptionPlan': None,  # Will need to import if needed
            'LoyaltyReward': LoyaltyReward,
            'NotificationTemplate': NotificationTemplate,
        }
        
        if entity_type not in entity_models:
            return validation_error_response(f'Unsupported entity type: {entity_type}')

        model_class = entity_models[entity_type]
        if not model_class:
            return validation_error_response(f'Model not available for entity type: {entity_type}')
        
        # Get entities to sync
        query = model_class.query
        if entity_ids:
            query = query.filter(model_class.id.in_(entity_ids))
        
        entities = query.all()
        synced_count = 0
        
        for entity in entities:
            # Get translatable fields for this entity
            translatable_fields = getattr(entity, '_translatable_fields', [])
            
            if translatable_fields:
                # Sync translations (create baseline if not exists)
                translations = {}
                for field in translatable_fields:
                    field_value = getattr(entity, field, None)
                    if field_value:
                        translations.setdefault(field, {})['uz'] = field_value
                
                if translations and hasattr(entity, 'set_translations'):
                    entity.set_translations(translations)
                    synced_count += 1

        db.session.commit()

        return success_response(
            message=f'Synced translations for {synced_count} {entity_type} entities'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Sync translations error: {e}")
        return internal_error_response('Failed to sync translations')


@admin_bp.route('/translations/export', methods=['GET'])
@jwt_required()
@manager_or_higher_required
def export_translations():
    """Export translations in various formats (CSV, JSON)"""
    try:
        format_type = request.args.get('format', 'json').lower()
        entity_type = request.args.get('entity_type')
        language = request.args.get('language')
        
        query = Translation.query.filter(Translation.key.like("%.%.%"))
        if entity_type:
            query = query.filter(Translation.category == entity_type)
        if language:
            query = query.filter(Translation.language == language)
        
        translations = query.all()
        
        if format_type == 'csv':
            import csv
            import io
            
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(['entity_type', 'entity_id', 'field_name', 'language', 'content', 'is_active'])
            
            for t in translations:
                writer.writerow([t.entity_type, t.entity_id, t.field_name, t.language, t.content, t.is_active])
            
            response = current_app.response_class(
                output.getvalue(),
                mimetype='text/csv',
                headers={'Content-Disposition': 'attachment;filename=translations.csv'}
            )
            return response
            
        else:  # JSON format
            data = []
            for t in translations:
                data.append({
                    'entity_type': t.entity_type,
                    'entity_id': t.entity_id,
                    'field_name': t.field_name,
                    'language': t.language,
                    'content': t.content,
                    'is_active': t.is_active,
                    'version': t.version
                })

            return success_response(
                data={
                    'translations': data,
                    'count': len(data)
                }
            )

    except Exception as e:
        current_app.logger.error(f"Export translations error: {e}")
        return internal_error_response('Failed to export translations')


@admin_bp.route('/translations/import', methods=['POST'])
@jwt_required()
@admin_required  # Require admin for imports
@validate_json()
def import_translations():
    """Import translations from uploaded data"""
    try:
        data = request.get_json()
        translations_data = data.get('translations', [])
        update_existing = data.get('update_existing', False)

        if not translations_data:
            return validation_error_response('No translations data provided')
        
        created_count = 0
        updated_count = 0
        skipped_count = 0
        errors = []
        
        for item in translations_data:
            try:
                # Validate required fields
                required = ['entity_type', 'entity_id', 'field_name', 'language', 'content']
                if not all(field in item for field in required):
                    errors.append(f"Missing required fields in item: {item}")
                    continue
                
                # Check if exists
                existing = Translation.query.filter(Translation.key.like("%.%.%")).filter_by(
                    entity_type=item['entity_type'],
                    entity_id=item['entity_id'],
                    field_name=item['field_name'],
                    language=item['language']
                ).first()
                
                if existing:
                    if update_existing:
                        existing.content = item['content']
                        existing.is_active = item.get('is_active', True)
                        existing.version += 1
                        existing.updated_at = datetime.now(UTC)
                        updated_count += 1
                    else:
                        skipped_count += 1
                else:
                    # Use unified Translation model with entity key format
                    success = Translation.set_entity_translation(
                        entity_type=item['entity_type'],
                        entity_id=item['entity_id'],
                        field_name=item['field_name'],
                        language=item['language'],
                        value=item['content'],
                        user_id=get_jwt_identity()
                    )
                    if success:
                        created_count += 1
                    
            except Exception as e:
                errors.append(f"Error processing item {item}: {e}")

        db.session.commit()

        return success_response(
            data={
                'results': {
                    'created': created_count,
                    'updated': updated_count,
                    'skipped': skipped_count,
                    'errors': len(errors)
                },
                'errors': errors[:10]  # Return first 10 errors
            },
            message='Import completed'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Import translations error: {e}")
        return internal_error_response('Failed to import translations')


@admin_bp.route('/translations/completion', methods=['GET'])
@jwt_required()
@manager_or_higher_required
def get_translation_completion():
    """Get translation completion statistics for both entity and static translations"""
    try:
        # Get all languages from config
        languages = ['en', 'uz', 'ru']  # From config
        entity_type_filter = request.args.get('entity_type')

        # ========== ENTITY TRANSLATIONS ==========
        # Get all entity translations (category starts with 'entity_')
        entity_translations_query = Translation.query.filter(Translation.category.like('entity_%'))

        if entity_type_filter:
            entity_translations_query = entity_translations_query.filter(
                Translation.category == f'entity_{entity_type_filter.lower()}'
            )

        all_entity_translations = entity_translations_query.all()

        # Parse unique entity/field combinations from keys (format: EntityType.field.ID)
        unique_entity_combinations = set()
        for trans in all_entity_translations:
            key_parts = trans.key.split('.')
            if len(key_parts) == 3:
                entity_type_name, field_name, entity_id = key_parts
                unique_entity_combinations.add((trans.category, entity_id, field_name))

        unique_entity_fields = list(unique_entity_combinations)

        # ========== STATIC TRANSLATIONS ==========
        # Get all static translations (category does NOT start with 'entity_')
        static_translations_query = Translation.query.filter(~Translation.category.like('entity_%'))
        all_static_translations = static_translations_query.all()

        # Parse unique static keys (each unique key should have translations for all languages)
        unique_static_keys = set()
        for trans in all_static_translations:
            unique_static_keys.add((trans.category, trans.key))

        unique_static_keys = list(unique_static_keys)

        # ========== OVERALL STATS INITIALIZATION ==========
        completion_stats = []

        # Separate entity and static totals for clarity
        total_entity_fields = len(unique_entity_fields)
        total_static_keys = len(unique_static_keys)
        total_translatable_items = total_entity_fields + total_static_keys

        overall_stats = {
            'total_translatable_items': total_translatable_items,
            'entity_translatable_fields': total_entity_fields,
            'static_translatable_keys': total_static_keys,
            'total_possible_translations': total_translatable_items * len(languages),
            'entity_possible_translations': total_entity_fields * len(languages),
            'static_possible_translations': total_static_keys * len(languages),
            'total_actual_translations': 0,
            'entity_actual_translations': 0,
            'static_actual_translations': 0,
            'overall_completion_percentage': 0.0,
            'language_breakdown': {}
        }

        for lang in languages:
            overall_stats['language_breakdown'][lang] = {
                'translated': 0,
                'entity_translated': 0,
                'static_translated': 0,
                'total': total_translatable_items,
                'percentage': 0.0
            }

        # ========== ENTITY TRANSLATION COMPLETION BY CATEGORY ==========
        entity_categories = db.session.query(Translation.category).filter(
            Translation.category.like('entity_%')
        ).distinct().all()

        for (category_name,) in entity_categories:
            if entity_type_filter and category_name != f'entity_{entity_type_filter.lower()}':
                continue

            # Get all fields for this entity category
            entity_fields = [uf for uf in unique_entity_fields if uf[0] == category_name]

            # Count translations per language
            lang_stats = {}
            for lang in languages:
                translated_count = Translation.query.filter(
                    Translation.category == category_name,
                    Translation.language == lang,
                    Translation.is_active == True
                ).count()

                lang_stats[lang] = {
                    'translated': translated_count,
                    'total': len(entity_fields),
                    'percentage': round((translated_count / len(entity_fields) * 100) if entity_fields else 0, 2)
                }

                # Add to overall stats
                overall_stats['language_breakdown'][lang]['translated'] += translated_count
                overall_stats['language_breakdown'][lang]['entity_translated'] += translated_count
                overall_stats['total_actual_translations'] += translated_count
                overall_stats['entity_actual_translations'] += translated_count

            # Calculate overall completion for this entity category
            total_possible = len(entity_fields) * len(languages)
            total_actual = sum(lang_stats[lang]['translated'] for lang in languages)
            completion_percentage = round((total_actual / total_possible * 100) if total_possible else 0, 2)

            completion_stats.append({
                'type': 'entity',
                'category': category_name,
                'display_name': category_name.replace('entity_', '').title(),
                'total_fields': len(entity_fields),
                'total_possible_translations': total_possible,
                'total_actual_translations': total_actual,
                'completion_percentage': completion_percentage,
                'language_breakdown': lang_stats,
                'missing_translations': total_possible - total_actual
            })

        # ========== STATIC TRANSLATION COMPLETION BY CATEGORY ==========
        static_categories = db.session.query(Translation.category).filter(
            ~Translation.category.like('entity_%')
        ).distinct().all()

        for (category_name,) in static_categories:
            # Get all keys for this static category
            static_keys = [sk for sk in unique_static_keys if sk[0] == category_name]

            # Count translations per language
            lang_stats = {}
            for lang in languages:
                translated_count = Translation.query.filter(
                    Translation.category == category_name,
                    Translation.language == lang,
                    Translation.is_active == True
                ).count()

                lang_stats[lang] = {
                    'translated': translated_count,
                    'total': len(static_keys),
                    'percentage': round((translated_count / len(static_keys) * 100) if static_keys else 0, 2)
                }

                # Add to overall stats
                overall_stats['language_breakdown'][lang]['translated'] += translated_count
                overall_stats['language_breakdown'][lang]['static_translated'] += translated_count
                overall_stats['total_actual_translations'] += translated_count
                overall_stats['static_actual_translations'] += translated_count

            # Calculate overall completion for this static category
            total_possible = len(static_keys) * len(languages)
            total_actual = sum(lang_stats[lang]['translated'] for lang in languages)
            completion_percentage = round((total_actual / total_possible * 100) if total_possible else 0, 2)

            completion_stats.append({
                'type': 'static',
                'category': category_name,
                'display_name': category_name.title(),
                'total_keys': len(static_keys),
                'total_possible_translations': total_possible,
                'total_actual_translations': total_actual,
                'completion_percentage': completion_percentage,
                'language_breakdown': lang_stats,
                'missing_translations': total_possible - total_actual
            })

        # ========== CALCULATE OVERALL PERCENTAGES ==========
        if overall_stats['total_possible_translations'] > 0:
            overall_stats['overall_completion_percentage'] = round(
                (overall_stats['total_actual_translations'] / overall_stats['total_possible_translations'] * 100), 2
            )

        for lang in languages:
            if overall_stats['language_breakdown'][lang]['total'] > 0:
                overall_stats['language_breakdown'][lang]['percentage'] = round(
                    (overall_stats['language_breakdown'][lang]['translated'] /
                     overall_stats['language_breakdown'][lang]['total'] * 100), 2
                )

        return success_response(
            data={
                'completion_stats': completion_stats,
                'overall_stats': overall_stats
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get translation completion error: {e}")
        return internal_error_response('Failed to get translation completion stats')


@admin_bp.route('/translations/missing', methods=['GET'])
@jwt_required()
@manager_or_higher_required
def get_missing_translations():
    """Get list of missing translations for both entity and static translations"""
    try:
        languages = ['en', 'uz', 'ru']
        entity_type = request.args.get('entity_type')
        language = request.args.get('language')
        translation_type = request.args.get('type')  # 'entity', 'static', or None for all
        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 50, type=int), 100)

        missing_translations = []

        # ========== CHECK MISSING ENTITY TRANSLATIONS ==========
        if not translation_type or translation_type == 'entity':
            # Get all entity translations (category starts with 'entity_')
            entity_translations_query = Translation.query.filter(Translation.category.like('entity_%'))

            if entity_type:
                entity_translations_query = entity_translations_query.filter(
                    Translation.category == f'entity_{entity_type.lower()}'
                )

            all_entity_translations = entity_translations_query.all()

            # Parse unique entity/field combinations from keys
            unique_entity_combinations = set()
            for trans in all_entity_translations:
                key_parts = trans.key.split('.')
                if len(key_parts) == 3:
                    entity_type_name, field_name, entity_id = key_parts
                    unique_entity_combinations.add((trans.category, entity_id, field_name))

            unique_entity_combinations = list(unique_entity_combinations)

            for entity_type_val, entity_id, field_name in unique_entity_combinations:
                check_languages = [language] if language else languages

                for lang in check_languages:
                    # Construct the expected key format: EntityType.field.ID
                    # Extract entity type name from category (entity_product -> Product)
                    category = entity_type_val
                    expected_key = None
                    existing = None

                    if category.startswith('entity_'):
                        entity_type_name = category.replace('entity_', '').title()
                        expected_key = f"{entity_type_name}.{field_name}.{entity_id}"

                        # Check if translation exists
                        existing = Translation.query.filter_by(
                            key=expected_key,
                            language=lang,
                            is_active=True
                        ).first()

                    if not existing and expected_key:
                        missing_translations.append({
                            'type': 'entity',
                            'category': entity_type_val,
                            'entity_id': entity_id,
                            'field_name': field_name,
                            'key': expected_key,
                            'language': lang,
                            'priority': 'high' if lang == 'uz' else 'medium'  # Uzbek (default) translations higher priority
                        })

        # ========== CHECK MISSING STATIC TRANSLATIONS ==========
        if not translation_type or translation_type == 'static':
            # Get all static translations (category does NOT start with 'entity_')
            static_translations_query = Translation.query.filter(~Translation.category.like('entity_%'))
            all_static_translations = static_translations_query.all()

            # Parse unique static keys (each unique key should have translations for all languages)
            unique_static_keys = {}  # key -> category mapping
            for trans in all_static_translations:
                if trans.key not in unique_static_keys:
                    unique_static_keys[trans.key] = trans.category

            # Check each unique static key for all languages
            for static_key, category in unique_static_keys.items():
                check_languages = [language] if language else languages

                for lang in check_languages:
                    # Check if translation exists for this language
                    existing = Translation.query.filter_by(
                        key=static_key,
                        language=lang,
                        is_active=True
                    ).first()

                    if not existing:
                        missing_translations.append({
                            'type': 'static',
                            'category': category,
                            'key': static_key,
                            'language': lang,
                            'priority': 'high' if lang == 'uz' else 'medium'  # Uzbek (default) translations higher priority
                        })

        # Sort by priority, type, and category
        missing_translations.sort(key=lambda x: (
            x['priority'] == 'medium',  # high priority first
            x['type'],  # entity before static
            x['category'],
            x.get('entity_id', ''),
            x['language']
        ))

        # Manual pagination
        start = (page - 1) * per_page
        end = start + per_page
        paginated_missing = missing_translations[start:end]

        total_pages = (len(missing_translations) + per_page - 1) // per_page if missing_translations else 1

        # Calculate summary statistics
        entity_missing = len([m for m in missing_translations if m['type'] == 'entity'])
        static_missing = len([m for m in missing_translations if m['type'] == 'static'])

        return success_response(
            data={
                'missing_translations': paginated_missing,
                'summary': {
                    'total_missing': len(missing_translations),
                    'entity_missing': entity_missing,
                    'static_missing': static_missing,
                    'high_priority': len([m for m in missing_translations if m['priority'] == 'high']),
                    'medium_priority': len([m for m in missing_translations if m['priority'] == 'medium']),
                    'by_language': {
                        lang: len([m for m in missing_translations if m['language'] == lang])
                        for lang in languages
                    }
                }
            },
            meta={
                'page': page,
                'pages': total_pages,
                'per_page': per_page,
                'total': len(missing_translations),
                'has_next': page < total_pages,
                'has_prev': page > 1
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get missing translations error: {e}")
        return internal_error_response('Failed to get missing translations')


@admin_bp.route('/translations/completeness', methods=['GET'])
@jwt_required()
@manager_or_higher_required
def get_translation_completeness():
    """
    Get comprehensive translation completeness statistics for both system and entity translations

    Query Parameters:
    - include_entities: Include entity translations (default: true)
    - include_system: Include system translations (default: true)
    """
    try:
        languages = ['uz', 'en', 'ru']  # Uzbek is default
        include_entities = request.args.get('include_entities', 'true').lower() == 'true'
        include_system = request.args.get('include_system', 'true').lower() == 'true'

        completeness_data = {
            'summary': {
                'total_unique_keys': 0,
                'total_possible_translations': 0,
                'total_actual_translations': 0,
                'overall_completion_percentage': 0.0
            },
            'by_language': {},
            'by_category': {},
            'system_translations': None,
            'entity_translations': None
        }

        # Initialize language stats
        for lang in languages:
            completeness_data['by_language'][lang] = {
                'total_keys': 0,
                'translated': 0,
                'missing': 0,
                'percentage': 0.0
            }

        # ===========================
        # SYSTEM TRANSLATIONS (api.*, error.*, ui.*, etc.)
        # ===========================
        if include_system:
            # Get all translations and filter in Python (regex in DB varies by engine)
            all_translations = Translation.query.filter(Translation.is_active == True).all()

            # Filter system translations (keys that don't match EntityType.field.ID format)
            import re
            entity_pattern = re.compile(r'^[A-Z][a-zA-Z]+\.[a-z_]+\.\d+$')
            system_translations = [
                trans for trans in all_translations
                if not entity_pattern.match(trans.key)
            ]

            # Group by key to find unique keys
            system_keys = {}
            for trans in system_translations:
                if trans.key not in system_keys:
                    system_keys[trans.key] = {
                        'key': trans.key,
                        'category': trans.category,
                        'languages': {}
                    }
                system_keys[trans.key]['languages'][trans.language] = {
                    'value': trans.value,
                    'translation_id': trans.id
                }

            # Calculate system translation completeness
            system_stats = {
                'total_unique_keys': len(system_keys),
                'total_possible': len(system_keys) * len(languages),
                'by_language': {},
                'by_category': {}
            }

            for lang in languages:
                translated_count = sum(1 for key_data in system_keys.values() if lang in key_data['languages'])
                missing_count = len(system_keys) - translated_count
                system_stats['by_language'][lang] = {
                    'translated': translated_count,
                    'missing': missing_count,
                    'total': len(system_keys),
                    'percentage': round((translated_count / len(system_keys) * 100) if system_keys else 0, 2)
                }

            # Group by category
            categories = {}
            for key, data in system_keys.items():
                category = data['category'] or 'uncategorized'
                if category not in categories:
                    categories[category] = {
                        'total_keys': 0,
                        'by_language': {lang: {'translated': 0, 'missing': 0} for lang in languages}
                    }
                categories[category]['total_keys'] += 1
                for lang in languages:
                    if lang in data['languages']:
                        categories[category]['by_language'][lang]['translated'] += 1
                    else:
                        categories[category]['by_language'][lang]['missing'] += 1

            # Add percentages to categories
            for category, stats in categories.items():
                for lang in languages:
                    total = stats['total_keys']
                    translated = stats['by_language'][lang]['translated']
                    stats['by_language'][lang]['percentage'] = round((translated / total * 100) if total > 0 else 0, 2)

            system_stats['by_category'] = categories
            system_stats['total_actual'] = sum(
                system_stats['by_language'][lang]['translated'] for lang in languages
            )
            system_stats['overall_percentage'] = round(
                (system_stats['total_actual'] / system_stats['total_possible'] * 100) if system_stats['total_possible'] > 0 else 0,
                2
            )

            completeness_data['system_translations'] = system_stats

            # Update summary
            completeness_data['summary']['total_unique_keys'] += system_stats['total_unique_keys']
            completeness_data['summary']['total_possible_translations'] += system_stats['total_possible']
            completeness_data['summary']['total_actual_translations'] += system_stats['total_actual']

            # Update language stats
            for lang in languages:
                completeness_data['by_language'][lang]['total_keys'] += system_stats['by_language'][lang]['total']
                completeness_data['by_language'][lang]['translated'] += system_stats['by_language'][lang]['translated']
                completeness_data['by_language'][lang]['missing'] += system_stats['by_language'][lang]['missing']

        # ===========================
        # ENTITY TRANSLATIONS (Product.name.123, etc.)
        # ===========================
        if include_entities:
            # Filter entity translations (keys matching EntityType.field.ID format)
            if not include_system:
                # Need to fetch all translations if we haven't already
                all_translations = Translation.query.filter(Translation.is_active == True).all()
                import re
                entity_pattern = re.compile(r'^[A-Z][a-zA-Z]+\.[a-z_]+\.\d+$')

            entity_translations = [
                trans for trans in all_translations
                if entity_pattern.match(trans.key)
            ]

            # Group by key to find unique keys
            entity_keys = {}
            for trans in entity_translations:
                if trans.key not in entity_keys:
                    # Parse entity type, field, and ID from key
                    parts = trans.key.split('.')
                    if len(parts) == 3:
                        entity_type, field, entity_id = parts
                        entity_keys[trans.key] = {
                            'key': trans.key,
                            'entity_type': entity_type,
                            'field': field,
                            'entity_id': entity_id,
                            'category': trans.category,
                            'languages': {}
                        }
                if trans.key in entity_keys:
                    entity_keys[trans.key]['languages'][trans.language] = {
                        'value': trans.value,
                        'translation_id': trans.id
                    }

            # Calculate entity translation completeness
            entity_stats = {
                'total_unique_keys': len(entity_keys),
                'total_possible': len(entity_keys) * len(languages),
                'by_language': {},
                'by_entity_type': {}
            }

            for lang in languages:
                translated_count = sum(1 for key_data in entity_keys.values() if lang in key_data['languages'])
                missing_count = len(entity_keys) - translated_count
                entity_stats['by_language'][lang] = {
                    'translated': translated_count,
                    'missing': missing_count,
                    'total': len(entity_keys),
                    'percentage': round((translated_count / len(entity_keys) * 100) if entity_keys else 0, 2)
                }

            # Group by entity type
            entity_types = {}
            for key, data in entity_keys.items():
                entity_type = data['entity_type']
                if entity_type not in entity_types:
                    entity_types[entity_type] = {
                        'total_keys': 0,
                        'by_language': {lang: {'translated': 0, 'missing': 0} for lang in languages}
                    }
                entity_types[entity_type]['total_keys'] += 1
                for lang in languages:
                    if lang in data['languages']:
                        entity_types[entity_type]['by_language'][lang]['translated'] += 1
                    else:
                        entity_types[entity_type]['by_language'][lang]['missing'] += 1

            # Add percentages to entity types
            for entity_type, stats in entity_types.items():
                for lang in languages:
                    total = stats['total_keys']
                    translated = stats['by_language'][lang]['translated']
                    stats['by_language'][lang]['percentage'] = round((translated / total * 100) if total > 0 else 0, 2)

            entity_stats['by_entity_type'] = entity_types
            entity_stats['total_actual'] = sum(
                entity_stats['by_language'][lang]['translated'] for lang in languages
            )
            entity_stats['overall_percentage'] = round(
                (entity_stats['total_actual'] / entity_stats['total_possible'] * 100) if entity_stats['total_possible'] > 0 else 0,
                2
            )

            completeness_data['entity_translations'] = entity_stats

            # Update summary
            completeness_data['summary']['total_unique_keys'] += entity_stats['total_unique_keys']
            completeness_data['summary']['total_possible_translations'] += entity_stats['total_possible']
            completeness_data['summary']['total_actual_translations'] += entity_stats['total_actual']

            # Update language stats
            for lang in languages:
                completeness_data['by_language'][lang]['total_keys'] += entity_stats['by_language'][lang]['total']
                completeness_data['by_language'][lang]['translated'] += entity_stats['by_language'][lang]['translated']
                completeness_data['by_language'][lang]['missing'] += entity_stats['by_language'][lang]['missing']

        # Calculate overall percentages
        if completeness_data['summary']['total_possible_translations'] > 0:
            completeness_data['summary']['overall_completion_percentage'] = round(
                (completeness_data['summary']['total_actual_translations'] /
                 completeness_data['summary']['total_possible_translations'] * 100),
                2
            )

        for lang in languages:
            total = completeness_data['by_language'][lang]['total_keys']
            translated = completeness_data['by_language'][lang]['translated']
            completeness_data['by_language'][lang]['percentage'] = round(
                (translated / total * 100) if total > 0 else 0,
                2
            )

        # Group completeness by category (for both system and entity)
        all_categories = {}

        # Add system categories
        if include_system and completeness_data['system_translations']:
            for category, stats in completeness_data['system_translations']['by_category'].items():
                all_categories[f"system:{category}"] = stats

        # Add entity categories
        if include_entities and completeness_data['entity_translations']:
            for entity_type, stats in completeness_data['entity_translations']['by_entity_type'].items():
                all_categories[f"entity:{entity_type}"] = stats

        completeness_data['by_category'] = all_categories

        return success_response(data=completeness_data)

    except Exception as e:
        current_app.logger.error(f"Get translation completeness error: {e}", exc_info=True)
        return internal_error_response('Failed to get translation completeness statistics')


# ============================================================================
# BLOG MANAGEMENT ENDPOINTS
# ============================================================================

@admin_bp.route('/blog/posts', methods=['GET'])
@jwt_required()
@admin_required
def get_all_blog_posts():
    """
    Admin: Get all blog posts (including drafts and archived)
    Query params:
    - page: Page number (default: 1)
    - per_page: Items per page (default: 20)
    - status: Filter by status (draft/published/archived)
    - category: Filter by category
    - search: Search in title
    - language: Language code
    """
    try:
        from business_app.models.blog import BlogPost, BlogStatus, BlogCategory

        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 20, type=int), 100)
        status = request.args.get('status', None)
        category = request.args.get('category', None)
        search = request.args.get('search', None)
        language = request.args.get('language', 'uz')

        # Base query
        query = BlogPost.query

        # Apply filters
        if status:
            try:
                status_enum = BlogStatus(status)
                query = query.filter(BlogPost.status == status_enum)
            except ValueError:
                return error_response('Invalid status', status_code=400)

        if category:
            try:
                category_enum = BlogCategory(category)
                query = query.filter(BlogPost.category == category_enum)
            except ValueError:
                return error_response('Invalid category', status_code=400)

        if search:
            query = query.filter(BlogPost.title.ilike(f'%{search}%'))

        # Order by updated date
        query = query.order_by(desc(BlogPost.updated_at))

        # Paginate
        pagination = query.paginate(
            page=page,
            per_page=per_page,
            error_out=False
        )

        # Serialize posts
        posts = [post.to_dict(language, include_all_translations=True) for post in pagination.items]

        return paginated_response(
            items=posts,
            page=pagination.page,
            per_page=pagination.per_page,
            total=pagination.total
        )

    except Exception as e:
        current_app.logger.error(f"Error in admin get posts: {str(e)}")
        return internal_error_response()


@admin_bp.route('/blog/posts/<int:post_id>', methods=['GET'])
@jwt_required()
@admin_required
def admin_get_blog_post(post_id):
    """Admin: Get a single blog post by ID with all translations"""
    try:
        from business_app.models.blog import BlogPost

        post = BlogPost.query.get(post_id)
        if not post:
            return not_found_response('Blog post not found')

        language = request.args.get('language', 'uz')
        return success_response(
            data=post.to_dict(language, include_all_translations=True),
            message='Blog post retrieved successfully'
        )

    except Exception as e:
        current_app.logger.error(f"Error in admin get post {post_id}: {str(e)}")
        return internal_error_response()


@admin_bp.route('/blog/posts', methods=['POST'])
@jwt_required()
@admin_required
@validate_json()
def admin_create_blog_post():
    """
    Admin: Create a new blog post
    Required fields:
    - title_uz, title_ru, title_en: Titles in all languages
    - excerpt_uz, excerpt_ru, excerpt_en: Excerpts in all languages
    - content_uz, content_ru, content_en: Full content in all languages
    - category: Blog category
    - slug: URL slug (unique)

    Optional fields:
    - author_name_uz, author_name_ru, author_name_en: Author names
    - featured_image: Image URL
    - image_alt_text: Alt text for image
    - tags: Comma-separated tags
    - is_featured: Boolean
    - status: draft/published (default: draft)
    """
    try:
        from business_app.models.blog import BlogPost, BlogStatus, BlogCategory

        data = request.get_json()
        current_user_id = get_jwt_identity()

        # Validate required fields
        required_fields = ['title_uz', 'title_ru', 'title_en', 'slug', 'category',
                          'excerpt_uz', 'excerpt_ru', 'excerpt_en',
                          'content_uz', 'content_ru', 'content_en']

        missing_fields = [field for field in required_fields if not data.get(field)]
        if missing_fields:
            return validation_error_response(f"Missing required fields: {', '.join(missing_fields)}")

        # Check if slug is unique
        if BlogPost.query.filter_by(slug=data['slug']).first():
            return error_response('Slug already exists', status_code=409)

        # Validate category
        try:
            category_enum = BlogCategory(data['category'])
        except ValueError:
            return error_response('Invalid category', status_code=400)

        # Create blog post (Uzbek as default)
        post = BlogPost(
            title=data['title_uz'],
            slug=data['slug'],
            excerpt=data['excerpt_uz'],
            content=data['content_uz'],
            author_name=data.get('author_name_uz', 'Admin'),
            author_id=current_user_id,
            category=category_enum,
            tags=data.get('tags'),
            featured_image=data.get('featured_image'),
            image_alt_text=data.get('image_alt_text'),
            is_featured=data.get('is_featured', False),
            sort_order=data.get('sort_order', 0),
            status=BlogStatus(data.get('status', 'draft'))
        )

        db.session.add(post)
        db.session.flush()  # Get the post ID

        # Set translations for all languages
        translations = {
            'title': {
                'uz': data['title_uz'],
                'ru': data['title_ru'],
                'en': data['title_en']
            },
            'excerpt': {
                'uz': data['excerpt_uz'],
                'ru': data['excerpt_ru'],
                'en': data['excerpt_en']
            },
            'content': {
                'uz': data['content_uz'],
                'ru': data['content_ru'],
                'en': data['content_en']
            }
        }

        # Add author name translations if provided
        if data.get('author_name_uz'):
            translations['author_name'] = {
                'uz': data.get('author_name_uz', 'Admin'),
                'ru': data.get('author_name_ru', data.get('author_name_uz', 'Admin')),
                'en': data.get('author_name_en', data.get('author_name_uz', 'Admin'))
            }

        # Add SEO translations if provided
        if data.get('meta_title_uz'):
            translations['meta_title'] = {
                'uz': data.get('meta_title_uz', ''),
                'ru': data.get('meta_title_ru', ''),
                'en': data.get('meta_title_en', '')
            }

        if data.get('meta_description_uz'):
            translations['meta_description'] = {
                'uz': data.get('meta_description_uz', ''),
                'ru': data.get('meta_description_ru', ''),
                'en': data.get('meta_description_en', '')
            }

        post.set_translations(translations)

        # If published, set published_at
        if post.status == BlogStatus.PUBLISHED and not post.published_at:
            post.published_at = datetime.now(UTC)

        db.session.commit()

        current_app.logger.info(f"Blog post created: {post.id} by user {current_user_id}")

        return created_response(
            data=post.to_dict('uz', include_all_translations=True),
            message='Blog post created successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error creating blog post: {str(e)}")
        return internal_error_response()


@admin_bp.route('/blog/posts/<int:post_id>', methods=['PUT'])
@jwt_required()
@admin_required
@validate_json()
def admin_update_blog_post(post_id):
    """Admin: Update a blog post"""
    try:
        from business_app.models.blog import BlogPost, BlogStatus, BlogCategory

        post = BlogPost.query.get(post_id)
        if not post:
            return not_found_response('Blog post not found')

        data = request.get_json()

        # Update basic fields
        if 'slug' in data and data['slug'] != post.slug:
            # Check if new slug is unique
            if BlogPost.query.filter(BlogPost.slug == data['slug'], BlogPost.id != post_id).first():
                return error_response('Slug already exists', status_code=409)
            post.slug = data['slug']

        if 'category' in data:
            try:
                post.category = BlogCategory(data['category'])
            except ValueError:
                return error_response('Invalid category', status_code=400)

        if 'tags' in data:
            post.tags = data['tags']
        if 'featured_image' in data:
            post.featured_image = data['featured_image']
        if 'image_alt_text' in data:
            post.image_alt_text = data['image_alt_text']
        if 'is_featured' in data:
            post.is_featured = data['is_featured']
        if 'sort_order' in data:
            post.sort_order = data['sort_order']
        if 'status' in data:
            old_status = post.status
            post.status = BlogStatus(data['status'])
            # Set published_at when publishing for the first time
            if post.status == BlogStatus.PUBLISHED and old_status != BlogStatus.PUBLISHED:
                if not post.published_at:
                    post.published_at = datetime.now(UTC)

        # Update Uzbek default values
        if 'title_uz' in data:
            post.title = data['title_uz']
        if 'excerpt_uz' in data:
            post.excerpt = data['excerpt_uz']
        if 'content_uz' in data:
            post.content = data['content_uz']
        if 'author_name_uz' in data:
            post.author_name = data['author_name_uz']

        # Update translations
        translations = {}

        # Title translations
        if any(key in data for key in ['title_uz', 'title_ru', 'title_en']):
            translations['title'] = {
                'uz': data.get('title_uz', post.title),
                'ru': data.get('title_ru', post.get_translated('title', 'ru')),
                'en': data.get('title_en', post.get_translated('title', 'en'))
            }

        # Excerpt translations
        if any(key in data for key in ['excerpt_uz', 'excerpt_ru', 'excerpt_en']):
            translations['excerpt'] = {
                'uz': data.get('excerpt_uz', post.excerpt),
                'ru': data.get('excerpt_ru', post.get_translated('excerpt', 'ru')),
                'en': data.get('excerpt_en', post.get_translated('excerpt', 'en'))
            }

        # Content translations
        if any(key in data for key in ['content_uz', 'content_ru', 'content_en']):
            translations['content'] = {
                'uz': data.get('content_uz', post.content),
                'ru': data.get('content_ru', post.get_translated('content', 'ru')),
                'en': data.get('content_en', post.get_translated('content', 'en'))
            }

        # Author name translations
        if any(key in data for key in ['author_name_uz', 'author_name_ru', 'author_name_en']):
            translations['author_name'] = {
                'uz': data.get('author_name_uz', post.author_name),
                'ru': data.get('author_name_ru', post.get_translated('author_name', 'ru')),
                'en': data.get('author_name_en', post.get_translated('author_name', 'en'))
            }

        # SEO translations
        if any(key in data for key in ['meta_title_uz', 'meta_title_ru', 'meta_title_en']):
            translations['meta_title'] = {
                'uz': data.get('meta_title_uz', ''),
                'ru': data.get('meta_title_ru', ''),
                'en': data.get('meta_title_en', '')
            }

        if any(key in data for key in ['meta_description_uz', 'meta_description_ru', 'meta_description_en']):
            translations['meta_description'] = {
                'uz': data.get('meta_description_uz', ''),
                'ru': data.get('meta_description_ru', ''),
                'en': data.get('meta_description_en', '')
            }

        if translations:
            post.set_translations(translations)

        post.updated_at = datetime.now(UTC)
        db.session.commit()

        current_app.logger.info(f"Blog post updated: {post.id}")

        return success_response(
            data=post.to_dict('uz', include_all_translations=True),
            message='Blog post updated successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error updating blog post {post_id}: {str(e)}")
        return internal_error_response()


@admin_bp.route('/blog/posts/<int:post_id>', methods=['DELETE'])
@jwt_required()
@admin_required
def admin_delete_blog_post(post_id):
    """Admin: Delete a blog post"""
    try:
        from business_app.models.blog import BlogPost

        post = BlogPost.query.get(post_id)
        if not post:
            return not_found_response('Blog post not found')

        db.session.delete(post)
        db.session.commit()

        current_app.logger.info(f"Blog post deleted: {post_id}")

        return success_response(message=get_translation('api.admin.success.blog_post_deleted'))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting blog post {post_id}: {str(e)}")
        return internal_error_response()


@admin_bp.route('/blog/posts/<int:post_id>/publish', methods=['POST'])
@jwt_required()
@admin_required
def admin_publish_blog_post(post_id):
    """Admin: Publish a blog post"""
    try:
        from business_app.models.blog import BlogPost

        post = BlogPost.query.get(post_id)
        if not post:
            return not_found_response('Blog post not found')

        post.publish()
        db.session.commit()

        current_app.logger.info(f"Blog post published: {post_id}")

        return success_response(
            data=post.to_dict('uz'),
            message='Blog post published successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error publishing blog post {post_id}: {str(e)}")
        return internal_error_response()


@admin_bp.route('/blog/posts/<int:post_id>/unpublish', methods=['POST'])
@jwt_required()
@admin_required
def admin_unpublish_blog_post(post_id):
    """Admin: Unpublish a blog post"""
    try:
        from business_app.models.blog import BlogPost

        post = BlogPost.query.get(post_id)
        if not post:
            return not_found_response('Blog post not found')

        post.unpublish()
        db.session.commit()

        current_app.logger.info(f"Blog post unpublished: {post_id}")

        return success_response(
            data=post.to_dict('uz'),
            message='Blog post unpublished successfully'
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error unpublishing blog post {post_id}: {str(e)}")
        return internal_error_response()


# ========================================================================
# FILE UPLOAD ENDPOINTS
# ========================================================================

@admin_bp.route('/upload/image', methods=['POST'])
@jwt_required()
@admin_required
def upload_image():
    """
    Upload image file for blog posts, products, etc.
    Returns the file URL that can be stored in the database

    Expected multipart/form-data with:
    - file: Image file
    - folder: (optional) Subfolder name (default: 'blog')
    - resize: (optional) Whether to resize (default: true)
    - max_width: (optional) Max width in pixels (default: 1920)
    - max_height: (optional) Max height in pixels (default: 1080)
    """
    try:
        current_user_id = get_jwt_identity()

        # Check if file is in request
        if 'file' not in request.files:
            return validation_error_response('No file provided')

        file = request.files['file']

        if file.filename == '':
            return validation_error_response('No file selected')

        # Get optional parameters
        folder = request.form.get('folder', 'blog')
        resize = request.form.get('resize', 'true').lower() == 'true'
        max_width = int(request.form.get('max_width', 1920))
        max_height = int(request.form.get('max_height', 1080))
        quality = int(request.form.get('quality', 85))

        # Initialize file storage service
        from business_app.services.file_storage_service import FileStorageService
        storage_service = FileStorageService()

        current_app.logger.info(f"filename: {file.filename}")

        # Upload image
        upload_result = storage_service.upload_image(
            file=file,
            filename=file.filename,
            folder=f'images/{folder}',
            user_id=current_user_id,
            resize=resize,
            max_width=max_width,
            max_height=max_height,
            quality=quality
        )

        current_app.logger.info(f"Image uploaded by admin {current_user_id}: {upload_result['filename']}")

        return created_response(
            data={
                'url': upload_result['url'],
                'file_path': upload_result['file_path'],
                'filename': upload_result['filename'],
                'size': upload_result['size'],
                'thumbnails': {
                    name: thumb['url']
                    for name, thumb in upload_result.get('thumbnails', {}).items()
                }
            },
            message='Image uploaded successfully'
        )

    except Exception as e:
        current_app.logger.error(f"Error uploading image: {str(e)}")
        return error_response(str(e), status_code=500)


@admin_bp.route('/upload/file', methods=['POST'])
@jwt_required()
@admin_required
def upload_file():
    """
    Upload generic file (documents, etc.)
    Returns the file URL

    Expected multipart/form-data with:
    - file: File
    - folder: (optional) Subfolder name (default: 'documents')
    """
    try:
        current_user_id = get_jwt_identity()

        # Check if file is in request
        if 'file' not in request.files:
            return validation_error_response('No file provided')

        file = request.files['file']

        if file.filename == '':
            return validation_error_response('No file selected')

        # Get optional parameters
        folder = request.form.get('folder', 'documents')

        # Initialize file storage service
        from business_app.services.file_storage_service import FileStorageService
        storage_service = FileStorageService()

        # Upload file
        upload_result = storage_service.upload_file(
            file=file,
            filename=file.filename,
            folder=folder,
            user_id=current_user_id
        )

        current_app.logger.info(f"File uploaded by admin {current_user_id}: {upload_result['filename']}")

        return created_response(
            data={
                'url': upload_result['url'],
                'file_path': upload_result['file_path'],
                'filename': upload_result['filename'],
                'size': upload_result['size'],
                'content_type': upload_result.get('content_type')
            },
            message='File uploaded successfully'
        )

    except Exception as e:
        current_app.logger.error(f"Error uploading file: {str(e)}")
        return error_response(str(e), status_code=500)