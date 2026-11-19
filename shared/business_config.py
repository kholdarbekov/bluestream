"""
Business configuration — the single source of truth for env-driven business,
money, and auth tunables shared across every service (backend, Celery, the
customer telegram_bot, and the staff_bot).

This is a PURE module: it imports only ``os`` and ``logging`` and holds no Flask / app-context
state, so it is safe to import from ``business_app`` (including at config-class
definition time), Celery workers, and both bots — exactly like
``shared/constants.py::DISPLAY_TIMEZONE``.

Single-default rule: each value's default literal lives HERE exactly once.
``business_app/config/base.py`` derives its Flask config keys from this module;
do NOT re-declare the literal there, and do NOT add per-call-site ``.get(KEY,
literal)`` fallbacks elsewhere — read ``current_app.config["KEY"]`` (backend) or
import the name from here (bots).
"""

import logging
import os

_logger = logging.getLogger(__name__)


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


def _lenient(name: str, default, convert):
    """Read an env var through `convert`, falling back to `default` WITHOUT raising.

    For knobs that a bot imports but never needs in order to start: an unparsable value is
    logged as an error naming the variable and the value, and the default is used. Range checks
    belong to the consumer (see the reorder-reminder section at the end of this module).
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return convert(raw.strip())
    except (TypeError, ValueError):
        _logger.error("%s=%r is not a valid value; using the default %r", name, raw, default)
        return default


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

# ─── Delivery ETA: the Google Routes budget ─────────────────────────────
# The most Google Routes `computeRoutes` calls one Tashkent day may make (owner rule 2026-11-03: Google serves the
# customer's "driver is on the way" estimate and nothing else). Google's free allowance is 5,000 calls a month;
# 150 x 31 days = 4,650 stays inside it, so the bill stays at zero. Once the day's count is over this number,
# `google_routes.get_traffic_aware_leg` sends no request and returns None: that tap gets no minutes, and the customer
# sees "On the way" (our own engine's time is still recorded, in the hidden shadow row only). 0 switches Google off.
# Counted in Redis per Tashkent day (RedisKeyspace.google_routes_calls).
GOOGLE_ROUTES_DAILY_CAP = _int("GOOGLE_ROUTES_DAILY_CAP", 150)

# ─── Delivery: automatic arrival ────────────────────────────────────────
# A driver's shared location within this many metres of an in-transit stop marks it arrived (owner decision 2026-11-17).
AUTO_ARRIVAL_RADIUS_METERS = _int("AUTO_ARRIVAL_RADIUS_METERS", 100)
# Readings whose reported GPS accuracy is worse than this many metres (or unreported) never mark a stop arrived.
AUTO_ARRIVAL_MAX_ACCURACY_METERS = _int("AUTO_ARRIVAL_MAX_ACCURACY_METERS", 100)

# ─── Delivery ETA: the "on the way" promise ─────────────────────────────
# Spec 2026-11-03 §4.8 S2. Read only by DeliveryEtaService. When the driver taps `in_transit` to a stop, the
# customer is promised "by tap + ETA_OFF_ROAD_MINUTES + headroom x G", G being Google's live-traffic drive time
# from the driver's position.
# Google's live traffic is asked only from a driver position at most this old at the tap. An older one is not where
# the driver is: no call is spent, and the time comes from our own engine instead (ETA_STALE_*_ALLOWANCE_MINUTES), up
# to DRIVER_LOCATION_FRESH_SECONDS (30 minutes) old. Older still, or none, and the customer is told no time.
ETA_POSITION_MAX_AGE_SECONDS = _int("ETA_POSITION_MAX_AGE_SECONDS", 600)
# The minutes off the road at the door end: parking, the walk, the stairs.
ETA_OFF_ROAD_MINUTES = _int("ETA_OFF_ROAD_MINUTES", 5)
# Headroom, the factor on G. The seed stands until ETA_HEADROOM_MIN_LEGS legs are recorded. From then on it is
# learned from the last four weeks of legs: the 90th percentile of (actual drive - off-road minutes) / G, blended
# with the seed as if the seed were ETA_HEADROOM_PRIOR_LEGS more legs, so a few odd days cannot swing it. Never
# below 1.0.
ETA_HEADROOM_SEED = _float("ETA_HEADROOM_SEED", 1.3)
ETA_HEADROOM_MIN_LEGS = _int("ETA_HEADROOM_MIN_LEGS", 30)
ETA_HEADROOM_PRIOR_LEGS = _int("ETA_HEADROOM_PRIOR_LEGS", 20)
# From a position older than ETA_POSITION_MAX_AGE_SECONDS at the tap: "by tap + our own engine's minutes from it + an
# allowance". Such a position is wrong by an amount of time, not by a share of the route, so it takes an allowance and
# not a headroom. Backtested on 274 real stale legs (2026-05 to 2026-11): about 87% of these promises are kept.
# Only a position at most DRIVER_LOCATION_FRESH_SECONDS old is priced so (owner rule 2026-11-09). The last position
# is a door or a pin:
ETA_STALE_DOOR_ALLOWANCE_MINUTES = _int("ETA_STALE_DOOR_ALLOWANCE_MINUTES", 30)
# The last position is the depot's stamp of a load or a return. Less, as drivers mostly tap after they have left:
ETA_STALE_DEPOT_ALLOWANCE_MINUTES = _int("ETA_STALE_DEPOT_ALLOWANCE_MINUTES", 20)

# ─── Delivery ETA: the capacity-aware plan ──────────────────────────────
# Spec 2026-11-03 §4 and the Phase 2 plan of 2026-11-04 (rulings R1-R19): the engine that plans every waiting order onto
# the day's cars and records the window it expects, hidden from customers. Read by `eta_plan`, `eta_recompute_dispatch`,
# `delivery_eta_tasks` and the accuracy report. Plan days run 05:00 to 05:00 local (spec §6,
# `local_windows.work_day_of`) and are not a tunable. Not defined here, because the route optimiser already owns them:
# COMMITTED_STOP_MAX_AGE_HOURS and DRIVER_LOCATION_FRESH_SECONDS.
#
# Which cars work today (R1). An OPEN session counts only if it was active within this many hours: the latest of its
# start, its orders' real delivered taps, and its transfers' sent, confirmed or resolved times.
ETA_SESSION_ACTIVITY_HOURS = _int("ETA_SESSION_ACTIVITY_HOURS", 24)

# A driver's typical start and end (R3), in minutes past the plan day's local midnight. They come from the driver's last
# ETA_HISTORY_WORK_DAYS plan days with delivered taps, looked for within ETA_HISTORY_LOOKBACK_DAYS: the median start,
# and the p75 end capped at that day's roster end.
ETA_HISTORY_WORK_DAYS = _int("ETA_HISTORY_WORK_DAYS", 14)
ETA_HISTORY_LOOKBACK_DAYS = _int("ETA_HISTORY_LOOKBACK_DAYS", 60)
# With fewer such days than this, the fleet's typical start and end over all drivers' days stand in.
ETA_MIN_DRIVER_DAYS = _int("ETA_MIN_DRIVER_DAYS", 5)
# With no history at all (spec §2): the fleet's start seed, 700 = 11:40...
ETA_FLEET_START_SEED_MINUTES = _int("ETA_FLEET_START_SEED_MINUTES", 700)
# ...and its end seed, 1200 = 20:00.
ETA_FLEET_END_SEED_MINUTES = _int("ETA_FLEET_END_SEED_MINUTES", 1200)
# A car with no load or pickup by its typical start plus this many minutes has not set off when it should have...
ETA_START_GRACE_MINUTES = _int("ETA_START_GRACE_MINUTES", 30)
# ...so its start is rolled forward to now plus this many minutes.
ETA_START_ROLL_MINUTES = _int("ETA_START_ROLL_MINUTES", 15)

# Minutes a car spends loading at the depot: after a load it leaves this long later, to its first stop (R4). Nothing is
# added after the typical first pickup, which is already measured after loading.
ETA_LOADING_ALLOWANCE_MINUTES = _int("ETA_LOADING_ALLOWANCE_MINUTES", 10)
# A full load (R6) is the driver's median bottles_loaded over their last this-many closed sessions that had a load, else
# the fleet's median, else ETA_RELOAD_FULL_BOTTLES_DEFAULT. A car reloads at most once a day, back to full.
ETA_FULL_LOAD_SESSIONS = _int("ETA_FULL_LOAD_SESSIONS", 10)
ETA_RELOAD_FULL_BOTTLES_DEFAULT = _int("ETA_RELOAD_FULL_BOTTLES_DEFAULT", 30)
# A stop without coordinates (R7) is placed this many straight-line km after the previous point in its car's sequence.
# A float: read through `_int`, `ETA_UNKNOWN_LEG_KM=2.5` would raise at import.
ETA_UNKNOWN_LEG_KM = _float("ETA_UNKNOWN_LEG_KM", 2.3)

# A stop no car can take within this many plan days is "unconfirmed" (R10, spec §4.7): it has no time and no window. A
# stop the customer dated beyond today keeps its date.
ETA_UNCONFIRMED_HORIZON_DAYS = _int("ETA_UNCONFIRMED_HORIZON_DAYS", 3)
# Extra minutes on hi of a window whose car has not started (spec §4.6), when lo and hi come from the seeds. A fitted
# not-started bucket already holds that spread.
ETA_NOT_STARTED_EXTRA_HI_MINUTES = _int("ETA_NOT_STARTED_EXTRA_HI_MINUTES", 60)
# An engine row is recorded (R12) when the window end has moved by this many minutes or more, and on any change of
# stage, of promised day, or of whether there is a window at all.
ETA_RECORD_MOVE_MINUTES = _int("ETA_RECORD_MOVE_MINUTES", 15)

# The trigger (R15). A commit that touches the plan's inputs requests a recompute: the "already queued" marker
# (RedisKeyspace.eta_recompute_scheduled) lives this many seconds, so a burst of commits queues one run, and a queued
# run expires this long after its countdown.
ETA_RECOMPUTE_SCHEDULED_TTL_SECONDS = _int("ETA_RECOMPUTE_SCHEDULED_TTL_SECONDS", 30)
# The delay between a commit that requests a recompute and its run, so the commits of one action land first.
ETA_RECOMPUTE_COUNTDOWN_SECONDS = _int("ETA_RECOMPUTE_COUNTDOWN_SECONDS", 5)
# How long a recompute holds its run lock (RedisKeyspace.eta_recompute_lock). The recompute task's time limit is this
# same number, so a run that dies frees the lock by itself.
ETA_RECOMPUTE_LOCK_SECONDS = _int("ETA_RECOMPUTE_LOCK_SECONDS", 120)
# The kill switch of the whole shadow engine: with it off, the commit detector requests nothing, the beat run does
# nothing and the weekly fit does not run. Off in the tests' config.
ETA_SHADOW_ENGINE_ENABLED = _bool("ETA_SHADOW_ENGINE_ENABLED", True)

# When a moved promise is worth a message to the customer (spec §4.9). Phase 2 only counts such moves, in shadow.
# A message when the window end moves later by this many minutes or more...
ETA_CHANGE_LATER_MINUTES = _int("ETA_CHANGE_LATER_MINUTES", 30)
# ...or earlier by this many (good news).
ETA_CHANGE_EARLIER_MINUTES = _int("ETA_CHANGE_EARLIER_MINUTES", 60)
# At most one message per this many minutes...
ETA_CHANGE_MIN_GAP_MINUTES = _int("ETA_CHANGE_MIN_GAP_MINUTES", 60)
# ...and this many per order.
ETA_CHANGE_MAX_PER_ORDER = _int("ETA_CHANGE_MAX_PER_ORDER", 3)
# None in the quiet hours, in minutes past local midnight (1320 = 22:00 to 480 = 08:00): a pending change goes out when
# they end, if it still holds.
ETA_QUIET_START_MINUTES = _int("ETA_QUIET_START_MINUTES", 1320)
ETA_QUIET_END_MINUTES = _int("ETA_QUIET_END_MINUTES", 480)

# The weekly fit (spec §4.10, R5, R13). It reads the last this-many plan days...
ETA_FIT_WINDOW_DAYS = _int("ETA_FIT_WINDOW_DAYS", 28)
# ...keeps the previous point of a stop-curve bin that has fewer legs than this...
ETA_FIT_MIN_LEGS_PER_BIN = _int("ETA_FIT_MIN_LEGS_PER_BIN", 30)
# ...and keeps the previous depot line when fewer (first pickup, first delivery) pairs than this fit it.
ETA_FIT_MIN_DEPOT_PAIRS = _int("ETA_FIT_MIN_DEPOT_PAIRS", 15)
# A leg of 1 km or more faster than this many km/h straight-line is an unreliable tap and is left out; a fitted curve
# whose last segment is faster than this is rejected, since that slope prices every leg beyond the last point.
ETA_FIT_MAX_KMH = _int("ETA_FIT_MAX_KMH", 60)
# A stop whose pickup and delivered taps are under this many minutes apart is an unreliable tap and is left out.
ETA_FIT_MIN_PICKUP_TO_DELIVERED_MINUTES = _int("ETA_FIT_MIN_PICKUP_TO_DELIVERED_MINUTES", 2)

# The accuracy report. The gate (spec §5) wants at least this share of delivered orders to arrive by the end of the
# first window the engine recorded for them. A float: it is a share, not a count.
ETA_GATE_KEPT_SHARE = _float("ETA_GATE_KEPT_SHARE", 0.90)
# The report's range when none is asked for, in local days ending today (the gate is judged over a rolling two weeks).
ETA_REPORT_DEFAULT_DAYS = _int("ETA_REPORT_DEFAULT_DAYS", 14)

# Windows (spec §4.6, R11). A (stops ahead, started) bucket with fewer fitted promises than this borrows the next band
# up.
ETA_WINDOW_MIN_PROMISES = _int("ETA_WINDOW_MIN_PROMISES", 30)
# A window is shown rounded outward to this many minutes (the half hour).
ETA_WINDOW_ROUND_MINUTES = _int("ETA_WINDOW_ROUND_MINUTES", 30)
# A window wider than this many hours is shown as "by HH:MM", its end only.
ETA_WINDOW_BY_ONLY_HOURS = _int("ETA_WINDOW_BY_ONLY_HOURS", 4)

# The structured seeds. Plain constants, not env-read: a weekly fit moves them, not an operator.
# The stop curve (R5, spec §4.5): minutes from one stop to the next, piecewise linear in the straight-line km between
# them, through these (km, minutes) points (the production medians of spec §2). Below the first point it is that point's
# value; beyond the last it continues the last segment's slope.
ETA_CYCLE_SEED_POINTS = ((0.1, 13.0), (0.8, 16.0), (2.3, 24.0), (4.6, 32.0), (7.7, 44.0), (13.2, 67.0))
# The fit bins by straight-line km, [0, 0.2) [0.2, 1) [1, 3) [3, 6) [6, 10) [10, ∞): one seed point sits in each, and a
# bin's fitted (median km, median minutes) replaces it.
ETA_CYCLE_BIN_EDGES_KM = (0.2, 1.0, 3.0, 6.0, 10.0)
# The depot line (R4), (a, b): every leg that leaves the depot costs a + b x km minutes, never the stop curve, which was
# fitted to legs of 13.2 km or less and says about 133 minutes at the depot's 29 km from the centre.
ETA_DEPOT_LEG_SEED = (10.0, 1.5)
# The window seeds (spec §4.6): (lo, hi) minutes around the expected arrival, by the stops ahead of the stop in its
# car's plan, before a fit has enough promises of its own.
ETA_WINDOW_SEEDS = {"0": (-20, 45), "1-2": (-40, 90), "3-4": (-60, 120), "5-7": (-75, 150), "8+": (-90, 180)}

# ─── Loyalty ────────────────────────────────────────────────────────────
# Earning rate / bonus amounts / tier thresholds / expiry are all DB-driven
# (LoyaltyProgram + LoyaltyTierConfig). LOYALTY_POINTS_RATIO is only the
# bootstrap default for a new default program. Points are redeemed ONLY via
# rewards (LoyaltyReward.points_cost) — no direct points→UZS conversion.
LOYALTY_POINTS_RATIO = _int("LOYALTY_POINTS_RATIO", 250)  # UZS per earned point (bootstrap default)


# ─── Sales-agent pay ────────────────────────────────────────────────────
# The largest sum one pay input may carry (an adjustment either way, a base salary, a penalty, a
# penalty type's default, a plan's bonus amount or threshold). `pay_rules.check_amount_bound`
# refuses anything above it with SALES_PAY_AMOUNT_INVALID before rounding: past it a typo
# overflowed `Numeric(14,2)` (a 500), and one huge adjustment made the month's close 500 for
# every agent at once.
MAX_PAY_AMOUNT = _int("MAX_PAY_AMOUNT", 10_000_000_000)  # UZS per input


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
# Days between creating a subscription order and delivering it (spec 2026-10-23 §4.2, §6.1): each
# order is created the day before its delivery, so a new subscription's first delivery, and the
# first one after a resume, is never sooner than tomorrow. Read only by subscription_schedule.
SUBSCRIPTION_ORDER_LEAD_DAYS = _int("SUBSCRIPTION_ORDER_LEAD_DAYS", 1)
# Hours an unpaid Click order has before `cancel_abandoned_orders` cancels it (spec 2026-10-23 §4.5). A
# subscription order also waits for the end of its delivery window, never less than this. Read only by
# OrderService.payment_deadline and the sweep's candidate query.
CLICK_PAYMENT_WINDOW_HOURS = _int("CLICK_PAYMENT_WINDOW_HOURS", 24)

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

# ─── Shared-place detection (first-order customer hands back empties) ───
# Driver-candidate channel: how near another customer's pin must be to be offered
# to the driver as "the place this customer's empties came from", and how long the
# driver has to answer before the admin is alerted. Unrelated to the 10 m place
# suggestion radius above and the 50 m link-suggestion radius.
SHARED_PLACE_CANDIDATE_RADIUS_M = _float("SHARED_PLACE_CANDIDATE_RADIUS_M", 50.0)
SHARED_PLACE_DRIVER_ANSWER_WINDOW_S = _int("SHARED_PLACE_DRIVER_ANSWER_WINDOW_S", 900)
# The longest note a driver may leave on a case (characters, after stripping). The staff
# bot refuses a longer one before sending it and the backend refuses it again, so both
# read this one value. Not env-driven: the two processes must never disagree.
SHARED_PLACE_DRIVER_NOTE_MAX_CHARS = 500

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
# exceed this multiple of that outlet's usual interval: the median of the whole
# local-day gaps between its newest delivery days, floored at 7
# (business_app/utils/delivery_cadence.py::typical_interval_days, D23).
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

# ─── Sales agents (pay: the earnings ledger sync, the same-day approval hold) ───
# The ledger sync re-reads an agent order created within this many days, whatever changed it:
# a late payment or a cash correction months later still moves the commission. It is also the
# horizon of the agent's "on the way" pipeline.
SALES_PAY_SYNC_LOOKBACK_DAYS = _int("SALES_PAY_SYNC_LOOKBACK_DAYS", 120)

# ...plus any agent order whose row changed within this many days, however old it is.
SALES_PAY_SYNC_TOUCH_DAYS = _int("SALES_PAY_SYNC_TOUCH_DAYS", 7)

# An estimate syncs that agent's ledger first, at most once per this many seconds. A bot user
# tapping "My earnings" twice gets the same figure without a second sync.
SALES_PAY_ONDEMAND_SYNC_SECONDS = _int("SALES_PAY_ONDEMAND_SYNC_SECONDS", 120)

# How long a held agent order's stock stays reserved while it waits for a manager. It is not a
# decision deadline: a manager may still approve it the next morning.
SALES_STAFF_APPROVAL_HOLD_HOURS = _int("SALES_STAFF_APPROVAL_HOLD_HOURS", 12)


# ─── Lapsed-customer reorder reminders (spec 2026-10-09 §11) ────────────
# Read through `_lenient`, not `_int`/`_float`: both bots import this module, so a typo in a
# reminder knob must cost a logged error and the default, never a bot that will not start
# (review R18). The RANGE rules — the mode vocabulary (an unknown mode reads as off), 1..2
# reminders per lapse, a 09:00–15:59 run time — are applied once, by
# business_app/services/reorder_reminders/config.py::ReminderConfig.from_mapping, which falls
# back to THIS dict, so each default literal still lives here exactly once.
REORDER_REMINDER_DEFAULTS = {
    # off | dry_run | on. Ships off: dry_run first, then on after copy review and counsel (§16).
    "REORDER_REMINDERS_MODE": "off",
    # The threshold itself when history is too short: "no order for more than 30 days" (D1).
    "REORDER_REMINDER_FALLBACK_DAYS": 30,
    # Lapsed once the days since the supply delivery exceed this multiple of the usual gap.
    "REORDER_REMINDER_RATIO": 1.5,
    # Delivery days needed before the customer's own rhythm replaces the preset.
    "REORDER_REMINDER_MIN_DELIVERIES": 3,
    # Reminders per lapse. The copy (R1, silent R2) and the Later button assume at most 2.
    "REORDER_REMINDER_MAX_PER_EPISODE": 2,
    # Days between reminder 1 and reminder 2.
    "REORDER_REMINDER_GAP_DAYS": 7,
    # How far "Remind me later" on reminder 1 pushes reminder 2 (D13).
    "REORDER_REMINDER_SNOOZE_DAYS": 14,
    # Days after the last reminder before staff are asked to follow up.
    "REORDER_REMINDER_ESCALATE_AFTER_DAYS": 7,
    # A lapse whose due day is older than this gets no message, only a worklist row (D11).
    "REORDER_REMINDER_FRESH_DAYS": 14,
    # A live order younger than this, or dated today or later, is on its way; else stuck (D12).
    "REORDER_REMINDER_LIVE_ORDER_GRACE_DAYS": 7,
    # The daily run, local "HH:MM", valid 09:00–15:59 so sends and retries end before 18:00.
    "REORDER_REMINDER_LOCAL_TIME": "10:00",
    # Where the admin UI lives; the escalation digest links to its Lapsed customers page.
    "ADMIN_UI_BASE_URL": "https://admin.aqua-element.uz",
}


def _reminder_knob(name: str, convert=str):
    return _lenient(name, REORDER_REMINDER_DEFAULTS[name], convert)


REORDER_REMINDERS_MODE = _reminder_knob("REORDER_REMINDERS_MODE")
REORDER_REMINDER_FALLBACK_DAYS = _reminder_knob("REORDER_REMINDER_FALLBACK_DAYS", int)
REORDER_REMINDER_RATIO = _reminder_knob("REORDER_REMINDER_RATIO", float)
REORDER_REMINDER_MIN_DELIVERIES = _reminder_knob("REORDER_REMINDER_MIN_DELIVERIES", int)
REORDER_REMINDER_MAX_PER_EPISODE = _reminder_knob("REORDER_REMINDER_MAX_PER_EPISODE", int)
REORDER_REMINDER_GAP_DAYS = _reminder_knob("REORDER_REMINDER_GAP_DAYS", int)
REORDER_REMINDER_SNOOZE_DAYS = _reminder_knob("REORDER_REMINDER_SNOOZE_DAYS", int)
REORDER_REMINDER_ESCALATE_AFTER_DAYS = _reminder_knob("REORDER_REMINDER_ESCALATE_AFTER_DAYS", int)
REORDER_REMINDER_FRESH_DAYS = _reminder_knob("REORDER_REMINDER_FRESH_DAYS", int)
REORDER_REMINDER_LIVE_ORDER_GRACE_DAYS = _reminder_knob("REORDER_REMINDER_LIVE_ORDER_GRACE_DAYS", int)
REORDER_REMINDER_LOCAL_TIME = _reminder_knob("REORDER_REMINDER_LOCAL_TIME")
ADMIN_UI_BASE_URL = _reminder_knob("ADMIN_UI_BASE_URL")


# --- Driver cash expenses (spec 2026-11-17) ---------------------------------------------------
# The daily digest lists claims that have waited longer than this.
DRIVER_EXPENSE_DIGEST_AFTER_HOURS = 24
# "My expenses" shows this many decided claims.
DRIVER_EXPENSE_DECIDED_SHOWN = 10
