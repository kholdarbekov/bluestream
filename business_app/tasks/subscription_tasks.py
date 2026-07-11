"""
Subscription-related Celery tasks for the Water Business Platform
This file should be placed in business_app/tasks/subscription_tasks.py
"""

from celery import shared_task
from celery.utils.log import get_task_logger
from datetime import datetime, timezone, timedelta
from flask import current_app

from business_app.models.subscription import Subscription
from business_app.models.user import User
from business_app.models.order import Order
from business_app.services.subscription_service import SubscriptionService
from business_app.services.notification_service import NotificationService
from business_app.services.order_service import OrderService
from shared.enums import SubscriptionStatus, OrderStatus, PaymentMethod, PaymentStatus, UserRole, UserStatus
from business_app.utils.helpers import get_current_language
from business_app import db

logger = get_task_logger(__name__)


@shared_task(bind=True, max_retries=3, time_limit=600, soft_time_limit=540)
def schedule_subscription_delivery_task(self, subscription_id: int):
    """Schedule delivery for subscription"""
    try:
        logger.info(f"Scheduling delivery for subscription {subscription_id}")

        subscription = Subscription.query.get(subscription_id)
        if not subscription:
            logger.error(f"Subscription {subscription_id} not found")
            return {"success": False, "error": "Subscription not found"}

        if subscription.status != SubscriptionStatus.ACTIVE:
            logger.info(f"Subscription {subscription_id} is not active")
            return {"success": False, "error": "Subscription not active"}

        # Calculate next delivery date based on frequency
        next_delivery_date = subscription.next_billing_date

        # Schedule the actual delivery creation closer to the delivery date
        # For now, create delivery 1 day before scheduled date
        schedule_time = next_delivery_date - timedelta(days=1)

        if schedule_time <= datetime.now(timezone.utc):
            # Create delivery immediately
            create_subscription_delivery_task.delay(subscription_id)
        else:
            # Schedule for later
            create_subscription_delivery_task.apply_async(args=[subscription_id], eta=schedule_time)

        logger.info(f"Delivery scheduled for subscription {subscription_id}")
        return {"success": True, "next_delivery_date": next_delivery_date.isoformat()}

    except Exception as exc:
        logger.error(f"Failed to schedule subscription delivery: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, time_limit=600, soft_time_limit=540)
def create_subscription_delivery_task(self, subscription_id: int):
    """Generate this subscription's order for the current cycle.

    The DELIVERY is NOT created here. Delivery creation belongs to the
    CONFIRMED status transition (order_service._handle_status_change_actions),
    exactly as it does for an ordinary order. A subscription order that is
    still PENDING — a first-time COD customer, or an unpaid click order — has
    no delivery yet, and that is correct: the auto_confirm_pending_orders
    backstop or the customer's payment will confirm it.
    """
    try:
        logger.info(f"Billing subscription {subscription_id}")

        subscription_service = SubscriptionService()
        billing_result = subscription_service.process_subscription_billing(subscription_id)

        if not billing_result.get("success"):
            logger.error(f"Billing failed for subscription {subscription_id}: {billing_result.get('error')}")
            return billing_result

        if billing_result.get("skipped"):
            # Already billed this cycle — there is no order_id to report.
            logger.info(f"Subscription {subscription_id} skipped: {billing_result.get('reason')}")
            return billing_result

        order_id = billing_result["order_id"]
        order = Order.query.get(order_id)
        delivery_id = order.delivery.id if order is not None and order.delivery is not None else None

        logger.info(
            "Subscription %s billed: order=%s delivery=%s",
            subscription_id,
            order_id,
            delivery_id,
        )
        return {
            "success": True,
            "subscription_id": subscription_id,
            "order_id": order_id,
            "delivery_id": delivery_id,
        }

    except Exception as exc:
        logger.error(f"Failed to bill subscription {subscription_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task(time_limit=600, soft_time_limit=540)
def process_daily_subscription_billing():
    """Process subscription billing for all due subscriptions"""
    try:
        logger.info("Processing daily subscription billing")

        # Get subscriptions due for billing today
        today = datetime.now(timezone.utc).date()
        next_day_start = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)

        due_subscriptions = Subscription.query.filter(
            Subscription.next_billing_date < next_day_start,
            Subscription.status == SubscriptionStatus.ACTIVE,
        ).all()

        results = {"total_processed": 0, "successful": 0, "failed": 0, "errors": []}

        subscription_service = SubscriptionService()

        for subscription in due_subscriptions:
            try:
                billing_result = subscription_service.process_subscription_billing(subscription.id)

                results["total_processed"] += 1

                if billing_result["success"]:
                    results["successful"] += 1
                    logger.info(f"Billing successful for subscription {subscription.id}")
                else:
                    results["failed"] += 1
                    results["errors"].append({"subscription_id": subscription.id, "error": billing_result.get("error")})
                    logger.error(f"Billing failed for subscription {subscription.id}")

            except Exception as e:
                results["failed"] += 1
                results["errors"].append({"subscription_id": subscription.id, "error": str(e)})
                logger.error(f"Exception processing subscription {subscription.id}: {e}")
                continue

        logger.info(f"Daily billing completed: {results['successful']} successful, {results['failed']} failed")
        return results

    except Exception as e:
        logger.error(f"Failed to process daily subscription billing: {e}")
        return {"error": str(e)}


@shared_task(time_limit=600, soft_time_limit=540)
def send_renewal_reminders():
    """Send subscription renewal reminders"""
    try:
        logger.info("Sending subscription renewal reminders")

        # Send reminders 3 days before renewal
        reminder_date = datetime.now(timezone.utc).date() + timedelta(days=3)
        reminder_start = datetime.combine(reminder_date, datetime.min.time(), tzinfo=timezone.utc)
        reminder_end = reminder_start + timedelta(days=1)

        upcoming_renewals = Subscription.query.filter(
            Subscription.next_billing_date >= reminder_start,
            Subscription.next_billing_date < reminder_end,
            Subscription.status == SubscriptionStatus.ACTIVE,
        ).all()

        notification_service = NotificationService()
        sent_count = 0

        for subscription in upcoming_renewals:
            try:
                template_data = {
                    "subscription_id": subscription.id,
                    "plan_name": subscription.name,
                    "renewal_date": subscription.next_billing_date.isoformat(),
                    "amount": subscription.billing_amount,
                    "frequency": (
                        subscription.billing_cycle.value
                        if hasattr(subscription.billing_cycle, "value")
                        else subscription.billing_cycle
                    ),
                }

                notification_service.send_notification(
                    subscription.user_id, "subscription_renewal_reminder", template_data=template_data
                )

                sent_count += 1

            except Exception as e:
                logger.error(f"Failed to send renewal reminder for subscription {subscription.id}: {e}")
                continue

        logger.info(f"Sent {sent_count} renewal reminders")
        return {"sent_count": sent_count}

    except Exception as e:
        logger.error(f"Failed to send renewal reminders: {e}")
        return {"error": str(e)}


@shared_task(time_limit=600, soft_time_limit=540)
def handle_failed_subscription_payments():
    """Handle failed subscription payments and retry logic"""
    try:
        logger.info("Handling failed subscription payments")

        # Get subscriptions with failed payments in last 24 hours
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)

        failed_subscriptions = Subscription.query.filter(
            Subscription.last_billing_date >= cutoff_time,
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.failed_payment_count > 0,
        ).all()

        notification_service = NotificationService()
        retry_count = 0
        # Single threshold governs both the retry and pause boundaries so they
        # can never drift apart (SSOT: Config.SUBSCRIPTION_FAILED_PAYMENT_MAX_ATTEMPTS).
        max_failed_attempts = current_app.config["SUBSCRIPTION_FAILED_PAYMENT_MAX_ATTEMPTS"]

        for subscription in failed_subscriptions:
            try:
                # Retry payment while still under the max failed-attempt budget
                if subscription.failed_payment_count < max_failed_attempts:
                    # Retry billing
                    subscription_service = SubscriptionService()
                    billing_result = subscription_service.process_subscription_billing(subscription.id)

                    if billing_result["success"]:
                        # Reset failed payment count
                        subscription.failed_payment_count = 0
                        db.session.commit()

                        # Send success notification
                        notification_service.send_notification(
                            subscription.user_id,
                            "payment_retry_success",
                            template_data={
                                "subscription_id": subscription.id,
                                "amount": float(subscription.billing_amount or 0),
                            },
                        )

                        retry_count += 1
                        logger.info(f"Payment retry successful for subscription {subscription.id}")
                    else:
                        # Increment failed payment count
                        subscription.failed_payment_count += 1

                        # Pause subscription once the max failed-attempt budget is reached
                        if subscription.failed_payment_count >= max_failed_attempts:
                            subscription_service = SubscriptionService()
                            subscription_service.pause_subscription(
                                subscription.id, reason="Multiple failed payment attempts"
                            )

                            # Send suspension notification
                            notification_service.send_notification(
                                subscription.user_id,
                                "subscription_suspended",
                                template_data={"subscription_id": subscription.id, "reason": "Payment failures"},
                            )

                        db.session.commit()

            except Exception as e:
                logger.error(f"Failed to handle payment failure for subscription {subscription.id}: {e}")
                continue

        logger.info(f"Handled failed payments: {retry_count} successful retries")
        return {"retry_count": retry_count, "processed_subscriptions": len(failed_subscriptions)}

    except Exception as e:
        logger.error(f"Failed to handle failed subscription payments: {e}")
        return {"error": str(e)}


@shared_task(bind=True, max_retries=2, time_limit=600, soft_time_limit=540)
def cancel_subscription_deliveries_task(self, subscription_id: int):
    """Cancel pending deliveries for a paused/cancelled subscription"""
    try:
        logger.info(f"Cancelling deliveries for subscription {subscription_id}")

        subscription = Subscription.query.get(subscription_id)
        if not subscription:
            logger.error(f"Subscription {subscription_id} not found")
            return {"success": False, "error": "Subscription not found"}

        # Order.subscription_id is authoritative and set inside create_order's
        # transaction. The previous filter referenced Order.notes — a column
        # that does not exist on Order (only delivery_notes does) — so the
        # query raised AttributeError unconditionally at build time. This task
        # never completed successfully, regardless of order contents.
        pending_orders = Order.query.filter(
            Order.subscription_id == subscription_id,
            Order.status.in_([OrderStatus.PENDING, OrderStatus.CONFIRMED]),
        ).all()

        cancelled_count = 0

        for order in pending_orders:
            try:
                # Cancel the order
                order_service = OrderService()
                order_service.cancel_order(order.id, reason="Subscription cancelled/paused")

                cancelled_count += 1

            except Exception as e:
                logger.error(f"Failed to cancel order {order.id}: {e}")
                continue

        logger.info(f"Cancelled {cancelled_count} deliveries for subscription {subscription_id}")
        return {"success": True, "cancelled_deliveries": cancelled_count}

    except Exception as exc:
        logger.error(f"Failed to cancel subscription deliveries: {exc}")
        raise self.retry(exc=exc)


@shared_task(time_limit=600, soft_time_limit=540)
def generate_subscription_churn_prediction():
    """Generate churn predictions for subscription customers"""
    try:
        logger.info("Generating subscription churn predictions")

        # Get all active subscriptions
        active_subscriptions = Subscription.query.filter_by(status=SubscriptionStatus.ACTIVE).all()

        churn_predictions = []
        high_risk_count = 0

        for subscription in active_subscriptions:
            try:
                # Calculate churn risk factors
                risk_score = _calculate_subscription_churn_risk(subscription)

                if risk_score > 0.7:  # High risk threshold
                    risk_level = "high"
                    high_risk_count += 1
                elif risk_score > 0.4:  # Medium risk threshold
                    risk_level = "medium"
                else:
                    risk_level = "low"

                if risk_level in ["high", "medium"]:  # Only track medium and high risk
                    churn_predictions.append(
                        {
                            "subscription_id": subscription.id,
                            "user_id": subscription.user_id,
                            "user_name": f"{subscription.user.first_name} {subscription.user.last_name}",
                            "risk_score": round(risk_score, 3),
                            "risk_level": risk_level,
                            "plan_name": (
                                subscription.plan.get_translated("name", get_current_language())
                                if subscription.plan
                                else "Standard"
                            ),
                            "monthly_value": float(subscription.billing_amount or 0),
                        }
                    )

            except Exception as e:
                logger.error(f"Failed to calculate churn risk for subscription {subscription.id}: {e}")
                continue

        # Sort by risk score
        churn_predictions.sort(key=lambda x: x["risk_score"], reverse=True)

        # Send alerts for high-risk customers
        if high_risk_count > 0:
            admin_users = User.query.filter(
                User.role.in_([UserRole.ADMIN, UserRole.MANAGER]), User.status == UserStatus.ACTIVE
            ).all()

            notification_service = NotificationService()

            for admin in admin_users:
                notification_service.send_notification(
                    admin.id,
                    "subscription_churn_alert",
                    template_data={
                        "high_risk_count": high_risk_count,
                        "total_at_risk": len(churn_predictions),
                        "top_risk_customers": churn_predictions[:5],  # Top 5 at-risk
                    },
                )

        # Store predictions
        from business_app.services.analytics_service import AnalyticsService

        analytics_service = AnalyticsService()
        analytics_service.store_subscription_churn_predictions(churn_predictions)

        logger.info(f"Churn prediction completed: {high_risk_count} high-risk subscriptions identified")
        return {
            "total_analyzed": len(active_subscriptions),
            "high_risk_count": high_risk_count,
            "predictions": churn_predictions[:20],  # Return top 20 for response
        }

    except Exception as e:
        logger.error(f"Failed to generate subscription churn predictions: {e}")
        return {"error": str(e)}


def _calculate_subscription_churn_risk(subscription: Subscription) -> float:
    """Calculate churn risk score for a subscription"""
    risk_factors = {
        "payment_failures": 0,
        "usage_decline": 0,
        "support_issues": 0,
        "engagement_drop": 0,
        "plan_downgrades": 0,
    }

    # Payment failure history
    if subscription.failed_payment_count > 0:
        risk_factors["payment_failures"] = min(subscription.failed_payment_count / 3, 1.0)

    # Usage decline (based on order frequency)
    recent_orders = Order.query.filter(
        Order.user_id == subscription.user_id, Order.created_at >= datetime.now(timezone.utc) - timedelta(days=30)
    ).count()

    older_orders = Order.query.filter(
        Order.user_id == subscription.user_id,
        Order.created_at.between(
            datetime.now(timezone.utc) - timedelta(days=60), datetime.now(timezone.utc) - timedelta(days=30)
        ),
    ).count()

    if older_orders > 0 and recent_orders < older_orders * 0.7:
        risk_factors["usage_decline"] = 0.3

    # Plan downgrades
    if hasattr(subscription, "plan_downgrades") and subscription.plan_downgrades > 0:
        risk_factors["plan_downgrades"] = 0.2

    # Time since last interaction
    if subscription.last_billing_date:
        days_since_billing = (datetime.now(timezone.utc) - subscription.last_billing_date).days
        if days_since_billing > 35:  # More than expected for monthly billing
            risk_factors["engagement_drop"] = 0.2

    # Calculate weighted risk score
    weights = {
        "payment_failures": 0.4,
        "usage_decline": 0.3,
        "support_issues": 0.1,
        "engagement_drop": 0.15,
        "plan_downgrades": 0.05,
    }

    risk_score = sum(risk_factors[factor] * weights[factor] for factor in risk_factors)
    return min(1.0, max(0.0, risk_score))


@shared_task(bind=True, max_retries=3, default_retry_delay=300, time_limit=600, soft_time_limit=540)
def process_subscription_billing(self, subscription_id: int):
    """Process billing for a subscription"""
    try:
        logger.info(f"Processing billing for subscription {subscription_id}")

        subscription_service = SubscriptionService()
        subscription = Subscription.query.get(subscription_id)

        if not subscription:
            logger.error(f"Subscription {subscription_id} not found")
            return {"success": False, "error": "Subscription not found"}

        if subscription.status != SubscriptionStatus.ACTIVE:
            logger.info(f"Subscription {subscription_id} is not active, skipping billing")
            return {"success": False, "error": "Subscription not active"}

        # Process billing through subscription service
        billing_result = subscription_service.process_subscription_billing(subscription_id)

        if billing_result.get("success"):
            logger.info(f"Billing processed successfully for subscription {subscription_id}")

            # Forward-looking data for a future subscription_billed notification
            # template (that follow-up is explicitly descoped for now — see
            # Task 20). Guarded end-to-end: the "already billed this cycle"
            # success path has no order_id, and the order/payment rows may not
            # exist even when it is present, so neither access is assumed safe.
            order_number = None
            payment_action_required = False
            billed_order = Order.query.get(billing_result["order_id"]) if billing_result.get("order_id") else None
            if billed_order:
                order_number = billed_order.order_number
                payment = billed_order.payment
                if payment and payment.payment_method in (PaymentMethod.CLICK, PaymentMethod.PAYME):
                    payment_action_required = payment.status != PaymentStatus.COMPLETED

            # Send billing notification
            notification_service = NotificationService()
            notification_service.send_notification(
                subscription.user_id,
                "subscription_billed",
                template_data={
                    "subscription_name": subscription.get_translated("name", get_current_language()),
                    "billing_amount": subscription.billing_amount,
                    "next_billing_date": (
                        subscription.next_billing_date.isoformat() if subscription.next_billing_date else None
                    ),
                    "order_number": order_number,
                    "payment_action_required": payment_action_required,
                },
            )

            # Schedule next billing
            if subscription.auto_renew and subscription.next_billing_date:
                next_billing_time = subscription.next_billing_date
                process_subscription_billing.apply_async(args=[subscription_id], eta=next_billing_time)

            return billing_result
        else:
            logger.error(f"Billing failed for subscription {subscription_id}: {billing_result.get('error')}")

            # Send billing failure notification
            notification_service = NotificationService()
            notification_service.send_notification(
                subscription.user_id,
                "subscription_billing_failed",
                template_data={
                    "subscription_name": subscription.get_translated("name", get_current_language()),
                    "billing_amount": subscription.billing_amount,
                    "error_message": billing_result.get("error", "Unknown error"),
                },
            )

            return billing_result

    except Exception as exc:
        logger.error(f"Subscription billing processing failed: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, time_limit=600, soft_time_limit=540)
def send_subscription_reminder(self, subscription_id: int, reminder_type: str):
    """Send subscription-related reminders"""
    try:
        logger.info(f"Sending {reminder_type} reminder for subscription {subscription_id}")

        subscription = Subscription.query.get(subscription_id)

        if not subscription:
            logger.error(f"Subscription {subscription_id} not found")
            return {"success": False, "error": "Subscription not found"}

        notification_service = NotificationService()

        # Determine notification type and template data based on reminder type
        if reminder_type == "upcoming_billing":
            notification_type = "subscription_billing_reminder"
            template_data = {
                "subscription_name": subscription.name,
                "billing_amount": subscription.billing_amount,
                "billing_date": subscription.next_billing_date.isoformat() if subscription.next_billing_date else None,
                "days_until_billing": (
                    (subscription.next_billing_date - datetime.now(timezone.utc)).days
                    if subscription.next_billing_date
                    else None
                ),
            }

        elif reminder_type == "upcoming_delivery":
            notification_type = "subscription_delivery_reminder"
            # Include time slot details if available
            time_slot_info = None
            if subscription.delivery_time_slot:
                time_slot_info = (
                    f"{subscription.delivery_time_slot.start_time} - {subscription.delivery_time_slot.end_time}"
                )

            template_data = {
                "subscription_name": subscription.name,
                "delivery_frequency": (
                    subscription.delivery_frequency.value
                    if hasattr(subscription.delivery_frequency, "value")
                    else str(subscription.delivery_frequency)
                ),
                "delivery_time_slot": time_slot_info or "Flexible",
            }

        elif reminder_type == "renewal_due":
            notification_type = "subscription_renewal_reminder"
            template_data = {
                "subscription_name": subscription.name,
                "end_date": subscription.end_date.isoformat() if subscription.end_date else None,
                "auto_renew": subscription.auto_renew,
            }

        elif reminder_type == "payment_failed":
            notification_type = "subscription_payment_failed"
            template_data = {
                "subscription_name": subscription.name,
                "billing_amount": subscription.billing_amount,
                "failed_date": datetime.now(timezone.utc).isoformat(),
            }

        else:
            logger.error(f"Unknown reminder type: {reminder_type}")
            return {"success": False, "error": f"Unknown reminder type: {reminder_type}"}

        # Send notification
        result = notification_service.send_notification(
            subscription.user_id, notification_type, template_data=template_data
        )

        if result:
            logger.info(f"{reminder_type} reminder sent successfully for subscription {subscription_id}")
            return {"success": True, "reminder_type": reminder_type}
        else:
            logger.error(f"Failed to send {reminder_type} reminder for subscription {subscription_id}")
            return {"success": False, "error": "Failed to send notification"}

    except Exception as exc:
        logger.error(f"Subscription reminder sending failed: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, time_limit=600, soft_time_limit=540)
def resume_subscription_task(self, subscription_id: int, user_id: int = None):
    """Resume a paused subscription"""
    try:
        logger.info(f"Resuming subscription {subscription_id}")

        subscription = Subscription.query.get(subscription_id)

        if not subscription:
            logger.error(f"Subscription {subscription_id} not found")
            return {"success": False, "error": "Subscription not found"}

        # Verify user ownership if user_id provided
        if user_id and subscription.user_id != user_id:
            logger.error(f"User {user_id} does not own subscription {subscription_id}")
            return {"success": False, "error": "Unauthorized"}

        if subscription.status != SubscriptionStatus.PAUSED:
            logger.warning(f"Subscription {subscription_id} is not paused (status: {subscription.status})")
            return {"success": False, "error": f"Subscription is not paused (current status: {subscription.status})"}

        # Resume the subscription
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.pause_start_date = None
        subscription.pause_end_date = None
        subscription.updated_at = datetime.now(timezone.utc)

        # If next billing date has passed, calculate new billing date
        if subscription.next_billing_date and subscription.next_billing_date < datetime.now(timezone.utc).date():
            # Calculate next billing date from today
            subscription_service = SubscriptionService()
            from shared.enums import SubscriptionFrequency

            billing_cycle = subscription.billing_cycle or SubscriptionFrequency.MONTHLY
            subscription.next_billing_date = subscription_service._calculate_next_billing_date(
                datetime.now(timezone.utc), billing_cycle
            )

        # If next delivery date has passed, calculate new delivery date
        if subscription.next_delivery_date and subscription.next_delivery_date < datetime.now(timezone.utc).date():
            subscription_service = SubscriptionService()
            from shared.enums import SubscriptionFrequency

            delivery_frequency = subscription.delivery_frequency or SubscriptionFrequency.WEEKLY
            subscription.next_delivery_date = subscription_service._calculate_next_delivery_date(
                datetime.now(timezone.utc),
                delivery_frequency,
                subscription.delivery_day_of_week,
                subscription.delivery_day_of_month,
            )

        # Reset failed payment count on resume
        subscription.failed_payment_count = 0

        db.session.commit()

        # Send resume notification
        notification_service = NotificationService()
        notification_service.send_notification(
            subscription.user_id,
            "subscription_resumed",
            template_data={
                "subscription_name": subscription.name,
                "next_billing_date": (
                    subscription.next_billing_date.isoformat() if subscription.next_billing_date else None
                ),
                "next_delivery_date": (
                    subscription.next_delivery_date.isoformat() if subscription.next_delivery_date else None
                ),
            },
        )

        # Schedule next billing if auto-renew enabled
        if subscription.auto_renew and subscription.next_billing_date:
            process_subscription_billing.apply_async(args=[subscription_id], eta=subscription.next_billing_date)

        # Schedule next delivery
        if subscription.next_delivery_date:
            create_subscription_delivery_task.apply_async(
                args=[subscription_id], eta=datetime.combine(subscription.next_delivery_date, datetime.min.time())
            )

        logger.info(f"Subscription {subscription_id} resumed successfully")
        return {
            "success": True,
            "subscription_id": subscription_id,
            "next_billing_date": subscription.next_billing_date.isoformat() if subscription.next_billing_date else None,
            "next_delivery_date": (
                subscription.next_delivery_date.isoformat() if subscription.next_delivery_date else None
            ),
        }

    except Exception as exc:
        logger.error(f"Failed to resume subscription: {exc}")
        raise self.retry(exc=exc)
