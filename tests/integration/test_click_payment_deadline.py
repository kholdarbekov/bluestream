"""The Click payment deadline: one rule (plan 2026-10-24 Task 3; spec 2026-10-23 §2.5, §4.5, §5 item 4, §9).

`OrderService.payment_deadline(order)`:
- an ordinary order: created + CLICK_PAYMENT_WINDOW_HOURS (24 h), exactly as before;
- a subscription order: the later of that and the end of its delivery window (the end of the delivery day,
  local, without a window). Never sooner than 24 hours.

`cancel_abandoned_orders` keeps its `created_at < now - 24h` candidate query and cancels only the candidates
whose deadline has passed. Each cancellation tells the customer "not paid by <deadline>" through the customer
bot's `/internal/customer-notice` route (else email), published only after the cancel committed; the generic
"status changed: cancelled" message is not sent beside it.

The sweep's clock (`order_tasks.get_utc_now`) is frozen; orders carry explicit `created_at` instants. Only
external I/O is replaced: the Celery publish, the HTTP hop to the bot and Brevo's POST.
"""

import re
from datetime import date, datetime, time, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.services.order_service import OrderService
from business_app.tasks import notification_tasks, order_tasks
from business_app.tasks.order_tasks import cancel_abandoned_orders, send_payment_expired_notice_task
from shared import business_config
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus
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
from tests.notification_send_fakes import BREVO_URL, live_send_path

pytestmark = pytest.mark.integration

MONDAY = date(2027, 1, 4)
TUESDAY = date(2027, 1, 5)


def local(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TASHKENT)


def _click_order(db, user, *, created: datetime, delivery_date=None, window=None, subscription=False) -> Order:
    """An unpaid Click order, as `create_order` leaves one before the customer pays."""
    order = Order(
        user_id=user.id,
        status=OrderStatus.PENDING,
        subtotal=Decimal("30000.00"),
        total_amount=Decimal("30000.00"),
        payment_method=PaymentMethod.CLICK,
        is_subscription_order=subscription,
        delivery_date=delivery_date,
        delivery_window_start=window[0] if window else None,
        delivery_window_end=window[1] if window else None,
    )
    db.session.add(order)
    db.session.flush()
    order.created_at = created.astimezone(timezone.utc)
    db.session.add(
        Payment(
            order_id=order.id,
            user_id=user.id,
            amount=order.total_amount,
            payment_method=PaymentMethod.CLICK,
            status=PaymentStatus.PENDING,
        )
    )
    db.session.commit()
    return order


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
