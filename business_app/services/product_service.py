"""
Product Service for the Water Business Platform
Handles all product-related business logic including querying, filtering, pricing, and analytics
"""
import logging
from datetime import datetime, timedelta, UTC
from typing import List, Dict, Any, Optional, Tuple
from decimal import Decimal
from flask import current_app
from sqlalchemy import and_, or_, func, desc

from business_app.models.product import Product, ProductCategory, PriceRule
from business_app.models.review import Review
from business_app.models.user import User
from business_app.models.order import Order, OrderItem
from business_app.utils.exceptions import ValidationError, NotFoundError
from business_app.utils.constants import PriceRuleType
from business_app.utils.service_logging import (
    log_service_call, log_business_event, log_database_query
)
from business_app import db

logger = logging.getLogger(__name__)


class ProductService:
    """
    Service for managing products, categories, pricing, and product-related business logic

    Responsibilities:
    - Product querying with complex filters
    - Dynamic pricing calculation
    - Product recommendations
    - Search and autocomplete
    - Category management
    - Inventory awareness
    - Analytics tracking
    """

    def __init__(self):
        self.default_per_page = 20
        self.max_per_page = 100
        self.cache_ttl = 300  # 5 minutes

    @log_service_call(operation_type='product_query', track_performance=True)
    @log_database_query(query_type='SELECT', entity_type='product')
    def get_products_with_filters(
        self,
        page: int = 1,
        per_page: int = 20,
        category_id: Optional[int] = None,
        search: Optional[str] = None,
        sort_by: str = 'name',
        sort_order: str = 'asc',
        is_featured: Optional[bool] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        in_stock_only: bool = False,
        current_user: Optional[User] = None,
        language: str = 'uz'
    ) -> Tuple[List[Product], dict]:
        """
        Get products with advanced filtering and pagination

        Args:
            page: Page number (1-indexed)
            per_page: Items per page
            category_id: Filter by category
            search: Search term (name, description, SKU)
            sort_by: Field to sort by (name, price, rating, popularity)
            sort_order: Sort direction (asc, desc)
            is_featured: Filter featured products
            min_price: Minimum price filter
            max_price: Maximum price filter
            in_stock_only: Only show in-stock products
            current_user: User for personalized pricing
            language: Language for translations

        Returns:
            Tuple of (products list, pagination metadata)

        Raises:
            ValidationError: If filter parameters are invalid
        """
        # Validate pagination
        per_page = min(per_page, self.max_per_page)
        if page < 1:
            raise ValidationError("Page number must be >= 1")
        if per_page < 1:
            raise ValidationError("Per page must be >= 1")

        # Build base query
        query = Product.query.filter_by(is_active=True)

        # Apply filters
        if category_id:
            query = query.filter_by(category_id=category_id)

        if search:
            search_term = f"%{search.strip()}%"
            query = query.filter(or_(
                Product.name.ilike(search_term),
                Product.description.ilike(search_term),
                Product.sku.ilike(search_term)
            ))

        if is_featured is not None:
            query = query.filter_by(is_featured=is_featured)

        if min_price is not None:
            query = query.filter(Product.base_price >= min_price)

        if max_price is not None:
            query = query.filter(Product.base_price <= max_price)

        if in_stock_only:
            query = query.filter(
                or_(
                    Product.track_inventory == False,
                    Product.stock_quantity > 0
                )
            )

        # Apply sorting
        order_field = self._get_sort_field(sort_by)
        if sort_order == 'desc':
            order_field = order_field.desc()

        query = query.order_by(order_field)

        # Execute paginated query
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Track analytics if search was performed
        if search:
            self._track_search_analytics(search, len(pagination.items), current_user)

        # Prepare metadata
        filters_applied = {
            'category_id': category_id,
            'search': search,
            'is_featured': is_featured,
            'min_price': min_price,
            'max_price': max_price,
            'in_stock_only': in_stock_only,
            'sort_by': sort_by,
            'sort_order': sort_order
        }

        metadata = {
            'filters': {k: v for k, v in filters_applied.items() if v is not None},
            'total_results': pagination.total
        }

        return pagination.items, pagination.total, page, per_page, metadata

    @log_service_call(operation_type='product_fetch', track_performance=True)
    def get_product_by_id(
        self,
        product_id: int,
        current_user_id: Optional[int] = None,
        language: str = 'uz'
    ) -> Product:
        """
        Get product by ID with user-specific pricing

        Args:
            product_id: Product ID
            current_user: User for personalized pricing
            language: Language for translations

        Returns:
            Product object

        Raises:
            NotFoundError: If product not found
        """
        product = Product.query.filter_by(id=product_id, is_active=True).first()

        if not product:
            raise NotFoundError(f"Product with ID {product_id} not found")

        # Track product view
        self._track_product_view(product_id, current_user_id)

        return product

    @log_service_call(operation_type='pricing', track_performance=True)
    def calculate_product_price(
        self,
        product_id: int,
        quantity: int = 1,
        user: Optional[User] = None,
        promo_code: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Calculate product price with all applicable discounts

        Args:
            product_id: Product ID
            quantity: Order quantity
            user: User for personalized pricing
            promo_code: Promotional code

        Returns:
            Dictionary with pricing breakdown

        Raises:
            NotFoundError: If product not found
            ValidationError: If quantity is invalid
        """
        if quantity < 1:
            raise ValidationError("Quantity must be >= 1")

        product = Product.query.get(product_id)
        if not product:
            raise NotFoundError(f"Product with ID {product_id} not found")

        # Base price
        base_price = float(product.base_price)
        discount_price = float(product.discount_price) if product.discount_price else None

        # Start with best available price
        unit_price = discount_price if discount_price else base_price

        # Apply price rules (volume discounts, customer type discounts, etc.)
        price_rule_discount = self._calculate_price_rule_discount(
            product, quantity, user
        )

        # Apply promotional code discount
        promo_discount = 0.0
        if promo_code:
            promo_discount = self._calculate_promo_discount(
                product, quantity, promo_code, user
            )

        # Calculate final prices
        total_discount = price_rule_discount + promo_discount
        final_unit_price = max(unit_price - total_discount, 0)
        subtotal = final_unit_price * quantity

        return {
            'product_id': product_id,
            'quantity': quantity,
            'base_price': base_price,
            'discount_price': discount_price,
            'price_rule_discount': price_rule_discount,
            'promo_discount': promo_discount,
            'total_discount': total_discount,
            'unit_price': final_unit_price,
            'subtotal': subtotal,
            'savings': (base_price - final_unit_price) * quantity
        }

    @log_service_call(operation_type='category_query', track_performance=True)
    def get_categories(
        self,
        include_inactive: bool = False,
        language: str = 'uz'
    ) -> List[ProductCategory]:
        """
        Get all product categories

        Args:
            include_inactive: Include inactive categories
            language: Language for translations

        Returns:
            List of ProductCategory objects
        """
        query = ProductCategory.query

        if not include_inactive:
            query = query.filter_by(is_active=True)

        categories = query.order_by(
            ProductCategory.sort_order,
            ProductCategory.name
        ).all()

        return categories

    @log_service_call(operation_type='category_fetch', track_performance=True)
    def get_category_by_id(
        self,
        category_id: int,
        language: str = 'uz'
    ) -> ProductCategory:
        """
        Get category by ID

        Args:
            category_id: Category ID
            language: Language for translations

        Returns:
            ProductCategory object

        Raises:
            NotFoundError: If category not found
        """
        category = ProductCategory.query.filter_by(
            id=category_id,
            is_active=True
        ).first()

        if not category:
            raise NotFoundError(f"Category with ID {category_id} not found")

        return category

    @log_service_call(operation_type='search_suggestions', track_performance=True)
    def get_search_suggestions(
        self,
        query: str,
        limit: int = 10,
        language: str = 'uz'
    ) -> List[Dict[str, Any]]:
        """
        Get search suggestions/autocomplete for products

        Args:
            query: Search query
            limit: Maximum suggestions to return
            language: Language for translations

        Returns:
            List of product suggestions
        """
        if not query or len(query) < 2:
            return []

        search_term = f"%{query.strip()}%"

        products = Product.query.filter(
            Product.is_active == True,
            or_(
                Product.name.ilike(search_term),
                Product.sku.ilike(search_term)
            )
        ).order_by(
            Product.is_featured.desc(),
            Product.name
        ).limit(limit).all()

        suggestions = []
        for product in products:
            suggestions.append({
                'id': product.id,
                'name': product.get_translated('name', language),
                'sku': product.sku,
                'price': float(product.base_price),
                'image': product.images[0] if product.images else None
            })

        return suggestions

    @log_service_call(operation_type='featured_products', track_performance=True)
    def get_featured_products(
        self,
        limit: int = 10,
        language: str = 'uz'
    ) -> List[Product]:
        """
        Get featured products

        Args:
            limit: Maximum products to return
            language: Language for translations

        Returns:
            List of featured Product objects
        """
        products = Product.query.filter_by(
            is_active=True,
            is_featured=True
        ).order_by(
            Product.created_at.desc()
        ).limit(limit).all()

        return products

    @log_service_call(operation_type='popular_products', track_performance=True)
    def get_popular_products(
        self,
        period_days: int = 30,
        limit: int = 10,
        language: str = 'uz'
    ) -> List[Dict[str, Any]]:
        """
        Get popular products based on sales

        Args:
            period_days: Period to analyze (days)
            limit: Maximum products to return
            language: Language for translations

        Returns:
            List of popular products with sales data
        """
        start_date = datetime.now(UTC) - timedelta(days=period_days)

        # Query top-selling products
        popular = db.session.query(
            OrderItem.product_id,
            func.sum(OrderItem.quantity).label('total_quantity'),
            func.count(OrderItem.id).label('order_count'),
            func.sum(OrderItem.total_price).label('total_revenue')
        ).join(Order).filter(
            Order.created_at >= start_date,
            Order.status.in_(['confirmed', 'delivered'])
        ).group_by(
            OrderItem.product_id
        ).order_by(
            desc('total_quantity')
        ).limit(limit).all()

        results = []
        for item in popular:
            product = Product.query.get(item.product_id)
            if product and product.is_active:
                results.append({
                    'product': product,
                    'total_sold': item.total_quantity,
                    'order_count': item.order_count,
                    'revenue': float(item.total_revenue)
                })

        return results

    # Private helper methods

    def _get_sort_field(self, sort_by: str):
        """Get SQLAlchemy field for sorting"""
        sort_fields = {
            'name': Product.name,
            'price': Product.base_price,
            'rating': Product.id,  # Will be average_rating when review system is integrated
            'popularity': Product.id,  # Will be total_sold when integrated
            'created': Product.created_at
        }
        return sort_fields.get(sort_by, Product.name)

    def _calculate_price_rule_discount(
        self,
        product: Product,
        quantity: int,
        user: Optional[User]
    ) -> float:
        """Calculate discount from price rules"""
        # Query applicable price rules
        query = PriceRule.query.filter_by(
            product_id=product.id,
            is_active=True
        )

        # Filter by validity dates
        now = datetime.now(UTC)
        query = query.filter(
            or_(
                PriceRule.valid_from == None,
                PriceRule.valid_from <= now
            ),
            or_(
                PriceRule.valid_until == None,
                PriceRule.valid_until >= now
            )
        )

        # Filter by quantity
        query = query.filter(
            PriceRule.min_quantity <= quantity,
            or_(
                PriceRule.max_quantity == None,
                PriceRule.max_quantity >= quantity
            )
        )

        # Filter by customer type if user provided
        if user:
            customer_type = 'vip' if getattr(user, 'is_premium', False) else 'regular'
            query = query.filter(
                or_(
                    PriceRule.customer_type == None,
                    PriceRule.customer_type == customer_type
                )
            )

        # Get best discount
        price_rules = query.all()
        best_discount = 0.0

        for rule in price_rules:
            discount_value = float(rule.discount_value)

            if rule.discount_type == 'percentage':
                discount = float(product.base_price) * (discount_value / 100)
            else:  # fixed
                discount = discount_value

            best_discount = max(best_discount, discount)

        return best_discount

    def _calculate_promo_discount(
        self,
        product: Product,
        quantity: int,
        promo_code: str,
        user: Optional[User]
    ) -> float:
        """Calculate discount from promotional code"""
        # This will integrate with promotional campaign system
        # For now, return 0 - will be implemented when needed
        return 0.0

    def _track_product_view(
        self,
        product_id: int,
        user_id: Optional[int]
    ) -> None:
        """Track product view for analytics"""
        try:
            from business_app.utils.service_factory import get_analytics_service
            analytics = get_analytics_service()

            analytics.track_product_view(
                product_id=product_id,
                user_id=user_id
            )
        except Exception as e:
            logger.warning(f"Failed to track product view: {e}")

    def _track_search_analytics(
        self,
        search_term: str,
        results_count: int,
        user: Optional[User]
    ) -> None:
        """Track search for analytics"""
        try:
            from business_app.utils.service_factory import get_analytics_service
            analytics = get_analytics_service()

            analytics.track_search(
                search_term=search_term,
                results_count=results_count,
                user_id=user.id if user else None
            )
        except Exception as e:
            logger.warning(f"Failed to track search analytics: {e}")


# Singleton instance
_product_service = None


def get_product_service() -> ProductService:
    """Get or create ProductService singleton instance"""
    global _product_service
    if _product_service is None:
        _product_service = ProductService()
    return _product_service


# Export
__all__ = ['ProductService', 'get_product_service']
