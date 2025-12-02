"""
Unit tests for AnalyticsService
Tests analytics, reporting, and business intelligence functionality
"""
import pytest
from decimal import Decimal
from datetime import datetime, timedelta, UTC
from unittest.mock import patch, MagicMock

from business_app.services.analytics_service import AnalyticsService
from business_app.models.user import User, UserRole
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product
from business_app.models.payment import Payment
from business_app.models.delivery import Delivery
from business_app.models.analytics import UserBehavior
from business_app.utils.constants import OrderStatus, PaymentStatus, DeliveryStatus


@pytest.fixture
def analytics_service():
    """Create AnalyticsService instance"""
    return AnalyticsService()


@pytest.fixture
def sample_orders_with_items(db, sample_user, sample_product):
    """Create sample orders with items for analytics testing"""
    orders = []

    # Create orders over the past 30 days
    for i in range(10):
        order = Order(
            user_id=sample_user.id,
            status=OrderStatus.DELIVERED,
            total_amount=Decimal(f'{15000 + i * 1000}.00'),
            delivery_address='Test Address',
            created_at=datetime.now(UTC) - timedelta(days=i * 3)
        )
        db.session.add(order)
        db.session.flush()

        # Add order item
        item = OrderItem(
            order_id=order.id,
            product_id=sample_product.id,
            quantity=i + 1,
            unit_price=Decimal('15000.00'),
            total_price=Decimal(f'{15000 * (i + 1)}.00')
        )
        db.session.add(item)

        # Add payment
        payment = Payment(
            order_id=order.id,
            user_id=sample_user.id,
            amount=order.total_amount,
            status=PaymentStatus.COMPLETED,
            payment_method='card'
        )
        db.session.add(payment)

        orders.append(order)

    db.session.commit()
    return orders


@pytest.mark.analytics
class TestDashboardOverview:
    """Test dashboard overview analytics"""

    def test_get_dashboard_overview_basic(self, analytics_service, sample_orders_with_items, db):
        """Test getting basic dashboard overview"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        overview = analytics_service.get_dashboard_overview(start_date, end_date)

        assert 'period' in overview
        assert 'revenue' in overview
        assert 'orders' in overview
        assert 'customers' in overview
        assert 'delivery' in overview
        assert 'growth' in overview
        assert 'generated_at' in overview

    def test_get_dashboard_overview_default_period(self, analytics_service, sample_orders_with_items, db):
        """Test dashboard overview with default period"""
        overview = analytics_service.get_dashboard_overview()

        assert overview is not None
        assert 'period' in overview

    def test_get_dashboard_overview_revenue_metrics(self, analytics_service, sample_orders_with_items, db):
        """Test revenue metrics in dashboard"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        overview = analytics_service.get_dashboard_overview(start_date, end_date)

        assert 'revenue' in overview
        revenue = overview['revenue']
        assert 'total_revenue' in revenue
        assert revenue['total_revenue'] > 0

    def test_get_dashboard_overview_order_metrics(self, analytics_service, sample_orders_with_items, db):
        """Test order metrics in dashboard"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        overview = analytics_service.get_dashboard_overview(start_date, end_date)

        assert 'orders' in overview
        orders = overview['orders']
        assert 'total_orders' in orders
        assert orders['total_orders'] > 0


@pytest.mark.analytics
class TestSalesAnalytics:
    """Test sales analytics functionality"""

    def test_get_sales_analytics(self, analytics_service, sample_orders_with_items, db):
        """Test getting sales analytics"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        sales = analytics_service.get_sales_analytics(start_date, end_date)

        assert 'daily_trends' in sales
        assert 'product_performance' in sales
        assert 'hourly_distribution' in sales
        assert 'weekly_distribution' in sales

    def test_get_sales_analytics_daily_trends(self, analytics_service, sample_orders_with_items, db):
        """Test daily sales trends"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        sales = analytics_service.get_sales_analytics(start_date, end_date)

        assert 'daily_trends' in sales
        daily_trends = sales['daily_trends']
        assert isinstance(daily_trends, list)

    def test_get_sales_analytics_product_performance(self, analytics_service, sample_orders_with_items, db):
        """Test product performance analytics"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        sales = analytics_service.get_sales_analytics(start_date, end_date)

        assert 'product_performance' in sales
        products = sales['product_performance']
        assert isinstance(products, list)


@pytest.mark.analytics
class TestCustomerAnalytics:
    """Test customer analytics functionality"""

    def test_get_customer_analytics(self, analytics_service, sample_orders_with_items, db):
        """Test getting customer analytics"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        customer_analytics = analytics_service.get_customer_analytics(start_date, end_date)

        assert 'acquisition' in customer_analytics
        assert 'retention' in customer_analytics
        assert 'lifetime_value' in customer_analytics

    def test_get_customer_analytics_acquisition(self, analytics_service, sample_user, db):
        """Test customer acquisition metrics"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        customer_analytics = analytics_service.get_customer_analytics(start_date, end_date)

        assert 'acquisition' in customer_analytics
        acquisition = customer_analytics['acquisition']
        assert 'new_customers' in acquisition


@pytest.mark.analytics
class TestDeliveryAnalytics:
    """Test delivery analytics functionality"""

    def test_get_delivery_analytics(self, analytics_service, sample_orders_with_items, db):
        """Test getting delivery analytics"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        delivery_analytics = analytics_service.get_delivery_analytics(start_date, end_date)

        assert 'on_time_rate' in delivery_analytics
        assert 'average_delivery_time' in delivery_analytics

    def test_get_delivery_analytics_with_deliveries(self, analytics_service, sample_orders_with_items, db):
        """Test delivery analytics with actual deliveries"""
        # Create deliveries for orders
        for order in sample_orders_with_items:
            delivery = Delivery(
                order_id=order.id,
                status=DeliveryStatus.DELIVERED,
                scheduled_date=order.created_at.date(),
                delivered_at=order.created_at + timedelta(hours=2)
            )
            db.session.add(delivery)
        db.session.commit()

        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        delivery_analytics = analytics_service.get_delivery_analytics(start_date, end_date)

        assert 'on_time_rate' in delivery_analytics


@pytest.mark.analytics
class TestPredictiveAnalytics:
    """Test predictive analytics functionality"""

    def test_predict_demand(self, analytics_service, sample_orders_with_items, db):
        """Test demand prediction"""
        with patch.object(analytics_service, '_get_historical_demand_data') as mock_data:
            # Mock historical data to avoid ML model complexity in tests
            mock_data.return_value = [
                {'date': (datetime.now(UTC) - timedelta(days=i)).date(), 'demand': 100 + i}
                for i in range(30)
            ]

            forecast = analytics_service.predict_demand(forecast_days=7)

            assert 'forecast' in forecast
            assert 'confidence' in forecast

    def test_predict_demand_default_period(self, analytics_service, db):
        """Test demand prediction with default period"""
        with patch.object(analytics_service, '_get_historical_demand_data') as mock_data:
            mock_data.return_value = [
                {'date': (datetime.now(UTC) - timedelta(days=i)).date(), 'demand': 100}
                for i in range(30)
            ]

            forecast = analytics_service.predict_demand()

            assert forecast is not None
            assert 'forecast' in forecast

    def test_predict_customer_churn(self, analytics_service, sample_user, sample_orders_with_items, db):
        """Test customer churn prediction"""
        churn = analytics_service.predict_customer_churn(user_id=sample_user.id)

        assert 'user_id' in churn or 'churn_probability' in churn or 'batch_predictions' in churn

    def test_predict_customer_churn_batch(self, analytics_service, db):
        """Test batch customer churn prediction"""
        # Create multiple users
        users = []
        for i in range(5):
            user = User(
                email=f'user{i}@example.com',
                phone=f'+99890123456{i}',
                role=UserRole.CUSTOMER,
                created_at=datetime.now(UTC) - timedelta(days=30)
            )
            db.session.add(user)
            users.append(user)
        db.session.commit()

        churn = analytics_service.predict_customer_churn()

        assert 'batch_predictions' in churn or 'churn_probability' in churn


@pytest.mark.analytics
class TestBusinessReporting:
    """Test business report generation"""

    def test_generate_business_report_sales(self, analytics_service, sample_orders_with_items, db):
        """Test generating sales report"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        report = analytics_service.generate_business_report(
            report_type='sales',
            start_date=start_date,
            end_date=end_date
        )

        assert report is not None
        assert 'report_type' in report
        assert report['report_type'] == 'sales'

    def test_generate_business_report_customer(self, analytics_service, sample_orders_with_items, db):
        """Test generating customer report"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        report = analytics_service.generate_business_report(
            report_type='customer',
            start_date=start_date,
            end_date=end_date
        )

        assert report is not None
        assert 'report_type' in report
        assert report['report_type'] == 'customer'

    def test_generate_business_report_delivery(self, analytics_service, sample_orders_with_items, db):
        """Test generating delivery report"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        report = analytics_service.generate_business_report(
            report_type='delivery',
            start_date=start_date,
            end_date=end_date
        )

        assert report is not None
        assert 'report_type' in report
        assert report['report_type'] == 'delivery'


@pytest.mark.analytics
class TestUserBehaviorTracking:
    """Test user behavior tracking"""

    def test_track_user_behavior(self, analytics_service, sample_user, db):
        """Test tracking user behavior"""
        analytics_service.track_user_behavior(
            user_id=sample_user.id,
            action='product_view',
            metadata={'product_id': 1}
        )

        # Verify behavior was tracked
        behavior = UserBehavior.query.filter_by(
            user_id=sample_user.id,
            action='product_view'
        ).first()

        assert behavior is not None

    def test_track_user_behavior_with_metadata(self, analytics_service, sample_user, db):
        """Test tracking user behavior with metadata"""
        metadata = {
            'product_id': 1,
            'category': 'water',
            'source': 'search'
        }

        analytics_service.track_user_behavior(
            user_id=sample_user.id,
            action='product_view',
            metadata=metadata
        )

        behavior = UserBehavior.query.filter_by(
            user_id=sample_user.id
        ).first()

        assert behavior is not None
        assert behavior.metadata is not None

    def test_track_user_behavior_different_actions(self, analytics_service, sample_user, db):
        """Test tracking different user actions"""
        actions = ['product_view', 'add_to_cart', 'checkout', 'purchase']

        for action in actions:
            analytics_service.track_user_behavior(
                user_id=sample_user.id,
                action=action
            )

        # Verify all actions were tracked
        behaviors = UserBehavior.query.filter_by(user_id=sample_user.id).all()
        tracked_actions = [b.action for b in behaviors]

        for action in actions:
            assert action in tracked_actions


@pytest.mark.analytics
class TestRevenueMetrics:
    """Test revenue calculation metrics"""

    def test_get_revenue_metrics_basic(self, analytics_service, sample_orders_with_items, db):
        """Test basic revenue metrics calculation"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        metrics = analytics_service._get_revenue_metrics(start_date, end_date)

        assert 'total_revenue' in metrics
        assert metrics['total_revenue'] > 0

    def test_get_revenue_metrics_no_orders(self, analytics_service, db):
        """Test revenue metrics with no orders"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        metrics = analytics_service._get_revenue_metrics(start_date, end_date)

        assert 'total_revenue' in metrics
        assert metrics['total_revenue'] == 0


@pytest.mark.analytics
class TestOrderMetrics:
    """Test order calculation metrics"""

    def test_get_order_metrics_basic(self, analytics_service, sample_orders_with_items, db):
        """Test basic order metrics calculation"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        metrics = analytics_service._get_order_metrics(start_date, end_date)

        assert 'total_orders' in metrics
        assert metrics['total_orders'] > 0

    def test_get_order_metrics_average_value(self, analytics_service, sample_orders_with_items, db):
        """Test average order value calculation"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        metrics = analytics_service._get_order_metrics(start_date, end_date)

        assert 'average_order_value' in metrics
        if metrics['total_orders'] > 0:
            assert metrics['average_order_value'] > 0


@pytest.mark.analytics
class TestCustomerMetrics:
    """Test customer calculation metrics"""

    def test_get_customer_metrics_basic(self, analytics_service, sample_user, db):
        """Test basic customer metrics calculation"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        metrics = analytics_service._get_customer_metrics(start_date, end_date)

        assert 'total_customers' in metrics

    def test_get_customer_metrics_new_customers(self, analytics_service, db):
        """Test new customer metrics"""
        # Create customers within period
        for i in range(5):
            user = User(
                email=f'newuser{i}@example.com',
                phone=f'+99890123456{i}',
                role=UserRole.CUSTOMER,
                created_at=datetime.now(UTC) - timedelta(days=i)
            )
            db.session.add(user)
        db.session.commit()

        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        metrics = analytics_service._get_customer_metrics(start_date, end_date)

        assert 'new_customers' in metrics


@pytest.mark.analytics
class TestGrowthTrends:
    """Test growth trend calculations"""

    def test_get_growth_trends(self, analytics_service, sample_orders_with_items, db):
        """Test growth trend calculation"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        trends = analytics_service._get_growth_trends(start_date, end_date)

        assert 'revenue_growth' in trends or 'order_growth' in trends

    def test_get_growth_trends_comparison(self, analytics_service, sample_orders_with_items, db):
        """Test growth trends with comparison period"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        trends = analytics_service._get_growth_trends(start_date, end_date)

        assert trends is not None


@pytest.mark.analytics
class TestProductPerformance:
    """Test product performance analytics"""

    def test_get_product_performance(self, analytics_service, sample_orders_with_items, db):
        """Test product performance metrics"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        performance = analytics_service._get_product_performance(start_date, end_date)

        assert isinstance(performance, list)

    def test_get_product_performance_with_sales(self, analytics_service, sample_orders_with_items, db):
        """Test product performance includes sales data"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        performance = analytics_service._get_product_performance(start_date, end_date)

        if len(performance) > 0:
            assert 'product_id' in performance[0] or 'total_sold' in performance[0]


@pytest.mark.analytics
class TestSalesDistribution:
    """Test sales distribution analytics"""

    def test_get_hourly_sales_distribution(self, analytics_service, sample_orders_with_items, db):
        """Test hourly sales distribution"""
        start_date = datetime.now(UTC) - timedelta(days=7)
        end_date = datetime.now(UTC)

        distribution = analytics_service._get_hourly_sales_distribution(start_date, end_date)

        assert isinstance(distribution, list)

    def test_get_weekly_sales_distribution(self, analytics_service, sample_orders_with_items, db):
        """Test weekly sales distribution"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        distribution = analytics_service._get_weekly_sales_distribution(start_date, end_date)

        assert isinstance(distribution, list)

    def test_get_daily_sales_trend(self, analytics_service, sample_orders_with_items, db):
        """Test daily sales trend"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        trend = analytics_service._get_daily_sales_trend(start_date, end_date)

        assert isinstance(trend, list)


@pytest.mark.analytics
class TestChurnCalculation:
    """Test churn probability calculations"""

    def test_get_churn_risk_level_low(self, analytics_service):
        """Test low churn risk classification"""
        risk_level = analytics_service._get_churn_risk_level(0.2)
        assert risk_level in ['low', 'Low', 'LOW']

    def test_get_churn_risk_level_medium(self, analytics_service):
        """Test medium churn risk classification"""
        risk_level = analytics_service._get_churn_risk_level(0.5)
        assert risk_level in ['medium', 'Medium', 'MEDIUM']

    def test_get_churn_risk_level_high(self, analytics_service):
        """Test high churn risk classification"""
        risk_level = analytics_service._get_churn_risk_level(0.8)
        assert risk_level in ['high', 'High', 'HIGH']

    def test_calculate_user_churn_probability(self, analytics_service, sample_user, sample_orders_with_items, db):
        """Test user churn probability calculation"""
        with patch.object(analytics_service, '_calculate_user_churn_probability_optimized') as mock_calc:
            mock_calc.return_value = 0.3

            probability = analytics_service._calculate_user_churn_probability(sample_user.id)

            assert 0 <= probability <= 1


@pytest.mark.analytics
class TestCustomerSegmentation:
    """Test customer segmentation analytics"""

    def test_get_customer_segment_analysis(self, analytics_service, sample_orders_with_items, db):
        """Test customer segment analysis"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        segments = analytics_service._get_customer_segment_analysis(start_date, end_date)

        assert isinstance(segments, dict)

    def test_get_customer_lifetime_value_analysis(self, analytics_service, sample_orders_with_items, db):
        """Test customer lifetime value analysis"""
        ltv = analytics_service._get_customer_lifetime_value_analysis()

        assert isinstance(ltv, dict)


@pytest.mark.analytics
class TestGeographicAnalytics:
    """Test geographic analytics"""

    def test_get_geographic_sales_distribution(self, analytics_service, sample_orders_with_items, db):
        """Test geographic sales distribution"""
        start_date = datetime.now(UTC) - timedelta(days=30)
        end_date = datetime.now(UTC)

        distribution = analytics_service._get_geographic_sales_distribution(start_date, end_date)

        assert isinstance(distribution, list)
