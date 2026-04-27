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


def test_guard_false_when_never_billed():
    subscription = _sub(last_billing_date=None)
    assert SubscriptionService._already_billed_this_cycle(subscription) is False


def test_guard_false_when_last_bill_was_yesterday():
    yesterday = datetime.now(timezone.utc) - timedelta(days=1, hours=1)
    assert SubscriptionService._already_billed_this_cycle(_sub(yesterday)) is False


def test_guard_true_when_last_bill_was_today():
    today_now = datetime.now(timezone.utc)
    assert SubscriptionService._already_billed_this_cycle(_sub(today_now)) is True


def test_guard_true_when_last_bill_was_today_midnight_exact():
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    assert SubscriptionService._already_billed_this_cycle(_sub(today_start)) is True


def test_guard_handles_naive_datetime_as_utc():
    # Defensive: legacy rows may have tz-naive timestamps.
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    assert SubscriptionService._already_billed_this_cycle(_sub(now_naive, tz_aware=False)) is True


def test_guard_returns_false_on_bad_input_type():
    bad = _sub("not-a-datetime")
    # Must not raise; returning False keeps the billing flow safe.
    assert SubscriptionService._already_billed_this_cycle(bad) is False
