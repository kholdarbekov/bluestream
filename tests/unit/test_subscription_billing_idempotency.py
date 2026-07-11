"""Unit tests for CEL-002 — subscription billing idempotency guard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from business_app.services.subscription_service import SubscriptionService


def _sub(last_billing_date, tz_aware=True):
    """Build a stub that looks enough like a Subscription for the guard."""
    if last_billing_date is not None and not tz_aware and last_billing_date.tzinfo is not None:
        last_billing_date = last_billing_date.replace(tzinfo=None)
    return SimpleNamespace(
        id=1,
        last_billing_date=last_billing_date,
        next_billing_date=None,
    )


# _already_billed_this_cycle became an instance method in task 5 (it now
# also queries the orders table for a crash-proof check — see
# SubscriptionService._already_billed_this_cycle). Calls below go through an
# instance rather than the class. The "never billed" / "billed yesterday"
# cases now fall through to a real `Order` query, so those two need `db`
# (table creation) and an app context; the "billed today" cases still
# short-circuit on `last_billing_date` alone and don't touch the database.


def test_guard_false_when_never_billed(app, db):
    subscription = _sub(last_billing_date=None)
    assert SubscriptionService()._already_billed_this_cycle(subscription) is False


def test_guard_false_when_last_bill_was_yesterday(app, db):
    yesterday = datetime.now(timezone.utc) - timedelta(days=1, hours=1)
    assert SubscriptionService()._already_billed_this_cycle(_sub(yesterday)) is False


def test_guard_true_when_last_bill_was_today():
    today_now = datetime.now(timezone.utc)
    assert SubscriptionService()._already_billed_this_cycle(_sub(today_now)) is True


def test_guard_true_when_last_bill_was_today_midnight_exact():
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    assert SubscriptionService()._already_billed_this_cycle(_sub(today_start)) is True


def test_guard_handles_naive_datetime_as_utc():
    # Defensive: legacy rows may have tz-naive timestamps.
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    assert SubscriptionService()._already_billed_this_cycle(_sub(now_naive, tz_aware=False)) is True


def test_guard_returns_false_on_bad_input_type(app, db):
    bad = _sub("not-a-datetime")
    # Must not raise; a non-datetime last_billing_date is treated as
    # unusable and the guard falls through to the order-existence check
    # instead (see the narrow try/except in _already_billed_this_cycle).
    # No Order exists for this subscription, so the fallthrough is False.
    assert SubscriptionService()._already_billed_this_cycle(bad) is False
