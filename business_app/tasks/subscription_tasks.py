"""
Subscription-related Celery tasks for the Water Business Platform
This file should be placed in business_app/tasks/subscription_tasks.py
"""
from celery import shared_task
from celery.utils.log import get_task_logger
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List
from flask import current_app
from sqlalchemy import func, and_, or_, text

from business_app.models.subscription import Subscription
from business_app.models.user import User
from business_app.models.order import Order
from business_app.services.subscription_service import SubscriptionService
from business_app.services.notification_service import NotificationService
from business_app.services.order_service import OrderService
from business_app.services.payment_service import PaymentService
from business_app.utils.constants import SubscriptionStatus, OrderStatus, UserRole
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
            return {'success': False, 'error': 'Subscription not found'}
        
        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL]:
            logger.info(f"Subscription {subscription_id} is not active")
            return {'success': False, 'error': 'Subscription not active'}
        
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
            create_subscription_delivery_task.apply_async(
                args=[subscription_id],
                eta=schedule_time
            )
        
        logger.info(f"Delivery scheduled for subscription {subscription_id}")
        return {'success': True, 'next_delivery_date': next_delivery_date.isoformat()}
        
    except Exception as exc:
        logger.error(f"Failed to schedule subscription delivery: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, time_limit=600, soft_time_limit=540)
def create_subscription_delivery_task(self, subscription_id: int):
    """Create order and delivery for subscription"""
    try:
        logger.info(f"Creating delivery for subscription {subscription_id}")
        
        subscription_service = SubscriptionService()
        
        # Process subscription billing (creates order)
        billing_result = subscription_service.process_subscription_billing(subscription_id)
        
        if billing_result['success']:
            order_id = billing_result['order_id']
            
            # Create delivery for the order
            from business_app.services.delivery_service import DeliveryService
            delivery_service = DeliveryService()
            
            delivery = delivery_service.create_delivery(order_id)
            
            logger.info(f"Delivery created for subscription {subscription_id}, order {order_id}")
            return {
                'success': True,
                'subscription_id': subscription_id,
                'order_id': order_id,
                'delivery_id': delivery.id
            }
        else:
            logger.error(f"Billing failed for subscription {subscription_id}: {billing_result.get('error')}")
            return billing_result
            
    except Exception as exc:
        logger.error(f"Failed to create subscription delivery: {exc}")
        raise self.retry(exc=exc)


@shared_task(time_limit=600, soft_time_limit=540)
def process_daily_subscription_billing():
    """Process subscription billing for all due subscriptions"""
    try:
        logger.info("Processing daily subscription billing")
        
        # Get subscriptions due for billing today
        today = datetime.now(timezone.utc).date()
        
        due_subscriptions = Subscription.query.filter(
            Subscription.next_billing_date <= today,
            Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL])
        ).all()
        
        results = {
            'total_processed': 0,
            'successful': 0,
            'failed': 0,
            'errors': []
        }
        
        subscription_service = SubscriptionService()
        
        for subscription in due_subscriptions:
            try:
                billing_result = subscription_service.process_subscription_billing(subscription.id)
                
                results['total_processed'] += 1
                
                if billing_result['success']:
                    results['successful'] += 1
                    logger.info(f"Billing successful for subscription {subscription.id}")
                else:
                    results['failed'] += 1
                    results['errors'].append({
                        'subscription_id': subscription.id,
                        'error': billing_result.get('error')
                    })
                    logger.error(f"Billing failed for subscription {subscription.id}")
                    
            except Exception as e:
                results['failed'] += 1
                results['errors'].append({
                    'subscription_id': subscription.id,
                    'error': str(e)
                })
                logger.error(f"Exception processing subscription {subscription.id}: {e}")
                continue
        
        logger.info(f"Daily billing completed: {results['successful']} successful, {results['failed']} failed")
        return results
        
    except Exception as e:
        logger.error(f"Failed to process daily subscription billing: {e}")
        return {'error': str(e)}


@shared_task(time_limit=600, soft_time_limit=540)
def send_renewal_reminders():
    """Send subscription renewal reminders"""
    try:
        logger.info("Sending subscription renewal reminders")
        
        # Send reminders 3 days before renewal
        reminder_date = datetime.now(timezone.utc).date() + timedelta(days=3)
        
        upcoming_renewals = Subscription.query.filter(
            Subscription.next_billing_date == reminder_date,
            Subscription.status == SubscriptionStatus.ACTIVE
        ).all()
        
        notification_service = NotificationService()
        sent_count = 0
        
        for subscription in upcoming_renewals:
            try:
                language = get_current_language()
                template_data = {
                    'subscription_id': subscription.id,
                    'plan_name': subscription.plan.get_translated('name', language) if subscription.plan else 'Standard',
                    'renewal_date': subscription.next_billing_date.isoformat(),
                    'amount': subscription.total_amount,
                    'frequency': subscription.frequency.value
                }
                
                notification_service.send_notification(
                    subscription.user_id,
                    'subscription_renewal_reminder',
                    template_data=template_data
                )
                
                sent_count += 1
                
            except Exception as e:
                logger.error(f"Failed to send renewal reminder for subscription {subscription.id}: {e}")
                continue
        
        logger.info(f"Sent {sent_count} renewal reminders")
        return {'sent_count': sent_count}
        
    except Exception as e:
        logger.error(f"Failed to send renewal reminders: {e}")
        return {'error': str(e)}


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
            Subscription.failed_payment_count > 0
        ).all()
        
        notification_service = NotificationService()
        retry_count = 0
        
        for subscription in failed_subscriptions:
            try:
                # Retry payment for subscriptions with 1-2 failed attempts
                if subscription.failed_payment_count <= 2:
                    # Retry billing
                    subscription_service = SubscriptionService()
                    billing_result = subscription_service.process_subscription_billing(subscription.id)
                    
                    if billing_result['success']:
                        # Reset failed payment count
                        subscription.failed_payment_count = 0
                        db.session.commit()
                        
                        # Send success notification
                        notification_service.send_notification(
                            subscription.user_id,
                            'payment_retry_success',
                            template_data={
                                'subscription_id': subscription.id,
                                'amount': subscription.total_amount
                            }
                        )
                        
                        retry_count += 1
                        logger.info(f"Payment retry successful for subscription {subscription.id}")
                    else:
                        # Increment failed payment count
                        subscription.failed_payment_count += 1
                        
                        # Pause subscription after 3 failed attempts
                        if subscription.failed_payment_count >= 3:
                            subscription_service = SubscriptionService()
                            subscription_service.pause_subscription(
                                subscription.id,
                                reason="Multiple failed payment attempts"
                            )
                            
                            # Send suspension notification
                            notification_service.send_notification(
                                subscription.user_id,
                                'subscription_suspended',
                                template_data={
                                    'subscription_id': subscription.id,
                                    'reason': 'Payment failures'
                                }
                            )
                        
                        db.session.commit()
                
            except Exception as e:
                logger.error(f"Failed to handle payment failure for subscription {subscription.id}: {e}")
                continue
        
        logger.info(f"Handled failed payments: {retry_count} successful retries")
        return {'retry_count': retry_count, 'processed_subscriptions': len(failed_subscriptions)}
        
    except Exception as e:
        logger.error(f"Failed to handle failed subscription payments: {e}")
        return {'error': str(e)}


@shared_task(bind=True, max_retries=2, time_limit=600, soft_time_limit=540)
def cancel_subscription_deliveries_task(self, subscription_id: int):
    """Cancel pending deliveries for a paused/cancelled subscription"""
    try:
        logger.info(f"Cancelling deliveries for subscription {subscription_id}")
        
        subscription = Subscription.query.get(subscription_id)
        if not subscription:
            logger.error(f"Subscription {subscription_id} not found")
            return {'success': False, 'error': 'Subscription not found'}
        
        # Find pending orders and deliveries for this subscription
        pending_orders = Order.query.filter(
            Order.user_id == subscription.user_id,
            Order.status.in_([OrderStatus.PENDING, OrderStatus.CONFIRMED]),
            Order.notes.contains(f"Subscription order #{subscription_id}")
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
        return {'success': True, 'cancelled_deliveries': cancelled_count}
        
    except Exception as exc:
        logger.error(f"Failed to cancel subscription deliveries: {exc}")
        raise self.retry(exc=exc)


@shared_task(time_limit=600, soft_time_limit=540)
def update_subscription_analytics():
    """Update subscription analytics and metrics"""
    try:
        logger.info("Updating subscription analytics")
        
        subscription_service = SubscriptionService()
        
        # Generate subscription analytics
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=30)
        
        analytics = subscription_service.get_subscription_analytics(start_date, end_date)
        
        # Calculate additional metrics
        from sqlalchemy import func
        
        # Monthly recurring revenue
        mrr = db.session.query(func.sum(Subscription.total_amount)).filter(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.frequency == 'monthly'
        ).scalar() or 0
        
        # Annual recurring revenue
        arr = mrr * 12
        
        # Average revenue per user
        active_subscriptions = Subscription.query.filter_by(status=SubscriptionStatus.ACTIVE).count()
        arpu = mrr / active_subscriptions if active_subscriptions > 0 else 0
        
        # Subscription lifecycle metrics
        trial_conversions = db.session.query(func.count(Subscription.id)).filter(
            Subscription.created_at.between(start_date, end_date),
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.trial_end_date.isnot(None)
        ).scalar()
        
        total_trials = db.session.query(func.count(Subscription.id)).filter(
            Subscription.created_at.between(start_date, end_date),
            Subscription.trial_end_date.isnot(None)
        ).scalar()
        
        trial_conversion_rate = (trial_conversions / total_trials * 100) if total_trials > 0 else 0
        
        analytics.update({
            'monthly_recurring_revenue': float(mrr),
            'annual_recurring_revenue': float(arr),
            'average_revenue_per_user': round(arpu, 2),
            'trial_conversion_rate': round(trial_conversion_rate, 2),
            'updated_at': datetime.now(timezone.utc).isoformat()
        })
        
        # Store analytics
        from business_app.services.analytics_service import AnalyticsService
        analytics_service = AnalyticsService()
        analytics_service.store_subscription_analytics(analytics)
        
        logger.info("Subscription analytics updated successfully")
        return analytics
        
    except Exception as e:
        logger.error(f"Failed to update subscription analytics: {e}")
        return {'error': str(e)}


@shared_task(time_limit=600, soft_time_limit=540)
def process_subscription_upgrades_downgrades():
    """Process pending subscription plan changes"""
    try:
        logger.info("Processing subscription upgrades and downgrades")
        
        # This would handle plan changes that are scheduled for the next billing cycle
        # For now, implementing basic plan change logic
        
        # Find subscriptions with pending plan changes
        subscriptions_with_changes = Subscription.query.filter(
            Subscription.pending_plan_change_id.isnot(None),
            Subscription.next_billing_date <= datetime.now(timezone.utc).date()
        ).all()
        
        processed_count = 0
        
        for subscription in subscriptions_with_changes:
            try:
                new_plan = SubscriptionPlan.query.get(subscription.pending_plan_change_id)
                if not new_plan:
                    logger.error(f"New plan not found for subscription {subscription.id}")
                    continue
                
                language = get_current_language()
                old_plan_name = subscription.plan.get_translated('name', language) if subscription.plan else 'Unknown'
                
                # Update subscription plan
                subscription.plan_id = new_plan.id
                subscription.pending_plan_change_id = None
                subscription.plan_changed_at = datetime.now(timezone.utc)
                
                # Recalculate subscription total based on new plan
                subscription_service = SubscriptionService()
                new_total = subscription_service._calculate_subscription_total(
                    [{'product_id': item.product_id, 'quantity': item.quantity} for item in subscription.items],
                    new_plan
                )
                subscription.total_amount = new_total
                
                db.session.commit()
                
                # Send notification about plan change
                notification_service = NotificationService()
                notification_service.send_notification(
                    subscription.user_id,
                    'subscription_plan_changed',
                    template_data={
                        'subscription_id': subscription.id,
                        'old_plan': old_plan_name,
                        'new_plan': new_plan.get_translated('name', language),
                        'new_amount': new_total,
                        'effective_date': datetime.now(timezone.utc).date().isoformat()
                    }
                )
                
                processed_count += 1
                logger.info(f"Plan change processed for subscription {subscription.id}: {old_plan_name} -> {new_plan.get_translated('name', language)}")
                
            except Exception as e:
                logger.error(f"Failed to process plan change for subscription {subscription.id}: {e}")
                continue
        
        logger.info(f"Processed {processed_count} subscription plan changes")
        return {'processed_count': processed_count}
        
    except Exception as e:
        logger.error(f"Failed to process subscription upgrades/downgrades: {e}")
        return {'error': str(e)}


@shared_task(time_limit=600, soft_time_limit=540)
def cleanup_expired_trials():
    """Clean up expired trial subscriptions"""
    try:
        logger.info("Cleaning up expired trial subscriptions")
        
        # Find trial subscriptions that have expired
        expired_trials = Subscription.query.filter(
            Subscription.status == SubscriptionStatus.TRIAL,
            Subscription.trial_end_date < datetime.now(timezone.utc).date()
        ).all()
        
        converted_count = 0
        cancelled_count = 0
        
        subscription_service = SubscriptionService()
        notification_service = NotificationService()
        
        for subscription in expired_trials:
            try:
                # Check if user has added a payment method
                if subscription.payment_token:
                    # Convert to active subscription
                    subscription.status = SubscriptionStatus.ACTIVE
                    subscription.trial_end_date = None
                    subscription.updated_at = datetime.now(timezone.utc)
                    
                    converted_count += 1
                    
                    # Send conversion notification
                    notification_service.send_notification(
                        subscription.user_id,
                        'trial_converted',
                        template_data={
                            'subscription_id': subscription.id,
                            'plan_name': subscription.plan.get_translated('name', get_current_language()) if subscription.plan else 'Standard'
                        }
                    )
                    
                    logger.info(f"Trial converted to active for subscription {subscription.id}")
                    
                else:
                    # Cancel expired trial
                    subscription_service.cancel_subscription(
                        subscription.id,
                        reason="Trial period expired without payment method",
                        immediate=True
                    )
                    
                    cancelled_count += 1
                    logger.info(f"Expired trial cancelled for subscription {subscription.id}")
                
            except Exception as e:
                logger.error(f"Failed to process expired trial {subscription.id}: {e}")
                continue
        
        db.session.commit()
        
        logger.info(f"Trial cleanup completed: {converted_count} converted, {cancelled_count} cancelled")
        return {
            'converted_count': converted_count,
            'cancelled_count': cancelled_count,
            'total_processed': len(expired_trials)
        }
        
    except Exception as e:
        logger.error(f"Failed to cleanup expired trials: {e}")
        return {'error': str(e)}


@shared_task(bind=True, max_retries=3, time_limit=600, soft_time_limit=540)
def send_subscription_usage_summary(self, subscription_id: int):
    """Send monthly subscription usage summary to customer"""
    try:
        logger.info(f"Sending usage summary for subscription {subscription_id}")
        
        subscription = Subscription.query.get(subscription_id)
        if not subscription:
            logger.error(f"Subscription {subscription_id} not found")
            return {'success': False, 'error': 'Subscription not found'}
        
        # Calculate usage for the last billing period
        end_date = subscription.last_billing_date or datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=30)  # Assuming monthly billing
        
        # Get orders related to this subscription
        subscription_orders = Order.query.filter(
            Order.user_id == subscription.user_id,
            Order.created_at.between(start_date, end_date),
            Order.notes.contains(f"Subscription order #{subscription_id}")
        ).all()
        
        # Calculate usage metrics
        total_deliveries = len(subscription_orders)
        total_items = sum(len(order.order_items) for order in subscription_orders)
        total_value = sum(order.total_amount for order in subscription_orders)
        
        # Calculate savings (if applicable)
        estimated_regular_price = total_value * 1.15  # Assuming 15% subscription discount
        savings = estimated_regular_price - total_value
        
        usage_data = {
            'period': {
                'start_date': start_date.date().isoformat(),
                'end_date': end_date.date().isoformat()
            },
            'subscription_plan': subscription.plan.get_translated('name', get_current_language()) if subscription.plan else 'Standard',
            'total_deliveries': total_deliveries,
            'total_items': total_items,
            'total_value': total_value,
            'estimated_savings': savings,
            'next_billing_date': subscription.next_billing_date.isoformat() if subscription.next_billing_date else None
        }
        
        # Send usage summary notification
        notification_service = NotificationService()
        notification_service.send_notification(
            subscription.user_id,
            'subscription_usage_summary',
            template_data=usage_data
        )
        
        logger.info(f"Usage summary sent for subscription {subscription_id}")
        return {'success': True, 'usage_data': usage_data}
        
    except Exception as exc:
        logger.error(f"Failed to send usage summary: {exc}")
        raise self.retry(exc=exc)


@shared_task(time_limit=600, soft_time_limit=540)
def optimize_subscription_delivery_schedule():
    """Optimize delivery schedules for all active subscriptions"""
    try:
        logger.info("Optimizing subscription delivery schedules")
        
        # Get all active subscriptions
        active_subscriptions = Subscription.query.filter_by(
            status=SubscriptionStatus.ACTIVE
        ).all()
        
        # Group subscriptions by delivery area for optimization
        area_groups = {}
        
        for subscription in active_subscriptions:
            city = subscription.delivery_address_city
            if city not in area_groups:
                area_groups[city] = []
            area_groups[city].append(subscription)
        
        optimization_results = {}
        
        for city, subscriptions in area_groups.items():
            try:
                # Optimize delivery schedule for this area
                # Group subscriptions by preferred delivery day/time
                schedule_optimization = _optimize_area_delivery_schedule(subscriptions)
                optimization_results[city] = schedule_optimization
                
            except Exception as e:
                logger.error(f"Failed to optimize schedule for {city}: {e}")
                continue
        
        logger.info(f"Delivery schedule optimization completed for {len(area_groups)} areas")
        return optimization_results
        
    except Exception as e:
        logger.error(f"Failed to optimize subscription delivery schedules: {e}")
        return {'error': str(e)}


def _optimize_area_delivery_schedule(subscriptions: List[Subscription]) -> Dict[str, Any]:
    """Optimize delivery schedule for subscriptions in a specific area"""
    # Group by delivery frequency and preferred time
    frequency_groups = {}
    
    for subscription in subscriptions:
        freq = subscription.frequency.value
        if freq not in frequency_groups:
            frequency_groups[freq] = []
        frequency_groups[freq].append(subscription)
    
    # Calculate optimal delivery windows
    optimization = {
        'total_subscriptions': len(subscriptions),
        'frequency_distribution': {freq: len(subs) for freq, subs in frequency_groups.items()},
        'recommended_time_slots': [],
        'efficiency_score': 0
    }
    
    # Simple optimization: recommend time slots based on subscription density
    time_slot_demand = {}
    
    for subscription in subscriptions:
        preferred_time = subscription.preferred_delivery_time or '10:00-12:00'
        time_slot_demand[preferred_time] = time_slot_demand.get(preferred_time, 0) + 1
    
    # Sort time slots by demand
    sorted_slots = sorted(time_slot_demand.items(), key=lambda x: x[1], reverse=True)
    optimization['recommended_time_slots'] = [
        {'time_slot': slot, 'subscription_count': count}
        for slot, count in sorted_slots[:5]  # Top 5 time slots
    ]
    
    # Calculate efficiency score (higher is better)
    if subscriptions:
        max_demand = max(time_slot_demand.values()) if time_slot_demand else 0
        optimization['efficiency_score'] = round((max_demand / len(subscriptions)) * 100, 2)
    
    return optimization


@shared_task(time_limit=600, soft_time_limit=540)
def generate_subscription_churn_prediction():
    """Generate churn predictions for subscription customers"""
    try:
        logger.info("Generating subscription churn predictions")
        
        # Get all active subscriptions
        active_subscriptions = Subscription.query.filter_by(
            status=SubscriptionStatus.ACTIVE
        ).all()
        
        churn_predictions = []
        high_risk_count = 0
        
        for subscription in active_subscriptions:
            try:
                # Calculate churn risk factors
                risk_score = _calculate_subscription_churn_risk(subscription)
                
                if risk_score > 0.7:  # High risk threshold
                    risk_level = 'high'
                    high_risk_count += 1
                elif risk_score > 0.4:  # Medium risk threshold
                    risk_level = 'medium'
                else:
                    risk_level = 'low'
                
                if risk_level in ['high', 'medium']:  # Only track medium and high risk
                    churn_predictions.append({
                        'subscription_id': subscription.id,
                        'user_id': subscription.user_id,
                        'user_name': f"{subscription.user.first_name} {subscription.user.last_name}",
                        'risk_score': round(risk_score, 3),
                        'risk_level': risk_level,
                        'plan_name': subscription.plan.get_translated('name', get_current_language()) if subscription.plan else 'Standard',
                        'monthly_value': subscription.total_amount
                    })
                
            except Exception as e:
                logger.error(f"Failed to calculate churn risk for subscription {subscription.id}: {e}")
                continue
        
        # Sort by risk score
        churn_predictions.sort(key=lambda x: x['risk_score'], reverse=True)
        
        # Send alerts for high-risk customers
        if high_risk_count > 0:
            admin_users = User.query.filter(
                User.role.in_([UserRole.ADMIN, UserRole.MANAGER]),
                User.is_active == True
            ).all()
            
            notification_service = NotificationService()
            
            for admin in admin_users:
                notification_service.send_notification(
                    admin.id,
                    'subscription_churn_alert',
                    template_data={
                        'high_risk_count': high_risk_count,
                        'total_at_risk': len(churn_predictions),
                        'top_risk_customers': churn_predictions[:5]  # Top 5 at-risk
                    }
                )
        
        # Store predictions
        from business_app.services.analytics_service import AnalyticsService
        analytics_service = AnalyticsService()
        analytics_service.store_subscription_churn_predictions(churn_predictions)
        
        logger.info(f"Churn prediction completed: {high_risk_count} high-risk subscriptions identified")
        return {
            'total_analyzed': len(active_subscriptions),
            'high_risk_count': high_risk_count,
            'predictions': churn_predictions[:20]  # Return top 20 for response
        }
        
    except Exception as e:
        logger.error(f"Failed to generate subscription churn predictions: {e}")
        return {'error': str(e)}


def _calculate_subscription_churn_risk(subscription: Subscription) -> float:
    """Calculate churn risk score for a subscription"""
    risk_factors = {
        'payment_failures': 0,
        'usage_decline': 0,
        'support_issues': 0,
        'engagement_drop': 0,
        'plan_downgrades': 0
    }
    
    # Payment failure history
    if subscription.failed_payment_count > 0:
        risk_factors['payment_failures'] = min(subscription.failed_payment_count / 3, 1.0)
    
    # Usage decline (based on order frequency)
    recent_orders = Order.query.filter(
        Order.user_id == subscription.user_id,
        Order.created_at >= datetime.now(timezone.utc) - timedelta(days=30)
    ).count()
    
    older_orders = Order.query.filter(
        Order.user_id == subscription.user_id,
        Order.created_at.between(
            datetime.now(timezone.utc) - timedelta(days=60),
            datetime.now(timezone.utc) - timedelta(days=30)
        )
    ).count()
    
    if older_orders > 0 and recent_orders < older_orders * 0.7:
        risk_factors['usage_decline'] = 0.3
    
    # Plan downgrades
    if hasattr(subscription, 'plan_downgrades') and subscription.plan_downgrades > 0:
        risk_factors['plan_downgrades'] = 0.2
    
    # Time since last interaction
    if subscription.last_billing_date:
        days_since_billing = (datetime.now(timezone.utc) - subscription.last_billing_date).days
        if days_since_billing > 35:  # More than expected for monthly billing
            risk_factors['engagement_drop'] = 0.2
    
    # Calculate weighted risk score
    weights = {
        'payment_failures': 0.4,
        'usage_decline': 0.3,
        'support_issues': 0.1,
        'engagement_drop': 0.15,
        'plan_downgrades': 0.05
    }
    
    risk_score = sum(risk_factors[factor] * weights[factor] for factor in risk_factors)
    return min(1.0, max(0.0, risk_score))


@shared_task(time_limit=600, soft_time_limit=540)
def send_subscription_satisfaction_survey():
    """Send satisfaction surveys to subscription customers"""
    try:
        logger.info("Sending subscription satisfaction surveys")
        
        # Send surveys to customers who have been subscribed for 3+ months
        three_months_ago = datetime.now(timezone.utc) - timedelta(days=90)
        
        eligible_subscriptions = Subscription.query.filter(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.created_at <= three_months_ago,
            # Only send survey once per 6 months
            or_(
                Subscription.last_survey_sent.is_(None),
                Subscription.last_survey_sent <= datetime.now(timezone.utc) - timedelta(days=180)
            )
        ).all()
        
        notification_service = NotificationService()
        sent_count = 0
        
        for subscription in eligible_subscriptions:
            try:
                survey_link = f"{current_app.config['COMPANY_WEBSITE']}/survey/subscription/{subscription.id}"
                
                template_data = {
                    'subscription_id': subscription.id,
                    'customer_name': subscription.user.first_name,
                    'plan_name': subscription.plan.get_translated('name', get_current_language()) if subscription.plan else 'Standard',
                    'survey_link': survey_link,
                    'subscription_duration': (datetime.now(timezone.utc) - subscription.created_at).days // 30  # months
                }
                
                notification_service.send_notification(
                    subscription.user_id,
                    'subscription_satisfaction_survey',
                    template_data=template_data
                )
                
                # Update last survey sent date
                subscription.last_survey_sent = datetime.now(timezone.utc)
                sent_count += 1
                
            except Exception as e:
                logger.error(f"Failed to send survey for subscription {subscription.id}: {e}")
                continue
        
        db.session.commit()
        
        logger.info(f"Sent {sent_count} satisfaction surveys")
        return {'sent_count': sent_count, 'eligible_count': len(eligible_subscriptions)}
        
    except Exception as e:
        logger.error(f"Failed to send subscription satisfaction surveys: {e}")
        return {'error': str(e)}


@shared_task(bind=True, max_retries=3, default_retry_delay=300, time_limit=600, soft_time_limit=540)
def process_subscription_billing(self, subscription_id: int):
    """Process billing for a subscription"""
    try:
        logger.info(f"Processing billing for subscription {subscription_id}")
        
        subscription_service = SubscriptionService()
        subscription = Subscription.query.get(subscription_id)
        
        if not subscription:
            logger.error(f"Subscription {subscription_id} not found")
            return {'success': False, 'error': 'Subscription not found'}
        
        if subscription.status != SubscriptionStatus.ACTIVE:
            logger.info(f"Subscription {subscription_id} is not active, skipping billing")
            return {'success': False, 'error': 'Subscription not active'}
        
        # Process billing through subscription service
        billing_result = subscription_service.process_subscription_billing(subscription_id)
        
        if billing_result.get('success'):
            logger.info(f"Billing processed successfully for subscription {subscription_id}")
            
            # Send billing notification
            notification_service = NotificationService()
            notification_service.send_notification(
                subscription.user_id,
                'subscription_billed',
                template_data={
                    'subscription_name': subscription.get_translated('name', get_current_language()),
                    'billing_amount': subscription.billing_amount,
                    'next_billing_date': subscription.next_billing_date.isoformat() if subscription.next_billing_date else None
                }
            )
            
            # Schedule next billing
            if subscription.auto_renew and subscription.next_billing_date:
                next_billing_time = subscription.next_billing_date
                process_subscription_billing.apply_async(
                    args=[subscription_id],
                    eta=next_billing_time
                )
            
            return billing_result
        else:
            logger.error(f"Billing failed for subscription {subscription_id}: {billing_result.get('error')}")
            
            # Send billing failure notification
            notification_service = NotificationService()
            notification_service.send_notification(
                subscription.user_id,
                'subscription_billing_failed',
                template_data={
                    'subscription_name': subscription.get_translated('name', get_current_language()),
                    'billing_amount': subscription.billing_amount,
                    'error_message': billing_result.get('error', 'Unknown error')
                }
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
            return {'success': False, 'error': 'Subscription not found'}
        
        notification_service = NotificationService()
        
        # Determine notification type and template data based on reminder type
        if reminder_type == 'upcoming_billing':
            notification_type = 'subscription_billing_reminder'
            template_data = {
                'subscription_name': subscription.name,
                'billing_amount': subscription.billing_amount,
                'billing_date': subscription.next_billing_date.isoformat() if subscription.next_billing_date else None,
                'days_until_billing': (subscription.next_billing_date - datetime.now(timezone.utc)).days if subscription.next_billing_date else None
            }
            
        elif reminder_type == 'upcoming_delivery':
            notification_type = 'subscription_delivery_reminder'
            # Include time slot details if available
            time_slot_info = None
            if subscription.delivery_time_slot:
                time_slot_info = f"{subscription.delivery_time_slot.start_time} - {subscription.delivery_time_slot.end_time}"

            template_data = {
                'subscription_name': subscription.name,
                'delivery_frequency': subscription.delivery_frequency.value if hasattr(subscription.delivery_frequency, 'value') else str(subscription.delivery_frequency),
                'delivery_time_slot': time_slot_info or 'Flexible'
            }
            
        elif reminder_type == 'renewal_due':
            notification_type = 'subscription_renewal_reminder'
            template_data = {
                'subscription_name': subscription.name,
                'end_date': subscription.end_date.isoformat() if subscription.end_date else None,
                'auto_renew': subscription.auto_renew
            }
            
        elif reminder_type == 'payment_failed':
            notification_type = 'subscription_payment_failed'
            template_data = {
                'subscription_name': subscription.name,
                'billing_amount': subscription.billing_amount,
                'failed_date': datetime.now(timezone.utc).isoformat()
            }
            
        else:
            logger.error(f"Unknown reminder type: {reminder_type}")
            return {'success': False, 'error': f'Unknown reminder type: {reminder_type}'}
        
        # Send notification
        result = notification_service.send_notification(
            subscription.user_id,
            notification_type,
            template_data=template_data
        )
        
        if result:
            logger.info(f"{reminder_type} reminder sent successfully for subscription {subscription_id}")
            return {'success': True, 'reminder_type': reminder_type}
        else:
            logger.error(f"Failed to send {reminder_type} reminder for subscription {subscription_id}")
            return {'success': False, 'error': 'Failed to send notification'}
            
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
            return {'success': False, 'error': 'Subscription not found'}

        # Verify user ownership if user_id provided
        if user_id and subscription.user_id != user_id:
            logger.error(f"User {user_id} does not own subscription {subscription_id}")
            return {'success': False, 'error': 'Unauthorized'}

        if subscription.status != SubscriptionStatus.PAUSED:
            logger.warning(f"Subscription {subscription_id} is not paused (status: {subscription.status})")
            return {'success': False, 'error': f'Subscription is not paused (current status: {subscription.status})'}

        # Resume the subscription
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.pause_start_date = None
        subscription.pause_end_date = None
        subscription.updated_at = datetime.now(timezone.utc)

        # If next billing date has passed, calculate new billing date
        if subscription.next_billing_date and subscription.next_billing_date < datetime.now(timezone.utc).date():
            # Calculate next billing date from today
            subscription_service = SubscriptionService()
            from business_app.utils.constants import SubscriptionFrequency
            # billing_cycle and delivery_frequency are now enums
            billing_cycle = subscription.billing_cycle or SubscriptionFrequency.MONTHLY
            subscription.next_billing_date = subscription_service._calculate_next_billing_date(
                datetime.now(timezone.utc),
                billing_cycle
            )

        # If next delivery date has passed, calculate new delivery date
        if subscription.next_delivery_date and subscription.next_delivery_date < datetime.now(timezone.utc).date():
            subscription_service = SubscriptionService()
            from business_app.utils.constants import SubscriptionFrequency
            delivery_frequency = subscription.delivery_frequency or SubscriptionFrequency.WEEKLY
            subscription.next_delivery_date = subscription_service._calculate_next_delivery_date(
                datetime.now(timezone.utc),
                delivery_frequency,
                subscription.delivery_day_of_week,
                subscription.delivery_day_of_month
            )

        # Reset failed payment count on resume
        subscription.failed_payment_count = 0

        db.session.commit()

        # Send resume notification
        notification_service = NotificationService()
        notification_service.send_notification(
            subscription.user_id,
            'subscription_resumed',
            template_data={
                'subscription_name': subscription.name,
                'next_billing_date': subscription.next_billing_date.isoformat() if subscription.next_billing_date else None,
                'next_delivery_date': subscription.next_delivery_date.isoformat() if subscription.next_delivery_date else None
            }
        )

        # Schedule next billing if auto-renew enabled
        if subscription.auto_renew and subscription.next_billing_date:
            process_subscription_billing.apply_async(
                args=[subscription_id],
                eta=subscription.next_billing_date
            )

        # Schedule next delivery
        if subscription.next_delivery_date:
            create_subscription_delivery_task.apply_async(
                args=[subscription_id],
                eta=datetime.combine(subscription.next_delivery_date, datetime.min.time())
            )

        logger.info(f"Subscription {subscription_id} resumed successfully")
        return {
            'success': True,
            'subscription_id': subscription_id,
            'next_billing_date': subscription.next_billing_date.isoformat() if subscription.next_billing_date else None,
            'next_delivery_date': subscription.next_delivery_date.isoformat() if subscription.next_delivery_date else None
        }

    except Exception as exc:
        logger.error(f"Failed to resume subscription: {exc}")
        raise self.retry(exc=exc)