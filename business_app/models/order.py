from datetime import datetime, timedelta
from decimal import Decimal
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, Enum, JSON, Index, Numeric
from sqlalchemy.orm import relationship, backref
from sqlalchemy.ext.hybrid import hybrid_property
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
import uuid
from business_app import db
from business_app.utils.constants import OrderStatus, PaymentMethod, PaymentStatus, DeliveryStatus
from business_app.models import TimestampMixin


class Order(db.Model, TimestampMixin):
    __tablename__ = 'orders'
    
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
    delivery_date = Column(DateTime, nullable=True)
    delivery_time_slot = Column(String(20), nullable=True)  # "09:00-12:00"
    delivery_notes = Column(Text, nullable=True)
    is_urgent = Column(Boolean, default=False)
    
    # Payment
    payment_method = Column(Enum(PaymentMethod, name='payment_method', values_callable=lambda x: [e.value for e in x]), nullable=True)
    is_paid = Column(Boolean, default=False, index=True)
    paid_at = Column(DateTime, nullable=True)
    
    # Special fields
    is_subscription_order = Column(Boolean, default=False)
    subscription_id = Column(Integer, ForeignKey('subscriptions.id'), nullable=True)
    loyalty_points_used = Column(Integer, default=0)
    loyalty_points_earned = Column(Integer, default=0)
    
    # Order source tracking
    order_source = Column(String(20), default='web')  # web, telegram, mobile, phone
    
    # Relationships
    user = relationship('User', back_populates='orders')
    delivery_address = relationship('UserAddress', back_populates='orders')
    order_items = relationship('OrderItem', back_populates='order', cascade='all, delete-orphan')
    payments = relationship('Payment', back_populates='order')
    delivery = relationship('Delivery', back_populates='order', uselist=False)
    subscription = relationship('Subscription', back_populates='orders')
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not self.order_number:
            self.generate_order_number()
    
    def generate_order_number(self):
        """Generate unique order number"""
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        random_suffix = str(uuid.uuid4().hex[:4]).upper()
        self.order_number = f"WB{timestamp}{random_suffix}"
    
    def calculate_total(self):
        """Calculate order total including discounts and delivery fee"""
        self.subtotal = sum(item.total_price for item in self.order_items)
        
        # Calculate loyalty points discount (1 point = 100 UZS)
        self.loyalty_discount = Decimal(str(self.loyalty_points_used)) * Decimal('100')
        
        # Calculate final total
        self.total_amount = self.subtotal - self.discount_amount - self.loyalty_discount + self.delivery_fee
        
        # Calculate loyalty points earned (1% of total)
        if not self.is_subscription_order:
            self.loyalty_points_earned = int(self.total_amount * Decimal('0.01') / Decimal('100'))
        
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
    changed_at = Column(DateTime, nullable=False, default=datetime.utcnow)
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