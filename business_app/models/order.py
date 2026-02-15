import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, Enum, JSON, Index, Numeric, text
from sqlalchemy.orm import relationship, backref
from business_app import db
from business_app.utils.constants import OrderStatus, PaymentMethod, PaymentStatus, DeliveryStatus, ORDER_SOURCE_PREFIXES
from business_app.models import TimestampMixin
from business_app.models.order_sequence import OrderSequence
from business_app.utils.helpers import generate_random_string

logger = logging.getLogger(__name__)


class Order(db.Model, TimestampMixin):
    __tablename__ = 'orders'
    __table_args__ = (
        Index('idx_orders_user_status', 'user_id', 'status'),
        Index('idx_orders_status_created', 'status', 'created_at'),
        Index('idx_orders_user_created', 'user_id', 'created_at'),
    )

    id = Column(Integer, primary_key=True)
    order_number = Column(String(50), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    status = Column(Enum(OrderStatus, name='order_status', values_callable=lambda x: [e.value for e in x]), default=OrderStatus.PENDING, index=True)
    
    # Pricing
    subtotal = Column(Numeric(precision=10, scale=2), nullable=False, default=Decimal('0.00'))
    discount_amount = Column(Numeric(precision=10, scale=2), default=Decimal('0.00'))
    delivery_fee = Column(Numeric(precision=10, scale=2), default=Decimal('0.00'))
    loyalty_discount = Column(Numeric(precision=10, scale=2), default=Decimal('0.00'))
    total_amount = Column(Numeric(precision=10, scale=2), nullable=False, default=Decimal('0.00'))
    
    # Delivery information
    delivery_address_id = Column(Integer, ForeignKey('addresses.id'), nullable=True)
    delivery_date = Column(DateTime(timezone=True), nullable=True)
    delivery_time_slot = Column(String(20), nullable=True)  # "09:00-12:00"
    delivery_notes = Column(Text, nullable=True)
    is_urgent = Column(Boolean, default=False)
    
    # Payment
    payment_method = Column(Enum(PaymentMethod, name='payment_method', values_callable=lambda x: [e.value for e in x]), nullable=True)
    is_paid = Column(Boolean, default=False, index=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    
    # Special fields
    is_subscription_order = Column(Boolean, default=False)
    subscription_id = Column(Integer, ForeignKey('subscriptions.id'), nullable=True)
    loyalty_points_used = Column(Integer, default=0)
    loyalty_points_earned = Column(Integer, default=0)
    
    # Order source tracking
    order_source = Column(String(20), default='web')  # web, telegram, mobile, phone

    # Staff tracking (which operator/staff created the order, null for self-service)
    created_by_staff_id = Column(Integer, ForeignKey('users.id'), nullable=True)

    # Relationships
    user = relationship('User', foreign_keys=[user_id], back_populates='orders')
    created_by_staff = relationship('User', foreign_keys=[created_by_staff_id])
    delivery_address = relationship('UserAddress', back_populates='orders')
    order_items = relationship('OrderItem', back_populates='order', cascade='all, delete-orphan')
    payment = relationship('Payment', back_populates='order', uselist=False)
    delivery = relationship('Delivery', back_populates='order', uselist=False)
    subscription = relationship('Subscription', back_populates='orders')
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not self.order_number:
            self.order_number = self._generate_order_number()

    def _generate_order_number(self) -> str:
        """
        Generate a unique order number with format: {PREFIX}_{SEQUENCE}_{YY}

        Args:
            order_source: The source of the order (telegram, web, phone, admin, api, mobile)

        Returns:
            Order number string, e.g., "TG_000042_26"

        The sequence resets annually on January 1st for each source prefix.
        """

        # Get prefix for source (default to 'WB' for unknown sources)
        prefix = ORDER_SOURCE_PREFIXES.get(self.order_source, 'WB')

        # Get current year
        current_year = datetime.now(timezone.utc).year
        year_suffix = str(current_year)[-2:]  # Last 2 digits

        # Get next sequence number atomically using PostgreSQL UPSERT
        try:
            result = db.session.execute(
                text("""
                    INSERT INTO order_sequences (source_prefix, year, current_sequence, created_at, updated_at)
                    VALUES (:prefix, :year, 1, NOW(), NOW())
                    ON CONFLICT (source_prefix, year)
                    DO UPDATE SET
                        current_sequence = order_sequences.current_sequence + 1,
                        updated_at = NOW()
                    RETURNING current_sequence
                """),
                {'prefix': prefix, 'year': current_year}
            )
            sequence = result.scalar()

            # Format: PREFIX + _ + 6-digit sequence + _ + 2-digit year
            return f"{prefix}_{sequence:06d}_{year_suffix}"

        except Exception as e:
            # Fallback to legacy format if sequence generation fails
            logger.error(f"Failed to generate sequential order number: {e}")
            timestamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
            random_suffix = generate_random_string(4).upper()
            return f"WB{timestamp}{random_suffix}"
    
    def calculate_total(self):
        """Calculate order total including discounts and delivery fee"""
        self.subtotal = sum(item.total_price for item in self.order_items)
        
        # Calculate loyalty points discount (1 point = 100 UZS)
        self.loyalty_discount = Decimal(str(self.loyalty_points_used)) * Decimal('100')
        
        # Calculate final total
        self.total_amount = self.subtotal - self.discount_amount - self.loyalty_discount + self.delivery_fee
        
        # NOTE: loyalty_points_earned is calculated by LoyaltyService.calculate_points_for_purchase()
        # when the order is confirmed/processed, using LoyaltyProgram configuration and tier multipliers.
        # Do NOT calculate points here to avoid duplication and ensure proper program settings are used.
        
        return self.total_amount
    
    def can_be_cancelled(self):
        """Check if order can be cancelled"""
        return self.status in [OrderStatus.PENDING, OrderStatus.CONFIRMED]
    
    def to_dict(self):
        return {
            'id': self.id,
            'order_number': self.order_number,
            'status': self.status.value,
            'subtotal': self.subtotal,
            'discount_amount': self.discount_amount,
            'delivery_fee': self.delivery_fee,
            'loyalty_discount': self.loyalty_discount,
            'total_amount': self.total_amount,
            'delivery_date': self.delivery_date.isoformat() if self.delivery_date else None,
            'delivery_time_slot': self.delivery_time_slot,
            'is_paid': self.is_paid,
            'is_urgent': self.is_urgent,
            'loyalty_points_used': self.loyalty_points_used,
            'loyalty_points_earned': self.loyalty_points_earned,
            'order_source': self.order_source,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'order_items': [item.to_dict() for item in self.order_items],
            'delivery_address': self.delivery_address.to_dict() if self.delivery_address else None,
            'delivery': self.delivery.to_dict() if self.delivery else None
        }

class OrderItem(db.Model):
    __tablename__ = 'order_items'
    
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Numeric(precision=10, scale=2), nullable=False)
    discount_amount = Column(Numeric(precision=10, scale=2), default=Decimal('0.00'))
    total_price = Column(Numeric(precision=10, scale=2), nullable=False)
    
    order = relationship('Order', back_populates='order_items')
    # Removed back_populates since Product model doesn't have order_items relationship
    product = relationship('Product')
    
    def calculate_total(self):
        """Calculate total price for this item"""
        self.total_price = (self.unit_price * Decimal(str(self.quantity))) - self.discount_amount
        return self.total_price
    
    def to_dict(self):
        return {
            'id': self.id,
            'product_id': self.product_id,
            'quantity': self.quantity,
            'unit_price': self.unit_price,
            'discount_amount': self.discount_amount,
            'total_price': self.total_price,
            'product': self.product.to_dict() if self.product else None
        }

class OrderStatusHistory(db.Model, TimestampMixin):
    """Track order status changes"""
    __tablename__ = 'order_status_history'
    
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False, index=True)
    old_status = Column(Enum(OrderStatus, name='order_status', values_callable=lambda x: [e.value for e in x]), nullable=False)
    new_status = Column(Enum(OrderStatus, name='order_status', values_callable=lambda x: [e.value for e in x]), nullable=False)
    changed_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    changed_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    notes = Column(Text, nullable=True)
    
    # Additional context
    reason = Column(String(100), nullable=True)  # cancelled_by_customer, delivery_failed, etc.
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(500), nullable=True)
    
    order = relationship('Order', backref='status_history')
    changed_by_user = relationship('User', foreign_keys=[changed_by])
    
    def to_dict(self):
        return {
            'id': self.id,
            'order_id': self.order_id,
            'old_status': self.old_status.value if self.old_status else None,
            'new_status': self.new_status.value if self.new_status else None,
            'changed_by': self.changed_by,
            'changed_at': self.changed_at.isoformat() if self.changed_at else None,
            'notes': self.notes,
            'reason': self.reason,
            'changed_by_user': {
                'id': self.changed_by_user.id,
                'name': f"{self.changed_by_user.first_name} {self.changed_by_user.last_name}",
                'role': self.changed_by_user.role.value
            } if self.changed_by_user else None
        }