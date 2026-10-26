"""The admin Subscriptions page against the real admin endpoints (plan 2026-10-24 Task 5; spec §2.1, §9).

The page sends ONE `frequency`, the day that frequency uses (cleared for the others), a slot, and for
"Set next delivery date" a calendar DATE behind the `override_manual_billing_dates` switch. The bodies
are the ones `admin_ui/src/utils/subscriptionPayload.js` builds (its Vitest pins the same keys). The list
filter is `frequency`, and every row carries the schedule fields the page shows.
"""

from datetime import date, timedelta

import pytest

from business_app.models.delivery import DeliveryTimeSlot
from business_app.models.subscription import Subscription
from business_app.models.user import UserAddress
from shared.enums import SubscriptionFrequency

pytestmark = pytest.mark.integration


@pytest.fixture
def address(db, sample_user):
    addr = UserAddress(
        user_id=sample_user.id, title="Home", full_address="Amir Temur 1, Tashkent",
        street_address="Amir Temur 1", city="Tashkent", latitude=41.311, longitude=69.279,
    )
    db.session.add(addr)
    db.session.commit()
    return addr


@pytest.fixture
def slot(db):
    row = DeliveryTimeSlot(
        name="Morning", start_time="10:00", end_time="12:00", is_active=True, available_days=[0, 1, 2, 3, 4, 5, 6]
    )
    db.session.add(row)
    db.session.commit()
    return row


def _create_body(user, address, product, slot, **overrides):
    """What the page's create form submits (buildSubscriptionPayload, isEdit false)."""
    body = {
        "user_id": user.id,
        "name": "Weekly Water",
        "description": None,
        "frequency": "weekly",
        "delivery_day_of_week": 3,
        "delivery_day_of_month": None,
        "delivery_time_slot_id": slot.id,
        "delivery_address_id": address.id,
        "payment_method": "cash",
        "auto_renew": True,
        "discount_percentage": 0,
        "loyalty_points_multiplier": None,
        "items": [{"product_id": product.id, "quantity": 2}],
    }
    body.update(overrides)
    return body


def _first_15th_after(days):
    day = date.today() + timedelta(days=days)
    while day.day != 15:
        day += timedelta(days=1)
    return day


def test_creating_weekly_on_thursday_stores_that_frequency_weekday_and_slot(
    client, db, admin_auth_headers, sample_user, address, sample_product, slot
):
    response = client.post(
        "/api/v1/admin/subscriptions",
        headers=admin_auth_headers,
        json=_create_body(sample_user, address, sample_product, slot),
    )

    assert response.status_code == 201, response.get_json()
    row = db.session.get(Subscription, response.get_json()["data"]["subscription"]["id"])
    assert row.frequency == SubscriptionFrequency.WEEKLY
    assert row.delivery_day_of_week == 3 and row.delivery_day_of_month is None
    assert row.delivery_time_slot_id == slot.id
    assert row.next_delivery_date.weekday() == 3


def test_the_list_filters_on_frequency_and_every_row_carries_the_schedule_the_page_shows(
    client, db, admin_auth_headers, sample_user, address, sample_product, slot
):
    created = client.post(
        "/api/v1/admin/subscriptions",
        headers=admin_auth_headers,
        json=_create_body(sample_user, address, sample_product, slot),
    ).get_json()["data"]["subscription"]

    weekly = client.get("/api/v1/admin/subscriptions?frequency=weekly", headers=admin_auth_headers)
    daily = client.get("/api/v1/admin/subscriptions?frequency=daily", headers=admin_auth_headers)

    (row,) = [r for r in weekly.get_json()["data"]["items"] if r["id"] == created["id"]]
    assert [r for r in daily.get_json()["data"]["items"] if r["id"] == created["id"]] == []
    assert row["frequency"] == "weekly"
    assert row["upcoming_delivery"]["date"] == row["next_delivery_date"]
    assert (row["upcoming_delivery"]["window_start"], row["upcoming_delivery"]["window_end"]) == ("10:00", "12:00")
    assert row["deliveries_per_month"] == pytest.approx(52 / 12)
    detail = client.get(f"/api/v1/admin/subscriptions/{created['id']}", headers=admin_auth_headers)
    shown = detail.get_json()["data"]["subscription"]
    assert shown["frequency"] == "weekly"
    assert shown["upcoming_delivery"] == row["upcoming_delivery"]


def test_editing_to_monthly_with_a_set_next_delivery_date_is_applied_through_the_override(
    client, db, admin_auth_headers, sample_user, address, sample_product, slot
):
    sub_id = client.post(
        "/api/v1/admin/subscriptions",
        headers=admin_auth_headers,
        json=_create_body(sample_user, address, sample_product, slot),
    ).get_json()["data"]["subscription"]["id"]
    target = _first_15th_after(3)

    # buildSubscriptionPayload(values, { isEdit: true }) for: frequency monthly, day 15, switch on, date picked
    response = client.put(
        f"/api/v1/admin/subscriptions/{sub_id}",
        headers=admin_auth_headers,
        json={
            "name": "Weekly Water",
            "frequency": "monthly",
            "delivery_day_of_week": None,
            "delivery_day_of_month": 15,
            "delivery_time_slot_id": slot.id,
            "delivery_address_id": address.id,
            "payment_method": "cash",
            "auto_renew": True,
            "discount_percentage": 0,
            "override_edit_any_status": False,
            "override_manual_billing_amount": False,
            "override_manual_billing_dates": True,
            "next_delivery_date": target.isoformat(),
        },
    )

    assert response.status_code == 200, response.get_json()
    db.session.expire_all()
    row = db.session.get(Subscription, sub_id)
    assert row.frequency == SubscriptionFrequency.MONTHLY
    # (a null day is "unchanged" to the endpoint, so the old weekday stays on the row; monthly ignores it)
    assert row.delivery_day_of_month == 15
    assert row.next_delivery_date == target


def test_a_next_delivery_date_without_the_switch_is_ignored(
    client, db, admin_auth_headers, sample_user, address, sample_product, slot
):
    created = client.post(
        "/api/v1/admin/subscriptions",
        headers=admin_auth_headers,
        json=_create_body(sample_user, address, sample_product, slot),
    ).get_json()["data"]["subscription"]
    before = db.session.get(Subscription, created["id"]).next_delivery_date

    response = client.put(
        f"/api/v1/admin/subscriptions/{created['id']}",
        headers=admin_auth_headers,
        json={"override_manual_billing_dates": False, "next_delivery_date": (before + timedelta(days=1)).isoformat()},
    )

    assert response.status_code == 200, response.get_json()
    db.session.expire_all()
    assert db.session.get(Subscription, created["id"]).next_delivery_date == before
