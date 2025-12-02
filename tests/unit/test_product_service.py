"""
Unit tests for ProductService
Tests product management, pricing, search, and filtering functionality
"""
import pytest
from decimal import Decimal
from datetime import datetime, timedelta, UTC
from unittest.mock import patch, MagicMock

from business_app.services.product_service import ProductService
from business_app.models.product import Product, ProductCategory, PriceRule
from business_app.models.user import User, UserRole
from business_app.models.order import Order, OrderItem
from business_app.utils.exceptions import ValidationError, NotFoundError
from business_app.utils.constants import PriceRuleType


@pytest.fixture
def product_service():
    """Create ProductService instance"""
    return ProductService()


@pytest.fixture
def sample_category(db):
    """Create a sample product category"""
    category = ProductCategory(
        name='Water Products',
        description='Pure drinking water products',
        is_active=True,
        sort_order=1
    )
    db.session.add(category)
    db.session.commit()
    return category


@pytest.fixture
def sample_products(db, sample_category):
    """Create multiple sample products for testing"""
    products = [
        Product(
            name='Pure Water 19L',
            description='Premium quality drinking water',
            sku='WATER-19L',
            base_price=Decimal('15000.00'),
            stock_quantity=100,
            is_active=True,
            is_featured=True,
            category_id=sample_category.id,
            track_inventory=True
        ),
        Product(
            name='Pure Water 5L',
            description='Convenient 5 liter water bottle',
            sku='WATER-5L',
            base_price=Decimal('5000.00'),
            discount_price=Decimal('4500.00'),
            stock_quantity=200,
            is_active=True,
            is_featured=False,
            category_id=sample_category.id,
            track_inventory=True
        ),
        Product(
            name='Sparkling Water 1.5L',
            description='Refreshing sparkling water',
            sku='SPARKLING-1.5L',
            base_price=Decimal('3000.00'),
            stock_quantity=0,
            is_active=True,
            is_featured=False,
            category_id=sample_category.id,
            track_inventory=True
        ),
        Product(
            name='Mineral Water 0.5L',
            description='Natural mineral water',
            sku='MINERAL-0.5L',
            base_price=Decimal('2000.00'),
            stock_quantity=150,
            is_active=True,
            is_featured=True,
            category_id=sample_category.id,
            track_inventory=True
        ),
    ]

    for product in products:
        db.session.add(product)

    db.session.commit()
    return products


@pytest.fixture
def sample_price_rule(db, sample_products):
    """Create a sample price rule for volume discount"""
    rule = PriceRule(
        product_id=sample_products[0].id,
        rule_type=PriceRuleType.VOLUME_DISCOUNT,
        discount_type='percentage',
        discount_value=Decimal('10.00'),
        min_quantity=5,
        max_quantity=None,
        is_active=True,
        valid_from=datetime.now(UTC) - timedelta(days=1),
        valid_until=datetime.now(UTC) + timedelta(days=30)
    )
    db.session.add(rule)
    db.session.commit()
    return rule


@pytest.mark.critical
@pytest.mark.product
class TestProductRetrieval:
    """Test product retrieval methods"""

    def test_get_products_with_filters_basic(self, product_service, sample_products, db):
        """Test getting products with basic pagination"""
        products, total, page, per_page, metadata = product_service.get_products_with_filters(
            page=1,
            per_page=10
        )

        assert len(products) == 4
        assert total == 4
        assert page == 1
        assert per_page == 10

    def test_get_products_with_category_filter(self, product_service, sample_products, sample_category, db):
        """Test filtering products by category"""
        products, total, page, per_page, metadata = product_service.get_products_with_filters(
            category_id=sample_category.id,
            page=1,
            per_page=10
        )

        assert len(products) == 4
        assert all(p.category_id == sample_category.id for p in products)

    def test_get_products_with_search(self, product_service, sample_products, db):
        """Test search functionality"""
        products, total, page, per_page, metadata = product_service.get_products_with_filters(
            search='5L',
            page=1,
            per_page=10
        )

        assert len(products) == 1
        assert products[0].sku == 'WATER-5L'

    def test_get_products_with_price_filter(self, product_service, sample_products, db):
        """Test filtering by price range"""
        products, total, page, per_page, metadata = product_service.get_products_with_filters(
            min_price=3000,
            max_price=10000,
            page=1,
            per_page=10
        )

        assert len(products) == 2
        assert all(3000 <= p.base_price <= 10000 for p in products)

    def test_get_products_featured_only(self, product_service, sample_products, db):
        """Test filtering featured products"""
        products, total, page, per_page, metadata = product_service.get_products_with_filters(
            is_featured=True,
            page=1,
            per_page=10
        )

        assert len(products) == 2
        assert all(p.is_featured for p in products)

    def test_get_products_in_stock_only(self, product_service, sample_products, db):
        """Test filtering in-stock products"""
        products, total, page, per_page, metadata = product_service.get_products_with_filters(
            in_stock_only=True,
            page=1,
            per_page=10
        )

        # Should exclude the sparkling water (stock_quantity=0)
        assert len(products) == 3
        assert all(p.stock_quantity > 0 for p in products if p.track_inventory)

    def test_get_products_sorting_by_name(self, product_service, sample_products, db):
        """Test sorting products by name"""
        products, total, page, per_page, metadata = product_service.get_products_with_filters(
            sort_by='name',
            sort_order='asc',
            page=1,
            per_page=10
        )

        assert products[0].name == 'Mineral Water 0.5L'
        assert products[-1].name == 'Sparkling Water 1.5L'

    def test_get_products_sorting_by_price_desc(self, product_service, sample_products, db):
        """Test sorting products by price descending"""
        products, total, page, per_page, metadata = product_service.get_products_with_filters(
            sort_by='price',
            sort_order='desc',
            page=1,
            per_page=10
        )

        assert products[0].base_price == Decimal('15000.00')
        assert products[-1].base_price == Decimal('2000.00')

    def test_get_products_pagination(self, product_service, sample_products, db):
        """Test pagination works correctly"""
        # Get first page with 2 items
        products_page1, total, page, per_page, metadata = product_service.get_products_with_filters(
            page=1,
            per_page=2
        )

        # Get second page
        products_page2, total2, page2, per_page2, metadata2 = product_service.get_products_with_filters(
            page=2,
            per_page=2
        )

        assert len(products_page1) == 2
        assert len(products_page2) == 2
        assert total == 4
        assert products_page1[0].id != products_page2[0].id

    def test_get_products_invalid_page(self, product_service, db):
        """Test validation for invalid page number"""
        with pytest.raises(ValidationError, match="Page number must be >= 1"):
            product_service.get_products_with_filters(page=0, per_page=10)

    def test_get_products_invalid_per_page(self, product_service, db):
        """Test validation for invalid per_page"""
        with pytest.raises(ValidationError, match="Per page must be >= 1"):
            product_service.get_products_with_filters(page=1, per_page=0)

    def test_get_products_max_per_page_limit(self, product_service, sample_products, db):
        """Test per_page is capped at max limit"""
        products, total, page, per_page, metadata = product_service.get_products_with_filters(
            page=1,
            per_page=200  # Exceeds max_per_page of 100
        )

        assert per_page == 100

    def test_get_product_by_id_success(self, product_service, sample_products, db):
        """Test retrieving product by ID"""
        with patch.object(product_service, '_track_product_view'):
            product = product_service.get_product_by_id(
                product_id=sample_products[0].id
            )

            assert product.id == sample_products[0].id
            assert product.name == 'Pure Water 19L'

    def test_get_product_by_id_not_found(self, product_service, db):
        """Test retrieving non-existent product"""
        with pytest.raises(NotFoundError, match="Product with ID 99999 not found"):
            product_service.get_product_by_id(product_id=99999)

    def test_get_product_by_id_tracks_view(self, product_service, sample_products, db):
        """Test that product view is tracked"""
        with patch.object(product_service, '_track_product_view') as mock_track:
            product = product_service.get_product_by_id(
                product_id=sample_products[0].id
            )

            mock_track.assert_called_once_with(sample_products[0].id, None)


@pytest.mark.critical
@pytest.mark.product
class TestProductPricing:
    """Test dynamic pricing logic"""

    def test_calculate_price_basic(self, product_service, sample_products, db):
        """Test basic price calculation without discounts"""
        pricing = product_service.calculate_product_price(
            product_id=sample_products[0].id,
            quantity=1
        )

        assert pricing['product_id'] == sample_products[0].id
        assert pricing['quantity'] == 1
        assert pricing['base_price'] == 15000.00
        assert pricing['unit_price'] == 15000.00
        assert pricing['subtotal'] == 15000.00

    def test_calculate_price_with_discount_price(self, product_service, sample_products, db):
        """Test price calculation uses discount_price if available"""
        # sample_products[1] has discount_price
        pricing = product_service.calculate_product_price(
            product_id=sample_products[1].id,
            quantity=1
        )

        assert pricing['base_price'] == 5000.00
        assert pricing['discount_price'] == 4500.00
        assert pricing['unit_price'] == 4500.00

    def test_calculate_price_with_volume_discount(self, product_service, sample_products, sample_price_rule, db):
        """Test volume discount is applied"""
        # sample_price_rule: 10% off for 5+ items
        pricing = product_service.calculate_product_price(
            product_id=sample_products[0].id,
            quantity=5
        )

        # 10% off 15000 = 1500 discount
        assert pricing['price_rule_discount'] == 1500.00
        assert pricing['unit_price'] == 13500.00
        assert pricing['subtotal'] == 67500.00

    def test_calculate_price_volume_discount_not_met(self, product_service, sample_products, sample_price_rule, db):
        """Test volume discount is not applied when minimum quantity not met"""
        pricing = product_service.calculate_product_price(
            product_id=sample_products[0].id,
            quantity=3  # Less than min_quantity of 5
        )

        assert pricing['price_rule_discount'] == 0.00
        assert pricing['unit_price'] == 15000.00

    def test_calculate_price_invalid_quantity(self, product_service, sample_products, db):
        """Test validation for invalid quantity"""
        with pytest.raises(ValidationError, match="Quantity must be >= 1"):
            product_service.calculate_product_price(
                product_id=sample_products[0].id,
                quantity=0
            )

    def test_calculate_price_product_not_found(self, product_service, db):
        """Test error when product doesn't exist"""
        with pytest.raises(NotFoundError, match="Product with ID 99999 not found"):
            product_service.calculate_product_price(
                product_id=99999,
                quantity=1
            )

    def test_calculate_price_savings_calculation(self, product_service, sample_products, sample_price_rule, db):
        """Test savings calculation is correct"""
        pricing = product_service.calculate_product_price(
            product_id=sample_products[0].id,
            quantity=5
        )

        # Savings: (15000 - 13500) * 5 = 7500
        assert pricing['savings'] == 7500.00


@pytest.mark.product
class TestProductCategories:
    """Test category management"""

    def test_get_categories(self, product_service, sample_category, db):
        """Test getting all categories"""
        categories = product_service.get_categories()

        assert len(categories) >= 1
        assert sample_category in categories

    def test_get_categories_active_only(self, product_service, sample_category, db):
        """Test filtering active categories only"""
        # Create inactive category
        inactive = ProductCategory(
            name='Inactive Category',
            is_active=False
        )
        db.session.add(inactive)
        db.session.commit()

        categories = product_service.get_categories(include_inactive=False)

        assert inactive not in categories
        assert sample_category in categories

    def test_get_categories_include_inactive(self, product_service, sample_category, db):
        """Test including inactive categories"""
        inactive = ProductCategory(
            name='Inactive Category',
            is_active=False
        )
        db.session.add(inactive)
        db.session.commit()

        categories = product_service.get_categories(include_inactive=True)

        assert inactive in categories
        assert sample_category in categories

    def test_get_category_by_id(self, product_service, sample_category, db):
        """Test getting category by ID"""
        category = product_service.get_category_by_id(sample_category.id)

        assert category.id == sample_category.id
        assert category.name == 'Water Products'

    def test_get_category_by_id_not_found(self, product_service, db):
        """Test error when category not found"""
        with pytest.raises(NotFoundError, match="Category with ID 99999 not found"):
            product_service.get_category_by_id(99999)


@pytest.mark.product
class TestProductSearch:
    """Test search and suggestions"""

    def test_get_search_suggestions(self, product_service, sample_products, db):
        """Test search suggestions"""
        suggestions = product_service.get_search_suggestions(
            query='Water',
            limit=5
        )

        assert len(suggestions) >= 3
        assert all('id' in s for s in suggestions)
        assert all('name' in s for s in suggestions)
        assert all('price' in s for s in suggestions)

    def test_get_search_suggestions_by_sku(self, product_service, sample_products, db):
        """Test search suggestions by SKU"""
        suggestions = product_service.get_search_suggestions(
            query='WATER-5L',
            limit=5
        )

        assert len(suggestions) == 1
        assert suggestions[0]['sku'] == 'WATER-5L'

    def test_get_search_suggestions_short_query(self, product_service, sample_products, db):
        """Test no suggestions for short query"""
        suggestions = product_service.get_search_suggestions(
            query='W',  # Less than 2 characters
            limit=5
        )

        assert len(suggestions) == 0

    def test_get_search_suggestions_empty_query(self, product_service, sample_products, db):
        """Test no suggestions for empty query"""
        suggestions = product_service.get_search_suggestions(
            query='',
            limit=5
        )

        assert len(suggestions) == 0

    def test_get_search_suggestions_featured_first(self, product_service, sample_products, db):
        """Test featured products appear first in suggestions"""
        suggestions = product_service.get_search_suggestions(
            query='Water',
            limit=10
        )

        # First suggestions should be featured products
        # We know 'Pure Water 19L' and 'Mineral Water 0.5L' are featured
        featured_count = sum(1 for p in sample_products if p.is_featured and 'Water' in p.name)
        assert featured_count >= 2

    def test_get_featured_products(self, product_service, sample_products, db):
        """Test getting featured products"""
        featured = product_service.get_featured_products(limit=5)

        assert len(featured) == 2  # We have 2 featured products
        assert all(p.is_featured for p in featured)

    def test_get_featured_products_limit(self, product_service, sample_products, db):
        """Test limit is respected for featured products"""
        featured = product_service.get_featured_products(limit=1)

        assert len(featured) == 1


@pytest.mark.product
class TestPopularProducts:
    """Test popular products functionality"""

    def test_get_popular_products_with_sales(self, product_service, sample_products, sample_user, db):
        """Test getting popular products based on sales"""
        # Create orders with items
        order1 = Order(
            user_id=sample_user.id,
            status='delivered',
            total_amount=Decimal('45000.00'),
            created_at=datetime.now(UTC)
        )
        db.session.add(order1)
        db.session.flush()

        # Add order items
        item1 = OrderItem(
            order_id=order1.id,
            product_id=sample_products[0].id,
            quantity=3,
            unit_price=Decimal('15000.00'),
            total_price=Decimal('45000.00')
        )
        db.session.add(item1)
        db.session.commit()

        popular = product_service.get_popular_products(
            period_days=30,
            limit=5
        )

        assert len(popular) >= 1
        assert popular[0]['product'].id == sample_products[0].id
        assert popular[0]['total_sold'] == 3
        assert popular[0]['order_count'] == 1

    def test_get_popular_products_no_sales(self, product_service, sample_products, db):
        """Test getting popular products when no sales exist"""
        popular = product_service.get_popular_products(
            period_days=30,
            limit=5
        )

        assert len(popular) == 0

    def test_get_popular_products_period_filter(self, product_service, sample_products, sample_user, db):
        """Test period filtering for popular products"""
        # Create old order (outside period)
        old_order = Order(
            user_id=sample_user.id,
            status='delivered',
            total_amount=Decimal('15000.00'),
            created_at=datetime.now(UTC) - timedelta(days=60)
        )
        db.session.add(old_order)
        db.session.flush()

        item = OrderItem(
            order_id=old_order.id,
            product_id=sample_products[0].id,
            quantity=1,
            unit_price=Decimal('15000.00'),
            total_price=Decimal('15000.00')
        )
        db.session.add(item)
        db.session.commit()

        # Should not appear in 30-day popular products
        popular = product_service.get_popular_products(
            period_days=30,
            limit=5
        )

        assert len(popular) == 0


@pytest.mark.product
class TestPriceRuleDiscounts:
    """Test price rule discount calculation"""

    def test_calculate_price_rule_fixed_discount(self, product_service, sample_products, db):
        """Test fixed amount discount"""
        # Create fixed discount rule
        rule = PriceRule(
            product_id=sample_products[0].id,
            rule_type=PriceRuleType.VOLUME_DISCOUNT,
            discount_type='fixed',
            discount_value=Decimal('1000.00'),
            min_quantity=1,
            is_active=True
        )
        db.session.add(rule)
        db.session.commit()

        discount = product_service._calculate_price_rule_discount(
            product=sample_products[0],
            quantity=1,
            user=None
        )

        assert discount == 1000.00

    def test_calculate_price_rule_percentage_discount(self, product_service, sample_products, db):
        """Test percentage discount"""
        rule = PriceRule(
            product_id=sample_products[0].id,
            rule_type=PriceRuleType.VOLUME_DISCOUNT,
            discount_type='percentage',
            discount_value=Decimal('20.00'),
            min_quantity=1,
            is_active=True
        )
        db.session.add(rule)
        db.session.commit()

        discount = product_service._calculate_price_rule_discount(
            product=sample_products[0],
            quantity=1,
            user=None
        )

        # 20% of 15000 = 3000
        assert discount == 3000.00

    def test_calculate_price_rule_best_discount(self, product_service, sample_products, db):
        """Test that best discount is selected when multiple rules apply"""
        # Create two overlapping rules
        rule1 = PriceRule(
            product_id=sample_products[0].id,
            rule_type=PriceRuleType.VOLUME_DISCOUNT,
            discount_type='fixed',
            discount_value=Decimal('1000.00'),
            min_quantity=1,
            is_active=True
        )
        rule2 = PriceRule(
            product_id=sample_products[0].id,
            rule_type=PriceRuleType.CUSTOMER_TYPE,
            discount_type='fixed',
            discount_value=Decimal('2000.00'),
            min_quantity=1,
            is_active=True
        )
        db.session.add_all([rule1, rule2])
        db.session.commit()

        discount = product_service._calculate_price_rule_discount(
            product=sample_products[0],
            quantity=1,
            user=None
        )

        # Should get the better discount
        assert discount == 2000.00

    def test_calculate_price_rule_expired(self, product_service, sample_products, db):
        """Test expired price rule is not applied"""
        rule = PriceRule(
            product_id=sample_products[0].id,
            rule_type=PriceRuleType.VOLUME_DISCOUNT,
            discount_type='fixed',
            discount_value=Decimal('1000.00'),
            min_quantity=1,
            is_active=True,
            valid_until=datetime.now(UTC) - timedelta(days=1)  # Expired
        )
        db.session.add(rule)
        db.session.commit()

        discount = product_service._calculate_price_rule_discount(
            product=sample_products[0],
            quantity=1,
            user=None
        )

        assert discount == 0.00

    def test_calculate_price_rule_not_yet_valid(self, product_service, sample_products, db):
        """Test future price rule is not applied"""
        rule = PriceRule(
            product_id=sample_products[0].id,
            rule_type=PriceRuleType.VOLUME_DISCOUNT,
            discount_type='fixed',
            discount_value=Decimal('1000.00'),
            min_quantity=1,
            is_active=True,
            valid_from=datetime.now(UTC) + timedelta(days=1)  # Future
        )
        db.session.add(rule)
        db.session.commit()

        discount = product_service._calculate_price_rule_discount(
            product=sample_products[0],
            quantity=1,
            user=None
        )

        assert discount == 0.00


@pytest.mark.product
class TestAnalyticsTracking:
    """Test analytics tracking integration"""

    def test_track_product_view(self, product_service, sample_products, db):
        """Test product view tracking"""
        mock_analytics = MagicMock()

        with patch('business_app.services.product_service.get_analytics_service', return_value=mock_analytics):
            product_service._track_product_view(
                product_id=sample_products[0].id,
                user=None
            )

            mock_analytics.track_product_view.assert_called_once_with(
                product_id=sample_products[0].id,
                user_id=None
            )

    def test_track_search_analytics(self, product_service, db):
        """Test search analytics tracking"""
        mock_analytics = MagicMock()

        with patch('business_app.services.product_service.get_analytics_service', return_value=mock_analytics):
            product_service._track_search_analytics(
                search_term='water',
                results_count=5,
                user=None
            )

            mock_analytics.track_search.assert_called_once_with(
                search_term='water',
                results_count=5,
                user_id=None
            )

    def test_track_analytics_handles_errors(self, product_service, sample_products, db):
        """Test that analytics errors don't break product operations"""
        mock_analytics = MagicMock()
        mock_analytics.track_product_view.side_effect = Exception("Analytics service unavailable")

        with patch('business_app.services.product_service.get_analytics_service', return_value=mock_analytics):
            # Should not raise exception
            product_service._track_product_view(
                product_id=sample_products[0].id,
                user=None
            )
