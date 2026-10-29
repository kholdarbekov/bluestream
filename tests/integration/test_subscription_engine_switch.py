"""The engine switch of the single-frequency change (plan 2026-10-24 Task 2; spec 2026-10-23 §2.7-2.9,
§4.3, §4.6, §5 items 1, 2, 5 and 6, §9).

Every subscription order is created on ONE frequency, dated and windowed to the customer's day and
slot, the lead (one day) before its delivery, and held until then. The clock is `next_delivery_date`.
A refusal or a date that already passed is never back-dated: that delivery is skipped, the clock
moves on and a notice is owed (recorded as a `SubscriptionLog` row and handed to
`subscription_notices.deliver` only after the commit; plan Task 3 sends it). Skip, pause, resume,
cancel and changes follow one rule for customers and admins, and a paid upcoming order always stays.

Driven through the real endpoints with the bodies the clients send, and the daily job with a frozen
business clock. Only external I/O is mocked: the Celery publish and the route notifications.
"""

from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import create_autospec, patch
from zoneinfo import ZoneInfo

import pytest
import redis
from flask_jwt_extended import create_access_token
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from werkzeug.exceptions import MethodNotAllowed, NotFound

from business_app.models.delivery import DeliveryTimeSlot
from business_app.models.order import Order
from business_app.models.subscription import Subscription, SubscriptionItem, SubscriptionLog
from business_app.services import subscription_notices, subscription_schedule
from business_app.services.inventory_service import get_inventory_service
from business_app.services.order_schedule_service import OrderScheduleService
from business_app.tasks.subscription_tasks import create_subscription_delivery_task, process_daily_subscription_billing
from business_app.utils.local_windows import local_day_bounds
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus, SubscriptionFrequency, SubscriptionStatus
from tests.integration.sales_pay_builders import freeze_local_now
from tests.integration.test_subscription_action_guards import _state

pytestmark = pytest.mark.integration

TASHKENT = ZoneInfo(DISPLAY_TIMEZONE)
BASE = "/api/v1/subscriptions"
ADMIN_BASE = "/api/v1/admin/subscriptions"
MONDAY, WEDNESDAY, THURSDAY, FRIDAY = 0, 2, 3, 4
WEEK = timedelta(days=7)


# --------------------------------------------------------------------------- fixtures and builders


@pytest.fixture(autouse=True)
def delay():
    """The Celery publish (external I/O). Yields the `.delay` mock so tests can read who was enqueued."""
    with patch.object(create_subscription_delivery_task, "delay") as enqueued, patch(
        "business_app.api.subscriptions.get_notification_service"
    ):
        yield enqueued


class _Clock:
    def __init__(self, monkeypatch):
        self._monkeypatch = monkeypatch

    def at(self, day: date, hour: int = 9, minute: int = 0):
        return freeze_local_now(self._monkeypatch, datetime.combine(day, time(hour, minute), tzinfo=TASHKENT))


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    return _Clock(monkeypatch)


@pytest.fixture
def monday(clock) -> date:
    """The first Monday on or after the real local date, 10:00 Tashkent: `start_date` still defaults to
    the wall clock and must not fall after the frozen one."""
    today = datetime.now(TASHKENT).date()
    day = today + timedelta(days=(7 - today.weekday()) % 7)
    clock.at(day, 10)
    return day


@pytest.fixture
def morning_slot(db):
    slot = DeliveryTimeSlot(
        name="Morning", start_time="10:00", end_time="12:00", is_active=True, available_days=[0, 1, 2, 3, 4, 5, 6]
    )
    db.session.add(slot)
    db.session.commit()
    return slot


@pytest.fixture
def admin_headers(app, admin_user):
    """`validate_admin_action` reads the role claim (CLAUDE.md)."""
    with app.app_context():
        token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture
def notices(db, monkeypatch):
    """What reaches the Task 3 hook, and whether anything was still uncommitted when it did."""
    seen = []
    real = subscription_notices.deliver

    def spy(log):
        seen.append(
            {
                "action": log.action,
                "subscription_id": log.subscription_id,
                "extra_data": dict(log.extra_data or {}),
                "uncommitted": bool(db.session.new or db.session.dirty),
            }
        )
        return real(log)

    monkeypatch.setattr(subscription_notices, "deliver", spy)
    return seen


def _create_body(address_id, product_id, **fields):
    """The body the bot and the web builder send once Tasks 4-5 land: ONE `frequency`, the day, the slot."""
    body = {
        "name": "Home water",
        "frequency": "weekly",
        "delivery_address_id": address_id,
        "payment_method": "cash",
        "items": [{"product_id": product_id, "quantity": 2}],
        "auto_renew": True,
    }
    body.update(fields)
    return body


def _create(client, headers, address_id, product_id, **fields) -> int:
    resp = client.post(f"{BASE}/", headers=headers, json=_create_body(address_id, product_id, **fields))
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["data"]["subscription"]["id"]


def _model_subscription(
    db, user, address, product, *, frequency, next_delivery_date, day_of_week=None, day_of_month=None, slot=None
) -> int:
    """A subscription row as the migration leaves one (only the new columns are written)."""
    subscription = Subscription(
        user_id=user.id,
        name="Water",
        status=SubscriptionStatus.ACTIVE,
        frequency=frequency,
        delivery_day_of_week=day_of_week,
        delivery_day_of_month=day_of_month,
        delivery_time_slot_id=slot.id if slot is not None else None,
        delivery_address_id=address.id,
        payment_method=PaymentMethod.CASH,
        billing_amount=Decimal("30000.00"),
        start_date=datetime.now(UTC),
        next_delivery_date=next_delivery_date,
    )
    db.session.add(subscription)
    db.session.flush()
    db.session.add(
        SubscriptionItem(
            subscription_id=subscription.id,
            product_id=product.id,
            quantity=2,
            unit_price=product.base_price,
            total_price=product.base_price * 2,
        )
    )
    db.session.commit()
    return subscription.id


def _row(db, subscription_id) -> Subscription:
    db.session.expire_all()
    return db.session.get(Subscription, subscription_id)


def _orders(db, subscription_id):
    db.session.expire_all()
    return Order.query.filter_by(subscription_id=subscription_id).order_by(Order.delivery_date, Order.id).all()


def _logs(db, subscription_id, action):
    db.session.expire_all()
    return (
        SubscriptionLog.query.filter_by(subscription_id=subscription_id, action=action)
        .order_by(SubscriptionLog.id)
        .all()
    )


def _mark_paid(db, order_id):
    """What the Click webhook leaves behind (the gateway itself is external I/O)."""
    order = db.session.get(Order, order_id)
    order.is_paid = True
    order.payment.status = PaymentStatus.COMPLETED
    db.session.commit()


def _deliver_once_before(db, user, address, product):
    """A returning customer: one delivered order makes `create_order` confirm cash orders at once."""
    db.session.add(
        Order(
            user_id=user.id,
            status=OrderStatus.DELIVERED,
            subtotal=Decimal("30000.00"),
            total_amount=Decimal("30000.00"),
            delivery_address_id=address.id,
            payment_method=PaymentMethod.CASH,
        )
    )
    db.session.commit()


def _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot, **fields) -> int:
    return _create(
        client,
        auth_headers,
        user_address.id,
        sample_product.id,
        frequency="weekly",
        delivery_day_of_week=THURSDAY,
        delivery_time_slot_id=morning_slot.id,
        **fields,
    )


def _order_ref(order):
    return {"id": order.id, "order_number": order.order_number, "delivery_date": order.delivery_date.isoformat()}


def _ids(refs):
    """Returned order refs without their display labels (tests/integration/test_subscription_page_data.py pins
    those)."""
    return [{key: ref[key] for key in ("id", "order_number", "delivery_date", "reason_code") if key in ref} for ref in refs]


# --------------------------------------------------------------------------- §5.1 the daily job


class TestWeeklyThursdayWithASlot:
    """§5.1: weekly on Thursday, slot 10:00-12:00, created on a Monday."""

    def test_the_order_is_created_wednesday_dated_thursday_windowed_and_held(
        self, client, db, auth_headers, sample_user, user_address, sample_product, morning_slot, clock, monday, delay
    ):
        _deliver_once_before(db, sample_user, user_address, sample_product)
        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)
        thursday = monday + timedelta(days=3)

        row = _row(db, sub_id)
        assert row.next_delivery_date == thursday
        delay.assert_not_called()  # Thursday is outside Monday's lead window: no order at creation

        for day in (monday, monday + timedelta(days=1)):
            clock.at(day)
            process_daily_subscription_billing()
            assert _orders(db, sub_id) == []

        clock.at(monday + timedelta(days=2))  # Wednesday 09:00
        process_daily_subscription_billing()

        [order] = _orders(db, sub_id)
        assert (order.delivery_date, order.delivery_window_start, order.delivery_window_end) == (
            thursday,
            time(10, 0),
            time(12, 0),
        )
        assert order.status is OrderStatus.CONFIRMED  # a returning customer's cash order...
        assert order.delivery is None  # ...still has no Delivery: drivers cannot see it before Thursday
        assert OrderScheduleService.is_awaiting_release(order) is True
        row = _row(db, sub_id)
        assert (row.last_delivery_date, row.next_delivery_date) == (thursday, thursday + WEEK)

    def test_running_the_job_twice_creates_no_second_order(
        self, client, db, auth_headers, user_address, sample_product, morning_slot, clock, monday
    ):
        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)
        clock.at(monday + timedelta(days=2))

        process_daily_subscription_billing()
        process_daily_subscription_billing()

        assert len(_orders(db, sub_id)) == 1

    def test_a_live_order_for_the_date_blocks_a_second_one_and_the_clock_heals(
        self, client, db, auth_headers, user_address, sample_product, morning_slot, clock, monday
    ):
        """The crash window: the order committed, the advance did not."""
        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)
        thursday = monday + timedelta(days=3)
        clock.at(monday + timedelta(days=2))
        process_daily_subscription_billing()
        row = _row(db, sub_id)
        row.next_delivery_date = thursday
        db.session.commit()

        process_daily_subscription_billing()

        assert [o.delivery_date for o in _orders(db, sub_id)] == [thursday]
        assert _row(db, sub_id).next_delivery_date == thursday + WEEK

    def test_a_cancelled_order_for_the_date_does_not_block(
        self, client, db, auth_headers, user_address, sample_product, morning_slot, clock, monday
    ):
        """Only a non-cancelled order guards the date (§4.3); the clock is what stops a re-creation."""
        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)
        thursday = monday + timedelta(days=3)
        clock.at(monday + timedelta(days=2))
        process_daily_subscription_billing()
        [first] = _orders(db, sub_id)
        first.status = OrderStatus.CANCELLED
        db.session.commit()
        _row(db, sub_id).next_delivery_date = thursday
        db.session.commit()

        process_daily_subscription_billing()

        orders = _orders(db, sub_id)
        assert [(o.delivery_date, o.status) for o in orders] == [
            (thursday, OrderStatus.CANCELLED),
            (thursday, OrderStatus.PENDING),
        ]

    def test_the_created_order_is_recorded_and_handed_to_the_notice_hook_after_commit(
        self, client, db, auth_headers, user_address, sample_product, morning_slot, clock, monday, notices
    ):
        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)
        clock.at(monday + timedelta(days=2))

        process_daily_subscription_billing()

        [order] = _orders(db, sub_id)
        expected = {
            "order_id": order.id,
            "order_number": order.order_number,
            "delivery_date": (monday + timedelta(days=3)).isoformat(),
        }
        [log] = _logs(db, sub_id, subscription_notices.ORDER_CREATED)
        assert log.extra_data == expected
        assert notices == [
            {
                "action": subscription_notices.ORDER_CREATED,
                "subscription_id": sub_id,
                "extra_data": expected,
                "uncommitted": False,
            }
        ]


class TestFirstOrderAtCreation:
    def test_a_first_delivery_inside_the_lead_is_created_now(
        self, client, db, auth_headers, user_address, sample_product, monday, delay
    ):
        """No day chosen: the first delivery is tomorrow, inside the lead, so creation enqueues it."""
        sub_id = _create(client, auth_headers, user_address.id, sample_product.id)

        assert _row(db, sub_id).next_delivery_date == monday + timedelta(days=1)
        delay.assert_called_once_with(sub_id)

        create_subscription_delivery_task(sub_id)  # what the worker runs

        [order] = _orders(db, sub_id)
        assert order.delivery_date == monday + timedelta(days=1)
        assert (order.delivery_window_start, order.delivery_window_end) == (None, None)  # no slot chosen
        assert _row(db, sub_id).next_delivery_date == monday + timedelta(days=8)


# --------------------------------------------------------------------------- §5.2 cadences through the engine


class TestCadencesThroughTheJob:
    def _run_on(self, clock, *days):
        for day in days:
            clock.at(day)
            process_daily_subscription_billing()

    def test_monthly_on_the_31st_lands_on_feb_28_and_apr_30(self, db, clock, sample_user, user_address, sample_product):
        sub_id = _model_subscription(
            db,
            sample_user,
            user_address,
            sample_product,
            frequency=SubscriptionFrequency.MONTHLY,
            day_of_month=31,
            next_delivery_date=date(2027, 1, 31),
        )

        self._run_on(clock, date(2027, 1, 30), date(2027, 2, 27), date(2027, 3, 30), date(2027, 4, 29))

        assert [o.delivery_date for o in _orders(db, sub_id)] == [
            date(2027, 1, 31),
            date(2027, 2, 28),
            date(2027, 3, 31),
            date(2027, 4, 30),
        ]
        assert _row(db, sub_id).next_delivery_date == date(2027, 5, 31)

    def test_biweekly_keeps_its_parity(self, db, clock, sample_user, user_address, sample_product):
        sub_id = _model_subscription(
            db,
            sample_user,
            user_address,
            sample_product,
            frequency=SubscriptionFrequency.BIWEEKLY,
            day_of_week=THURSDAY,
            next_delivery_date=date(2027, 1, 7),
        )

        self._run_on(clock, date(2027, 1, 6), date(2027, 1, 13), date(2027, 1, 20))

        assert [o.delivery_date for o in _orders(db, sub_id)] == [date(2027, 1, 7), date(2027, 1, 21)]

    def test_daily_skips_the_days_the_slot_is_not_offered(self, db, clock, sample_user, user_address, sample_product):
        slot = DeliveryTimeSlot(
            name="MWF", start_time="14:00", end_time="16:00", is_active=True, available_days=[MONDAY, WEDNESDAY, FRIDAY]
        )
        db.session.add(slot)
        db.session.commit()
        sub_id = _model_subscription(
            db,
            sample_user,
            user_address,
            sample_product,
            frequency=SubscriptionFrequency.DAILY,
            slot=slot,
            next_delivery_date=date(2027, 1, 4),  # a Monday
        )

        self._run_on(clock, date(2027, 1, 3), date(2027, 1, 4), date(2027, 1, 5))

        assert [(o.delivery_date, o.delivery_window_start) for o in _orders(db, sub_id)] == [
            (date(2027, 1, 4), time(14, 0)),
            (date(2027, 1, 6), time(14, 0)),
        ]
        assert _row(db, sub_id).next_delivery_date == date(2027, 1, 8)


# --------------------------------------------------------------------------- §2.8 refusals and passed dates


class TestNeverBackDated:
    def test_a_passed_date_is_skipped_and_the_clock_moves_to_the_next_scheduled_date(
        self, db, clock, sample_user, user_address, sample_product, notices
    ):
        sub_id = _model_subscription(
            db,
            sample_user,
            user_address,
            sample_product,
            frequency=SubscriptionFrequency.WEEKLY,
            day_of_week=THURSDAY,
            next_delivery_date=date(2027, 1, 7),
        )
        clock.at(date(2027, 1, 9))  # Saturday: Thursday passed during downtime

        process_daily_subscription_billing()

        assert _orders(db, sub_id) == []
        assert _row(db, sub_id).next_delivery_date == date(2027, 1, 14)
        expected = {
            "skipped_by": "system",
            "reason_code": subscription_notices.SKIP_DATE_PASSED,
            "skipped_dates": ["2027-01-07"],
            "next_delivery_date": "2027-01-14",
            "detail": None,
        }
        [log] = _logs(db, sub_id, subscription_notices.DELIVERY_SKIPPED)
        assert log.extra_data == expected
        assert notices == [
            {
                "action": subscription_notices.DELIVERY_SKIPPED,
                "subscription_id": sub_id,
                "extra_data": expected,
                "uncommitted": False,
            }
        ]

        clock.at(date(2027, 1, 13))
        process_daily_subscription_billing()
        assert [o.delivery_date for o in _orders(db, sub_id)] == [date(2027, 1, 14)]

    def test_after_downtime_every_passed_date_is_skipped_at_once_and_the_due_ones_are_created(
        self, db, clock, sample_user, user_address, sample_product
    ):
        sub_id = _model_subscription(
            db,
            sample_user,
            user_address,
            sample_product,
            frequency=SubscriptionFrequency.DAILY,
            next_delivery_date=date(2027, 1, 5),
        )
        clock.at(date(2027, 1, 7))

        process_daily_subscription_billing()

        orders = _orders(db, sub_id)
        assert [o.delivery_date for o in orders] == [date(2027, 1, 7), date(2027, 1, 8)]
        assert all(o.delivery_date >= date(2027, 1, 7) for o in orders)  # nothing back-dated
        [log] = _logs(db, sub_id, subscription_notices.DELIVERY_SKIPPED)
        assert log.extra_data["skipped_dates"] == ["2027-01-05", "2027-01-06"]
        assert _row(db, sub_id).next_delivery_date == date(2027, 1, 9)

    def test_a_refused_order_skips_that_delivery_and_moves_on(
        self, db, clock, sample_user, user_address, sample_product, notices
    ):
        sub_id = _model_subscription(
            db,
            sample_user,
            user_address,
            sample_product,
            frequency=SubscriptionFrequency.WEEKLY,
            day_of_week=THURSDAY,
            next_delivery_date=date(2027, 1, 7),
        )
        sample_user.phone = None  # `OrderService.customer_order_refusal`: no phone, no order
        db.session.commit()
        clock.at(date(2027, 1, 6))

        process_daily_subscription_billing()
        process_daily_subscription_billing()  # no daily retry: the next run has nothing due

        assert _orders(db, sub_id) == []
        row = _row(db, sub_id)
        assert (row.status, row.next_delivery_date) == (SubscriptionStatus.ACTIVE, date(2027, 1, 14))
        [log] = _logs(db, sub_id, subscription_notices.DELIVERY_SKIPPED)
        assert log.extra_data == {
            "skipped_by": "system",
            "reason_code": subscription_notices.SKIP_CUSTOMER_BARRED,
            "skipped_dates": ["2027-01-07"],
            "next_delivery_date": "2027-01-14",
            "detail": "Phone number is required to place an order. Please update your profile.",
        }
        assert [n["action"] for n in notices] == [subscription_notices.DELIVERY_SKIPPED]


def _redis_misconf():
    """Redis refusing writes (`MISCONF`: its disk is full), so the reservation write inside `create_order`
    fails. `create_order` reports it as a ValidationError with no error code."""
    client = create_autospec(redis.Redis, instance=True)
    client.keys.return_value = []
    client.setex.side_effect = redis.exceptions.ResponseError(
        "MISCONF Redis is configured to save RDB snapshots, but it's currently unable to persist to disk."
    )
    return patch.object(get_inventory_service(), "redis_client", client)


@contextmanager
def _db_deadlock():
    """A deadlock on the order INSERT inside `create_order`'s transaction, which `create_order` reports as a
    ValidationError with no error code. It is injected at the INSERT, before any audit write: on the test
    SQLite (one shared connection) the audit logger's separate-session commit would also commit the
    half-made order, which Postgres never does."""

    def deadlock(mapper, connection, target):
        raise OperationalError("INSERT INTO orders ...", {}, Exception("deadlock detected"))

    event.listen(Order, "before_insert", deadlock)
    try:
        yield
    finally:
        event.remove(Order, "before_insert", deadlock)


_OUTAGES = pytest.mark.parametrize("outage", [_redis_misconf, _db_deadlock], ids=["redis-misconf", "db-deadlock"])


class TestATemporaryFailureIsNotARefusal:
    """§2.8 skips a delivery only for its named refusals: out of stock, under the minimum, customer barred,
    over the COD debt cap, the date passed. Any other `create_order` failure, such as a Redis or database
    outage during the 09:00 run, moves nothing and owes no notice. The next run tries again, and the date
    is skipped as passed only when it has passed. Nothing is back-dated."""

    def _thursday(self, db, sample_user, user_address, sample_product) -> int:
        return _model_subscription(
            db,
            sample_user,
            user_address,
            sample_product,
            frequency=SubscriptionFrequency.WEEKLY,
            day_of_week=THURSDAY,
            next_delivery_date=date(2027, 1, 7),
        )

    @_OUTAGES
    def test_the_delivery_is_left_for_the_next_run(
        self, db, clock, sample_user, user_address, sample_product, notices, outage
    ):
        sub_id = self._thursday(db, sample_user, user_address, sample_product)
        clock.at(date(2027, 1, 6))  # Wednesday: Thursday's order is due

        with outage():
            summary = process_daily_subscription_billing()

        assert (summary["successful"], summary["skipped"], summary["failed"]) == (0, 0, 1)
        assert _orders(db, sub_id) == []
        row = _row(db, sub_id)
        assert (row.status, row.next_delivery_date, row.last_delivery_date) == (
            SubscriptionStatus.ACTIVE,
            date(2027, 1, 7),
            None,
        )
        assert _logs(db, sub_id, subscription_notices.DELIVERY_SKIPPED) == []
        assert notices == []

        clock.at(date(2027, 1, 7))  # the next run, the outage over: Thursday's order, dated Thursday
        process_daily_subscription_billing()

        assert [o.delivery_date for o in _orders(db, sub_id)] == [date(2027, 1, 7)]
        assert _row(db, sub_id).next_delivery_date == date(2027, 1, 14)
        assert [n["action"] for n in notices] == [subscription_notices.ORDER_CREATED]

    @_OUTAGES
    def test_an_outage_that_outlasts_the_date_skips_it_as_passed(
        self, db, clock, sample_user, user_address, sample_product, notices, outage
    ):
        sub_id = self._thursday(db, sample_user, user_address, sample_product)
        with outage():
            for day in (date(2027, 1, 6), date(2027, 1, 7)):  # Wednesday's and Thursday's runs both fail
                clock.at(day)
                process_daily_subscription_billing()
        assert notices == []

        clock.at(date(2027, 1, 8))  # Friday: Thursday has passed
        process_daily_subscription_billing()

        assert _orders(db, sub_id) == []
        assert _row(db, sub_id).next_delivery_date == date(2027, 1, 14)
        expected = {
            "skipped_by": "system",
            "reason_code": subscription_notices.SKIP_DATE_PASSED,
            "skipped_dates": ["2027-01-07"],
            "next_delivery_date": "2027-01-14",
            "detail": None,
        }
        [log] = _logs(db, sub_id, subscription_notices.DELIVERY_SKIPPED)
        assert log.extra_data == expected
        assert notices == [
            {
                "action": subscription_notices.DELIVERY_SKIPPED,
                "subscription_id": sub_id,
                "extra_data": expected,
                "uncommitted": False,
            }
        ]

    def test_admin_create_next_order_now_reports_it_and_moves_nothing(
        self, client, db, auth_headers, admin_headers, user_address, sample_product, morning_slot, monday, notices
    ):
        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)
        thursday = monday + timedelta(days=3)

        with _redis_misconf():
            resp = client.post(f"{ADMIN_BASE}/{sub_id}/billing/process", headers=admin_headers, json={})

        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["data"]["success"] is False
        assert body["message"].startswith("No order created: Inventory reservation failed: Reservation failed: MISCONF")
        assert _orders(db, sub_id) == []
        assert _row(db, sub_id).next_delivery_date == thursday
        assert _logs(db, sub_id, subscription_notices.DELIVERY_SKIPPED) == []
        assert notices == []


# --------------------------------------------------------------------------- §5.5 actions, customer and admin


@pytest.fixture
def thursday_order(client, db, auth_headers, user_address, sample_product, morning_slot, clock, monday):
    """A weekly-Thursday subscription whose Thursday order the Wednesday run has created."""

    def build(payment_method="cash", paid=False):
        sub_id = _weekly_thursday(
            client, auth_headers, user_address, sample_product, morning_slot, payment_method=payment_method
        )
        clock.at(monday + timedelta(days=2), 10)
        process_daily_subscription_billing()
        [order] = _orders(db, sub_id)
        if paid:
            _mark_paid(db, order.id)
        return sub_id, order.id

    return build


class TestSkip:
    def test_with_no_order_created_the_clock_moves_one_cycle(
        self, client, db, auth_headers, user_address, sample_product, morning_slot, monday
    ):
        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)
        thursday = monday + timedelta(days=3)

        resp = client.post(
            f"{BASE}/{sub_id}/skip-next-delivery", headers=auth_headers, json={"expected_date": thursday.isoformat()}
        )

        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert (data["original_delivery_date"], data["new_next_delivery_date"]) == (
            thursday.isoformat(),
            (thursday + WEEK).isoformat(),
        )
        assert (_ids(data["cancelled_orders"]), _ids(data["kept_orders"])) == ([], [])
        assert _row(db, sub_id).next_delivery_date == thursday + WEEK
        assert _orders(db, sub_id) == []

    def test_an_unpaid_created_order_is_cancelled_and_the_clock_stays(
        self, client, db, auth_headers, monday, thursday_order
    ):
        sub_id, order_id = thursday_order()
        thursday = monday + timedelta(days=3)

        resp = client.post(
            f"{BASE}/{sub_id}/skip-next-delivery", headers=auth_headers, json={"expected_date": thursday.isoformat()}
        )

        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        [order] = _orders(db, sub_id)
        assert order.status is OrderStatus.CANCELLED
        assert (data["original_delivery_date"], data["new_next_delivery_date"]) == (
            thursday.isoformat(),
            (thursday + WEEK).isoformat(),
        )
        assert _ids(data["cancelled_orders"]) == [_order_ref(order)]
        assert _row(db, sub_id).next_delivery_date == thursday + WEEK  # already one cycle on since creation

    def test_a_paid_created_order_stays_and_the_following_delivery_is_skipped(
        self, client, db, auth_headers, monday, thursday_order, notices
    ):
        sub_id, order_id = thursday_order(payment_method="click", paid=True)
        thursday = monday + timedelta(days=3)

        # The page names the delivery after the paid one (spec 2026-10-26 §12).
        resp = client.post(
            f"{BASE}/{sub_id}/skip-next-delivery",
            headers=auth_headers,
            json={"expected_date": (thursday + WEEK).isoformat()},
        )

        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        [order] = _orders(db, sub_id)
        assert order.status is not OrderStatus.CANCELLED
        assert (data["original_delivery_date"], data["new_next_delivery_date"]) == (
            (thursday + WEEK).isoformat(),
            thursday.isoformat(),  # the paid order is still the upcoming delivery
        )
        assert _ids(data["kept_orders"]) == [{**_order_ref(order), "reason_code": "PAID"}]
        assert _row(db, sub_id).next_delivery_date == thursday + 2 * WEEK
        kept = {
            "order_id": order.id,
            "order_number": order.order_number,
            "delivery_date": thursday.isoformat(),
            "trigger": "skip",
            "next_delivery_date": (thursday + 2 * WEEK).isoformat(),
        }
        [log] = _logs(db, sub_id, subscription_notices.PAID_ORDER_KEPT)
        assert log.extra_data == kept
        assert notices[-1] == {
            "action": subscription_notices.PAID_ORDER_KEPT,
            "subscription_id": sub_id,
            "extra_data": kept,
            "uncommitted": False,
        }


ACTORS = ["customer", "admin"]


def _pause(client, actor, sub_id, auth_headers, admin_headers):
    if actor == "customer":
        token = _state(client, sub_id, auth_headers)
        return client.post(f"{BASE}/{sub_id}/pause", headers=auth_headers, json={"expected_state": token})
    return client.post(
        f"{ADMIN_BASE}/{sub_id}/pause", headers=admin_headers, json={"pause_reason": "Paused by administrator"}
    )


def _resume(client, actor, sub_id, auth_headers, admin_headers):
    if actor == "customer":
        return client.post(f"{BASE}/{sub_id}/resume", headers=auth_headers)
    return client.post(f"{ADMIN_BASE}/{sub_id}/resume", headers=admin_headers, json={})


def _cancel(client, actor, sub_id, auth_headers, admin_headers):
    if actor == "customer":
        # `immediate: False` once asked for the end-of-period cancel; a cancel is always immediate now (§4.6).
        return client.post(
            f"{BASE}/{sub_id}/cancel",
            headers=auth_headers,
            json={"reason": "Moving", "immediate": False, "expected_state": _state(client, sub_id, auth_headers)},
        )
    return client.post(
        f"{ADMIN_BASE}/{sub_id}/cancel",
        headers=admin_headers,
        json={"cancellation_reason": "Cancelled by administrator"},
    )


def _update(client, actor, sub_id, auth_headers, admin_headers, body):
    if actor == "customer":
        token = _state(client, sub_id, auth_headers)
        return client.put(f"{BASE}/{sub_id}", headers=auth_headers, json={**body, "expected_state": token})
    return client.put(f"{ADMIN_BASE}/{sub_id}", headers=admin_headers, json=body)


@pytest.mark.parametrize("actor", ACTORS)
class TestPause:
    def test_an_unpaid_upcoming_order_is_cancelled_and_listed(
        self, client, db, auth_headers, admin_headers, thursday_order, actor
    ):
        sub_id, _ = thursday_order()

        resp = _pause(client, actor, sub_id, auth_headers, admin_headers)

        assert resp.status_code == 200, resp.get_json()
        [order] = _orders(db, sub_id)
        assert order.status is OrderStatus.CANCELLED
        data = resp.get_json()["data"]
        assert (_ids(data["cancelled_orders"]), _ids(data["kept_orders"])) == ([_order_ref(order)], [])
        assert _row(db, sub_id).status is SubscriptionStatus.PAUSED

    def test_a_paid_upcoming_order_stays(
        self, client, db, auth_headers, admin_headers, monday, thursday_order, notices, actor
    ):
        sub_id, _ = thursday_order(payment_method="click", paid=True)

        resp = _pause(client, actor, sub_id, auth_headers, admin_headers)

        assert resp.status_code == 200, resp.get_json()
        [order] = _orders(db, sub_id)
        assert order.status is not OrderStatus.CANCELLED
        data = resp.get_json()["data"]
        assert (_ids(data["cancelled_orders"]), _ids(data["kept_orders"])) == ([], [{**_order_ref(order), "reason_code": "PAID"}])
        assert _row(db, sub_id).status is SubscriptionStatus.PAUSED
        [log] = _logs(db, sub_id, subscription_notices.PAID_ORDER_KEPT)
        assert log.extra_data == {
            "order_id": order.id,
            "order_number": order.order_number,
            "delivery_date": (monday + timedelta(days=3)).isoformat(),
            "trigger": "pause",
            "next_delivery_date": None,
        }
        assert notices[-1]["action"] == subscription_notices.PAID_ORDER_KEPT
        assert notices[-1]["uncommitted"] is False


@pytest.mark.parametrize("actor", ACTORS)
class TestResume:
    def test_inside_the_lead_the_first_order_is_created_now(
        self,
        client,
        db,
        auth_headers,
        admin_headers,
        user_address,
        sample_product,
        morning_slot,
        clock,
        monday,
        delay,
        actor,
    ):
        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)
        assert _pause(client, "customer", sub_id, auth_headers, admin_headers).status_code == 200
        clock.at(monday + timedelta(days=2), 10)  # Wednesday: Thursday is the first allowed delivery

        resp = _resume(client, actor, sub_id, auth_headers, admin_headers)

        assert resp.status_code == 200, resp.get_json()
        row = _row(db, sub_id)
        assert (row.status, row.next_delivery_date) == (SubscriptionStatus.ACTIVE, monday + timedelta(days=3))
        delay.assert_called_once_with(sub_id)

    def test_outside_the_lead_the_daily_job_creates_it(
        self,
        client,
        db,
        auth_headers,
        admin_headers,
        user_address,
        sample_product,
        morning_slot,
        clock,
        monday,
        delay,
        actor,
    ):
        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)
        assert _pause(client, "customer", sub_id, auth_headers, admin_headers).status_code == 200
        clock.at(monday + timedelta(days=3), 10)  # Thursday: tomorrow is too soon to be Thursday again

        resp = _resume(client, actor, sub_id, auth_headers, admin_headers)

        assert resp.status_code == 200, resp.get_json()
        assert _row(db, sub_id).next_delivery_date == monday + timedelta(days=10)
        delay.assert_not_called()


@pytest.mark.parametrize("actor", ACTORS)
class TestCancel:
    def test_cancel_is_immediate_and_cancels_the_unpaid_upcoming_order(
        self, client, db, auth_headers, admin_headers, thursday_order, actor
    ):
        sub_id, _ = thursday_order()

        resp = _cancel(client, actor, sub_id, auth_headers, admin_headers)

        assert resp.status_code == 200, resp.get_json()
        row = _row(db, sub_id)
        assert row.status is SubscriptionStatus.CANCELLED
        assert row.end_date is not None
        [order] = _orders(db, sub_id)
        assert order.status is OrderStatus.CANCELLED
        assert _ids(resp.get_json()["data"]["cancelled_orders"]) == [_order_ref(order)]

    def test_a_paid_upcoming_order_stays(self, client, db, auth_headers, admin_headers, thursday_order, actor):
        sub_id, _ = thursday_order(payment_method="click", paid=True)

        resp = _cancel(client, actor, sub_id, auth_headers, admin_headers)

        assert resp.status_code == 200, resp.get_json()
        assert _row(db, sub_id).status is SubscriptionStatus.CANCELLED
        [order] = _orders(db, sub_id)
        assert order.status is not OrderStatus.CANCELLED
        assert _ids(resp.get_json()["data"]["kept_orders"]) == [{**_order_ref(order), "reason_code": "PAID"}]
        [log] = _logs(db, sub_id, subscription_notices.PAID_ORDER_KEPT)
        assert log.extra_data["trigger"] == "cancel"


@pytest.mark.parametrize("actor", ACTORS)
class TestChangeFrequencyDayOrSlot:
    def test_the_bots_frequency_body_persists_and_recomputes_the_next_delivery(
        self, client, db, auth_headers, admin_headers, user_address, sample_product, monday, actor
    ):
        """telegram_bot `update_frequency_confirm` sends exactly `{'frequency': X}`."""
        sub_id = _create(client, auth_headers, user_address.id, sample_product.id, delivery_day_of_month=15)

        resp = _update(client, actor, sub_id, auth_headers, admin_headers, {"frequency": "monthly"})

        assert resp.status_code == 200, resp.get_json()
        row = _row(db, sub_id)
        assert row.frequency == SubscriptionFrequency.MONTHLY
        assert row.next_delivery_date == subscription_schedule.first_delivery_on_or_after(
            row, monday + timedelta(days=1)
        )
        assert row.next_delivery_date.day == 15

    def test_a_day_change_with_the_order_created_never_gives_back_to_back_deliveries(
        self, client, db, auth_headers, admin_headers, monday, thursday_order, actor
    ):
        sub_id, order_id = thursday_order()

        resp = _update(client, actor, sub_id, auth_headers, admin_headers, {"delivery_day_of_week": FRIDAY})

        assert resp.status_code == 200, resp.get_json()
        # Thursday's order stays; the next is the first Friday on or after Thursday + one week.
        assert _row(db, sub_id).next_delivery_date == monday + timedelta(days=11)
        [order] = _orders(db, sub_id)
        assert (order.id, order.delivery_date, order.status) == (
            order_id,
            monday + timedelta(days=3),
            OrderStatus.PENDING,
        )

    def test_a_day_change_with_no_order_created_takes_the_new_day_this_week(
        self, client, db, auth_headers, admin_headers, user_address, sample_product, morning_slot, monday, actor
    ):
        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)

        resp = _update(client, actor, sub_id, auth_headers, admin_headers, {"delivery_day_of_week": FRIDAY})

        assert resp.status_code == 200, resp.get_json()
        assert _row(db, sub_id).next_delivery_date == monday + timedelta(days=4)


class TestAdminScheduleControls:
    def test_the_admin_form_resending_unchanged_values_does_not_move_the_clock(
        self, client, db, auth_headers, admin_headers, monday, thursday_order
    ):
        """`buildSubscriptionPayload` sends every field on every save. A rename must not reschedule."""
        sub_id, _ = thursday_order()
        before = _row(db, sub_id).next_delivery_date

        resp = client.put(
            f"{ADMIN_BASE}/{sub_id}",
            headers=admin_headers,
            json={
                "name": "Renamed",
                "frequency": "weekly",
                "delivery_day_of_week": THURSDAY,
                "auto_renew": True,
            },
        )

        assert resp.status_code == 200, resp.get_json()
        row = _row(db, sub_id)
        assert (row.name, row.next_delivery_date) == ("Renamed", before)

    def test_create_next_order_now_dates_the_order_by_the_rule(
        self, client, db, auth_headers, admin_headers, user_address, sample_product, morning_slot, monday
    ):
        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)
        thursday = monday + timedelta(days=3)

        resp = client.post(f"{ADMIN_BASE}/{sub_id}/billing/process", headers=admin_headers, json={})

        assert resp.status_code == 200, resp.get_json()
        [order] = _orders(db, sub_id)
        assert resp.get_json()["data"]["order_id"] == order.id
        assert (order.delivery_date, order.delivery_window_start) == (thursday, time(10, 0))
        assert _row(db, sub_id).next_delivery_date == thursday + WEEK

    @pytest.mark.parametrize(
        "offset, accepted",
        [(17, True), (12, False), (-4, False)],
        ids=["a-later-thursday", "a-saturday", "a-past-thursday"],
    )
    def test_set_next_delivery_date_is_validated_by_the_rule(
        self,
        client,
        db,
        auth_headers,
        admin_headers,
        user_address,
        sample_product,
        morning_slot,
        monday,
        offset,
        accepted,
    ):
        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)
        before = _row(db, sub_id).next_delivery_date
        wanted = monday + timedelta(days=offset)

        resp = client.put(
            f"{ADMIN_BASE}/{sub_id}",
            headers=admin_headers,
            json={"override_manual_billing_dates": True, "next_delivery_date": wanted.isoformat()},
        )

        assert (resp.status_code == 200) is accepted, resp.get_json()
        assert _row(db, sub_id).next_delivery_date == (wanted if accepted else before)

    def test_set_next_delivery_date_needs_the_override(
        self, client, db, auth_headers, admin_headers, user_address, sample_product, morning_slot, monday
    ):
        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)

        resp = client.put(
            f"{ADMIN_BASE}/{sub_id}",
            headers=admin_headers,
            json={"next_delivery_date": (monday + timedelta(days=17)).isoformat()},
        )

        assert resp.status_code == 200, resp.get_json()
        assert _row(db, sub_id).next_delivery_date == monday + timedelta(days=3)


# --------------------------------------------------------------------------- what the backend publishes


def _schedule_keys(upcoming):
    """`upcoming_delivery`'s schedule half; its labels and payment state are pinned in
    tests/integration/test_subscription_page_data.py."""
    return {key: upcoming[key] for key in ("date", "window_start", "window_end", "order_id")}


class TestPublishedSchedule:
    def test_upcoming_delivery_before_and_after_the_order_is_created(
        self, client, db, auth_headers, admin_headers, user_address, sample_product, morning_slot, clock, monday
    ):
        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)
        thursday = monday + timedelta(days=3)
        weekly = float(subscription_schedule.deliveries_per_month(SubscriptionFrequency.WEEKLY))

        def published():
            return [
                client.get(f"{BASE}/{sub_id}", headers=auth_headers).get_json()["data"]["subscription"],
                client.get(f"{BASE}/", headers=auth_headers).get_json()["data"]["items"][0],
                client.get(f"{ADMIN_BASE}/{sub_id}", headers=admin_headers).get_json()["data"]["subscription"],
                client.get(ADMIN_BASE, headers=admin_headers).get_json()["data"]["items"][0],
            ]

        for view in published():
            assert (view["frequency"], view["deliveries_per_month"], view["next_delivery_date"]) == (
                "weekly",
                weekly,
                thursday.isoformat(),
            )
            assert _schedule_keys(view["upcoming_delivery"]) == {
                "date": thursday.isoformat(),
                "window_start": "10:00",
                "window_end": "12:00",
                "order_id": None,
            }

        clock.at(monday + timedelta(days=2))
        process_daily_subscription_billing()
        [order] = _orders(db, sub_id)

        for view in published():
            assert view["next_delivery_date"] == (thursday + WEEK).isoformat()
            assert _schedule_keys(view["upcoming_delivery"]) == {
                "date": thursday.isoformat(),
                "window_start": "10:00",
                "window_end": "12:00",
                "order_id": order.id,
            }

    def test_a_paused_subscription_has_no_upcoming_delivery(
        self, client, db, auth_headers, user_address, sample_product, morning_slot, monday
    ):
        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)
        token = _state(client, sub_id, auth_headers)
        paused = client.post(f"{BASE}/{sub_id}/pause", headers=auth_headers, json={"expected_state": token})
        assert paused.status_code == 200

        view = client.get(f"{BASE}/{sub_id}", headers=auth_headers).get_json()["data"]["subscription"]

        assert view["upcoming_delivery"] is None


# --------------------------------------------------------------------------- §5.6 one deliveries_per_month


class TestOneDeliveriesPerMonth:
    def test_preview_detail_stats_and_mrr_agree_for_every_frequency(
        self, client, db, auth_headers, admin_headers, sample_user, user_address, sample_product, monday
    ):
        """30 000 a delivery: daily 912 500, weekly 130 000, biweekly 65 000, monthly 30 000 a month."""
        expected_monthly = {}
        for frequency in SubscriptionFrequency:
            per_month = subscription_schedule.deliveries_per_month(frequency)
            monthly = (Decimal("30000.00") * per_month).quantize(Decimal("0.01"))
            expected_monthly[frequency.value] = monthly

            preview = client.post(
                f"{BASE}/preview",
                headers=auth_headers,
                json={"frequency": frequency.value, "items": [{"product_id": sample_product.id, "quantity": 2}]},
            )
            assert preview.status_code == 200, preview.get_json()
            quote = preview.get_json()["data"]["preview"]
            assert (quote["frequency"], quote["deliveries_per_month"], quote["estimated_monthly_cost"]) == (
                frequency.value,
                float(per_month),
                float(monthly),
            )

            sub_id = _create(client, auth_headers, user_address.id, sample_product.id, frequency=frequency.value)
            detail = client.get(f"{BASE}/{sub_id}", headers=auth_headers).get_json()["data"]["subscription"]
            assert detail["deliveries_per_month"] == float(per_month)
            # Counted by the customer's monthly trend only from the month after it started.
            _row(db, sub_id).created_at = datetime(2026, 1, 1, tzinfo=UTC)
            db.session.commit()

        mrr = float(sum(expected_monthly.values()))
        analytics = client.get(f"{ADMIN_BASE}/analytics", headers=admin_headers).get_json()["data"]["analytics"]
        assert analytics["monthly_recurring_revenue"] == mrr
        report = client.post(
            "/api/v1/admin/reports/generate", headers=admin_headers, json={"report_type": "subscription_report"}
        )
        assert report.status_code == 200, report.get_json()
        assert report.get_json()["data"]["report"]["summary"]["monthly_recurring_revenue"] == mrr
        dashboard = client.get("/api/v1/admin/dashboard", headers=admin_headers).get_json()["data"]["dashboard"]
        assert dashboard["overview"]["subscriptions"]["monthly_revenue"] == mrr
        stats = client.get(f"{BASE}/statistics", headers=auth_headers).get_json()["data"]["statistics"]
        this_month = datetime.now(UTC).strftime("%Y-%m")
        assert stats["monthly_spending_trend"][this_month] == mrr


# --------------------------------------------------------------------------- deleted routes


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/api/v1/subscriptions/templates"),
        ("POST", "/api/v1/subscriptions/1/retry-billing"),
        ("POST", "/api/v1/orders/subscription"),
        ("POST", "/api/v1/payments/subscription"),
    ],
)
def test_the_deleted_routes_are_gone(app, client, db, auth_headers, method, path):
    """Gone from the URL map, not merely answering 404 for a missing row."""
    with pytest.raises((NotFound, MethodNotAllowed)):
        app.url_map.bind("localhost").match(path, method=method)
    resp = client.open(path, method=method, headers=auth_headers, json={})
    assert resp.status_code in (404, 405)


# --------------------------------------------------------------------------- final review of the change (2026-10-26)


class TestAdminCreateNextOrderNowBeforeItIsDue:
    """Controller ruling (final review I3): an admin run for a delivery NOT yet due (beyond today + the lead)
    that is refused or fails reports it and changes nothing: no skip, no clock move, no notice. The refusal
    could clear by the delivery day, and the daily job then creates the order (or skips it, §2.8)."""

    @pytest.mark.parametrize(
        "refuse, reason, message",
        [
            (
                lambda mp, app, user: setattr(user, "phone", None),
                subscription_notices.SKIP_CUSTOMER_BARRED,
                "No order created: Phone number is required to place an order.",
            ),
            (
                lambda mp, app, user: mp.setitem(app.config, "MIN_ORDER_AMOUNT", 1_000_000),
                subscription_notices.SKIP_BELOW_MINIMUM,
                "No order created: Minimum order amount is 1000000",
            ),
        ],
        ids=["customer-barred", "below-min-amount"],
    )
    def test_a_refusal_is_reported_and_moves_nothing(
        self,
        app,
        monkeypatch,
        client,
        db,
        auth_headers,
        admin_headers,
        sample_user,
        user_address,
        sample_product,
        morning_slot,
        monday,
        notices,
        refuse,
        reason,
        message,
    ):
        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)
        thursday = monday + timedelta(days=3)  # pressed on Monday: due only on Wednesday
        refuse(monkeypatch, app, sample_user)
        db.session.commit()

        resp = client.post(f"{ADMIN_BASE}/{sub_id}/billing/process", headers=admin_headers, json={})

        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["message"].startswith(message), body["message"]
        assert body["message"].endswith("Nothing was changed.")
        data = body["data"]
        assert (data["success"], data["advanced"], data["reason"], data["created_order_ids"]) == (
            False,
            False,
            reason,
            [],
        )
        assert _orders(db, sub_id) == []
        row = _row(db, sub_id)
        assert (row.status, row.next_delivery_date, row.last_delivery_date) == (
            SubscriptionStatus.ACTIVE,
            thursday,
            None,
        )
        assert _logs(db, sub_id, subscription_notices.DELIVERY_SKIPPED) == []
        assert notices == []

    def test_on_the_due_path_the_refusal_still_skips_and_tells_the_customer(
        self, client, db, auth_headers, admin_headers, sample_user, user_address, sample_product, morning_slot, clock,
        monday, notices,
    ):
        """§2.8 is unchanged when the delivery IS due: pressed on Wednesday for Thursday."""
        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)
        thursday = monday + timedelta(days=3)
        sample_user.phone = None
        db.session.commit()
        clock.at(monday + timedelta(days=2), 10)

        resp = client.post(f"{ADMIN_BASE}/{sub_id}/billing/process", headers=admin_headers, json={})

        assert resp.status_code == 200, resp.get_json()
        assert _row(db, sub_id).next_delivery_date == thursday + WEEK
        [log] = _logs(db, sub_id, subscription_notices.DELIVERY_SKIPPED)
        assert log.extra_data["reason_code"] == subscription_notices.SKIP_CUSTOMER_BARRED
        assert [n["action"] for n in notices] == [subscription_notices.DELIVERY_SKIPPED]


def _subscription_selects(statements):
    return [sql for sql in statements if "FROM subscriptions" in sql]


@contextmanager
def _orm_selects():
    """Every ORM SELECT the request runs, compiled for PostgreSQL: the test SQLite renders no FOR UPDATE, so
    the lock is read off the statement the production database would receive."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.orm import Session

    seen = []

    def record(state):
        if state.is_select and not state.is_relationship_load:
            try:
                seen.append(str(state.statement.compile(dialect=postgresql.dialect())))
            except Exception as exc:  # a construct PostgreSQL cannot render is not a subscription read
                seen.append(f"<not compiled: {exc}>")

    event.listen(Session, "do_orm_execute", record)
    try:
        yield seen
    finally:
        event.remove(Session, "do_orm_execute", record)


# `t` is the customer's `expected_state`, read before the write (spec 2026-10-27 §16 M2), so the write's own first
# subscription read is the one these tests watch.
_LOCKED_ACTIONS = {
    "customer-skip": lambda c, s, a, h, m, t: c.post(
        f"{BASE}/{s}/skip-next-delivery", headers=a, json={"expected_date": (m + timedelta(days=3)).isoformat()}
    ),
    "customer-update": lambda c, s, a, h, m, t: c.put(
        f"{BASE}/{s}", headers=a, json={"delivery_day_of_week": FRIDAY, "expected_state": t}
    ),
    "customer-pause": lambda c, s, a, h, m, t: c.post(f"{BASE}/{s}/pause", headers=a, json={"expected_state": t}),
    "customer-cancel": lambda c, s, a, h, m, t: c.post(
        f"{BASE}/{s}/cancel", headers=a, json={"reason": "Moving", "expected_state": t}
    ),
    "admin-update": lambda c, s, a, h, m, t: c.put(
        f"{ADMIN_BASE}/{s}", headers=h, json={"delivery_day_of_week": FRIDAY}
    ),
    "admin-set-next-delivery": lambda c, s, a, h, m, t: c.put(
        f"{ADMIN_BASE}/{s}",
        headers=h,
        json={"override_manual_billing_dates": True, "next_delivery_date": (m + timedelta(days=10)).isoformat()},
    ),
    "admin-pause": lambda c, s, a, h, m, t: c.post(f"{ADMIN_BASE}/{s}/pause", headers=h, json={"pause_reason": "x"}),
    "admin-cancel": lambda c, s, a, h, m, t: c.post(
        f"{ADMIN_BASE}/{s}/cancel", headers=h, json={"cancellation_reason": "x"}
    ),
    "admin-create-next-order-now": lambda c, s, a, h, m, t: c.post(
        f"{ADMIN_BASE}/{s}/billing/process", headers=h, json={}
    ),
}


class TestActionsLockTheSubscription:
    """Final review I4: every mutating path takes the subscription row FOR UPDATE before it reads the clock,
    so it serialises with the 09:00 run (which locks the same row) instead of racing it."""

    @pytest.mark.parametrize("action", sorted(_LOCKED_ACTIONS))
    def test_the_first_read_of_the_subscription_is_for_update(
        self, client, db, auth_headers, admin_headers, user_address, sample_product, morning_slot, monday, action
    ):
        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)
        token = _state(client, sub_id, auth_headers)

        with _orm_selects() as statements:
            resp = _LOCKED_ACTIONS[action](client, sub_id, auth_headers, admin_headers, monday, token)

        assert resp.status_code == 200, resp.get_json()
        selects = _subscription_selects(statements)
        assert selects, statements
        assert selects[0].rstrip().endswith("FOR UPDATE"), selects[0]

    @pytest.mark.parametrize("actor", ACTORS)
    def test_resume_locks_too(self, client, db, auth_headers, admin_headers, user_address, sample_product, monday, actor):
        sub_id = _create(client, auth_headers, user_address.id, sample_product.id)
        assert _pause(client, "customer", sub_id, auth_headers, admin_headers).status_code == 200

        with _orm_selects() as statements:
            resp = _resume(client, actor, sub_id, auth_headers, admin_headers)

        assert resp.status_code == 200, resp.get_json()
        assert _subscription_selects(statements)[0].rstrip().endswith("FOR UPDATE")

    def test_a_cancel_committed_before_the_jobs_lock_gives_no_order_and_no_notice(
        self, monkeypatch, client, db, auth_headers, sample_user, user_address, sample_product, morning_slot, clock,
        monday, notices,
    ):
        """The job picked the subscription as due, then the customer's cancel committed before the job locked
        the row: the job sees the cancel, so no order and no notice."""
        from business_app.services.subscription_service import SubscriptionService

        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)
        clock.at(monday + timedelta(days=2), 9)
        real = SubscriptionService.create_next_order

        def cancel_first(self, subscription_id, **kwargs):
            shown = SubscriptionService.state_token(db.session.get(Subscription, subscription_id))
            SubscriptionService().cancel_subscription_for_user(
                subscription_id, sample_user.id, reason="Moving", expected_state=shown
            )
            return real(self, subscription_id, **kwargs)

        monkeypatch.setattr(SubscriptionService, "create_next_order", cancel_first)
        process_daily_subscription_billing()

        assert _row(db, sub_id).status is SubscriptionStatus.CANCELLED
        assert _orders(db, sub_id) == []
        assert _logs(db, sub_id, subscription_notices.ORDER_CREATED) == []
        assert notices == []

    def test_a_refusal_skip_rechecks_the_locked_row(
        self, monkeypatch, db, clock, sample_user, user_address, sample_product, notices
    ):
        """`create_order` rolled back, so the lock was released before the refusal skip. A pause that committed
        in that gap wins: the skip re-locks, sees the change and moves nothing."""
        from business_app.services.order_service import OrderService
        from business_app.services.subscription_service import SubscriptionService
        from business_app.utils.exceptions import ValidationError

        sub_id = _model_subscription(
            db,
            sample_user,
            user_address,
            sample_product,
            frequency=SubscriptionFrequency.WEEKLY,
            day_of_week=THURSDAY,
            next_delivery_date=date(2027, 1, 7),
        )
        clock.at(date(2027, 1, 6))

        def refused_then_paused(self, *args, **kwargs):
            shown = SubscriptionService.state_token(db.session.get(Subscription, sub_id))
            SubscriptionService().pause_subscription_for_user(
                sub_id, sample_user.id, reason="Away", expected_state=shown
            )
            raise ValidationError("Minimum order amount is 1000000", error_code="ORDER_MIN_AMOUNT")

        monkeypatch.setattr(OrderService, "create_order", refused_then_paused)
        result = SubscriptionService().create_next_order(sub_id, today=date(2027, 1, 6))

        assert (result["success"], result["advanced"]) == (False, False)
        row = _row(db, sub_id)
        assert (row.status, row.next_delivery_date) == (SubscriptionStatus.PAUSED, date(2027, 1, 7))
        assert _logs(db, sub_id, subscription_notices.DELIVERY_SKIPPED) == []
        assert notices == []


@pytest.mark.parametrize("actor", ACTORS)
class TestAChangeKeepsTheCreatedDelivery:
    """Final review M1: the "+1 cycle after the last delivery" rule reads `last_delivery_date` and whether a live
    order exists for it, not only the order the customer may still cancel."""

    def test_a_day_change_on_delivery_day_with_the_driver_holding_the_order_is_not_back_to_back(
        self, client, db, auth_headers, admin_headers, clock, monday, thursday_order, actor
    ):
        sub_id, order_id = thursday_order()
        thursday = monday + timedelta(days=3)
        order = db.session.get(Order, order_id)
        order.status = OrderStatus.OUT_FOR_DELIVERY  # the driver has it: no longer "upcoming" (§9)
        db.session.commit()
        clock.at(thursday, 8)

        resp = _update(client, actor, sub_id, auth_headers, admin_headers, {"delivery_day_of_week": FRIDAY})

        assert resp.status_code == 200, resp.get_json()
        # Not tomorrow (Friday, back to back with today's delivery): the first Friday a full week after it.
        assert _row(db, sub_id).next_delivery_date == monday + timedelta(days=11)

    def test_with_two_created_orders_the_next_one_follows_the_latest(
        self, client, db, auth_headers, admin_headers, clock, sample_user, user_address, sample_product, morning_slot,
        actor,
    ):
        sub_id = _model_subscription(
            db,
            sample_user,
            user_address,
            sample_product,
            frequency=SubscriptionFrequency.DAILY,
            next_delivery_date=date(2027, 1, 7),
        )
        clock.at(date(2027, 1, 6))
        process_daily_subscription_billing()
        clock.at(date(2027, 1, 7))
        process_daily_subscription_billing()
        assert [o.delivery_date for o in _orders(db, sub_id)] == [date(2027, 1, 7), date(2027, 1, 8)]

        resp = _update(
            client, actor, sub_id, auth_headers, admin_headers, {"delivery_time_slot_id": morning_slot.id}
        )

        assert resp.status_code == 200, resp.get_json()
        assert _row(db, sub_id).next_delivery_date == date(2027, 1, 9)  # never a second delivery on the 8th


class TestAChangeInsideTheLeadCreatesTheOrderNow:
    """Final review M4: after a change of frequency, day or slot, and after "Set next delivery date", a next
    delivery inside the lead window gets its order at once, by the same step create and resume use (§4.3)."""

    @pytest.mark.parametrize("actor", ACTORS)
    def test_a_day_change_to_tomorrow(
        self, client, db, auth_headers, admin_headers, user_address, sample_product, morning_slot, monday, delay,
        actor,
    ):
        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)
        delay.assert_not_called()  # Thursday is outside Monday's lead

        resp = _update(client, actor, sub_id, auth_headers, admin_headers, {"delivery_day_of_week": 1})

        assert resp.status_code == 200, resp.get_json()
        assert _row(db, sub_id).next_delivery_date == monday + timedelta(days=1)
        delay.assert_called_once_with(sub_id)

    def test_a_change_outside_the_lead_leaves_it_to_the_daily_job(
        self, client, db, auth_headers, user_address, sample_product, morning_slot, monday, delay
    ):
        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)

        resp = client.put(
            f"{BASE}/{sub_id}",
            headers=auth_headers,
            json={"delivery_day_of_week": FRIDAY, "expected_state": _state(client, sub_id, auth_headers)},
        )

        assert resp.status_code == 200, resp.get_json()
        delay.assert_not_called()

    def test_set_next_delivery_date_inside_the_lead(
        self, client, db, auth_headers, admin_headers, user_address, sample_product, morning_slot, clock, monday,
        delay,
    ):
        sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)
        thursday = monday + timedelta(days=3)
        assert (
            client.post(
                f"{BASE}/{sub_id}/skip-next-delivery", headers=auth_headers, json={"expected_date": thursday.isoformat()}
            ).status_code
            == 200
        )
        clock.at(monday + timedelta(days=2), 10)  # Wednesday: the admin puts Thursday back

        resp = client.put(
            f"{ADMIN_BASE}/{sub_id}",
            headers=admin_headers,
            json={"override_manual_billing_dates": True, "next_delivery_date": thursday.isoformat()},
        )

        assert resp.status_code == 200, resp.get_json()
        assert _row(db, sub_id).next_delivery_date == thursday
        delay.assert_called_once_with(sub_id)


class TestADayIsFixedAtCreation:
    """Final review M5: a weekly, biweekly or monthly subscription created without its day gets one fixed from
    its first delivery, as the migration does for old rows."""

    @pytest.mark.parametrize("frequency", ["weekly", "biweekly"])
    def test_weekday_from_the_first_delivery(
        self, client, db, auth_headers, admin_headers, sample_user, user_address, sample_product, monday, frequency
    ):
        customer = _create(client, auth_headers, user_address.id, sample_product.id, frequency=frequency)
        admin = client.post(
            ADMIN_BASE,
            headers=admin_headers,
            json={
                "user_id": sample_user.id,
                "name": "Office water",
                "frequency": frequency,
                "delivery_address_id": user_address.id,
                "payment_method": "cash",
                "items": [{"product_id": sample_product.id, "quantity": 2}],
            },
        )
        assert admin.status_code == 201, admin.get_json()
        admin_id = admin.get_json()["data"]["subscription"]["id"]

        for sub_id in (customer, admin_id):
            row = _row(db, sub_id)
            assert row.next_delivery_date == monday + timedelta(days=1)
            assert (row.delivery_day_of_week, row.delivery_day_of_month) == (row.next_delivery_date.weekday(), None)

    def test_day_of_month_from_the_first_delivery(self, client, db, auth_headers, user_address, sample_product, monday):
        sub_id = _create(client, auth_headers, user_address.id, sample_product.id, frequency="monthly")

        row = _row(db, sub_id)
        assert (row.delivery_day_of_month, row.delivery_day_of_week) == (row.next_delivery_date.day, None)

    def test_a_chosen_day_is_kept_and_daily_gets_none(
        self, client, db, auth_headers, user_address, sample_product, monday
    ):
        weekly = _create(client, auth_headers, user_address.id, sample_product.id, delivery_day_of_week=THURSDAY)
        daily = _create(client, auth_headers, user_address.id, sample_product.id, frequency="daily")

        assert _row(db, weekly).delivery_day_of_week == THURSDAY
        assert (_row(db, daily).delivery_day_of_week, _row(db, daily).delivery_day_of_month) == (None, None)


class TestTheFrequencyFilter:
    """Final review M10: an unknown `?frequency=` is the client's mistake (400), not a 500."""

    def test_an_unknown_value_is_a_400_on_both_lists(
        self, client, db, auth_headers, admin_headers, user_address, sample_product, monday
    ):
        _create(client, auth_headers, user_address.id, sample_product.id)

        for path, headers in ((f"{BASE}/", auth_headers), (ADMIN_BASE, admin_headers)):
            resp = client.get(f"{path}?frequency=fortnightly", headers=headers)
            assert resp.status_code == 400, (path, resp.status_code, resp.get_json())

    def test_a_known_value_still_filters(self, client, db, auth_headers, admin_headers, user_address, sample_product, monday):
        weekly = _create(client, auth_headers, user_address.id, sample_product.id)
        _create(client, auth_headers, user_address.id, sample_product.id, frequency="monthly")

        customer = client.get(f"{BASE}/?frequency=weekly", headers=auth_headers).get_json()["data"]["items"]
        admin = client.get(f"{ADMIN_BASE}?frequency=weekly", headers=admin_headers).get_json()["data"]["items"]

        assert [s["id"] for s in customer] == [weekly]
        assert [s["id"] for s in admin] == [weekly]


@pytest.mark.parametrize("actor", ACTORS)
def test_a_slot_change_gives_the_next_created_order_the_new_window(
    client, db, auth_headers, admin_headers, user_address, sample_product, morning_slot, clock, monday, actor
):
    """Final review M11 (§5 item 5): the slot through the customer PUT and the admin PUT."""
    afternoon = DeliveryTimeSlot(
        name="Afternoon", start_time="14:00", end_time="16:00", is_active=True, available_days=[0, 1, 2, 3, 4, 5, 6]
    )
    db.session.add(afternoon)
    db.session.commit()
    sub_id = _weekly_thursday(client, auth_headers, user_address, sample_product, morning_slot)

    resp = _update(client, actor, sub_id, auth_headers, admin_headers, {"delivery_time_slot_id": afternoon.id})
    assert resp.status_code == 200, resp.get_json()
    clock.at(monday + timedelta(days=2))
    process_daily_subscription_billing()

    [order] = _orders(db, sub_id)
    assert (order.delivery_date, order.delivery_window_start, order.delivery_window_end) == (
        monday + timedelta(days=3),
        time(14, 0),
        time(16, 0),
    )


def test_next_delivery_has_one_published_answer(client, db, auth_headers, user_address, sample_product, monday):
    """Final review M2: the detail's `billing_info` no longer publishes the raw clock next to
    `upcoming_delivery`. The always-empty billing history is retired (spec 2026-10-26 §5 B6)."""
    sub_id = _create(client, auth_headers, user_address.id, sample_product.id)

    detail = client.get(f"{BASE}/{sub_id}", headers=auth_headers).get_json()["data"]

    assert detail["subscription"]["upcoming_delivery"]["date"] == (monday + timedelta(days=1)).isoformat()
    assert not {"next_delivery_date", "days_until_delivery"} & detail["billing_info"].keys(), detail["billing_info"]
    assert client.get(f"{BASE}/{sub_id}/billing-history", headers=auth_headers).status_code == 404
