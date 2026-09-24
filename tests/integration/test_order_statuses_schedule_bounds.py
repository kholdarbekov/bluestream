"""`GET /orders/statuses` publishes the schedule picker's bounds (spec §5).

Driven over HTTP twice. First, the bounds are read the way the Create Order modal
reads them. Then a real write path, `PATCH /admin/orders/<id>/schedule`, which
validates through the same `parse_and_validate_schedule`, proves the published
range is the accepted range, day for day. The JS `SCHEDULE_HORIZON_DAYS` copy and
the browser clock allowed a picker that offers a day the write path refuses, or
hides one it accepts.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from shared.business_config import MAX_SCHEDULE_HORIZON_DAYS

pytestmark = pytest.mark.integration

TZ = ZoneInfo("Asia/Tashkent")
# 00:30 in Tashkent is still 19:30 on the 23rd in UTC, so a UTC-dated "today"
# would be a day behind.
FROZEN_LOCAL = datetime(2026, 9, 24, 0, 30, tzinfo=TZ)


@pytest.fixture
def frozen_business_clock(monkeypatch):
    # `local_windows` binds its own copy of `local_now`, so freeze both (spec §7).
    monkeypatch.setattr("business_app.utils.delivery_window.local_now", lambda: FROZEN_LOCAL)
    monkeypatch.setattr("business_app.utils.local_windows.local_now", lambda: FROZEN_LOCAL)
    return FROZEN_LOCAL


def _published_bounds(client):
    response = client.get("/api/v1/orders/statuses")
    assert response.status_code == 200, response.get_data(as_text=True)
    data = response.get_json()["data"]
    return data["schedule_min_date"], data["schedule_max_date"]


def test_the_bounds_are_tashkent_local_today_through_the_horizon(client, db, frozen_business_clock):
    first, last = _published_bounds(client)

    assert first == "2026-09-24"
    assert last == (date(2026, 9, 24) + timedelta(days=MAX_SCHEDULE_HORIZON_DAYS)).isoformat()

    data = client.get("/api/v1/orders/statuses").get_json()["data"]
    assert set(data) == {"statuses", "transitions", "schedule_min_date", "schedule_max_date"}


@pytest.mark.parametrize(
    "day, accepted",
    [("before_first", False), ("first", True), ("last", True), ("after_last", False)],
)
def test_the_write_path_accepts_exactly_the_published_range(
    client, db, admin_claim_headers, sample_order, frozen_business_clock, day, accepted
):
    first, last = (date.fromisoformat(value) for value in _published_bounds(client))
    requested = {
        "before_first": first - timedelta(days=1),
        "first": first,
        "last": last,
        "after_last": last + timedelta(days=1),
    }[day]

    response = client.patch(
        f"/api/v1/admin/orders/{sample_order.id}/schedule",
        json={"delivery_date": requested.isoformat()},
        headers=admin_claim_headers,
    )

    db.session.refresh(sample_order)
    if accepted:
        assert response.status_code == 200, response.get_data(as_text=True)
        assert sample_order.delivery_date == requested
    else:
        assert response.status_code == 400, response.get_data(as_text=True)
        assert sample_order.delivery_date is None
