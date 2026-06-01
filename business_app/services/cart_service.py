"""
Cart Service for the Water Business Platform
Handles shopping cart operations, price calculations, and order preparation
"""

import logging
from datetime import datetime, timedelta, UTC
from typing import List, Dict, Any, Optional, Tuple
from decimal import Decimal
from flask import current_app
from sqlalchemy import or_, func

from business_app.models.product import Product, PriceRule
from business_app.models.user import User
from business_app.models.order import Order, OrderItem
from business_app.models.cart import Cart, CartItem
from business_app.models.analytics import PromotionalCampaign
from business_app.utils.exceptions import ValidationError, NotFoundError
from shared.enums import OrderStatus
from business_app.utils.service_logging import log_service_call, log_database_query
from business_app import db

logger = logging.getLogger(__name__)


class CartService:
    """
    Service for managing shopping cart operations

    Responsibilities:
    - Cart item validation
    - Price calculations with discounts
    - Cart totals and estimates
    - Promotional code validation
    - Delivery fee calculations
    - Quick reorder suggestions
    - Cart preparation for checkout
    """

    def __init__(self, inventory_service=None):
        self.min_order_amount = current_app.config["MIN_ORDER_AMOUNT"]
        self.max_cart_items = current_app.config["MAX_CART_ITEMS"]
        # Single flat delivery fee (env-driven; 0 = free). No free-delivery threshold.
        self.standard_delivery_fee = current_app.config["DEFAULT_DELIVERY_FEE"]
        self._inventory_service = inventory_service

    @property
    def inventory_service(self):
        """Lazy-initialise inventory service if not injected for testing."""
        if self._inventory_service is None:
            from business_app.services.inventory_service import get_inventory_service

            self._inventory_service = get_inventory_service()
        return self._inventory_service

    @log_service_call(operation_type="cart_validate", track_performance=True)
    def validate_cart_items(
        self, items: List[Dict[str, Any]], user: Optional[User] = None
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """
        Validate cart items and check availability

        Args:
            items: List of cart items with product_id and quantity
            user: Optional user for personalized validation

        Returns:
            Tuple of (validated_items, error_messages)

        Raises:
            ValidationError: If cart validation fails
        """
        if not items:
            raise ValidationError("Cart cannot be empty")

        if len(items) > self.max_cart_items:
            raise ValidationError(f"Maximum {self.max_cart_items} items allowed in cart")

        validated_items = []
        errors = []

        # Track product IDs to detect duplicates
        seen_products = set()

        for idx, item in enumerate(items):
            # Validate item structure
            if "product_id" not in item or "quantity" not in item:
                errors.append(f"Item {idx + 1}: Missing product_id or quantity")
                continue

            product_id = item["product_id"]
            quantity = item["quantity"]

            # Check for duplicates
            if product_id in seen_products:
                errors.append(f"Product {product_id}: Duplicate item in cart")
                continue
            seen_products.add(product_id)

            # Validate quantity
            if not isinstance(quantity, int) or quantity < 1:
                errors.append(f"Product {product_id}: Invalid quantity")
                continue

            # Get product
            product = Product.query.filter_by(id=product_id, is_active=True).first()
            if not product:
                errors.append(f"Product {product_id}: Not found or inactive")
                continue

            # Check inventory against reservation-aware availability.
            if product.track_inventory:
                is_available, error_message = self._check_product_quantity_availability(product, quantity)
                if not is_available:
                    errors.append(f"Product {product_id} ({product.name}): {error_message}")
                    continue

            # Per-product purchase minimum.
            min_order_quantity = int(product.min_order_quantity or 1)
            if quantity < min_order_quantity:
                errors.append(
                    f"Product {product_id} ({product.name}): minimum order quantity is "
                    f"{min_order_quantity} (you ordered {quantity})"
                )
                continue

            # Calculate price
            unit_price = self._calculate_unit_price(product, quantity, user)

            validated_items.append(
                {
                    "product_id": product_id,
                    "product": product,
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "subtotal": unit_price * quantity,
                }
            )

        return validated_items, errors

    @log_service_call(operation_type="cart_estimate", track_performance=True)
    def calculate_cart_estimate(
        self,
        user_id: int,
        items: List[Dict[str, Any]],
        delivery_address_id: Optional[int] = None,
        delivery_date: Optional[str] = None,
        delivery_time_slot: Optional[str] = None,
        loyalty_points_used: int = 0,
        promo_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Calculate comprehensive cart estimate with all costs and discounts

        Args:
            user_id: User ID
            items: Cart items
            delivery_address_id: Delivery address
            delivery_date: Requested delivery date
            delivery_time_slot: Requested time slot
            loyalty_points_used: Loyalty points to apply
            promo_code: Promotional code

        Returns:
            Dictionary with complete price breakdown

        Raises:
            ValidationError: If calculation fails
            NotFoundError: If user not found
        """
        # Get user
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError(f"User with ID {user_id} not found")

        # Validate and price items
        validated_items, errors = self.validate_cart_items(items, user)
        if errors:
            raise ValidationError(f"Cart validation failed: {'; '.join(errors)}")

        # Calculate items subtotal
        items_subtotal = sum(item["subtotal"] for item in validated_items)

        # Calculate delivery fee
        delivery_fee = self._calculate_delivery_fee(
            items_subtotal, delivery_address_id, delivery_date, delivery_time_slot, user
        )

        # Calculate promotional discount
        promo_discount = 0.0
        promo_details = None
        if promo_code:
            promo_discount, promo_details = self._apply_promo_code(promo_code, items_subtotal, user_id)

        # Loyalty points are redeemed ONLY via rewards (LoyaltyReward.points_cost),
        # never converted directly to a UZS discount. No cart-level points discount.
        loyalty_discount = 0.0

        # Calculate totals
        total_discount = promo_discount + loyalty_discount
        total_before_discount = items_subtotal + delivery_fee
        final_total = total_before_discount - total_discount

        # Ensure minimum order amount (before delivery and discounts)
        if items_subtotal < self.min_order_amount:
            raise ValidationError(
                f"Minimum order amount is {self.min_order_amount} UZS. " f"Current cart total: {items_subtotal} UZS"
            )

        # Calculate potential loyalty points earned
        loyalty_points_earned = self._calculate_loyalty_points_earned(final_total, user)

        return {
            "items": [
                {
                    "product_id": item["product_id"],
                    "product_name": item["product"].name,
                    "quantity": item["quantity"],
                    "unit_price": item["unit_price"],
                    "subtotal": item["subtotal"],
                }
                for item in validated_items
            ],
            "pricing": {
                "items_subtotal": items_subtotal,
                "delivery_fee": delivery_fee,
                "promo_discount": promo_discount,
                "loyalty_discount": loyalty_discount,
                "total_discount": total_discount,
                "total_before_discount": total_before_discount,
                "final_total": final_total,
            },
            "promotional_code": promo_details,
            "loyalty": {
                "points_used": loyalty_points_used,
                "points_earned": loyalty_points_earned,
                "discount_applied": loyalty_discount,
            },
            "delivery": {
                "fee": delivery_fee,
                "is_free": delivery_fee == 0,
            },
            "validation": {
                "meets_minimum": items_subtotal >= self.min_order_amount,
                "minimum_order_amount": self.min_order_amount,
                "cart_item_count": len(validated_items),
            },
        }

    @log_service_call(operation_type="promo_validate", track_performance=True)
    def validate_promo_code(self, promo_code: str, user_id: int, cart_total: float = 0) -> Dict[str, Any]:
        """
        Validate promotional code

        Args:
            promo_code: Promotional code
            user_id: User ID
            cart_total: Current cart total

        Returns:
            Dictionary with promo code details and discount

        Raises:
            ValidationError: If promo code is invalid
        """
        promo_code = promo_code.upper().strip()

        campaign = PromotionalCampaign.query.filter_by(promo_code=promo_code, is_active=True).first()

        if not campaign:
            raise ValidationError("Invalid promotional code")

        # Check validity
        now = datetime.now(UTC)
        if campaign.start_date and campaign.start_date > now:
            raise ValidationError("Promotional code not yet valid")

        if campaign.end_date and campaign.end_date < now:
            raise ValidationError("Promotional code has expired")

        # Check usage limit
        if campaign.max_uses and campaign.times_used >= campaign.max_uses:
            raise ValidationError("Promotional code usage limit reached")

        # Check per-customer limit
        if campaign.max_uses_per_customer:
            user_usage = (
                db.session.query(func.count(Order.id))
                .filter(
                    Order.user_id == user_id,
                    Order.promo_code == promo_code,
                    Order.status.in_([OrderStatus.CONFIRMED, OrderStatus.DELIVERED]),
                )
                .scalar()
            )

            if user_usage >= campaign.max_uses_per_customer:
                raise ValidationError("You have already used this promotional code")

        # Check minimum order value
        if campaign.min_order_value and cart_total < campaign.min_order_value:
            raise ValidationError(f"Minimum order value of {campaign.min_order_value} UZS required for this code")

        # Calculate discount
        discount = self._calculate_campaign_discount(campaign, cart_total)

        return {
            "valid": True,
            "code": promo_code,
            "campaign_name": campaign.name,
            "description": campaign.description,
            "discount_type": campaign.discount_type,
            "discount_value": campaign.discount_value,
            "discount_amount": discount,
            "min_order_value": campaign.min_order_value,
            "max_discount": campaign.max_discount_amount,
        }

    @log_service_call(operation_type="quick_reorder", track_performance=True)
    @log_database_query(query_type="SELECT", entity_type="order")
    def get_quick_reorder_suggestions(
        self, user_id: int, limit: int = 5, period_days: int = 90
    ) -> List[Dict[str, Any]]:
        """
        Get quick reorder suggestions based on purchase history

        Args:
            user_id: User ID
            limit: Maximum suggestions
            period_days: Period to analyze (days)

        Returns:
            List of product suggestions with frequency data
        """
        cutoff_date = datetime.now(UTC) - timedelta(days=period_days)

        # Query frequently ordered products
        frequent_items = (
            db.session.query(
                OrderItem.product_id,
                func.sum(OrderItem.quantity).label("total_quantity"),
                func.count(OrderItem.id).label("order_count"),
                func.max(Order.created_at).label("last_ordered"),
                func.avg(OrderItem.unit_price).label("avg_price"),
            )
            .join(Order)
            .filter(
                Order.user_id == user_id,
                Order.created_at >= cutoff_date,
                Order.status.in_([OrderStatus.DELIVERED, OrderStatus.CONFIRMED]),
            )
            .group_by(OrderItem.product_id)
            .order_by(func.count(OrderItem.id).desc(), func.sum(OrderItem.quantity).desc())
            .limit(limit)
            .all()
        )

        suggestions = []
        for item in frequent_items:
            product = Product.query.filter_by(id=item.product_id, is_active=True).first()

            if product:
                # Calculate suggested quantity (average per order)
                suggested_quantity = max(1, min(int(item.total_quantity / item.order_count), 10))  # Cap at 10

                suggestions.append(
                    {
                        "product_id": product.id,
                        "product_name": product.name,
                        "current_price": float(product.base_price),
                        "suggested_quantity": suggested_quantity,
                        "order_frequency": item.order_count,
                        "total_ordered": item.total_quantity,
                        "last_ordered": item.last_ordered.isoformat() if item.last_ordered else None,
                        "in_stock": not product.track_inventory or product.stock_quantity > 0,
                        "stock_quantity": product.stock_quantity if product.track_inventory else None,
                    }
                )

        return suggestions

    @log_service_call(operation_type="cart_prepare", track_performance=True)
    def prepare_cart_for_checkout(self, user_id: int, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Prepare cart for checkout with final validation

        Args:
            user_id: User ID
            items: Cart items

        Returns:
            Prepared cart data ready for order creation

        Raises:
            ValidationError: If cart cannot be checked out
        """
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError(f"User with ID {user_id} not found")

        # Final validation
        validated_items, errors = self.validate_cart_items(items, user)
        if errors:
            raise ValidationError(f"Cart validation failed: {'; '.join(errors)}")

        # Check minimum order amount
        items_subtotal = sum(item["subtotal"] for item in validated_items)
        if items_subtotal < self.min_order_amount:
            raise ValidationError(f"Minimum order amount is {self.min_order_amount} UZS")

        return {"items": validated_items, "subtotal": items_subtotal, "ready_for_checkout": True, "warnings": []}

    def get_cart_by_user_id(self, user_id: int) -> Optional["Cart"]:
        """Retrieve cart for a given user"""
        return Cart.query.filter_by(user_id=user_id).first()

    def get_cart_details(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Return cart payload enriched with effective pricing for each line item."""
        cart = self.get_cart_by_user_id(user_id)
        if not cart:
            return None

        cart_payload = cart.to_dict()
        summary = self.get_cart_summary(user_id)
        pricing_by_product_id = {item["product_id"]: item for item in summary.get("items", [])}

        enriched_items = []
        for cart_item in cart_payload.get("cart_items", []):
            product_id = cart_item.get("product_id")
            pricing = pricing_by_product_id.get(product_id)
            if not pricing:
                enriched_items.append(cart_item)
                continue

            product_payload = cart_item.get("product") or {}
            product_payload["current_price"] = float(pricing.get("unit_price", 0) or 0)
            cart_item["product"] = product_payload
            cart_item["unit_price"] = float(pricing.get("unit_price", 0) or 0)
            cart_item["total_price"] = float(pricing.get("total_price", 0) or 0)
            cart_item["in_stock"] = bool(pricing.get("in_stock", True))
            cart_item["stock_quantity"] = pricing.get("stock_quantity")
            cart_item["available_quantity"] = pricing.get("available_quantity")
            cart_item["reserved_quantity"] = pricing.get("reserved_quantity")
            enriched_items.append(cart_item)

        cart_payload["cart_items"] = enriched_items
        cart_payload["item_count"] = int(summary.get("item_count", 0) or 0)
        cart_payload["subtotal"] = float(summary.get("subtotal", 0) or 0)
        cart_payload["estimated_delivery_fee"] = float(summary.get("estimated_delivery_fee", 0) or 0)
        cart_payload["estimated_total"] = float(summary.get("estimated_total", 0) or 0)
        from business_app.services.cash_collection_service import CashCollectionService

        prepaid_balance = float(CashCollectionService().get_customer_prepaid_balance(user_id))
        potential_applied_amount = min(prepaid_balance, float(cart_payload.get("estimated_total") or 0))
        cart_payload["cod_prepayment"] = {
            "available_balance": prepaid_balance,
            "potential_applied_amount": potential_applied_amount,
            "estimated_payable_after_prepayment": max(
                0.0,
                float(cart_payload.get("estimated_total") or 0) - potential_applied_amount,
            ),
        }
        return cart_payload

    def add_item_to_cart(self, user_id: int, product_id: int, quantity: int) -> "Cart":
        """Add item to user's cart"""
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError(f"User with ID {user_id} not found")

        if not isinstance(quantity, int) or quantity <= 0:
            raise ValidationError("Quantity must be a positive integer")

        product = Product.query.filter_by(id=product_id, is_active=True).first()
        if not product:
            raise NotFoundError(f"Product {product_id} not found or inactive")

        cart = self.get_cart_by_user_id(user_id)
        cart_item = None
        if cart:
            cart_item = CartItem.query.filter_by(cart_id=cart.id, product_id=product_id).first()
        requested_quantity = quantity + (cart_item.quantity if cart_item else 0)
        self._ensure_product_quantity_available(product, requested_quantity)

        if not cart:
            cart = Cart(user_id=user_id)
            db.session.add(cart)
            db.session.commit()

        # Check if item already in cart
        if cart_item is None:
            cart_item = CartItem.query.filter_by(cart_id=cart.id, product_id=product_id).first()
        if cart_item:
            cart_item.quantity += quantity
        else:
            cart_item = CartItem(cart_id=cart.id, product_id=product_id, quantity=quantity)
            db.session.add(cart_item)

        db.session.commit()
        return cart

    def update_item_quantity(self, user_id: int, product_id: int, quantity: int) -> "Cart":
        """Update quantity of an item in user's cart"""
        cart = self.get_cart_by_user_id(user_id)
        if not cart:
            raise NotFoundError("Cart not found for user")

        cart_item: CartItem = CartItem.query.filter_by(cart_id=cart.id, product_id=product_id).first()
        if not cart_item:
            raise NotFoundError("Item not found in cart")

        if not isinstance(quantity, int):
            raise ValidationError("Quantity must be an integer")

        if quantity <= 0:
            db.session.delete(cart_item)
        else:
            product = Product.query.filter_by(id=product_id, is_active=True).first()
            if not product:
                raise NotFoundError(f"Product {product_id} not found or inactive")
            self._ensure_product_quantity_available(product, quantity)
            cart_item.quantity = quantity

        db.session.commit()
        return cart

    def remove_item_from_cart(self, user_id: int, product_id: int) -> "Cart":
        """Remove item from user's cart"""
        cart = self.get_cart_by_user_id(user_id)
        if not cart:
            raise NotFoundError("Cart not found for user")

        cart_item: CartItem = CartItem.query.filter_by(cart_id=cart.id, product_id=product_id).first()
        if not cart_item:
            raise NotFoundError("Item not found in cart")

        db.session.delete(cart_item)
        db.session.commit()
        return cart

    def clear_cart(self, user_id: int) -> None:
        """Clear all items from user's cart"""
        cart = self.get_cart_by_user_id(user_id)
        if cart:
            CartItem.query.filter_by(cart_id=cart.id).delete()
            db.session.commit()

    @log_service_call(operation_type="cart_sync", track_performance=True)
    def sync_cart_from_local(self, user_id: int, local_cart_items: List[Dict[str, Any]]) -> Optional["Cart"]:
        """
        Sync localStorage cart to database on user login
        Merges local cart with existing database cart

        Args:
            user_id: User ID
            local_cart_items: List of cart items from localStorage
                              Format: [{'product_id': int, 'quantity': int}, ...]

        Returns:
            Updated Cart object
        """
        if not local_cart_items:
            # Just return existing cart if no local items
            return self.get_cart_by_user_id(user_id)

        # Get or create cart
        cart = self.get_cart_by_user_id(user_id)
        if not cart:
            cart = Cart(user_id=user_id)
            db.session.add(cart)
            db.session.flush()

        # Get existing cart items
        existing_items = {item.product_id: item for item in cart.cart_items}

        # Merge local cart items
        for local_item in local_cart_items:
            product_id = local_item.get("product_id")
            quantity = local_item.get("quantity", 1)

            if not product_id or quantity <= 0:
                continue

            # Verify product exists and is active
            product = Product.query.filter_by(id=product_id, is_active=True).first()
            if not product:
                logger.warning(f"Skipping invalid product {product_id} in cart sync")
                continue

            if product_id in existing_items:
                # Update existing item - use max quantity from both sources
                existing_items[product_id].quantity = max(existing_items[product_id].quantity, quantity)
            else:
                # Add new item
                cart_item = CartItem(cart_id=cart.id, product_id=product_id, quantity=quantity)
                db.session.add(cart_item)

        db.session.commit()

        # Refresh to get updated cart_items relationship
        db.session.refresh(cart)

        logger.info(f"Synced {len(local_cart_items)} local items to cart for user {user_id}")
        return cart

    @log_service_call(operation_type="cart_summary", track_performance=True)
    def get_cart_summary(self, user_id: int) -> Dict[str, Any]:
        """
        Get cart with calculated totals and item details

        Args:
            user_id: User ID

        Returns:
            Dictionary with cart summary including totals
        """
        cart = self.get_cart_by_user_id(user_id)

        if not cart or not cart.cart_items:
            return {
                "cart_id": None,
                "items": [],
                "item_count": 0,
                "subtotal": 0,
                "estimated_delivery_fee": self.standard_delivery_fee,
                "estimated_total": self.standard_delivery_fee,
            }

        user = User.query.get(user_id)
        items_with_details = []
        subtotal = 0

        for cart_item in cart.cart_items:
            product = cart_item.product
            if not product or not product.is_active:
                continue

            unit_price = self._calculate_unit_price(product, cart_item.quantity, user)
            item_total = unit_price * cart_item.quantity
            subtotal += item_total
            product_images = product.images or []
            product_image = product_images[0] if isinstance(product_images, list) and product_images else None
            in_stock = True
            available_quantity = None
            reserved_quantity = None

            if product.track_inventory:
                availability_result = self.inventory_service.check_product_availability(
                    product.id,
                    cart_item.quantity,
                )
                in_stock = availability_result.is_available
                available_quantity = availability_result.available_quantity
                reserved_quantity = availability_result.reserved_quantity

            items_with_details.append(
                {
                    "cart_item_id": cart_item.id,
                    "product_id": product.id,
                    "product_name": product.name,
                    "product_image": product_image,
                    "quantity": cart_item.quantity,
                    "unit_price": unit_price,
                    "total_price": item_total,
                    "in_stock": in_stock,
                    "stock_quantity": product.stock_quantity if product.track_inventory else None,
                    "available_quantity": available_quantity,
                    "reserved_quantity": reserved_quantity,
                }
            )

        # Calculate delivery fee
        delivery_fee = self._calculate_delivery_fee(subtotal, None, None, None, user)
        estimated_total = subtotal + delivery_fee

        return {
            "cart_id": cart.id,
            "items": items_with_details,
            "item_count": sum(item["quantity"] for item in items_with_details),
            "subtotal": subtotal,
            "estimated_delivery_fee": delivery_fee,
            "estimated_total": estimated_total,
        }

    # Private helper methods

    def _calculate_unit_price(self, product: Product, quantity: int, user: Optional[User]) -> float:
        """Calculate unit price with volume discounts"""
        base_price = float(product.discount_price if product.discount_price else product.base_price)
        effective_price = base_price

        # Check for volume-based price rules
        price_rule = self._get_best_price_rule(product.id, quantity, user)
        if price_rule:
            discount = self._calculate_rule_discount(price_rule, base_price)
            effective_price = max(0, base_price - discount)

        # Contract pricing overrides fallback product/rule pricing for entity users.
        user_id = getattr(user, "id", None)
        if user_id:
            try:
                from business_app.utils.service_factory import get_corporate_contract_service

                resolution = get_corporate_contract_service().resolve_contract_pricing_for_user_product(
                    user_id=user_id,
                    product_id=product.id,
                    fallback_price=Decimal(str(effective_price)),
                )
                return float(resolution["unit_price"])
            except ValidationError:
                raise
            except Exception as exc:
                logger.warning(
                    "Failed to resolve contract price for cart user_id=%s product_id=%s: %s",
                    user_id,
                    product.id,
                    exc,
                )

        return effective_price

    def _check_product_quantity_availability(
        self,
        product: Product,
        requested_quantity: int,
    ) -> Tuple[bool, str]:
        """Return availability verdict and user-facing error for inventory-tracked products."""
        if not product.track_inventory:
            return True, ""

        result = self.inventory_service.check_product_availability(product.id, requested_quantity)
        if result.is_available:
            return True, ""

        if result.reason == "Insufficient stock":
            return (
                False,
                f"Only {result.available_quantity} available (reserved: {result.reserved_quantity}), "
                f"requested {requested_quantity}",
            )
        return False, result.reason or "Unavailable"

    def _ensure_product_quantity_available(self, product: Product, requested_quantity: int) -> None:
        """Raise validation error when requested quantity exceeds reservation-aware availability."""
        is_available, error_message = self._check_product_quantity_availability(product, requested_quantity)
        if not is_available:
            raise ValidationError(f"Product {product.id} ({product.name}): {error_message}")

    def _get_best_price_rule(self, product_id: int, quantity: int, user: Optional[User]) -> Optional[PriceRule]:
        """Get best applicable price rule for product"""
        query = PriceRule.query.filter_by(product_id=product_id, is_active=True)

        # Filter by validity dates
        now = datetime.now(UTC)
        query = query.filter(
            or_(PriceRule.valid_from == None, PriceRule.valid_from <= now),
            or_(PriceRule.valid_until == None, PriceRule.valid_until >= now),
        )

        # Filter by quantity
        query = query.filter(
            PriceRule.min_quantity <= quantity, or_(PriceRule.max_quantity == None, PriceRule.max_quantity >= quantity)
        )

        # Filter by customer type
        if user:
            customer_type = "vip" if getattr(user, "is_premium", False) else "regular"
            query = query.filter(or_(PriceRule.customer_type == None, PriceRule.customer_type == customer_type))

        # Get rule with highest discount
        rules = query.all()
        return max(rules, key=lambda r: r.discount_value) if rules else None

    def _calculate_rule_discount(self, rule: PriceRule, base_price: float) -> float:
        """Calculate discount from price rule"""
        if rule.discount_type == "percentage":
            return base_price * (float(rule.discount_value) / 100)
        else:  # fixed
            return float(rule.discount_value)

    def _calculate_delivery_fee(
        self,
        items_subtotal: float,
        delivery_address_id: Optional[int],
        delivery_date: Optional[str],
        delivery_time_slot: Optional[str],
        user: Optional[User],
    ) -> float:
        """Calculate delivery fee via DeliveryService (single source of truth)"""
        from business_app.services.delivery_service import DeliveryService

        delivery_service = DeliveryService()
        # DeliveryService handles free-delivery threshold internally;
        # premium-user override is cart-specific for now.
        if user and getattr(user, "is_premium", False):
            return 0.0
        # Use 0,0 coordinates since we don't resolve the address here;
        # DeliveryService currently returns 0 (free delivery campaign).
        return float(delivery_service.calculate_delivery_fee(0, 0, int(items_subtotal)))

    def _apply_promo_code(
        self, promo_code: str, cart_total: float, user_id: int
    ) -> Tuple[float, Optional[Dict[str, Any]]]:
        """Apply promotional code and return discount"""
        try:
            promo_details = self.validate_promo_code(promo_code, user_id, cart_total)
            return promo_details["discount_amount"], promo_details
        except ValidationError as e:
            logger.warning(f"Promo code validation failed: {e}")
            return 0.0, None

    def _calculate_campaign_discount(self, campaign: PromotionalCampaign, cart_total: float) -> float:
        """Calculate discount from campaign"""
        if campaign.discount_type == "percentage":
            discount = cart_total * (float(campaign.discount_value) / 100)
        else:  # fixed
            discount = float(campaign.discount_value)

        # Apply max discount limit
        if campaign.max_discount_amount:
            discount = min(discount, float(campaign.max_discount_amount))

        return discount

    def _calculate_loyalty_points_earned(self, final_total: float, user: User) -> int:
        """
        Calculate loyalty points to be earned.

        Uses LoyaltyService.calculate_points_for_purchase() for proper
        program-aware and tier-based point calculation.
        """
        if not user or not user.id:
            return 0

        try:
            from .loyalty_service import LoyaltyService

            loyalty_service = LoyaltyService()
            return loyalty_service.calculate_points_for_purchase(user.id, int(final_total))
        except Exception as e:
            logger.warning(f"Failed to calculate loyalty points for user {user.id}: {e}")
            # Fallback to simple calculation if service fails
            return max(0, int(final_total / 100))


# Singleton instance
_cart_service = None


def get_cart_service() -> CartService:
    """Get or create CartService singleton instance"""
    global _cart_service
    if _cart_service is None:
        _cart_service = CartService()
    return _cart_service


# Export
__all__ = ["CartService", "get_cart_service"]
