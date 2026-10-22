"""Admin bulk action service."""

from datetime import datetime, UTC
from typing import Dict, List

from flask import current_app

from business_app import db
from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.product import Product
from business_app.models.review import Review
from business_app.models.subscription import Subscription
from business_app.models.user import User
from business_app.services.delivery_service import DeliveryService
from business_app.services.session_revocation_service import SessionRevocationService
from business_app.utils.exceptions import ValidationError
from shared.enums import OrderStatus, SubscriptionStatus, UserRole, UserStatus
from shared.status_transitions import is_valid_order_transition


class AdminBulkActionService:
    """Service for admin bulk actions."""

    VALID_ACTIONS = {
        "user": ["activate", "deactivate", "suspend", "delete", "send_email", "assign_role"],
        "order": ["cancel", "confirm", "process", "mark_delivered"],
        "product": ["activate", "deactivate", "delete", "update_stock", "update_price"],
        "review": ["approve", "reject", "delete", "feature"],
        "subscription": ["pause", "resume", "cancel"],
        "delivery": ["assign_driver", "mark_in_transit", "mark_delivered"],
    }

    @staticmethod
    def get_valid_actions() -> Dict[str, List[str]]:
        return AdminBulkActionService.VALID_ACTIONS

    @staticmethod
    def is_valid_action(target_type: str, action: str) -> bool:
        valid = AdminBulkActionService.VALID_ACTIONS
        return target_type in valid and action in valid[target_type]

    @staticmethod
    def perform(action, target_type, target_ids, parameters, reason, admin_id):
        """Dispatch and perform bulk action for target type."""
        parameters = parameters or {}

        if target_type == "user":
            return AdminBulkActionService._bulk_action_users(action, target_ids, parameters, reason, admin_id)
        if target_type == "order":
            return AdminBulkActionService._bulk_action_orders(action, target_ids, parameters, reason, admin_id)
        if target_type == "product":
            return AdminBulkActionService._bulk_action_products(action, target_ids, parameters, reason, admin_id)
        if target_type == "review":
            return AdminBulkActionService._bulk_action_reviews(action, target_ids, parameters, reason, admin_id)
        if target_type == "subscription":
            return AdminBulkActionService._bulk_action_subscriptions(action, target_ids, parameters, reason, admin_id)
        if target_type == "delivery":
            return AdminBulkActionService._bulk_action_deliveries(action, target_ids, parameters, reason, admin_id)

        return {
            "success_count": 0,
            "failed_count": len(target_ids),
            "errors": [{"target_type": target_type, "error": "Invalid target type"}],
            "total_errors": 1,
        }

    @staticmethod
    def _bulk_action_users(action, user_ids, parameters, reason, admin_id):
        success_count = 0
        failed_count = 0
        errors = []

        for user_id in user_ids:
            try:
                user = User.query.get(user_id)
                if not user:
                    failed_count += 1
                    errors.append({"user_id": user_id, "error": "User not found"})
                    continue

                if action == "activate":
                    user.status = UserStatus.ACTIVE
                    user.is_active = True
                elif action == "deactivate":
                    user.status = UserStatus.INACTIVE
                    user.is_active = False
                elif action == "suspend":
                    user.status = UserStatus.SUSPENDED
                    user.is_active = False
                elif action == "delete":
                    user.status = UserStatus.DELETED
                    user.is_active = False
                elif action == "assign_role":
                    new_role = parameters.get("role")
                    # Deliberately WITHOUT sales_agent: this writes `users.role` and nothing
                    # else, so it would mint an agent with no `SalesAgentProfile` behind it -- no
                    # districts, no cadence -- and every agent-scoped path would then refuse the
                    # person it just promoted. Agents are created only through
                    # POST /admin/staff/sales-agents, which creates the profile in the same call.
                    valid_roles = [
                        UserRole.CUSTOMER.value,
                        UserRole.ADMIN.value,
                        UserRole.MANAGER.value,
                        UserRole.OPERATOR.value,
                    ]
                    if new_role in valid_roles:
                        old_role = user.role
                        roles_before = SessionRevocationService.roles_of(user)
                        user.role = UserRole(new_role)
                        # The new role applies at once: the person's logins end (owner decision R9c).
                        # With Redis down this raises, and the user's change rolls back below.
                        SessionRevocationService.revoke_on_role_change(user, roles_before)
                        current_app.logger.info("Assigned role %s to user %s (was %s)", new_role, user_id, old_role)
                    else:
                        failed_count += 1
                        errors.append({"user_id": user_id, "error": "Invalid role"})
                        continue
                elif action == "send_email":
                    pass

                db.session.commit()
                success_count += 1

            except Exception as e:
                db.session.rollback()
                failed_count += 1
                errors.append({"user_id": user_id, "error": str(e)})
                current_app.logger.exception("Bulk user action '%s' failed for user_id=%s", action, user_id)

        return {
            "success_count": success_count,
            "failed_count": failed_count,
            "errors": errors[:10],
            "total_errors": len(errors),
        }

    @staticmethod
    def _bulk_action_orders(action, order_ids, parameters, reason, admin_id):
        success_count = 0
        failed_count = 0
        errors = []

        for order_id in order_ids:
            try:
                order = Order.query.get(order_id)
                if not order:
                    failed_count += 1
                    errors.append({"order_id": order_id, "error": "Order not found"})
                    continue

                if action == "cancel":
                    if order.status in [OrderStatus.PENDING, OrderStatus.CONFIRMED]:
                        from business_app.services.order_service import OrderService

                        OrderService().cancel_order(
                            order.id,
                            reason=reason,
                            actor_user_id=admin_id,
                        )
                    else:
                        failed_count += 1
                        errors.append(
                            {"order_id": order_id, "error": f"Cannot cancel order with status {order.status}"}
                        )
                        continue

                elif action == "confirm":
                    if is_valid_order_transition(order.status, OrderStatus.CONFIRMED):
                        from business_app.services.order_service import OrderService

                        OrderService().update_order_status(
                            order.id, OrderStatus.CONFIRMED, updated_by=admin_id, notes=reason
                        )
                    else:
                        failed_count += 1
                        errors.append(
                            {"order_id": order_id, "error": f"Cannot confirm order with status {order.status}"}
                        )
                        continue

                elif action == "process":
                    from business_app.services.order_schedule_service import OrderScheduleService

                    # Locked and re-read before the `confirmed` check: a customer may cancel a
                    # confirmed order a driver has accepted (F5, amended 2026-10-21), and the read
                    # above may predate that cancel's commit. The ORDER only: this arm writes the
                    # order and only reads the delivery (F18), and a Delivery-then-Order lock could
                    # deadlock against the Order-then-Delivery cancel cascade (spec §10 of
                    # docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md).
                    order = db.session.get(Order, order.id, with_for_update=True, populate_existing=True)
                    # Both refusals are this arm's own failed item, not the loop's except, which
                    # would log an expected refusal as a crash.
                    refused = None
                    if order.status != OrderStatus.CONFIRMED:
                        refused = f"Cannot process order with status {order.status}"
                    else:
                        # F18: the one forward-move rule.
                        try:
                            OrderScheduleService.assert_can_advance(order)
                        except ValidationError as refusal:
                            refused = refusal.message
                    if refused is not None:
                        failed_count += 1
                        errors.append({"order_id": order_id, "error": refused})
                        # The refused item wrote nothing and earlier items have committed: end
                        # its transaction now, which releases the Order lock it took.
                        db.session.rollback()
                        continue
                    order.status = OrderStatus.PREPARING

                elif action == "mark_delivered":
                    if is_valid_order_transition(order.status, OrderStatus.DELIVERED):
                        from business_app.services.order_service import OrderService

                        OrderService().update_order_status(
                            order.id, OrderStatus.DELIVERED, updated_by=admin_id, notes=reason
                        )
                    else:
                        failed_count += 1
                        errors.append(
                            {"order_id": order_id, "error": f"Cannot mark as delivered with status {order.status}"}
                        )
                        continue

                db.session.commit()
                success_count += 1

            except Exception as e:
                db.session.rollback()
                failed_count += 1
                errors.append({"order_id": order_id, "error": str(e)})
                current_app.logger.exception("Bulk order action '%s' failed for order_id=%s", action, order_id)

        return {
            "success_count": success_count,
            "failed_count": failed_count,
            "errors": errors[:10],
            "total_errors": len(errors),
        }

    @staticmethod
    def _bulk_action_products(action, product_ids, parameters, reason, admin_id):
        success_count = 0
        failed_count = 0
        errors = []

        for product_id in product_ids:
            try:
                product = Product.query.get(product_id)
                if not product:
                    failed_count += 1
                    errors.append({"product_id": product_id, "error": "Product not found"})
                    continue

                if action == "activate":
                    product.is_active = True
                elif action == "deactivate":
                    product.is_active = False
                elif action == "delete":
                    product.is_active = False
                    product.deleted_at = datetime.now(UTC)
                elif action == "update_stock":
                    stock_adjustment = parameters.get("stock_adjustment", 0)
                    new_stock = parameters.get("new_stock")

                    if new_stock is not None:
                        product.stock_quantity = new_stock
                    else:
                        product.stock_quantity = (product.stock_quantity or 0) + stock_adjustment

                elif action == "update_price":
                    new_price = parameters.get("new_price")
                    price_adjustment = parameters.get("price_adjustment", 0)

                    if new_price is not None:
                        product.price = new_price
                    elif price_adjustment != 0:
                        product.price = product.price + price_adjustment

                db.session.commit()
                success_count += 1

            except Exception as e:
                db.session.rollback()
                failed_count += 1
                errors.append({"product_id": product_id, "error": str(e)})
                current_app.logger.exception("Bulk product action '%s' failed for product_id=%s", action, product_id)

        return {
            "success_count": success_count,
            "failed_count": failed_count,
            "errors": errors[:10],
            "total_errors": len(errors),
        }

    @staticmethod
    def _bulk_action_reviews(action, review_ids, parameters, reason, admin_id):
        success_count = 0
        failed_count = 0
        errors = []

        for review_id in review_ids:
            try:
                review = Review.query.get(review_id)
                if not review:
                    failed_count += 1
                    errors.append({"review_id": review_id, "error": "Review not found"})
                    continue

                if action == "approve":
                    review.is_approved = True
                    review.moderator_notes = reason
                elif action == "reject":
                    review.is_approved = False
                    review.moderator_notes = reason
                elif action == "delete":
                    db.session.delete(review)
                elif action == "feature":
                    review.is_featured = True
                    review.is_approved = True

                db.session.commit()
                success_count += 1

            except Exception as e:
                db.session.rollback()
                failed_count += 1
                errors.append({"review_id": review_id, "error": str(e)})
                current_app.logger.exception("Bulk review action '%s' failed for review_id=%s", action, review_id)

        return {
            "success_count": success_count,
            "failed_count": failed_count,
            "errors": errors[:10],
            "total_errors": len(errors),
        }

    @staticmethod
    def _bulk_action_subscriptions(action, subscription_ids, parameters, reason, admin_id):
        success_count = 0
        failed_count = 0
        errors = []

        for subscription_id in subscription_ids:
            try:
                subscription = Subscription.query.get(subscription_id)
                if not subscription:
                    failed_count += 1
                    errors.append({"subscription_id": subscription_id, "error": "Subscription not found"})
                    continue

                if action == "pause":
                    if subscription.status == SubscriptionStatus.ACTIVE:
                        subscription.status = SubscriptionStatus.PAUSED
                        subscription.paused_at = datetime.now(UTC)
                        subscription.pause_reason = reason
                    else:
                        failed_count += 1
                        errors.append(
                            {"subscription_id": subscription_id, "error": "Can only pause active subscriptions"}
                        )
                        continue

                elif action == "resume":
                    if subscription.status == SubscriptionStatus.PAUSED:
                        subscription.status = SubscriptionStatus.ACTIVE
                        subscription.paused_at = None
                        subscription.pause_reason = None
                    else:
                        failed_count += 1
                        errors.append(
                            {"subscription_id": subscription_id, "error": "Can only resume paused subscriptions"}
                        )
                        continue

                elif action == "cancel":
                    subscription.status = SubscriptionStatus.CANCELLED
                    subscription.end_date = datetime.now(UTC)

                db.session.commit()
                success_count += 1

            except Exception as e:
                db.session.rollback()
                failed_count += 1
                errors.append({"subscription_id": subscription_id, "error": str(e)})
                current_app.logger.exception(
                    "Bulk subscription action '%s' failed for subscription_id=%s", action, subscription_id
                )

        return {
            "success_count": success_count,
            "failed_count": failed_count,
            "errors": errors[:10],
            "total_errors": len(errors),
        }

    @staticmethod
    def _bulk_action_deliveries(action, delivery_ids, parameters, reason, admin_id):
        success_count = 0
        failed_count = 0
        errors = []

        for delivery_id in delivery_ids:
            try:
                delivery = Delivery.query.get(delivery_id)
                if not delivery:
                    failed_count += 1
                    errors.append({"delivery_id": delivery_id, "error": "Delivery not found"})
                    continue

                if action == "assign_driver":
                    driver_user_id = parameters.get("driver_id")
                    if not driver_user_id:
                        failed_count += 1
                        errors.append({"delivery_id": delivery_id, "error": "driver_id required"})
                        continue

                    from business_app.services.delivery_assignment_service import DeliveryAssignmentService
                    from shared.enums import AssignmentSource

                    DeliveryAssignmentService.assign_driver(
                        delivery_id,
                        driver_user_id=driver_user_id,
                        actor_id=admin_id,
                        source=AssignmentSource.ADMIN_BULK,
                        note=reason or "Bulk-assigned by admin",
                    )
                    success_count += 1
                    continue

                elif action == "mark_in_transit":
                    DeliveryService().begin_delivery_in_transit(
                        delivery.id,
                        actor_user_id=admin_id,
                        notes=reason or "Updated via admin bulk action to in_transit",
                    )
                    success_count += 1
                    continue

                elif action == "mark_delivered":
                    from business_app.services.order_service import OrderService

                    OrderService().update_order_status(
                        delivery.order_id, OrderStatus.DELIVERED, updated_by=admin_id, notes=reason
                    )
                    success_count += 1
                    continue

                db.session.commit()
                success_count += 1

            except ValidationError as refusal:
                # An expected refusal (e.g. a start on a stop that is no longer assigned, or whose
                # order was cancelled): a failed item, not a crash.
                db.session.rollback()
                failed_count += 1
                errors.append({"delivery_id": delivery_id, "error": str(refusal)})
                current_app.logger.warning(
                    "Bulk delivery action '%s' refused for delivery_id=%s: %s", action, delivery_id, refusal
                )
            except Exception as e:
                db.session.rollback()
                failed_count += 1
                errors.append({"delivery_id": delivery_id, "error": str(e)})
                current_app.logger.exception("Bulk delivery action '%s' failed for delivery_id=%s", action, delivery_id)

        return {
            "success_count": success_count,
            "failed_count": failed_count,
            "errors": errors[:10],
            "total_errors": len(errors),
        }
