"""
Unit tests for CartService
Tests cart validation, pricing, discounts, and checkout preparation
"""
import pytest
from decimal import Decimal
from datetime import datetime, timedelta, UTC
from unittest.mock import patch, MagicMock

from business_app.services.cart_service import CartService
from business_app.models.product import Product, PriceRule
from business_app.models.user import User, UserRole
from business_app.models.order import Order, OrderItem
from business_app.models.promotional_campaign import PromotionalCampaign
from business_app.utils.exceptions import ValidationError, NotFoundError
from business_app.utils.constants import OrderStatus, PriceRuleType


@pytest.fixture
def cart_service(app):
    """Create CartService instance with app context"""
    with app.app_context():
        return CartService()


@pytest.fixture
def sample_promo_campaign(db):
    """Create a sample promotional campaign"""
    campaign = PromotionalCampaign(
        name='Summer Sale',
        description='10% off all orders',
        promo_code='SUMMER10',
        discount_type='percentage',
        discount_value=Decimal('10.00'),
        min_order_value=Decimal('20000.00'),
        max_discount_amount=Decimal('5000.00'),
        start_date=datetime.now(UTC) - timedelta(days=1),
        end_date=datetime.now(UTC) + timedelta(days=30),
        is_active=True,
        max_uses=100,
        times_used=0,
        max_uses_per_customer=2
    )
    db.session.add(campaign)
    db.session.commit()
    return campaign


@pytest.fixture
def premium_user(db):
    """Create a premium user"""
    user = User(
        email='premium@example.com',
        phone='+998901234599',
        role=UserRole.CUSTOMER,
        is_verified=True,
        is_premium=True,
        loyalty_points=1000
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.mark.critical
@pytest.mark.cart
class TestCartValidation:
    """Test cart validation logic"""

    def test_validate_cart_items_basic(self, cart_service, sample_product, db):
        """Test basic cart validation"""
        items = [
            {'product_id': sample_product.id, 'quantity': 2}
        ]

        validated, errors = cart_service.validate_cart_items(items)

        assert len(validated) == 1
        assert len(errors) == 0
        assert validated[0]['product_id'] == sample_product.id
        assert validated[0]['quantity'] == 2

    def test_validate_cart_empty(self, cart_service, db):
        """Test validation fails for empty cart"""
        with pytest.raises(ValidationError, match="Cart cannot be empty"):
            cart_service.validate_cart_items([])

    def test_validate_cart_too_many_items(self, cart_service, sample_product, db, app):
        """Test validation fails when cart exceeds maximum items"""
        with app.app_context():
            max_items = cart_service.max_cart_items
            items = [{'product_id': sample_product.id, 'quantity': 1} for _ in range(max_items + 1)]

            with pytest.raises(ValidationError, match=f"Maximum {max_items} items allowed"):
                cart_service.validate_cart_items(items)

    def test_validate_cart_missing_fields(self, cart_service, db):
        """Test validation catches missing fields"""
        items = [{'product_id': 1}]  # Missing quantity

        validated, errors = cart_service.validate_cart_items(items)

        assert len(validated) == 0
        assert len(errors) == 1
        assert 'Missing product_id or quantity' in errors[0]

    def test_validate_cart_duplicate_products(self, cart_service, sample_product, db):
        """Test validation catches duplicate products"""
        items = [
            {'product_id': sample_product.id, 'quantity': 1},
            {'product_id': sample_product.id, 'quantity': 2}  # Duplicate
        ]

        validated, errors = cart_service.validate_cart_items(items)

        assert any('Duplicate item' in error for error in errors)

    def test_validate_cart_invalid_quantity(self, cart_service, sample_product, db):
        """Test validation catches invalid quantity"""
        items = [
            {'product_id': sample_product.id, 'quantity': 0},
            {'product_id': sample_product.id + 1, 'quantity': -1}
        ]

        validated, errors = cart_service.validate_cart_items(items)

        assert len(errors) >= 1
        assert any('Invalid quantity' in error for error in errors)

    def test_validate_cart_product_not_found(self, cart_service, db):
        """Test validation handles non-existent products"""
        items = [{'product_id': 99999, 'quantity': 1}]

        validated, errors = cart_service.validate_cart_items(items)

        assert len(validated) == 0
        assert any('Not found or inactive' in error for error in errors)

    def test_validate_cart_insufficient_stock(self, cart_service, sample_product, db):
        """Test validation catches insufficient stock"""
        sample_product.stock_quantity = 5
        sample_product.track_inventory = True
        db.session.commit()

        items = [{'product_id': sample_product.id, 'quantity': 10}]

        validated, errors = cart_service.validate_cart_items(items)

        assert len(validated) == 0
        assert any('Only 5 available' in error for error in errors)

    def test_validate_cart_no_inventory_tracking(self, cart_service, sample_product, db):
        """Test validation passes when inventory tracking disabled"""
        sample_product.track_inventory = False
        sample_product.stock_quantity = 0
        db.session.commit()

        items = [{'product_id': sample_product.id, 'quantity': 100}]

        validated, errors = cart_service.validate_cart_items(items)

        assert len(validated) == 1
        assert len(errors) == 0


@pytest.mark.critical
@pytest.mark.cart
class TestCartEstimate:
    """Test cart estimate calculation"""

    def test_calculate_cart_estimate_basic(self, cart_service, sample_user, sample_product, db):
        """Test basic cart estimate calculation"""
        items = [{'product_id': sample_product.id, 'quantity': 2}]

        estimate = cart_service.calculate_cart_estimate(
            user_id=sample_user.id,
            items=items
        )

        assert 'items' in estimate
        assert 'pricing' in estimate
        assert estimate['pricing']['items_subtotal'] > 0
        assert estimate['validation']['cart_item_count'] == 1

    def test_calculate_cart_estimate_with_delivery(self, cart_service, sample_user, sample_product, db, app):
        """Test cart estimate includes delivery fee"""
        items = [{'product_id': sample_product.id, 'quantity': 1}]

        with app.app_context():
            estimate = cart_service.calculate_cart_estimate(
                user_id=sample_user.id,
                items=items
            )

            # Should have delivery fee since below free delivery threshold
            assert estimate['pricing']['delivery_fee'] > 0

    def test_calculate_cart_estimate_free_delivery_threshold(self, cart_service, sample_user, db, app):
        """Test free delivery when threshold met"""
        # Create expensive product to meet threshold
        with app.app_context():
            expensive_product = Product(
                name='Expensive Water',
                base_price=Decimal('150000.00'),
                stock_quantity=100,
                is_active=True
            )
            db.session.add(expensive_product)
            db.session.commit()

            items = [{'product_id': expensive_product.id, 'quantity': 1}]

            estimate = cart_service.calculate_cart_estimate(
                user_id=sample_user.id,
                items=items
            )

            # Should have free delivery
            assert estimate['delivery']['is_free'] is True
            assert estimate['pricing']['delivery_fee'] == 0

    def test_calculate_cart_estimate_premium_user_free_delivery(self, cart_service, premium_user, sample_product, db):
        """Test premium users get free delivery"""
        items = [{'product_id': sample_product.id, 'quantity': 1}]

        estimate = cart_service.calculate_cart_estimate(
            user_id=premium_user.id,
            items=items
        )

        assert estimate['delivery']['is_free'] is True
        assert estimate['pricing']['delivery_fee'] == 0

    def test_calculate_cart_estimate_with_promo_code(self, cart_service, sample_user, sample_product, sample_promo_campaign, db):
        """Test cart estimate with promotional code"""
        # Create cart above minimum
        product = Product(
            name='Water Bundle',
            base_price=Decimal('25000.00'),
            stock_quantity=100,
            is_active=True
        )
        db.session.add(product)
        db.session.commit()

        items = [{'product_id': product.id, 'quantity': 1}]

        estimate = cart_service.calculate_cart_estimate(
            user_id=sample_user.id,
            items=items,
            promo_code='SUMMER10'
        )

        assert estimate['pricing']['promo_discount'] > 0
        assert estimate['promotional_code'] is not None
        assert estimate['promotional_code']['code'] == 'SUMMER10'

    def test_calculate_cart_estimate_with_loyalty_points(self, cart_service, premium_user, sample_product, db):
        """Test cart estimate with loyalty points"""
        items = [{'product_id': sample_product.id, 'quantity': 1}]

        estimate = cart_service.calculate_cart_estimate(
            user_id=premium_user.id,
            items=items,
            loyalty_points_used=5  # 5 points = 500 UZS
        )

        assert estimate['loyalty']['points_used'] == 5
        assert estimate['loyalty']['discount_applied'] > 0
        assert estimate['pricing']['loyalty_discount'] > 0

    def test_calculate_cart_estimate_below_minimum(self, cart_service, sample_user, db, app):
        """Test cart estimate fails below minimum order"""
        with app.app_context():
            # Create cheap product below minimum
            cheap_product = Product(
                name='Cheap Water',
                base_price=Decimal('1000.00'),
                stock_quantity=100,
                is_active=True
            )
            db.session.add(cheap_product)
            db.session.commit()

            items = [{'product_id': cheap_product.id, 'quantity': 1}]

            with pytest.raises(ValidationError, match="Minimum order amount"):
                cart_service.calculate_cart_estimate(
                    user_id=sample_user.id,
                    items=items
                )

    def test_calculate_cart_estimate_user_not_found(self, cart_service, sample_product, db):
        """Test cart estimate fails for non-existent user"""
        items = [{'product_id': sample_product.id, 'quantity': 1}]

        with pytest.raises(NotFoundError, match="User with ID 99999 not found"):
            cart_service.calculate_cart_estimate(
                user_id=99999,
                items=items
            )

    def test_calculate_cart_estimate_loyalty_points_earned(self, cart_service, sample_user, db):
        """Test loyalty points earned calculation"""
        # Create product worth enough to earn points
        product = Product(
            name='Premium Water',
            base_price=Decimal('50000.00'),
            stock_quantity=100,
            is_active=True
        )
        db.session.add(product)
        db.session.commit()

        items = [{'product_id': product.id, 'quantity': 1}]

        estimate = cart_service.calculate_cart_estimate(
            user_id=sample_user.id,
            items=items
        )

        assert estimate['loyalty']['points_earned'] > 0


@pytest.mark.cart
class TestPromoCodeValidation:
    """Test promotional code validation"""

    def test_validate_promo_code_valid(self, cart_service, sample_user, sample_promo_campaign, db):
        """Test validating a valid promo code"""
        result = cart_service.validate_promo_code(
            promo_code='SUMMER10',
            user_id=sample_user.id,
            cart_total=25000
        )

        assert result['valid'] is True
        assert result['code'] == 'SUMMER10'
        assert result['discount_amount'] > 0

    def test_validate_promo_code_case_insensitive(self, cart_service, sample_user, sample_promo_campaign, db):
        """Test promo code is case insensitive"""
        result = cart_service.validate_promo_code(
            promo_code='summer10',  # lowercase
            user_id=sample_user.id,
            cart_total=25000
        )

        assert result['valid'] is True
        assert result['code'] == 'SUMMER10'

    def test_validate_promo_code_invalid(self, cart_service, sample_user, db):
        """Test invalid promo code"""
        with pytest.raises(ValidationError, match="Invalid promotional code"):
            cart_service.validate_promo_code(
                promo_code='INVALID',
                user_id=sample_user.id,
                cart_total=25000
            )

    def test_validate_promo_code_not_yet_valid(self, cart_service, sample_user, db):
        """Test promo code not yet valid"""
        campaign = PromotionalCampaign(
            name='Future Sale',
            promo_code='FUTURE',
            discount_type='percentage',
            discount_value=Decimal('10.00'),
            start_date=datetime.now(UTC) + timedelta(days=1),  # Future
            is_active=True
        )
        db.session.add(campaign)
        db.session.commit()

        with pytest.raises(ValidationError, match="not yet valid"):
            cart_service.validate_promo_code(
                promo_code='FUTURE',
                user_id=sample_user.id,
                cart_total=25000
            )

    def test_validate_promo_code_expired(self, cart_service, sample_user, db):
        """Test expired promo code"""
        campaign = PromotionalCampaign(
            name='Past Sale',
            promo_code='EXPIRED',
            discount_type='percentage',
            discount_value=Decimal('10.00'),
            end_date=datetime.now(UTC) - timedelta(days=1),  # Expired
            is_active=True
        )
        db.session.add(campaign)
        db.session.commit()

        with pytest.raises(ValidationError, match="expired"):
            cart_service.validate_promo_code(
                promo_code='EXPIRED',
                user_id=sample_user.id,
                cart_total=25000
            )

    def test_validate_promo_code_usage_limit_reached(self, cart_service, sample_user, db):
        """Test promo code usage limit"""
        campaign = PromotionalCampaign(
            name='Limited Sale',
            promo_code='LIMITED',
            discount_type='percentage',
            discount_value=Decimal('10.00'),
            is_active=True,
            max_uses=10,
            times_used=10  # Already maxed out
        )
        db.session.add(campaign)
        db.session.commit()

        with pytest.raises(ValidationError, match="usage limit reached"):
            cart_service.validate_promo_code(
                promo_code='LIMITED',
                user_id=sample_user.id,
                cart_total=25000
            )

    def test_validate_promo_code_below_minimum(self, cart_service, sample_user, sample_promo_campaign, db):
        """Test promo code with cart below minimum"""
        with pytest.raises(ValidationError, match="Minimum order value"):
            cart_service.validate_promo_code(
                promo_code='SUMMER10',
                user_id=sample_user.id,
                cart_total=10000  # Below minimum of 20000
            )

    def test_validate_promo_code_per_customer_limit(self, cart_service, sample_user, sample_promo_campaign, db):
        """Test per-customer usage limit"""
        # Create 2 previous orders with this promo code
        for i in range(2):
            order = Order(
                user_id=sample_user.id,
                status=OrderStatus.DELIVERED,
                total_amount=Decimal('25000.00'),
                promo_code='SUMMER10'
            )
            db.session.add(order)
        db.session.commit()

        # Third attempt should fail
        with pytest.raises(ValidationError, match="already used this promotional code"):
            cart_service.validate_promo_code(
                promo_code='SUMMER10',
                user_id=sample_user.id,
                cart_total=25000
            )


@pytest.mark.cart
class TestQuickReorder:
    """Test quick reorder suggestions"""

    def test_get_quick_reorder_suggestions(self, cart_service, sample_user, sample_product, db):
        """Test getting reorder suggestions"""
        # Create past order
        order = Order(
            user_id=sample_user.id,
            status=OrderStatus.DELIVERED,
            total_amount=Decimal('15000.00'),
            created_at=datetime.now(UTC) - timedelta(days=7)
        )
        db.session.add(order)
        db.session.flush()

        item = OrderItem(
            order_id=order.id,
            product_id=sample_product.id,
            quantity=2,
            unit_price=Decimal('7500.00'),
            total_price=Decimal('15000.00')
        )
        db.session.add(item)
        db.session.commit()

        suggestions = cart_service.get_quick_reorder_suggestions(
            user_id=sample_user.id,
            limit=5,
            period_days=30
        )

        assert len(suggestions) == 1
        assert suggestions[0]['product_id'] == sample_product.id
        assert suggestions[0]['suggested_quantity'] == 2
        assert suggestions[0]['order_frequency'] == 1

    def test_get_quick_reorder_no_history(self, cart_service, sample_user, db):
        """Test reorder suggestions with no history"""
        suggestions = cart_service.get_quick_reorder_suggestions(
            user_id=sample_user.id
        )

        assert len(suggestions) == 0

    def test_get_quick_reorder_outside_period(self, cart_service, sample_user, sample_product, db):
        """Test reorder suggestions outside time period"""
        # Create old order outside 90-day window
        order = Order(
            user_id=sample_user.id,
            status=OrderStatus.DELIVERED,
            total_amount=Decimal('15000.00'),
            created_at=datetime.now(UTC) - timedelta(days=100)
        )
        db.session.add(order)
        db.session.flush()

        item = OrderItem(
            order_id=order.id,
            product_id=sample_product.id,
            quantity=2,
            unit_price=Decimal('7500.00'),
            total_price=Decimal('15000.00')
        )
        db.session.add(item)
        db.session.commit()

        suggestions = cart_service.get_quick_reorder_suggestions(
            user_id=sample_user.id,
            period_days=90
        )

        assert len(suggestions) == 0

    def test_get_quick_reorder_inactive_product_excluded(self, cart_service, sample_user, sample_product, db):
        """Test inactive products are excluded from suggestions"""
        # Create order
        order = Order(
            user_id=sample_user.id,
            status=OrderStatus.DELIVERED,
            total_amount=Decimal('15000.00'),
            created_at=datetime.now(UTC) - timedelta(days=7)
        )
        db.session.add(order)
        db.session.flush()

        item = OrderItem(
            order_id=order.id,
            product_id=sample_product.id,
            quantity=2,
            unit_price=Decimal('7500.00'),
            total_price=Decimal('15000.00')
        )
        db.session.add(item)
        db.session.commit()

        # Deactivate product
        sample_product.is_active = False
        db.session.commit()

        suggestions = cart_service.get_quick_reorder_suggestions(
            user_id=sample_user.id
        )

        assert len(suggestions) == 0


@pytest.mark.cart
class TestCheckoutPreparation:
    """Test checkout preparation"""

    def test_prepare_cart_for_checkout(self, cart_service, sample_user, sample_product, db):
        """Test preparing cart for checkout"""
        items = [{'product_id': sample_product.id, 'quantity': 1}]

        result = cart_service.prepare_cart_for_checkout(
            user_id=sample_user.id,
            items=items
        )

        assert result['ready_for_checkout'] is True
        assert len(result['items']) == 1
        assert result['subtotal'] > 0

    def test_prepare_cart_validation_errors(self, cart_service, sample_user, db):
        """Test checkout preparation with validation errors"""
        items = [{'product_id': 99999, 'quantity': 1}]

        with pytest.raises(ValidationError, match="Cart validation failed"):
            cart_service.prepare_cart_for_checkout(
                user_id=sample_user.id,
                items=items
            )

    def test_prepare_cart_below_minimum(self, cart_service, sample_user, db, app):
        """Test checkout preparation below minimum order"""
        with app.app_context():
            cheap_product = Product(
                name='Cheap Water',
                base_price=Decimal('1000.00'),
                stock_quantity=100,
                is_active=True
            )
            db.session.add(cheap_product)
            db.session.commit()

            items = [{'product_id': cheap_product.id, 'quantity': 1}]

            with pytest.raises(ValidationError, match="Minimum order amount"):
                cart_service.prepare_cart_for_checkout(
                    user_id=sample_user.id,
                    items=items
                )


@pytest.mark.cart
class TestPriceRuleIntegration:
    """Test price rule integration in cart"""

    def test_calculate_unit_price_with_volume_discount(self, cart_service, sample_product, db):
        """Test volume discount is applied to unit price"""
        # Create volume discount rule
        rule = PriceRule(
            product_id=sample_product.id,
            rule_type=PriceRuleType.VOLUME_DISCOUNT,
            discount_type='percentage',
            discount_value=Decimal('10.00'),
            min_quantity=5,
            is_active=True
        )
        db.session.add(rule)
        db.session.commit()

        items = [{'product_id': sample_product.id, 'quantity': 5}]

        validated, errors = cart_service.validate_cart_items(items)

        # Unit price should have discount applied
        assert len(validated) == 1
        # 10% off 15000 = 13500
        assert validated[0]['unit_price'] == 13500.00

    def test_calculate_unit_price_uses_discount_price(self, cart_service, db):
        """Test that discount_price is used when available"""
        product = Product(
            name='Discounted Water',
            base_price=Decimal('15000.00'),
            discount_price=Decimal('12000.00'),
            stock_quantity=100,
            is_active=True
        )
        db.session.add(product)
        db.session.commit()

        items = [{'product_id': product.id, 'quantity': 1}]

        validated, errors = cart_service.validate_cart_items(items)

        assert validated[0]['unit_price'] == 12000.00


@pytest.mark.cart
class TestLoyaltyPointsCalculations:
    """Test loyalty points calculations"""

    def test_calculate_loyalty_discount(self, cart_service, premium_user, db):
        """Test loyalty points discount calculation"""
        # 5 points * 100 UZS = 500 UZS discount
        discount = cart_service._calculate_loyalty_discount(
            points_used=5,
            user=premium_user,
            cart_total=10000
        )

        assert discount == 500.0

    def test_calculate_loyalty_discount_insufficient_points(self, cart_service, sample_user, db):
        """Test loyalty discount with insufficient points"""
        sample_user.loyalty_points = 2
        db.session.commit()

        with pytest.raises(ValidationError, match="Insufficient loyalty points"):
            cart_service._calculate_loyalty_discount(
                points_used=10,
                user=sample_user,
                cart_total=10000
            )

    def test_calculate_loyalty_discount_capped_at_total(self, cart_service, premium_user, db):
        """Test loyalty discount doesn't exceed cart total"""
        discount = cart_service._calculate_loyalty_discount(
            points_used=200,  # Would be 20000 UZS
            user=premium_user,
            cart_total=5000   # But cart is only 5000
        )

        assert discount == 5000.0

    def test_calculate_loyalty_points_earned_standard(self, cart_service, sample_user, db):
        """Test loyalty points earned for standard user"""
        points = cart_service._calculate_loyalty_points_earned(
            final_total=50000,  # 50 points
            user=sample_user
        )

        assert points == 50

    def test_calculate_loyalty_points_earned_premium(self, cart_service, premium_user, db):
        """Test loyalty points earned for premium user (2x)"""
        points = cart_service._calculate_loyalty_points_earned(
            final_total=50000,  # 100 points for premium (2x)
            user=premium_user
        )

        assert points == 100


@pytest.mark.cart
class TestDeliveryFeeCalculations:
    """Test delivery fee calculations"""

    def test_delivery_fee_free_above_threshold(self, cart_service, sample_user, db):
        """Test free delivery above threshold"""
        fee = cart_service._calculate_delivery_fee(
            items_subtotal=150000,  # Above threshold
            delivery_address_id=None,
            delivery_date=None,
            delivery_time_slot=None,
            user=sample_user
        )

        assert fee == 0.0

    def test_delivery_fee_premium_user(self, cart_service, premium_user, db):
        """Test premium users get free delivery"""
        fee = cart_service._calculate_delivery_fee(
            items_subtotal=10000,  # Below threshold
            delivery_address_id=None,
            delivery_date=None,
            delivery_time_slot=None,
            user=premium_user
        )

        assert fee == 0.0

    def test_delivery_fee_standard(self, cart_service, sample_user, db, app):
        """Test standard delivery fee"""
        with app.app_context():
            fee = cart_service._calculate_delivery_fee(
                items_subtotal=10000,  # Below threshold
                delivery_address_id=None,
                delivery_date=None,
                delivery_time_slot=None,
                user=sample_user
            )

            assert fee == cart_service.standard_delivery_fee
