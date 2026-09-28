"""The operator's failed-delivery API: the list, the one-row read and the dated re-dispatch (F9, F12).

Driven with an operator token through the three routes the staff bot calls:
- `GET /api/v1/staff/delivery/failed`. Each row names the driver who attempted the door, when the
  delivery failed, and the days it may move to. `total` is the whole set, never the length of the
  capped page.
- `GET /api/v1/staff/delivery/failed/<id>`. One row with no cap, or the re-dispatch's own refusal.
  The bot asks it on every tap that draws dates, so a date button always comes from the bounds the
  backend published for that delivery at that moment.
- `POST /api/v1/staff/delivery/redispatch/<id>`, body `{"delivery_date": "YYYY-MM-DD"}`. It lands on
  the day tapped and says whether, and how, the customer is told. No date means local today. A date
  that does not parse is refused by name. A date the re-date rule refuses answers the rule's code.

The business clock is frozen on the three seams that read it (2026-09-23 reschedule spec §7,
through `_freeze_business_clock`). Celery publishes are recorded against each task's own
signature (`enqueued`). Nothing else is mocked.

Spec: docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md (§3.5, §8 item 6,
Review Focus 1).
"""

import inspect
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from business_app.models.corporate import CorporateContract, CorporateContractStatus
from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.staff_service import StaffService
from business_app.tasks.notification_tasks import send_delivery_rescheduled_notification_task
from business_app.utils.password_security import hash_password
from business_app.utils.timezone_utils import ensure_utc
from shared.business_config import MAX_SCHEDULE_HORIZON_DAYS
from shared.enums import (
    CorporateContractTrackingMode,
    DeliveryStatus,
    EntitySubtype,
    OrderStatus,
    PaymentMethod,
    UserRole,
    UserType,
)
from staff_bot.handlers.base import BaseHandler
from tests.integration.test_redispatch_reschedules_to_today import (
    CUSTOMER_WINDOW,
    REFUSALS,
    STAFF_REDISPATCH,
    TZ,
    _freeze_business_clock,
    _last_history,
    _make_stale,
    _published_release,
    _snapshot,
)
from tests.integration.test_redispatch_reschedules_to_today import enqueued, rostered_driver  # noqa: F401 (fixtures)

pytestmark = pytest.mark.integration

FAILED_LIST = "/api/v1/staff/delivery/failed"
FAILED_ROW = "/api/v1/staff/delivery/failed/{}"
ADMIN_DELIVERY = "/api/v1/admin/deliveries/{}"
OPERATOR_USERS = "/api/v1/staff/operator/users"
# The page the list route serves: `get_failed_deliveries`' own default, read rather than restated.
PAGE_SIZE = inspect.signature(StaffService.get_failed_deliveries).parameters["limit"].default


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def _utc(day, wall_clock):
    """A Tashkent wall-clock moment as the aware UTC instant the timestamp columns store."""
    return datetime.combine(day, wall_clock, tzinfo=TZ).astimezone(timezone.utc)


def _move_business_clock(monkeypatch, moment):
    """Move the frozen business clock on to `moment`, on the three seams `_freeze_business_clock` pins.

    That helper keeps the real date, so crossing local midnight needs this one.
    """
    from business_app.services import order_schedule_service
    from business_app.utils import delivery_window, local_windows

    monkeypatch.setattr(delivery_window, "local_now", lambda: moment)
    monkeypatch.setattr(local_windows, "local_now", lambda: moment)
    monkeypatch.setattr(order_schedule_service, "get_utc_now", lambda: moment.astimezone(timezone.utc))
    return moment


def _delivery(
    db,
    customer,
    driver,
    number,
    *,
    due,
    status=DeliveryStatus.FAILED,
    reason="customer_unavailable",
    attempts=1,
    failures=(),
    updated_at=None,
):
    """An order out for delivery on `due`, in the customer's 09:00-11:00 window, that a driver took to the door.

    A FAILED row carries what the failed branch writes: the reason, the attempt count, and the
    driver, who stays on the row. `failures` adds one FAILED history row per instant, as that
    branch writes one on each failed attempt. The re-dates between them are left out, because
    only FAILED rows date a failure. With none, the row looks like one from before history was
    kept. `updated_at` sets the row's last update: it is the list's newest-first key.
    """
    address = UserAddress(
        user_id=customer.id,
        title="Home",
        full_address=f"{number} Chilonzor 9, Tashkent",
        street_address="Chilonzor 9",
        city="Tashkent",
        latitude=41.29,
        longitude=69.21,
    )
    db.session.add(address)
    db.session.flush()
    order = Order(
        user_id=customer.id,
        order_number=number,
        status=OrderStatus.OUT_FOR_DELIVERY,
        subtotal=Decimal("45000.00"),
        delivery_fee=Decimal("0.00"),
        total_amount=Decimal("45000.00"),
        payment_method=PaymentMethod.CASH,
        delivery_address_id=address.id,
        delivery_date=due,
        delivery_window_start=CUSTOMER_WINDOW[0],
        delivery_window_end=CUSTOMER_WINDOW[1],
    )
    db.session.add(order)
    db.session.flush()
    failed = status == DeliveryStatus.FAILED
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver.id,
        status=status,
        failed_delivery_reason=reason if failed else None,
        delivery_attempts=attempts if failed else 0,
        scheduled_date=_utc(due, CUSTOMER_WINDOW[0]),
        scheduled_time_slot="09:00-11:00",
    )
    if updated_at is not None:
        delivery.updated_at = updated_at
    db.session.add(delivery)
    db.session.flush()
    for failed_at in failures:
        db.session.add(
            DeliveryStatusHistory(
                delivery_id=delivery.id,
                old_status=DeliveryStatus.ARRIVED,
                new_status=DeliveryStatus.FAILED,
                changed_by=driver.id,
                changed_at=failed_at,
                reason=reason,
            )
        )
    db.session.commit()
    return delivery


def _grocery_store(db, *, contract_last_day):
    """A grocery store on an AMOUNT contract whose last day is `contract_last_day` (R11)."""
    store = User(
        email="bahor-savdo@example.com",
        phone="+998901230055",
        password_hash=hash_password("StorePassword123!"),
        first_name="Bahor",
        last_name="Savdo",
        company_name="Bahor Savdo",
        user_type=UserType.ENTITY,
        entity_subtype=EntitySubtype.GROCERY_STORE,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(store)
    db.session.flush()
    contract = CorporateContract(
        user_id=store.id,
        contract_number=f"CTR-{uuid4().hex[:10]}",
        name="Bahor savdo",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(timezone.utc) - timedelta(days=30),
        end_date=_utc(contract_last_day, time(12, 0)),
        currency="UZS",
        is_active=True,
    )
    contract.tracking_mode = CorporateContractTrackingMode.AMOUNT
    db.session.add(contract)
    db.session.commit()
    return store


def _error_code(response):
    return response.get_json().get("error_code")


# --------------------------------------------------------------------------- #
# 1. The list and the one-row read: one row shape
# --------------------------------------------------------------------------- #


def test_each_row_names_the_driver_who_failed_not_the_admin_who_marked_it(
    client, db, admin_claim_headers, admin_user, operator_auth_headers, rostered_driver, sample_user, monkeypatch
):
    """F9, §3.5. The admin Delivery page marks a failure through the driver's own failed branch, so
    the FAILED history row's `changed_by` is the admin. The card still names the driver who attempted
    the door, because the failed branch keeps `delivery_person_id`. `failed_at` is that history row,
    published in UTC with its offset. The bounds are today through the booking horizon, the same ones
    the admin Reschedule modal is given."""
    today = _freeze_business_clock(monkeypatch, time(12, 0)).date()
    delivery = _delivery(db, sample_user, rostered_driver, "ORD-FDA-ADMIN", due=today, status=DeliveryStatus.ARRIVED)
    delivery_id, order_id = delivery.id, delivery.order_id

    # What the Delivery page sends for "Failed" (admin_ui/src/pages/Delivery.js handleUpdateSubmit).
    marked = client.put(
        ADMIN_DELIVERY.format(delivery_id),
        json={"status": "failed", "notes": "", "fail_reason": "wrong_address"},
        headers=admin_claim_headers,
    )
    assert marked.status_code == 200, marked.get_data(as_text=True)
    failure = DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id, new_status=DeliveryStatus.FAILED).one()
    assert failure.changed_by == admin_user.id

    response = client.get(FAILED_LIST, headers=operator_auth_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"] == {
        "items": [
            {
                "delivery_id": delivery_id,
                "order_id": order_id,
                "order_number": "ORD-FDA-ADMIN",
                "status": "failed",
                "customer_name": "Test User",
                "customer_phone": "+998901234567",
                "address": "ORD-FDA-ADMIN Chilonzor 9, Tashkent",
                "total_amount": 45000.0,
                "failed_delivery_reason": "wrong_address",
                "delivery_attempts": 1,
                "driver_name": "Rustam Driver",
                "failed_at": ensure_utc(failure.changed_at).isoformat(),
                "reschedule_min_date": today.isoformat(),
                "reschedule_max_date": (today + timedelta(days=MAX_SCHEDULE_HORIZON_DAYS)).isoformat(),
            }
        ],
        "total": 1,
    }


def test_a_delivery_below_the_page_gets_its_own_row_with_its_contract_cut_and_the_list_counts_it(
    client, db, operator_auth_headers, rostered_driver, sample_user, monkeypatch
):
    """F9, F12. The list shows the newest page and says how many there are in all. An alert can name
    a delivery further down, and the bot draws its dates from the one-row read, which has no cap.

    This one belongs to a grocery store whose AMOUNT contract ends in three days, so its last day is
    the contract's (R11), not the horizon's. It failed before history was kept, so `failed_at` falls
    back to the row's last update."""
    today = _freeze_business_clock(monkeypatch, time(12, 0)).date()
    for index in range(PAGE_SIZE):
        _delivery(db, sample_user, rostered_driver, f"ORD-FDA-{index:02d}", due=today)
    store = _grocery_store(db, contract_last_day=today + timedelta(days=3))
    last_touched = _utc(today - timedelta(days=3), time(9, 40))
    oldest = _delivery(
        db,
        store,
        rostered_driver,
        "ORD-FDA-STORE",
        due=today,
        reason="customer_refused",
        attempts=2,
        updated_at=last_touched,
    )
    oldest_id, oldest_order_id = oldest.id, oldest.order_id

    listed = client.get(FAILED_LIST, headers=operator_auth_headers)

    assert listed.status_code == 200, listed.get_data(as_text=True)
    page = listed.get_json()["data"]
    assert len(page["items"]) == PAGE_SIZE
    assert oldest_id not in {item["delivery_id"] for item in page["items"]}
    assert page["total"] == PAGE_SIZE + 1

    response = client.get(FAILED_ROW.format(oldest_id), headers=operator_auth_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"] == {
        "delivery": {
            "delivery_id": oldest_id,
            "order_id": oldest_order_id,
            "order_number": "ORD-FDA-STORE",
            "status": "failed",
            "customer_name": "Bahor Savdo",
            "customer_phone": "+998901230055",
            "address": "ORD-FDA-STORE Chilonzor 9, Tashkent",
            "total_amount": 45000.0,
            "failed_delivery_reason": "customer_refused",
            "delivery_attempts": 2,
            "driver_name": "Rustam Driver",
            "failed_at": last_touched.isoformat(),
            "reschedule_min_date": today.isoformat(),
            "reschedule_max_date": (today + timedelta(days=3)).isoformat(),
        }
    }


def test_a_delivery_that_failed_again_is_dated_by_its_latest_failure_in_both_reads(
    client, db, operator_auth_headers, rostered_driver, sample_user, monkeypatch
):
    """§3.5. `failed_at` is the latest FAILED history row, never the row's last update. Both
    callers of the one row builder publish it: the list loads it for its whole page, and the
    one-row read loads it for its one row. This delivery failed yesterday at 10:05, was re-dated,
    and failed again today at 10:20. The row was touched again at 11:30, so its last update is
    not when it failed. Both reads say 10:20 today, and they are the same row."""
    today = _freeze_business_clock(monkeypatch, time(12, 0)).date()
    latest_failure = _utc(today, time(10, 20))
    delivery = _delivery(
        db,
        sample_user,
        rostered_driver,
        "ORD-FDA-AGAIN",
        due=today,
        attempts=2,
        failures=(_utc(today - timedelta(days=1), time(10, 5)), latest_failure),
        updated_at=_utc(today, time(11, 30)),
    )
    delivery_id = delivery.id

    listed = client.get(FAILED_LIST, headers=operator_auth_headers)
    card = client.get(FAILED_ROW.format(delivery_id), headers=operator_auth_headers)

    assert listed.status_code == 200, listed.get_data(as_text=True)
    assert card.status_code == 200, card.get_data(as_text=True)
    row = card.get_json()["data"]["delivery"]
    assert (row["delivery_id"], row["delivery_attempts"], row["failed_at"]) == (
        delivery_id,
        2,
        latest_failure.isoformat(),
    )
    assert listed.get_json()["data"]["items"] == [row]


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_the_one_row_read_refuses_with_the_redispatchs_own_code(
    client, db, operator_auth_headers, rostered_driver, sample_user, monkeypatch, case
):
    """A date step is drawn only for a delivery the re-dispatch would still take. If the order was
    cancelled meanwhile, or a colleague re-dated the delivery first, the read answers the same code
    the POST would. The bot already maps that code to its own sentence (spec §3.5)."""
    today = _freeze_business_clock(monkeypatch, time(12, 0)).date()
    delivery = _delivery(db, sample_user, rostered_driver, f"ORD-FDA-{case}", due=today)
    _make_stale(db, delivery, case)

    response = client.get(FAILED_ROW.format(delivery.id), headers=operator_auth_headers)

    assert response.status_code == 400, response.get_data(as_text=True)
    assert _error_code(response) == REFUSALS[case]


def test_the_one_row_read_is_for_operators_and_names_an_unknown_delivery(
    client, db, operator_auth_headers, driver_auth_headers, rostered_driver, sample_user, monkeypatch
):
    today = _freeze_business_clock(monkeypatch, time(12, 0)).date()
    delivery = _delivery(db, sample_user, rostered_driver, "ORD-FDA-WHO", due=today)

    as_driver = client.get(FAILED_ROW.format(delivery.id), headers=driver_auth_headers)
    unknown = client.get(FAILED_ROW.format(987654), headers=operator_auth_headers)

    assert as_driver.status_code == 403, as_driver.get_data(as_text=True)
    assert _error_code(as_driver) == "STAFF_NO_ROLE"
    assert unknown.status_code == 404, unknown.get_data(as_text=True)
    assert _error_code(unknown) == "STAFF_DELIVERY_NOT_FOUND"


# --------------------------------------------------------------------------- #
# 2. The dated re-dispatch
# --------------------------------------------------------------------------- #


def test_a_today_button_drawn_before_midnight_and_tapped_after_it_is_refused_in_words(
    client, db, operator_auth_headers, rostered_driver, sample_user, enqueued, monkeypatch
):
    """Review Focus 1. The operator opens the date step at 23:50, so its Today button carries that
    day's date: the `reschedule_min_date` the backend published. They tap it at 00:30. That date is
    yesterday by now, and `reschedule` judges it on the locked row (F10). The answer is 400
    ORDER_RESCHEDULE_DATE_IN_PAST, which the bot maps to its own sentence, not a stack trace.
    Nothing moves and nothing is sent. The next read publishes the new Today, which is why the bot
    fetches the bounds again on every tap that draws dates."""
    drawn = _freeze_business_clock(monkeypatch, time(23, 50))
    delivery = _delivery(db, sample_user, rostered_driver, "ORD-FDA-MIDNIGHT", due=drawn.date())
    delivery_id = delivery.id
    card = client.get(FAILED_ROW.format(delivery_id), headers=operator_auth_headers)
    assert card.status_code == 200, card.get_data(as_text=True)
    today_button = card.get_json()["data"]["delivery"]["reschedule_min_date"]
    assert today_button == drawn.date().isoformat()
    before = _snapshot(delivery)

    tapped = _move_business_clock(monkeypatch, drawn + timedelta(minutes=40))
    response = client.post(
        STAFF_REDISPATCH.format(delivery_id), json={"delivery_date": today_button}, headers=operator_auth_headers
    )

    assert response.status_code == 400, response.get_data(as_text=True)
    assert _error_code(response) == "ORDER_RESCHEDULE_DATE_IN_PAST"
    assert BaseHandler.API_ERROR_CODE_KEY_MAP[_error_code(response)] == "staff.error.api.reschedule_date_in_past"
    db.session.expire_all()
    assert _snapshot(db.session.get(Delivery, delivery_id)) == before
    assert enqueued == []
    fresh = client.get(FAILED_ROW.format(delivery_id), headers=operator_auth_headers)
    assert fresh.get_json()["data"]["delivery"]["reschedule_min_date"] == tapped.date().isoformat()


def test_a_tapped_day_lands_on_that_day_and_the_reply_says_the_customer_is_told_in_telegram(
    client, db, operator_auth_headers, operator_user, rostered_driver, sample_user, enqueued, monkeypatch
):
    """F12, F14. The operator picks the day after tomorrow for a customer who uses the bot.
    - The delivery is held until that day's 08:00 shift start (R5).
    - The order carries the new date, with the customer's window unchanged.
    - The customer has been waiting on a new date since the attempt failed, so they are told, in
      Telegram.
    The response carries that decision as `reschedule` made it under its locks. Once the row has
    left `failed`, asking again would answer "not told"."""
    today = _freeze_business_clock(monkeypatch, time(12, 0)).date()
    target = today + timedelta(days=2)
    sample_user.telegram_id = "700100901"
    sample_user.is_bot_active = True
    db.session.commit()
    delivery = _delivery(db, sample_user, rostered_driver, "ORD-FDA-TG", due=today)
    delivery_id, order_id = delivery.id, delivery.order_id

    response = client.post(
        STAFF_REDISPATCH.format(delivery_id), json={"delivery_date": target.isoformat()}, headers=operator_auth_headers
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"] == {
        "delivery_id": delivery_id,
        "status": "rescheduled",
        "message": "Delivery rescheduled",
        "delivery_date": target.isoformat(),
        "release_at": _published_release(target),
        "notifies_customer": True,
        "customer_channel": "telegram",
    }
    db.session.expire_all()
    order = db.session.get(Order, order_id)
    assert (order.delivery_date, order.delivery_window_start, order.delivery_window_end, order.status) == (
        target,
        *CUSTOMER_WINDOW,
        OrderStatus.CONFIRMED,
    )
    row = db.session.get(Delivery, delivery_id)
    assert (row.status, row.delivery_person_id) == (DeliveryStatus.RESCHEDULED, None)
    last = _last_history(delivery_id)
    assert (last.old_status, last.new_status, last.changed_by) == (
        DeliveryStatus.FAILED,
        DeliveryStatus.RESCHEDULED,
        operator_user.id,
    )
    # Held, so nothing is offered to drivers. The one publish is the notice the reply promised.
    assert enqueued == [(send_delivery_rescheduled_notification_task.name, (order_id,), {})]


def test_a_customer_created_in_the_staff_bot_is_owed_the_notice_but_has_no_channel(
    client, db, operator_auth_headers, rostered_driver, enqueued, monkeypatch
):
    """Spec §3.5, the normal case for phone orders. The operator created the customer in the staff
    bot, which gives them a phone and nothing else: no email and no Telegram. The notice is still
    owed (F14) and still queued. No channel can carry it, so `customer_channel` is null, and the bot
    tells the operator to contact the customer themselves."""
    today = _freeze_business_clock(monkeypatch, time(12, 0)).date()
    tomorrow = today + timedelta(days=1)
    # The body the staff bot's create-user flow posts (staff_bot/handlers/operator/create_user.py).
    created = client.post(
        OPERATOR_USERS,
        json={"phone": "+998901230077", "first_name": "Dilnoza", "last_name": None, "preferred_language": "uz"},
        headers=operator_auth_headers,
    )
    assert created.status_code == 201, created.get_data(as_text=True)
    customer = db.session.get(User, created.get_json()["data"]["id"])
    assert (customer.email, customer.telegram_id) == (None, None)
    delivery = _delivery(db, customer, rostered_driver, "ORD-FDA-PHONE", due=today)
    delivery_id, order_id = delivery.id, delivery.order_id
    enqueued.clear()  # only what the re-dispatch publishes

    response = client.post(
        STAFF_REDISPATCH.format(delivery_id), json={"delivery_date": tomorrow.isoformat()}, headers=operator_auth_headers
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"] == {
        "delivery_id": delivery_id,
        "status": "rescheduled",
        "message": "Delivery rescheduled",
        "delivery_date": tomorrow.isoformat(),
        "release_at": _published_release(tomorrow),
        "notifies_customer": True,
        "customer_channel": None,
    }
    assert enqueued == [(send_delivery_rescheduled_notification_task.name, (order_id,), {})]


def test_with_no_body_it_is_today_as_a_bot_from_before_the_date_step_sends_it(
    client, db, operator_auth_headers, rostered_driver, sample_user, monkeypatch
):
    """F12 and rolling safety (spec §9). A bot from before the date step posts no body at all. A
    missing date means local today. After today's 08:00 release the delivery goes straight back to
    the pool, so there is no release instant to quote. `sample_user` has no bot, so the notice goes
    by email."""
    today = _freeze_business_clock(monkeypatch, time(12, 0)).date()
    delivery = _delivery(db, sample_user, rostered_driver, "ORD-FDA-NOBODY", due=today - timedelta(days=1))
    delivery_id, order_id = delivery.id, delivery.order_id

    response = client.post(STAFF_REDISPATCH.format(delivery_id), headers=operator_auth_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"] == {
        "delivery_id": delivery_id,
        "status": "scheduled",
        "message": "Delivery rescheduled",
        "delivery_date": today.isoformat(),
        "release_at": None,
        "notifies_customer": True,
        "customer_channel": "email",
    }
    db.session.expire_all()
    assert db.session.get(Order, order_id).delivery_date == today


def test_a_second_tap_on_the_same_card_is_refused_and_moves_nothing(
    client, db, operator_auth_headers, rostered_driver, sample_user, enqueued, monkeypatch
):
    """The first tap re-dated the delivery, so it no longer awaits a new date. The second tap, and
    the date step the bot would draw for it, are refused with STAFF_DELIVERY_NOT_REDISPATCHABLE. The
    first landing stands and the customer is not told twice."""
    today = _freeze_business_clock(monkeypatch, time(12, 0)).date()
    tomorrow = (today + timedelta(days=1)).isoformat()
    delivery = _delivery(db, sample_user, rostered_driver, "ORD-FDA-TWICE", due=today)
    delivery_id = delivery.id
    first = client.post(
        STAFF_REDISPATCH.format(delivery_id), json={"delivery_date": tomorrow}, headers=operator_auth_headers
    )
    assert first.status_code == 200, first.get_data(as_text=True)
    db.session.expire_all()
    before = _snapshot(db.session.get(Delivery, delivery_id))
    enqueued.clear()

    second = client.post(
        STAFF_REDISPATCH.format(delivery_id), json={"delivery_date": tomorrow}, headers=operator_auth_headers
    )
    card = client.get(FAILED_ROW.format(delivery_id), headers=operator_auth_headers)

    assert second.status_code == 400, second.get_data(as_text=True)
    assert _error_code(second) == "STAFF_DELIVERY_NOT_REDISPATCHABLE"
    assert card.status_code == 400, card.get_data(as_text=True)
    assert _error_code(card) == "STAFF_DELIVERY_NOT_REDISPATCHABLE"
    db.session.expire_all()
    assert _snapshot(db.session.get(Delivery, delivery_id)) == before
    assert enqueued == []


@pytest.mark.parametrize(
    "raw_date",
    ["2026-13-01", "30.09.2026", "tomorrow"],
    ids=["month-13", "day-first", "a-word"],
)
def test_a_date_that_does_not_parse_is_refused_by_name_and_moves_nothing(
    client, db, operator_auth_headers, rostered_driver, sample_user, enqueued, monkeypatch, raw_date
):
    """F12. The bot builds the date from the bounds the backend published, so a date that does not
    parse is a damaged or hand-made callback. It is never a reason to fall back to today. It gets
    its own code, which the bot maps to its own sentence, and nothing moves."""
    today = _freeze_business_clock(monkeypatch, time(12, 0)).date()
    delivery = _delivery(db, sample_user, rostered_driver, "ORD-FDA-BADDATE", due=today)
    delivery_id = delivery.id
    before = _snapshot(delivery)

    response = client.post(
        STAFF_REDISPATCH.format(delivery_id), json={"delivery_date": raw_date}, headers=operator_auth_headers
    )

    assert response.status_code == 400, response.get_data(as_text=True)
    assert _error_code(response) == "STAFF_REDISPATCH_DATE_INVALID"
    assert BaseHandler.API_ERROR_CODE_KEY_MAP.get("STAFF_REDISPATCH_DATE_INVALID") == (
        "staff.error.api.redispatch_date_invalid"
    )
    db.session.expire_all()
    assert _snapshot(db.session.get(Delivery, delivery_id)) == before
    assert enqueued == []


def test_a_non_string_reason_is_refused_uncoded_and_moves_nothing(
    client, db, operator_auth_headers, rostered_driver, sample_user, enqueued, monkeypatch
):
    """Controller ruling (Task 8 review): the route used to run `(payload.get("reason") or
    "").strip()` itself, so a non-string `reason` (e.g. a JSON number) was a 500. It is now refused
    the same way `PATCH /admin/orders/<id>/schedule` refuses one: an uncoded 400, no `error_code`.
    Nothing moves."""
    today = _freeze_business_clock(monkeypatch, time(12, 0)).date()
    delivery = _delivery(db, sample_user, rostered_driver, "ORD-FDA-REASONTYPE", due=today)
    delivery_id = delivery.id
    before = _snapshot(delivery)

    response = client.post(
        STAFF_REDISPATCH.format(delivery_id), json={"reason": 12345}, headers=operator_auth_headers
    )

    assert response.status_code == 400, response.get_data(as_text=True)
    assert "reason must be a string" in response.get_data(as_text=True)
    assert _error_code(response) is None
    db.session.expire_all()
    assert _snapshot(db.session.get(Delivery, delivery_id)) == before
    assert enqueued == []
