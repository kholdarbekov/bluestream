from datetime import datetime, timezone

from types import SimpleNamespace

from business_app.utils.order_timing import delivered_at_utc


def test_prefers_actual_delivery_time_and_makes_it_utc():
    naive = datetime(2026, 6, 23, 10, 0, 0)  # tz-naive
    order = SimpleNamespace(delivery=SimpleNamespace(actual_delivery_time=naive), paid_at=None)
    result = delivered_at_utc(order)
    assert result == naive.replace(tzinfo=timezone.utc)
    assert result.tzinfo is not None


def test_falls_back_to_paid_at_when_no_delivery_time():
    paid = datetime(2026, 6, 23, 9, 0, 0, tzinfo=timezone.utc)
    order = SimpleNamespace(delivery=None, paid_at=paid)
    assert delivered_at_utc(order) == paid


def test_returns_none_when_no_timestamps():
    order = SimpleNamespace(delivery=None, paid_at=None)
    assert delivered_at_utc(order) is None
