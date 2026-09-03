"""
Business configuration — the single source of truth for env-driven business,
money, and auth tunables shared across every service (backend, Celery, the
customer telegram_bot, and the staff_bot).

This is a PURE module: it imports only ``os`` and holds no Flask / app-context
state, so it is safe to import from ``business_app`` (including at config-class
definition time), Celery workers, and both bots — exactly like
``shared/constants.py::DISPLAY_TIMEZONE``.

Single-default rule: each value's default literal lives HERE exactly once.
``business_app/config/base.py`` derives its Flask config keys from this module;
do NOT re-declare the literal there, and do NOT add per-call-site ``.get(KEY,
literal)`` fallbacks elsewhere — read ``current_app.config["KEY"]`` (backend) or
import the name from here (bots).
"""

import os


def _int(name: str, default: int) -> int:
    """Read an int env var, treating unset/empty as the default."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _float(name: str, default: float) -> float:
    """Read a float env var, treating unset/empty as the default."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _bool(name: str, default: bool) -> bool:
    """Read a boolean env var, treating unset/empty as the default.

    Truthy spellings are exactly {"1", "true", "yes", "on"}, case-insensitively;
    any OTHER non-empty value is False. Deliberately strict in that direction:
    a typo in a deployed value must never be read as "on" by accident. Note
    this is orthogonal to the default — an unset variable means "no opinion"
    and yields whatever the caller declared.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ─── Orders ─────────────────────────────────────────────────────────────
MIN_ORDER_AMOUNT = _int("MIN_ORDER_AMOUNT", 20000)  # UZS — minimum order floor
MAX_ORDER_ITEMS = _int("MAX_ORDER_ITEMS", 50)
MAX_CART_ITEMS = _int("MAX_CART_ITEMS", 50)
MAX_QUANTITY_PER_ITEM = _int("MAX_QUANTITY_PER_ITEM", 100)
LARGE_ORDER_THRESHOLD_UZS = _int("LARGE_ORDER_THRESHOLD_UZS", 500000)  # fraud/anomaly flag

# ─── Delivery ───────────────────────────────────────────────────────────
# Delivery is currently always free (default 0 matches the live .env). The keys
# stay env-driven so a future zone-based fee model can set them per environment.
# There is intentionally NO amount-based free-delivery threshold.
DEFAULT_DELIVERY_FEE = _int("DEFAULT_DELIVERY_FEE", 0)  # UZS
EMERGENCY_DELIVERY_FEE = _int("EMERGENCY_DELIVERY_FEE", 0)  # UZS

# Scheduled (future-dated) orders. An order carrying a `delivery_date` is held
# out of the driver pool until that day's release moment; see
# OrderScheduleService. The horizon is how far ahead an operator may book.
MAX_SCHEDULE_HORIZON_DAYS = _int("MAX_SCHEDULE_HORIZON_DAYS", 15)
# Fallback release time used ONLY when no active driver is rostered at all, so
# a scheduled order can never strand on an empty roster. Matches the
# DeliveryPerson.working_hours_start column default.
DEFAULT_DISPATCH_OPEN_TIME = os.environ.get("DEFAULT_DISPATCH_OPEN_TIME") or "09:00"
SCHEDULED_RELEASE_SWEEP_MINUTES = _int("SCHEDULED_RELEASE_SWEEP_MINUTES", 5)

# ─── Loyalty ────────────────────────────────────────────────────────────
# Earning rate / bonus amounts / tier thresholds / expiry are all DB-driven
# (LoyaltyProgram + LoyaltyTierConfig). LOYALTY_POINTS_RATIO is only the
# bootstrap default for a new default program. Points are redeemed ONLY via
# rewards (LoyaltyReward.points_cost) — no direct points→UZS conversion.
LOYALTY_POINTS_RATIO = _int("LOYALTY_POINTS_RATIO", 250)  # UZS per earned point (bootstrap default)


# ─── OTP / Auth ─────────────────────────────────────────────────────────
OTP_EXPIRY_SECONDS = _int("OTP_EXPIRY_SECONDS", 300)  # generic/email OTP (5 min)
# Phone-registration OTP is a deliberately distinct, shorter flow (3 min) — do
# not collapse it into OTP_EXPIRY_SECONDS.
PHONE_OTP_EXPIRY = _int("PHONE_OTP_EXPIRY", 180)
PHONE_OTP_RESEND_COOLDOWN = _int("PHONE_OTP_RESEND_COOLDOWN", 60)
PHONE_OTP_MAX_ATTEMPTS = _int("PHONE_OTP_MAX_ATTEMPTS", 5)
PHONE_OTP_LOCKOUT_DURATION = _int("PHONE_OTP_LOCKOUT_DURATION", 600)
OTP_CODE_LENGTH = _int("OTP_CODE_LENGTH", 6)
PASSWORD_MIN_LENGTH = _int("PASSWORD_MIN_LENGTH", 8)
MAX_LOGIN_ATTEMPTS = _int("MAX_LOGIN_ATTEMPTS", 5)

# ─── Post-delivery admin edit windows (hours) ────────────────────────────
# Mirrors ORDER_EDIT_WINDOW_HOURS;
# CASH_EDIT_WINDOW_HOURS governs how long after delivery an ADMIN may correct
# the driver-collected cash amount on a delivered COD order.
ORDER_EDIT_WINDOW_HOURS = _int("ORDER_EDIT_WINDOW_HOURS", 72)
CASH_EDIT_WINDOW_HOURS = _int("CASH_EDIT_WINDOW_HOURS", 72)

# ─── COD custody thresholds ─────────────────────────────────────────────
COD_CASH_WARNING_THRESHOLD_UZS = _int("COD_CASH_WARNING_THRESHOLD_UZS", 200000)
COD_CASH_ESCALATION_THRESHOLD_UZS = _int("COD_CASH_ESCALATION_THRESHOLD_UZS", 400000)

# ─── COD debt cap ───────────────────────────────────────────────────────
# Cash-on-delivery is refused only when BOTH arms fire: the scope (a customer's
# linked cluster, or a grouped place) holds at least COD_ACTIVE_DEBT_LIMIT open
# delivered COD debts AND those debts together exceed COD_DEBT_AMOUNT_THRESHOLD
# of NET open receivable.
#
# The amount arm exists because a tier discount can leave a total of 35 280: a
# customer handing over 35 000 leaves a 280-sum shortfall, and two of those used
# to be enough to take cash off their menu — pushing exactly the customers the
# discount rewards back onto the fiscalized rail.
#
# COD_ACTIVE_DEBT_LIMIT lived as a literal on CashCollectionService until this
# change. It moved here so business_app/utils/cod_cap.py (a pure module) can read
# it without importing a service, which would be a circular import. The class
# attribute is kept as a re-export for the readers that already use it.
COD_ACTIVE_DEBT_LIMIT = _int("COD_ACTIVE_DEBT_LIMIT", 2)
COD_DEBT_AMOUNT_THRESHOLD = _int("COD_DEBT_AMOUNT_THRESHOLD", 10000)  # UZS

# ─── Customer segmentation thresholds (monthly UZS spend) ───────────────
CUSTOMER_SEGMENT_HIGH_VALUE_UZS = _int("CUSTOMER_SEGMENT_HIGH_VALUE_UZS", 100000)
CUSTOMER_SEGMENT_MEDIUM_VALUE_UZS = _int("CUSTOMER_SEGMENT_MEDIUM_VALUE_UZS", 25000)

# ─── Subscriptions ──────────────────────────────────────────────────────
SUBSCRIPTION_FAILED_PAYMENT_MAX_ATTEMPTS = _int("SUBSCRIPTION_FAILED_PAYMENT_MAX_ATTEMPTS", 3)

# ─── Bot token lifecycle (shared by telegram_bot + staff_bot) ───────────
TOKEN_REFRESH_BUFFER_SECONDS = _int("TOKEN_REFRESH_BUFFER_SECONDS", 300)  # refresh this long before expiry
REFRESH_TOKEN_LIFETIME_DAYS = _int("REFRESH_TOKEN_LIFETIME_DAYS", 30)

# ─── Multi-phone customer link suggestions (Phase 1C) ───────────────────
CUSTOMER_LINK_SUGGESTION_RADIUS_KM = _float("CUSTOMER_LINK_SUGGESTION_RADIUS_KM", 0.05)  # 50 m — "same address" proximity
CUSTOMER_LINK_SHARED_GEO_DAMPEN_CUTOFF = _int("CUSTOMER_LINK_SHARED_GEO_DAMPEN_CUTOFF", 4)  # >= this many distinct customers at a point => shared building

# ─── Place-group ("same office") proximity suggestions ──────────────────
# METRES, not km — deliberately different from CUSTOMER_LINK_SUGGESTION_RADIUS_KM
# above, because a mis-parsed 0.01/0.1 is a silent 10x radius error while
# 10 vs 100 metres reads wrong on sight. Converted once, at the single point of
# use, with /1000.0.
#
# This governs the PLACE channel (WHERE: "are these two addresses one office?").
# CUSTOMER_LINK_SUGGESTION_RADIUS_KM governs the LINK channel (WHO: "are these
# two accounts one person?") and is unrelated — do not conflate them.
#
# Suggestions are ADVISORY ONLY: an admin must still confirm every grouping
# (spec 2.1/2.2). Widening this never creates a group by itself.
PLACE_SUGGESTION_RADIUS_M = _float("PLACE_SUGGESTION_RADIUS_M", 10.0)

# ─── Place COD attribution (Plan E) ─────────────────────────────────────
# ON by default (owner ruling A2, 2026-08-04). When enabled:
#   * the staff bot's place screen collects DIRECTLY — no coworker selection —
#     and offers the PLACE's total as the ceiling. The place row posts the
#     orderer of the place's oldest open COD debt as `customer_id` purely so the
#     engine can be REACHED: `post_collection`'s `customer_id: int` is
#     keyword-only with no default (cash_collection_service.py:2510), and
#     `resolve_allocation_scope` refuses PLACE scope unless the posting cluster
#     intersects the place's members (:621). That anchor is a SCOPE INPUT, NOT
#     an attribution (owner ruling A3/A3-bis, 2026-08-04) — surplus follows the
#     collection context: the orderer when cash is taken at a delivery or by
#     personal card transfer, and the debtor the driver already selected for a
#     standalone COD collection (that selection IS the attribution);
#   * a COD debtor's row on the staff list carries their grouped place's whole
#     debt, not only their own orders;
#   * an admin standalone collection forwards `delivery_address_id`, so it
#     reaches the same PLACE scope the staff endpoint already reaches.
# It never widens WHAT counts as a place (a place is an admin-created
# AddressGroup, unchanged) and never touches the allocation engine — only which
# inputs reach it, and which questions a human is asked.
# Set PLACE_COD_COLLECTION_ENABLED=false to roll back to Plan D behaviour
# without a code change; a restart is required (read at import time).
PLACE_COD_COLLECTION_ENABLED = _bool("PLACE_COD_COLLECTION_ENABLED", True)
