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
    'OUTLET_CREATED': 'outlet_created',
    'OUTLET_ACTIVATION_REQUESTED': 'outlet_activation_requested',
    'OUTLET_APPROVED': 'outlet_approved',
    'OUTLET_REJECTED': 'outlet_rejected',
    'VISIT_STARTED': 'visit_started',
    'VISIT_CLOSED': 'visit_closed',
    'VISIT_ABANDONED': 'visit_abandoned',
    'AGENT_ORDER_CREATED': 'agent_order_created',
    'AGENT_TRYOUT_CREATED': 'agent_tryout_created',
    'VISIT_PHOTO_ADDED': 'visit_photo_added',
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
STAFF_BOT_ROLES = ['delivery_driver', 'operator', 'sales_agent']

# The sales-agent event vocabulary: one definition for THREE consumers.
#
# `staff_bot/webhook_server.py::sales_event_handler` refuses anything outside
# this tuple; `staff_bot/i18n.py::_add_dynamic_family_keys` and
# `scripts/seed_staff_translations.py::_add_dynamic_keys` build
# `staff.sales.notify.<event>` from it. The key is an f-string, so the literal
# scraper behind /health cannot see it — a seventh event added in one place and
# forgotten in another either ships a humanised key tail to an agent or is
# refused at the door for a message the backend believes it delivered.
#
# Produced by `business_app/services/sales/notifications.py` (outlet events and
# the store's answer to an agent order) and by
# `business_app/tasks/sales_agent_tasks.py` (the managers' activation ping and
# the agent's morning digest).
#
# Each event is NAMED, and the tuple is built from the names (ruling 66):
# producers pass a member, and a bare-string tuple has no member to pass. A
# literal spelled at a producer is how a message gets minted, delivered and
# then refused at the door by the same tuple.
SALES_EVENT_OUTLET_APPROVED = 'outlet_approved'
SALES_EVENT_OUTLET_REJECTED = 'outlet_rejected'
SALES_EVENT_ACTIVATION_REQUESTED = 'activation_requested'
SALES_EVENT_AGENT_ORDER_CONFIRMED = 'agent_order_confirmed'
SALES_EVENT_AGENT_ORDER_DECLINED = 'agent_order_declined'
SALES_EVENT_MORNING_DIGEST = 'morning_digest'

SALES_EVENTS = (
    SALES_EVENT_OUTLET_APPROVED,
    SALES_EVENT_OUTLET_REJECTED,
    SALES_EVENT_ACTIVATION_REQUESTED,
    SALES_EVENT_AGENT_ORDER_CONFIRMED,
    SALES_EVENT_AGENT_ORDER_DECLINED,
    SALES_EVENT_MORNING_DIGEST,
)

# Risk flags a driver cash-reconciliation session can carry.
#
# SSOT for a value with TWO expressions: `DriverReconciliationService.
# _build_risk_flags` PRODUCES them and the staff bot RENDERS them (via
# `staff.delivery.risk_flag.<flag>`). Adding a flag in the service without a
# translation used to print the bare snake_case identifier onto a driver's
# money screen in every language, so the producer, the translation catalog and
# the bot's required-key set all read this list.
RECONCILIATION_RISK_FLAGS = [
    'cash_on_hand_escalation',
    'cash_on_hand_warning',
    'repeated_mismatch_pattern',
    'submission_overdue',
    'reconciliation_warning_due',
]

# Ceiling for a single truck load-out.
#
# SSOT for a value with TWO enforcement points: the staff bot refuses it at the
# keypad, where the driver can still be told what was wrong with what they
# typed, and `DriverBottleSessionOpenRequest` refuses it at the HTTP boundary,
# so a direct API call, a replayed request or any future client is bounded too.
# A bot-only bound is a bound the backend does not have:
# `DriverBottleSession.bottles_loaded` is a 4-byte PostgreSQL integer, so an
# unbounded count -- a phone number typed into the quantity box -- reached the
# depot as a DataError 500 with no hint in it.
#
# 500 x 18.9 l is ~9.5 tonnes, far past any van on this fleet, and ~4 million
# times below the column's own ceiling (2147483647): anything past this number
# is a typo, not a shift, and no keypad slip reaches the column.
MAX_BOTTLES_PER_SESSION = 500

# Ceiling for the end-of-shift RETURN count -- a STORAGE bound, not a business
# rule. It says nothing about how many bottles a driver may hand back, and it
# refuses no return a driver could actually be holding.
#
# Deliberately NOT `MAX_BOTTLES_PER_SESSION`: over-returning is legitimate on
# this side. Everything the truck left with PLUS every empty collected at a
# door comes back through this one field, and a place can be over-returned all
# on its own (tests/unit/test_staff_bot_over_returned.py), so a
# business-plausibility ceiling of any size would eventually turn away a real
# shift. The only thing left to refuse is a number the storage cannot carry:
# `DriverBottleSession.bottles_returned_to_warehouse` is a 4-byte PostgreSQL
# integer, so a keypad slip -- a phone number typed into the quantity box --
# reached the depot as a DataError 500 with no hint in it, exactly as it did on
# the load side before MAX_BOTTLES_PER_SESSION existed.
#
# The number is the column's own ceiling (2147483647) less one full load-out of
# headroom, because `DriverBottleSession.compute_discrepancy` subtracts this
# count from what the session carried and stores the result in another 4-byte
# column: reserving what a session may legally have loaded keeps BOTH writes
# inside the type for every value this bound admits. What it turns away starts
# at ~2.1 BILLION bottles -- over 4 million full truck load-outs handed back at
# one depot in one shift -- so nothing but a typo can reach it.
#
# Enforced twice, like the load-out ceiling: the staff bot refuses it at the
# keypad, where the driver can still be told what was wrong with what they
# typed, and `DriverBottleSessionCloseRequest` / `AdminForceCloseSessionRequest`
# refuse it at the HTTP boundary, so a direct API call, a replayed request or
# any future client is bounded too.
BOTTLE_RETURN_COLUMN_CEILING = 2_147_483_647 - MAX_BOTTLES_PER_SESSION
