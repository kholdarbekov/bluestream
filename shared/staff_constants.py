"""
Staff-specific constants for the Water Business Platform.
Used by staff bot, backend, and admin UI.
"""

from shared.status_transitions import driver_delivery_transitions_as_strings

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

# Delivery status transitions a driver may make. The staff bot draws its status
# buttons from this, and StaffService.update_delivery_status refuses anything else
# with STAFF_INVALID_STATUS_TRANSITION. The admin Delivery page reaches that gate
# too, but only with moves this view keeps. It is the driver view in
# shared.status_transitions (single source of truth), which has no CANCELLED (F4).
# Do not edit this dict directly. Update shared/status_transitions.py instead.
DELIVERY_STATUS_TRANSITIONS = driver_delivery_transitions_as_strings()

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

# ---- SALES PAY ----
# The sales-agent pay vocabulary. One block with many readers: the pay models build their
# CHECKs from these tuples, the pay services and serializers publish them, and the staff bot,
# `staff_bot/i18n.py` and the staff seed build key families from them. Migration b7e3c1a95d42
# keeps frozen literal copies, as every migration must, so a member added here also needs a
# migration that widens its CHECK.
SALES_PAY_PERIOD_STATUSES = ('open', 'closed', 'approved', 'paid')
SALES_PAY_PERIOD_ACTIONS = ('close', 'recalculate', 'approve', 'mark_paid')
SALES_PAY_RATE_MODES = ('per_unit', 'percent')
SALES_PAY_LEDGER_KINDS = ('commission_credit', 'commission_reversal', 'commission_difference', 'new_outlet_bonus')
SALES_PAY_COMMISSION_KINDS = SALES_PAY_LEDGER_KINDS[:3]
# A reversal takes the order's units out of its month's count. A difference moves the order's
# level: the money level fell / rose, or a credited product's counted units fell / rose (OQ-B3,
# I-41); pay_rules.difference_cause picks one. v5 retired 'plan_changed': rates apply at month level.
SALES_PAY_REVERSAL_CAUSES = ('not_delivered', 'nothing_received')
SALES_PAY_DIFFERENCE_CAUSES = ('received_reduced', 'received_restored', 'units_reduced', 'units_restored')
SALES_PAY_PENALTY_STATUSES = ('proposed', 'confirmed', 'rejected', 'cancelled')
SALES_PAY_PENALTY_ORIGINS = ('proposal', 'direct')
SALES_PAY_ADJUSTMENT_SOURCES = ('admin', 'carry_forward')
# Where a statement's carry-in came from: last month's carried shortfall, or an owed balance
# this month nets.
SALES_PAY_CARRY_IN_SOURCES = ('carry_forward', 'owed')
SALES_PAY_OUTLET_CHECK_STATUSES = ('tracking', 'qualified', 'prior_customer', 'not_eligible', 'expired')
SALES_PAY_BONUS_RULES = ('orders_with_total', 'orders_any')
SALES_PAY_GATE_RULES = ('band', 'below_min_due')
# 'non_working' is unreachable under the seven-day default (C2, I-37); it stays for a narrowed week.
SALES_PAY_DAY_STATUSES = ('worked', 'non_working', 'holiday', 'unpaid', 'not_employed')
# 'approval' is an order held for a manager by the same-day approval hold.
SALES_PAY_PIPELINE_WAITING = ('approval', 'delivery', 'payment')
# A self-decision (an admin deciding about their own pay) names the input it touched and
# what was done to it; the statement is tagged with both.
SALES_PAY_SELF_DECISION_INPUTS = ('terms', 'employment', 'unpaid_day', 'adjustment', 'penalty', 'order_approval')
SALES_PAY_SELF_DECISION_ACTIONS = ('added', 'set', 'created', 'proposed', 'confirmed', 'cancelled', 'approved')
SALES_PAY_REASON_MIN_LENGTH = 5
# Named so producers pass a member (ruling 66). Each joins SALES_EVENTS together with its
# producer and its seeded row, never before: a member without its row fails /health.
SALES_EVENT_PAY_PENALTY_CONFIRMED = 'pay_penalty_confirmed'
SALES_EVENT_PAY_STATEMENT_APPROVED = 'pay_statement_approved'

# The states `place_order` reports for an agent's order, which the staff bot's receipt line
# renders. 'awaiting_staff_approval' is the same-day hold: an agent's second order at one
# outlet on one local day waits for a manager or an admin.
SALES_AGENT_ORDER_STATES = ('auto_confirmed', 'pending_confirmation', 'confirmed', 'awaiting_staff_approval')
SALES_EVENT_AGENT_ORDER_APPROVED = 'agent_order_approved'
SALES_EVENT_AGENT_ORDER_REJECTED = 'agent_order_rejected'

# Every sales push the staff bot's `/internal/sales-event` door accepts, in producer order: the six
# phase-2 events, then the same-day hold's two outcomes (C14), then the two pay pushes (§7.5).
# Defined HERE, below the pay block, because the C14 and pay names are defined in it.
SALES_EVENTS = (
    SALES_EVENT_OUTLET_APPROVED,
    SALES_EVENT_OUTLET_REJECTED,
    SALES_EVENT_ACTIVATION_REQUESTED,
    SALES_EVENT_AGENT_ORDER_CONFIRMED,
    SALES_EVENT_AGENT_ORDER_DECLINED,
    SALES_EVENT_MORNING_DIGEST,
    SALES_EVENT_AGENT_ORDER_APPROVED,
    SALES_EVENT_AGENT_ORDER_REJECTED,
    SALES_EVENT_PAY_PENALTY_CONFIRMED,
    SALES_EVENT_PAY_STATEMENT_APPROVED,
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
