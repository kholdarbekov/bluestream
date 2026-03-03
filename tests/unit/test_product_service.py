"""Unit tests for ProductService aligned with the current implementation."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import event

from business_app.models.order import Order, OrderItem
from business_app.models.product import PriceRule, Product
from business_app.services.product_service import ProductService
from business_app.utils.constants import OrderStatus, PriceRuleType
from business_app.utils.exceptions import NotFoundError, ValidationError


@pytest.fixture
def product_service(app):
    with app.app_context():
        return ProductService()


@pytest.fixture
def sample_products(db, sample_category):
    products = [
        Product(
            name='Pure Water 19L',
            description='Premium quality drinking water',
            sku='WATER-19L',
            base_price=Decimal('15000.00'),
            category_id=sample_category.id,
            size='19L',
            volume=19.0,
            volume_unit='L',
            stock_quantity=100,
            is_featured=True,
            is_active=True,
            track_inventory=True,
        ),
        Product(
            name='Pure Water 5L',
            description='Convenient 5 liter bottle',
            sku='WATER-5L',
            base_price=Decimal('5000.00'),
            discount_price=Decimal('4500.00'),
            category_id=sample_category.id,
            size='5L',
            volume=5.0,
            volume_unit='L',
            stock_quantity=200,
            is_featured=False,
            is_active=True,
            track_inventory=True,
        ),
        Product(
            name='Sparkling Water 1.5L',
            description='Refreshing sparkling water',
            sku='SPARKLING-1.5L',
            base_price=Decimal('3000.00'),
            category_id=sample_category.id,
            size='5L',
            volume=1.5,
            volume_unit='L',
            stock_quantity=0,
            is_featured=False,
            is_active=True,
            track_inventory=True,
        ),
        Product(
            name='Mineral Water 0.5L',
            description='Natural mineral water',
            sku='MINERAL-0.5L',
            base_price=Decimal('2000.00'),
            category_id=sample_category.id,
            size='5L',
            volume=0.5,
            volume_unit='L',
            stock_quantity=150,
            is_featured=True,
            is_active=True,
            track_inventory=True,
        ),
    ]

    db.session.add_all(products)
    db.session.commit()
    return products


@pytest.mark.unit
@pytest.mark.product
class TestProductRetrieval:
    def test_get_products_with_filters_basic(self, product_service, sample_products):
        products, total, page, per_page, metadata = product_service.get_products_with_filters(page=1, per_page=10)

        assert len(products) == 4
        assert total == 4
        assert page == 1
        assert per_page == 10
        assert metadata['total_results'] == 4

    def test_get_products_with_search_filter(self, product_service, sample_products):
        products, total, *_ = product_service.get_products_with_filters(search='WATER-5L', page=1, per_page=10)

        assert total == 1
        assert products[0].sku == 'WATER-5L'

    def test_get_products_in_stock_only(self, product_service, sample_products):
        products, total, *_ = product_service.get_products_with_filters(in_stock_only=True, page=1, per_page=10)

        assert total == 3
        assert all(p.stock_quantity > 0 for p in products)

    def test_get_products_eager_loads_category_with_bounded_queries(self, product_service, sample_products, db):
        statements = []

        def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", _before_cursor_execute)
        try:
            products, total, *_ = product_service.get_products_with_filters(page=1, per_page=10)
            category_names = [product.category.name if product.category else None for product in products]
        finally:
            event.remove(db.engine, "before_cursor_execute", _before_cursor_execute)

        assert total == 4
        assert all(name == 'Water' for name in category_names)
        assert len(statements) <= 3

    def test_get_products_validates_pagination(self, product_service):
        with pytest.raises(ValidationError):
            product_service.get_products_with_filters(page=0, per_page=10)

        with pytest.raises(ValidationError):
            product_service.get_products_with_filters(page=1, per_page=0)

    def test_get_products_caps_per_page(self, product_service, sample_products):
        _, _, _, per_page, _ = product_service.get_products_with_filters(page=1, per_page=1000)
        assert per_page == 100

    def test_get_product_by_id_tracks_view(self, product_service, sample_products):
        with patch.object(product_service, '_track_product_view') as mock_track:
            product = product_service.get_product_by_id(sample_products[0].id, current_user_id=123)

        assert product.id == sample_products[0].id
        mock_track.assert_called_once_with(sample_products[0].id, 123)

    def test_get_product_by_id_not_found(self, product_service, db):
        with pytest.raises(NotFoundError):
            product_service.get_product_by_id(999999)


@pytest.mark.unit
@pytest.mark.product
class TestPricing:
    def test_calculate_product_price_uses_discount_price(self, product_service, sample_products):
        pricing = product_service.calculate_product_price(product_id=sample_products[1].id, quantity=1)

        assert pricing['base_price'] == 5000.0
        assert pricing['discount_price'] == 4500.0
        assert pricing['unit_price'] == 4500.0

    def test_calculate_product_price_applies_best_price_rule(self, product_service, sample_products, db):
        discount_rule = PriceRule(
            product_id=sample_products[0].id,
            rule_type=PriceRuleType.BULK_DISCOUNT,
            name='Bulk 10%',
            discount_type='percentage',
            discount_value=Decimal('10.00'),
            min_quantity=5,
            is_active=True,
            valid_from=datetime.now(UTC) - timedelta(days=1),
            valid_until=datetime.now(UTC) + timedelta(days=1),
        )
        db.session.add(discount_rule)
        db.session.commit()

        pricing = product_service.calculate_product_price(product_id=sample_products[0].id, quantity=5)

        assert pricing['price_rule_discount'] == 1500.0
        assert pricing['unit_price'] == 13500.0
        assert pricing['subtotal'] == 67500.0

    def test_calculate_product_price_validates_quantity(self, product_service, sample_products):
        with pytest.raises(ValidationError):
            product_service.calculate_product_price(product_id=sample_products[0].id, quantity=0)


@pytest.mark.unit
@pytest.mark.product
class TestCategoriesAndSearch:
    def test_get_categories_active_only(self, product_service, sample_category, db):
        inactive = type(sample_category)(name='Inactive', description='x', is_active=False, sort_order=99)
        db.session.add(inactive)
        db.session.commit()

        categories = product_service.get_categories(include_inactive=False)
        assert all(c.is_active for c in categories)

    def test_get_category_by_id_not_found(self, product_service, db):
        with pytest.raises(NotFoundError):
            product_service.get_category_by_id(999999)

    def test_get_search_suggestions(self, product_service, sample_products):
        suggestions = product_service.get_search_suggestions('Water', limit=5)

        assert len(suggestions) >= 1
        assert {'id', 'name', 'sku', 'price', 'image'}.issubset(set(suggestions[0].keys()))

    def test_get_search_suggestions_short_query_returns_empty(self, product_service):
        assert product_service.get_search_suggestions('W', limit=5) == []

    def test_get_featured_products_respects_limit(self, product_service, sample_products):
        featured = product_service.get_featured_products(limit=1)
        assert len(featured) == 1
        assert featured[0].is_featured is True


@pytest.mark.unit
@pytest.mark.product
class TestPopularAndAnalytics:
    def test_get_popular_products_with_sales(self, product_service, sample_products, sample_user, db):
        order = Order(
            user_id=sample_user.id,
            status=OrderStatus.DELIVERED,
            subtotal=Decimal('30000.00'),
            total_amount=Decimal('30000.00'),
        )
        db.session.add(order)
        db.session.flush()

        item = OrderItem(
            order_id=order.id,
            product_id=sample_products[0].id,
            quantity=2,
            unit_price=Decimal('15000.00'),
            total_price=Decimal('30000.00'),
        )
        db.session.add(item)
        db.session.commit()

        popular = product_service.get_popular_products(period_days=30, limit=5)

        assert len(popular) == 1
        assert popular[0]['product'].id == sample_products[0].id
        assert int(popular[0]['total_sold']) == 2

    def test_track_product_view_uses_analytics_service(self, product_service, sample_products):
        mock_analytics = MagicMock()

        with patch('business_app.utils.service_factory.get_analytics_service', return_value=mock_analytics):
            product_service._track_product_view(sample_products[0].id, user_id=None)

        mock_analytics.track_product_view.assert_called_once_with(product_id=sample_products[0].id, user_id=None)

    def test_track_search_analytics_uses_analytics_service(self, product_service, sample_user):
        mock_analytics = MagicMock()

        with patch('business_app.utils.service_factory.get_analytics_service', return_value=mock_analytics):
            product_service._track_search_analytics('water', 3, user=sample_user)

        mock_analytics.track_search.assert_called_once_with(search_term='water', results_count=3, user_id=sample_user.id)
