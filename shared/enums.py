"""
Shared enum definitions for the Water Business Platform.
Canonical source of truth for enums used across backend, bot, and admin services.

Import directly from this module — never via business_app.utils.constants.
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
    PARTIALLY_PAID = 'partially_paid'
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


class FiscalizationStatus(Enum):
    """Canonical fiscalization state for payment receipts."""

    NOT_REQUIRED = 'not_required'
    PENDING = 'pending'
    PROCESSING = 'processing'
    COMPLETED = 'completed'
    FAILED = 'failed'


class MarkingCodeStatus(Enum):
    """Current lifecycle state of a product marking code."""

    AVAILABLE = 'available'
    RESERVED = 'reserved'
    USED = 'used'
    ARCHIVED = 'archived'


class MarkingCodeLedgerEventType(Enum):
    """Audit/event history for marking code inventory transitions."""

    CREATED = 'created'
    IMPORTED = 'imported'
    RESERVED = 'reserved'
    RELEASED = 'released'
    USED = 'used'
    UTILISED = 'utilised'
    ARCHIVED = 'archived'
    RESTORED = 'restored'


class CashCollectionSource(Enum):
    """COD cash collection source enumeration."""

    DELIVERY_COMPLETION = 'delivery_completion'
    NEXT_DELIVERY = 'next_delivery'
    STANDALONE_MEETING = 'standalone_meeting'
    ADMIN_ADJUSTMENT = 'admin_adjustment'
    PERSONAL_CARD_TRANSFER = 'personal_card_transfer'
    BACKFILL = 'backfill'


class DriverCashSessionStatus(Enum):
    """Driver cash reconciliation session state."""

    OPEN = 'open'
    PARTIAL = 'partial'
    SUBMITTED = 'submitted'
    VERIFIED = 'verified'
    MISMATCH = 'mismatch'
    OVERDUE = 'overdue'
    RESOLVED = 'resolved'


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


class AssignmentSource(Enum):
    """How a driver came to own a delivery — for audit/notes, not behavior gating
    beyond what assign_driver's explicit flags express."""
    BOT_SELF_ACCEPT = "bot_self_accept"
    ADMIN_ASSIGN = "admin_assign"
    ADMIN_BULK = "admin_bulk"
    AUTO = "auto"
    REASSIGN = "reassign"


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


class EntitySubtype(Enum):
    """Subtype distinguishing real corporate workplaces from grocery-store retail accounts.

    Only meaningful when the parent user has user_type=ENTITY.
    Workplaces use the prepaid bottle-unit ledger (BUSINESS_ACCOUNT payment).
    Grocery stores pay cash/card on or after delivery and track debt in money.
    """
    WORKPLACE = 'workplace'
    GROCERY_STORE = 'grocery_store'


class CorporateContractTrackingMode(Enum):
    """How a corporate contract's prepayment account tracks balances.

    UNITS  -- per-product bottle-unit ledger (workplace, original behavior).
    AMOUNT -- single money-only ledger on the account (grocery store).
    """
    UNITS = 'units'
    AMOUNT = 'amount'


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


class BottleLedgerEventType(Enum):
    """Returnable bottle ledger event type."""
    DELIVERY = 'delivery'
    RETURN_ON_DELIVERY = 'return_on_delivery'
    STANDALONE_COLLECTION = 'standalone_collection'
    ADMIN_ADJUSTMENT = 'admin_adjustment'
    FINE_ISSUED = 'fine_issued'
    FINE_REVERSED = 'fine_reversed'
    FINE_PAID = 'fine_paid'
    INITIAL_BALANCE = 'initial_balance'


class BottleFineStatus(Enum):
    """Returnable bottle fine status."""
    PENDING = 'pending'
    INVOICED = 'invoiced'
    PAID = 'paid'
    WAIVED = 'waived'


class DriverBottleSessionStatus(Enum):
    """Driver bottle trip session lifecycle states.

    A driver opens a session when loading from the warehouse and closes it
    when returning. Only one OPEN session per driver is allowed at a time.
    """
    OPEN         = 'open'          # Active trip, bottles loaded from WH
    CLOSED       = 'closed'        # Returned to WH, discrepancy computed
    FORCE_CLOSED = 'force_closed'  # Admin override for an abandoned session
    CANCELLED    = 'cancelled'     # Admin: session opened but no bottles loaded


class DriverBottleTransferStatus(Enum):
    """Status of a driver-to-driver mid-route bottle transfer."""
    PENDING   = 'pending'    # Sender submitted; receiver not yet confirmed
    CONFIRMED = 'confirmed'  # Receiver accepted the declared quantity
    DISPUTED  = 'disputed'   # Receiver claims a different quantity
    RESOLVED  = 'resolved'   # Admin arbitrated the dispute


class DriverSessionMembershipStatus(Enum):
    """Lifecycle state for a driver joining another driver's open bottle session.

    A driver who does not have their own OPEN session may join a colleague's
    session (e.g. two drivers sharing the same truck).  While ACTIVE the
    member driver's order acceptances are validated against and tallied to the
    owner's session.  At most ONE active membership is allowed per driver at
    any time (enforced via a partial unique index).
    """
    ACTIVE  = 'active'   # Member is currently using the owner's session
    LEFT    = 'left'     # Member voluntarily left the session
    REVOKED = 'revoked'  # Session was closed/force-closed; membership auto-terminated
