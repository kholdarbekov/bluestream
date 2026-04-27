"""
Query optimization utilities for SQLAlchemy eager loading and performance improvements
"""

from typing import Dict, List, Optional, Any, Type
from sqlalchemy.orm import joinedload, selectinload, Query
from sqlalchemy import func, case


class EagerLoadingStrategy:
    """Central strategy for eager loading commonly accessed relationships"""

    @classmethod
    def _build_strategies(cls):
        """Build loading strategies dynamically to avoid import issues"""
        # Start with conservative strategies to avoid relationship issues
        strategies = {
            "user_admin_list": [
                # Minimal loading for admin list views - just basic user data
            ],
            "delivery_with_order": [
                # Basic delivery with order - will populate safely at runtime
            ],
            "payment_with_order": [
                # Basic payment with order - will populate safely at runtime
            ],
            "product_with_category": [
                # Basic product with category - will populate safely at runtime
            ],
        }

        # Try to build more advanced strategies if models are available
        try:
            pass

            # Only add strategies for relationships we know exist
            strategies.update(
                {
                    "order_with_items": [
                        # Will be populated dynamically to avoid relationship issues
                    ],
                    "order_admin_detail": [
                        # Will be populated dynamically to avoid relationship issues
                    ],
                }
            )

        except (ImportError, AttributeError):
            # Models not available or relationships not configured properly
            pass

        return strategies

    @classmethod
    def get_strategy(cls, strategy_name: str) -> List[Any]:
        """Get a predefined loading strategy by name"""
        if not hasattr(cls, "_strategies_cache"):
            cls._strategies_cache = cls._build_strategies()
        return cls._strategies_cache.get(strategy_name, [])

    @classmethod
    def apply_strategy(cls, query: Query, strategy_name: str) -> Query:
        """Apply a loading strategy to a query"""
        strategies = cls.get_strategy(strategy_name)

        # If strategies list is empty, try to build them dynamically
        if not strategies and strategy_name in (
            "order_with_items",
            "order_admin_detail",
            "delivery_with_order",
            "payment_with_order",
        ):
            strategies = cls._build_runtime_strategy(strategy_name, query)

        for strategy in strategies:
            try:
                query = query.options(strategy)
            except Exception:
                # Skip strategies that fail to apply
                continue
        return query

    @classmethod
    def _build_runtime_strategy(cls, strategy_name: str, query: Query) -> List[Any]:
        """Build eager-loading strategies at runtime based on the query model.

        ARCH-009: admin list endpoints serialize several relationships per row
        (user, payment, order_items, delivery, delivery_address, fiscalization).
        Each missing eager-load translates to an extra query per row; with the
        default 50-per-page admin lists that's ~400 extra round-trips. We load
        everything the admin/delivery/payment serializers touch, using
        ``selectinload`` for one-to-many (avoids cartesian blow-up) and
        ``joinedload`` for to-one relationships.
        """
        from business_app.models.order import Order, OrderItem
        from business_app.models.payment import Payment
        from business_app.models.delivery import Delivery

        strategies: List[Any] = []

        try:
            model_class = query.column_descriptions[0]["type"]

            if strategy_name == "order_with_items" and model_class is Order:
                strategies.extend(
                    [
                        joinedload(Order.user),
                        selectinload(Order.order_items).joinedload(OrderItem.product),
                        joinedload(Order.delivery_address),
                        joinedload(Order.payment),
                    ]
                )
            elif strategy_name == "order_admin_detail" and model_class is Order:
                strategies.extend(
                    [
                        joinedload(Order.user),
                        joinedload(Order.delivery_address),
                        joinedload(Order.payment).joinedload(Payment.fiscalization),
                        selectinload(Order.order_items).joinedload(OrderItem.product),
                        joinedload(Order.delivery).joinedload(Delivery.delivery_person),
                    ]
                )
            elif strategy_name == "delivery_with_order" and model_class is Delivery:
                strategies.extend(
                    [
                        joinedload(Delivery.order).joinedload(Order.user),
                        joinedload(Delivery.order).joinedload(Order.delivery_address),
                        joinedload(Delivery.delivery_person),
                    ]
                )
            elif strategy_name == "payment_with_order" and model_class is Payment:
                strategies.extend(
                    [
                        joinedload(Payment.order).joinedload(Order.user),
                        joinedload(Payment.order).joinedload(Order.delivery_address),
                        joinedload(Payment.fiscalization),
                    ]
                )

        except Exception:
            # Never let eager-load inference break the request. Worst case we
            # fall back to lazy loading (the existing behaviour).
            pass

        return strategies

    @classmethod
    def apply_custom_loading(cls, query: Query, *load_options) -> Query:
        """Apply custom loading options to a query"""
        for option in load_options:
            query = query.options(option)
        return query


class QueryOptimizer:
    """Advanced query optimization utilities"""

    @staticmethod
    def optimize_list_query(
        model_class: Type,
        query: Query,
        eager_load_strategy: Optional[str] = None,
        custom_loads: Optional[List[Any]] = None,
        include_counts: bool = False,
    ) -> Query:
        """
        Optimize a list query with proper eager loading

        Args:
            model_class: The main model being queried
            query: The base query to optimize
            eager_load_strategy: Predefined strategy name
            custom_loads: Custom loading options
            include_counts: Whether to include relationship counts

        Returns:
            Optimized query
        """
        # Apply eager loading strategy
        if eager_load_strategy:
            query = EagerLoadingStrategy.apply_strategy(query, eager_load_strategy)

        # Apply custom loading options
        if custom_loads:
            query = EagerLoadingStrategy.apply_custom_loading(query, *custom_loads)

        # Add counts if requested (useful for admin interfaces)
        if include_counts:
            query = QueryOptimizer._add_relationship_counts(query, model_class)

        return query

    @staticmethod
    def _add_relationship_counts(query: Query, model_class: Type) -> Query:
        """Add relationship counts to avoid separate count queries"""
        # This would add subqueries for counts, specific to each model
        # Implementation depends on specific model relationships
        try:
            from business_app.models.user import User
            from business_app.models.order import Order

            if model_class == User:
                # Add order count, delivery count, etc.
                pass
            elif model_class == Order:
                # Add item count, payment count, etc.
                pass
        except ImportError:
            pass

        return query

    @staticmethod
    def optimize_detail_query(model_class: Type, query: Query, eager_load_strategy: Optional[str] = None) -> Query:
        """
        Optimize a detail query (single record) with comprehensive eager loading

        Args:
            model_class: The main model being queried
            query: The base query to optimize
            eager_load_strategy: Predefined strategy name

        Returns:
            Optimized query
        """
        if eager_load_strategy:
            query = EagerLoadingStrategy.apply_strategy(query, eager_load_strategy)
        else:
            # Apply default comprehensive loading for detail views
            try:
                from business_app.models.user import User
                from business_app.models.order import Order
                from business_app.models.delivery import Delivery

                if model_class == User:
                    query = EagerLoadingStrategy.apply_strategy(query, "user_admin_list")
                elif model_class == Order:
                    query = EagerLoadingStrategy.apply_strategy(query, "order_admin_detail")
                elif model_class == Delivery:
                    query = EagerLoadingStrategy.apply_strategy(query, "delivery_with_order")
            except ImportError:
                pass

        return query


class PaginationOptimizer:
    """Optimize paginated queries to reduce N+1 problems"""

    @staticmethod
    def optimize_paginated_query(
        query: Query, page: int, per_page: int, eager_load_strategy: Optional[str] = None, order_by=None
    ) -> Any:  # Returns pagination object
        """
        Optimize a paginated query with proper eager loading

        Args:
            query: Base query to paginate
            page: Page number
            per_page: Items per page
            eager_load_strategy: Eager loading strategy to apply
            order_by: Ordering clause

        Returns:
            Optimized pagination object
        """
        # Apply eager loading before pagination to avoid N+1 on results
        if eager_load_strategy:
            query = EagerLoadingStrategy.apply_strategy(query, eager_load_strategy)

        # Apply ordering if provided
        if order_by is not None:
            query = query.order_by(order_by)

        # Paginate with optimized query
        return query.paginate(page=page, per_page=per_page, error_out=False)


class AggregationOptimizer:
    """Optimize aggregation queries to avoid N+1 problems"""

    @staticmethod
    def get_user_statistics(user_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """
        Get user statistics efficiently for multiple users

        Args:
            user_ids: List of user IDs to get statistics for

        Returns:
            Dictionary mapping user_id to statistics
        """
        try:
            from business_app import db
            from business_app.models.order import Order
            from business_app.models.delivery import Delivery

            # Get order counts and totals in one query
            order_stats = (
                db.session.query(
                    Order.user_id,
                    func.count(Order.id).label("order_count"),
                    func.sum(Order.total_amount).label("total_spent"),
                    func.max(Order.created_at).label("last_order_date"),
                )
                .filter(Order.user_id.in_(user_ids))
                .group_by(Order.user_id)
                .all()
            )

            # Get delivery counts in one query
            delivery_stats = (
                db.session.query(
                    Order.user_id,
                    func.count(Delivery.id).label("delivery_count"),
                    func.sum(case((Delivery.status == "delivered", 1), else_=0)).label("successful_deliveries"),
                )
                .join(Delivery, Order.id == Delivery.order_id)
                .filter(Order.user_id.in_(user_ids))
                .group_by(Order.user_id)
                .all()
            )
        except ImportError:
            # Return empty stats if imports fail
            return {
                user_id: {
                    "order_count": 0,
                    "total_spent": 0,
                    "last_order_date": None,
                    "delivery_count": 0,
                    "successful_deliveries": 0,
                }
                for user_id in user_ids
            }

        # Combine statistics
        stats = {}
        for user_id in user_ids:
            stats[user_id] = {
                "order_count": 0,
                "total_spent": 0,
                "last_order_date": None,
                "delivery_count": 0,
                "successful_deliveries": 0,
            }

        # Apply order statistics
        for stat in order_stats:
            stats[stat.user_id].update(
                {
                    "order_count": stat.order_count,
                    "total_spent": float(stat.total_spent or 0),
                    "last_order_date": stat.last_order_date,
                }
            )

        # Apply delivery statistics
        for stat in delivery_stats:
            stats[stat.user_id].update(
                {"delivery_count": stat.delivery_count, "successful_deliveries": stat.successful_deliveries}
            )

        return stats

    @staticmethod
    def get_order_statistics(order_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """
        Get order statistics efficiently for multiple orders

        Args:
            order_ids: List of order IDs to get statistics for

        Returns:
            Dictionary mapping order_id to statistics
        """
        try:
            from business_app import db
            from business_app.models.order import OrderItem
            from business_app.models.payment import Payment

            # Get item counts and totals in one query
            item_stats = (
                db.session.query(
                    OrderItem.order_id,
                    func.count(OrderItem.id).label("item_count"),
                    func.sum(OrderItem.quantity).label("total_quantity"),
                )
                .filter(OrderItem.order_id.in_(order_ids))
                .group_by(OrderItem.order_id)
                .all()
            )

            # Get payment information
            payment_stats = (
                db.session.query(
                    Payment.order_id,
                    func.count(Payment.id).label("payment_count"),
                    func.max(Payment.created_at).label("last_payment_date"),
                )
                .filter(Payment.order_id.in_(order_ids))
                .group_by(Payment.order_id)
                .all()
            )
        except ImportError:
            # Return empty stats if imports fail
            return {
                order_id: {"item_count": 0, "total_quantity": 0, "payment_count": 0, "last_payment_date": None}
                for order_id in order_ids
            }

        # Combine statistics
        stats = {}
        for order_id in order_ids:
            stats[order_id] = {"item_count": 0, "total_quantity": 0, "payment_count": 0, "last_payment_date": None}

        # Apply item statistics
        for stat in item_stats:
            stats[stat.order_id].update({"item_count": stat.item_count, "total_quantity": stat.total_quantity})

        # Apply payment statistics
        for stat in payment_stats:
            stats[stat.order_id].update(
                {"payment_count": stat.payment_count, "last_payment_date": stat.last_payment_date}
            )

        return stats


class QueryProfiler:
    """Utility for profiling and monitoring query performance"""

    @staticmethod
    def profile_query(query: Query, description: str = "") -> Any:
        """
        Profile a query and log performance metrics

        Args:
            query: Query to profile
            description: Description for logging

        Returns:
            Query result
        """
        import time
        import logging

        logger = logging.getLogger(__name__)

        # Log the SQL query
        logger.debug(f"Executing query: {description}")
        logger.debug(f"SQL: {query.statement.compile(compile_kwargs={'literal_binds': True})}")

        start_time = time.time()
        result = query.all()
        execution_time = time.time() - start_time

        logger.info(f"Query '{description}' executed in {execution_time:.4f}s, returned {len(result)} rows")

        # Warn about potentially slow queries
        if execution_time > 1.0:
            logger.warning(f"Slow query detected: {description} took {execution_time:.4f}s")

        return result


# Convenience functions for common use cases
def get_users_with_stats(query: Query) -> Query:
    """Get users with minimal stats for admin list view"""
    return EagerLoadingStrategy.apply_strategy(query, "user_admin_list")


def get_orders_with_details(query: Query) -> Query:
    """Get orders with full details for admin view"""
    return EagerLoadingStrategy.apply_strategy(query, "order_admin_detail")


def get_deliveries_optimized(query: Query) -> Query:
    """Get deliveries with related order and user data"""
    return EagerLoadingStrategy.apply_strategy(query, "delivery_with_order")


def get_payments_optimized(query: Query) -> Query:
    """Get payments with related order data"""
    return EagerLoadingStrategy.apply_strategy(query, "payment_with_order")


def get_products_optimized(query: Query) -> Query:
    """Get products with category and review data"""
    return EagerLoadingStrategy.apply_strategy(query, "product_with_category")
