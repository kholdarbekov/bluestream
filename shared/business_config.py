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

# ─── COD custody thresholds ─────────────────────────────────────────────
COD_CASH_WARNING_THRESHOLD_UZS = _int("COD_CASH_WARNING_THRESHOLD_UZS", 200000)
COD_CASH_ESCALATION_THRESHOLD_UZS = _int("COD_CASH_ESCALATION_THRESHOLD_UZS", 400000)

# ─── Customer segmentation thresholds (monthly UZS spend) ───────────────
CUSTOMER_SEGMENT_HIGH_VALUE_UZS = _int("CUSTOMER_SEGMENT_HIGH_VALUE_UZS", 100000)
CUSTOMER_SEGMENT_MEDIUM_VALUE_UZS = _int("CUSTOMER_SEGMENT_MEDIUM_VALUE_UZS", 25000)

# ─── Subscriptions ──────────────────────────────────────────────────────
SUBSCRIPTION_FAILED_PAYMENT_MAX_ATTEMPTS = _int("SUBSCRIPTION_FAILED_PAYMENT_MAX_ATTEMPTS", 3)

# ─── Bot token lifecycle (shared by telegram_bot + staff_bot) ───────────
TOKEN_REFRESH_BUFFER_SECONDS = _int("TOKEN_REFRESH_BUFFER_SECONDS", 300)  # refresh this long before expiry
REFRESH_TOKEN_LIFETIME_DAYS = _int("REFRESH_TOKEN_LIFETIME_DAYS", 30)
