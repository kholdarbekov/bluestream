"""
Centralised Redis-failure reporting shared by both Telegram bots (RED-005).

Per the audit's RedisUsageTier policy:
  - TIER_SECURITY (rate limits, OTP, replay guards) → fail CLOSED
  - TIER_RELIABILITY (dedup, idempotency) → fail CLOSED
  - TIER_CACHE (token cache, message-id lookups) → fall through, but alert

The rate-limit subsystem already has its own throttled Sentry alerting
(see RateLimiter._report_redis_failure in telegram_bot/utils.py). This module
is the shared path for the remaining TIER_CACHE / TIER_RELIABILITY sites so
every silent fall-through becomes an observable signal.

sentry_sdk is imported lazily inside report_redis_failure so this module
stays importable in environments where sentry isn't installed (tests, etc.).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict

logger = logging.getLogger(__name__)

# Throttle repeated alerts per (component, tier) pair so a Redis outage
# doesn't generate thousands of Sentry events.
_ALERT_COOLDOWN_SECONDS = 300
_last_alert_at: Dict[str, datetime] = {}


def report_redis_failure(component: str, reason: str, tier: str = "cache") -> None:
    """Emit a throttled Sentry alert when a Redis operation fails.

    Args:
        component: dotted identifier, e.g. "token_manager.get_cached_tokens"
        reason: short error description (exception message or cause)
        tier: one of "security", "reliability", "cache" — sets Sentry severity
    """
    now = datetime.now(timezone.utc)
    key = f"{component}:{tier}"
    last = _last_alert_at.get(key)
    if last is not None and (now - last).total_seconds() < _ALERT_COOLDOWN_SECONDS:
        return
    _last_alert_at[key] = now

    # Log at critical so Loki/Sentry log pipeline picks it up regardless of
    # Sentry SDK availability.
    logger.critical(
        "Redis failure in component=%s tier=%s: %s", component, tier, reason
    )

    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("component", component)
            scope.set_tag("subsystem", "bot_redis")
            scope.set_tag("tier", tier)
            # TIER_SECURITY / TIER_RELIABILITY are user-visible security holes.
            # TIER_CACHE just means degraded performance, not a security gap.
            scope.set_level("error" if tier in ("security", "reliability") else "warning")
            sentry_sdk.capture_message(
                f"Bot Redis unavailable (component={component}, tier={tier}): {reason}"
            )
    except ImportError:
        # sentry_sdk not installed (tests, minimal envs) — critical log above suffices.
        pass
    except Exception:  # pragma: no cover — never let reporting mask the real error
        pass
