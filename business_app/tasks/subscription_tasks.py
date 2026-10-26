"""
Subscription Celery tasks.

The daily job creates every subscription order the lead before its delivery, and the order for a
first delivery (or the first after a resume) inside the lead window is created at once by
`create_subscription_delivery_task`. The rule itself lives in `SubscriptionService.create_next_order`
and `business_app/services/subscription_schedule.py` (spec 2026-10-23 §4.3). The customer is told by
`send_subscription_notice_task`, published after each commit (§9).
"""

from celery import shared_task
from celery.utils.log import get_task_logger
from datetime import datetime, timezone, timedelta

from business_app.models.subscription import Subscription
from business_app.models.user import User
from business_app.models.order import Order
from business_app.services.subscription_service import SubscriptionService
from business_app.services.notification_service import NotificationService
from business_app.utils.exceptions import ValidationError
from shared.enums import SubscriptionStatus, UserRole, UserStatus
from business_app.utils.helpers import get_current_language
from business_app.utils.local_windows import local_date

logger = get_task_logger(__name__)


@shared_task(bind=True, max_retries=3, time_limit=600, soft_time_limit=540)
def create_subscription_delivery_task(self, subscription_id: int):
    """Create the order for a first delivery inside the lead window, at creation or resume (§4.3, §4.6).

    The DELIVERY is NOT created here. Delivery creation belongs to the
    CONFIRMED status transition (order_service._handle_status_change_actions),
    exactly as it does for an ordinary order, and a dated order is held until
    its day by OrderScheduleService.
    """
    try:
        result = SubscriptionService().create_next_order(subscription_id)
    except ValidationError as exc:  # paused or cancelled in the meantime: nothing to create
        logger.info("Subscription %s: no first order: %s", subscription_id, exc)
        return {"success": False, "subscription_id": subscription_id, "error": str(exc)}
    except Exception as exc:
        logger.error(f"Failed to create the first order of subscription {subscription_id}: {exc}")
        raise self.retry(exc=exc)
    logger.info("Subscription %s first order: %s", subscription_id, result)
    return result


@shared_task(bind=True, max_retries=3, default_retry_delay=60, time_limit=120, soft_time_limit=100)
def send_subscription_notice_task(self, log_id: int):
    """Send the notice one committed `SubscriptionLog` row owes (spec 2026-10-23 §9). Published by
    `subscription_notices.deliver` only after the row's transaction committed. A bot failure retries; the bot
    keeps it one message per notice id. Email is one attempt, never retried."""
    from business_app.services import customer_notices, subscription_notices

    try:
        return subscription_notices.send(log_id)
    except customer_notices.RetryableNoticeError as exc:
        logger.warning("Subscription notice for log %s will be retried: %s", log_id, exc)
        raise self.retry(exc=exc)


@shared_task(time_limit=600, soft_time_limit=540)
def process_daily_subscription_billing():
    """The daily 09:00 job: create every subscription order whose delivery is due by the lead (§4.3)."""
    try:
        results = SubscriptionService().create_due_orders()
        logger.info(
            "Daily subscription orders: %s created, %s deliveries skipped, %s failed",
            results["successful"],
            results["skipped"],
            results["failed"],
        )
        return results
    except Exception as e:
        logger.error(f"Failed to process daily subscription orders: {e}")
        return {"error": str(e)}


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
    if subscription.last_delivery_date:
        days_since_billing = (local_date() - subscription.last_delivery_date).days
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
