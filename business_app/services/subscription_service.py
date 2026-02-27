"""
Subscription service for the Water Business Platform
Handles recurring water delivery subscriptions
"""
from datetime import UTC, date, datetime, timezone, timedelta, time
from typing import List, Dict, Any, Optional
from flask import current_app
from dateutil.relativedelta import relativedelta

from business_app.models.subscription import Subscription, SubscriptionItem, SubscriptionLog
from business_app.models.user import User, UserAddress
from business_app.models.product import Product
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.delivery import DeliveryTimeSlot
from business_app.utils.exceptions import ValidationError, NotFoundError, ConflictError
from business_app.utils.constants import SubscriptionStatus, SubscriptionFrequency, PaymentMethod
from business_app.utils.translations import get_translation
from sqlalchemy.orm import joinedload
from business_app import db


class SubscriptionService:
    """Service for managing subscriptions"""
    
    def __init__(self):
        self.trial_days = current_app.config.get('SUBSCRIPTION_TRIAL_DAYS', 7)
        self.billing_day = current_app.config.get('SUBSCRIPTION_BILLING_DAY', 1)
        self.max_items = current_app.config.get('MAX_SUBSCRIPTION_ITEMS', 10)
    
    def create_subscription(self, subscription_data: Dict[str, Any], items: List[Dict[str, Any]]) -> Subscription:
        """
        Create a new subscription

        Args:
            subscription_data: Dictionary containing subscription configuration
            items: List of subscription items with product_id and quantity

        Returns:
            Created Subscription object
        """
        user_id = subscription_data['user_id']

        # Validate user
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")

        # Note: Users can have multiple subscriptions with the same name
        # This allows flexibility in naming (e.g., "Weekly Water - Home" for different products)

        # Validate items
        if not items or len(items) == 0:
            raise ValidationError("Subscription must have at least one item")

        self._validate_subscription_items(items)

        # Calculate pricing
        total_amount = self._calculate_items_total(items)

        # Apply discount if provided
        discount_percentage = subscription_data.get('discount_percentage', 0)
        if discount_percentage > 0:
            total_amount = total_amount * (1 - discount_percentage / 100)

        # Set start date
        start_date = subscription_data.get('start_date')
        if start_date is None:
            start_date = datetime.now(timezone.utc)

        # Get and validate billing cycle and delivery frequency
        billing_cycle_str = subscription_data.get('billing_cycle', 'monthly')
        delivery_frequency_str = subscription_data.get('delivery_frequency', 'weekly')

        # Convert to enum
        try:
            billing_cycle = SubscriptionFrequency(billing_cycle_str)
            delivery_frequency = SubscriptionFrequency(delivery_frequency_str)
        except ValueError as e:
            raise ValidationError(f"Invalid frequency value: {e}")

        # Validate billing vs delivery frequency
        self._validate_billing_frequency(billing_cycle, delivery_frequency)

        # Calculate next billing date based on billing cycle
        next_billing_date = self._calculate_next_billing_date(start_date, billing_cycle)

        # Calculate next delivery date based on delivery frequency
        next_delivery_date = self._calculate_next_delivery_date(
            start_date,
            delivery_frequency,
            subscription_data.get('delivery_day_of_week'),
            subscription_data.get('delivery_day_of_month')
        )

        # Create subscription
        subscription = Subscription(
            user_id=user_id,
            name=subscription_data.get('name', 'Water Delivery Subscription'),
            description=subscription_data.get('description', ''),
            status=SubscriptionStatus.ACTIVE,
            billing_cycle=billing_cycle,
            delivery_frequency=delivery_frequency,
            delivery_day_of_week=subscription_data.get('delivery_day_of_week'),
            delivery_day_of_month=subscription_data.get('delivery_day_of_month'),
            delivery_time_slot_id=subscription_data.get('delivery_time_slot_id'),
            delivery_address_id=subscription_data.get('delivery_address_id'),
            payment_method=subscription_data.get('payment_method', PaymentMethod.CASH),
            auto_payment=subscription_data.get('auto_payment', False),
            auto_renew=subscription_data.get('auto_renew', True),
            discount_percentage=discount_percentage,
            billing_amount=total_amount,
            start_date=start_date,
            end_date=subscription_data.get('end_date'),
            next_billing_date=next_billing_date,
            next_delivery_date=next_delivery_date,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )

        db.session.add(subscription)
        db.session.flush()  # Get subscription ID

        current_app.logger.info(f"Subscription object CREATED SUCCESSFULLY")

        # Add subscription items
        for item_data in items:
            product: Product = Product.query.get(item_data['product_id'])
            if not product:
                raise NotFoundError(f"Product {item_data['product_id']} not found")

            subscription_item = SubscriptionItem(
                subscription_id=subscription.id,
                product_id=item_data['product_id'],
                quantity=item_data['quantity'],
                unit_price=product.base_price,
                special_instructions=item_data.get('special_instructions')
            )
            subscription_item.calculate_total()
            db.session.add(subscription_item)
            current_app.logger.info(f"SubscriptionItem object CREATED SUCCESSFULLY")

        db.session.commit()
        current_app.logger.info(f"SubscriptionItem object CREATED SUCCESSFULLY with db commit")

        # Schedule first delivery (async task)
        try:
            from business_app.tasks.subscription_tasks import create_subscription_delivery_task
            create_subscription_delivery_task.delay(subscription.id)
            current_app.logger.info(f"create_subscription SERVICE create_subscription_delivery_task task scheduled")
        except Exception as e:
            # Log error but don't fail subscription creation if Celery is unavailable
            current_app.logger.error(f"Failed to schedule delivery task: {e}")
            current_app.logger.warning(f"Subscription {subscription.id} created but delivery task not scheduled")

        return subscription.to_dict()
    
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
            product: Product = Product.query.get(item_data['product_id'])
            unit_price = product.base_price if product else 0
            
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
        query = Subscription.query.options(
            joinedload(Subscription.subscription_items).joinedload(SubscriptionItem.product),
            joinedload(Subscription.delivery_address)
        ).filter_by(user_id=user_id)
        
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

    def get_user_subscriptions_paginated(
        self,
        user_id: int,
        page: int,
        per_page: int,
        status: Optional[str] = None,
        billing_cycle: Optional[str] = None,
    ):
        """Get user subscriptions with filtering and pagination."""
        query = Subscription.query.filter_by(user_id=user_id)

        if status:
            try:
                query = query.filter_by(status=SubscriptionStatus(status))
            except ValueError as exc:
                raise ValidationError("api.subscriptions.error.invalid_status_value") from exc

        if billing_cycle:
            query = query.filter_by(billing_cycle=billing_cycle)

        return query.order_by(Subscription.created_at.desc()).paginate(
            page=page,
            per_page=per_page,
            error_out=False,
        )

    def get_subscription_details_for_user(self, subscription_id: int, user_id: int) -> Dict[str, Any]:
        """Get full subscription details for a specific user-owned subscription."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id)
        recent_orders = (
            Order.query.filter_by(subscription_id=subscription_id)
            .order_by(Order.created_at.desc())
            .limit(10)
            .all()
        )
        billing_info = self.get_billing_info(subscription_id)

        return {
            "subscription": subscription,
            "recent_orders": recent_orders,
            "billing_info": billing_info,
        }

    def create_subscription_for_user(self, user_id: int, validated_data: Any) -> Dict[str, Any]:
        """Validate ownership/dependencies and create a subscription for a user."""
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("user_not_found")

        address = UserAddress.query.filter_by(
            id=validated_data.delivery_address_id,
            user_id=user_id,
        ).first()
        if not address:
            raise NotFoundError("api.subscriptions.error.invalid_delivery_address")

        if validated_data.delivery_time_slot_id:
            time_slot = DeliveryTimeSlot.query.get(validated_data.delivery_time_slot_id)
            if not time_slot or not time_slot.is_active:
                raise NotFoundError("api.subscriptions.error.invalid_or_inactive_time_slot")

        try:
            payment_method = PaymentMethod(validated_data.payment_method)
        except ValueError as exc:
            raise ValidationError("api.subscriptions.error.invalid_payment_method") from exc

        subscription_data = {
            "user_id": user_id,
            "name": validated_data.name,
            "description": validated_data.description or "",
            "billing_cycle": validated_data.billing_cycle,
            "delivery_frequency": validated_data.delivery_frequency,
            "delivery_day_of_week": validated_data.delivery_day_of_week,
            "delivery_day_of_month": validated_data.delivery_day_of_month,
            "delivery_time_slot_id": validated_data.delivery_time_slot_id,
            "delivery_address_id": validated_data.delivery_address_id,
            "payment_method": payment_method,
            "auto_payment": validated_data.auto_payment,
            "auto_renew": validated_data.auto_renew,
            "discount_percentage": validated_data.discount_percentage,
            "start_date": validated_data.start_date or datetime.now(UTC),
            "end_date": validated_data.end_date,
        }

        return self.create_subscription(subscription_data, validated_data.items)

    def update_subscription_for_user(
        self,
        subscription_id: int,
        user_id: int,
        update_data: Dict[str, Any],
    ) -> Subscription:
        """Update a user-owned subscription."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id)

        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED]:
            raise ValidationError("api.subscriptions.error.cannot_update_cancelled")

        changes = {}
        for field, new_value in update_data.items():
            if not hasattr(subscription, field):
                continue

            old_value = getattr(subscription, field)
            if field == "delivery_address_id":
                address = UserAddress.query.filter_by(id=new_value, user_id=user_id).first()
                if not address:
                    raise NotFoundError("api.subscriptions.error.invalid_delivery_address")

            if field == "delivery_time_slot_id" and new_value is not None:
                time_slot = DeliveryTimeSlot.query.get(new_value)
                if not time_slot or not time_slot.is_active:
                    raise NotFoundError("api.subscriptions.error.invalid_or_inactive_time_slot")

            if field == "payment_method":
                try:
                    new_value = PaymentMethod(new_value)
                except ValueError as exc:
                    raise ValidationError("api.subscriptions.error.invalid_payment_method") from exc

            setattr(subscription, field, new_value)
            changes[field] = {
                "old": self._serialize_for_log(old_value),
                "new": self._serialize_for_log(new_value),
            }

        subscription.updated_at = datetime.now(UTC)
        if changes:
            log = SubscriptionLog(
                subscription_id=subscription_id,
                action="updated",
                details=get_translation(
                    "api.subscriptions.log.updated_fields",
                    fields=", ".join(changes.keys()),
                ),
                user_id=user_id,
                extra_data={"changes": changes},
            )
            db.session.add(log)

        db.session.commit()
        return subscription

    def pause_subscription_for_user(
        self,
        subscription_id: int,
        user_id: int,
        reason: str,
        resume_date: Optional[datetime] = None,
    ) -> Subscription:
        """Pause a user-owned subscription."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id)
        if subscription.status != SubscriptionStatus.ACTIVE:
            raise ValidationError("api.subscriptions.error.only_active_pause")

        if resume_date and resume_date <= datetime.now(UTC):
            raise ValidationError("api.subscriptions.error.resume_date_future")

        subscription.pause(reason=reason, resume_date=resume_date)
        db.session.add(
            SubscriptionLog(
                subscription_id=subscription_id,
                action="paused",
                details=get_translation("api.subscriptions.log.reason", reason=reason),
            )
        )
        db.session.commit()
        return subscription

    def resume_subscription_for_user(self, subscription_id: int, user_id: int) -> Subscription:
        """Resume a paused user-owned subscription."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id)
        if subscription.status != SubscriptionStatus.PAUSED:
            raise ValidationError("api.subscriptions.error.only_paused_resume")

        subscription.resume()
        db.session.add(
            SubscriptionLog(
                subscription_id=subscription_id,
                action="resumed",
                details=get_translation("api.subscriptions.log.resumed"),
            )
        )
        db.session.commit()
        return subscription

    def cancel_subscription_for_user(
        self,
        subscription_id: int,
        user_id: int,
        reason: str,
        immediate: bool,
    ) -> Subscription:
        """Cancel a user-owned subscription."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id)
        if subscription.status == SubscriptionStatus.CANCELLED:
            raise ValidationError("api.subscriptions.error.already_cancelled")

        if immediate:
            subscription.cancel(reason=reason)
            db.session.add(
                SubscriptionLog(
                    subscription_id=subscription_id,
                    action="cancelled",
                    details=get_translation(
                        "api.subscriptions.log.cancelled_with_reason",
                        reason=reason,
                    ),
                )
            )
        else:
            subscription.auto_renew = False
            subscription.end_date = subscription.next_billing_date
            db.session.add(
                SubscriptionLog(
                    subscription_id=subscription_id,
                    action="cancellation_scheduled",
                    details=get_translation(
                        "api.subscriptions.log.cancellation_scheduled",
                        date=subscription.end_date.strftime("%Y-%m-%d"),
                        reason=reason,
                    ),
                    user_id=user_id,
                )
            )

        db.session.commit()
        return subscription

    def get_subscription_items_for_user(self, subscription_id: int, user_id: int) -> List[SubscriptionItem]:
        """Get subscription items for a user-owned subscription."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id)
        return subscription.subscription_items

    def add_subscription_item_for_user(
        self,
        subscription_id: int,
        user_id: int,
        product_id: int,
        quantity: int,
        special_instructions: Optional[str] = None,
        language: str = "en",
    ) -> Dict[str, Any]:
        """Add an item to a user-owned subscription."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id)
        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED]:
            raise ValidationError("api.subscriptions.error.cannot_modify_cancelled")

        product = Product.query.filter_by(id=product_id, is_active=True).first()
        if not product:
            raise NotFoundError("api.subscriptions.error.product_not_found")

        existing_item = SubscriptionItem.query.filter_by(
            subscription_id=subscription_id,
            product_id=product_id,
        ).first()
        if existing_item:
            raise ConflictError("api.subscriptions.error.product_already_exists")

        item = SubscriptionItem(
            subscription_id=subscription_id,
            product_id=product_id,
            quantity=quantity,
            unit_price=product.calculate_price(),
            special_instructions=special_instructions,
        )
        item.calculate_total()
        db.session.add(item)
        db.session.flush()

        subscription.billing_amount = subscription.get_total_value()
        subscription.updated_at = datetime.now(UTC)

        product_name = product.get_translated("name", language)
        db.session.add(
            SubscriptionLog(
                subscription_id=subscription_id,
                action="item_added",
                details=get_translation(
                    "api.subscriptions.log.item_added",
                    quantity=quantity,
                    product=product_name,
                ),
                user_id=user_id,
            )
        )
        db.session.commit()

        return {
            "item": item,
            "billing_amount": subscription.billing_amount,
        }

    def update_subscription_item_for_user(
        self,
        subscription_id: int,
        item_id: int,
        user_id: int,
        quantity: int,
        special_instructions: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update a subscription item for a user-owned subscription."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id)
        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED]:
            raise ValidationError("api.subscriptions.error.cannot_modify_cancelled")

        item = SubscriptionItem.query.filter_by(
            id=item_id,
            subscription_id=subscription_id,
        ).first()
        if not item:
            raise NotFoundError("api.subscriptions.error.item_not_found")

        old_quantity = item.quantity
        item.quantity = quantity
        if special_instructions is not None:
            item.special_instructions = special_instructions

        item.calculate_total()
        db.session.flush()

        subscription.billing_amount = subscription.get_total_value()
        subscription.updated_at = datetime.now(UTC)

        product_name = (
            item.product.name if item.product else get_translation("api.subscriptions.unknown_product")
        )
        db.session.add(
            SubscriptionLog(
                subscription_id=subscription_id,
                action="item_updated",
                details=get_translation(
                    "api.subscriptions.log.item_updated",
                    product=product_name,
                    old_quantity=old_quantity,
                    new_quantity=quantity,
                ),
                user_id=user_id,
            )
        )
        db.session.commit()

        return {
            "item": item,
            "billing_amount": subscription.billing_amount,
        }

    def remove_subscription_item_for_user(
        self,
        subscription_id: int,
        item_id: int,
        user_id: int,
    ) -> Subscription:
        """Remove an item from a user-owned subscription."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id)
        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED]:
            raise ValidationError("api.subscriptions.error.cannot_modify_cancelled")

        item = SubscriptionItem.query.filter_by(
            id=item_id,
            subscription_id=subscription_id,
        ).first()
        if not item:
            raise NotFoundError("api.subscriptions.error.item_not_found")

        remaining_items = (
            SubscriptionItem.query.filter_by(subscription_id=subscription_id)
            .filter(SubscriptionItem.id != item_id)
            .count()
        )
        if remaining_items == 0:
            raise ValidationError("api.subscriptions.error.cannot_remove_last_item")

        product_name = item.product.name if item.product else get_translation("api.subscriptions.unknown_product")
        db.session.delete(item)
        db.session.flush()

        subscription.billing_amount = subscription.get_total_value()
        subscription.updated_at = datetime.now(UTC)
        db.session.add(
            SubscriptionLog(
                subscription_id=subscription_id,
                action="item_removed",
                details=get_translation("api.subscriptions.log.item_removed", product=product_name),
                user_id=user_id,
            )
        )
        db.session.commit()
        return subscription

    def get_subscription_billing_history_for_user(self, subscription_id: int, user_id: int) -> Dict[str, Any]:
        """Get billing history payload for a user-owned subscription."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id)
        payments = (
            Payment.query.filter_by(subscription_id=subscription_id)
            .order_by(Payment.created_at.desc())
            .all()
        )

        total_paid = sum(
            float(payment.amount)
            for payment in payments
            if getattr(payment.status, "value", payment.status) == "completed"
        )
        failed_payments = len(
            [
                payment
                for payment in payments
                if getattr(payment.status, "value", payment.status) == "failed"
            ]
        )

        return {
            "billing_history": [
                {
                    "payment_id": payment.payment_id,
                    "amount": payment.amount,
                    "status": payment.status.value,
                    "payment_method": payment.payment_method.value,
                    "created_at": payment.created_at.isoformat() if payment.created_at else None,
                    "failure_reason": payment.failure_reason,
                }
                for payment in payments
            ],
            "summary": {
                "total_paid": total_paid,
                "total_payments": len(payments),
                "failed_payments": failed_payments,
                "next_billing_date": (
                    subscription.next_billing_date.isoformat()
                    if subscription.next_billing_date
                    else None
                ),
                "next_billing_amount": subscription.billing_amount,
            },
        }

    def get_subscription_logs_paginated_for_user(
        self,
        subscription_id: int,
        user_id: int,
        page: int,
        per_page: int,
    ):
        """Get paginated logs for a user-owned subscription."""
        self._get_user_subscription_or_raise(subscription_id, user_id)
        return (
            SubscriptionLog.query.filter_by(subscription_id=subscription_id)
            .order_by(SubscriptionLog.created_at.desc())
            .paginate(page=page, per_page=per_page, error_out=False)
        )

    def get_subscription_statistics_for_user(self, user_id: int, language: str = "en") -> Dict[str, Any]:
        """Calculate subscription statistics for a user."""
        subscriptions = Subscription.query.filter_by(user_id=user_id).all()

        total_subscriptions = len(subscriptions)
        active_subscriptions = len(
            [subscription for subscription in subscriptions if subscription.status == SubscriptionStatus.ACTIVE]
        )
        paused_subscriptions = len(
            [subscription for subscription in subscriptions if subscription.status == SubscriptionStatus.PAUSED]
        )
        cancelled_subscriptions = len(
            [subscription for subscription in subscriptions if subscription.status == SubscriptionStatus.CANCELLED]
        )

        total_spent = sum((subscription.total_amount_billed or 0) for subscription in subscriptions)
        total_savings = sum(
            (subscription.total_amount_billed or 0) * (subscription.discount_percentage / 100)
            for subscription in subscriptions
            if subscription.discount_percentage and subscription.discount_percentage > 0
        )
        total_deliveries = sum((subscription.total_orders_generated or 0) for subscription in subscriptions)

        average_order_value = 0.0
        if total_deliveries > 0:
            average_order_value = round(float(total_spent) / total_deliveries, 2)

        product_counts = {}
        for subscription in subscriptions:
            for item in subscription.subscription_items:
                if item.product:
                    product_counts[item.product_id] = product_counts.get(item.product_id, 0) + item.quantity

        most_ordered_product = None
        if product_counts:
            most_popular_product_id = max(product_counts, key=product_counts.get)
            for subscription in subscriptions:
                for item in subscription.subscription_items:
                    if item.product_id == most_popular_product_id and item.product:
                        most_ordered_product = item.product.get_translated("name", language)
                        break
                if most_ordered_product:
                    break

        upcoming_deliveries = 0
        today = date.today()
        for subscription in subscriptions:
            if subscription.status == SubscriptionStatus.ACTIVE:
                next_delivery = subscription.calculate_next_delivery_date()
                if next_delivery >= today:
                    upcoming_deliveries += 1

        monthly_spending = {}
        for i in range(12):
            month_start = (datetime.now(UTC).replace(day=1) - timedelta(days=32 * i)).replace(day=1)
            month_key = month_start.strftime("%Y-%m")

            month_total = 0
            for subscription in subscriptions:
                if (
                    subscription.created_at.date() <= month_start.date()
                    and (not subscription.end_date or subscription.end_date.date() >= month_start.date())
                ):
                    billing_amount = subscription.billing_amount or 0
                    if subscription.billing_cycle == SubscriptionFrequency.MONTHLY:
                        month_total += billing_amount
                    elif subscription.billing_cycle == SubscriptionFrequency.WEEKLY:
                        month_total += billing_amount * 4
                    elif subscription.billing_cycle == SubscriptionFrequency.DAILY:
                        month_total += billing_amount * 30
                    elif str(subscription.billing_cycle) == SubscriptionFrequency.MONTHLY.value:
                        month_total += billing_amount
                    elif str(subscription.billing_cycle) == SubscriptionFrequency.WEEKLY.value:
                        month_total += billing_amount * 4
                    elif str(subscription.billing_cycle) == SubscriptionFrequency.DAILY.value:
                        month_total += billing_amount * 30

            monthly_spending[month_key] = float(month_total)

        return {
            "total_subscriptions": total_subscriptions,
            "active_subscriptions": active_subscriptions,
            "paused_subscriptions": paused_subscriptions,
            "cancelled_subscriptions": cancelled_subscriptions,
            "total_spent": float(total_spent),
            "total_savings": float(total_savings),
            "total_deliveries": total_deliveries,
            "average_order_value": float(average_order_value),
            "most_ordered_product": most_ordered_product,
            "upcoming_deliveries": upcoming_deliveries,
            "monthly_spending_trend": monthly_spending,
        }

    def skip_next_delivery_for_user(
        self,
        subscription_id: int,
        user_id: int,
        reason: str,
    ) -> Dict[str, Any]:
        """Log skip-next-delivery and calculate adjusted next date."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id)
        if subscription.status != SubscriptionStatus.ACTIVE:
            raise ValidationError("api.subscriptions.error.only_active_skip")

        current_next_delivery = subscription.calculate_next_delivery_date()
        frequency = (
            subscription.delivery_frequency.value
            if hasattr(subscription.delivery_frequency, "value")
            else str(subscription.delivery_frequency)
        )
        if frequency == SubscriptionFrequency.DAILY.value:
            new_next_delivery = current_next_delivery + timedelta(days=1)
        elif frequency == SubscriptionFrequency.WEEKLY.value:
            new_next_delivery = current_next_delivery + timedelta(weeks=1)
        elif frequency == SubscriptionFrequency.MONTHLY.value:
            new_next_delivery = current_next_delivery + relativedelta(months=1)
        else:
            new_next_delivery = current_next_delivery + timedelta(days=7)

        db.session.add(
            SubscriptionLog(
                subscription_id=subscription_id,
                action="delivery_skipped",
                details=get_translation(
                    "api.subscriptions.log.delivery_skipped",
                    date=current_next_delivery.strftime("%Y-%m-%d"),
                    reason=reason,
                ),
                user_id=user_id,
                extra_data={
                    "original_delivery_date": current_next_delivery.isoformat(),
                    "new_delivery_date": new_next_delivery.isoformat(),
                    "reason": reason,
                },
            )
        )
        db.session.commit()

        return {
            "subscription": subscription,
            "original_delivery_date": current_next_delivery,
            "new_next_delivery_date": new_next_delivery,
        }

    def change_payment_method_for_user(
        self,
        subscription_id: int,
        user_id: int,
        payment_method: str,
    ) -> Dict[str, Any]:
        """Change payment method for a user-owned subscription."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id)
        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED]:
            raise ValidationError("api.subscriptions.error.cannot_change_payment_cancelled")

        try:
            new_payment_method = PaymentMethod(payment_method)
        except ValueError as exc:
            raise ValidationError("api.subscriptions.error.invalid_payment_method") from exc

        old_payment_method = subscription.payment_method
        subscription.payment_method = new_payment_method
        subscription.updated_at = datetime.now(UTC)

        db.session.add(
            SubscriptionLog(
                subscription_id=subscription_id,
                action="payment_method_changed",
                details=get_translation(
                    "api.subscriptions.log.payment_method_changed",
                    old_method=old_payment_method.value,
                    new_method=new_payment_method.value,
                ),
                user_id=user_id,
            )
        )
        db.session.commit()

        return {
            "subscription": subscription,
            "old_payment_method": old_payment_method,
            "new_payment_method": new_payment_method,
        }

    def validate_retry_billing_for_user(self, subscription_id: int, user_id: int) -> Subscription:
        """Validate whether billing retry can be initiated."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id)
        if subscription.status != SubscriptionStatus.ACTIVE:
            raise ValidationError("api.subscriptions.error.only_active_retry")
        if subscription.failed_billing_attempts == 0:
            raise ValidationError("api.subscriptions.error.no_failed_billing_to_retry")
        return subscription

    def _get_user_subscription_or_raise(self, subscription_id: int, user_id: int) -> Subscription:
        """Fetch a subscription scoped to a user or raise not found."""
        subscription = Subscription.query.filter_by(id=subscription_id, user_id=user_id).first()
        if not subscription:
            raise NotFoundError("api.subscriptions.error.not_found")
        return subscription

    @staticmethod
    def _serialize_for_log(value: Any) -> Any:
        """Convert non-JSON values to storage-friendly representations."""
        if hasattr(value, "value"):
            return value.value
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        return value
    
    # create_subscription_plan removed - users create custom subscriptions instead

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
    
    # _calculate_subscription_total with plan removed - discounts applied directly to subscriptions

    def _validate_billing_frequency(self, billing_cycle: SubscriptionFrequency,
                                    delivery_frequency: SubscriptionFrequency):
        """
        Validate that billing cycle is appropriate for delivery frequency.
        Billing period should not be shorter than delivery period.
        """
        frequency_order = {
            SubscriptionFrequency.DAILY: 1,
            SubscriptionFrequency.WEEKLY: 2,
            SubscriptionFrequency.BIWEEKLY: 3,
            SubscriptionFrequency.MONTHLY: 4
        }

        billing_rank = frequency_order.get(billing_cycle, 4)
        delivery_rank = frequency_order.get(delivery_frequency, 2)

        if billing_rank < delivery_rank:
            raise ValidationError(
                f"Billing cycle ({billing_cycle.value}) cannot be shorter than "
                f"delivery frequency ({delivery_frequency.value}). "
                f"Example: You cannot bill monthly for daily deliveries."
            )

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
        try:
            from ..tasks.subscription_tasks import schedule_subscription_delivery_task
            schedule_subscription_delivery_task.delay(subscription_id)
        except Exception as e:
            current_app.logger.error(f"Failed to schedule subscription delivery: {e}")

    def _send_subscription_confirmation(self, subscription: Subscription):
        """Send subscription confirmation"""
        try:
            from ..tasks.notification_tasks import send_subscription_confirmation_task
            send_subscription_confirmation_task.delay(subscription.id)
        except Exception as e:
            current_app.logger.error(f"Failed to send subscription confirmation: {e}")

    def _send_subscription_notification(self, subscription: Subscription, event_type: str):
        """Send subscription notification"""
        try:
            from ..tasks.notification_tasks import send_subscription_notification_task
            send_subscription_notification_task.delay(subscription.id, event_type)
        except Exception as e:
            current_app.logger.error(f"Failed to send subscription notification: {e}")

    def _cancel_pending_deliveries(self, subscription_id: int):
        """Cancel pending deliveries for subscription"""
        try:
            from ..tasks.subscription_tasks import cancel_subscription_deliveries_task
            cancel_subscription_deliveries_task.delay(subscription_id)
        except Exception as e:
            current_app.logger.error(f"Failed to cancel pending deliveries: {e}")

    def _calculate_items_total(self, items: List[Dict[str, Any]]) -> float:
        """Calculate total amount for subscription items"""
        total = 0.0

        for item in items:
            product: Product = Product.query.get(item['product_id'])
            if not product:
                raise NotFoundError(f"Product {item['product_id']} not found")

            total += float(product.base_price * item['quantity'])

        return total

    def _calculate_next_delivery_date(self, start_date: datetime, frequency: SubscriptionFrequency,
                                     day_of_week: Optional[int] = None,
                                     day_of_month: Optional[int] = None) -> datetime:
        """Calculate next delivery date based on frequency and preferences"""
        if frequency == SubscriptionFrequency.DAILY:
            return start_date + timedelta(days=1)

        elif frequency == SubscriptionFrequency.WEEKLY:
            # If day_of_week specified, find next occurrence of that day
            if day_of_week is not None:
                days_ahead = day_of_week - start_date.weekday()
                if days_ahead <= 0:  # Target day already happened this week
                    days_ahead += 7
                return start_date + timedelta(days=days_ahead)
            else:
                return start_date + timedelta(weeks=1)

        elif frequency == SubscriptionFrequency.BIWEEKLY:
            if day_of_week is not None:
                days_ahead = day_of_week - start_date.weekday()
                if days_ahead <= 0:
                    days_ahead += 14
                else:
                    days_ahead += 7  # Skip to next occurrence after 2 weeks
                return start_date + timedelta(days=days_ahead)
            else:
                return start_date + timedelta(weeks=2)

        elif frequency == SubscriptionFrequency.MONTHLY:
            # If day_of_month specified, use that day
            if day_of_month is not None:
                next_month = start_date + relativedelta(months=1)
                # Handle edge case where day doesn't exist in month (e.g., Feb 30)
                try:
                    return next_month.replace(day=day_of_month)
                except ValueError:
                    # Use last day of month if specified day doesn't exist
                    return next_month + relativedelta(day=31)
            else:
                return start_date + relativedelta(months=1)

        else:
            # Default to weekly
            return start_date + timedelta(weeks=1)

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
    
    def get_billing_info(self, subscription_id: int) -> Dict[str, Any]:
        """
        Get billing information for a subscription

        Args:
            subscription_id: Subscription ID

        Returns:
            Dictionary with billing information
        """
        subscription: Subscription = Subscription.query.get(subscription_id)
        if not subscription:
            raise NotFoundError("Subscription not found")

        # Calculate next billing amount
        next_billing_amount = subscription.billing_amount

        # Get payment method
        payment_method = subscription.payment_method.value or PaymentMethod.CASH

        # Calculate days until next billing
        days_until_billing = 0
        if subscription.next_billing_date:
            # Ensure both datetimes are timezone-aware for comparison
            next_billing = subscription.next_billing_date
            if next_billing.tzinfo is None:
                # If next_billing_date is naive, assume UTC
                next_billing = next_billing.replace(tzinfo=timezone.utc)

            now = datetime.now(timezone.utc)
            days_until_billing = (next_billing.date() - now.date()).days

        return {
            'subscription_id': subscription_id,
            'next_billing_date': subscription.next_billing_date,
            'next_billing_amount': next_billing_amount,
            'payment_method': payment_method.value if hasattr(payment_method, 'value') else payment_method,
            'days_until_billing': max(0, days_until_billing),
            # 'is_trial': subscription.status == SubscriptionStatus.TRIAL,
            # 'trial_ends_at': subscription.trial_end_date if subscription.status == SubscriptionStatus.TRIAL else None
        }

    def calculate_subscription_preview(self, user_id: int, billing_cycle: str,
                                       delivery_frequency: str, items: List[Dict[str, Any]],
                                       discount_percentage: float = 0) -> Dict[str, Any]:
        """
        Calculate preview of subscription before creation

        Args:
            user_id: User ID
            billing_cycle: Billing cycle (monthly, quarterly, annually)
            delivery_frequency: Delivery frequency (daily, weekly, biweekly, monthly)
            items: List of items with product_id and quantity
            discount_percentage: Discount percentage to apply

        Returns:
            Preview information with pricing breakdown
        """
        # Validate user
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")

        # Validate items
        self._validate_subscription_items(items)

        # Calculate base amount
        subtotal = 0
        item_details = []

        for item in items:
            product = Product.query.get(item['product_id'])
            if not product:
                raise NotFoundError(f"Product {item['product_id']} not found")

            quantity = item.get('quantity', 1)
            item_total = product.base_price * quantity
            subtotal += item_total

            item_details.append({
                'product_id': product.id,
                'product_name': product.name,
                'unit_price': float(product.base_price),
                'quantity': quantity,
                'item_total': float(item_total)
            })

        # Apply discount
        discount_amount = (subtotal * discount_percentage / 100) if discount_percentage > 0 else 0
        total_after_discount = subtotal - discount_amount

        # Calculate delivery fee (if applicable)
        delivery_fee = 0  # Can be calculated based on address/distance

        # Calculate final total
        total_amount = total_after_discount + delivery_fee

        # Calculate frequency-based pricing
        frequency_multiplier = {
            'daily': 30,
            'weekly': 4,
            'biweekly': 2,
            'monthly': 1
        }.get(delivery_frequency, 1)

        monthly_cost = total_amount * frequency_multiplier

        return {
            'items': item_details,
            'subtotal': float(subtotal),
            'discount_percentage': discount_percentage,
            'discount_amount': float(discount_amount),
            'delivery_fee': float(delivery_fee),
            'total_amount': float(total_amount),
            'billing_cycle': billing_cycle,
            'delivery_frequency': delivery_frequency,
            'estimated_monthly_cost': float(monthly_cost),
            'estimated_annual_cost': float(monthly_cost * 12),
            'trial_available': True,
            'trial_days': self.trial_days
        }

    def calculate_subscription_statistics(self, user_id: int) -> Dict[str, Any]:
        """
        Calculate subscription statistics for a user

        Args:
            user_id: User ID

        Returns:
            Statistics dictionary
        """
        # Get all user subscriptions
        subscriptions = Subscription.query.filter_by(user_id=user_id).all()

        if not subscriptions:
            return {
                'total_subscriptions': 0,
                'active_subscriptions': 0,
                'total_spent': 0,
                'total_deliveries': 0,
                'average_order_value': 0,
                'total_savings': 0,
                'most_ordered_product': None
            }

        # Calculate statistics
        total_spent = 0
        total_deliveries = 0
        active_count = 0
        product_counts = {}

        for subscription in subscriptions:
            if subscription.status == SubscriptionStatus.ACTIVE:
                active_count += 1

            # Count deliveries (estimated from subscription duration and frequency)
            if subscription.created_at and subscription.frequency:
                days_active = (datetime.now(timezone.utc) - subscription.created_at).days
                deliveries_per_week = {
                    'daily': 7,
                    'weekly': 1,
                    'biweekly': 0.5,
                    'monthly': 0.25
                }.get(subscription.frequency.value if hasattr(subscription.frequency, 'value') else subscription.frequency, 1)

                estimated_deliveries = int((days_active / 7) * deliveries_per_week)
                total_deliveries += estimated_deliveries
                total_spent += subscription.total_amount * estimated_deliveries

            # Count products
            for item in subscription.items:
                product_name = item.product.name if item.product else 'Unknown'
                product_counts[product_name] = product_counts.get(product_name, 0) + item.quantity

        # Find most ordered product
        most_ordered = max(product_counts.items(), key=lambda x: x[1])[0] if product_counts else None

        # Calculate average order value
        avg_order = total_spent / total_deliveries if total_deliveries > 0 else 0

        # Calculate savings (example: 10% off regular pricing)
        estimated_savings = total_spent * 0.1

        return {
            'total_subscriptions': len(subscriptions),
            'active_subscriptions': active_count,
            'total_spent': float(total_spent),
            'total_deliveries': total_deliveries,
            'average_order_value': float(avg_order),
            'total_savings': float(estimated_savings),
            'most_ordered_product': most_ordered
        }

    def skip_next_delivery(self, subscription_id: int, user_id: int = None,
                           skip_reason: str = None) -> Subscription:
        """
        Skip the next delivery for a subscription

        Args:
            subscription_id: Subscription ID
            user_id: Optional user ID for validation
            skip_reason: Reason for skipping

        Returns:
            Updated Subscription object
        """
        subscription = Subscription.query.get(subscription_id)
        if not subscription:
            raise NotFoundError("Subscription not found")

        # Validate ownership
        if user_id and subscription.user_id != user_id:
            raise ValidationError("Subscription does not belong to user")

        # Can only skip active subscriptions
        if subscription.status != SubscriptionStatus.ACTIVE:
            raise ValidationError("Can only skip deliveries for active subscriptions")

        # Calculate next delivery date after skip
        if subscription.next_delivery_date:
            frequency_days = {
                'daily': 1,
                'weekly': 7,
                'biweekly': 14,
                'monthly': 30
            }.get(subscription.frequency.value if hasattr(subscription.frequency, 'value') else subscription.frequency, 7)

            new_next_delivery = subscription.next_delivery_date + timedelta(days=frequency_days)
            subscription.next_delivery_date = new_next_delivery

        # Log the skip
        if hasattr(subscription, 'add_log'):
            subscription.add_log(
                action='delivery_skipped',
                description=f'Delivery skipped. Reason: {skip_reason or "User request"}',
                user_id=user_id
            )

        db.session.commit()

        return subscription

    def _get_frequency_breakdown(self, subscriptions: List[Subscription]) -> Dict[str, int]:
        """Get subscription frequency breakdown"""
        breakdown = {}
        for subscription in subscriptions:
            freq = subscription.frequency.value
            breakdown[freq] = breakdown.get(freq, 0) + 1
        return breakdown
