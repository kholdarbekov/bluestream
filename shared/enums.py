"""
Shared enum definitions for the Water Business Platform.
Canonical source of truth for enums used across backend, bot, and admin services.

Backend re-exports these via business_app.utils.constants for backward compatibility.
"""
from enum import Enum


class OrderStatus(Enum):
    """Order status enumeration"""
    PENDING = 'pending'
    CONFIRMED = 'confirmed'
    PREPARING = 'preparing'
    OUT_FOR_DELIVERY = 'out_for_delivery'
    DELIVERED = 'delivered'
    CANCELLED = 'cancelled'
    RETURNED = 'returned'


class PaymentStatus(Enum):
    """Payment status enumeration"""
    PENDING = 'pending'
    PROCESSING = 'processing'
    COMPLETED = 'completed'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    REFUNDED = 'refunded'
    PARTIALLY_REFUNDED = 'partially_refunded'


class PaymentMethod(Enum):
    """Payment method enumeration"""
    CASH = 'cash'
    CARD = 'card'
    PAYME = 'payme'
    CLICK = 'click'
    LOYALTY_POINTS = 'loyalty_points'
    BUSINESS_ACCOUNT = 'business_account'


class DeliveryStatus(Enum):
    """Delivery status enumeration"""
    SCHEDULED = 'scheduled'
    PENDING = 'pending'
    ASSIGNED = 'assigned'
    PICKED_UP = 'picked_up'
    IN_TRANSIT = 'in_transit'
    ARRIVED = 'arrived'
    DELIVERED = 'delivered'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    RETURNED = 'returned'


class SubscriptionStatus(Enum):
    """Subscription status enumeration"""
    ACTIVE = 'active'
    PAUSED = 'paused'
    CANCELLED = 'cancelled'
    EXPIRED = 'expired'
    TRIAL = 'trial'


class SubscriptionFrequency(Enum):
    """Subscription frequency enumeration"""
    DAILY = 'daily'
    WEEKLY = 'weekly'
    BIWEEKLY = 'biweekly'
    MONTHLY = 'monthly'


class UserRole(Enum):
    """User role enumeration"""
    CUSTOMER = 'customer'
    ADMIN = 'admin'
    MANAGER = 'manager'
    DELIVERY_DRIVER = 'delivery_driver'
    OPERATOR = 'operator'


class UserType(Enum):
    """Top-level actor classification for users."""
    INDIVIDUAL = 'individual'
    ENTITY = 'entity'
    STAFF = 'staff'


class UserStatus(Enum):
    """User status enumeration"""
    ACTIVE = 'active'
    INACTIVE = 'inactive'
    BANNED = 'banned'
    PENDING_VERIFICATION = 'pending_verification'


class TryoutStatus(Enum):
    """Try-out lifecycle status."""
    DRAFT = 'draft'
    SCHEDULED = 'scheduled'
    ACTIVE = 'active'
    CLOSED = 'closed'
    CANCELLED = 'cancelled'


class TryoutOutcome(Enum):
    """Commercial outcome of a try-out."""
    PENDING = 'pending'
    CONVERTED = 'converted'
    DECLINED = 'declined'


class TryoutTaskType(Enum):
    """Operational task type for try-outs."""
    HANDOFF = 'handoff'
    PICKUP = 'pickup'


class TryoutTaskStatus(Enum):
    """Driver task status for try-outs."""
    OPEN = 'open'
    ASSIGNED = 'assigned'
    COMPLETED = 'completed'
    CANCELLED = 'cancelled'


class TryoutBottleLedgerEventType(Enum):
    """Bottle custody ledger event type."""
    HANDOFF = 'handoff'
    PICKUP = 'pickup'
    ADJUSTMENT = 'adjustment'
    VOID = 'void'


class UserGender(Enum):
    """User gender enumeration"""
    MALE = 'male'
    FEMALE = 'female'
    UNKNOWN = 'unknown'
