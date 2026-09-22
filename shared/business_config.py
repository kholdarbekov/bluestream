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


def _str(name: str, default: str) -> str:
    """Read a string env var, treating unset/blank as the default.

    Surrounding whitespace is stripped for the same reason the boolean reader is
    strict: a value typed into a compose `.env` file carries whatever spacing it
    was typed with, and `"08:30 "` is not a different setting from `"08:30"` —
    it is the same setting that no longer parses.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


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

# ─── Sales agents (phase 1) ─────────────────────────────────────────────
# Two outlets with similar names closer than this are flagged as duplicates
# at field onboarding (spec 2026-09-07-sales-agent-role-design.md, D14).
SALES_DEDUPE_RADIUS_M = _int("SALES_DEDUPE_RADIUS_M", 150)

# ─── Sales agents (phase 2a — visit loop) ───────────────────────────────
# Visit check-in geofence. The pin the agent shares is compared against the
# outlet pin and `in_radius` is published as a FIELD (D15) — neither bot nor
# admin UI re-derives it. An out-of-radius check-in is RECORDED, never refused:
# this radius sizes an honesty signal, not a gate.
SALES_GEOFENCE_RADIUS_M = _int("SALES_GEOFENCE_RADIUS_M", 250)

# Suggested quantity (D15):
#   cover_days    = cadence_days(outlet) + SALES_DELIVERY_LEAD_DAYS
#   suggested_qty = max(0, ceil(rate_per_day × cover_days × SAFETY_FACTOR − on_hand))
# The factor is the margin that keeps a store from running dry between visits.
# It is a FLOAT deliberately, and the damage would arrive with the first deployed
# OVERRIDE rather than with the default: `_int` returns an unset default untouched,
# so the shipped 1.5 would survive, but `SALES_SUGGEST_SAFETY_FACTOR=1.5` in the
# environment would then raise `ValueError: invalid literal for int()` at import and
# only whole numbers would load — a `1` cuts every suggestion a third of its cover.
SALES_SUGGEST_SAFETY_FACTOR = _float("SALES_SUGGEST_SAFETY_FACTOR", 1.5)
# Days between placing an agent order and the goods arriving — added to the
# cadence above, and subtracted from a predicted stock-out when the backend
# computes `next_visit_due_at` (D7), so the visit lands before the shelf empties.
SALES_DELIVERY_LEAD_DAYS = _int("SALES_DELIVERY_LEAD_DAYS", 1)

# Default visit cadence per outlet class. `Outlet.outlet_class` (DB column
# "class", business_app/models/sales.py:97) selects A/B/C; a NULL class reads C,
# and `Outlet.cadence_days_override` (:98) beats all three.
SALES_CADENCE_DAYS_A = _int("SALES_CADENCE_DAYS_A", 7)
SALES_CADENCE_DAYS_B = _int("SALES_CADENCE_DAYS_B", 14)
SALES_CADENCE_DAYS_C = _int("SALES_CADENCE_DAYS_C", 30)

# A visit the agent never closed (phone died, walked out of the store) is swept
# to `abandoned` after this many hours. One agent may hold only one open visit,
# so without the sweep a single dead phone strands that agent out of the flow.
SALES_VISIT_AUTO_ABANDON_HOURS = _int("SALES_VISIT_AUTO_ABANDON_HOURS", 6)

# How long the store owner has to Confirm / Decline an order the agent placed on
# their behalf in the customer bot. On expiry the order is CONFIRMED anyway
# ("delivery confirms", D5) — this TTL bounds the wait, it never cancels.
SALES_CONFIRMATION_TTL_HOURS = _int("SALES_CONFIRMATION_TTL_HOURS", 3)

# Upper bound for a single on-hand / empties figure typed at a stock check. A
# fat-fingered 5000 must come back as a validation error, not as a consumption
# rate that books the whole depot on the next visit.
SALES_STOCK_QTY_MAX = _int("SALES_STOCK_QTY_MAX", 500)

# Fallback consumption window. With fewer than two stock checks for an
# outlet × product, `rate_per_day` is the delivered quantity over this many days
# DIVIDED BY the same number (spec, `rate_per_day` source 2). Named once so the
# window and its divisor can never drift apart.
#
# ONE tunable, two consumers (backlog L-ruling R10): the same number also bounds
# how far back `ReplenishmentService` looks for delivered history when it sizes
# the in-transit horizon. Splitting it into two knobs would let an operator widen
# the rate window and leave the horizon behind it, which reads on the agent's
# screen as a suggestion that ignores a delivery the shop has already had.
SALES_RATE_HISTORY_DAYS = _int("SALES_RATE_HISTORY_DAYS", 60)

# ─── Sales agents (phase 2b — nightly jobs, digest, nearby) ─────────────
# `active → at_risk` when the days since the outlet's last DELIVERED order
# exceed this multiple of that outlet's OWN median inter-order interval (D23).
# A float deliberately, and the damage lands on the first deployed override:
# read through `_int`, `SALES_AT_RISK_RATIO=1.5` raises `ValueError` at import,
# so only whole multiples would ever load and a `1` would flag every outlet
# at-risk the day after its normal interval passed.
SALES_AT_RISK_RATIO = _float("SALES_AT_RISK_RATIO", 1.5)

# How long an outlet may stay `at_risk` before the job stops planning visits to
# it and calls it `dormant`. Measured from the AT-RISK TRANSITION, not from the
# last delivered order (Task 2): with the default class C (30 d) the at-risk
# threshold is 1.5 × 30 = 45, so an absolute "days since delivery" clock would
# give every class-NULL outlet — which is every outlet phase 1 imported — a
# one-night at-risk window, i.e. an alert nobody ever sees.
SALES_DORMANT_DAYS = _int("SALES_DORMANT_DAYS", 45)

# An active-stage outlet nobody has walked into for longer than this is listed
# in its agent's morning digest (top five by days). Not a due date and not a
# stage change: purely the "you have forgotten this shop" line.
SALES_UNVISITED_ALERT_DAYS = _int("SALES_UNVISITED_ALERT_DAYS", 21)

# When the morning digest goes out, as local "HH:MM" in DISPLAY_TIMEZONE.
# Celery beat already runs in that timezone (the nightly-backup entries rely on
# the same fact), so this parses straight into a crontab with no conversion.
# One string rather than two ints because it is ONE setting an operator moves,
# and a pair could be half-overridden.
SALES_DIGEST_LOCAL_TIME = _str("SALES_DIGEST_LOCAL_TIME", "08:30")

# How many rows the agent's *Nearby* list answers with, ordered by distance
# from the pin the agent just shared. There is deliberately no radius tunable:
# a radius would hide the one shop the agent is standing next to whenever the
# pin is coarse indoors, and a list is cheap to scroll.
SALES_NEARBY_LIMIT = _int("SALES_NEARBY_LIMIT", 20)

# ─── Sales agents (phase 3 — KPIs, plan-vs-fact, exceptions) ────────────
# A completed visit whose door-to-door time is under this many seconds is listed
# in the supervisor's exceptions feed. Measured from `checkin_at` (presence), not
# from `started_at` (the Start tap, which can be minutes up the street), so the
# number means "how long the agent was actually in the shop".
SALES_SHORT_VISIT_SECONDS = _int("SALES_SHORT_VISIT_SECONDS", 60)

# The widest date range any KPI / visits / exceptions read will answer, in local
# calendar days inclusive. A ceiling rather than a silent clamp: a mistyped year
# must come back as a clear 400, because a clamped answer is a number an owner
# would read as the whole period and act on.
SALES_METRICS_MAX_RANGE_DAYS = _int("SALES_METRICS_MAX_RANGE_DAYS", 92)
