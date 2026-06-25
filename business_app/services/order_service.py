"""
Order service for the Water Business Platform
"""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Dict, Any, Optional, Tuple
from flask import current_app
from sqlalchemy import desc, func
from sqlalchemy.orm import joinedload

from business_app.utils.service_logging import log_service_call, log_business_event

logger = logging.getLogger(__name__)

from business_app.models.order import Order, OrderItem  # noqa: E402
from business_app.models.product import Product  # noqa: E402
from business_app.models.user import User, UserAddress  # noqa: E402
from business_app.utils.exceptions import ValidationError, NotFoundError, ConflictError, ForbiddenError  # noqa: E402
from shared.enums import (  # noqa: E402
    OrderStatus,
    PaymentStatus,
    DeliveryStatus,
    PaymentMethod,
    SubscriptionFrequency,
    UserRole,
)
from shared.status_transitions import is_valid_order_transition  # noqa: E402
from business_app.models.order import OrderStatusHistory  # noqa: E402
from business_app.models.delivery import DeliveryStatusHistory  # noqa: E402
from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity  # noqa: E402
from business_app.utils.state_validators import (  # noqa: E402
    assert_order_address_for_status,
    assert_order_creator_for_source,
)
from business_app import db  # noqa: E402


class OrderService:
    """Service for managing orders"""

    def __init__(self, inventory_service=None, delivery_service=None):
        self.min_order_amount = current_app.config["MIN_ORDER_AMOUNT"]
        self.max_order_items = current_app.config["MAX_ORDER_ITEMS"]
        self._inventory_service = inventory_service
        self._delivery_service = delivery_service

    @property
    def inventory_service(self):
        """Lazy-initialise inventory service if not injected via constructor."""
        if self._inventory_service is None:
            from business_app.services.inventory_service import get_inventory_service

            self._inventory_service = get_inventory_service()
        return self._inventory_service

    @property
    def delivery_service(self):
        """Lazy-initialise delivery service if not injected via constructor."""
        if self._delivery_service is None:
            from business_app.services.delivery_service import DeliveryService

            self._delivery_service = DeliveryService()
        return self._delivery_service

    @log_service_call(operation_type="order", track_performance=True)
    @log_business_event(event_type="created", entity_type="order")
    def create_order(self, user_id: int, order_data: Dict[str, Any], *, bypass_cod_check: bool = False) -> Order:
        """
        Create a new order

        Args:
            user_id: ID of the user placing the order
            order_data: Order information including items, delivery address, etc.
            bypass_cod_check: When True, skip the COD active-debt cap. Reserved
                for the PSP-failure rescue path (see
                ``rescue_order_after_psp_failure``). Never expose this to the
                normal create_order API path.

        Returns:
            Created Order object

        Raises:
            ValidationError: If order data is invalid
            NotFoundError: If user or products not found
        """
        # Validate order data
        self._validate_order_data(order_data)

        # A promo code and a redeemed loyalty reward are mutually exclusive on a
        # single order. Guard up front (before any DB work) so the conflict is
        # reported before points are touched or rows are created.
        reward_id = order_data.get("reward_id")
        if reward_id and order_data.get("promo_code"):
            raise ValidationError("A promo code and a loyalty reward cannot be used on the same order")

        # Get user
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")

        # A redeemed loyalty reward may only be applied by a loyalty-eligible
        # user. Reject early — before any reward lookup or DB writes.
        if reward_id:
            from business_app.services.loyalty_service import LoyaltyService

            if not LoyaltyService.is_user_loyalty_eligible(user):
                raise ValidationError("Loyalty rewards are not available for this account")

        # Require phone number for placing orders
        if not user.phone:
            raise ValidationError("Phone number is required to place an order. Please update your profile.")

        # Entity users must have an entity_subtype assigned (workplace vs grocery
        # store) before they can place orders. Legacy entity rows are NULL until
        # an admin sets it.
        if user.is_entity_user and user.normalized_entity_subtype is None:
            raise ValidationError("Entity subtype must be assigned by admin before placing orders")

        # Validate and calculate order items
        items_data = order_data["items"]
        order_items, subtotal = self._process_order_items(items_data, user_id=user_id)

        # Calculate delivery fee via DeliveryService (single source of truth).
        delivery_address = order_data["delivery_address"]
        delivery_fee = self.delivery_service.calculate_delivery_fee(
            delivery_address.get("latitude", 0),
            delivery_address.get("longitude", 0),
            subtotal,
        )

        # Calculate total
        total_amount = subtotal + delivery_fee

        # Check minimum order amount
        if total_amount < self.min_order_amount:
            raise ValidationError(f"Minimum order amount is {self.min_order_amount}")

        # Map payment method string to enum if provided
        payment_method = None
        payment_method_str = order_data.get("payment_method")
        if payment_method_str:
            # PaymentMethod is imported at module level (top of file). Do NOT
            # re-import it locally here: a local ``from shared.enums import
            # PaymentMethod`` would make the name function-local for the whole
            # method, so callers that omit ``payment_method`` (e.g. subscription
            # billing, which skips this block) crashed with UnboundLocalError at
            # the ``payment_method == PaymentMethod.CASH`` check below.
            # Loyalty points are spent only on rewards, never as a payment method —
            # 'loyalty_points'/'points' is intentionally not mapped (rejected upstream).
            payment_method_map = {
                "cash": PaymentMethod.CASH,
                "payme": PaymentMethod.PAYME,
                "click": PaymentMethod.CLICK,
                "card": PaymentMethod.CLICK,
                "business_account": PaymentMethod.BUSINESS_ACCOUNT,
            }
            payment_method = payment_method_map.get(payment_method_str)
            if payment_method == PaymentMethod.CASH and not bypass_cod_check:
                from business_app.services.cash_collection_service import CashCollectionService

                CashCollectionService().validate_customer_can_use_cod(user_id)
            if payment_method == PaymentMethod.BUSINESS_ACCOUNT:
                from business_app.services.corporate_contract_service import CorporateContractService

                CorporateContractService().validate_business_account_order(
                    user=user,
                    order_items=order_items,
                )

        # Create order
        order_source = order_data.get("order_source", "web")
        created_by_staff_id = order_data.get("created_by_staff_id")
        # ARCH-006: staff-channel orders must record the creating operator.
        assert_order_creator_for_source(
            order_source=order_source,
            created_by_staff_id=created_by_staff_id,
        )
        order = Order(
            user_id=user_id,
            status=OrderStatus.PENDING,
            subtotal=subtotal,
            delivery_fee=delivery_fee,
            total_amount=total_amount,
            delivery_address_id=delivery_address["delivery_address_id"],
            payment_method=payment_method,
            delivery_date=order_data.get("delivery_date"),
            delivery_time_slot=order_data.get("delivery_time_slot"),
            delivery_notes=order_data.get("delivery_notes"),
            is_urgent=bool(order_data.get("is_urgent", False)),
            # Points redeem only via rewards; orders never consume points directly.
            loyalty_points_used=0,
            order_source=order_source,
            created_by_staff_id=created_by_staff_id,
        )

        # ARCH-008: explicit transaction boundary covering the entire order
        # creation flow. The previous code had three separate db.session.commit()
        # calls scattered through the method, which made partial-failure recovery
        # impossible — a downstream step could fail after an earlier step had
        # already persisted, leaving orphaned rows that needed manual cleanup.
        #
        # Now: ONE transaction covers Order + OrderItems + corporate prepayment +
        # Payment row + cash credit reservation. Any step raising rolls back the
        # whole thing — no orphans.
        #
        # Cross-system compensation:
        #   - Inventory reservation writes to Redis (not DB). It runs INSIDE the
        #     transaction so we know the order.id. If a later DB step fails, the
        #     except handler explicitly calls release_reservations(order.id) to
        #     undo the Redis writes; the DB rollback handles everything else.
        #   - Audit log entries via audit_logger are emitted post-commit (audit
        #     records describe successful events; rolled-back work doesn't fire).
        from business_app.utils.transactions import atomic_transaction

        reservation_result = None
        try:
            with atomic_transaction():
                db.session.add(order)
                db.session.flush()  # Get order ID inside the transaction

                # Add order items
                for item_data in order_items:
                    order_item = OrderItem(
                        order_id=order.id,
                        product_id=item_data["product_id"],
                        contract_id=item_data.get("contract_id"),
                        contract_product_price_id=item_data.get("contract_product_price_id"),
                        quantity=item_data["quantity"],
                        unit_price=item_data["unit_price"],  # Use current price at order time
                        total_price=item_data["total_price"],
                    )
                    db.session.add(order_item)
                db.session.flush()

                # Apply a redeemed loyalty reward (discount or free product) to this
                # order, atomically within the order transaction. Adjusts
                # order.loyalty_discount / order.total_amount or injects a free item,
                # deducts points, and records a RewardRedemption. Done before the
                # payment row is created so order.total_amount is final.
                if reward_id:
                    from business_app.services.loyalty_service import LoyaltyService

                    _redemption = LoyaltyService().apply_reward_to_order(order, reward_id, commit=False)
                    db.session.flush()
                    # A free-product reward injects an OrderItem that confirm_reservations
                    # will decrement at confirmation; include it in the reservation so it
                    # is availability-checked + reserved (prevents oversell). If the free
                    # product is out of stock, reservation fails and the whole order
                    # (incl. the points deduction) rolls back atomically.
                    if _redemption is not None and getattr(_redemption, "free_product_id", None):
                        from business_app.models.loyalty import LoyaltyReward

                        _free_reward = LoyaltyReward.query.get(_redemption.reward_id)
                        _free_qty = (_free_reward.free_product_quantity or 1) if _free_reward else 1
                        items_data = items_data + [{"product_id": _redemption.free_product_id, "quantity": _free_qty}]

                # Reserve inventory (Redis-backed). Surface a failure as
                # ValidationError so the outer transaction rolls back DB state
                # alongside the Redis self-rollback in release_reservations.
                reservation_result = self.inventory_service.reserve_inventory(
                    order_id=order.id,
                    items=items_data,
                    user_id=user_id,
                )
                logger.info(f"CREATE ORDER: reservation_result: {reservation_result}")
                if not reservation_result["success"]:
                    raise ValidationError(f"Inventory reservation failed: {reservation_result['reason']}")

                # Reserve corporate prepayment bottle units (DB writes participate
                # in this transaction).
                from business_app.services.corporate_contract_service import CorporateContractService

                CorporateContractService().reserve_for_order(order.id)

                # Create the canonical payment row in the same transaction.
                # commit=False keeps the outer atomic_transaction in charge.
                from business_app.services.payment_service import PaymentService

                payment = PaymentService().initialize_order_payment(
                    order.id,
                    metadata={
                        "consume_marking_codes": bool(order_data.get("consume_marking_codes", False)),
                    },
                    commit=False,
                )

                if payment_method == PaymentMethod.CASH and payment:
                    from business_app.services.cash_collection_service import CashCollectionService

                    CashCollectionService().reserve_customer_prepaid_credit_for_payment(
                        payment,
                        actor_user_id=user_id,
                    )

            # Post-commit side effects:
            logger.info("CREATE ORDER: Order has been inserted successfully")
            db.session.refresh(order)
            audit_logger.log_event(
                event_type=AuditEventType.ORDER_CREATED,
                action="inventory_reserved_for_order",
                severity=AuditSeverity.MEDIUM,
                resource_type="order",
                resource_id=str(order.id),
                description=f"Inventory reserved for order {order.order_number}",
                additional_data={
                    "order_id": order.id,
                    "order_number": order.order_number,
                    "reservation_expires_at": reservation_result.get("expires_at") if reservation_result else None,
                    "items_count": len(items_data),
                },
            )
            logger.info("CREATE ORDER: FINISHED")

        except ValidationError:
            # DB already rolled back by atomic_transaction. Release Redis
            # reservation if we got past inventory step.
            if reservation_result and reservation_result.get("success"):
                try:
                    self.inventory_service.release_reservations(order.id)
                except Exception:
                    logger.exception("Failed to release inventory after rollback for order %s", order.id)
            raise
        except Exception as e:
            logger.exception("Order creation failed for user %s", user_id)
            if reservation_result and reservation_result.get("success"):
                try:
                    self.inventory_service.release_reservations(order.id)
                except Exception:
                    logger.exception("Failed to release inventory after rollback for order %s", order.id)
            raise ValidationError(f"Failed to create order: {str(e)}")

        # Send order confirmation notification
        # self._send_order_notification(order, 'order_created')

        # Schedule automatic confirmation if enabled
        # self._schedule_auto_confirmation(order.id)

        return order

    def get_order(self, order_id: int, user_id: int = None) -> Order:
        """Get order by ID"""
        query = Order.query.options(
            joinedload(Order.order_items).joinedload(OrderItem.product),
            joinedload(Order.payment),
            joinedload(Order.delivery_address),
            joinedload(Order.delivery),
        ).filter_by(id=order_id)

        if user_id:
            query = query.filter_by(user_id=user_id)

        order = query.first()
        if not order:
            raise NotFoundError("Order not found")

        return order

    # implement order timeline retrieval
    def get_order_timeline(self, order_id: int) -> List[Dict[str, Any]]:
        """
        Get order status history as a timeline.

        Returns a list of timeline entries in chronological order,
        starting with order creation and including all status changes.
        """
        # First, get the order itself for the creation timestamp
        order = Order.query.get(order_id)
        if not order:
            return []

        timeline = []

        # Add order creation as the first entry
        timeline.append(
            {
                "status": "created",
                "timestamp": order.created_at.isoformat() if order.created_at else None,
                "notes": None,
                "reason": None,
                "is_current": order.status == OrderStatus.PENDING,
            }
        )

        # Get status history from database
        history = (
            OrderStatusHistory.query.filter_by(order_id=order_id).order_by(OrderStatusHistory.changed_at.asc()).all()
        )

        for i, entry in enumerate(history):
            is_last = i == len(history) - 1
            timeline.append(
                {
                    "status": entry.new_status.value,
                    "timestamp": entry.changed_at.isoformat() if entry.changed_at else None,
                    "notes": entry.notes,
                    "reason": entry.reason,
                    "is_current": is_last,  # Mark the last entry as current
                }
            )

        return timeline

    def get_user_orders(
        self, user_id: int, status: OrderStatus = None, page: int = 1, per_page: int = 20
    ) -> Dict[str, Any]:
        """Get user's orders with pagination"""
        query = Order.query.options(
            joinedload(Order.order_items).joinedload(OrderItem.product),
            joinedload(Order.payment),
            joinedload(Order.delivery_address),
        ).filter_by(user_id=user_id)

        if status:
            query = query.filter_by(status=status)

        query = query.order_by(Order.created_at.desc())

        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        return {
            "orders": [self._serialize_order(order) for order in pagination.items],
            "total": pagination.total,
            "pages": pagination.pages,
            "current_page": page,
            "per_page": per_page,
            "has_next": pagination.has_next,
            "has_prev": pagination.has_prev,
        }

    def get_user_orders_paginated(
        self,
        user_id: int,
        page: int,
        per_page: int,
        status: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Get paginated user orders with optional status/date filters."""
        query = Order.query.options(
            joinedload(Order.order_items).joinedload(OrderItem.product),
            joinedload(Order.payment),
            joinedload(Order.delivery_address),
            joinedload(Order.delivery),
        ).filter_by(user_id=user_id)

        if status:
            try:
                query = query.filter(Order.status == OrderStatus(status))
            except ValueError as exc:
                raise ValidationError("Invalid status value") from exc

        if start_date:
            query = query.filter(Order.created_at >= start_date)
        if end_date:
            query = query.filter(Order.created_at <= end_date)

        pagination = query.order_by(Order.created_at.desc()).paginate(
            page=page,
            per_page=per_page,
            error_out=False,
        )
        return {
            "items": pagination.items,
            "total": pagination.total,
            "page": page,
            "per_page": per_page,
        }

    def get_order_details_for_user(self, order_id: int, user_id: int) -> Dict[str, Any]:
        """Return full order details for a user-owned order."""
        order = self.get_order(order_id, user_id=user_id)
        return {
            "order": order,
            "delivery": order.delivery,
            "timeline": self.get_order_timeline(order_id),
        }

    def get_user_and_address_for_order(
        self,
        user_id: int,
        delivery_address_id: Optional[int],
    ) -> Tuple[User, Optional[UserAddress]]:
        """Resolve and validate user and optional delivery address ownership."""
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")

        if delivery_address_id is None:
            return user, None

        address = UserAddress.query.filter_by(
            id=delivery_address_id,
            user_id=user_id,
        ).first()
        if not address:
            raise ValidationError("Invalid delivery address")
        return user, address

    def get_user_or_raise(self, user_id: int) -> User:
        """Return user by id or raise not-found error."""
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")
        return user

    def validate_user_emergency_order_access(
        self,
        user_id: int,
        daily_limit: int = 3,
    ) -> Dict[str, Any]:
        """Validate emergency-order permissions and per-day rate limit."""
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")

        role_value = user.role.value if hasattr(user.role, "value") else user.role
        staff_roles = {UserRole.ADMIN.value, UserRole.MANAGER.value, UserRole.OPERATOR.value}
        if not user.is_premium and role_value not in staff_roles:
            raise ForbiddenError("Emergency orders require premium or staff access")

        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        today_count = Order.query.filter(
            Order.user_id == user_id,
            Order.is_urgent.is_(True),
            Order.created_at >= today_start,
        ).count()
        if today_count >= daily_limit:
            raise ConflictError("Emergency order daily limit exceeded")

        return {"user": user, "today_count": today_count}

    def get_user_order_statistics(self, user_id: int, period: str = "year") -> Dict[str, Any]:
        """Get aggregated order statistics for a user."""
        now = datetime.now(timezone.utc)
        start_date: Optional[datetime]
        if period == "month":
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == "quarter":
            quarter_start_month = ((now.month - 1) // 3) * 3 + 1
            start_date = now.replace(
                month=quarter_start_month,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        elif period == "year":
            start_date = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == "all":
            start_date = None
        else:
            raise ValidationError("Invalid period value")

        query = Order.query.filter_by(user_id=user_id)
        if start_date:
            query = query.filter(Order.created_at >= start_date)

        total_orders, total_spent = query.with_entities(
            func.count(Order.id),
            func.coalesce(func.sum(Order.total_amount), 0),
        ).first()
        total_spent_value = float(total_spent or 0)

        status_rows = (
            query.with_entities(
                Order.status,
                func.count(Order.id),
            )
            .group_by(Order.status)
            .all()
        )
        status_counts = {}
        for status_value, count in status_rows:
            key = status_value.value if hasattr(status_value, "value") else str(status_value)
            status_counts[key] = count
        for enum_status in OrderStatus:
            status_counts.setdefault(enum_status.value, 0)

        top_products_query = (
            db.session.query(
                Product.name,
                func.sum(OrderItem.quantity).label("total_qty"),
            )
            .join(OrderItem, OrderItem.product_id == Product.id)
            .join(
                Order,
                Order.id == OrderItem.order_id,
            )
            .filter(Order.user_id == user_id)
        )
        if start_date:
            top_products_query = top_products_query.filter(Order.created_at >= start_date)
        top_products_rows = (
            top_products_query.group_by(
                Product.id,
                Product.name,
            )
            .order_by(desc("total_qty"))
            .limit(5)
            .all()
        )
        top_products = [{"name": name, "quantity": int(qty)} for name, qty in top_products_rows]

        monthly_spending: Dict[str, float] = {}
        monthly_samples = (
            db.session.query(
                Order.created_at,
                Order.total_amount,
            )
            .filter(Order.user_id == user_id)
            .all()
        )
        for created_at, total_amount in monthly_samples:
            if not created_at:
                continue
            month_key = created_at.strftime("%Y-%m")
            monthly_spending[month_key] = monthly_spending.get(month_key, 0.0) + float(total_amount or 0)

        # Keep only most recent 12 months to stabilize payload size.
        monthly_spending = dict(sorted(monthly_spending.items(), reverse=True)[:12])

        return {
            "period": period,
            "statistics": {
                "total_orders": int(total_orders or 0),
                "total_spent": total_spent_value,
                "average_order_value": round(total_spent_value / total_orders, 2) if total_orders else 0,
                "orders_by_status": status_counts,
                "top_products": top_products,
                "monthly_spending_trend": monthly_spending,
            },
        }

    def submit_order_feedback_for_user(
        self,
        order_id: int,
        user_id: int,
        rating: int,
        comment: Optional[str] = None,
    ) -> Order:
        """Persist feedback for a delivered user order."""
        order = self.get_order(order_id, user_id=user_id)
        if order.status != OrderStatus.DELIVERED:
            raise ConflictError("Feedback can be submitted only for delivered orders")
        if not order.delivery:
            raise ValidationError("Order has no delivery record")

        order.delivery.customer_rating = rating
        order.delivery.customer_feedback = comment
        db.session.commit()
        return order

    def repeat_order_for_user(self, order_id: int, user_id: int) -> Order:
        """Create a new order from an existing user-owned order."""
        original_order = self.get_order(order_id, user_id=user_id)
        if not original_order.order_items:
            raise ValidationError("Original order has no items")
        if not original_order.delivery_address:
            raise ValidationError("Original order has no delivery address")

        order_data = {
            "items": [
                {"product_id": item.product_id, "quantity": item.quantity} for item in original_order.order_items
            ],
            "delivery_address": {
                "delivery_address_id": original_order.delivery_address.id,
                "street": original_order.delivery_address.street_address,
                "latitude": original_order.delivery_address.latitude,
                "longitude": original_order.delivery_address.longitude,
            },
            "delivery_notes": original_order.delivery_notes,
            "payment_method": (
                original_order.payment_method.value
                if hasattr(original_order.payment_method, "value")
                else original_order.payment_method
            ),
            "order_source": "web",
        }
        return self.create_order(user_id, order_data)

    def get_order_tracking_for_user(self, order_id: int, user_id: int) -> Dict[str, Any]:
        """Get tracking payload for a user-owned order."""
        order = self.get_order(order_id, user_id=user_id)
        timeline = self.get_order_timeline(order_id)

        time_remaining = None
        if order.delivery and order.delivery.estimated_delivery_time:
            estimated_time = order.delivery.estimated_delivery_time
            if estimated_time.tzinfo is None:
                estimated_time = estimated_time.replace(tzinfo=timezone.utc)
            remaining = estimated_time - datetime.now(timezone.utc)
            if remaining.total_seconds() > 0:
                total_minutes = int(remaining.total_seconds() // 60)
                time_remaining = {
                    "hours": total_minutes // 60,
                    "minutes": total_minutes % 60,
                    "total_minutes": total_minutes,
                }

        return {
            "order": order,
            "delivery": order.delivery,
            "timeline": timeline,
            "estimated_time_remaining": time_remaining,
        }

    def perform_bulk_action(self, action: str, order_ids: List[int], actor_user_id: int) -> List[Dict[str, Any]]:
        """Perform bulk actions for admin users on selected orders."""
        actor = User.query.get(actor_user_id)
        if not actor or not actor.is_admin:
            raise ForbiddenError("Admin access required")

        valid_actions = {"confirm", "cancel", "mark_priority", "assign_delivery"}
        if action not in valid_actions:
            raise ValidationError("Invalid action")

        results: List[Dict[str, Any]] = []
        for order_id in order_ids:
            order = Order.query.get(order_id)
            if not order:
                results.append({"order_id": order_id, "success": False, "error": "Order not found"})
                continue

            try:
                if action == "confirm":
                    self.update_order_status(order.id, OrderStatus.CONFIRMED, updated_by=actor_user_id)
                elif action == "cancel":
                    self.cancel_order(order.id, reason="Bulk cancellation", actor_user_id=actor_user_id)
                elif action == "mark_priority":
                    order.is_urgent = True
                    order.updated_at = datetime.now(timezone.utc)
                    db.session.commit()
                elif action == "assign_delivery":
                    if not order.delivery:
                        from business_app.services.delivery_service import DeliveryService

                        DeliveryService().create_delivery(order.id)

                results.append({"order_id": order_id, "success": True})
            except Exception as exc:
                db.session.rollback()
                logger.exception("Bulk order action '%s' failed for order_id=%s", action, order_id)
                results.append({"order_id": order_id, "success": False, "error": str(exc)})

        return results

    def export_orders(
        self,
        format_type: str,
        filters: Dict[str, Any],
        start_date: Optional[str],
        end_date: Optional[str],
        user_id: int,
    ) -> Dict[str, Any]:
        """Build order export file and return metadata."""
        if format_type not in {"csv", "excel"}:
            raise ValidationError("Invalid format")

        query = Order.query.options(
            joinedload(Order.order_items).joinedload(OrderItem.product),
            joinedload(Order.delivery_address),
        )
        if filters.get("user_id") is not None:
            query = query.filter(Order.user_id == filters["user_id"])
        if filters.get("status"):
            try:
                query = query.filter(Order.status == OrderStatus(filters["status"]))
            except ValueError as exc:
                raise ValidationError("Invalid status value") from exc

        if start_date:
            try:
                query = query.filter(Order.created_at >= datetime.fromisoformat(start_date))
            except ValueError as exc:
                raise ValidationError("Invalid start_date format") from exc
        if end_date:
            try:
                query = query.filter(Order.created_at <= datetime.fromisoformat(end_date))
            except ValueError as exc:
                raise ValidationError("Invalid end_date format") from exc

        orders = query.order_by(Order.created_at.desc()).all()

        import csv
        import os
        import uuid

        extension = "xlsx" if format_type == "excel" else "csv"
        filename = f"orders_export_{user_id}_{uuid.uuid4().hex[:8]}.{extension}"
        filepath = os.path.join("/tmp", filename)

        with open(filepath, "w", newline="", encoding="utf-8") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(["order_id", "order_number", "status", "total_amount", "created_at"])
            for order in orders:
                status_value = order.status.value if hasattr(order.status, "value") else order.status
                writer.writerow(
                    [
                        order.id,
                        order.order_number,
                        status_value,
                        float(order.total_amount),
                        order.created_at.isoformat() if order.created_at else "",
                    ]
                )

        file_size = os.path.getsize(filepath)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        return {
            "download_url": f"/tmp/{filename}",
            "file_size": file_size,
            "expires_at": expires_at,
        }

    def create_subscription_order(
        self,
        subscription_data: Dict[str, Any],
        items_data: List[Dict[str, Any]],
    ) -> Any:
        """Create recurring subscription via subscription service."""
        frequency_value = subscription_data.get("frequency")
        try:
            frequency = SubscriptionFrequency(frequency_value)
        except ValueError as exc:
            raise ValidationError("Invalid subscription frequency") from exc

        payment_method_raw = subscription_data.get("payment_method") or PaymentMethod.CASH.value
        try:
            payment_method = PaymentMethod(payment_method_raw)
        except ValueError as exc:
            raise ValidationError("Invalid payment method") from exc

        from business_app.utils.service_factory import get_subscription_service

        payload = {
            "user_id": subscription_data["user_id"],
            "name": "Recurring water delivery",
            "description": subscription_data.get("delivery_notes") or "",
            "billing_cycle": frequency.value,
            "delivery_frequency": frequency.value,
            "delivery_address_id": subscription_data.get("delivery_address_id"),
            "payment_method": payment_method,
            "auto_payment": bool(subscription_data.get("auto_pay", True)),
            "auto_renew": True,
            "start_date": (
                datetime.fromisoformat(subscription_data["start_date"])
                if subscription_data.get("start_date")
                else datetime.now(timezone.utc)
            ),
        }

        return get_subscription_service().create_subscription(payload, items_data)

    def create_scheduled_order(self, order_data: Dict[str, Any], items_data: List[Dict[str, Any]]) -> Order:
        """Create an order scheduled for future processing."""
        user_id = order_data["user_id"]
        _, address = self.get_user_and_address_for_order(user_id, order_data.get("delivery_address_id"))
        if not address:
            raise ValidationError("Delivery address is required")

        scheduled_date = order_data.get("scheduled_date")
        if isinstance(scheduled_date, str):
            scheduled_date = datetime.fromisoformat(scheduled_date)
        if scheduled_date and scheduled_date.tzinfo is None:
            scheduled_date = scheduled_date.replace(tzinfo=timezone.utc)
        if scheduled_date and scheduled_date <= datetime.now(timezone.utc):
            raise ValidationError("Scheduled date must be in the future")

        create_payload = {
            "items": items_data,
            "delivery_address": {
                "delivery_address_id": address.id,
                "street": address.street_address,
                "latitude": address.latitude,
                "longitude": address.longitude,
            },
            "delivery_date": order_data.get("delivery_date") or (scheduled_date.date() if scheduled_date else None),
            "delivery_time_slot": order_data.get("delivery_time_slot"),
            "delivery_notes": order_data.get("delivery_notes"),
            "payment_method": order_data.get("payment_method"),
            "order_source": order_data.get("order_source", "web"),
            "is_urgent": bool(order_data.get("is_urgent", False)),
        }
        return self.create_order(user_id, create_payload)

    def update_order_status(
        self,
        order_id: int,
        new_status: OrderStatus,
        updated_by: int = None,
        notes: str = None,
        bottles_returned: int = None,
        commit: bool = True,
    ) -> Order:
        """Update order status.

        When ``commit`` is False the caller owns the transaction boundary —
        the status change and any side-effects spawned by
        ``_handle_status_change_actions`` are flushed but not committed,
        so a downstream failure rolls the whole sequence back.
        """
        order = Order.query.get(order_id)
        if not order:
            raise NotFoundError("Order not found")

        # Connect to DB status being potentially a string
        current_status = order.status
        if isinstance(current_status, str):
            try:
                # Try to convert string to Enum
                current_status = OrderStatus(current_status)
            except ValueError:
                # If invalid string, keep as is (validation will likely fail)
                pass

        # Validate status transition
        if not self._is_valid_status_transition(current_status, new_status):
            current_val = current_status.value if hasattr(current_status, "value") else str(current_status)
            new_val = new_status.value if hasattr(new_status, "value") else str(new_status)
            raise ValidationError(f"Cannot change status from {current_val} to {new_val}")

        # ARCH-006: required FKs must be present before crossing into states
        # that imply downstream fulfilment (delivery flow needs an address).
        assert_order_address_for_status(order, new_status)

        # Update order
        old_status = current_status
        order.status = new_status
        order.updated_at = datetime.now(timezone.utc)

        if updated_by:
            order.updated_by = updated_by

        # Update status-specific fields
        self._update_status_fields(order, new_status)

        # Create status history
        self._create_status_history(order_id, old_status, new_status, updated_by, notes)

        db.session.flush()

        # Handle status-specific actions (run before commit so a failure rolls
        # back the status change atomically).
        self._handle_status_change_actions(
            order, new_status, bottles_returned=bottles_returned, updated_by=updated_by, commit=commit
        )

        if commit:
            db.session.commit()
            # Notification dispatch happens only after a successful commit so
            # rolled-back transitions do not fire stale notifications.
            self._send_order_notification(order, f"status_changed_{new_status.value}")

        return order

    @log_service_call(operation_type="order", track_performance=True)
    @log_business_event(event_type="cancelled", entity_type="order")
    def cancel_order(
        self,
        order_id: int,
        user_id: int = None,
        reason: str = None,
        *,
        actor_user_id: int = None,
        process_payment_refund: bool = True,
    ) -> Order:
        """Cancel an order"""
        from shared.enums import PaymentMethod

        order = self.get_order(order_id, user_id)

        # Ensure status is Enum for logic checks
        current_status = order.status
        if isinstance(current_status, str):
            try:
                current_status = OrderStatus(current_status)
            except ValueError:
                pass

        actor_id = actor_user_id if actor_user_id is not None else user_id

        # An order can be cancelled at any stage *except* once it is delivered
        # (or already cancelled). In-transit / out-for-delivery orders are now
        # cancellable too — the associated delivery is cancelled in cascade by
        # update_order_status -> _handle_status_change_actions, regardless of
        # how far the delivery had progressed.
        if current_status in [OrderStatus.DELIVERED, OrderStatus.CANCELLED]:
            raise ConflictError("Order cannot be cancelled")

        # Determine if stock was already deducted from the database
        # For non-cash orders: stock is deducted on CONFIRMED
        # For cash orders: stock is deducted on DELIVERED (which can't be cancelled anyway)
        is_cash_order = order.payment_method == PaymentMethod.CASH if order.payment_method else False
        stock_was_deducted = not is_cash_order and current_status in [
            OrderStatus.CONFIRMED,
            OrderStatus.PREPARING,
            OrderStatus.OUT_FOR_DELIVERY,
        ]

        if stock_was_deducted:
            # Restore stock quantities for confirmed orders
            self._restore_stock_for_order(order, reason)
        else:
            # Just release Redis reservations for pending orders
            try:
                release_result = self.inventory_service.release_reservations(order_id)
                if release_result["success"]:
                    logger.info(f"Released inventory reservations for cancelled order {order_id}")
                else:
                    logger.warning(
                        f"Failed to release inventory reservations for order {order_id}: {release_result.get('reason')}"
                    )
            except Exception:
                logger.exception("Error releasing inventory reservations for order %s", order_id)

        # Cancel order
        order = self.update_order_status(order_id, OrderStatus.CANCELLED, actor_id, reason)

        # Reverse any applied reward redemption: refund spent points (non-tier-
        # qualifying), flip the redemption to cancelled, decrement reward usage.
        # Best-effort: a loyalty failure must not block the cancellation itself.
        from business_app.services.loyalty_service import LoyaltyService

        try:
            LoyaltyService().cancel_redemption_for_order(order_id, commit=True)
        except Exception:
            logger.exception("Failed to refund reward redemption for cancelled order %s", order_id)

        # Release reserved corporate prepayment units (if any).
        from business_app.services.corporate_contract_service import CorporateContractService

        CorporateContractService().release_for_order(
            order_id=order.id,
            reason=reason,
            actor_user_id=actor_id,
        )

        if order.payment:
            try:
                from business_app.services.payment_fiscalization_service import PaymentFiscalizationService

                PaymentFiscalizationService().release_reserved_marking_codes(
                    order.payment,
                    reason=reason or "order_cancelled",
                    actor_user_id=actor_id,
                )
            except Exception:
                logger.exception("Failed to release reserved marking codes for order %s", order.id)
        db.session.commit()

        # Handle refund if payment was made
        if process_payment_refund and order.payment and order.payment.status == PaymentStatus.COMPLETED:
            from .payment_service import PaymentService

            payment_service = PaymentService()
            payment_service.process_refund(order.payment.id, order.total_amount, reason)

        # Note: the delivery is cancelled in cascade inside update_order_status
        # (_handle_status_change_actions, CANCELLED branch) above, so every
        # order-cancel path — including the admin status dropdown — cancels it.

        return order

    def rescue_order_after_psp_failure(self, cancelled_order_id: int, user_id: int) -> Order:
        """Clone a tax-committee-cancelled order into a new PENDING cash order.

        Rescue path for the case where a customer attempted a card/click
        payment, the Tax Committee (Asl belgisi) was unavailable, and the
        order was cancelled by ``business_app/api/orders.create_order``.
        Customers with outstanding COD debts can only normally pay via card,
        so a PSP outage leaves them with no working payment option without
        this rescue.

        Strategy: rather than mutate the cancelled row back to PENDING (which
        would require bypassing the order state machine + manually resetting
        payment status, delivery status, marking codes, etc.), we treat the
        cancelled order as a "template" and create a brand new order from
        its items + address + delivery slot. The cancelled row stays as
        history. The new order goes through the canonical ``create_order``
        pipeline, so all downstream side-effects (inventory reservation,
        payment record creation, audit logging, etc.) fire normally — only
        the COD active-debt cap is bypassed.

        Security: only orders cancelled with reason="tax_committee_unavailable"
        qualify. User- or admin-cancelled orders cannot use this path.

        Returns the newly created order. Caller is responsible for creating
        the delivery row + dispatching auto-assign (mirroring the main
        create_order endpoint).
        """
        cancelled = self.get_order(cancelled_order_id, user_id=user_id)

        current_status = cancelled.status
        if isinstance(current_status, str):
            try:
                current_status = OrderStatus(current_status)
            except ValueError:
                pass

        if current_status != OrderStatus.CANCELLED:
            raise ConflictError("Only cancelled orders can be retried with cash")

        # The cancellation reason is stored in OrderStatusHistory.notes (set by
        # cancel_order → update_order_status with notes=reason). We look at the
        # most recent CANCELLED transition for this order.
        last_cancel_history = (
            OrderStatusHistory.query.filter(
                OrderStatusHistory.order_id == cancelled.id,
                OrderStatusHistory.new_status == OrderStatus.CANCELLED,
            )
            .order_by(desc(OrderStatusHistory.changed_at), desc(OrderStatusHistory.id))
            .first()
        )
        cancel_notes = ((last_cancel_history.notes if last_cancel_history else "") or "").strip().lower()
        if "tax_committee_unavailable" not in cancel_notes:
            raise ConflictError("This order is not eligible for cash retry")

        if not cancelled.order_items:
            raise ValidationError("Cancelled order has no items to retry")
        if not cancelled.delivery_address:
            raise ValidationError("Cancelled order has no delivery address")

        address = cancelled.delivery_address
        order_data = {
            "items": [{"product_id": item.product_id, "quantity": item.quantity} for item in cancelled.order_items],
            "delivery_address": {
                "delivery_address_id": address.id,
                "street": address.street_address,
                "longitude": address.longitude,
                "latitude": address.latitude,
            },
            "delivery_date": cancelled.delivery_date,
            "delivery_time_slot": cancelled.delivery_time_slot,
            "delivery_notes": cancelled.delivery_notes,
            "payment_method": "cash",
            "order_source": cancelled.order_source or "telegram",
        }

        new_order = self.create_order(user_id, order_data, bypass_cod_check=True)

        audit_logger.log_event(
            event_type=AuditEventType.ORDER_CREATED,
            action="cod_rescue_after_psp_failure",
            severity=AuditSeverity.HIGH,
            resource_type="order",
            resource_id=str(new_order.id),
            description=(
                f"Order {new_order.order_number} created as cash rescue for "
                f"cancelled order {cancelled.order_number} after Asl belgisi "
                f"unavailability. COD active-debt limit was bypassed."
            ),
            additional_data={
                "rescued_order_id": new_order.id,
                "rescued_order_number": new_order.order_number,
                "source_cancelled_order_id": cancelled.id,
                "source_cancelled_order_number": cancelled.order_number,
                "user_id": user_id,
            },
        )

        return new_order

    def _restore_stock_for_order(self, order: Order, reason: str = None):
        """Restore stock quantities for a cancelled order that had stock deducted"""
        from business_app.services.inventory_service import InventoryOperationType

        inventory_service = self.inventory_service
        cancellation_reason = reason or "Order cancelled"

        for item in order.order_items:
            try:
                result = inventory_service.adjust_inventory(
                    product_id=item.product_id,
                    quantity_change=item.quantity,  # Positive to restore stock
                    operation_type=InventoryOperationType.RETURN_RESTOCK,
                    reason=f"Stock restored for cancelled order {order.order_number}: {cancellation_reason}",
                    user_id=order.user_id,
                )

                if result["success"]:
                    logger.info(
                        f"Restored stock for product {item.product_id}: "
                        f"+{item.quantity} units (order {order.order_number})"
                    )
                else:
                    logger.error(f"Failed to restore stock for product {item.product_id}: " f"{result.get('reason')}")
            except Exception:
                logger.exception(
                    "Error restoring stock for product %s (order %s)",
                    item.product_id,
                    order.order_number,
                )

    def get_order_summary(
        self, user_id: int = None, start_date: datetime = None, end_date: datetime = None
    ) -> Dict[str, Any]:
        """Get order summary statistics"""
        query = Order.query

        if user_id:
            query = query.filter_by(user_id=user_id)

        if start_date:
            query = query.filter(Order.created_at >= start_date)

        if end_date:
            query = query.filter(Order.created_at <= end_date)

        orders = query.all()

        summary = {
            "total_orders": len(orders),
            "total_amount": sum(order.total_amount for order in orders),
            "status_breakdown": {},
            "average_order_value": 0,
            "most_ordered_products": self._get_most_ordered_products(orders),
        }

        # Status breakdown
        for status in OrderStatus:
            count = len([o for o in orders if o.status == status])
            summary["status_breakdown"][status.value] = count

        # Average order value
        if orders:
            summary["average_order_value"] = summary["total_amount"] / len(orders)

        return summary

    def reorder(self, order_id: int, user_id: int) -> Order:
        """Create a new order based on a previous order"""
        original_order = self.get_order(order_id, user_id)

        # Prepare new order data
        order_data = {
            "items": [
                {"product_id": item.product_id, "quantity": item.quantity} for item in original_order.order_items
            ],
            "delivery_address": {
                "street": original_order.delivery_address.street_address if original_order.delivery_address else None,
                "city": original_order.delivery_address.city if original_order.delivery_address else None,
                "latitude": original_order.delivery_address.latitude if original_order.delivery_address else None,
                "longitude": original_order.delivery_address.longitude if original_order.delivery_address else None,
            },
            "delivery_instructions": original_order.delivery_notes,
        }

        return self.create_order(user_id, order_data)

    # Private methods
    def _validate_order_data(self, order_data: Dict[str, Any]):
        """Validate order data"""
        required_fields = ["items", "delivery_address"]

        for field in required_fields:
            if field not in order_data:
                raise ValidationError(f"Missing required field: {field}")

        # Validate items
        items = order_data["items"]
        if not items or len(items) == 0:
            raise ValidationError("Order must contain at least one item")

        if len(items) > self.max_order_items:
            raise ValidationError(f"Order cannot contain more than {self.max_order_items} items")

        # Validate delivery address
        address = order_data["delivery_address"]
        required_address_fields = ["street", "latitude", "longitude"]

        for field in required_address_fields:
            if field not in address:
                raise ValidationError(f"Missing required address field: {field}")

    def _process_order_items(
        self,
        items_data: List[Dict[str, Any]],
        user_id: int,
        order_id: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Decimal]:
        """Process and validate order items with comprehensive inventory checks"""
        processed_items = []
        subtotal = Decimal("0.00")

        # Validate basic item structure
        for item in items_data:
            if "product_id" not in item or "quantity" not in item:
                raise ValidationError("Each item must have product_id and quantity")

            quantity = int(item["quantity"])
            if quantity <= 0:
                raise ValidationError("Quantity must be positive")

            max_quantity = current_app.config["MAX_QUANTITY_PER_ITEM"]
            if quantity > max_quantity:  # Reasonable limit to prevent abuse
                raise ValidationError(f"Maximum quantity per item is {max_quantity}")

        # Perform comprehensive inventory availability check
        availability_results = self.inventory_service.check_multiple_products_availability(
            items_data, exclude_order_id=order_id
        )

        # Check for any unavailable items
        unavailable_items = []
        for result in availability_results:
            if not result.is_available:
                product = Product.query.get(result.product_id)
                product_name = product.name if product else f"Product {result.product_id}"
                unavailable_items.append(f"{product_name}: {result.reason}")

        if unavailable_items:
            raise ValidationError(f"Inventory check failed: {'; '.join(unavailable_items)}")

        # Process items and calculate pricing
        from business_app.services.corporate_contract_service import CorporateContractService

        corporate_service = CorporateContractService()
        for item in items_data:
            product: Product = Product.query.get(item["product_id"])
            if not product:
                raise NotFoundError(f"Product {item['product_id']} not found")

            if not product.is_active:
                raise ValidationError(f"Product {product.name} is not available")

            quantity = int(item["quantity"])

            # Per-product purchase minimum. Fire before contract pricing to avoid
            # unnecessary DB work on rejected orders.
            min_order_quantity = int(product.min_order_quantity or 1)
            if quantity < min_order_quantity:
                raise ValidationError(
                    f"{product.name}: minimum order quantity is {min_order_quantity} " f"(you ordered {quantity})"
                )

            fallback_price = Decimal(str(product.calculate_price(quantity=quantity)))
            resolution = corporate_service.resolve_contract_pricing_for_user_product(
                user_id=user_id,
                product_id=product.id,
                fallback_price=fallback_price,
            )
            unit_price = Decimal(str(resolution["unit_price"]))
            total_price = Decimal(str(unit_price)) * Decimal(str(quantity))

            processed_items.append(
                {
                    "product_id": product.id,
                    "contract_id": resolution["contract"].id if resolution["contract"] else None,
                    "contract_product_price_id": (
                        resolution["contract_price_row"].id if resolution["contract_price_row"] else None
                    ),
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "total_price": total_price,
                }
            )

            subtotal += total_price

        # Log inventory check for audit
        audit_logger.log_event(
            event_type=AuditEventType.ORDER_CREATED,
            action="order_inventory_validated",
            severity=AuditSeverity.MEDIUM,
            resource_type="order_inventory",
            description=f"Inventory validated for {len(processed_items)} items",
            additional_data={
                "items_count": len(processed_items),
                "total_quantity": sum(item["quantity"] for item in processed_items),
                "subtotal": float(subtotal),
            },
        )

        return processed_items, subtotal

    def _is_valid_status_transition(self, current_status: OrderStatus, new_status: OrderStatus) -> bool:
        """Check if status transition is valid (delegates to shared.status_transitions)."""
        return is_valid_order_transition(current_status, new_status)

    def _update_status_fields(self, order: Order, new_status: OrderStatus):
        """Update status-specific fields"""
        now = datetime.now(timezone.utc)

        if new_status == OrderStatus.CONFIRMED:
            order.confirmed_at = now
        elif new_status == OrderStatus.PREPARING:
            order.preparing_at = now
        elif new_status == OrderStatus.OUT_FOR_DELIVERY:
            order.out_for_delivery_at = now
        elif new_status == OrderStatus.DELIVERED:
            order.delivered_at = now
        elif new_status == OrderStatus.CANCELLED:
            order.cancelled_at = now

    def _create_status_history(
        self, order_id: int, old_status: OrderStatus, new_status: OrderStatus, updated_by: int = None, notes: str = None
    ):
        """Create order status history record"""
        history = OrderStatusHistory(
            order_id=order_id,
            old_status=old_status,
            new_status=new_status,
            changed_by=updated_by,
            notes=notes,
            changed_at=datetime.now(timezone.utc),
        )

        db.session.add(history)

    def _send_order_notification(self, order: Order, notification_type: str):
        """Send order notification"""
        from ..tasks.notification_tasks import send_order_notification_task

        send_order_notification_task.delay(order.id, notification_type)

    def _schedule_auto_confirmation(self, order_id: int):
        """Schedule automatic order confirmation"""
        from ..tasks.order_tasks import auto_confirm_order_task

        # Confirm order automatically after 10 minutes if not manually confirmed
        auto_confirm_order_task.apply_async(args=[order_id], countdown=600)

    def _handle_status_change_actions(
        self,
        order: Order,
        new_status: OrderStatus,
        bottles_returned: int = None,
        updated_by: int = None,
        commit: bool = True,
    ):
        """Handle actions when order status changes.

        When ``commit`` is False the caller owns the transaction boundary and
        no commit is issued from inside this method or the inner services it
        invokes (delivery completion, cash-collection helpers).
        """
        from shared.enums import PaymentMethod

        if new_status == OrderStatus.CONFIRMED:
            # For non-cash orders, confirm inventory reservations - reduce actual stock
            # Cash on delivery orders will have stock deducted on DELIVERED status
            is_cash_order = order.payment_method == PaymentMethod.CASH if order.payment_method else False

            if not is_cash_order:
                self._confirm_inventory_for_order(order)
            else:
                logger.info(f"Skipping inventory confirmation for cash order {order.id} - will deduct on delivery")

            # Create delivery record (idempotent — API/scheduled-task paths may have created it already)
            if not order.delivery:
                from .delivery_service import DeliveryService

                DeliveryService().create_delivery(order.id)

            # NOTE: purchase AquaCoins are NOT awarded at CONFIRMED. They are
            # earned only once the order is delivered AND fully paid (see the
            # DELIVERED branch and the payment-edge hooks), so a confirmed-but-
            # unpaid order — including admin/manual confirmations with no
            # completed payment — never accrues points prematurely.

        elif new_status == OrderStatus.DELIVERED:
            # Mark delivery as completed (sync_order_status=False to prevent circular callback)
            if order.delivery:
                from .delivery_service import DeliveryService

                delivery_service = DeliveryService()
                delivery_status = (
                    order.delivery.status.value if hasattr(order.delivery.status, "value") else order.delivery.status
                )
                if delivery_status != DeliveryStatus.DELIVERED.value:
                    delivery_service.complete_delivery(order.delivery.id, sync_order_status=False, commit=commit)

            # For cash orders, confirm inventory and settle any prepaid COD balance on delivery
            is_cash_order = order.payment_method == PaymentMethod.CASH if order.payment_method else False
            if is_cash_order:
                self._confirm_inventory_for_order(order)
                # Auto-apply any customer COD prepayment balance to this delivered COD order.
                from business_app.services.cash_collection_service import CashCollectionService

                cash_collection_service = CashCollectionService()
                payment = order.payment or cash_collection_service.ensure_cod_payment_for_order(order)
                cash_collection_service.consume_reserved_prepayment_for_payment(
                    payment,
                    collected_at=order.delivered_at,
                    collected_by=updated_by,
                )
                cash_collection_service.apply_customer_prepaid_credit_to_payment(payment)

            # Award purchase AquaCoins now that the order is delivered. The guard
            # self-checks (delivered AND fully paid) and is idempotent, so:
            #   - prepaid orders (is_paid set at payment time) earn here;
            #   - COD orders settled by prepaid credit at delivery (above) earn here;
            #   - COD orders whose cash is collected later earn when the
            #     cash-collection projection flips is_paid (see CashCollectionService).
            self.maybe_award_purchase_points(order, commit=commit)

            # --- LOYALTY OVERHAUL TRIGGERS ---
            # Triggers that must happen only on successful delivery
            try:
                from .loyalty_service import LoyaltyService

                loyalty_service = LoyaltyService()

                # Check/Update Streak
                loyalty_service.update_streak(order.user_id, commit=commit)

                # NOTE: Surprise rewards are no longer evaluated here. They run in a
                # nightly batch (LoyaltyService.process_daily_surprise_rewards via
                # the process-daily-surprise-rewards beat task) over the day's
                # delivered AND fully-paid orders — so a COD order paid later the
                # same day still qualifies, while one paid the next day does not.

            except Exception:
                logger.exception("Failed to process loyalty triggers for delivered order %s", order.id)

            # Corporate prepayment ledger update on successful delivery.
            # - Workplace (UNITS-mode) contracts: consume the units reserved at
            #   order creation, recording per-product CONSUME ledger entries.
            # - Grocery store (AMOUNT-mode) contracts: post a CHARGE ledger
            #   entry for the order total against the contract's money debt.
            from business_app.services.corporate_contract_service import CorporateContractService

            corporate_service = CorporateContractService()
            amount_contract = None
            if order.user and order.user.is_grocery_store:
                amount_contract = corporate_service.get_active_amount_contract_for_user(order.user.id)

            if amount_contract:
                corporate_service.charge_on_delivery(
                    order=order,
                    delivery_id=order.delivery.id if order.delivery else None,
                    actor_user_id=updated_by,
                )
            else:
                corporate_service.consume_for_order(
                    order_id=order.id,
                    delivery_id=order.delivery.id if order.delivery else None,
                )

            # --- Returnable bottle tracking ---
            logger.info(
                f"[BOTTLE] Starting bottle tracking for order={order.id} user={order.user_id} address={order.delivery_address_id} updated_by={updated_by} bottles_returned={bottles_returned}"  # noqa: E501
            )
            try:
                from business_app.services.bottle_tracking_service import BottleTrackingService

                bottle_service = BottleTrackingService()
                bottles_in_order = bottle_service.calculate_bottles_for_order(order)
                logger.info(f"[BOTTLE] order={order.id} calculated bottles_in_order={bottles_in_order}")

                if bottles_in_order <= 0:
                    logger.info(f"[BOTTLE] order={order.id} skipping — no returnable bottles in order items")
                elif not order.delivery_address_id:
                    logger.info(f"[BOTTLE] order={order.id} skipping — delivery_address_id is None")
                else:
                    logger.info(
                        f"[BOTTLE] order={order.id} recording delivery: user={order.user_id} address={order.delivery_address_id} qty={bottles_in_order} actor={updated_by}",  # noqa: E501
                    )
                    bottle_service.record_bottles_delivered(
                        order_id=order.id,
                        user_id=order.user_id,
                        address_id=order.delivery_address_id,
                        quantity=bottles_in_order,
                        actor_user_id=updated_by,
                    )
                    logger.info(f"[BOTTLE] order={order.id} record_bottles_delivered OK")

                    bottles_returned_qty = Decimal(str(bottles_returned)) if bottles_returned else Decimal("0")
                    logger.info(f"[BOTTLE] order={order.id} bottles_returned_qty={bottles_returned_qty}")
                    if bottles_returned_qty > 0:
                        logger.info(
                            f"[BOTTLE] order={order.id} recording return: qty={bottles_returned_qty} delivery={order.delivery.id if order.delivery else None}",  # noqa: E501
                        )
                        bottle_service.record_bottles_returned(
                            user_id=order.user_id,
                            address_id=order.delivery_address_id,
                            quantity=bottles_returned_qty,
                            order_id=order.id,
                            delivery_id=order.delivery.id if order.delivery else None,
                            actor_user_id=updated_by,
                        )
                        logger.info(f"[BOTTLE] order={order.id} record_bottles_returned OK", order.id)

                    # Credit the session the order is bound to. The progress
                    # guard (assert_driver_can_progress_delivery) migrates the
                    # binding onto the driver's current open session at pickup,
                    # so by delivery time binding.session_id is the session that
                    # physically carried these bottles — even when the order was
                    # accepted under an earlier, now-closed session (carry-over).
                    # Closed sessions keep their sealed counters; the carry-over
                    # tallies here, against the delivering session.
                    from business_app.models.bottle import DriverBottleSession, DriverBottleSessionOrder

                    binding = DriverBottleSessionOrder.query.filter_by(order_id=order.id).first()
                    if binding:
                        bottle_session = DriverBottleSession.query.get(binding.session_id)
                        if bottle_session:
                            prev_delivered = bottle_session.bottles_delivered or 0
                            prev_collected = bottle_session.bottles_collected_from_customers or 0
                            bottle_session.bottles_delivered = prev_delivered + int(bottles_in_order)
                            bottle_session.bottles_collected_from_customers = prev_collected + int(bottles_returned_qty)
                            db.session.flush()
                            logger.info(
                                "[BOTTLE] order=%s tallied to bound session=%s " "delivered=%s→%s collected=%s→%s",
                                order.id,
                                bottle_session.id,
                                prev_delivered,
                                bottle_session.bottles_delivered,
                                prev_collected,
                                bottle_session.bottles_collected_from_customers,
                            )
                        else:
                            self._handle_missing_bottle_session_on_delivery(
                                order,
                                f"binding {binding.id} references missing session {binding.session_id}",
                            )
                    else:
                        self._handle_missing_bottle_session_on_delivery(
                            order,
                            "no DriverBottleSessionOrder binding exists " "(should have been created at accept time)",
                        )

            except ValidationError:
                # Bottle-session invariant violations must abort the
                # delivery transition rather than be swallowed (that's
                # the bug we're fixing). Let the outer transaction
                # roll back.
                raise
            except Exception as bottle_exc:
                logger.error(
                    "[BOTTLE] FAILED for order=%s: %s",
                    order.id,
                    bottle_exc,
                    exc_info=True,
                )

            if commit:
                db.session.commit()
        elif new_status in {OrderStatus.CANCELLED, OrderStatus.RETURNED}:
            payment_synced = self._sync_payment_status_for_terminal_order_state(order, new_status)
            released_reserved_prepayment = False
            if order.payment_method == PaymentMethod.CASH:
                from business_app.services.cash_collection_service import CashCollectionService

                CashCollectionService().release_reserved_prepayment_for_order(
                    order_id=order.id,
                    actor_user_id=getattr(order, "updated_by", None),
                    reason=f"Order moved to {new_status.value}",
                )
                released_reserved_prepayment = True

            # Cascade an order cancellation onto its delivery so a cancelled
            # order never leaves a live/scheduled delivery behind. Centralised
            # here (not only in cancel_order) so EVERY path that moves an order
            # to CANCELLED — including the admin status dropdown's direct
            # update_order_status call — cancels the delivery. One-directional:
            # this never changes the order status.
            delivery_cancelled = False
            if new_status == OrderStatus.CANCELLED:
                delivery_cancelled = self._cancel_delivery_for_cancelled_order(order)

            if commit and (payment_synced or released_reserved_prepayment or delivery_cancelled):
                db.session.commit()

    def _cancel_delivery_for_cancelled_order(self, order: Order) -> bool:
        """Cascade an order cancellation onto its delivery.

        Cancels any non-terminal delivery (scheduled → arrived) so a cancelled
        order never leaves a live/scheduled delivery behind, and releases the
        assigned driver's active-workload counter. One-directional: it sets the
        delivery to CANCELLED but never touches the order status (the order is
        already being cancelled by the caller).

        Returns True if a delivery was cancelled (so the caller commits).
        """
        delivery = getattr(order, "delivery", None)
        if delivery is None:
            return False

        status = delivery.status
        if isinstance(status, str):
            try:
                status = DeliveryStatus(status)
            except ValueError:
                status = None

        terminal = {
            DeliveryStatus.DELIVERED,
            DeliveryStatus.CANCELLED,
            DeliveryStatus.FAILED,
            DeliveryStatus.RETURNED,
        }
        if status in terminal:
            return False

        now = datetime.now(timezone.utc)
        old_status = delivery.status
        driver_id = delivery.delivery_person_id
        delivery.status = DeliveryStatus.CANCELLED
        delivery.updated_at = now

        db.session.add(
            DeliveryStatusHistory(
                delivery_id=delivery.id,
                old_status=old_status,
                new_status=DeliveryStatus.CANCELLED,
                changed_by=getattr(order, "updated_by", None),
                changed_at=now,
                notes="Delivery cancelled because the order was cancelled",
            )
        )

        if driver_id:
            from business_app.services.staff_service import StaffService

            StaffService.sync_active_delivery_counters([driver_id])

        logger.info(f"Delivery {delivery.id} cancelled in cascade from order {order.id} cancellation")
        return True

    def _sync_payment_status_for_terminal_order_state(self, order: Order, new_status: OrderStatus) -> bool:
        """Cancel non-settled payments when the order reaches a terminal non-delivered state."""
        payment = getattr(order, "payment", None)
        if not payment:
            return False

        current_status = payment.status
        if isinstance(current_status, str):
            try:
                current_status = PaymentStatus(current_status)
            except ValueError:
                return False

        non_settled_statuses = {
            PaymentStatus.PENDING,
            PaymentStatus.PROCESSING,
        }
        if current_status not in non_settled_statuses:
            return False

        payment.status = PaymentStatus.CANCELLED
        payment.paid_at = None
        payment.failure_reason = (
            payment.failure_reason or f"Payment cancelled because order moved to {new_status.value}"
        )
        order.is_paid = False
        order.paid_at = None
        return True

    def _handle_missing_bottle_session_on_delivery(self, order: Order, detail: str) -> None:
        """Handle a delivered order whose bottle-session binding is missing or broken.

        With ``BOTTLE_SESSION_ENFORCEMENT_STRICT`` on, this raises so the
        outer transaction rolls back rather than silently committing a
        desynced truck-side ledger. With the flag off (legacy default for
        PR 1) it logs at WARN so we can measure the at-risk population
        without breaking in-flight deliveries.
        """
        from flask import current_app

        msg = f"order={order.id} reached DELIVERED but bottle-session binding is " f"unusable: {detail}"
        strict = False
        try:
            strict = bool(current_app.config.get("BOTTLE_SESSION_ENFORCEMENT_STRICT", False))
        except RuntimeError:
            strict = False

        if strict:
            raise ValidationError(msg, error_code="BOTTLE_SESSION_REQUIRED")
        logger.warning("[BOTTLE] (legacy) %s — skipping session tally", msg)

    def maybe_award_purchase_points(self, order: Order, commit: bool = True) -> None:
        """Award purchase AquaCoins for an order, but only once it is BOTH
        delivered AND fully paid.

        This is the single guarded entry point for purchase accrual. The two
        qualifying events — delivery and full payment — fire in different
        services and in either order, so this is invoked from every edge
        (delivery completion, prepaid payment success, COD cash collection).
        It is idempotent: an order that already earned its purchase AquaCoins is
        never credited again, so concurrent edges collapse to a single award.

        Errors are swallowed so loyalty accrual never breaks the order/payment
        flow that triggered it.
        """
        try:
            status_value = order.status.value if hasattr(order.status, "value") else order.status
            if status_value != OrderStatus.DELIVERED.value or not order.is_paid:
                return

            from .loyalty_service import LoyaltyService

            if LoyaltyService().has_purchase_award(order.id):
                return

            # Entity-eligibility gate (product-owner decision 2026-06-24): an
            # ineligible entity user (entity with no active loyalty-eligible
            # corporate contract) earns NO purchase AquaCoins. Clean early-return
            # so delivery/payment flow is never blocked.
            if not LoyaltyService.is_user_loyalty_eligible(order.user):
                return

            self._process_loyalty_points_for_order(order, commit=commit)
        except Exception:
            logger.exception("Failed to evaluate purchase AquaCoins for order %s", getattr(order, "id", "?"))

    def _process_loyalty_points_for_order(self, order: Order, commit: bool = True):
        """
        Process loyalty points for an order:
        Award points based on the loyalty-eligible amount.
        """
        from .loyalty_service import LoyaltyService
        from business_app.services.corporate_contract_service import CorporateContractService
        from business_app.utils.constants import LoyaltyActionType

        try:
            loyalty_service = LoyaltyService()
            eligible_amount = CorporateContractService().get_loyalty_eligible_amount_for_order(order)
            if eligible_amount <= 0:
                order.loyalty_points_earned = 0
                logger.info(f"Skipping loyalty points for order {order.order_number}: no eligible amount")
                return

            # Contract-linked orders use the eligible line-item subtotal as the points basis.
            points_earned = loyalty_service.calculate_points_for_purchase(order.user_id, int(eligible_amount))
            if points_earned > 0:
                loyalty_service.award_points(
                    order.user_id,
                    points_earned,
                    f"Order #{order.order_number}",
                    LoyaltyActionType.PURCHASE,
                    order.id,
                    commit=commit,
                )
                # Update order with earned points for reference
                order.loyalty_points_earned = points_earned
                logger.info(f"Awarded {points_earned} points for order {order.order_number}")
            else:
                order.loyalty_points_earned = 0

        except Exception:
            # Don't fail the order if loyalty processing fails
            logger.exception("Failed to process loyalty points for order %s", order.order_number)

    def _confirm_inventory_for_order(self, order: Order):
        """Confirm inventory reservations and reduce stock for an order"""
        try:
            confirmation_result = self.inventory_service.confirm_reservations(order.id)
            if confirmation_result["success"]:
                logger.info(f"Confirmed inventory reservations for order {order.id}")

                # Log inventory confirmation
                audit_logger.log_event(
                    event_type=AuditEventType.ORDER_UPDATED,
                    action="inventory_confirmed_for_order",
                    severity=AuditSeverity.HIGH,
                    resource_type="order",
                    resource_id=str(order.id),
                    description=f"Inventory confirmed and stock reduced for order {order.order_number}",
                    additional_data={
                        "order_id": order.id,
                        "order_number": order.order_number,
                        "confirmed_items": confirmation_result.get("confirmed_items", []),
                    },
                )
            else:
                logger.error(f"Failed to confirm inventory for order {order.id}: {confirmation_result.get('reason')}")
                raise ValidationError(f"Inventory confirmation failed: {confirmation_result.get('reason')}")

        except Exception as e:
            logger.exception("Error confirming inventory for order %s", order.id)
            raise ValidationError(f"Failed to confirm inventory: {str(e)}")

    def _get_most_ordered_products(self, orders: List[Order]) -> List[Dict[str, Any]]:
        """Get most ordered products from order list"""
        product_counts = {}

        for order in orders:
            for item in order.order_items:
                if item.product_id not in product_counts:
                    product_counts[item.product_id] = {
                        "product_id": item.product_id,
                        "product_name": item.product.name if item.product else "Unknown",
                        "total_quantity": 0,
                        "total_orders": 0,
                    }

                product_counts[item.product_id]["total_quantity"] += item.quantity
                product_counts[item.product_id]["total_orders"] += 1

        # Sort by total quantity
        sorted_products = sorted(product_counts.values(), key=lambda x: x["total_quantity"], reverse=True)

        return sorted_products[:10]  # Top 10

    def _serialize_order(self, order: Order) -> Dict[str, Any]:
        """Serialize order to dictionary"""
        return {
            "id": order.id,
            "order_number": order.order_number,
            "status": order.status.value,
            "subtotal": order.subtotal,
            "delivery_fee": order.delivery_fee,
            "discount_amount": order.discount_amount or 0,
            "total_amount": order.total_amount,
            "created_at": order.created_at.isoformat(),
            "confirmed_at": order.confirmed_at.isoformat() if order.confirmed_at else None,
            "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
            "delivery_address": {
                "street": order.delivery_address.street_address if order.delivery_address else None,
                "city": order.delivery_address.city if order.delivery_address else None,
                "latitude": order.delivery_address.latitude if order.delivery_address else None,
                "longitude": order.delivery_address.longitude if order.delivery_address else None,
            },
            "items": [
                {
                    "id": item.id,
                    "product_id": item.product_id,
                    "product_name": item.product.name if item.product else "Unknown",
                    "quantity": item.quantity,
                    "unit_price": item.unit_price,
                    "total_price": item.total_price,
                }
                for item in order.order_items
            ],
            "payment": (
                {
                    "status": order.payment.status.value if order.payment else "pending",
                    "method": order.payment.payment_method.value if order.payment else None,
                }
                if order.payment
                else None
            ),
            "delivery": (
                {
                    "status": order.delivery.status.value if order.delivery else "pending",
                    "tracking_code": order.delivery.tracking_code if order.delivery else None,
                    "estimated_delivery_time": (
                        order.delivery.estimated_delivery_time.isoformat()
                        if order.delivery and order.delivery.estimated_delivery_time
                        else None
                    ),
                }
                if order.delivery
                else None
            ),
        }
