"""One slot writer (plan 2026-10-27 Task 7; spec 2026-10-27 §6.4, §6.5, §17 Q7, owner call 6).

Every admin slot write (create, edit, delete) goes through `DeliveryTimeSlotService`, which rewrites the schedules
the write touches in the same transaction and tells each customer once, after the commit:
- a slot switched off, or narrowed so it no longer covers a subscription's day: the subscription becomes Any time;
- new times: the subscription keeps the slot and is told the new window;
- a weekly or biweekly day that is no longer a delivery day: it moves to the next delivery day;
- a next delivery on a lost day: it moves on to the next delivery day;
- a delivery day added: a daily subscription now delivers on it too and is told (owner, 2026-10-27).
Orders already created keep their date and window. Deleting a slot a live subscription still uses is refused in
words. `flask subscriptions reconcile-delivery-days` applies the day rules once at deploy.

Driven through the endpoints the admin UI calls (`TimeSlots.js` `handleCreateSubmit` / `handleEditSubmit` bodies,
the role-claim bearer token) and the storefront's cookie JWT. Only external I/O is replaced: the Celery publish
(`published`), the HTTP hop to the customer bot (`bot`) and Brevo's POST.
"""

import os
import threading
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from flask import current_app
from flask_jwt_extended import create_access_token

from business_app.models.delivery import DeliveryTimeSlot
from business_app.models.subscription import Subscription, SubscriptionItem, SubscriptionLog
from business_app.models.translation import Translation
from business_app.models.user import User, UserAddress
from business_app.services import subscription_notices, subscription_schedule
from business_app.services.subscription_service import SubscriptionService
from business_app.tasks.subscription_tasks import process_daily_subscription_billing, send_subscription_notice_task
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for
from business_app.utils.local_windows import local_date
from business_app.utils.password_security import hash_password
from shared.enums import PaymentMethod, SubscriptionFrequency, SubscriptionStatus, UserRole, UserStatus, UserType
from tests.integration.test_customer_cancel_races_pg import _await_a_lock_waiter, _finish, _thread
from tests.integration.customer_notice_builders import (  # noqa: F401  (fixtures by their *_fixture name)
    BOT_URL,
    CONTRACT_KEYS,
    assert_published_after_commit,
    bot_fixture,
    on_email_fixture,
    on_telegram_fixture,
    timeline_fixture,
)
from tests.integration.test_subscription_action_guards import (  # noqa: F401  (fixtures by name)
    BASE,
    _create,
    _external_io,
    _orders,
    _published,
    _row,
    _state,
    admin_headers,
    clock,
    copy,
    every_day_slot,
    monday,
    other_every_day_slot,
    web,
    weekday_slot,
)
from tests.integration.test_subscription_order_notice import published  # noqa: F401  (the Celery publish spy)
from tests.notification_send_fakes import live_send_path

pytestmark = pytest.mark.integration

SLOTS = "/api/v1/admin/delivery/time-slots"
SATURDAY, SUNDAY = 5, 6
EVERY_DAY = [0, 1, 2, 3, 4, 5, 6]
MON_TO_SAT = [0, 1, 2, 3, 4, 5]
KIND = "subscription_schedule_changed"
# The copy these paths answer and log with, beyond the guards' own (`copy`).
SLOT_KEY_PREFIXES = ("api.admin.error.time_slot_", "api.admin.success.time_slot_", "api.subscriptions.log.")


@pytest.fixture(autouse=True)
def slot_copy(db, copy):
    keys = [key for key in BACKEND_TRANSLATIONS if key.startswith(SLOT_KEY_PREFIXES)]
    for key in keys:
        for language, value in BACKEND_TRANSLATIONS[key].items():
            db.session.add(Translation(key=key, language=language, value=value, category=_category_for(key)))
    db.session.commit()
    return keys


@pytest.fixture(autouse=True)
def _spied_publish(published):
    """Every notice publish in this module goes to the spy, never to the broker."""
    yield


def _edit_body(slot, **changes):
    """Exactly what `TimeSlots.js` `handleEditSubmit` sends."""
    return {"name": slot.name, "start_time": slot.start_time, "end_time": slot.end_time, "max_orders": slot.max_orders,
            "delivery_fee": float(slot.delivery_fee or 0), "is_premium": bool(slot.is_premium),
            "premium_fee": float(slot.premium_fee or 0), "is_active": slot.is_active,
            "available_days": list(slot.available_days), **changes}


def _create_body(**changes):
    """Exactly what `TimeSlots.js` `handleCreateSubmit` sends."""
    return {"name": "Evening", "start_time": "18:00", "end_time": "20:00", "max_orders": 50, "delivery_fee": 0,
            "is_premium": False, "premium_fee": 0, "is_active": True, "available_days": EVERY_DAY, **changes}


def _slot(db, days, *, name="Weekdays", start="10:00", end="12:00"):
    slot = DeliveryTimeSlot(name=name, start_time=start, end_time=end, is_active=True, available_days=list(days))
    db.session.add(slot)
    db.session.commit()
    return slot


def _items(product):
    return [{"product_id": product.id, "quantity": 2}]


def _changed_logs(db, sub_id=None):
    db.session.expire_all()
    query = SubscriptionLog.query.filter_by(action=subscription_notices.SCHEDULE_CHANGED)
    if sub_id is not None:
        query = query.filter_by(subscription_id=sub_id)
    return query.order_by(SubscriptionLog.id).all()


def _translated(key, **values):
    return {value.format(**values) for value in BACKEND_TRANSLATIONS[key].values()}


def _sent_payloads(bot, log_ids):
    """Each committed notice, sent by the real task through the bot recorder."""
    for log_id in log_ids:
        assert send_subscription_notice_task(log_id) == "telegram"
    return [call["payload"] for call in bot.calls[-len(log_ids):]]


def _pause(web, sub_id):
    assert web.post(f"{BASE}/{sub_id}/pause", {"expected_state": _state(web, sub_id)}).status_code == 200


# --------------------------------------------------------------------------- switched off, narrowed, new times


def test_switching_off_a_slot_moves_its_subscriptions_to_any_time_and_tells_each_customer(
    db, client, web, clock, monday, admin_headers, on_telegram, bot, published, timeline, user_address,
    sample_product, every_day_slot, other_every_day_slot,
):
    active = _create(web, user_address.id, _items(sample_product), delivery_time_slot_id=every_day_slot.id)
    paused = _create(web, user_address.id, _items(sample_product), delivery_time_slot_id=every_day_slot.id)
    other = _create(web, user_address.id, _items(sample_product), delivery_time_slot_id=other_every_day_slot.id)
    _pause(web, paused)
    clock.at(monday + timedelta(days=2))
    process_daily_subscription_billing()
    [order] = _orders(db, active)
    published.clear()

    resp = client.put(f"{SLOTS}/{every_day_slot.id}", headers=admin_headers,
                      json=_edit_body(every_day_slot, is_active=False))

    assert resp.status_code == 200, resp.get_json()
    assert _row(db, active).delivery_time_slot_id is None
    assert _row(db, paused).delivery_time_slot_id is None
    assert _row(db, other).delivery_time_slot_id == other_every_day_slot.id
    [kept] = _orders(db, active)
    assert (kept.id, kept.delivery_window_start) == (order.id, time(10, 0))  # the created order keeps its window
    logs = _changed_logs(db)
    assert [log.subscription_id for log in logs] == [active, paused]
    for log in logs:
        assert log.extra_data["changes"] == ["time_cleared"]
        assert log.extra_data["old_window"] == "10:00–12:00"
        assert log.extra_data["new_window"] is None
        assert log.extra_data["time_kind"] == "any_time"
    assert _changed_logs(db, other) == []
    assert published == [log.id for log in logs]
    for log in logs:
        assert_published_after_commit(timeline, "SubscriptionLog", log.id)

    payloads = _sent_payloads(bot, published)
    for payload in payloads:
        assert payload["kind"] == KIND
        assert set(payload) == CONTRACT_KEYS[KIND]
    assert [payload["next_delivery_date"] for payload in payloads] == [
        _row(db, active).next_delivery_date.isoformat(), None,  # a paused subscription has no next delivery
    ]


def test_saving_a_slot_already_switched_off_moves_the_subscriptions_left_on_it_once(
    db, client, web, monday, admin_headers, on_telegram, bot, published, user_address, sample_product,
    every_day_slot,
):
    """A slot switched off before this release left its subscriptions on "Time to be confirmed": switching off
    cleared nothing then, and the deploy reconcile moves only lost days. The next save of that slot, still off,
    moves them to Any time naming the window they had, and tells each customer once; the delete it was refused
    then goes through, as its refusal says."""
    sub_id = _create(web, user_address.id, _items(sample_product), delivery_time_slot_id=every_day_slot.id)
    every_day_slot.is_active = False  # switched off with no cascade, as before this release
    db.session.commit()
    assert _published(web, sub_id)["time_kind"] == "to_be_confirmed"
    refused = client.delete(f"{SLOTS}/{every_day_slot.id}", headers=admin_headers)
    assert refused.status_code == 409, refused.get_json()
    published.clear()

    resp = client.put(f"{SLOTS}/{every_day_slot.id}", headers=admin_headers, json=_edit_body(every_day_slot))

    assert resp.status_code == 200, resp.get_json()
    assert _row(db, sub_id).delivery_time_slot_id is None
    [log] = _changed_logs(db, sub_id)
    assert log.extra_data["changes"] == ["time_cleared"]
    assert (log.extra_data["old_window"], log.extra_data["new_window"]) == ("10:00–12:00", None)
    assert log.extra_data["time_kind"] == "any_time"
    assert published == [log.id]
    [payload] = _sent_payloads(bot, published)
    assert set(payload) == CONTRACT_KEYS[KIND]
    assert _published(web, sub_id)["time_kind"] == "any_time"

    again = client.put(f"{SLOTS}/{every_day_slot.id}", headers=admin_headers,
                       json=_edit_body(every_day_slot, name="Retired"))
    assert again.status_code == 200, again.get_json()
    assert [row.id for row in _changed_logs(db)] == [log.id]  # told once: the next save finds no one left on it
    assert published == [log.id]
    deleted = client.delete(f"{SLOTS}/{every_day_slot.id}", headers=admin_headers)
    assert deleted.status_code == 200, deleted.get_json()


def test_narrowing_days_clears_only_the_subscriptions_the_slot_no_longer_covers(
    db, client, web, monday, admin_headers, user_address, sample_product, every_day_slot, other_every_day_slot,
):
    on_slot = {"delivery_time_slot_id": every_day_slot.id}
    thursday = _create(web, user_address.id, _items(sample_product), **on_slot)
    saturday = _create(web, user_address.id, _items(sample_product), delivery_day_of_week=SATURDAY, **on_slot)
    daily = _create(web, user_address.id, _items(sample_product), frequency="daily", delivery_day_of_week=None,
                    **on_slot)
    monthly = _create(web, user_address.id, _items(sample_product), frequency="monthly", delivery_day_of_week=None,
                      delivery_day_of_month=15, **on_slot)
    dates = {sub_id: _row(db, sub_id).next_delivery_date for sub_id in (thursday, saturday, daily, monthly)}

    resp = client.put(f"{SLOTS}/{every_day_slot.id}", headers=admin_headers,
                      json=_edit_body(every_day_slot, available_days=[0, 1, 2, 3, 4]))

    assert resp.status_code == 200, resp.get_json()
    assert _row(db, thursday).delivery_time_slot_id == every_day_slot.id
    for sub_id in (saturday, daily, monthly):
        assert _row(db, sub_id).delivery_time_slot_id is None
    logs = _changed_logs(db)
    assert sorted(log.subscription_id for log in logs) == sorted([saturday, daily, monthly])
    assert all(log.extra_data["changes"] == ["time_cleared"] for log in logs)
    # Saturday is still a delivery day (the other slot), so nothing else moves.
    assert {sub_id: _row(db, sub_id).next_delivery_date for sub_id in dates} == dates


def test_new_times_keep_the_slot_and_tell_the_new_time(
    db, client, web, clock, monday, admin_headers, published, user_address, sample_product, every_day_slot,
):
    sub_id = _create(web, user_address.id, _items(sample_product), delivery_time_slot_id=every_day_slot.id)
    clock.at(monday + timedelta(days=2))
    process_daily_subscription_billing()
    published.clear()

    resp = client.put(f"{SLOTS}/{every_day_slot.id}", headers=admin_headers,
                      json=_edit_body(every_day_slot, start_time="11:00", end_time="13:00"))

    assert resp.status_code == 200, resp.get_json()
    assert _row(db, sub_id).delivery_time_slot_id == every_day_slot.id
    [log] = _changed_logs(db, sub_id)
    assert log.extra_data["changes"] == ["time_changed"]
    assert (log.extra_data["old_window"], log.extra_data["new_window"]) == ("10:00–12:00", "11:00–13:00")
    assert log.extra_data["time_kind"] == "window"
    assert published == [log.id]
    assert _published(web, sub_id)["schedule_sentence"] == "Every Thursday, 11:00–13:00"
    assert _orders(db, sub_id)[0].delivery_window_start == time(10, 0)


# --------------------------------------------------------------------------- delivery days lost


def test_a_weekday_that_stops_being_a_delivery_day_moves_the_subscription_to_the_next_one(
    db, client, web, monday, admin_headers, user_address, sample_product, every_day_slot,
):
    sub_id = _create(web, user_address.id, _items(sample_product), delivery_day_of_week=SUNDAY)
    sunday = _row(db, sub_id).next_delivery_date
    assert sunday.weekday() == SUNDAY

    resp = client.put(f"{SLOTS}/{every_day_slot.id}", headers=admin_headers,
                      json=_edit_body(every_day_slot, available_days=MON_TO_SAT))

    assert resp.status_code == 200, resp.get_json()
    row = _row(db, sub_id)
    assert row.delivery_day_of_week == 0
    assert row.next_delivery_date == sunday + timedelta(days=1)
    [log] = _changed_logs(db, sub_id)
    assert log.extra_data["changes"] == ["day_moved"]
    assert (log.extra_data["old_weekday"], log.extra_data["new_weekday"]) == (SUNDAY, 0)
    assert log.extra_data["next_delivery_date"] == (sunday + timedelta(days=1)).isoformat()


def test_a_daily_any_time_next_date_on_a_lost_day_moves_on(
    db, client, web, monday, admin_headers, user_address, sample_product, every_day_slot,
):
    sub_id = _create(web, user_address.id, _items(sample_product), frequency="daily", delivery_day_of_week=None)
    old_next = _row(db, sub_id).next_delivery_date
    lost = old_next.weekday()

    resp = client.put(f"{SLOTS}/{every_day_slot.id}", headers=admin_headers,
                      json=_edit_body(every_day_slot, available_days=[day for day in EVERY_DAY if day != lost]))

    assert resp.status_code == 200, resp.get_json()
    assert _row(db, sub_id).next_delivery_date == old_next + timedelta(days=1)
    [log] = _changed_logs(db, sub_id)
    assert log.extra_data["changes"] == ["date_moved"]
    assert log.extra_data["old_weekday"] == lost


def test_a_change_that_touches_nobody_writes_nothing(
    db, client, web, monday, admin_headers, published, user_address, sample_product, every_day_slot,
):
    sub_id = _create(web, user_address.id, _items(sample_product), delivery_time_slot_id=every_day_slot.id)
    published.clear()

    resp = client.put(f"{SLOTS}/{every_day_slot.id}", headers=admin_headers,
                      json=_edit_body(every_day_slot, name="Late morning"))

    assert resp.status_code == 200, resp.get_json()
    db.session.expire_all()
    assert db.session.get(DeliveryTimeSlot, every_day_slot.id).name == "Late morning"
    assert _row(db, sub_id).delivery_time_slot_id == every_day_slot.id
    assert _changed_logs(db) == []
    assert published == []


# --------------------------------------------------------------------------- delivery days added (owner call 6)


def test_adding_a_delivery_day_tells_each_daily_any_time_subscriber(
    db, client, web, monday, admin_headers, on_telegram, bot, published, user_address, sample_product,
):
    _slot(db, MON_TO_SAT)
    daily = _create(web, user_address.id, _items(sample_product), frequency="daily", delivery_day_of_week=None)
    paused = _create(web, user_address.id, _items(sample_product), frequency="daily", delivery_day_of_week=None)
    weekly = _create(web, user_address.id, _items(sample_product))
    _pause(web, paused)
    next_daily = _row(db, daily).next_delivery_date
    published.clear()

    resp = client.post(SLOTS, headers=admin_headers, json=_create_body(available_days=[SUNDAY]))

    assert resp.status_code == 201, resp.get_json()
    logs = _changed_logs(db)
    assert [log.subscription_id for log in logs] == [daily, paused]
    for log in logs:
        assert log.extra_data["changes"] == ["days_added"]
        assert log.extra_data["added_weekdays"] == [SUNDAY]
    assert _changed_logs(db, weekly) == []
    assert _row(db, daily).next_delivery_date == next_daily
    assert published == [log.id for log in logs]
    for payload in _sent_payloads(bot, published):
        assert set(payload) == CONTRACT_KEYS[KIND]
        assert (payload["changes"], payload["added_weekdays"]) == (["days_added"], [SUNDAY])


def test_a_new_delivery_day_never_skips_a_monthly_cycle(
    db, client, web, clock, admin_headers, published, sample_user, user_address, sample_product,
):
    """January's 31st, a Sunday, moved to Monday 1 February under Monday to Saturday. A Sunday slot added before
    it is delivered keeps it there: never earlier, no notice, and February's 28th follows."""
    _slot(db, MON_TO_SAT)
    clock.at(date(2027, 1, 18))
    row = Subscription(
        user_id=sample_user.id, name="Monthly water", status=SubscriptionStatus.ACTIVE,
        frequency=SubscriptionFrequency.MONTHLY, delivery_day_of_month=31, delivery_address_id=user_address.id,
        payment_method=PaymentMethod.CLICK, billing_amount=Decimal("30000.00"), start_date=datetime.now(UTC),
        next_delivery_date=date(2027, 2, 1),
    )
    db.session.add(row)
    db.session.flush()
    db.session.add(SubscriptionItem(subscription_id=row.id, product_id=sample_product.id, quantity=2,
                                    unit_price=sample_product.base_price, total_price=sample_product.base_price * 2))
    db.session.commit()
    sub_id = row.id

    resp = client.post(SLOTS, headers=admin_headers, json=_create_body(available_days=[SUNDAY]))

    assert resp.status_code == 201, resp.get_json()
    row = _row(db, sub_id)
    assert row.next_delivery_date == date(2027, 2, 1)
    assert _changed_logs(db, sub_id) == []
    assert published == []
    assert subscription_schedule.advance(row, row.next_delivery_date) == date(2027, 2, 28)
    upcoming = [day["date"] for day in _published(web, sub_id)["upcoming_dates"]]
    assert upcoming[:3] == ["2027-02-01", "2027-02-28", "2027-03-31"]


def test_widening_a_slot_tells_the_daily_subscribers_on_it(
    db, client, web, monday, admin_headers, published, user_address, sample_product, weekday_slot,
):
    """Monday to Friday is the only slot, so Saturday is not a delivery day until the slot widens to it."""
    on_slot = _create(web, user_address.id, _items(sample_product), frequency="daily", delivery_day_of_week=None,
                      delivery_time_slot_id=weekday_slot.id)
    any_time = _create(web, user_address.id, _items(sample_product), frequency="daily", delivery_day_of_week=None)
    published.clear()

    resp = client.put(f"{SLOTS}/{weekday_slot.id}", headers=admin_headers,
                      json=_edit_body(weekday_slot, available_days=[0, 1, 2, 3, 4, 5]))

    assert resp.status_code == 200, resp.get_json()
    assert _row(db, on_slot).delivery_time_slot_id == weekday_slot.id
    for sub_id in (on_slot, any_time):
        [log] = _changed_logs(db, sub_id)
        assert log.extra_data["changes"] == ["days_added"]
        assert log.extra_data["added_weekdays"] == [SATURDAY]
    assert len(published) == 2


def test_the_deploy_reconcile_never_reports_added_days(
    db, web, runner, monday, published, user_address, sample_product, weekday_slot,
):
    sub_id = _create(web, user_address.id, _items(sample_product), frequency="daily", delivery_day_of_week=None)
    weekday_slot.available_days = [0, 1, 2, 3, 4, 5]  # widened before the deploy, with no cascade
    db.session.commit()
    published.clear()

    result = runner.invoke(args=["subscriptions", "reconcile-delivery-days"])

    assert result.exit_code == 0, result.output
    assert "0 subscriptions changed" in result.output
    assert _changed_logs(db, sub_id) == []
    assert published == []


# --------------------------------------------------------------------------- delete


def test_deleting_a_slot_a_live_subscription_uses_is_refused_in_words(
    db, client, web, monday, admin_headers, user_address, sample_product, every_day_slot,
):
    active = _create(web, user_address.id, _items(sample_product), delivery_time_slot_id=every_day_slot.id)
    paused = _create(web, user_address.id, _items(sample_product), delivery_time_slot_id=every_day_slot.id)
    _pause(web, paused)

    resp = client.delete(f"{SLOTS}/{every_day_slot.id}", headers=admin_headers)

    assert resp.status_code == 409, resp.get_json()
    assert resp.get_json()["message"] in _translated("api.admin.error.time_slot_in_use", count=2)
    db.session.expire_all()
    assert db.session.get(DeliveryTimeSlot, every_day_slot.id) is not None
    assert _row(db, active).delivery_time_slot_id == every_day_slot.id
    assert _row(db, paused).delivery_time_slot_id == every_day_slot.id


def test_deleting_a_slot_only_ended_subscriptions_used_clears_them_and_deletes(
    db, client, web, monday, admin_headers, published, user_address, sample_product, every_day_slot,
    other_every_day_slot,
):
    sub_id = _create(web, user_address.id, _items(sample_product), delivery_time_slot_id=every_day_slot.id)
    assert web.post(f"{BASE}/{sub_id}/cancel", {"expected_state": _state(web, sub_id)}).status_code == 200
    published.clear()

    resp = client.delete(f"{SLOTS}/{every_day_slot.id}", headers=admin_headers)

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["message"] in _translated("api.admin.success.time_slot_deleted")
    db.session.expire_all()
    assert db.session.get(DeliveryTimeSlot, every_day_slot.id) is None
    assert _row(db, sub_id).delivery_time_slot_id is None
    assert _changed_logs(db) == []
    assert published == []


def test_deleting_the_only_sunday_slot_moves_sunday_subscriptions(
    db, client, web, monday, admin_headers, published, user_address, sample_product,
):
    _slot(db, MON_TO_SAT)
    sundays = _slot(db, [SUNDAY], name="Sunday")
    sub_id = _create(web, user_address.id, _items(sample_product), delivery_day_of_week=SUNDAY)
    sunday = _row(db, sub_id).next_delivery_date
    published.clear()

    resp = client.delete(f"{SLOTS}/{sundays.id}", headers=admin_headers)

    assert resp.status_code == 200, resp.get_json()
    row = _row(db, sub_id)
    assert (row.delivery_day_of_week, row.next_delivery_date) == (0, sunday + timedelta(days=1))
    [log] = _changed_logs(db, sub_id)
    assert log.extra_data["changes"] == ["day_moved"]
    assert published == [log.id]


def _pg_people(pg_db):
    """REAL POSTGRES: a customer with a home address and an admin, each with the bearer token its client sends."""
    users = []
    for phone, role, user_type in (("+998901242001", UserRole.CUSTOMER, UserType.INDIVIDUAL),
                                   ("+998901242002", UserRole.ADMIN, UserType.STAFF)):
        user = User(email=f"slot-{phone[-4:]}@example.com", phone=phone, password_hash=hash_password("Pass123!x"),
                    first_name="Slot", last_name=role.value.title(), user_type=user_type, role=role,
                    status=UserStatus.ACTIVE, is_verified=True, created_at=datetime.now(UTC))
        pg_db.session.add(user)
        users.append(user)
    pg_db.session.flush()
    customer, admin = users
    address = UserAddress(user_id=customer.id, title="Home", full_address="1 Slot Street, Tashkent",
                          street_address="1 Slot Street", city="Tashkent", latitude=41.3111, longitude=69.2797,
                          is_default=True)
    pg_db.session.add(address)
    pg_db.session.commit()
    return SimpleNamespace(
        customer=customer, address=address,
        customer_headers={"Authorization": "Bearer " + create_access_token(identity=str(customer.id))},
        admin_headers={"Authorization": "Bearer " + create_access_token(identity=str(admin.id),
                                                                        additional_claims={"role": "admin"})},
    )


def _pg_slots(pg_db, *slots):
    rows = [DeliveryTimeSlot(name=name, start_time=start, end_time=end, is_active=True, available_days=list(days))
            for name, start, end, days in slots]
    pg_db.session.add_all(rows)
    pg_db.session.commit()
    return rows


def _pg_subscription(pg_db, user, address, slot, status, *, day_of_week=3, next_delivery_date=None):
    row = Subscription(
        user_id=user.id, name="Water", status=status, frequency=SubscriptionFrequency.WEEKLY,
        delivery_day_of_week=day_of_week, delivery_time_slot_id=slot.id if slot is not None else None,
        delivery_address_id=address.id, payment_method=PaymentMethod.CASH, billing_amount=Decimal("30000.00"),
        start_date=datetime.now(UTC), next_delivery_date=next_delivery_date,
    )
    pg_db.session.add(row)
    pg_db.session.commit()
    return row.id


def _a_week_on(weekday):
    """A date on `weekday` more than a week away: no order is due for it yet."""
    day = local_date() + timedelta(days=8)
    return day + timedelta(days=(weekday - day.weekday()) % 7)


def test_deleting_a_slot_under_the_real_foreign_key(pg_app, pg_db):
    """REAL POSTGRES: `subscriptions.delivery_time_slot_id` is a NO ACTION foreign key the SQLite suite does not
    enforce. A slot only an ended subscription used is let go of and deleted; one a live subscription uses is
    refused, never the foreign key's 500. A slot chosen on the admin form is read FOR SHARE."""
    people = _pg_people(pg_db)
    customer, address = people.customer, people.address
    ended, used, chosen = _pg_slots(pg_db, ("Old", "08:00", "09:00", EVERY_DAY),
                                    ("Morning", "10:00", "12:00", EVERY_DAY), ("Noon", "12:00", "14:00", EVERY_DAY))
    cancelled_id = _pg_subscription(pg_db, customer, address, ended, SubscriptionStatus.CANCELLED)
    active_id = _pg_subscription(pg_db, customer, address, chosen, SubscriptionStatus.ACTIVE)
    headers = people.admin_headers
    client = pg_app.test_client()

    moved = client.put(f"/api/v1/admin/subscriptions/{active_id}", headers=headers,
                       json={"delivery_time_slot_id": used.id})
    refused = client.delete(f"{SLOTS}/{used.id}", headers=headers)
    deleted = client.delete(f"{SLOTS}/{ended.id}", headers=headers)

    assert moved.status_code == 200, moved.get_json()
    assert refused.status_code == 409, refused.get_json()
    assert deleted.status_code == 200, deleted.get_json()
    pg_db.session.expire_all()
    assert pg_db.session.get(DeliveryTimeSlot, ended.id) is None
    assert pg_db.session.get(DeliveryTimeSlot, used.id) is not None
    assert pg_db.session.get(Subscription, cancelled_id).delivery_time_slot_id is None
    assert pg_db.session.get(Subscription, active_id).delivery_time_slot_id == used.id


def test_a_slot_switched_off_while_a_customer_chooses_it_queues_behind_the_save(
    pg_app, pg_db, monkeypatch, published,
):
    """REAL POSTGRES, the two writes interleaved for real. The customer's schedule save (the cabinet's PUT) locks
    its subscription, then reads the chosen slot FOR SHARE; the slot write locks every live subscription before the
    slot. Held after its subscription lock, the save makes the switch-off wait on that row, never the other way
    round: a slot write that locked the slot first would wait on the row while the save waits on the slot, and
    Postgres would abort one of them (a 500). Released, the save commits its choice, and the switch-off then moves
    it to Any time and tells the customer."""
    current_app.config["REDIS_URL"] = os.environ["REDIS_URL"]
    people = _pg_people(pg_db)
    [noon] = _pg_slots(pg_db, ("Noon", "12:00", "14:00", EVERY_DAY))
    sub_id = _pg_subscription(pg_db, people.customer, people.address, None, SubscriptionStatus.ACTIVE,
                              next_delivery_date=_a_week_on(3))
    client = pg_app.test_client()
    shown = client.get(f"{BASE}/{sub_id}", headers=people.customer_headers).get_json()["data"]["subscription"]
    # What the cabinet's schedule editor sends (write_schedule_saturday.json): the schedule and the screen's state.
    choose = {"frequency": "weekly", "delivery_day_of_week": 3, "delivery_time_slot_id": noon.id,
              "expected_state": shown["allowed_actions"]["expected_state"]}
    save_holds_its_row = threading.Event()
    release_the_save = threading.Event()
    chosen_slot = SubscriptionService._chosen_slot

    # Asked right after the save's locked read of the subscription, before it reads the slot.
    def held_before_the_slot_read(slot_id):
        if threading.current_thread().name == "customer" and not save_holds_its_row.is_set():
            save_holds_its_row.set()
            assert release_the_save.wait(timeout=30), "the test never released the save"
        return chosen_slot(slot_id)

    monkeypatch.setattr(SubscriptionService, "_chosen_slot", staticmethod(held_before_the_slot_read))
    answers, errors = {}, []
    switch_off = _edit_body(noon, is_active=False)  # what TimeSlots.js sends, read before the threads start
    noon_id = noon.id
    customer = _thread("customer", pg_app, lambda c: c.put(f"{BASE}/{sub_id}", headers=people.customer_headers,
                                                         json=choose), answers, errors, finally_set=save_holds_its_row)
    slot_url = f"{SLOTS}/{noon_id}"
    admin = _thread("admin", pg_app, lambda c: c.put(slot_url, headers=people.admin_headers, json=switch_off),
                    answers, errors)

    customer.start()
    try:
        assert save_holds_its_row.wait(timeout=30), "the save never reached its slot read"
        assert errors == [], errors
        admin.start()
        assert _await_a_lock_waiter(pg_db), "the switch-off did not wait on the subscription the save holds"
        assert admin.is_alive(), "the switch-off finished while the save held the subscription"
    finally:
        release_the_save.set()
        _finish(customer, *([admin] if admin.ident else []))

    assert errors == []
    assert answers["customer"][0] == 200, answers["customer"]
    assert answers["admin"][0] == 200, answers["admin"]
    pg_db.session.expire_all()
    assert pg_db.session.get(DeliveryTimeSlot, noon_id).is_active is False
    assert pg_db.session.get(Subscription, sub_id).delivery_time_slot_id is None
    [log] = SubscriptionLog.query.filter_by(subscription_id=sub_id, action=subscription_notices.SCHEDULE_CHANGED).all()
    assert (log.extra_data["changes"], log.extra_data["old_window"]) == (["time_cleared"], "12:00–14:00")
    assert published == [log.id]


def test_two_slot_writes_at_once_each_see_the_delivery_days_the_other_left(pg_app, pg_db, monkeypatch, published):
    """REAL POSTGRES: two admins switch off the last two Sunday slots at the same moment. Each write locks the live
    subscriptions before it reads the delivery days, so the second waits for the first and then sees no Sunday
    left: the weekly Sunday subscription moves to Monday. Read before those locks, each would still see the
    other's Sunday slot, and neither would move it."""
    current_app.config["REDIS_URL"] = os.environ["REDIS_URL"]
    people = _pg_people(pg_db)
    _, early, late = _pg_slots(pg_db, ("Weekdays", "10:00", "12:00", MON_TO_SAT),
                               ("Sunday morning", "09:00", "11:00", [SUNDAY]),
                               ("Sunday noon", "12:00", "14:00", [SUNDAY]))
    sunday = _a_week_on(SUNDAY)
    sub_id = _pg_subscription(pg_db, people.customer, people.address, None, SubscriptionStatus.ACTIVE,
                              day_of_week=SUNDAY, next_delivery_date=sunday)
    first_holds_the_rows = threading.Event()
    release_the_first = threading.Event()
    after_slots = SubscriptionService._schedule_after_slots.__func__

    # Asked for each live subscription once the first write holds them and has read the delivery days.
    def held_in_the_first_reconcile(cls, *args):
        if threading.current_thread().name == "first" and not first_holds_the_rows.is_set():
            first_holds_the_rows.set()
            assert release_the_first.wait(timeout=30), "the test never released the first write"
        return after_slots(cls, *args)

    monkeypatch.setattr(SubscriptionService, "_schedule_after_slots", classmethod(held_in_the_first_reconcile))
    answers, errors = {}, []
    # What TimeSlots.js sends for each switch-off, read before the threads start.
    (early_url, early_off), (late_url, late_off) = (
        (f"{SLOTS}/{slot.id}", _edit_body(slot, is_active=False)) for slot in (early, late)
    )
    first = _thread("first", pg_app, lambda c: c.put(early_url, headers=people.admin_headers, json=early_off),
                    answers, errors, finally_set=first_holds_the_rows)
    second = _thread("second", pg_app, lambda c: c.put(late_url, headers=people.admin_headers, json=late_off),
                     answers, errors)

    first.start()
    try:
        assert first_holds_the_rows.wait(timeout=30), "the first write never reached its reconcile"
        assert errors == [], errors
        second.start()
        assert _await_a_lock_waiter(pg_db), "the second write did not wait for the first"
        assert second.is_alive(), "the second write finished while the first held the subscriptions"
    finally:
        release_the_first.set()
        _finish(first, *([second] if second.ident else []))

    assert errors == []
    assert (answers["first"][0], answers["second"][0]) == (200, 200), answers
    pg_db.session.expire_all()
    row = pg_db.session.get(Subscription, sub_id)
    assert (row.delivery_day_of_week, row.next_delivery_date) == (0, sunday + timedelta(days=1))
    [log] = SubscriptionLog.query.filter_by(subscription_id=sub_id, action=subscription_notices.SCHEDULE_CHANGED).all()
    assert log.extra_data["changes"] == ["day_moved"]
    assert (log.extra_data["old_weekday"], log.extra_data["new_weekday"]) == (SUNDAY, 0)
    assert published == [log.id]


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "method, changes, key",
    [
        ("put", {"available_days": []}, "api.admin.error.time_slot_days_invalid"),
        ("put", {"available_days": [7]}, "api.admin.error.time_slot_days_invalid"),
        ("put", {"available_days": [True]}, "api.admin.error.time_slot_days_invalid"),
        ("put", {"start_time": "25:00"}, "api.admin.error.time_slot_times_invalid"),
        ("put", {"end_time": "10"}, "api.admin.error.time_slot_times_invalid"),
        ("put", {"end_time": ""}, "api.admin.error.time_slot_times_invalid"),
        ("post", {"available_days": [9]}, "api.admin.error.time_slot_days_invalid"),
        ("post", {"start_time": "9:00"}, "api.admin.error.time_slot_times_invalid"),
    ],
    ids=["no-day", "day-7", "bool-day", "hour-25", "no-minutes", "blank", "post-day-9", "post-short-hour"],
)
def test_a_bad_day_list_or_time_is_refused(db, client, admin_headers, every_day_slot, method, changes, key):
    before = _edit_body(every_day_slot)

    if method == "put":
        resp = client.put(f"{SLOTS}/{every_day_slot.id}", headers=admin_headers,
                          json=_edit_body(every_day_slot, **changes))
    else:
        resp = client.post(SLOTS, headers=admin_headers, json=_create_body(**changes))

    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["message"] in _translated(key)
    db.session.expire_all()
    assert DeliveryTimeSlot.query.count() == 1
    assert _edit_body(db.session.get(DeliveryTimeSlot, every_day_slot.id)) == before


def test_a_day_list_is_saved_deduplicated_and_sorted(db, client, admin_headers, every_day_slot):
    resp = client.put(f"{SLOTS}/{every_day_slot.id}", headers=admin_headers,
                      json=_edit_body(every_day_slot, available_days=[4, 0, 4, 2]))

    assert resp.status_code == 200, resp.get_json()
    db.session.expire_all()
    assert db.session.get(DeliveryTimeSlot, every_day_slot.id).available_days == [0, 2, 4]


def test_an_unknown_slot_answers_404_in_words(db, client, admin_headers):
    for resp in (
        client.put(f"{SLOTS}/987654", headers=admin_headers, json=_create_body()),
        client.delete(f"{SLOTS}/987654", headers=admin_headers),
    ):
        assert resp.status_code == 404, resp.get_json()
        assert resp.get_json()["message"] in _translated("api.admin.error.time_slot_not_found")


# --------------------------------------------------------------------------- the deploy reconcile


def test_the_deploy_reconcile_moves_what_the_slots_no_longer_offer(
    db, web, runner, monday, published, user_address, sample_product, every_day_slot,
):
    sub_id = _create(web, user_address.id, _items(sample_product), delivery_day_of_week=SUNDAY)
    sunday = _row(db, sub_id).next_delivery_date
    every_day_slot.available_days = MON_TO_SAT  # narrowed before the deploy, with no cascade
    db.session.commit()
    published.clear()

    dry = runner.invoke(args=["subscriptions", "reconcile-delivery-days", "--dry-run"])

    assert dry.exit_code == 0, dry.output
    assert "1 subscriptions would change" in dry.output
    row = _row(db, sub_id)
    assert (row.delivery_day_of_week, row.next_delivery_date) == (SUNDAY, sunday)
    assert _changed_logs(db) == []
    assert published == []

    real = runner.invoke(args=["subscriptions", "reconcile-delivery-days"])

    assert real.exit_code == 0, real.output
    assert "1 subscriptions changed" in real.output
    row = _row(db, sub_id)
    assert (row.delivery_day_of_week, row.next_delivery_date) == (0, sunday + timedelta(days=1))
    [log] = _changed_logs(db, sub_id)
    assert log.extra_data["changes"] == ["day_moved"]
    assert log.user_id is None
    assert published == [log.id]


# --------------------------------------------------------------------------- the email


def test_the_schedule_notice_goes_by_email_without_telegram(
    app, monkeypatch, db, client, web, monday, admin_headers, on_email, bot, published, user_address,
    sample_product, every_day_slot,
):
    sub_id = _create(web, user_address.id, _items(sample_product), delivery_time_slot_id=every_day_slot.id)
    published.clear()
    resp = client.put(f"{SLOTS}/{every_day_slot.id}", headers=admin_headers,
                      json=_edit_body(every_day_slot, is_active=False))
    assert resp.status_code == 200, resp.get_json()
    [log] = _changed_logs(db, sub_id)

    with live_send_path(app, monkeypatch, bot_url=BOT_URL) as post:
        assert send_subscription_notice_task(log.id) == "email"

    assert bot.calls == []
    [call] = post.call_args_list
    body = call.kwargs["json"]
    assert "Your delivery schedule changed" in body["subject"]
    html = body["htmlContent"]
    assert "10:00–12:00" in html and "any time of the day" in html
    assert "Home water" in html
