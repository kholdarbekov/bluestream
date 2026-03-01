"""
Analytics API endpoints for the Water Business Platform
This file should be placed in business_app/api/analytics.py
"""
from flask import Blueprint, request, jsonify, current_app, g
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import and_, or_, desc, func, text
from datetime import datetime, UTC, timedelta, date

from business_app.models.user import User
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product
from business_app.models.payment import Payment
from business_app.models.delivery import Delivery
from business_app.models.subscription import Subscription
from business_app.models.analytics import (
    UserEvent, 
    ProductView, 
    SearchQuery, 
    ConversionEvent,
    RevenueMetric,
    UserSegment
)
from business_app.utils.service_factory import get_analytics_service
from business_app.serializers.analytics_serializers import (
    serialize_dashboard_metrics, serialize_chart_data, generate_sales_analytics,
    generate_customer_analytics, serialize_user_segment, AnalyticsDashboardSchema, 
    CreateReportRequest, AnalyticsQueryRequest, AnalyticsResponseSchema
)
# from business_app.services.prediction_service import PredictionService
from business_app.utils.decorators import validate_json, cache_response, rate_limit
from business_app.utils.constants import OrderStatus, PaymentStatus, UserRole, UserStatus
from business_app import db

analytics_bp = Blueprint('analytics', __name__)


_TIMEFRAME_TO_PERIOD = {
    '7d': 'week',
    '30d': 'month',
    '90d': 'quarter',
    '1y': 'year',
}


def _parse_request_datetime(value: str, *, end_of_day: bool = False):
    if not value:
        return None

    try:
        if 'T' in value:
            parsed = datetime.fromisoformat(value)
        else:
            parsed_date = date.fromisoformat(value)
            parsed = datetime.combine(
                parsed_date,
                datetime.max.time() if end_of_day else datetime.min.time(),
            )
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _resolve_analytics_range(default_period: str = 'month'):
    timeframe = request.args.get('timeframe')
    period = request.args.get('period') or _TIMEFRAME_TO_PERIOD.get(timeframe, default_period)
    start_date = _parse_request_datetime(request.args.get('start_date'))
    end_date = _parse_request_datetime(request.args.get('end_date'), end_of_day=True)

    if start_date and end_date:
        return start_date, end_date, 'custom'

    now = datetime.now(UTC)
    if period == 'week':
        start_date = now - timedelta(weeks=1)
    elif period == 'quarter':
        start_date = now - timedelta(days=90)
    elif period == 'year':
        start_date = now - timedelta(days=365)
    else:
        period = 'month'
        start_date = now - timedelta(days=30)

    return start_date, now, period


def _get_analytics_user():
    current_user_id = get_jwt_identity()
    user = User.query.get(current_user_id)
    return current_user_id, user


def _user_lacks_analytics_access(user) -> bool:
    return not user or user.role not in [UserRole.ADMIN, UserRole.MANAGER]



@analytics_bp.route('/dashboard', methods=['GET'])
@jwt_required()
def get_analytics_dashboard():
    """Get analytics dashboard data"""
    try:
        _, user = _get_analytics_user()
        if not user:
            return jsonify({'error': 'User not found'}), 404

        if _user_lacks_analytics_access(user):
            return jsonify({'error': 'Analytics access required'}), 403

        start_date, end_date, period = _resolve_analytics_range()
        dashboard_data = get_analytics_service().get_dashboard_metrics(start_date, end_date)

        return jsonify({
            'period': period,
            'dashboard': dashboard_data
        })
        
    except Exception as e:
        current_app.logger.error(f"Get analytics dashboard error: {e}")
        return jsonify({'error': 'Failed to get analytics dashboard'}), 500


@analytics_bp.route('/revenue', methods=['GET'])
@jwt_required()
def get_revenue_analytics():
    """Get revenue analytics"""
    try:
        _, user = _get_analytics_user()
        granularity = request.args.get('granularity', 'day')  # hour, day, week, month

        if _user_lacks_analytics_access(user):
            return jsonify({'error': 'Analytics access required'}), 403

        start_date, end_date, period = _resolve_analytics_range()
        revenue_data = get_analytics_service().get_revenue_analytics(start_date, end_date, granularity)

        previous_period_start = start_date - (end_date - start_date)
        previous_revenue = get_analytics_service().get_total_revenue(previous_period_start, start_date)
        current_revenue = revenue_data.get('total_revenue', 0)

        revenue_growth = 0
        if previous_revenue > 0:
            revenue_growth = ((current_revenue - previous_revenue) / previous_revenue) * 100

        return jsonify({
            'period': period,
            'revenue_analytics': {
                **revenue_data,
                'revenue_growth_percentage': round(revenue_growth, 2),
                'previous_period_revenue': previous_revenue
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Get revenue analytics error: {e}")
        return jsonify({'error': 'Failed to get revenue analytics'}), 500


@analytics_bp.route('/customers', methods=['GET'])
@jwt_required()
def get_customer_analytics():
    """Get customer analytics"""
    try:
        _, user = _get_analytics_user()
        if _user_lacks_analytics_access(user):
            return jsonify({'error': 'Analytics access required'}), 403

        start_date, end_date, period = _resolve_analytics_range()
        customer_data = get_analytics_service().get_customer_analytics(start_date, end_date)

        return jsonify({
            'period': period,
            'customer_analytics': customer_data
        })
        
    except Exception as e:
        current_app.logger.error(f"Get customer analytics error: {e}")
        return jsonify({'error': 'Failed to get customer analytics'}), 500


@analytics_bp.route('/products', methods=['GET'])
@jwt_required()
def get_product_analytics():
    """Get product performance analytics"""
    try:
        _, user = _get_analytics_user()
        limit = min(int(request.args.get('limit', 20)), 100)

        if _user_lacks_analytics_access(user):
            return jsonify({'error': 'Analytics access required'}), 403

        start_date, end_date, period = _resolve_analytics_range()
        product_data = get_analytics_service().get_product_analytics(start_date, end_date, limit)

        return jsonify({
            'period': period,
            'product_analytics': product_data
        })
        
    except Exception as e:
        current_app.logger.error(f"Get product analytics error: {e}")
        return jsonify({'error': 'Failed to get product analytics'}), 500


@analytics_bp.route('/orders', methods=['GET'])
@jwt_required()
def get_order_analytics():
    """Get order analytics"""
    try:
        current_user_id = get_jwt_identity()
        period = request.args.get('period', 'month')
        
        user = User.query.get(current_user_id)
        if not user or user.role not in [UserRole.ADMIN, UserRole.MANAGER]:
            return jsonify({'error': 'Analytics access required'}), 403
        
        # Calculate date range
        now = datetime.now(UTC)
        if period == 'week':
            start_date = now - timedelta(weeks=1)
        elif period == 'month':
            start_date = now - timedelta(days=30)
        elif period == 'quarter':
            start_date = now - timedelta(days=90)
        elif period == 'year':
            start_date = now - timedelta(days=365)
        else:
            start_date = now - timedelta(days=30)
        
        order_data = get_analytics_service().get_order_analytics(start_date, now)
        
        return jsonify({
            'period': period,
            'order_analytics': order_data
        })
        
    except Exception as e:
        current_app.logger.error(f"Get order analytics error: {e}")
        return jsonify({'error': 'Failed to get order analytics'}), 500


@analytics_bp.route('/delivery', methods=['GET'])
@jwt_required()
def get_delivery_analytics():
    """Get delivery performance analytics"""
    try:
        _, user = _get_analytics_user()
        if _user_lacks_analytics_access(user):
            return jsonify({'error': 'Analytics access required'}), 403

        start_date, end_date, period = _resolve_analytics_range()
        delivery_data = get_analytics_service().get_delivery_analytics(start_date, end_date)

        return jsonify({
            'period': period,
            'delivery_analytics': delivery_data
        })
        
    except Exception as e:
        current_app.logger.error(f"Get delivery analytics error: {e}")
        return jsonify({'error': 'Failed to get delivery analytics'}), 500


@analytics_bp.route('/user-behavior', methods=['GET'])
@jwt_required()
def get_user_behavior_analytics():
    """Get user behavior analytics"""
    try:
        current_user_id = get_jwt_identity()
        period = request.args.get('period', 'month')
        
        user = User.query.get(current_user_id)
        if not user or user.role not in [UserRole.ADMIN, UserRole.MANAGER]:
            return jsonify({'error': 'Analytics access required'}), 403
        
        # Calculate date range
        now = datetime.now(UTC)
        if period == 'week':
            start_date = now - timedelta(weeks=1)
        elif period == 'month':
            start_date = now - timedelta(days=30)
        elif period == 'quarter':
            start_date = now - timedelta(days=90)
        elif period == 'year':
            start_date = now - timedelta(days=365)
        else:
            start_date = now - timedelta(days=30)
        
        behavior_data = get_analytics_service().get_user_behavior_analytics(start_date, now)
        
        return jsonify({
            'period': period,
            'user_behavior_analytics': behavior_data
        })
        
    except Exception as e:
        current_app.logger.error(f"Get user behavior analytics error: {e}")
        return jsonify({'error': 'Failed to get user behavior analytics'}), 500


@analytics_bp.route('/conversion-funnel', methods=['GET'])
@jwt_required()
def get_conversion_funnel():
    """Get conversion funnel analytics"""
    try:
        _, user = _get_analytics_user()
        if _user_lacks_analytics_access(user):
            return jsonify({'error': 'Analytics access required'}), 403

        start_date, end_date, period = _resolve_analytics_range()
        funnel_data = get_analytics_service().get_conversion_funnel(start_date, end_date)

        return jsonify({
            'period': period,
            'conversion_funnel': funnel_data
        })
        
    except Exception as e:
        current_app.logger.error(f"Get conversion funnel error: {e}")
        return jsonify({'error': 'Failed to get conversion funnel'}), 500


@analytics_bp.route('/cohort', methods=['GET'])
@jwt_required()
def get_cohort_analysis():
    """Get cohort analysis"""
    try:
        current_user_id = get_jwt_identity()
        cohort_type = request.args.get('type', 'monthly')  # weekly, monthly
        periods = int(request.args.get('periods', 12))  # Number of periods to analyze
        
        user = User.query.get(current_user_id)
        if not user or user.role not in [UserRole.ADMIN, UserRole.MANAGER]:
            return jsonify({'error': 'Analytics access required'}), 403
        
        cohort_data = get_analytics_service().get_cohort_analysis(cohort_type, periods)
        
        return jsonify({
            'cohort_type': cohort_type,
            'periods': periods,
            'cohort_analysis': cohort_data
        })
        
    except Exception as e:
        current_app.logger.error(f"Get cohort analysis error: {e}")
        return jsonify({'error': 'Failed to get cohort analysis'}), 500


@analytics_bp.route('/segments', methods=['GET'])
@jwt_required()
def get_user_segments():
    """Get user segments"""
    try:
        current_user_id = get_jwt_identity()
        
        user = User.query.get(current_user_id)
        if not user or user.role not in [UserRole.ADMIN, UserRole.MANAGER]:
            return jsonify({'error': 'Analytics access required'}), 403
        
        segments = UserSegment.query.filter_by(is_active=True).order_by(
            UserSegment.name
        ).all()
        
        segment_data = []
        for segment in segments:
            segment_metrics = get_analytics_service().get_segment_metrics(segment.id)
            segment_data.append({
                **serialize_user_segment(segment),
                'metrics': segment_metrics
            })
        
        return jsonify({
            'segments': segment_data
        })
        
    except Exception as e:
        current_app.logger.error(f"Get user segments error: {e}")
        return jsonify({'error': 'Failed to get user segments'}), 500


@analytics_bp.route('/predictions', methods=['GET'])
@jwt_required()
def get_predictions():
    """Get AI-powered predictions"""
    try:
        _, user = _get_analytics_user()
        prediction_type = request.args.get('type', 'revenue')  # revenue, demand, churn
        horizon = int(request.args.get('horizon', 30))  # Days to predict

        if _user_lacks_analytics_access(user):
            return jsonify({'error': 'Analytics access required'}), 403

        if prediction_type == 'revenue':
            predictions = get_analytics_service().predict_revenue(max(horizon, 90))
        elif prediction_type == 'demand':
            predictions = get_analytics_service().predict_demand(horizon)
        elif prediction_type == 'churn':
            churn_predictions = get_analytics_service().predict_customer_churn()
            at_risk_count = (
                churn_predictions.get('high_risk_customers', 0)
                + churn_predictions.get('medium_risk_customers', 0)
            )
            total_active_customers = User.query.filter(
                User.status == UserStatus.ACTIVE
            ).count()
            predictions = {
                'churn_rate': round((at_risk_count / max(1, total_active_customers)) * 100, 2),
                'at_risk_count': at_risk_count,
                'high_risk_count': churn_predictions.get('high_risk_customers', 0),
                'customers': [
                    {
                        'id': customer['user_id'],
                        'customer_name': customer.get('user_name'),
                        'customer_email': customer.get('email'),
                        'risk_score': round(float(customer.get('churn_probability', 0)) * 100, 1),
                        'risk_level': customer.get('risk_level'),
                        'last_order_date': None,
                        'total_spent': 0,
                    }
                    for customer in churn_predictions.get('predictions', [])
                ],
            }
        else:
            return jsonify({'error': 'Invalid prediction type'}), 400

        return jsonify({
            'prediction_type': prediction_type,
            'horizon_days': horizon,
            'predictions': predictions
        })
        
    except Exception as e:
        current_app.logger.error(f"Get predictions error: {e}")
        return jsonify({'error': 'Failed to get predictions'}), 500


@analytics_bp.route('/track-event', methods=['POST'])
@jwt_required()
@validate_json(['event_type'])
def track_event():
    """Track user event for analytics"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        
        event_type = data.get('event_type')
        event_data = data.get('event_data', {})
        
        # Track the event
        event = get_analytics_service().track_user_event(
            user_id=current_user_id,
            event_type=event_type,
            event_data=event_data,
            request_info={
                'ip_address': request.remote_addr,
                'user_agent': request.headers.get('User-Agent'),
                'referrer': request.headers.get('Referer')
            }
        )
        
        return jsonify({
            'message': 'Event tracked successfully',
            'event_id': event.id
        })
        
    except Exception as e:
        current_app.logger.error(f"Track event error: {e}")
        return jsonify({'error': 'Failed to track event'}), 500


@analytics_bp.route('/search-analytics', methods=['GET'])
@jwt_required()
def get_search_analytics():
    """Get search analytics"""
    try:
        current_user_id = get_jwt_identity()
        period = request.args.get('period', 'month')
        limit = min(int(request.args.get('limit', 20)), 100)
        
        user = User.query.get(current_user_id)
        if not user or user.role not in [UserRole.ADMIN, UserRole.MANAGER]:
            return jsonify({'error': 'Analytics access required'}), 403
        
        # Calculate date range
        now = datetime.now(UTC)
        if period == 'week':
            start_date = now - timedelta(weeks=1)
        elif period == 'month':
            start_date = now - timedelta(days=30)
        elif period == 'quarter':
            start_date = now - timedelta(days=90)
        elif period == 'year':
            start_date = now - timedelta(days=365)
        else:
            start_date = now - timedelta(days=30)
        
        search_data = get_analytics_service().get_search_analytics(start_date, now, limit)
        
        return jsonify({
            'period': period,
            'search_analytics': search_data
        })
        
    except Exception as e:
        current_app.logger.error(f"Get search analytics error: {e}")
        return jsonify({'error': 'Failed to get search analytics'}), 500


@analytics_bp.route('/geographic', methods=['GET'])
@jwt_required()
def get_geographic_analytics():
    """Get geographic analytics"""
    try:
        current_user_id = get_jwt_identity()
        period = request.args.get('period', 'month')
        
        user = User.query.get(current_user_id)
        if not user or user.role not in [UserRole.ADMIN, UserRole.MANAGER]:
            return jsonify({'error': 'Analytics access required'}), 403
        
        # Calculate date range
        now = datetime.now(UTC)
        if period == 'week':
            start_date = now - timedelta(weeks=1)
        elif period == 'month':
            start_date = now - timedelta(days=30)
        elif period == 'quarter':
            start_date = now - timedelta(days=90)
        elif period == 'year':
            start_date = now - timedelta(days=365)
        else:
            start_date = now - timedelta(days=30)
        
        geographic_data = get_analytics_service().get_geographic_analytics(start_date, now)
        
        return jsonify({
            'period': period,
            'geographic_analytics': geographic_data
        })
        
    except Exception as e:
        current_app.logger.error(f"Get geographic analytics error: {e}")
        return jsonify({'error': 'Failed to get geographic analytics'}), 500


@analytics_bp.route('/real-time', methods=['GET'])
@jwt_required()
def get_real_time_analytics():
    """Get real-time analytics"""
    try:
        current_user_id = get_jwt_identity()
        
        user = User.query.get(current_user_id)
        if not user or user.role not in [UserRole.ADMIN, UserRole.MANAGER]:
            return jsonify({'error': 'Analytics access required'}), 403
        
        # Get real-time metrics (last hour)
        now = datetime.now(UTC)
        one_hour_ago = now - timedelta(hours=1)
        
        real_time_data = {
            'active_users': get_analytics_service().get_active_users_count(one_hour_ago, now),
            'current_orders': get_analytics_service().get_current_orders_count(),
            'revenue_today': get_analytics_service().get_revenue_today(),
            'conversion_rate': get_analytics_service().get_current_conversion_rate(),
            'top_products_today': get_analytics_service().get_top_products_today(limit=5),
            'recent_events': get_analytics_service().get_recent_events(limit=10),
            'system_health': get_analytics_service().get_system_health_metrics()
        }
        
        return jsonify({
            'timestamp': now.isoformat(),
            'real_time_analytics': real_time_data
        })
        
    except Exception as e:
        current_app.logger.error(f"Get real-time analytics error: {e}")
        return jsonify({'error': 'Failed to get real-time analytics'}), 500


@analytics_bp.route('/export', methods=['POST'])
@jwt_required()
@rate_limit(max_requests=5, window_seconds=1800, per='user')  # 5 exports per 30 minutes per user
@validate_json(['report_type', 'date_range'])
def export_analytics():
    """Export analytics data"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        
        user = User.query.get(current_user_id)
        if not user or user.role not in [UserRole.ADMIN, UserRole.MANAGER]:
            return jsonify({'error': 'Analytics access required'}), 403
        
        report_type = data.get('report_type')
        date_range = data.get('date_range')
        format_type = data.get('format', 'csv')  # csv, excel, pdf
        
        # Validate report type
        valid_reports = [
            'revenue', 'customers', 'products', 'orders', 
            'delivery', 'user_behavior', 'search'
        ]
        
        if report_type not in valid_reports:
            return jsonify({'error': 'Invalid report type'}), 400
        
        # Parse date range
        try:
            start_date = datetime.fromisoformat(date_range['start'])
            end_date = datetime.fromisoformat(date_range['end'])
        except (KeyError, ValueError):
            return jsonify({'error': 'Invalid date range format'}), 400
        
        # Generate export
        export_result = get_analytics_service().export_analytics_report(
            report_type=report_type,
            start_date=start_date,
            end_date=end_date,
            format_type=format_type,
            user_id=current_user_id
        )
        
        return jsonify({
            'message': 'Export generated successfully',
            'download_url': export_result['download_url'],
            'file_size': export_result['file_size'],
            'expires_at': export_result['expires_at'].isoformat()
        })
        
    except Exception as e:
        current_app.logger.error(f"Export analytics error: {e}")
        return jsonify({'error': 'Failed to export analytics'}), 500


@analytics_bp.route('/alerts', methods=['GET'])
@jwt_required()
def get_analytics_alerts():
    """Get analytics alerts and anomalies"""
    try:
        current_user_id = get_jwt_identity()
        
        user = User.query.get(current_user_id)
        if not user or user.role not in [UserRole.ADMIN, UserRole.MANAGER]:
            return jsonify({'error': 'Analytics access required'}), 403
        
        alerts = get_analytics_service().get_active_alerts()
        
        return jsonify({
            'alerts': alerts
        })
        
    except Exception as e:
        current_app.logger.error(f"Get analytics alerts error: {e}")
        return jsonify({'error': 'Failed to get analytics alerts'}), 500


@analytics_bp.route('/custom-report', methods=['POST'])
@jwt_required()
@rate_limit(max_requests=10, window_seconds=3600, per='user')  # 10 custom reports per hour per user
@validate_json(['metrics', 'dimensions', 'date_range'])
def generate_custom_report():
    """Generate custom analytics report"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        
        user = User.query.get(current_user_id)
        if not user or user.role not in [UserRole.ADMIN, UserRole.MANAGER]:
            return jsonify({'error': 'Analytics access required'}), 403
        
        metrics = data.get('metrics')
        dimensions = data.get('dimensions')
        date_range = data.get('date_range')
        filters = data.get('filters', {})
        
        # Parse date range
        try:
            start_date = datetime.fromisoformat(date_range['start'])
            end_date = datetime.fromisoformat(date_range['end'])
        except (KeyError, ValueError):
            return jsonify({'error': 'Invalid date range format'}), 400
        
        # Generate custom report
        report_data = get_analytics_service().generate_custom_report(
            metrics=metrics,
            dimensions=dimensions,
            start_date=start_date,
            end_date=end_date,
            filters=filters
        )
        
        return jsonify({
            'custom_report': report_data
        })
        
    except Exception as e:
        current_app.logger.error(f"Generate custom report error: {e}")
        return jsonify({'error': 'Failed to generate custom report'}), 500
