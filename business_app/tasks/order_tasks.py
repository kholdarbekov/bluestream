"""
Order-related Celery tasks for the Water Business Platform
This file should be placed in business_app/tasks/order_tasks.py
"""

from celery import shared_task
from celery.utils.log import get_task_logger
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List
from flask import current_app
from sqlalchemy import or_

from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.user import User
from business_app.models.product import Product
from business_app.services.order_service import OrderService
from business_app.services.notification_service import NotificationService
from business_app.utils.constants import NotificationChannel
from shared.enums import OrderStatus, PaymentStatus, UserRole, UserStatus
from business_app.utils.helpers import get_current_language
from business_app import db

logger = get_task_logger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=600, time_limit=600, soft_time_limit=540)
def auto_confirm_order_task(self, order_id: int):
    """Automatically confirm order after specified time"""
    try:
        logger.info(f"Auto-confirming order {order_id}")

        order = Order.query.get(order_id)
        if not order:
            logger.error(f"Order {order_id} not found")
            return {"success": False, "error": "Order not found"}

        # Check if order is still pending
        if order.status != OrderStatus.PENDING:
            logger.info(f"Order {order_id} is no longer pending, current status: {order.status.value}")
            return {"success": False, "error": "Order no longer pending"}

        # Auto-confirm the order
        order_service = OrderService()
        updated_order = order_service.update_order_status(
            order_id, OrderStatus.CONFIRMED, notes="Auto-confirmed after timeout"
        )

        logger.info(f"Order {order_id} auto-confirmed successfully")
        return {"success": True, "order_id": order_id, "new_status": updated_order.status.value}

    except Exception as exc:
        logger.error(f"Auto-confirmation failed for order {order_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task(time_limit=600, soft_time_limit=540)
def auto_confirm_pending_orders():
    """Auto-confirm orders that have been pending for too long"""
    try:
        logger.info("Auto-confirming pending orders")

        # Get orders pending for more than 15 minutes
        cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=15)

        pending_orders = Order.query.filter(Order.status == OrderStatus.PENDING, Order.created_at < cutoff_time).all()

        order_service = OrderService()
        confirmed_count = 0
        failed_count = 0

        for order in pending_orders:
            try:
                # Check if payment is completed or cash on delivery
                if (order.payment and order.payment.status == PaymentStatus.COMPLETED) or (
                    order.payment and order.payment.payment_method.value == "cash"
                ):

                    order_service.update_order_status(
                        order.id, OrderStatus.CONFIRMED, notes="Auto-confirmed - payment verified"
                    )
                    # Defence in depth: surface the silent failure mode where
                    # the order is committed as CONFIRMED but delivery creation
                    # fails inside _handle_status_change_actions.
                    #
                    # "No delivery after confirmation" used to mean something
                    # broke. For a future-dated order it is now the CORRECT
                    # expected state -- the release sweep creates the delivery
                    # on its morning -- so the release gate is consulted before
                    # crying wolf. This feature's stated risk is that "nothing
                    # was due" and "the sweep is broken" look identical from
                    # outside; an ERROR per scheduled order would poison the one
                    # channel that has to stay meaningful.
                    from business_app.services.order_schedule_service import OrderScheduleService

                    db.session.refresh(order)
                    if not order.delivery and not OrderScheduleService.is_awaiting_release(order):
                        logger.error(
                            "Order %s auto-confirmed but delivery was NOT created — address_id=%s, delivery_date=%s",
                            order.id,
                            order.delivery_address_id,
                            order.delivery_date,
                        )
                    confirmed_count += 1
                    logger.info(f"Auto-confirmed order {order.id}")

                elif not order.payment:
                    # Orders without payment method - send reminder
                    notification_service = NotificationService()
                    notification_service.send_notification(
                        order.user_id,
                        "payment_reminder",
                        channels=[NotificationChannel.TELEGRAM],
                        template_data={
                            "order_number": order.order_number,
                            "order_total": float(order.total_amount) if order.total_amount is not None else None,
                            "order_url": f"{current_app.config['COMPANY_WEBSITE']}/orders/{order.id}",
                        },
                    )

            except Exception as e:
                # Roll back so a poisoned session doesn't cascade into the next iteration.
                db.session.rollback()
                failed_count += 1
                logger.exception(f"Failed to auto-confirm order {order.id}: {e}")
                continue

        logger.info(f"Auto-confirmed {confirmed_count} pending orders ({failed_count} failed)")
        return {"confirmed_count": confirmed_count, "failed_count": failed_count}

    except Exception as e:
        logger.error(f"Failed to auto-confirm pending orders: {e}")
        return {"error": str(e)}


@shared_task(time_limit=600, soft_time_limit=540)
def cancel_abandoned_orders():
    """Cancel orders that have been abandoned (no payment after 24 hours)"""
    try:
        logger.info("Cancelling abandoned orders")

        # Get orders pending for more than 24 hours without payment
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)

        # Use FOR UPDATE SKIP LOCKED to prevent concurrent workers from processing same orders.
        # Order.payment is a relationship(uselist=False) — it has no .is_() and no
        # .status. Use the relationship's EXISTS comparator instead.
        abandoned_orders = (
            Order.query.filter(
                Order.status == OrderStatus.PENDING,
                Order.created_at < cutoff_time,
                or_(
                    ~Order.payment.has(),
                    Order.payment.has(Payment.status != PaymentStatus.COMPLETED),
                ),
            )
            .with_for_update(skip_locked=True)
            .all()
        )

        order_service = OrderService()
        notification_service = NotificationService()
        cancelled_count = 0

        for order in abandoned_orders:
            try:
                # Cancel the order
                order_service.cancel_order(order.id, reason="Order abandoned - no payment received within 24 hours")

                # Send abandonment notification
                notification_service.send_notification(
                    order.user_id,
                    "order_cancelled_abandoned",
                    template_data={
                        "order_number": order.order_number,
                        "cancellation_reason": "No payment received within 24 hours",
                        "reorder_url": f"{current_app.config['COMPANY_WEBSITE']}/reorder/{order.id}",
                    },
                )

                cancelled_count += 1
                logger.info(f"Cancelled abandoned order {order.id}")

            except Exception as e:
                logger.error(f"Failed to cancel abandoned order {order.id}: {e}")
                continue

        db.session.commit()  # Release row locks
        logger.info(f"Cancelled {cancelled_count} abandoned orders")
        return {"cancelled_count": cancelled_count}

    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to cancel abandoned orders: {e}")
        return {"error": str(e)}


@shared_task(bind=True, max_retries=3, time_limit=600, soft_time_limit=540)
def update_inventory_after_order(self, order_id: int):
    """Update product inventory after order confirmation"""
    try:
        logger.info(f"Updating inventory for order {order_id}")

        order = Order.query.get(order_id)
        if not order:
            logger.error(f"Order {order_id} not found")
            return {"success": False, "error": "Order not found"}

        if order.status != OrderStatus.CONFIRMED:
            logger.info(f"Order {order_id} is not confirmed, skipping inventory update")
            return {"success": False, "error": "Order not confirmed"}

        inventory_updates = []

        for item in order.order_items:
            try:
                # Atomic inventory decrement — prevents overselling via concurrent orders
                updated = Product.query.filter(
                    Product.id == item.product_id, Product.stock_quantity >= item.quantity
                ).update(
                    {
                        Product.stock_quantity: Product.stock_quantity - item.quantity,
                        Product.is_in_stock: (Product.stock_quantity - item.quantity) > 0,
                        Product.updated_at: datetime.now(timezone.utc),
                    },
                    synchronize_session=False,
                )

                if not updated:
                    # Insufficient stock — set to 0 if stock is less than requested
                    logger.warning(f"Insufficient stock for product {item.product_id}, " f"requested: {item.quantity}")
                    Product.query.filter(Product.id == item.product_id, Product.stock_quantity < item.quantity).update(
                        {
                            Product.stock_quantity: 0,
                            Product.is_in_stock: False,
                            Product.updated_at: datetime.now(timezone.utc),
                        },
                        synchronize_session=False,
                    )

                # Refresh product to get updated values
                product = Product.query.get(item.product_id)
                if product:
                    inventory_updates.append(
                        {
                            "product_id": product.id,
                            "product_name": product.get_translated("name", get_current_language()),
                            "quantity_reduced": item.quantity,
                            "new_stock": product.stock_quantity,
                        }
                    )

                    # Send low stock alert if needed
                    if product.stock_quantity <= product.min_stock_level:
                        send_low_stock_alert.delay(product.id)

            except Exception as e:
                logger.error(f"Failed to update inventory for item {item.id}: {e}")
                continue

        db.session.commit()

        logger.info(f"Inventory updated for order {order_id}: {len(inventory_updates)} products")
        return {"success": True, "order_id": order_id, "inventory_updates": inventory_updates}

    except Exception as exc:
        logger.error(f"Inventory update failed for order {order_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, time_limit=600, soft_time_limit=540)
def send_low_stock_alert(self, product_id: int):
    """Send low stock alert to management"""
    try:
        logger.info(f"Sending low stock alert for product {product_id}")

        product = Product.query.get(product_id)
        if not product:
            logger.error(f"Product {product_id} not found")
            return {"success": False, "error": "Product not found"}

        # Send alert to admin users
        admin_users = User.query.filter(
            User.role.in_([UserRole.ADMIN, UserRole.MANAGER]), User.status == UserStatus.ACTIVE
        ).all()

        notification_service = NotificationService()

        for admin in admin_users:
            notification_service.send_notification(
                admin.id,
                "low_stock_alert",
                template_data={
                    "product_id": product.id,
                    "product_name": product.get_translated("name", get_current_language()),
                    "current_stock": product.stock_quantity,
                    "min_stock_level": product.min_stock_level,
                    "suggested_reorder_quantity": product.reorder_quantity or 100,
                },
            )

        logger.info(f"Low stock alert sent for product {product_id}")
        return {"success": True, "product_id": product_id, "current_stock": product.stock_quantity}

    except Exception as exc:
        logger.error(f"Failed to send low stock alert: {exc}")
        raise self.retry(exc=exc)


@shared_task(time_limit=600, soft_time_limit=540)
def process_bulk_order_updates(order_updates: List[Dict[str, Any]]):
    """Process bulk order status updates"""
    try:
        logger.info(f"Processing {len(order_updates)} bulk order updates")

        order_service = OrderService()
        results = {"successful": 0, "failed": 0, "errors": []}

        for update in order_updates:
            try:
                order_id = update["order_id"]
                new_status = OrderStatus(update["status"])
                notes = update.get("notes")
                updated_by = update.get("updated_by")

                order_service.update_order_status(order_id, new_status, updated_by, notes)
                results["successful"] += 1

            except Exception as e:
                results["failed"] += 1
                results["errors"].append({"order_id": update.get("order_id"), "error": str(e)})
                logger.error(f"Failed to update order {update.get('order_id')}: {e}")
                continue

        logger.info(f"Bulk order updates completed: {results['successful']} successful, {results['failed']} failed")
        return results

    except Exception as e:
        logger.error(f"Failed to process bulk order updates: {e}")
        return {"error": str(e)}


@shared_task(bind=True, max_retries=2, time_limit=600, soft_time_limit=540)
def send_order_followup(self, order_id: int, days_after_delivery: int = 3):
    """Send follow-up message after order delivery"""
    try:
        logger.info(f"Sending follow-up for order {order_id}")

        order = Order.query.get(order_id)
        if not order:
            logger.error(f"Order {order_id} not found")
            return {"success": False, "error": "Order not found"}

        if order.status != OrderStatus.DELIVERED:
            logger.info(f"Order {order_id} not delivered yet, skipping follow-up")
            return {"success": False, "error": "Order not delivered"}

        if not order.delivered_at:
            logger.warning(f"Order {order_id} marked as delivered but no delivery timestamp")
            return {"success": False, "error": "No delivery timestamp"}

        # Check if enough time has passed since delivery
        time_since_delivery = datetime.now(timezone.utc) - order.delivered_at
        if time_since_delivery.days < days_after_delivery:
            # Reschedule for later
            eta = order.delivered_at + timedelta(days=days_after_delivery)
            raise self.retry(eta=eta)

        # Send follow-up notification
        notification_service = NotificationService()

        feedback_url = f"{current_app.config['COMPANY_WEBSITE']}/feedback/{order.id}"
        reorder_url = f"{current_app.config['COMPANY_WEBSITE']}/reorder/{order.id}"

        template_data = {
            "order_number": order.order_number,
            "delivery_date": order.delivered_at.strftime("%B %d, %Y"),
            "customer_name": order.user.first_name,
            "total_items": len(order.order_items),
            "feedback_url": feedback_url,
            "reorder_url": reorder_url,
            "company_name": current_app.config["COMPANY_NAME"],
        }

        notification_service.send_notification(order.user_id, "order_followup", template_data=template_data)

        logger.info(f"Follow-up sent for order {order_id}")
        return {"success": True, "order_id": order_id, "days_after_delivery": days_after_delivery}

    except Exception as exc:
        logger.error(f"Failed to send order follow-up: {exc}")
        raise self.retry(exc=exc)


@shared_task(time_limit=600, soft_time_limit=540)
def cleanup_old_orders():
    """Archive old completed orders to optimize database performance"""
    try:
        logger.info("Cleaning up old orders")

        # Archive orders older than 2 years
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=730)

        old_orders = Order.query.filter(
            Order.created_at < cutoff_date, Order.status.in_([OrderStatus.DELIVERED, OrderStatus.CANCELLED])
        ).all()

        archived_count = 0

        for order in old_orders:
            try:
                # Mark as archived instead of deleting (for audit purposes)
                order.is_archived = True
                order.archived_at = datetime.now(timezone.utc)
                archived_count += 1

            except Exception as e:
                logger.error(f"Failed to archive order {order.id}: {e}")
                continue

        db.session.commit()

        logger.info(f"Archived {archived_count} old orders")
        return {"archived_count": archived_count}

    except Exception as e:
        logger.error(f"Failed to cleanup old orders: {e}")
        db.session.rollback()
        return {"error": str(e)}


@shared_task(time_limit=600, soft_time_limit=540)
def monitor_order_anomalies():
    """Monitor for unusual order patterns that might indicate issues"""
    try:
        logger.info("Monitoring order anomalies")

        # Check for unusual patterns in the last hour
        one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)

        recent_orders = Order.query.filter(Order.created_at >= one_hour_ago).all()

        anomalies = []

        # Check for duplicate orders from same user
        user_order_counts = {}
        for order in recent_orders:
            user_order_counts[order.user_id] = user_order_counts.get(order.user_id, 0) + 1

        for user_id, count in user_order_counts.items():
            if count > 3:  # More than 3 orders in an hour
                user = User.query.get(user_id)
                anomalies.append(
                    {
                        "type": "excessive_orders",
                        "description": f"User {user.email if user else user_id} placed {count} orders in 1 hour",
                        "severity": "medium",
                    }
                )

        # Check for unusually large orders
        large_order_threshold = current_app.config["LARGE_ORDER_THRESHOLD_UZS"]
        for order in recent_orders:
            if order.total_amount > large_order_threshold:
                anomalies.append(
                    {
                        "type": "large_order",
                        "description": (
                            f"Order {order.order_number} has unusually large amount: {order.total_amount} UZS"
                        ),
                        "severity": "low",
                    }
                )

        # Check for orders with no items
        for order in recent_orders:
            if len(order.order_items) == 0:
                anomalies.append(
                    {
                        "type": "empty_order",
                        "description": f"Order {order.order_number} has no items",
                        "severity": "high",
                    }
                )

        # Send alerts for high severity anomalies
        high_severity_anomalies = [a for a in anomalies if a["severity"] == "high"]

        if high_severity_anomalies:
            admin_users = User.query.filter(
                User.role.in_([UserRole.ADMIN, UserRole.MANAGER]), User.status == UserStatus.ACTIVE
            ).all()

            notification_service = NotificationService()

            for admin in admin_users:
                notification_service.send_notification(
                    admin.id,
                    "order_anomaly_alert",
                    template_data={"anomaly_count": len(high_severity_anomalies), "anomalies": high_severity_anomalies},
                )

        logger.info(f"Order anomaly monitoring completed: {len(anomalies)} anomalies detected")
        return {
            "total_anomalies": len(anomalies),
            "high_severity": len(high_severity_anomalies),
            "anomalies": anomalies,
        }

    except Exception as e:
        logger.error(f"Failed to monitor order anomalies: {e}")
        return {"error": str(e)}


@shared_task(time_limit=300, soft_time_limit=240)
def release_due_scheduled_orders():
    """Hand every due future-dated order to the drivers.

    A future-dated order has NO delivery row, which is exactly what makes it
    invisible to every driver-facing surface. This sweep is what ends that.

    Query-driven rather than a per-order `apply_async(eta=...)`: an ETA is lost
    if the broker or worker restarts, whereas a sweep that matches
    `delivery_date <= today` self-heals — a day of downtime releases on the next
    tick instead of stranding orders forever.
    """
    from business_app.models.delivery import Delivery
    from business_app.services.order_schedule_service import (
        RELEASABLE_ORDER_STATUSES,
        OrderScheduleService,
        get_utc_now,
    )
    from business_app.utils.timezone_utils import utc_to_local
    from shared.constants import DISPLAY_TIMEZONE

    # Imported from order_schedule_service (not timezone_utils directly) so
    # that patching `business_app.services.order_schedule_service.get_utc_now`
    # in tests governs this sweep's clock too — the same symbol the gate
    # itself reads, not a second independent binding of it.
    local_today = utc_to_local(get_utc_now(), DISPLAY_TIMEZONE).date()

    candidates = (
        Order.query.outerjoin(Delivery, Delivery.order_id == Order.id)
        .filter(
            Delivery.id.is_(None),
            Order.delivery_date.isnot(None),
            Order.delivery_date <= local_today,
            Order.status.in_(RELEASABLE_ORDER_STATUSES),
        )
        .all()
    )

    released = failed = awaiting = 0
    now = get_utc_now()
    for order in candidates:
        try:
            # The gate owns the decision; this loop must not re-test
            # `release_at <= now` itself. A second copy of that comparison is
            # exactly the two-places-deciding-one-thing failure mode.
            if OrderScheduleService.ensure_delivery_if_due(order) is None:
                awaiting += 1
                continue
            # `released` is incremented last, after the late-warning check, so
            # the two counters stay disjoint by construction: an order only
            # ever lands in `released` once the entire try body — including
            # this lookup — has completed without raising. If this block ever
            # did raise, the order must land in `failed` alone, not both.
            release_at = OrderScheduleService.release_at(order)
            if release_at and (now - release_at).total_seconds() > 900:
                logger.warning(
                    "Order %s released %.0f min late — check celery beat health",
                    order.id,
                    (now - release_at).total_seconds() / 60,
                )
            released += 1
        except Exception:
            db.session.rollback()
            failed += 1
            logger.exception("Failed to release scheduled order %s", order.id)

    # Logged on EVERY tick, including quiet ones. A future-dated order is
    # invisible by design, so "nothing happened" and "the sweep is broken" look
    # identical from the outside — this line is the only routine signal that
    # separates them, and it is what prod forensics query in Loki.
    logger.info(
        "scheduled_order_release released=%d failed=%d awaiting=%d",
        released,
        failed,
        awaiting,
    )
    return {"released": released, "failed": failed, "awaiting": awaiting}
