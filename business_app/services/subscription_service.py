"""
Subscription service for the Water Business Platform
Handles recurring water delivery subscriptions
"""
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
from flask import current_app
from dateutil.relativedelta import relativedelta

from business_app.models.subscription import Subscription, SubscriptionPlan, SubscriptionItem
from business_app.models.user import User
from business_app.models.product import Product
from business_app.utils.exceptions import ValidationError, NotFoundError, ConflictError
from business_app.utils.constants import SubscriptionStatus, SubscriptionFrequency
from business_app import db


class SubscriptionService:
    """Service for managing subscriptions"""
    
    def __init__(self):
        self.trial_days = current_app.config.get('SUBSCRIPTION_TRIAL_DAYS', 7)
        self.billing_day = current_app.config.get('SUBSCRIPTION_BILLING_DAY', 1)
        self.max_items = current_app.config.get('MAX_SUBSCRIPTION_ITEMS', 10)
    
    def create_subscription(self, user_id: int, plan_id: int, 
                          items: List[Dict[str, Any]], 
                          delivery_address: Dict[str, Any],
                          start_date: datetime = None,
                          use_trial: bool = True) -> Subscription:
        """
        Create a new subscription
        
        Args:
            user_id: User ID
            plan_id: Subscription plan ID
            items: List of subscription items
            delivery_address: Delivery address
            start_date: Subscription start date
            use_trial: Whether to use trial period
        
        Returns:
            Created Subscription object
        """
        # Validate user
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")
        
        # Validate plan
        plan = SubscriptionPlan.query.get(plan_id)
        if not plan or not plan.is_active:
            raise NotFoundError("Subscription plan not found")
        
        # Check if user already has active subscription
        existing_subscription = Subscription.query.filter_by(
            user_id=user_id,
            status=SubscriptionStatus.ACTIVE
        ).first()
        
        if existing_subscription:
            raise ConflictError("User already has an active subscription")
        
        # Validate items
        self._validate_subscription_items(items)
        
        # Calculate pricing
        total_amount = self._calculate_subscription_total(items, plan)
        
        # Set start date
        if start_date is None:
            start_date = datetime.now(timezone.utc)
        
        # Calculate next billing date
        next_billing_date = self._calculate_next_billing_date(start_date, plan.frequency)
        
        # Create subscription
        subscription = Subscription(
            user_id=user_id,
            plan_id=plan_id,
            status=SubscriptionStatus.TRIAL if use_trial else SubscriptionStatus.ACTIVE,
            frequency=plan.frequency,
            total_amount=total_amount,
            delivery_address_street=delivery_address['street'],
            delivery_address_city=delivery_address.get('city', 'Tashkent'),
            delivery_address_latitude=delivery_address['latitude'],
            delivery_address_longitude=delivery_address['longitude'],
            delivery_instructions=delivery_address.get('instructions'),
            preferred_delivery_time=delivery_address.get('preferred_time'),
            start_date=start_date,
            trial_end_date=start_date + timedelta(days=self.trial_days) if use_trial else None,
            next_billing_date=next_billing_date,
            created_at=datetime.now(timezone.utc)
        )
        
        db.session.add(subscription)
        db.session.flush()  # Get subscription ID
        
        # Add subscription items
        for item_data in items:
            subscription_item = SubscriptionItem(
                subscription_id=subscription.id,
                product_id=item_data['product_id'],
                quantity=item_data['quantity'],
                unit_price=item_data.get('unit_price', 0)
            )
            db.session.add(subscription_item)
        
        db.session.commit()
        
        # Schedule first delivery
        self._schedule_subscription_delivery(subscription.id)
        
        # Send confirmation
        self._send_subscription_confirmation(subscription)
        
        return subscription
    
    def update_subscription(self, subscription_id: int, user_id: int = None,
                          **updates) -> Subscription:
        """Update subscription details"""
        query = Subscription.query.filter_by(id=subscription_id)
        if user_id:
            query = query.filter_by(user_id=user_id)
        
        subscription = query.first()
        if not subscription:
            raise NotFoundError("Subscription not found")
        
        # Only allow updates for active or paused subscriptions
        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED]:
            raise ValidationError("Cannot update cancelled or expired subscription")
        
        # Update allowed fields
        allowed_fields = [
            'delivery_address_street', 'delivery_address_city',
            'delivery_address_latitude', 'delivery_address_longitude',
            'delivery_instructions', 'preferred_delivery_time'
        ]
        
        for field, value in updates.items():
            if field in allowed_fields:
                setattr(subscription, field, value)
        
        subscription.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        
        return subscription
    
    def update_subscription_items(self, subscription_id: int, 
                                 items: List[Dict[str, Any]],
                                 user_id: int = None) -> Subscription:
        """Update subscription items"""
        query = Subscription.query.filter_by(id=subscription_id)
        if user_id:
            query = query.filter_by(user_id=user_id)
        
        subscription = query.first()
        if not subscription:
            raise NotFoundError("Subscription not found")
        
        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED]:
            raise ValidationError("Cannot update items for cancelled or expired subscription")
        
        # Validate new items
        self._validate_subscription_items(items)
        
        # Remove existing items
        SubscriptionItem.query.filter_by(subscription_id=subscription_id).delete()
        
        # Add new items
        total_amount = 0
        for item_data in items:
            product = Product.query.get(item_data['product_id'])
            unit_price = product.price if product else 0
            
            subscription_item = SubscriptionItem(
                subscription_id=subscription_id,
                product_id=item_data['product_id'],
                quantity=item_data['quantity'],
                unit_price=unit_price
            )
            db.session.add(subscription_item)
            
            total_amount += unit_price * item_data['quantity']
        
        # Update subscription total
        subscription.total_amount = total_amount
        subscription.updated_at = datetime.now(timezone.utc)
        
        db.session.commit()
        
        return subscription
    
    def pause_subscription(self, subscription_id: int, user_id: int = None,
                          pause_until: datetime = None) -> Subscription:
        """Pause subscription"""
        query = Subscription.query.filter_by(id=subscription_id)
        if user_id:
            query = query.filter_by(user_id=user_id)
        
        subscription = query.first()
        if not subscription:
            raise NotFoundError("Subscription not found")
        
        if subscription.status != SubscriptionStatus.ACTIVE:
            raise ValidationError("Can only pause active subscriptions")
        
        subscription.status = SubscriptionStatus.PAUSED
        subscription.paused_at = datetime.now(timezone.utc)
        subscription.pause_until = pause_until
        subscription.updated_at = datetime.now(timezone.utc)
        
        db.session.commit()
        
        # Cancel pending deliveries
        self._cancel_pending_deliveries(subscription_id)
        
        # Send notification
        self._send_subscription_notification(subscription, 'paused')
        
        return subscription
    
    def resume_subscription(self, subscription_id: int, user_id: int = None) -> Subscription:
        """Resume paused subscription"""
        query = Subscription.query.filter_by(id=subscription_id)
        if user_id:
            query = query.filter_by(user_id=user_id)
        
        subscription = query.first()
        if not subscription:
            raise NotFoundError("Subscription not found")
        
        if subscription.status != SubscriptionStatus.PAUSED:
            raise ValidationError("Can only resume paused subscriptions")
        
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.paused_at = None
        subscription.pause_until = None
        subscription.resumed_at = datetime.now(timezone.utc)
        subscription.updated_at = datetime.now(timezone.utc)
        
        # Recalculate next billing date
        subscription.next_billing_date = self._calculate_next_billing_date(
            datetime.now(timezone.utc), subscription.frequency
        )
        
        db.session.commit()
        
        # Schedule next delivery
        self._schedule_subscription_delivery(subscription_id)
        
        # Send notification
        self._send_subscription_notification(subscription, 'resumed')
        
        return subscription
    
    def cancel_subscription(self, subscription_id: int, user_id: int = None,
                           reason: str = None, immediate: bool = False) -> Subscription:
        """Cancel subscription"""
        query = Subscription.query.filter_by(id=subscription_id)
        if user_id:
            query = query.filter_by(user_id=user_id)
        
        subscription = query.first()
        if not subscription:
            raise NotFoundError("Subscription not found")
        
        if subscription.status == SubscriptionStatus.CANCELLED:
            raise ValidationError("Subscription is already cancelled")
        
        if immediate:
            subscription.status = SubscriptionStatus.CANCELLED
            subscription.cancelled_at = datetime.now(timezone.utc)
            subscription.end_date = datetime.now(timezone.utc)
        else:
            # Cancel at end of current billing period
            subscription.status = SubscriptionStatus.CANCELLED
            subscription.cancelled_at = datetime.now(timezone.utc)
            subscription.end_date = subscription.next_billing_date
        
        subscription.cancellation_reason = reason
        subscription.updated_at = datetime.now(timezone.utc)
        
        db.session.commit()
        
        # Cancel pending deliveries if immediate cancellation
        if immediate:
            self._cancel_pending_deliveries(subscription_id)
        
        # Send notification
        self._send_subscription_notification(subscription, 'cancelled')
        
        return subscription
    
    def process_subscription_billing(self, subscription_id: int) -> Dict[str, Any]:
        """Process subscription billing"""
        subscription = Subscription.query.get(subscription_id)
        if not subscription:
            raise NotFoundError("Subscription not found")
        
        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL]:
            raise ValidationError("Cannot bill inactive subscription")
        
        # Create order for subscription
        from .order_service import OrderService
        order_service = OrderService()
        
        order_data = {
            'items': [
                {
                    'product_id': item.product_id,
                    'quantity': item.quantity
                }
                for item in subscription.items
            ],
            'delivery_address': {
                'street': subscription.delivery_address_street,
                'city': subscription.delivery_address_city,
                'latitude': subscription.delivery_address_latitude,
                'longitude': subscription.delivery_address_longitude
            },
            'delivery_instructions': subscription.delivery_instructions,
            'notes': f"Subscription order #{subscription.id}"
        }
        
        try:
            order = order_service.create_order(subscription.user_id, order_data)
            
            # Process payment if not trial
            if subscription.status != SubscriptionStatus.TRIAL:
                from .payment_service import PaymentService
                payment_service = PaymentService()
                
                # Use subscription's preferred payment method
                payment_method = subscription.payment_method or 'card'
                payment = payment_service.create_payment(
                    order.id, payment_method, subscription.total_amount
                )
                
                # Auto-charge if payment method is stored
                if subscription.payment_token:
                    # Process automatic payment
                    success = self._process_auto_payment(payment, subscription.payment_token)
                    if not success:
                        # Payment failed - handle accordingly
                        self._handle_payment_failure(subscription)
            
            # Update subscription
            subscription.last_order_id = order.id
            subscription.last_billing_date = datetime.now(timezone.utc)
            subscription.next_billing_date = self._calculate_next_billing_date(
                datetime.now(timezone.utc), subscription.frequency
            )
            subscription.billing_cycle_count += 1
            
            # Convert from trial to active if applicable
            if subscription.status == SubscriptionStatus.TRIAL:
                subscription.status = SubscriptionStatus.ACTIVE
            
            db.session.commit()
            
            return {
                'success': True,
                'order_id': order.id,
                'amount': subscription.total_amount,
                'next_billing_date': subscription.next_billing_date.isoformat()
            }
            
        except Exception as e:
            current_app.logger.error(f"Subscription billing failed: {e}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def get_user_subscriptions(self, user_id: int, 
                              status: SubscriptionStatus = None) -> List[Subscription]:
        """Get user's subscriptions"""
        query = Subscription.query.filter_by(user_id=user_id)
        
        if status:
            query = query.filter_by(status=status)
        
        return query.order_by(Subscription.created_at.desc()).all()
    
    def get_subscription_analytics(self, start_date: datetime = None,
                                 end_date: datetime = None) -> Dict[str, Any]:
        """Get subscription analytics"""
        query = Subscription.query
        
        if start_date:
            query = query.filter(Subscription.created_at >= start_date)
        if end_date:
            query = query.filter(Subscription.created_at <= end_date)
        
        subscriptions = query.all()
        
        # Calculate metrics
        total_subscriptions = len(subscriptions)
        active_subscriptions = len([s for s in subscriptions if s.status == SubscriptionStatus.ACTIVE])
        trial_subscriptions = len([s for s in subscriptions if s.status == SubscriptionStatus.TRIAL])
        cancelled_subscriptions = len([s for s in subscriptions if s.status == SubscriptionStatus.CANCELLED])
        
        # Revenue metrics
        monthly_revenue = sum(s.total_amount for s in subscriptions 
                            if s.status == SubscriptionStatus.ACTIVE and s.frequency == SubscriptionFrequency.MONTHLY)
        
        # Churn rate
        churn_rate = (cancelled_subscriptions / total_subscriptions) * 100 if total_subscriptions > 0 else 0
        
        # Average subscription value
        avg_subscription_value = sum(s.total_amount for s in subscriptions) / len(subscriptions) if subscriptions else 0
        
        return {
            'total_subscriptions': total_subscriptions,
            'active_subscriptions': active_subscriptions,
            'trial_subscriptions': trial_subscriptions,
            'cancelled_subscriptions': cancelled_subscriptions,
            'churn_rate': round(churn_rate, 2),
            'monthly_recurring_revenue': monthly_revenue,
            'average_subscription_value': round(avg_subscription_value, 2),
            'frequency_breakdown': self._get_frequency_breakdown(subscriptions)
        }
    
    def create_subscription_plan(self, name: str, description: str,
                               frequency: SubscriptionFrequency,
                               discount_percentage: float = 0,
                               features: List[str] = None) -> SubscriptionPlan:
        """Create a new subscription plan"""
        plan = SubscriptionPlan(
            name=name,
            description=description,
            frequency=frequency,
            discount_percentage=discount_percentage,
            features=features or [],
            is_active=True
        )
        
        db.session.add(plan)
        db.session.commit()
        
        return plan
    
    # Private helper methods
    def _validate_subscription_items(self, items: List[Dict[str, Any]]):
        """Validate subscription items"""
        if not items:
            raise ValidationError("Subscription must have at least one item")
        
        if len(items) > self.max_items:
            raise ValidationError(f"Maximum {self.max_items} items allowed per subscription")
        
        for item in items:
            if 'product_id' not in item or 'quantity' not in item:
                raise ValidationError("Each item must have product_id and quantity")
            
            product = Product.query.get(item['product_id'])
            if not product or not product.is_active:
                raise ValidationError(f"Product {item['product_id']} not found or inactive")
            
            if item['quantity'] <= 0:
                raise ValidationError("Item quantity must be positive")
    
    def _calculate_subscription_total(self, items: List[Dict[str, Any]], 
                                    plan: SubscriptionPlan) -> int:
        """Calculate subscription total amount"""
        total = 0
        
        for item in items:
            product = Product.query.get(item['product_id'])
            if product:
                total += product.price * item['quantity']
        
        # Apply plan discount
        if plan.discount_percentage > 0:
            total = total * (1 - plan.discount_percentage / 100)
        
        return int(total)
    
    def _calculate_next_billing_date(self, start_date: datetime, 
                                   frequency: SubscriptionFrequency) -> datetime:
        """Calculate next billing date based on frequency"""
        if frequency == SubscriptionFrequency.DAILY:
            return start_date + timedelta(days=1)
        elif frequency == SubscriptionFrequency.WEEKLY:
            return start_date + timedelta(weeks=1)
        elif frequency == SubscriptionFrequency.BIWEEKLY:
            return start_date + timedelta(weeks=2)
        elif frequency == SubscriptionFrequency.MONTHLY:
            return start_date + relativedelta(months=1)
        else:
            return start_date + relativedelta(months=1)  # Default to monthly
    
    def _schedule_subscription_delivery(self, subscription_id: int):
        """Schedule subscription delivery"""
        from ..tasks.subscription_tasks import schedule_subscription_delivery_task
        schedule_subscription_delivery_task.delay(subscription_id)
    
    def _send_subscription_confirmation(self, subscription: Subscription):
        """Send subscription confirmation"""
        from ..tasks.notification_tasks import send_subscription_confirmation_task
        send_subscription_confirmation_task.delay(subscription.id)
    
    def _send_subscription_notification(self, subscription: Subscription, event_type: str):
        """Send subscription notification"""
        from ..tasks.notification_tasks import send_subscription_notification_task
        send_subscription_notification_task.delay(subscription.id, event_type)
    
    def _cancel_pending_deliveries(self, subscription_id: int):
        """Cancel pending deliveries for subscription"""
        from ..tasks.subscription_tasks import cancel_subscription_deliveries_task
        cancel_subscription_deliveries_task.delay(subscription_id)
    
    def _process_auto_payment(self, payment, payment_token: str) -> bool:
        """Process automatic payment using stored payment method"""
        # This would integrate with actual payment gateway for auto-charging
        # For now, return True as placeholder
        return True
    
    def _handle_payment_failure(self, subscription: Subscription):
        """Handle subscription payment failure"""
        # Increment failed payment count
        subscription.failed_payment_count = (subscription.failed_payment_count or 0) + 1
        
        # Pause subscription after 3 failed payments
        if subscription.failed_payment_count >= 3:
            subscription.status = SubscriptionStatus.PAUSED
            subscription.paused_at = datetime.now(timezone.utc)
        
        db.session.commit()
        
        # Send payment failure notification
        self._send_subscription_notification(subscription, 'payment_failed')
    
    def _get_frequency_breakdown(self, subscriptions: List[Subscription]) -> Dict[str, int]:
        """Get subscription frequency breakdown"""
        breakdown = {}
        for subscription in subscriptions:
            freq = subscription.frequency.value
            breakdown[freq] = breakdown.get(freq, 0) + 1
        return breakdown