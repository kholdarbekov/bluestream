"""The notices a subscription owes its customer (plan 2026-10-24 Task 3; spec 2026-10-23 §2.3-2.5, §2.7,
§2.8, §4.4, §5 item 3, §9 Messages).

Every subscription order the system creates (the daily job, the first order at creation, a resume, the
admin's "Create next order now") tells the customer once: order number, date and window ("time to be
confirmed" without one), items, total and the payment line. A Click order carries Pay now (the bot) or a
Click link (email). A skipped delivery and a paid order that stays get a notice too. Telegram through the
customer bot's `/internal/customer-notice` route when the bot is connected, otherwise email, never SMS,
and never from inside the transaction that wrote the order: the engine commits, then publishes a Celery
task, and the task sends.

The generic creation-time confirmations (`status_changed_confirmed` from the instant COD confirmation and
the business-account payment confirmation) are not sent for these orders: the order notice replaces them.

A refusal with a known code skips that delivery, advances the clock and is told to the customer (§2.8).
Any other failure, such as a Redis outage reported as a ValidationError with no code, skips nothing,
moves nothing and tells nobody; the next run creates the order (controller ruling 2026-10-26).

Only external I/O is replaced: the Celery publish, the HTTP hop to the customer bot (a recorder with
`trigger_bot_webhook`'s exact signature) and Brevo's HTTP POST.
"""

import logging
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy import text as sa_text

from business_app.models.delivery import DeliveryTimeSlot
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.product import ProductFiscalProfile
from business_app.models.subscription import Subscription, SubscriptionItem, SubscriptionLog
from business_app.models.user import User
from business_app.services import customer_notices, subscription_notices
from business_app.tasks import notification_tasks
from business_app.tasks.subscription_tasks import (
    create_subscription_delivery_task,
    process_daily_subscription_billing,
    send_subscription_notice_task,
)
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus, SubscriptionFrequency, SubscriptionStatus
from tests.integration.contract_pricing_factory import make_contract, make_workplace
from tests.integration.customer_notice_builders import (  # noqa: F401  (fixtures by their *_fixture name)
    BOT_BLOCKED,
    BOT_DELIVERED,
    BOT_DOWN,
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
from tests.integration.reorder_reminder_builders import captured_logs, seed_translations
from tests.integration.sales_pay_builders import freeze_local_now
from tests.integration.test_subscription_engine_switch import _redis_misconf
from tests.integration.tier_discount_factory import post_order, verify_phone
from tests.notification_send_fakes import BREVO_URL, live_send_path

pytestmark = pytest.mark.integration

BASE = "/api/v1/subscriptions"
ADMIN_BASE = "/api/v1/admin/subscriptions"
THURSDAY_DAY = 3
THURSDAY = date(2027, 1, 7)
WEDNESDAY = date(2027, 1, 6)
WEEK = timedelta(days=7)
CLICK_CONFIG = {"CLICK_SERVICE_ID": "31000", "CLICK_MERCHANT_ID": "52000"}
SKIP_REASON_KEYS = [
    "api.subscriptions.skip_reason.below_minimum",
    "api.subscriptions.skip_reason.date_passed",
    "api.subscriptions.skip_reason.cod_debt_limit",
]


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def quiet_side_channels():
    """The first-order Celery publish and the route notifications (external I/O)."""
    with patch.object(create_subscription_delivery_task, "delay"), patch(
        "business_app.api.subscriptions.get_notification_service"
    ):
        yield


@pytest.fixture
def published(timeline):
    """The Celery publish of the notice task (external I/O): the log ids, in order."""
    seen = []

    def spy(log_id):
        timeline.append(("published", log_id))
        seen.append(log_id)

    with patch.object(send_subscription_notice_task, "delay", side_effect=spy):
        yield seen


@pytest.fixture
def generic(monkeypatch):
    """The generic creation-time messages the order notice replaces: the status notice task, the payment
    confirmation task and the bot's payment-success push. Recorded, never sent."""
    state = SimpleNamespace(status=[], payment_confirmations=[], bot_pushes=[])

    def bot_push(endpoint, payload=None, *, request_id=None):
        state.bot_pushes.append(endpoint)
        return BOT_DELIVERED

    monkeypatch.setattr("business_app.utils.bot_webhook.trigger_bot_webhook", bot_push)
    with patch.object(
        notification_tasks.send_order_notification_task, "delay", side_effect=lambda *a: state.status.append(a)
    ), patch.object(
        notification_tasks.send_payment_confirmation_task,
        "delay",
        side_effect=lambda *a, **k: state.payment_confirmations.append(a),
    ):
        yield state


@pytest.fixture
def clock(monkeypatch):
    def at(day: date, hour: int = 9, minute: int = 0):
        return freeze_local_now(monkeypatch, datetime.combine(day, time(hour, minute), tzinfo=TASHKENT))

    return SimpleNamespace(at=at)


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
    with app.app_context():
        token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _subscription(db, user, address, product, *, payment_method=PaymentMethod.CASH, slot=None, name="Water") -> int:
    """A weekly-Thursday subscription as the migration leaves one (only the new columns are written)."""
    subscription = Subscription(
        user_id=user.id,
        name=name,
        status=SubscriptionStatus.ACTIVE,
        frequency=SubscriptionFrequency.WEEKLY,
        delivery_day_of_week=THURSDAY_DAY,
        delivery_time_slot_id=slot.id if slot is not None else None,
        delivery_address_id=address.id,
        payment_method=payment_method,
        billing_amount=Decimal("30000.00"),
        start_date=datetime.now(UTC),
        next_delivery_date=THURSDAY,
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


def _row(db, subscription_id):
    db.session.expire_all()
    return db.session.get(Subscription, subscription_id)


def _returning_customer(db, user, address):
    """One delivered order makes `create_order` confirm a cash order at once (the instant COD confirmation)."""
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


def _notice(log_id, kind, **facts):
    return {
        "endpoint": ROUTE,
        "request_id": f"subscription-log:{log_id}",
        "payload": {
            "notice_id": f"subscription-log:{log_id}",
            "kind": kind,
            "telegram_id": CHAT_ID,
            "language": "en",
            **facts,
        },
    }


def _order_created_facts(order, **payment):
    return {
        "order_id": order.id,
        "order_number": order.order_number,
        "delivery_date": THURSDAY.isoformat(),
        "window_start": "10:00",
        "window_end": "12:00",
        "items": [{"name": "Pure Water 19L", "qty": 2}],
        "total": float(order.total_amount),
        **payment,
    }


# --------------------------------------------------------------------------- the order notice, per rail


class TestTheOrderNotice:
    def test_a_cash_order_tells_the_customer_once_after_commit_and_replaces_the_confirmation(
        self, db, clock, on_telegram, user_address, sample_product, morning_slot, published, timeline, bot, generic
    ):
        _returning_customer(db, on_telegram, user_address)
        sub_id = _subscription(db, on_telegram, user_address, sample_product, slot=morning_slot)
        clock.at(WEDNESDAY)

        process_daily_subscription_billing()

        [order] = _orders(db, sub_id)
        assert order.status is OrderStatus.CONFIRMED  # the instant COD confirmation still happens...
        assert generic.status == []  # ...but its generic "confirmed" message is not sent
        [log] = _logs(db, sub_id, subscription_notices.ORDER_CREATED)
        assert published == [log.id]
        assert_published_after_commit(timeline, "SubscriptionLog", log.id)
        assert bot.calls == []  # the job only publishes; the worker sends

        assert send_subscription_notice_task(log.id) == "telegram"

        expected = _notice(
            log.id,
            "subscription_order_created",
            **_order_created_facts(
                order, payment="cash", amount_due=float(order.total_amount), prepaid_applied=None, deadline=None
            ),
        )
        assert bot.calls == [expected]
        assert set(expected["payload"]) == CONTRACT_KEYS["subscription_order_created"]
        assert expected["payload"]["total"] == 30000.0

    def test_prepaid_credit_already_applied_is_on_the_payment_line(
        self, db, clock, on_telegram, user_address, sample_product, morning_slot, published, bot
    ):
        sub_id = _subscription(db, on_telegram, user_address, sample_product, slot=morning_slot)
        clock.at(WEDNESDAY)
        process_daily_subscription_billing()
        [order] = _orders(db, sub_id)
        payment = order.payment
        payment.provider_data = {**(payment.provider_data or {}), "cod_prepayment_reserved_amount": 5000.0}
        db.session.commit()

        send_subscription_notice_task(published[0])

        facts = bot.calls[0]["payload"]
        assert (facts["payment"], facts["amount_due"], facts["prepaid_applied"]) == ("cash", 25000.0, 5000.0)

    def test_a_click_order_carries_the_payment_deadline_and_no_window_reads_time_to_be_confirmed(
        self, db, clock, on_telegram, user_address, sample_product, published, bot
    ):
        """No slot: dated, no window. The deadline is §4.5's: the end of the delivery day (no window), local,
        because that is later than 24 hours after creation."""
        sub_id = _subscription(db, on_telegram, user_address, sample_product, payment_method=PaymentMethod.CLICK)
        clock.at(WEDNESDAY)
        process_daily_subscription_billing()
        [order] = _orders(db, sub_id)
        [log_id] = published

        send_subscription_notice_task(log_id)

        assert bot.calls == [
            _notice(
                log_id,
                "subscription_order_created",
                **_order_created_facts(
                    order,
                    window_start=None,
                    window_end=None,
                    payment="online_unpaid",
                    amount_due=None,
                    prepaid_applied=None,
                    deadline="2027-01-07T23:59:00+05:00",
                ),
            )
        ]

    def test_a_click_order_with_a_slot_is_due_by_the_end_of_its_window(
        self, db, clock, on_telegram, user_address, sample_product, morning_slot, published, bot
    ):
        _subscription(
            db, on_telegram, user_address, sample_product, payment_method=PaymentMethod.CLICK, slot=morning_slot
        )
        clock.at(WEDNESDAY)
        process_daily_subscription_billing()

        send_subscription_notice_task(published[0])

        assert bot.calls[0]["payload"]["deadline"] == "2027-01-07T12:00:00+05:00"
        assert bot.calls[0]["payload"]["payment"] == "online_unpaid"

    def test_a_company_account_order_is_charged_and_no_payment_confirmation_goes_out(
        self, db, clock, sample_product, morning_slot, published, timeline, bot, generic
    ):
        user, address = make_workplace(db)
        make_contract(db, user, sample_product)
        user.telegram_id = CHAT_ID
        user.is_bot_active = True
        user.preferred_language = "en"
        db.session.commit()
        sub_id = _subscription(
            db, user, address, sample_product, payment_method=PaymentMethod.BUSINESS_ACCOUNT, slot=morning_slot
        )
        clock.at(WEDNESDAY)

        process_daily_subscription_billing()

        [order] = _orders(db, sub_id)
        assert order.status is OrderStatus.CONFIRMED
        assert order.payment.status is PaymentStatus.COMPLETED
        assert (generic.status, generic.payment_confirmations, generic.bot_pushes) == ([], [], [])
        [log_id] = published
        assert_published_after_commit(timeline, "SubscriptionLog", log_id)

        send_subscription_notice_task(log_id)

        assert bot.calls == [
            _notice(
                log_id,
                "subscription_order_created",
                **_order_created_facts(
                    order, payment="company_account", amount_due=None, prepaid_applied=None, deadline=None
                ),
            )
        ]

    def test_an_ordinary_order_still_gets_its_generic_confirmation(
        self, app, db, auth_headers, on_telegram, user_address, sample_product, generic
    ):
        """Only subscription orders are quiet at creation: a returning customer's own cash order is confirmed
        and told so, exactly as before."""
        _returning_customer(db, on_telegram, user_address)
        verify_phone(db, on_telegram)

        resp = post_order(
            app, auth_headers, product_id=sample_product.id, address_id=user_address.id, payment_method="cash"
        )

        assert resp.status_code == 201, resp.get_json()
        order_id = resp.get_json()["data"]["order"]["id"]
        assert generic.status == [(order_id, "status_changed_confirmed")]

    def test_without_telegram_the_notice_is_an_email_with_the_click_link(
        self, app, monkeypatch, db, clock, on_email, user_address, sample_product, morning_slot, published, bot
    ):
        sub_id = _subscription(
            db, on_email, user_address, sample_product, payment_method=PaymentMethod.CLICK, slot=morning_slot
        )
        clock.at(WEDNESDAY)
        process_daily_subscription_billing()
        [order] = _orders(db, sub_id)

        with live_send_path(app, monkeypatch, bot_url=BOT_URL, extra_config=CLICK_CONFIG) as post:
            assert send_subscription_notice_task(published[0]) == "email"

        assert bot.calls == []
        db.session.expire_all()
        payment = db.session.get(Payment, order.payment.id)
        assert payment.payment_link and payment.payment_link.startswith("https://my.click.uz/services/pay?")
        [call] = post.call_args_list
        assert call.args[0] == BREVO_URL
        body = call.kwargs["json"]
        assert body["to"] == [{"email": "test@example.com", "name": on_email.full_name.strip()}]
        assert body["subject"] == f"Subscription order #{order.order_number} placed - Aqua Element"
        html = body["htmlContent"]
        assert f'href="{payment.payment_link.replace("&", "&amp;")}"' in html
        assert "01/07/2027" in html and "10:00–12:00" in html  # the date and the window
        assert "Please pay by 01/07/2027 12:00" in html
        assert "Pure Water 19L" in html and "30,000" in html

    def test_a_blocked_bot_marks_the_account_and_falls_back_to_email(
        self, app, monkeypatch, db, clock, on_telegram, user_address, sample_product, morning_slot, published, bot
    ):
        _subscription(db, on_telegram, user_address, sample_product, slot=morning_slot)
        clock.at(WEDNESDAY)
        process_daily_subscription_billing()
        bot.answer = BOT_BLOCKED

        with live_send_path(app, monkeypatch, bot_url=BOT_URL) as post:
            assert send_subscription_notice_task(published[0]) == "email"

        assert [c["endpoint"] for c in bot.calls] == [ROUTE]
        user = db.session.get(User, on_telegram.id)
        db.session.refresh(user)
        assert user.is_bot_active is False and user.telegram_blocked_at is not None
        assert [c.args[0] for c in post.call_args_list] == [BREVO_URL]

    def test_a_bot_outage_is_retried_and_nothing_is_emailed(
        self, app, monkeypatch, db, clock, on_telegram, user_address, sample_product, published, bot
    ):
        _subscription(db, on_telegram, user_address, sample_product)
        clock.at(WEDNESDAY)
        process_daily_subscription_billing()
        bot.answer = BOT_DOWN

        with live_send_path(app, monkeypatch, bot_url=BOT_URL) as post, pytest.raises(
            customer_notices.RetryableNoticeError
        ):
            send_subscription_notice_task(published[0])

        post.assert_not_called()
        assert db.session.get(User, on_telegram.id).is_bot_active is True

    def test_an_order_cancelled_before_the_send_is_not_announced(
        self, db, clock, on_telegram, user_address, sample_product, published, bot
    ):
        sub_id = _subscription(db, on_telegram, user_address, sample_product)
        clock.at(WEDNESDAY)
        process_daily_subscription_billing()
        [order] = _orders(db, sub_id)
        order.status = OrderStatus.CANCELLED
        db.session.commit()

        assert send_subscription_notice_task(published[0]) == "skipped:order_inactive"
        assert bot.calls == []

    def test_the_first_order_at_creation_is_announced_too(
        self, client, db, auth_headers, on_telegram, user_address, sample_product, published, bot
    ):
        """No day chosen: the first delivery is tomorrow, inside the lead, so creation enqueues the order and the
        worker that creates it publishes the notice."""
        resp = client.post(
            f"{BASE}/",
            headers=auth_headers,
            json={
                "name": "Home water",
                "frequency": "weekly",
                "delivery_address_id": user_address.id,
                "payment_method": "cash",
                "items": [{"product_id": sample_product.id, "quantity": 2}],
                "auto_renew": True,
            },
        )
        assert resp.status_code == 201, resp.get_json()
        sub_id = resp.get_json()["data"]["subscription"]["id"]

        create_subscription_delivery_task(sub_id)

        [log] = _logs(db, sub_id, subscription_notices.ORDER_CREATED)
        assert published == [log.id]
        send_subscription_notice_task(log.id)
        assert [c["payload"]["kind"] for c in bot.calls] == ["subscription_order_created"]

    def test_admin_create_next_order_now_is_announced(
        self, client, db, admin_headers, on_telegram, user_address, sample_product, morning_slot, clock, published
    ):
        sub_id = _subscription(db, on_telegram, user_address, sample_product, slot=morning_slot)
        clock.at(date(2027, 1, 4))  # Monday: Thursday is outside the lead, the admin creates it anyway

        resp = client.post(f"{ADMIN_BASE}/{sub_id}/billing/process", headers=admin_headers, json={})

        assert resp.status_code == 200, resp.get_json()
        [log] = _logs(db, sub_id, subscription_notices.ORDER_CREATED)
        assert published == [log.id]


class TestTheCrashWindow:
    """`create_order` commits the order; the clock and the ORDER_CREATED row commit after it. A worker that
    dies in between leaves an order nobody was told about. The next run heals the clock AND owes the notice
    (a Click subscriber's only Pay now), exactly once."""

    def test_an_order_committed_without_its_notice_is_announced_by_the_heal_once(
        self, monkeypatch, db, clock, on_telegram, user_address, sample_product, morning_slot, published, bot
    ):
        from business_app.services.order_service import OrderService

        sub_id = _subscription(
            db, on_telegram, user_address, sample_product, payment_method=PaymentMethod.CLICK, slot=morning_slot
        )
        real_create_order = OrderService.create_order

        def create_then_die(self, *args, **kwargs):
            real_create_order(self, *args, **kwargs)  # committed
            raise RuntimeError("worker lost after the order committed")

        clock.at(WEDNESDAY)
        with patch.object(OrderService, "create_order", create_then_die):
            process_daily_subscription_billing()

        [order] = _orders(db, sub_id)
        row = _row(db, sub_id)
        assert (row.next_delivery_date, row.last_delivery_date) == (THURSDAY, None)  # the crash state
        assert _logs(db, sub_id, subscription_notices.ORDER_CREATED) == [] and published == []

        process_daily_subscription_billing()  # the next run heals

        row = _row(db, sub_id)
        assert (row.next_delivery_date, row.last_delivery_date) == (THURSDAY + WEEK, THURSDAY)
        assert _orders(db, sub_id) == [order]
        [log] = _logs(db, sub_id, subscription_notices.ORDER_CREATED)
        assert log.extra_data == {
            "order_id": order.id,
            "order_number": order.order_number,
            "delivery_date": THURSDAY.isoformat(),
        }
        assert published == [log.id]
        send_subscription_notice_task(log.id)
        assert [(c["payload"]["kind"], c["payload"]["order_id"]) for c in bot.calls] == [
            ("subscription_order_created", order.id)
        ]

        clock.at(THURSDAY)
        process_daily_subscription_billing()
        assert len(_logs(db, sub_id, subscription_notices.ORDER_CREATED)) == 1
        assert published == [log.id]

    def test_a_heal_whose_order_was_already_announced_sends_nothing_more(
        self, db, clock, on_telegram, user_address, sample_product, morning_slot, published
    ):
        sub_id = _subscription(db, on_telegram, user_address, sample_product, slot=morning_slot)
        clock.at(WEDNESDAY)
        process_daily_subscription_billing()
        [log] = _logs(db, sub_id, subscription_notices.ORDER_CREATED)
        row = _row(db, sub_id)
        row.next_delivery_date, row.last_delivery_date = THURSDAY, None  # the clock lost, the notice not
        db.session.commit()

        process_daily_subscription_billing()

        assert _row(db, sub_id).next_delivery_date == THURSDAY + WEEK
        assert _logs(db, sub_id, subscription_notices.ORDER_CREATED) == [log]
        assert published == [log.id]


# --------------------------------------------------------------------------- skipped deliveries (§2.8)


class TestASkippedDelivery:
    def test_a_known_refusal_skips_advances_and_tells_the_customer(
        self, app, monkeypatch, db, clock, on_telegram, user_address, sample_product, published, timeline, bot
    ):
        """Below the minimum (`ORDER_MIN_AMOUNT`, a §2.8 refusal): skipped, the clock moves, one notice."""
        seed_translations(db, SKIP_REASON_KEYS)
        monkeypatch.setitem(app.config, "MIN_ORDER_AMOUNT", 1_000_000)
        sub_id = _subscription(db, on_telegram, user_address, sample_product, name="Office water")
        clock.at(WEDNESDAY)

        process_daily_subscription_billing()

        assert _orders(db, sub_id) == []
        assert _row(db, sub_id).next_delivery_date == THURSDAY + WEEK
        [log] = _logs(db, sub_id, subscription_notices.DELIVERY_SKIPPED)
        assert log.extra_data["reason_code"] == subscription_notices.SKIP_BELOW_MINIMUM
        assert published == [log.id]
        assert_published_after_commit(timeline, "SubscriptionLog", log.id)

        send_subscription_notice_task(log.id)

        expected = _notice(
            log.id,
            "subscription_delivery_skipped",
            subscription_name="Office water",
            skipped_dates=["2027-01-07"],
            reason="the order was below the minimum order amount",
            next_delivery_date="2027-01-14",
        )
        assert bot.calls == [expected]
        assert set(expected["payload"]) == CONTRACT_KEYS["subscription_delivery_skipped"]

    def test_a_failure_with_no_code_moves_nothing_tells_nobody_and_the_next_run_creates_the_order(
        self, app, caplog, db, clock, on_telegram, user_address, sample_product, published, bot
    ):
        """`create_order` reports a Redis outage as a ValidationError with no code: not a refusal. It is logged
        at error level and the next daily run tries again."""
        sub_id = _subscription(db, on_telegram, user_address, sample_product)
        clock.at(WEDNESDAY)

        with _redis_misconf(), captured_logs(caplog, app.logger, logging.ERROR):
            summary = process_daily_subscription_billing()

        assert f"Subscription {sub_id}: creating the order for {THURSDAY} failed" in [
            record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR
        ]
        assert summary["failed"] == 1
        assert _orders(db, sub_id) == []
        assert _row(db, sub_id).next_delivery_date == THURSDAY
        assert _logs(db, sub_id, subscription_notices.DELIVERY_SKIPPED) == []
        assert published == []

        clock.at(THURSDAY)  # the next daily run, the outage over
        process_daily_subscription_billing()

        [order] = _orders(db, sub_id)
        assert order.delivery_date == THURSDAY
        [log] = _logs(db, sub_id, subscription_notices.ORDER_CREATED)
        assert published == [log.id]
        send_subscription_notice_task(log.id)
        assert [c["payload"]["kind"] for c in bot.calls] == ["subscription_order_created"]

    def test_a_passed_date_is_told_with_its_reason(
        self, db, clock, on_telegram, user_address, sample_product, published, bot
    ):
        seed_translations(db, SKIP_REASON_KEYS)
        sub_id = _subscription(db, on_telegram, user_address, sample_product)
        clock.at(date(2027, 1, 9))  # Saturday: Thursday passed during downtime

        process_daily_subscription_billing()

        [log] = _logs(db, sub_id, subscription_notices.DELIVERY_SKIPPED)
        send_subscription_notice_task(log.id)
        assert bot.calls == [
            _notice(
                log.id,
                "subscription_delivery_skipped",
                subscription_name="Water",
                skipped_dates=["2027-01-07"],
                reason="the delivery date had already passed",
                next_delivery_date="2027-01-14",
            )
        ]

    def test_the_cod_debt_cap_is_told_through_the_same_notice(
        self, db, clock, on_telegram, user_address, sample_product, published, bot
    ):
        from business_app.utils.exceptions import ValidationError

        seed_translations(db, SKIP_REASON_KEYS)
        sub_id = _subscription(db, on_telegram, user_address, sample_product)
        clock.at(WEDNESDAY)

        with patch(
            "business_app.services.cash_collection_service.CashCollectionService.validate_customer_can_use_cod",
            side_effect=ValidationError("COD debt cap reached", error_code="COD_DEBT_LIMIT_REACHED"),
        ):
            process_daily_subscription_billing()

        assert _orders(db, sub_id) == []
        assert _row(db, sub_id).next_delivery_date == THURSDAY + WEEK
        [log] = _logs(db, sub_id, subscription_notices.DELIVERY_SKIPPED)
        assert log.extra_data == {
            "skipped_by": "system",
            "reason_code": subscription_notices.SKIP_COD_DEBT_LIMIT,
            "skipped_dates": ["2027-01-07"],
            "next_delivery_date": "2027-01-14",
            "detail": "COD debt cap reached",
        }
        assert published == [log.id]

        send_subscription_notice_task(log.id)

        assert bot.calls == [
            _notice(
                log.id,
                "subscription_delivery_skipped",
                subscription_name="Water",
                skipped_dates=["2027-01-07"],
                reason="the limit of unpaid cash orders was reached",
                next_delivery_date="2027-01-14",
            )
        ]

    @pytest.mark.parametrize(
        "make_unorderable, reason_code, reason",
        [
            # An inactive product the stock check sees (`ORDER_STOCK_UNAVAILABLE`).
            (lambda db, product: setattr(product, "is_active", False), "out_of_stock", "a product was out of stock"),
            # An inactive marking-code product on a cash order skips the stock check and reaches the item
            # check, which used to refuse it with no code.
            (
                lambda db, product: (
                    setattr(product, "is_active", False),
                    db.session.add(ProductFiscalProfile(product_id=product.id, requires_marking_codes=True)),
                ),
                "out_of_stock",
                "a product was out of stock",
            ),
            # A product deleted since the subscription was made: `NotFoundError`.
            (
                lambda db, product: db.session.execute(
                    sa_text("UPDATE subscription_items SET product_id = 987654 WHERE product_id = :id"), {"id": product.id}
                ),
                "out_of_stock",
                "a product was out of stock",
            ),
            # The product's minimum quantity was raised above the subscription's line (2).
            (
                lambda db, product: setattr(product, "min_order_quantity", 5),
                "below_minimum",
                "the order was below the minimum order amount",
            ),
        ],
        ids=["inactive-product", "inactive-marking-code-product", "deleted-product", "below-min-quantity"],
    )
    def test_a_product_refusal_skips_once_with_its_reason_and_never_loops(
        self,
        app,
        caplog,
        db,
        clock,
        on_telegram,
        user_address,
        sample_product,
        published,
        bot,
        make_unorderable,
        reason_code,
        reason,
    ):
        """§2.8 names out of stock and under the minimum as refusals. `create_order` refused these with no code
        (or a NotFoundError), so the run logged an error and created nothing, and the next day the date was
        skipped as passed: the customer was told the wrong reason, every cycle."""
        seed_translations(db, [*SKIP_REASON_KEYS, "api.subscriptions.skip_reason.out_of_stock"])
        sub_id = _subscription(db, on_telegram, user_address, sample_product)
        make_unorderable(db, sample_product)
        db.session.commit()

        with captured_logs(caplog, app.logger, logging.ERROR):
            clock.at(WEDNESDAY)
            process_daily_subscription_billing()
            clock.at(THURSDAY)  # the next day's run: nothing is due, and nothing is skipped as passed
            process_daily_subscription_billing()

        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []
        assert _orders(db, sub_id) == []
        assert _row(db, sub_id).next_delivery_date == THURSDAY + WEEK
        [log] = _logs(db, sub_id, subscription_notices.DELIVERY_SKIPPED)
        assert (log.extra_data["reason_code"], log.extra_data["skipped_dates"]) == (reason_code, ["2027-01-07"])
        assert published == [log.id]

        send_subscription_notice_task(log.id)

        assert bot.calls == [
            _notice(
                log.id,
                "subscription_delivery_skipped",
                subscription_name="Water",
                skipped_dates=["2027-01-07"],
                reason=reason,
                next_delivery_date="2027-01-14",
            )
        ]

    def test_the_customers_own_skip_sends_no_notice(
        self, client, db, auth_headers, on_telegram, user_address, sample_product, morning_slot, clock, published
    ):
        sub_id = _subscription(db, on_telegram, user_address, sample_product, slot=morning_slot)
        clock.at(date(2027, 1, 4))

        resp = client.post(f"{BASE}/{sub_id}/skip-next-delivery", headers=auth_headers, json={})

        assert resp.status_code == 200, resp.get_json()
        assert len(_logs(db, sub_id, subscription_notices.DELIVERY_SKIPPED)) == 1
        assert published == []


# --------------------------------------------------------------------------- a paid order stays (§2.7)


@pytest.fixture
def paid_thursday_order(db, clock, on_telegram, user_address, sample_product, morning_slot, published):
    """A Click weekly-Thursday subscription whose Thursday order Wednesday's run created, then paid."""
    sub_id = _subscription(
        db, on_telegram, user_address, sample_product, payment_method=PaymentMethod.CLICK, slot=morning_slot
    )
    clock.at(WEDNESDAY)
    process_daily_subscription_billing()
    [order] = _orders(db, sub_id)
    order.is_paid = True
    order.payment.status = PaymentStatus.COMPLETED
    db.session.commit()
    published.clear()
    return sub_id, order


class TestAPaidOrderStays:
    @pytest.mark.parametrize(
        "path, body, trigger, next_day",
        [
            ("skip-next-delivery", {}, "skip", "2027-01-21"),
            ("pause", {}, "pause", None),
            ("cancel", {"reason": "Moving"}, "cancel", None),
        ],
    )
    def test_the_customer_is_told_the_paid_order_stays(
        self, client, db, auth_headers, paid_thursday_order, published, timeline, bot, path, body, trigger, next_day
    ):
        sub_id, order = paid_thursday_order

        resp = client.post(f"{BASE}/{sub_id}/{path}", headers=auth_headers, json=body)

        assert resp.status_code == 200, resp.get_json()
        [log] = _logs(db, sub_id, subscription_notices.PAID_ORDER_KEPT)
        assert published == [log.id]
        assert_published_after_commit(timeline, "SubscriptionLog", log.id)

        send_subscription_notice_task(log.id)

        expected = _notice(
            log.id,
            "subscription_paid_order_kept",
            order_id=order.id,
            order_number=order.order_number,
            delivery_date=THURSDAY.isoformat(),
            trigger=trigger,
            next_delivery_date=next_day,
        )
        assert bot.calls == [expected]
        assert set(expected["payload"]) == CONTRACT_KEYS["subscription_paid_order_kept"]


# --------------------------------------------------------------------------- the email templates


@pytest.mark.parametrize("language", ["en", "uz", "ru"])
@pytest.mark.parametrize("kind", sorted(CONTRACT_KEYS))
def test_every_notice_has_an_email_template_and_subject_in_every_language(app, kind, language):
    """A notice that reaches the email branch renders its own file template and subject, never the DB
    fallback (which has no row for these types)."""
    from business_app.services.email_template_service import get_email_template_service

    facts = {
        "subscription_order_created": {
            "order_id": 7,
            "order_number": "TG_000777_27",
            "delivery_date": "2027-01-07",
            "window_start": "10:00",
            "window_end": "12:00",
            "items": [{"name": "Pure Water 19L", "qty": 2}],
            "total": 30000.0,
            "payment": "online_unpaid",
            "amount_due": None,
            "prepaid_applied": None,
            "deadline": "2027-01-07T12:00:00+05:00",
        },
        "subscription_delivery_skipped": {
            "subscription_name": "Office water",
            "skipped_dates": ["2027-01-07"],
            "reason": "a product was out of stock",
            "next_delivery_date": "2027-01-14",
        },
        "subscription_paid_order_kept": {
            "order_id": 7,
            "order_number": "TG_000777_27",
            "delivery_date": "2027-01-07",
            "trigger": "skip",
            "next_delivery_date": "2027-01-21",
        },
        "order_payment_expired": {
            "order_id": 7,
            "order_number": "TG_000777_27",
            "deadline": "2027-01-07T12:00:00+05:00",
        },
    }[kind]

    with app.app_context():
        data = customer_notices.email_template_data(kind, facts, language, pay_url="https://pay.example/7")
        rendered = get_email_template_service().render_notification_email(kind, language, data)

    assert rendered is not None, f"no email template for {kind} in {language}"
    assert "{" not in rendered["subject"]
    assert "{{" not in rendered["content"] and "{%" not in rendered["content"]
    if "order_number" in facts:
        assert "TG_000777_27" in rendered["subject"] + rendered["content"]
    if kind == "subscription_order_created":
        assert 'href="https://pay.example/7"' in rendered["content"]
        assert "07.01.2027" in rendered["content"] or "01/07/2027" in rendered["content"]
