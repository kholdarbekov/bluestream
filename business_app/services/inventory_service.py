"""
Comprehensive Inventory Management Service for the BlueStream Platform
Provides proper inventory checks, reservations, and audit logging
"""

import logging
from typing import Dict, List, Any, Optional
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from enum import Enum


from business_app import db
from business_app.models.product import Product
from business_app.models.order import Order
from business_app.utils.exceptions import NotFoundError
from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity
from shared.redis_keyspace import RedisKeyspace

logger = logging.getLogger(__name__)


class InventoryOperationType(Enum):
    """Types of inventory operations"""

    RESERVE = "reserve"
    RELEASE = "release"
    CONFIRM_RESERVATION = "confirm_reservation"
    MANUAL_ADJUSTMENT = "manual_adjustment"
    RESTOCK = "restock"
    DAMAGE_WRITE_OFF = "damage_write_off"
    RETURN_RESTOCK = "return_restock"


@dataclass
class InventoryReservation:
    """Represents an inventory reservation"""

    product_id: int
    quantity: int
    order_id: Optional[int] = None
    user_id: Optional[int] = None
    expires_at: Optional[datetime] = None
    reason: str = "order_placement"


@dataclass
class InventoryCheckResult:
    """Result of inventory availability check"""

    product_id: int
    requested_quantity: int
    available_quantity: int
    reserved_quantity: int
    is_available: bool
    reason: str = ""


class InventoryService:
    """Comprehensive inventory management service"""

    def __init__(self):
        self.redis_client = None
        self._reservation_ttl = None
        self._min_stock_level_percentage = None

        # Redis client and config will be initialized lazily

    @property
    def reservation_ttl(self):
        """Get reservation TTL with lazy initialization"""
        if self._reservation_ttl is None:
            try:
                from flask import current_app

                self._reservation_ttl = current_app.config.get("INVENTORY_RESERVATION_TTL", 1800)  # 30 minutes
            except RuntimeError:
                self._reservation_ttl = 1800  # Default fallback
        return self._reservation_ttl

    @property
    def min_stock_level_percentage(self):
        """Get low stock threshold with lazy initialization"""
        if self._min_stock_level_percentage is None:
            try:
                from flask import current_app

                self._min_stock_level_percentage = current_app.config.get("min_stock_level_PERCENTAGE", 10)
            except RuntimeError:
                self._min_stock_level_percentage = 10  # Default fallback
        return self._min_stock_level_percentage

    def _get_redis_client(self):
        """Get Redis client with lazy initialization"""
        if self.redis_client is None:
            try:
                from business_app.utils.service_factory import get_auth_service

                auth_service = get_auth_service()
                self.redis_client = auth_service.redis_client
            except Exception as e:
                logger.warning(f"Redis not available for inventory reservations: {e}")
        return self.redis_client

    def check_product_availability(
        self, product_id: int, requested_quantity: int, exclude_order_id: Optional[int] = None
    ) -> InventoryCheckResult:
        """
        Check if a product has sufficient inventory for the requested quantity

        Args:
            product_id: Product ID to check
            requested_quantity: Quantity requested
            exclude_order_id: Order ID to exclude from reservation calculations

        Returns:
            InventoryCheckResult with availability information
        """
        product = Product.query.get(product_id)
        if not product:
            raise NotFoundError(f"Product {product_id} not found")

        if not product.is_active:
            return InventoryCheckResult(
                product_id=product_id,
                requested_quantity=requested_quantity,
                available_quantity=0,
                reserved_quantity=0,
                is_available=False,
                reason="Product is not active",
            )

        # Get current stock
        current_stock = product.stock_quantity or 0

        # Calculate reserved quantities
        reserved_quantity = self._get_reserved_quantity(product_id, exclude_order_id)

        # Calculate available quantity
        available_quantity = max(0, current_stock - reserved_quantity)

        # Check if request can be fulfilled
        is_available = available_quantity >= requested_quantity

        # Check minimum stock levels
        if is_available and product.min_stock_level:
            remaining_after_request = available_quantity - requested_quantity
            if remaining_after_request < product.min_stock_level:
                is_available = False
                reason = f"Would breach minimum stock level ({product.min_stock_level})"
            else:
                reason = "Available"
        else:
            reason = "Insufficient stock" if not is_available else "Available"

        return InventoryCheckResult(
            product_id=product_id,
            requested_quantity=requested_quantity,
            available_quantity=available_quantity,
            reserved_quantity=reserved_quantity,
            is_available=is_available,
            reason=reason,
        )

    def check_multiple_products_availability(
        self, items: List[Dict[str, Any]], exclude_order_id: Optional[int] = None
    ) -> List[InventoryCheckResult]:
        """
        Check availability for multiple products at once

        Args:
            items: List of items with 'product_id' and 'quantity' keys
            exclude_order_id: Order ID to exclude from calculations

        Returns:
            List of InventoryCheckResult objects
        """
        results = []

        # Group by product to handle multiple items of same product
        product_quantities = {}
        for item in items:
            product_id = item["product_id"]
            quantity = item["quantity"]
            product_quantities[product_id] = product_quantities.get(product_id, 0) + quantity

        for product_id, total_quantity in product_quantities.items():
            result = self.check_product_availability(product_id, total_quantity, exclude_order_id)
            results.append(result)

        return results

    def reserve_inventory(
        self, order_id: int, items: List[Dict[str, Any]], user_id: Optional[int] = None, ttl: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Reserve inventory for an order

        Args:
            order_id: Order ID
            items: List of items to reserve
            user_id: User making the reservation
            ttl: Time to live for reservations in seconds

        Returns:
            Dictionary with reservation details
        """
        if not self.redis_client:
            self.redis_client = self._get_redis_client()

        ttl = ttl or self.reservation_ttl
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)

        # Check availability for all items first
        availability_results = self.check_multiple_products_availability(items)

        unavailable_items = [result for result in availability_results if not result.is_available]
        if unavailable_items:
            reasons = [f"Product {r.product_id}: {r.reason}" for r in unavailable_items]
            return {"success": False, "reason": "Insufficient inventory", "details": reasons}

        # Reserve all items
        reservations = []
        try:
            for item in items:
                product_id = item["product_id"]
                quantity = item["quantity"]

                reservation_key = RedisKeyspace.inventory_reservation(order_id, product_id)
                details_key = RedisKeyspace.reservation_details(order_id, product_id)
                reservation_data = {
                    "order_id": order_id,
                    "product_id": product_id,
                    "quantity": quantity,
                    "user_id": user_id,
                    "expires_at": expires_at.isoformat(),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }

                redis_client = self._get_redis_client()
                if redis_client:
                    redis_client.setex(reservation_key, ttl, str(quantity))
                    redis_client.hset(details_key, mapping=reservation_data)
                    redis_client.expire(details_key, ttl)

                reservations.append(reservation_data)

                # Log reservation
                audit_logger.log_event(
                    event_type=AuditEventType.INVENTORY_UPDATED,
                    action="inventory_reserved",
                    severity=AuditSeverity.MEDIUM,
                    resource_type="product_inventory",
                    resource_id=str(product_id),
                    description=f"Reserved {quantity} units for order {order_id}",
                    additional_data={
                        "order_id": order_id,
                        "product_id": product_id,
                        "quantity": quantity,
                        "expires_at": expires_at.isoformat(),
                    },
                )

            logger.info(f"Reserved inventory for order {order_id}: {len(reservations)} items")
            return {"success": True, "reservations": reservations, "expires_at": expires_at.isoformat()}

        except Exception as e:
            # Rollback reservations on error
            self.release_reservations(order_id)
            logger.exception("Failed to reserve inventory for order %s", order_id)
            return {"success": False, "reason": f"Reservation failed: {str(e)}"}

    def release_reservations(self, order_id: int) -> Dict[str, Any]:
        """
        Release all inventory reservations for an order

        Args:
            order_id: Order ID to release reservations for

        Returns:
            Dictionary with release details
        """
        if not self.redis_client:
            return {"success": False, "reason": "Reservation system not available"}

        try:
            released_items = []

            # Find all reservation keys for this order
            redis_client = self._get_redis_client()
            if redis_client:
                reservation_pattern = RedisKeyspace.inventory_reservation_pattern(order_id)
                reservation_keys = redis_client.keys(reservation_pattern)

                details_pattern = RedisKeyspace.reservation_details_pattern(order_id)
                details_keys = redis_client.keys(details_pattern)

                # Delete all reservations
                if reservation_keys:
                    redis_client.delete(*reservation_keys)
                if details_keys:
                    redis_client.delete(*details_keys)

            # Extract product IDs for logging
            for key in reservation_keys:
                key_str = key.decode("utf-8") if isinstance(key, bytes) else key
                parts = key_str.split(":")
                if len(parts) >= 3:
                    product_id = parts[2]
                    released_items.append(product_id)

            # Log release
            if released_items:
                audit_logger.log_event(
                    event_type=AuditEventType.INVENTORY_UPDATED,
                    action="inventory_reservation_released",
                    severity=AuditSeverity.MEDIUM,
                    resource_type="order_inventory",
                    resource_id=str(order_id),
                    description=f"Released inventory reservations for order {order_id}",
                    additional_data={"order_id": order_id, "released_products": released_items},
                )

            logger.info(f"Released {len(released_items)} inventory reservations for order {order_id}")
            return {"success": True, "released_items": len(released_items)}

        except Exception as e:
            logger.exception("Failed to release reservations for order %s", order_id)
            return {"success": False, "reason": f"Release failed: {str(e)}"}

    def confirm_reservations(self, order_id: int) -> Dict[str, Any]:
        """
        Confirm reservations by actually reducing stock quantities

        Args:
            order_id: Order ID to confirm reservations for

        Returns:
            Dictionary with confirmation details
        """
        try:
            order = Order.query.get(order_id)
            if not order:
                raise NotFoundError(f"Order {order_id} not found")

            confirmed_items = []

            # Process inventory updates without explicit transaction management
            # The calling code (payment processing) manages the transaction
            for item in order.order_items:
                # Lock the product row for update
                product = db.session.query(Product).with_for_update().filter_by(id=item.product_id).first()

                if not product:
                    logger.error(f"Product {item.product_id} not found during confirmation")
                    continue

                old_stock = product.stock_quantity

                # Reduce stock
                if product.stock_quantity is not None:
                    new_stock = max(0, product.stock_quantity - item.quantity)
                    product.stock_quantity = new_stock
                    product.updated_at = datetime.now(timezone.utc)

                    # Update stock status
                    if new_stock == 0:
                        product.is_in_stock = False

                    confirmed_items.append(
                        {
                            "product_id": product.id,
                            "product_name": product.name,
                            "quantity_reduced": item.quantity,
                            "old_stock": old_stock,
                            "new_stock": new_stock,
                        }
                    )

                    # Check for low stock
                    if product.min_stock_level and new_stock <= product.min_stock_level:
                        self._send_low_stock_alert(product)

            # Release Redis reservations (this doesn't affect DB transaction)
            self.release_reservations(order_id)

            logger.info(f"Confirmed inventory for order {order_id}: {len(confirmed_items)} items")

            # Log inventory confirmation after all items processed
            # Note: audit_logger.log_event does its own commit, so we call it outside the main flow
            for item_info in confirmed_items:
                audit_logger.log_event(
                    event_type=AuditEventType.INVENTORY_UPDATED,
                    action="inventory_confirmed_and_reduced",
                    severity=AuditSeverity.HIGH,
                    resource_type="product_inventory",
                    resource_id=str(item_info["product_id"]),
                    description=f"Confirmed reservation and reduced stock for order {order_id}",
                    old_values={"stock_quantity": item_info["old_stock"]},
                    new_values={"stock_quantity": item_info["new_stock"]},
                    additional_data={
                        "order_id": order_id,
                        "quantity_reduced": item_info["quantity_reduced"],
                        "product_name": item_info["product_name"],
                    },
                )

            return {"success": True, "confirmed_items": confirmed_items}

        except Exception as e:
            logger.exception("Failed to confirm inventory for order %s", order_id)
            return {"success": False, "reason": f"Confirmation failed: {str(e)}"}

    def adjust_inventory(
        self,
        product_id: int,
        quantity_change: int,
        operation_type: InventoryOperationType,
        reason: str,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Manually adjust inventory levels

        Args:
            product_id: Product ID
            quantity_change: Change in quantity (positive for increase, negative for decrease)
            operation_type: Type of operation
            reason: Reason for adjustment
            user_id: User making the adjustment

        Returns:
            Dictionary with adjustment details
        """
        try:
            product = db.session.query(Product).with_for_update().filter_by(id=product_id).first()

            if not product:
                raise NotFoundError(f"Product {product_id} not found")

            old_stock = product.stock_quantity or 0
            new_stock = max(0, old_stock + quantity_change)

            product.stock_quantity = new_stock
            product.updated_at = datetime.now(timezone.utc)
            product.is_in_stock = new_stock > 0

            # Commit the inventory change
            db.session.commit()

            # Log adjustment after commit (audit logger does its own commit)
            audit_logger.log_event(
                event_type=AuditEventType.INVENTORY_UPDATED,
                action=f"inventory_{operation_type.value}",
                severity=AuditSeverity.HIGH,
                resource_type="product_inventory",
                resource_id=str(product_id),
                description=f"Manual inventory adjustment: {reason}",
                old_values={"stock_quantity": old_stock},
                new_values={"stock_quantity": new_stock},
                additional_data={
                    "quantity_change": quantity_change,
                    "operation_type": operation_type.value,
                    "reason": reason,
                    "adjusted_by_user_id": user_id,
                },
            )

            logger.info(f"Adjusted inventory for product {product_id}: {old_stock} → {new_stock} ({reason})")
            return {
                "success": True,
                "product_id": product_id,
                "old_stock": old_stock,
                "new_stock": new_stock,
                "quantity_change": quantity_change,
            }

        except Exception as e:
            db.session.rollback()
            logger.exception("Failed to adjust inventory for product %s", product_id)
            return {"success": False, "reason": f"Adjustment failed: {str(e)}"}

    def get_inventory_status(self, product_id: int) -> Dict[str, Any]:
        """
        Get comprehensive inventory status for a product

        Args:
            product_id: Product ID

        Returns:
            Dictionary with inventory status
        """
        product = Product.query.get(product_id)
        if not product:
            raise NotFoundError(f"Product {product_id} not found")

        current_stock = product.stock_quantity or 0
        reserved_quantity = self._get_reserved_quantity(product_id)
        available_quantity = max(0, current_stock - reserved_quantity)

        # Calculate stock status
        is_in_stock = available_quantity > 0
        is_low_stock = product.min_stock_level and available_quantity <= product.min_stock_level
        is_out_of_stock = available_quantity == 0

        return {
            "product_id": product_id,
            "product_name": product.name,
            "current_stock": current_stock,
            "reserved_quantity": reserved_quantity,
            "available_quantity": available_quantity,
            "min_stock_level": product.min_stock_level,
            "max_stock_level": product.max_stock_level,
            "is_in_stock": is_in_stock,
            "is_low_stock": is_low_stock,
            "is_out_of_stock": is_out_of_stock,
            "last_updated": product.updated_at.isoformat() if product.updated_at else None,
        }

    def _get_reserved_quantity(self, product_id: int, exclude_order_id: Optional[int] = None) -> int:
        """Get total reserved quantity for a product"""
        if not self.redis_client:
            self.redis_client = self._get_redis_client()
        if not self.redis_client:
            return 0

        try:
            pattern = RedisKeyspace.inventory_reservation_by_product_pattern(product_id)
            keys = self.redis_client.keys(pattern)

            total_reserved = 0
            for key in keys:
                key_str = key.decode("utf-8") if isinstance(key, bytes) else key

                # Skip if this is the excluded order
                if exclude_order_id:
                    parts = key_str.split(":")
                    if len(parts) >= 2 and parts[1] == str(exclude_order_id):
                        continue

                quantity = self.redis_client.get(key)
                if quantity:
                    total_reserved += int(quantity.decode("utf-8") if isinstance(quantity, bytes) else quantity)

            return total_reserved

        except Exception:
            logger.exception("Failed to get reserved quantity for product %s", product_id)
            return 0

    def _send_low_stock_alert(self, product: Product):
        """Send low stock alert for a product"""
        try:
            from business_app.tasks.inventory_tasks import send_low_stock_alert_task

            send_low_stock_alert_task.delay(product.id)
        except Exception:
            logger.exception("Failed to send low stock alert for product %s", product.id)

    def cleanup_expired_reservations(self) -> Dict[str, Any]:
        """
        Cleanup expired reservations (usually called by scheduled task)

        Returns:
            Dictionary with cleanup results
        """
        if not self.redis_client:
            return {"success": False, "reason": "Redis not available"}

        try:
            cleaned_count = 0

            # Redis TTL should handle most cleanup, but this is a safety net
            pattern = RedisKeyspace.all_reservation_details_pattern()
            keys = self.redis_client.keys(pattern)

            current_time = datetime.now(timezone.utc)

            for key in keys:
                try:
                    details = self.redis_client.hgetall(key)
                    if details and "expires_at" in details:
                        expires_at = datetime.fromisoformat(details["expires_at"].decode("utf-8"))
                        if current_time > expires_at:
                            # Extract order_id and product_id from key
                            key_str = key.decode("utf-8") if isinstance(key, bytes) else key
                            parts = key_str.split(":")
                            if len(parts) >= 3:
                                order_id, product_id = parts[1], parts[2]

                                # Delete expired reservation
                                self.redis_client.delete(key)
                                self.redis_client.delete(RedisKeyspace.inventory_reservation(order_id, product_id))
                                cleaned_count += 1

                except Exception:
                    logger.exception("Error cleaning reservation key %s", key)
                    continue

            logger.info(f"Cleaned up {cleaned_count} expired inventory reservations")
            return {"success": True, "cleaned_count": cleaned_count}

        except Exception as e:
            logger.exception("Failed to cleanup expired reservations")
            return {"success": False, "reason": f"Cleanup failed: {str(e)}"}


# Global inventory service instance - lazy initialization
_inventory_service = None


def get_inventory_service():
    """Get the global inventory service instance with lazy initialization"""
    global _inventory_service
    if _inventory_service is None:
        _inventory_service = InventoryService()
    return _inventory_service
