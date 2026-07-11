from datetime import datetime, timedelta, UTC
from decimal import Decimal
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, Enum, JSON, Index, Numeric
from sqlalchemy.orm import relationship
import uuid
from business_app import db
from shared.enums import SubscriptionStatus, PaymentMethod, SubscriptionFrequency
from business_app.models import TimestampMixin
from business_app.models.translatable import TranslatableMixin, translatable


@translatable("name", "description")
class Subscription(db.Model, TimestampMixin, TranslatableMixin):
    __tablename__ = "subscriptions"
    __table_args__ = (
        Index("idx_subscriptions_status_next_billing", "status", "next_billing_date"),
        Index("idx_subscriptions_status_next_delivery", "status", "next_delivery_date"),
    )

    id = Column(Integer, primary_key=True)
    subscription_number = Column(String(50), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    status = Column(
        Enum(SubscriptionStatus, name="subscription_status", values_callable=lambda x: [e.value for e in x]),
        default="active",
        index=True,
    )

    # Subscription details
    name = Column(String(200), nullable=False)  # Default/fallback name (Uzbek)
    description = Column(Text, nullable=True)  # Default/fallback description (Uzbek)

    # Billing cycle
    billing_cycle = Column(
        Enum(SubscriptionFrequency, name="subscription_frequency", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    billing_amount = Column(Numeric(precision=10, scale=2), nullable=False)
    next_billing_date = Column(DateTime(timezone=True), nullable=False)
    last_billing_date = Column(DateTime(timezone=True), nullable=True)

    # Delivery schedule
    delivery_frequency = Column(
        Enum(SubscriptionFrequency, name="subscription_frequency", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    delivery_day_of_week = Column(Integer, nullable=True)  # 1=Monday, 7=Sunday
    delivery_day_of_month = Column(Integer, nullable=True)  # 1-31
    delivery_time_slot_id = Column(
        Integer, ForeignKey("delivery_time_slots.id"), nullable=True
    )  # User may not have preference
    delivery_address_id = Column(Integer, ForeignKey("addresses.id"), nullable=False)
    next_delivery_date = Column(DateTime(timezone=True), nullable=True)
    last_delivery_date = Column(DateTime(timezone=True), nullable=True)

    # Subscription period
    start_date = Column(DateTime(timezone=True), nullable=False)
    end_date = Column(DateTime(timezone=True), nullable=True)  # null for indefinite
    auto_renew = Column(Boolean, default=True)

    # Payment settings
    payment_method = Column(
        Enum(PaymentMethod, name="payment_method", values_callable=lambda x: [e.value for e in x]), nullable=False
    )
    failed_payment_count = Column(Integer, default=0)

    # Pause/Resume functionality
    paused_at = Column(DateTime(timezone=True), nullable=True)
    pause_reason = Column(String(255), nullable=True)
    pause_start_date = Column(DateTime(timezone=True), nullable=True)
    pause_end_date = Column(DateTime(timezone=True), nullable=True)
    resume_date = Column(DateTime(timezone=True), nullable=True)

    # Analytics
    total_orders_generated = Column(Integer, default=0)
    total_amount_billed = Column(Numeric(precision=10, scale=2), default=Decimal("0.00"))
    failed_billing_attempts = Column(Integer, default=0)
    last_successful_billing = Column(DateTime(timezone=True), nullable=True)

    # Special features
    discount_percentage = Column(Float, default=0.0)  # Subscription discount
    loyalty_points_multiplier = Column(Float, default=1.0)  # Extra loyalty points

    # Relationships
    user = relationship("User", back_populates="subscriptions")
    delivery_address = relationship("UserAddress")
    delivery_time_slot = relationship("DeliveryTimeSlot", foreign_keys=[delivery_time_slot_id])
    subscription_items = relationship("SubscriptionItem", back_populates="subscription", cascade="all, delete-orphan")
    orders = relationship("Order", back_populates="subscription")
    payments = relationship("Payment", back_populates="subscription")
    subscription_logs = relationship("SubscriptionLog", back_populates="subscription", cascade="all, delete-orphan")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not self.subscription_number:
            self.generate_subscription_number()

    def generate_subscription_number(self):
        """Generate unique subscription number"""
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        random_suffix = str(uuid.uuid4().hex[:4]).upper()
        self.subscription_number = f"SUB{timestamp}{random_suffix}"

    def calculate_next_billing_date(self):
        """Calculate next billing date based on billing cycle"""
        if self.billing_cycle == "daily":
            return self.next_billing_date + timedelta(days=1)
        elif self.billing_cycle == "weekly":
            return self.next_billing_date + timedelta(weeks=1)
        elif self.billing_cycle == "monthly":
            # Handle month boundaries properly
            if self.next_billing_date.month == 12:
                return self.next_billing_date.replace(year=self.next_billing_date.year + 1, month=1)
            else:
                return self.next_billing_date.replace(month=self.next_billing_date.month + 1)
        return self.next_billing_date

    def calculate_next_delivery_date(self):
        """Calculate next delivery date based on delivery frequency.

        For weekly: finds the next occurrence of delivery_day_of_week (1=Mon, 7=Sun).
        For monthly: uses delivery_day_of_month, falling back to 28th if day doesn't exist.

        Returns:
            date: The next scheduled delivery date.
        """
        today = datetime.now(UTC).date()

        if self.delivery_frequency == SubscriptionFrequency.DAILY:
            return today + timedelta(days=1)
        elif self.delivery_frequency == SubscriptionFrequency.WEEKLY:
            day_of_week = self.delivery_day_of_week if self.delivery_day_of_week is not None else 1
            days_ahead = day_of_week - today.weekday()
            if days_ahead <= 0:  # Target day already happened this week
                days_ahead += 7
            return today + timedelta(days=days_ahead)
        elif self.delivery_frequency == SubscriptionFrequency.MONTHLY:
            day_of_month = self.delivery_day_of_month if self.delivery_day_of_month is not None else 1
            if today.month == 12:
                next_month = today.replace(year=today.year + 1, month=1, day=day_of_month)
            else:
                try:
                    next_month = today.replace(month=today.month + 1, day=day_of_month)
                except ValueError:  # e.g. Feb 30 → fall back to 28th
                    next_month = today.replace(month=today.month + 1, day=28)
            return next_month

        return today

    def pause(self, reason=None, resume_date=None):
        """Pause subscription (state change only — logging handled by service/API layer)"""
        self.status = SubscriptionStatus.PAUSED
        self.paused_at = datetime.now(UTC)
        self.pause_reason = reason
        self.resume_date = resume_date

    def resume(self):
        """Resume subscription (state change only — logging handled by service/API layer)"""
        self.status = SubscriptionStatus.ACTIVE
        self.paused_at = None
        self.pause_reason = None
        self.resume_date = None

        # Recalculate next billing date
        self.next_billing_date = self.calculate_next_billing_date()

    def cancel(self, reason=None):
        """Cancel subscription (state change only — logging handled by service/API layer)"""
        self.status = SubscriptionStatus.CANCELLED

    def get_total_value(self):
        """Calculate total subscription value per billing cycle"""
        total = sum(float(item.total_price) for item in self.subscription_items)
        discount = total * (self.discount_percentage / 100)
        return total - discount

    def to_dict(self, language=None, include_all_translations=False):
        """Convert to dictionary with multilingual support"""
        result = self.to_dict_multilingual(language, include_all_translations)

        # Add subscription-specific fields
        result.update(
            {
                "subscription_number": self.subscription_number,
                "status": self.status.value if hasattr(self.status, "value") else self.status,
                "billing_cycle": (
                    self.billing_cycle.value if hasattr(self.billing_cycle, "value") else self.billing_cycle
                ),
                "billing_amount": float(self.billing_amount),
                "next_billing_date": self.next_billing_date.isoformat() if self.next_billing_date else None,
                "delivery_frequency": (
                    self.delivery_frequency.value
                    if hasattr(self.delivery_frequency, "value")
                    else self.delivery_frequency
                ),
                "delivery_time_slot_id": self.delivery_time_slot_id,
                "delivery_time_slot": self.delivery_time_slot.to_dict() if self.delivery_time_slot else None,
                "start_date": self.start_date.isoformat() if self.start_date else None,
                "end_date": self.end_date.isoformat() if self.end_date else None,
                "auto_renew": self.auto_renew,
                "discount_percentage": self.discount_percentage,
                "total_orders_generated": self.total_orders_generated,
                "total_amount_billed": float(self.total_amount_billed),
                "subscription_items": [item.to_dict(language) for item in self.subscription_items],
                "delivery_address": self.delivery_address.to_dict() if self.delivery_address else None,
            }
        )

        return result


class SubscriptionItem(db.Model, TimestampMixin):
    __tablename__ = "subscription_items"

    id = Column(Integer, primary_key=True)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Numeric(precision=10, scale=2), nullable=False)
    total_price = Column(Numeric(precision=10, scale=2), nullable=False)
    special_instructions = Column(Text, nullable=True)

    subscription = relationship("Subscription", back_populates="subscription_items")
    product = relationship("Product")

    def calculate_total(self):
        """Calculate total price for this subscription item"""
        self.total_price = float(self.unit_price) * self.quantity
        return self.total_price

    def to_dict(self, language=None):
        return {
            "id": self.id,
            "product_id": self.product_id,
            "quantity": self.quantity,
            "unit_price": float(self.unit_price),
            "total_price": float(self.total_price),
            "special_instructions": self.special_instructions,
            "product": self.product.to_dict(language=language) if self.product else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class SubscriptionLog(db.Model, TimestampMixin):
    __tablename__ = "subscription_logs"

    id = Column(Integer, primary_key=True)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=False, index=True)
    action = Column(String(50), nullable=False)  # created, paused, resumed, cancelled, billed, etc.
    details = Column(Text, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # Who performed the action
    extra_data = Column(JSON, default={})

    subscription = relationship("Subscription", back_populates="subscription_logs")
    user = relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "action": self.action,
            "details": self.details,
            "extra_data": self.extra_data,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "user": {"id": self.user.id, "name": self.user.full_name} if self.user else None,
        }
