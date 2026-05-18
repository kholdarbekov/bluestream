"""
Staff-specific constants for the Water Business Platform.
Used by staff bot, backend, and admin UI.
"""

from shared.status_transitions import delivery_transitions_as_strings

# Staff notification types (used in Notification model's notification_type field)
STAFF_NOTIFICATION_TYPES = {
    'new_order_staff': 'New order available for pickup',
    'order_assigned_staff': 'Order assigned by admin',
    'order_reassigned_staff': 'Delivery reassigned to another person',
    'order_cancelled_staff': 'Order cancelled',
}

# Staff activity log action types
STAFF_ACTIONS = {
    'DELIVERY_ACCEPTED': 'delivery_accepted',
    'DELIVERY_STATUS_UPDATED': 'delivery_status_updated',
    'ORDER_CREATED': 'order_created',
    'USER_CREATED': 'user_created',
    'ORDER_PREPARING': 'order_preparing',
    'STAFF_LOGIN': 'staff_login',
}

# Delivery status transitions allowed from staff bot.
# Derived from shared.status_transitions (single source of truth) — do not edit
# this dict directly. Update shared/status_transitions.py instead.
DELIVERY_STATUS_TRANSITIONS = delivery_transitions_as_strings()

# Order status sync when delivery status changes
DELIVERY_TO_ORDER_STATUS_SYNC = {
    'picked_up': 'out_for_delivery',
    'delivered': 'delivered',
}

# Failed delivery reasons
FAILED_DELIVERY_REASONS = [
    'customer_unavailable',
    'wrong_address',
    'customer_refused',
    'product_damaged',
    'other',
]

# Staff roles that can access the staff bot
STAFF_BOT_ROLES = ['delivery_driver', 'operator']
