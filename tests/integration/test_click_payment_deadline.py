"""The Click payment deadline: one rule (plan 2026-10-24 Task 3; spec 2026-10-23 §2.5, §4.5, §5 item 4, §9; plan
2026-10-27 Task 3, spec 2026-10-27 §4.4, §17 Q1, Q3).

`OrderService.payment_deadline(order)`:
- an ordinary order: CLICK_PAYMENT_WINDOW_HOURS (24 h) from when it became payable on Click
  (`Order.payment_window_start`: `online_payable_since`, stamped at a Click checkout and on every move onto Click;
  `created_at` for an order nothing stamped);
- a subscription order: the later of that and the end of its delivery window (the end of the delivery day,
  local, without a window). Never sooner than 24 hours.

`cancel_abandoned_orders` cancels exactly the orders whose Pay is refused as `deadline_passed`
(`payment_projection.pay_refusal`, at the sweep's instant): unpaid online orders that are pending, confirmed or
preparing, not with the driver and not waiting for a new date. It never cancels a cash order or one holding money.
Each cancellation tells the customer "not paid by <deadline>" through the customer bot's
`/internal/customer-notice` route (else email), published only after the cancel committed; the generic
"status changed: cancelled" message is not sent beside it.

The sweep's clock (`order_tasks.get_utc_now`) is frozen; orders carry explicit `created_at` and
`online_payable_since` instants. Only external I/O is replaced: the Celery publish, the HTTP hop to the bot, Brevo's
POST and the Asl belgisi pre-utilisation.
"""

import inspect
import os
import re
import threading
import time as clock
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from flask import current_app
from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.services import customer_order_actions
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.order_schedule_service import OrderScheduleService
from business_app.services.order_service import OrderService
from business_app.services.payment_service import PaymentService
from business_app.tasks import notification_tasks, order_tasks
from business_app.tasks.delivery_tasks import refresh_driver_route_card_task
from business_app.tasks.order_tasks import cancel_abandoned_orders, send_payment_expired_notice_task
from business_app.utils.timezone_utils import ensure_utc, get_utc_now
from shared import business_config
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod, PaymentStatus
from tests.integration.customer_notice_builders import (  # noqa: F401  (fixtures by their *_fixture name)
    BOT_URL,
    CHAT_ID,
    CONTRACT_KEYS,
    ROUTE,
    TASHKENT,
    assert_published_after_commit,
    bot_fixture,
    on_email_fixture,
    on_telegram_fixture,
    timeline_fixture,
)
from tests.integration.fake_gateways import (
    TEST_CLICK_SHOP_SECRET_KEY,
    apply_test_provider_secrets,
    make_click_webhook_form,
)
from tests.integration.test_customer_cancel_races_pg import (  # plain helpers, safe to import
    DRIVER_STATUS_URL,
    _await_a_lock_waiter,
    _finish,
    _lock_waiters,
    _thread,
    _world,
)
from tests.integration.tier_discount_factory import post_order, verify_phone
from tests.notification_send_fakes import BREVO_URL, live_send_path

pytestmark = pytest.mark.integration

SUNDAY = date(2027, 1, 3)
MONDAY = date(2027, 1, 4)
TUESDAY = date(2027, 1, 5)
FRIDAY = date(2027, 1, 8)
ORDERS = "/api/v1/orders"
# The Asl belgisi pre-utilisation a Click checkout calls (external I/O).
PRE_UTILISE = (
    "business_app.services.payment_fiscalization_service.PaymentFiscalizationService"
    ".pre_utilise_marking_codes_for_payment"
)
# `_click_order`'s default start of the payment window: the order became payable when it was created, as a Click
# checkout stamps it.
AT_CREATION = object()


def local(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TASHKENT)


# Orders past the driver or the door (spec §4.4): the order's status, and its delivery when it has one.
SHAPES = {
    "out_for_delivery": (OrderStatus.OUT_FOR_DELIVERY, None),
    "preparing_picked_up": (
        OrderStatus.PREPARING,
        {"status": DeliveryStatus.PICKED_UP, "scheduled_date": local(TUESDAY, 10), "scheduled_time_slot": "anytime"},
    ),
    "delivered": (OrderStatus.DELIVERED, None),
    "awaiting_new_date": (
        OrderStatus.CONFIRMED,
        {
            "status": DeliveryStatus.FAILED,
            "failed_delivery_reason": "customer_unavailable",
            "delivery_attempts": 1,
            "scheduled_date": local(TUESDAY, 10),
            "scheduled_time_slot": "anytime",
        },
    ),
}


def _click_order(
    db,
    user,
    *,
    created: datetime,
    online_payable_since=AT_CREATION,
    status=OrderStatus.PENDING,
    method=PaymentMethod.CLICK,
    delivery_date=None,
    window=None,
    subscription=False,
) -> Order:
    """An unpaid order on `method` (Click unless named), as `create_order` leaves one before the customer pays.

    Its payment window opened at `online_payable_since`: when it was created unless named, None for an order nothing
    stamped (an old-code worker's, during a rolling deploy)."""
    order = Order(
        user_id=user.id,
        status=status,
        subtotal=Decimal("30000.00"),
        total_amount=Decimal("30000.00"),
        payment_method=method,
        is_subscription_order=subscription,
        delivery_date=delivery_date,
        delivery_window_start=window[0] if window else None,
        delivery_window_end=window[1] if window else None,
    )
    db.session.add(order)
    db.session.flush()
    order.created_at = created.astimezone(timezone.utc)
    start = created if online_payable_since is AT_CREATION else online_payable_since
    order.online_payable_since = start.astimezone(timezone.utc) if start is not None else None
    db.session.add(
        Payment(
            order_id=order.id,
            user_id=user.id,
            amount=order.total_amount,
            payment_method=method,
            status=PaymentStatus.PENDING,
        )
    )
    db.session.commit()
    return order


def _headers(app, user):
    """The customer clients' bearer token."""
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _fresh(db, order_id) -> Order:
    db.session.expire_all()
    return db.session.get(Order, order_id)


@pytest.fixture
def sweep(monkeypatch):
    """Run `cancel_abandoned_orders` with its clock at a given instant."""

    def run_at(when: datetime):
        monkeypatch.setattr(order_tasks, "get_utc_now", lambda: when.astimezone(timezone.utc))
        return cancel_abandoned_orders()

    return run_at


@pytest.fixture
def published(timeline):
    """The Celery publish of the deadline-cancel notice (external I/O): the order ids, in order."""
    seen = []

    def spy(order_id):
        timeline.append(("published", order_id))
        seen.append(order_id)

    with patch.object(send_payment_expired_notice_task, "delay", side_effect=spy):
        yield seen


@pytest.fixture
def status_notices():
    """The generic status-change notice task: recorded, never sent."""
    seen = []
    with patch.object(
        notification_tasks.send_order_notification_task, "delay", side_effect=lambda *args: seen.append(args)
    ):
        yield seen


@pytest.fixture
def card_refreshes():
    """The silent refresh of a driver's route card, a Celery publish (external I/O): each call's arguments, bound
    against the task's own signature so a drifted call fails here."""
    seen = []
    signature = inspect.signature(refresh_driver_route_card_task.run)

    def spy(*args, **kwargs):
        signature.bind(*args, **kwargs)
        seen.append((args, kwargs))

    with patch.object(refresh_driver_route_card_task, "delay", side_effect=spy):
        yield seen


@pytest.fixture
def click(app):
    """The real Click link builder, configured as production configures it; the app config is put back after."""
    before = dict(app.config)
    apply_test_provider_secrets(app)
    yield app
    for key in set(app.config) - set(before):
        del app.config[key]
    app.config.update(before)


def _status(db, order_id):
    db.session.expire_all()
    return db.session.get(Order, order_id).status


# --------------------------------------------------------------------------- the rule


class TestPaymentDeadline:
    def test_the_window_is_a_business_setting_of_24_hours(self):
        assert business_config.CLICK_PAYMENT_WINDOW_HOURS == 24

    def test_the_deadline_has_no_second_copy(self):
        """Final review M3: the dead `send_payment_reminder` task quoted `created_at + 24h`, which is wrong for
        a subscription order. `OrderService.payment_deadline` is the only expression of the rule."""
        from pathlib import Path

        from business_app.tasks import payment_tasks

        assert not hasattr(payment_tasks, "send_payment_reminder")
        backend = Path(payment_tasks.__file__).resolve().parents[1]
        copies = [
            f"{path.relative_to(backend.parent)}:{number}"
            for path in backend.rglob("*.py")
            if "migrations" not in path.parts
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if re.search(r"created_at\s*\+\s*timedelta\(hours=", line)
        ]
        assert copies == []

    def test_an_ordinary_order_has_24_hours(self, db, sample_user):
        order = _click_order(
            db, sample_user, created=local(MONDAY, 9), delivery_date=TUESDAY, window=(time(10), time(12))
        )
        assert OrderService.payment_deadline(order) == local(TUESDAY, 9)

    def test_a_subscription_order_for_later_today_still_gets_24_hours(self, db, sample_user):
        """Created 10:00 for today 12:00-14:00: a full 24 hours, not 4."""
        order = _click_order(
            db,
            sample_user,
            created=local(MONDAY, 10),
            delivery_date=MONDAY,
            window=(time(12), time(14)),
            subscription=True,
        )
        assert OrderService.payment_deadline(order) == local(TUESDAY, 10)

    def test_a_subscription_order_waits_for_the_end_of_its_window(self, db, sample_user):
        """Created Monday 09:00 for Tuesday 10:00-12:00: Tuesday 12:00, not Tuesday 09:00."""
        order = _click_order(
            db,
            sample_user,
            created=local(MONDAY, 9),
            delivery_date=TUESDAY,
            window=(time(10), time(12)),
            subscription=True,
        )
        assert OrderService.payment_deadline(order) == local(TUESDAY, 12)

    def test_without_a_window_it_is_the_end_of_the_delivery_day(self, db, sample_user):
        order = _click_order(db, sample_user, created=local(MONDAY, 9), delivery_date=TUESDAY, subscription=True)
        assert OrderService.payment_deadline(order) == local(TUESDAY, 23, 59)


# --------------------------------------------------------------------------- when the window starts


class TestWhenTheWindowStarts:
    """Spec 2026-10-27 §4.4, §17 Q3: the 24 hours count from when the order became payable on Click."""

    def test_the_24_hours_count_from_when_the_order_became_payable_on_click(self, db, sample_user):
        order = _click_order(db, sample_user, created=local(MONDAY, 9), online_payable_since=local(MONDAY, 15))
        assert OrderService.payment_deadline(order) == local(TUESDAY, 15)

    def test_a_click_checkout_stamps_the_start(self, app, db, sample_user, sample_product, user_address):
        verify_phone(db, sample_user)

        before = get_utc_now()
        with patch(PRE_UTILISE, return_value={"utilised_at": None, "fast_path": True}):
            resp = post_order(
                app,
                _headers(app, sample_user),
                product_id=sample_product.id,
                address_id=user_address.id,
                payment_method="click",
            )
        after = get_utc_now()

        assert resp.status_code == 201, resp.get_json()
        order = _fresh(db, resp.get_json()["data"]["order"]["id"])
        assert before <= ensure_utc(order.online_payable_since) <= after

    def test_a_cash_checkout_stamps_nothing(self, app, db, sample_user, sample_product, user_address):
        verify_phone(db, sample_user)

        resp = post_order(
            app,
            _headers(app, sample_user),
            product_id=sample_product.id,
            address_id=user_address.id,
            payment_method="cash",
        )

        assert resp.status_code == 201, resp.get_json()
        assert _fresh(db, resp.get_json()["data"]["order"]["id"]).online_payable_since is None

    def test_a_payme_order_moved_to_click_restarts_the_window(self, app, db, sample_user, click):
        """A legacy Payme order created 3 days ago, payable for 23 hours (an hour of its window left): Pay moves it
        onto Click (spec §4.3), and its window starts again."""
        now = get_utc_now()
        order = _click_order(
            db,
            sample_user,
            method=PaymentMethod.PAYME,
            created=now - timedelta(days=3),
            online_payable_since=now - timedelta(hours=23),
        )

        before_post = get_utc_now()
        resp = app.test_client().post(
            f"{ORDERS}/{order.id}/retry-payment?lang=en",
            json={"channel": "web", "expected_amount": str(order.total_amount)},
            headers=_headers(app, sample_user),
        )

        assert resp.status_code == 200, resp.get_json()
        order = _fresh(db, order.id)
        assert (order.payment_method, order.payment.payment_method) == (PaymentMethod.CLICK, PaymentMethod.CLICK)
        assert before_post <= ensure_utc(order.online_payable_since)

    def test_an_unstamped_order_falls_back_to_created_at(self, db, sample_user):
        """An order an old-code worker placed during a rolling deploy carries no stamp."""
        order = _click_order(db, sample_user, created=local(MONDAY, 9), online_payable_since=None)
        assert OrderService.payment_deadline(order) == local(TUESDAY, 9)


# --------------------------------------------------------------------------- the sweep, both ways


class TestTheSweep:
    def test_created_at_10_for_today_12_to_14_is_cancelled_only_after_24_hours(self, db, sample_user, sweep, published):
        order = _click_order(
            db,
            sample_user,
            created=local(MONDAY, 10),
            delivery_date=MONDAY,
            window=(time(12), time(14)),
            subscription=True,
        )

        sweep(local(MONDAY, 14, 30))  # the window is over, but 24 hours are not
        assert _status(db, order.id) is OrderStatus.PENDING
        sweep(local(TUESDAY, 9, 59))
        assert _status(db, order.id) is OrderStatus.PENDING

        sweep(local(TUESDAY, 10, 1))
        assert _status(db, order.id) is OrderStatus.CANCELLED
        assert published == [order.id]

    def test_created_monday_9_for_tuesday_10_to_12_is_cancelled_after_tuesday_12_not_at_24_hours(
        self, db, sample_user, sweep, published
    ):
        order = _click_order(
            db,
            sample_user,
            created=local(MONDAY, 9),
            delivery_date=TUESDAY,
            window=(time(10), time(12)),
            subscription=True,
        )

        result = sweep(local(TUESDAY, 9, 30))  # 24.5 hours on: a candidate, but its deadline is 12:00
        assert _status(db, order.id) is OrderStatus.PENDING
        assert result["cancelled_count"] == 0
        sweep(local(TUESDAY, 11, 59))
        assert _status(db, order.id) is OrderStatus.PENDING
        assert published == []

        sweep(local(TUESDAY, 12, 1))
        assert _status(db, order.id) is OrderStatus.CANCELLED
        assert published == [order.id]

    def test_an_ordinary_click_order_is_still_cancelled_at_24_hours(self, db, sample_user, sweep, published):
        order = _click_order(
            db, sample_user, created=local(MONDAY, 9), delivery_date=TUESDAY, window=(time(10), time(12))
        )

        sweep(local(TUESDAY, 8, 59))
        assert _status(db, order.id) is OrderStatus.PENDING

        sweep(local(TUESDAY, 9, 1))
        assert _status(db, order.id) is OrderStatus.CANCELLED
        assert published == [order.id]

    def test_a_paid_order_is_never_cancelled(self, db, sample_user, sweep, published):
        order = _click_order(db, sample_user, created=local(MONDAY, 9))
        order.payment.status = PaymentStatus.COMPLETED
        db.session.commit()

        sweep(local(TUESDAY, 12))

        assert _status(db, order.id) is OrderStatus.PENDING
        assert published == []

    @pytest.mark.parametrize("status", [OrderStatus.CONFIRMED, OrderStatus.PREPARING])
    def test_a_confirmed_or_preparing_click_order_is_cancelled_at_its_deadline(
        self, db, sample_user, sweep, published, status
    ):
        order = _click_order(db, sample_user, created=local(MONDAY, 9), status=status)

        sweep(local(TUESDAY, 8, 59))
        assert _status(db, order.id) is status

        sweep(local(TUESDAY, 9, 1))
        assert _status(db, order.id) is OrderStatus.CANCELLED
        assert published == [order.id]

    @pytest.mark.parametrize("status", [OrderStatus.CONFIRMED, OrderStatus.PREPARING])
    def test_a_stop_a_driver_has_only_accepted_is_cancelled_with_the_order(
        self, db, sample_user, delivery_driver, sweep, published, status_notices, card_refreshes, status
    ):
        """Spec §4.4: a driver who has only accepted the stop (`assigned`) does not have the order, so its deadline
        binds. The cancel takes the stop with it. The driver hears nothing but one silent refresh of their route card
        (owner, 2026-10-22), and the customer gets the deadline notice and not the generic status message."""
        order = _click_order(db, sample_user, created=local(MONDAY, 9), status=status)
        delivery = Delivery(
            order_id=order.id,
            status=DeliveryStatus.ASSIGNED,
            delivery_person_id=delivery_driver.id,
            scheduled_date=local(TUESDAY, 10),
            scheduled_time_slot="anytime",
        )
        db.session.add(delivery)
        db.session.commit()
        driver_id, delivery_id = delivery_driver.id, delivery.id

        sweep(local(TUESDAY, 9, 1))

        assert _status(db, order.id) is OrderStatus.CANCELLED
        assert db.session.get(Delivery, delivery_id).status is DeliveryStatus.CANCELLED
        assert card_refreshes == [((driver_id, delivery_id), {})]
        assert status_notices == []
        assert published == [order.id]

    def test_an_order_whose_cancel_fails_does_not_stop_the_run(self, db, sample_user, sweep, published, monkeypatch):
        """A cancel the database refuses mid-transaction (a deadlock with a driver's tap, say) leaves the session
        unusable until it is rolled back. The run rolls it back, so the next order is still cancelled, and the
        refused one is asked again by the next run."""
        refused_id = _click_order(db, sample_user, created=local(MONDAY, 9)).id
        next_id = _click_order(db, sample_user, created=local(MONDAY, 9, 30)).id
        cancel_order = OrderService.cancel_order

        def refused_by_the_database(self, order_id, *args, **kwargs):
            if order_id == refused_id:
                db.session.add(Payment(order_id=order_id, user_id=None, amount=None))  # NOT NULL: the flush fails
                db.session.flush()
            return cancel_order(self, order_id, *args, **kwargs)

        monkeypatch.setattr(OrderService, "cancel_order", refused_by_the_database)

        result = sweep(local(TUESDAY, 10))

        assert result == {"cancelled_count": 1}
        assert _status(db, refused_id) is OrderStatus.PENDING
        assert _status(db, next_id) is OrderStatus.CANCELLED
        assert published == [next_id]

    @pytest.mark.parametrize("shape", ["out_for_delivery", "preparing_picked_up", "delivered", "awaiting_new_date"])
    def test_an_order_past_the_driver_or_the_door_is_never_cancelled(self, db, sample_user, sweep, published, shape):
        """Spec §4.4: an order the driver has, one already delivered or one waiting for a new date is owed money and
        has no deadline; its Pay stays until it is paid."""
        status, delivery = SHAPES[shape]
        order = _click_order(db, sample_user, created=local(MONDAY, 9), status=status)
        if delivery is not None:
            db.session.add(Delivery(order_id=order.id, **delivery))
            db.session.commit()

        sweep(local(FRIDAY, 12))

        assert _status(db, order.id) is status
        assert published == []

    def test_a_cash_order_is_never_cancelled(self, db, sample_user, sweep, published):
        """Cash is paid at the door: a cash order has no payment deadline, however old."""
        order = _click_order(
            db, sample_user, created=local(MONDAY, 9), online_payable_since=None, method=PaymentMethod.CASH
        )

        sweep(local(FRIDAY, 12))

        assert _status(db, order.id) is OrderStatus.PENDING
        assert published == []

    @pytest.mark.parametrize("money", ["partially_paid", "collected_pending"])
    def test_an_order_holding_money_is_never_cancelled(self, db, sample_user, sweep, published, money):
        """Spec §17 Q1: some money is in, so the driver collects the rest at the door."""
        order = _click_order(db, sample_user, created=local(MONDAY, 9))
        payment = order.payment
        payment.status = PaymentStatus.PARTIALLY_PAID if money == "partially_paid" else PaymentStatus.PENDING
        payment.amount_collected = Decimal("10000.00")
        payment.outstanding_amount = Decimal("20000.00")
        db.session.commit()

        sweep(local(FRIDAY, 12))

        assert _status(db, order.id) is OrderStatus.PENDING
        assert published == []

    def test_a_backfilled_order_gets_a_full_window_from_deploy(self, db, on_telegram, sweep, published, bot):
        """Created Sunday 09:00, still unpaid on Click at the deploy (Monday 12:00), which the migration stamps: a
        full 24 hours from the deploy, and the notice quotes that deadline, not one dated before the deploy."""
        order = _click_order(db, on_telegram, created=local(SUNDAY, 9), online_payable_since=local(MONDAY, 12))

        sweep(local(MONDAY, 13))
        assert _status(db, order.id) is OrderStatus.PENDING

        sweep(local(TUESDAY, 12, 1))
        assert _status(db, order.id) is OrderStatus.CANCELLED
        assert published == [order.id]
        send_payment_expired_notice_task(order.id)
        assert bot.calls[0]["payload"]["deadline"] == local(TUESDAY, 12).isoformat()

    def test_pay_and_the_sweep_agree_at_the_deadline(self, app, monkeypatch, db, sample_user, sweep, published):
        """At the deadline instant Pay is refused and the sweep cancels: neither leaves a moment for the other."""
        order = _click_order(db, sample_user, created=local(MONDAY, 9))
        monkeypatch.setattr(customer_order_actions, "get_utc_now", lambda: local(TUESDAY, 9).astimezone(timezone.utc))

        resp = app.test_client().get(f"{ORDERS}/{order.id}?lang=en", headers=_headers(app, sample_user))

        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["data"]["order"]["customer_actions"]["pay"]["refusal"] == "deadline_passed"
        sweep(local(TUESDAY, 9))
        assert _status(db, order.id) is OrderStatus.CANCELLED
        assert published == [order.id]

    def test_the_sweep_cancels_at_a_deadline_that_falls_after_local_midnight(
        self, db, on_telegram, sweep, published, bot
    ):
        """Review Focus 2: payable from local Monday 02:00 (Sunday 21:00 UTC), so the deadline is local Tuesday
        02:00, while UTC is still Monday."""
        order = _click_order(db, on_telegram, created=local(MONDAY, 2))

        sweep(local(TUESDAY, 1, 59))
        assert _status(db, order.id) is OrderStatus.PENDING

        sweep(local(TUESDAY, 2, 1))
        assert _status(db, order.id) is OrderStatus.CANCELLED
        send_payment_expired_notice_task(order.id)
        assert bot.calls[0]["payload"]["deadline"] == local(TUESDAY, 2).isoformat()


# --------------------------------------------------------------------------- the deadline-cancel notice


class TestTheCancellationNotice:
    def test_it_is_published_after_the_cancel_commits_and_replaces_the_generic_message(
        self, db, on_telegram, sweep, published, timeline, status_notices, bot
    ):
        order = _click_order(
            db,
            on_telegram,
            created=local(MONDAY, 9),
            delivery_date=TUESDAY,
            window=(time(10), time(12)),
            subscription=True,
        )

        sweep(local(TUESDAY, 12, 1))

        assert published == [order.id]
        assert_published_after_commit(timeline, "Order", order.id)
        assert status_notices == []  # no generic "status changed: cancelled" beside it

        assert send_payment_expired_notice_task(order.id) == "telegram"

        expected = {
            "endpoint": ROUTE,
            "request_id": f"order-payment-expired:{order.id}",
            "payload": {
                "notice_id": f"order-payment-expired:{order.id}",
                "kind": "order_payment_expired",
                "telegram_id": CHAT_ID,
                "language": "en",
                "order_id": order.id,
                "order_number": order.order_number,
                "deadline": "2027-01-05T12:00:00+05:00",
            },
        }
        assert bot.calls == [expected]
        assert set(expected["payload"]) == CONTRACT_KEYS["order_payment_expired"]

    def test_an_ordinary_order_quotes_its_24_hour_deadline(self, db, on_telegram, sweep, published, bot):
        order = _click_order(db, on_telegram, created=local(MONDAY, 9))

        sweep(local(TUESDAY, 9, 1))
        send_payment_expired_notice_task(order.id)

        assert bot.calls[0]["payload"]["deadline"] == "2027-01-05T09:00:00+05:00"

    def test_without_telegram_it_is_an_email(self, app, monkeypatch, db, on_email, sweep, published, bot):
        order = _click_order(db, on_email, created=local(MONDAY, 9))
        sweep(local(TUESDAY, 9, 1))

        with live_send_path(app, monkeypatch, bot_url=BOT_URL) as post:
            assert send_payment_expired_notice_task(order.id) == "email"

        assert bot.calls == []
        [call] = post.call_args_list
        assert call.args[0] == BREVO_URL
        body = call.kwargs["json"]
        assert body["subject"] == f"Order #{order.order_number} cancelled: not paid - Aqua Element"
        assert "was not paid by 01/05/2027 09:00" in body["htmlContent"]

    def test_an_order_that_is_not_cancelled_sends_nothing(self, db, on_telegram, bot):
        order = _click_order(db, on_telegram, created=local(MONDAY, 9))

        assert send_payment_expired_notice_task(order.id) == "skipped:not_cancelled"
        assert bot.calls == []


# --------------------------------------------------------------------------- the sweep against live writers (Postgres)


@pytest.fixture
def pg_world(pg_app, pg_db, monkeypatch, no_fiscalization):
    """REAL POSTGRES: `_world`'s unpaid Click order, confirmed, its stop accepted by a driver and not picked up, with
    the sweep's clock 25 hours on, past its deadline, and Click's secrets set. `pg_app` is built without the suite's
    REDIS_URL, and a cancel releases stock reservations in Redis (the precedent of test_customer_cancel_races_pg.py)."""
    current_app.config["REDIS_URL"] = os.environ["REDIS_URL"]
    apply_test_provider_secrets(pg_app)
    world = _world(pg_db)
    later = get_utc_now() + timedelta(hours=25)
    monkeypatch.setattr(order_tasks, "get_utc_now", lambda: later)
    return world


@pytest.fixture
def notices_published():
    """The Celery publish of the deadline-cancel notice (external I/O), on any thread: the order ids, in order.

    Patched where the sweep looks it up, the `order_tasks` module global, which every thread reads the same. Not
    `delay` through the `@shared_task` Proxy: that sets it on the task of whichever Celery app is current on the
    patching (main) thread, and a sweep on its own thread resolves the Proxy to `default_app`'s task, a separate
    instance, so its publish would never land here. `spec` and the one-argument `append` refuse any other call."""
    seen = []
    publish = Mock(spec=["delay"], delay=Mock(side_effect=seen.append))
    with patch.object(order_tasks, "send_payment_expired_notice_task", publish):
        yield seen


def _click_complete_form(pg_db, world):
    """Click's successful COMPLETE for the world's order, signed as the gateway signs it."""
    order = pg_db.session.get(Order, world.order_id)
    return make_click_webhook_form(
        action="1",
        click_trans_id="970001",
        merchant_trans_id=order.order_number,
        amount=str(int(order.total_amount)),
        secret_key=TEST_CLICK_SHOP_SECRET_KEY,
        merchant_prepare_id=str(order.payment.id),
        click_paydoc_id="970001001",
    )


def _on_its_own_thread(name, pg_app, call, answers, errors):
    """`call()` on its own thread, app context, session and connection, as the webhook's gunicorn worker or the
    Celery worker runs it. Its return value lands in `answers[name]`; anything it raises lands in `errors`."""

    def run():
        with pg_app.app_context():
            try:
                answers[name] = call()
            except Exception as exc:  # noqa: BLE001 -- named by the assertion that reads `errors`
                errors.append(f"{name}: {exc!r}")

    return threading.Thread(target=run, name=name)


def _committed_or_queued(thread, pg_db) -> str:
    """What a writer just started on `thread` did: finished ("committed"), or queued on a row lock ("queued")."""
    deadline = clock.monotonic() + 10
    while clock.monotonic() < deadline:
        if not thread.is_alive():
            return "committed"
        if _lock_waiters(pg_db):
            return "queued"
        clock.sleep(0.05)
    return "neither"


def _prepaid_balance(world) -> Decimal:
    return CashCollectionService().get_customer_prepaid_balance(world.customer_id)


def _sweep() -> dict:
    """`cancel_abandoned_orders`' own body, run in the calling thread's app context: `pg_app`'s.

    Not the task call. A task call runs in whatever context the thread's CURRENT Celery app gives it, and Celery's
    current app is per thread. Once a module earlier in the same xdist worker imported `business_app.tasks.celery_app`
    (tests/unit/test_order_tasks_abandoned.py and others hand `make_celery()` the session SQLite `app`), that app's
    ContextTask pushes the SQLite `app` on the main thread, and the sweep queries a SQLite database with no tables
    instead of this test's Postgres. `.run` is the same function a worker runs, without that wrapper."""
    return cancel_abandoned_orders.run()


class TestTheSweepDecidesOnLockedRows:
    """Spec §4.4 and the step-4 Task 3 security review: the sweep decides each order on its Delivery, Payment and Order
    rows locked and re-read, in the order the customer's cancel, Pay and the payment webhooks take them, and cancels
    inside that same transaction. A Click COMPLETE or a driver's pickup that commits first is seen and the order is
    left alone. One that arrives while the sweep holds the rows waits for it, and then meets a cancelled order. The
    admin Cancel, which holds only the Order, re-reads the payment it cancels, so it never writes over a COMPLETE
    that committed behind it.

    The interleavings are forced: the other writer is started at a named point inside the sweep (or the sweep is
    started while the pickup holds its row), and every wait is bounded, so a missing lock fails the test instead of
    hanging it."""

    def test_a_click_payment_that_lands_while_the_sweep_cancels_waits_and_is_refused(
        self, pg_app, pg_db, pg_world, monkeypatch, notices_published
    ):
        """COMPLETE queues on the payment row the sweep holds. Once the cancel commits it reads the cancelled
        payment, whose amount the cancel set to what was collected (0), and answers Click with an error. Nothing is
        booked as paid on the cancelled order, and the cancelled row is not rewritten."""
        world = pg_world
        form = _click_complete_form(pg_db, world)
        answers, errors, seen = {}, [], {}
        complete = _on_its_own_thread(
            "click-complete", pg_app, lambda: PaymentService().handle_click_webhook(dict(form)), answers, errors
        )
        claim = OrderService._claim_status_transition

        def the_payment_lands_now(self, order, old_status, new_status):
            if new_status is OrderStatus.CANCELLED and not complete.ident:
                complete.start()
                seen["complete"] = _committed_or_queued(complete, pg_db)
            return claim(self, order, old_status, new_status)

        monkeypatch.setattr(OrderService, "_claim_status_transition", the_payment_lands_now)
        try:
            result = _sweep()
        finally:
            _finish(*([complete] if complete.ident else []))

        assert errors == []
        assert seen == {"complete": "queued"}, "the COMPLETE did not wait for the sweep's lock on the payment"
        assert result == {"cancelled_count": 1}
        assert answers["click-complete"] == {"error": -2, "error_note": "Incorrect amount"}
        pg_db.session.expire_all()
        order = pg_db.session.get(Order, world.order_id)
        assert (order.status, order.is_paid) == (OrderStatus.CANCELLED, False)
        payment = Payment.query.filter_by(order_id=world.order_id).one()
        assert (payment.status, payment.amount) == (PaymentStatus.CANCELLED, Decimal("0.00"))
        assert _prepaid_balance(world) == Decimal("0.00")
        assert notices_published == [world.order_id]

    def test_a_click_payment_that_completed_before_the_sweep_locked_the_order_keeps_it(
        self, pg_app, pg_db, pg_world, monkeypatch, notices_published
    ):
        """COMPLETE commits after the sweep picked the order and before it locks it, while the sweep's session still
        holds the order and its payment as they were: the locked re-read sees the payment and the order stays."""
        world = pg_world
        form = _click_complete_form(pg_db, world)
        answers, errors, seen, held = {}, [], {}, []
        complete = _on_its_own_thread(
            "click-complete", pg_app, lambda: PaymentService().handle_click_webhook(dict(form)), answers, errors
        )
        lock_delivery = OrderScheduleService._lock_delivery

        def the_payment_lands_first(order_id):
            if not complete.ident:
                # What a session that read the order before the payment completed holds.
                held.extend([pg_db.session.get(Order, order_id), Payment.query.filter_by(order_id=order_id).one()])
                complete.start()
                seen["complete"] = _committed_or_queued(complete, pg_db)
            return lock_delivery(order_id)

        monkeypatch.setattr(OrderScheduleService, "_lock_delivery", staticmethod(the_payment_lands_first))
        try:
            result = _sweep()
        finally:
            _finish(*([complete] if complete.ident else []))

        assert errors == []
        assert seen == {"complete": "committed"}, "the COMPLETE waited: the sweep held a lock before deciding"
        assert answers["click-complete"]["error"] == 0
        assert result == {"cancelled_count": 0}
        pg_db.session.expire_all()
        order = pg_db.session.get(Order, world.order_id)
        assert (order.status, order.is_paid) == (OrderStatus.CONFIRMED, True)
        payment = Payment.query.filter_by(order_id=world.order_id).one()
        assert (payment.status, payment.amount) == (PaymentStatus.COMPLETED, Decimal("15000.00"))
        assert pg_db.session.get(Delivery, world.delivery_id).status is DeliveryStatus.ASSIGNED
        assert _prepaid_balance(world) == Decimal("0.00")
        assert notices_published == []

    def test_an_admin_cancel_never_overwrites_a_click_payment_that_completed_behind_it(
        self, pg_app, pg_db, pg_world, monkeypatch
    ):
        """The admin's Cancel (`PUT /admin/orders/<id>/status`, as `adminService.updateOrderStatus` sends it) holds
        only the Order. A COMPLETE that commits after it read the payment keeps its COMPLETED row, and the cancelled
        order's money becomes the customer's balance (owner ruling 2026-08-24)."""
        world = pg_world
        form = _click_complete_form(pg_db, world)
        answers, errors, seen = {}, [], {}
        complete = _on_its_own_thread(
            "click-complete", pg_app, lambda: PaymentService().handle_click_webhook(dict(form)), answers, errors
        )
        claim = OrderService._claim_status_transition

        def the_payment_lands_now(self, order, old_status, new_status):
            if new_status is OrderStatus.CANCELLED and not complete.ident:
                complete.start()
                seen["complete"] = _committed_or_queued(complete, pg_db)
            return claim(self, order, old_status, new_status)

        monkeypatch.setattr(OrderService, "_claim_status_transition", the_payment_lands_now)
        try:
            response = pg_app.test_client().put(
                f"/api/v1/admin/orders/{world.order_id}/status",
                json={"status": "cancelled", "notes": "", "reason": "The customer asked in the bot chat"},
                headers=world.admin_headers,
            )
        finally:
            _finish(*([complete] if complete.ident else []))

        assert errors == []
        assert seen == {"complete": "committed"}
        assert answers["click-complete"]["error"] == 0
        assert response.status_code == 200, response.get_json()
        pg_db.session.expire_all()
        order = pg_db.session.get(Order, world.order_id)
        assert (order.status, order.is_paid) == (OrderStatus.CANCELLED, True)
        payment = Payment.query.filter_by(order_id=world.order_id).one()
        assert (payment.status, payment.amount) == (PaymentStatus.COMPLETED, Decimal("15000.00"))
        assert _prepaid_balance(world) == Decimal("15000.00")

    def test_the_sweep_waits_for_a_pickup_that_holds_the_stop_and_then_leaves_the_order(
        self, pg_app, pg_db, pg_world, monkeypatch, notices_published
    ):
        """The driver's Picked up (`PUT /staff/delivery/<id>/status`, as staff_bot sends it) holds the Delivery when
        the sweep reaches the order. The sweep waits for it, re-reads the order the driver now has, and leaves it."""
        world = pg_world
        pickup_holds_the_row = threading.Event()
        release_the_pickup = threading.Event()
        predicate = CashCollectionService.is_offline_settleable_electronic

        # `update_delivery_status` asks this right after its locked read of the Delivery and before it writes.
        def held_after_the_locked_read(cls, order):
            if threading.current_thread().name == "pickup" and not pickup_holds_the_row.is_set():
                pickup_holds_the_row.set()
                assert release_the_pickup.wait(timeout=30), "the test never released the pickup"
            return predicate(order)

        monkeypatch.setattr(
            CashCollectionService, "is_offline_settleable_electronic", classmethod(held_after_the_locked_read)
        )
        # A sweep that decided before the pickup committed starts a cancel, and Postgres then aborts one side of the
        # deadlock that follows. Whichever side loses, that cancel was the bug.
        cancels_started = []
        cancel_order = OrderService.cancel_order

        def recorded_cancel(self, order_id, *args, **kwargs):
            cancels_started.append(order_id)
            return cancel_order(self, order_id, *args, **kwargs)

        monkeypatch.setattr(OrderService, "cancel_order", recorded_cancel)
        # The sweep's per-order failures (a deadlock among them), which it logs and moves past.
        sweep_failures = []
        monkeypatch.setattr(
            order_tasks.logger, "exception", lambda message, *args, **_kwargs: sweep_failures.append(message % args)
        )
        answers, errors = {}, []
        pickup = _thread(
            "pickup",
            pg_app,
            lambda client: client.put(
                DRIVER_STATUS_URL.format(delivery_id=world.delivery_id),
                json={"status": "picked_up"},
                headers=world.driver_headers,
            ),
            answers,
            errors,
            finally_set=pickup_holds_the_row,
        )
        sweep = _on_its_own_thread("sweep", pg_app, _sweep, answers, errors)

        pickup.start()
        try:
            assert pickup_holds_the_row.wait(timeout=30), "the pickup never reached its locked read"
            assert errors == [], errors
            sweep.start()
            assert _await_a_lock_waiter(pg_db), "the sweep did not wait: it decided without the Delivery lock"
            assert sweep.is_alive(), "the sweep finished while the pickup held the row"
        finally:
            release_the_pickup.set()
            _finish(pickup, *([sweep] if sweep.ident else []))

        assert errors == []
        pickup_status, pickup_body = answers["pickup"]
        assert pickup_status == 200, pickup_body
        assert answers["sweep"] == {"cancelled_count": 0}
        assert cancels_started == [], "the sweep decided on the stop before the pickup committed"
        assert sweep_failures == [], "the sweep took the Order before the Delivery, against the pickup's lock order"
        pg_db.session.expire_all()
        assert pg_db.session.get(Delivery, world.delivery_id).status is DeliveryStatus.PICKED_UP
        assert pg_db.session.get(Order, world.order_id).status is OrderStatus.OUT_FOR_DELIVERY
        assert notices_published == []
