"""Every payment confirmation leaves the backend only after the transaction that writes the money commits
(step-4 Task Hfix).

`business_app/utils/payment_confirmation_dispatch.py` exists because of incident TG_000092_26: a confirmation task
published from inside the writing transaction was read by the worker under READ COMMITTED against a half-committed
world, and a customer who paid a surplus was told the whole order was still owed. Cash collections already parked
their message there. The online paths did not: a Click COMPLETE on a held order or on an order already past PENDING,
a late Click debit on an order settled in cash (Case C), the reconcile and the business-account completion all
published (or pushed to the bot) before their commit. In Case C the worker could read the cash allocations before
their reversal committed and describe the cash instead of the Click money.

Pinned here, on the real paths (Click Prepare/Complete on the real webhook, the admin's cash collection on its real
route, the reconcile task's own body):
- a confirmation is published after the commit: a connection of its own, at the instant of the publish, reads the
  payment COMPLETED and marked;
- Case C's late Click debit is described as the Click money by a worker that reads in a session of its own;
- a completion that rolls back sends nothing, and Click's retry then confirms once;
- a reconcile overlapping a COMPLETE decides on the locked payment row: one completion, one confirmation;
- the late-debit credit notice goes by the one channel rule (connected bot, else real email, else nothing; never SMS).

Only external I/O is replaced: the HTTP hop to the customer bot (a recorder with `trigger_bot_webhook`'s own
signature), the Celery publish of the email task (bound against the task's `run`), Click's status API, Redis' dedupe
key, and the notification send (bound against the real `send_notification`). The Postgres tests run each writer on its
own thread, app context, session and connection, as gunicorn and Celery workers would; every wait is bounded.
"""

import inspect
import logging
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import current_app
from flask_jwt_extended import create_access_token
from sqlalchemy.orm import Session

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.notification import NotificationPreference
from business_app.models.order import Order
from business_app.models.payment import CashCollectionEvent, Payment, PaymentTransaction
from business_app.models.product import Product
from business_app.models.sales_visits import Visit
from business_app.models.user import User, UserAddress
from business_app.services.click_payment_provider_service import ClickPaymentProviderService
from business_app.services.notification_service import NotificationService
from business_app.services.payment_service import PaymentService
from business_app.services.sales.visit_service import VisitService
from business_app.tasks import notification_tasks, payment_tasks
from business_app.tasks.payment_tasks import (
    process_payment_verification,
    reconcile_completed_payment_side_effects,
    reconcile_pending_payments,
)
from business_app.utils.constants import NotificationChannel, NotificationType
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import (
    DeliveryStatus,
    EntitySubtype,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
    UserType,
)
from shared.validators import PLACEHOLDER_EMAIL_DOMAIN_LOCAL
from tests.integration.fake_gateways import (
    TEST_CLICK_SHOP_SECRET_KEY,
    apply_test_provider_secrets,
    make_click_webhook_form,
)
from tests.integration.test_click_payment_deadline import (  # plain helpers, safe to import
    _committed_or_queued,
    _on_its_own_thread,
    lock_point,  # noqa: F401  (fixture)
)
from tests.integration.customer_notice_builders import BOT_BLOCKED, BOT_DOWN  # the bot's answers, as received
from tests.integration.reorder_reminder_builders import event_rows  # plain helper, safe to import
from tests.integration.test_customer_cancel_races_pg import _finish, _user  # plain helpers, safe to import
from tests.integration.test_payment_confirmation_one_per_payment import (  # plain helpers, safe to import
    _customer,
    _pay_by_click,
    _push,
)
from tests.integration.test_payment_matrix import _seed_click_payment  # plain helper, safe to import
from tests.integration.test_staff_sales_visits_api import _fund_business_account  # plain helper, safe to import
from tests.integration.test_sales_agent_order_hold_pg import (  # plain helpers, safe to import
    HELD,
    _payload,
    _visit_at_order_step,
)
from tests.integration.test_sales_agent_order_hold_pg import _world as _agent_world

pytestmark = pytest.mark.integration

WEBHOOK_URL = "/api/v1/payments/webhook/click"
COLLECTIONS_URL = "/api/v1/admin/staff/cash-reconciliation/collections"
CHAT_ID = "810000117"
REAL_EMAIL = "test@example.com"
PLACEHOLDER_EMAIL = f"telegram_{CHAT_ID}@{PLACEHOLDER_EMAIL_DOMAIN_LOCAL}"
TOTAL = Decimal("15000.00")
# Read before any fixture replaces it: the recorder binds every send against the real method.
SEND_NOTIFICATION = inspect.signature(NotificationService.send_notification)


# --------------------------------------------------------------------------- recorders (external I/O)


@pytest.fixture
def sent(monkeypatch):
    """The two ways a payment confirmation leaves the backend, each recorded and never sent: the HTTP hop to the
    customer bot (`trigger_bot_webhook`'s own signature) and the Celery publish of the email task (bound against the
    task's `run`). The task is replaced where the publisher looks it up, the `notification_tasks` module global, which
    every thread reads the same. `on_publish(payment_id)` runs at the instant of each publish."""
    task = notification_tasks.send_payment_confirmation_task
    signature = inspect.signature(task.run)
    state = SimpleNamespace(pushes=[], email_tasks=[], at_publish=[], payment_id=None, on_publish=None)

    def at_publish(payment_id):
        if state.on_publish is not None:
            state.at_publish.append(state.on_publish(payment_id))

    def trigger_bot_webhook(endpoint, payload=None, *, request_id=None):
        state.pushes.append((endpoint, request_id, dict(payload or {})))
        at_publish(state.payment_id)
        return {"success": True, "message": "Notification sent"}

    def delay(*args, **kwargs):
        signature.bind(*args, **kwargs)
        state.email_tasks.append((args, kwargs))
        at_publish(args[0])

    monkeypatch.setattr("business_app.utils.bot_webhook.trigger_bot_webhook", trigger_bot_webhook)
    monkeypatch.setattr(notification_tasks, "send_payment_confirmation_task", SimpleNamespace(delay=delay, run=task.run))
    return state


@pytest.fixture
def late_debit_notices(monkeypatch):
    """The Celery publish of the late-debit credit notice (external I/O), recorded and never sent; bound against the
    task's `run`, replaced as the `notification_tasks` module global. `on_publish()` runs at the instant of each
    publish; `run` is the task body the worker would run."""
    task = notification_tasks.send_late_debit_credit_notice_task
    signature = inspect.signature(task.run)
    state = SimpleNamespace(published=[], at_publish=[], on_publish=None, run=task.run)

    def delay(*args, **kwargs):
        signature.bind(*args, **kwargs)
        state.published.append((args, kwargs))
        if state.on_publish is not None:
            state.at_publish.append(state.on_publish())

    monkeypatch.setattr(
        notification_tasks, "send_late_debit_credit_notice_task", SimpleNamespace(delay=delay, run=task.run)
    )
    return state


def _what_a_fresh_connection_reads(pg_db):
    """The payment as a connection of its own reads it now, under READ COMMITTED (what had committed): its status
    and when it was marked confirmed, if it was."""

    def read(payment_id):
        with Session(pg_db.engine) as fresh:
            payment = fresh.get(Payment, payment_id)
            stamp = ((payment.provider_data or {}).get("post_payment") or {}).get("confirmation_enqueued_at")
            return payment.status, datetime.fromisoformat(stamp) if stamp else None

    return read


def _record_notices(monkeypatch):
    """Every `NotificationService.send_notification` call, bound against the real method: (user, type, channels,
    template data). Nothing is sent."""
    notices = []

    def send_notification(self, *args, **kwargs):
        bound = SEND_NOTIFICATION.bind(self, *args, **kwargs)
        bound.apply_defaults()
        arguments = bound.arguments
        notices.append(
            (arguments["user_id"], arguments["notification_type"], arguments["channels"], arguments["template_data"])
        )
        return {channel.value: {"success": True} for channel in arguments["channels"] or []}

    monkeypatch.setattr(NotificationService, "send_notification", send_notification)
    return notices


# --------------------------------------------------------------------------- Postgres worlds


def _reach(pg_db, user, channel):
    """`channel` is "bot" (connected bot, placeholder email) or "email" (no bot, a real address)."""
    user.telegram_id = CHAT_ID if channel == "bot" else None
    user.is_bot_active = channel == "bot"
    user.email = PLACEHOLDER_EMAIL if channel == "bot" else f"hfix-{uuid.uuid4().hex[:8]}@example.com"
    user.preferred_language = "en"
    pg_db.session.commit()


def _address(pg_db, user):
    address = UserAddress(
        user_id=user.id,
        title="Home",
        full_address="1 Commit Street, Tashkent",
        street_address="1 Commit Street",
        city="Tashkent",
        latitude=41.3111,
        longitude=69.2797,
        is_default=True,
    )
    pg_db.session.add(address)
    pg_db.session.flush()
    return address


def _order(pg_db, customer, *, status, method, provider_data=None):
    """An unpaid order of 15,000 on `method`, with its PENDING payment, as `create_order` leaves one."""
    order = Order(
        user_id=customer.id,
        order_number=f"ORD-HFIX-{uuid.uuid4().hex[:8].upper()}",
        status=status,
        payment_method=method,
        is_paid=False,
        subtotal=TOTAL,
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=TOTAL,
        delivery_address_id=_address(pg_db, customer).id,
    )
    pg_db.session.add(order)
    pg_db.session.flush()
    pg_db.session.add(
        Payment(
            order_id=order.id,
            user_id=customer.id,
            payment_method=method,
            amount=TOTAL,
            amount_collected=Decimal("0.00"),
            outstanding_amount=TOTAL,
            currency="UZS",
            status=PaymentStatus.PENDING,
            payment_id=f"PAY-HFIX-{order.id}",
            provider_data=provider_data or {},
        )
    )
    pg_db.session.commit()
    return order


def _customer_pg(pg_db, phone):
    return _user(pg_db, phone, role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL)


def _admin_headers(pg_db, phone):
    admin = _user(pg_db, phone, role=UserRole.ADMIN, user_type=UserType.STAFF)
    token = create_access_token(identity=str(admin.id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _pending_click_order(pg_app, pg_db):
    customer = _customer_pg(pg_db, "+998901251001")
    return _order(pg_db, customer, status=OrderStatus.PENDING, method=PaymentMethod.CLICK).id, customer.id


def _click_order_past_pending(pg_app, pg_db):
    customer = _customer_pg(pg_db, "+998901251002")
    return _order(pg_db, customer, status=OrderStatus.CONFIRMED, method=PaymentMethod.CLICK).id, customer.id


def _held_agent_order_on_click(pg_app, pg_db):
    """An agent's second business-account order at one outlet on one day is held (C14) and paid at creation; the
    admin then moves it to Click on the real route, as the store asked to pay by the link (the
    test_t_hold_3_a_held_order_paid_by_click_is_confirmed_once path, on Postgres). The business account is offered to
    a workplace, never a grocery store, so the T-HOLD-14 world's account is a workplace with a funded contract."""
    world = _agent_world(pg_db)
    pg_db.session.get(User, world.customer_id).entity_subtype = EntitySubtype.WORKPLACE
    pg_db.session.commit()
    _fund_business_account(pg_db, world.customer_id, pg_db.session.get(Product, world.product_id))
    on_account = {**_payload(world.product_id), "payment_method": "business_account"}
    states = [
        VisitService.place_order(
            pg_db.session.get(Visit, _visit_at_order_step(agent_id, world.outlet_id)), dict(on_account), agent_id
        )
        for agent_id in world.agent_ids
    ]
    (_first, _first_state), (held, state) = states
    assert state == HELD
    paid_on_account = pg_db.session.get(Order, held.id).payment
    assert (paid_on_account.status, PaymentService.payment_confirmation_dispatched(paid_on_account)) == (
        PaymentStatus.COMPLETED,
        True,
    ), "the business-account payment was confirmed (no channel) when the order was placed"
    moved = pg_app.test_client().post(
        f"/api/v1/admin/orders/{held.id}/payment-method",
        json={"new_method": "click", "reason": "Store pays by the Click link"},
        headers=_admin_headers(pg_db, "+998901251003"),
    )
    assert moved.status_code == 200, moved.get_data(as_text=True)
    return held.id, world.customer_id


SHAPES = {
    "pending": _pending_click_order,
    "held": _held_agent_order_on_click,
    "past_pending": _click_order_past_pending,
}
# What each order's status is once its Click payment completed: a held order stays PENDING until a manager decides.
STATUS_AFTER = {"pending": OrderStatus.CONFIRMED, "held": OrderStatus.PENDING, "past_pending": OrderStatus.CONFIRMED}


@pytest.fixture
def pg_world(pg_app, pg_db, no_fiscalization):
    """Click's secrets, and the suite's Redis (`pg_app` is built without REDIS_URL; placing an order reserves stock
    and the webhook claims its idempotency key there)."""
    current_app.config["REDIS_URL"] = os.environ["REDIS_URL"]
    apply_test_provider_secrets(pg_app)
    return pg_app


# --------------------------------------------------------------------------- 1. published after the commit


@pytest.mark.parametrize("channel", ["bot", "email"])
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_click_confirmation_is_published_only_after_the_commit(pg_world, pg_db, sent, shape, channel):
    """At the instant the push leaves, or the email task is published, a connection of its own already reads the
    payment COMPLETED and marked by this payment: the worker and the bot can only read committed money. A plain order
    commits its payment early (the order's confirmation commits it), but its mark did not; a held order and an order
    already past PENDING committed nothing at all before the old publish. (The mark is checked by its time: a held
    order's business-account payment was marked when it was placed.)"""
    order_id, customer_id = SHAPES[shape](pg_world, pg_db)
    customer = pg_db.session.get(User, customer_id)
    _reach(pg_db, customer, channel)
    order = pg_db.session.get(Order, order_id)
    payment = order.payment
    sent.payment_id = payment.id
    sent.on_publish = _what_a_fresh_connection_reads(pg_db)
    before = datetime.now(UTC)

    _pay_by_click(pg_world.test_client(), order, "960001")

    pg_db.session.expire_all()
    order = pg_db.session.get(Order, order_id)
    expected = ([_push(customer, order, payment)], []) if channel == "bot" else ([], [((payment.id,), {})])
    assert (sent.pushes, sent.email_tasks) == expected
    [(status, marked_at)] = sent.at_publish
    assert status == PaymentStatus.COMPLETED
    assert marked_at is not None and before <= marked_at <= datetime.now(UTC), "published before its mark committed"
    assert (order.status, order.payment.status) == (STATUS_AFTER[shape], PaymentStatus.COMPLETED)


# --------------------------------------------------------------------------- 2. Case C


def _the_worker_renders(pg_app, sent, notices):
    """At each publish, run the email task's body the way the worker does, on its own thread, app context and
    session, and keep the payment confirmation it renders. Redis' dedupe key is faked while it runs (the publisher
    waits for it)."""
    errors = []

    def render(_payment_id):
        args, kwargs = sent.email_tasks[-1]
        before = len(notices)

        def work():
            with pg_app.app_context():
                try:
                    notification_tasks.send_payment_confirmation_task.run(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 -- named by the assertion that reads `errors`
                    errors.append(repr(exc))

        worker = threading.Thread(target=work, name="celery-worker")
        with patch("business_app.redis_client", MagicMock(**{"get.return_value": None})):
            worker.start()
            worker.join(timeout=30)
        assert not worker.is_alive(), "the worker never finished"
        return [notice for notice in notices[before:] if notice[1] == NotificationType.PAYMENT_CONFIRMATION]

    return render, errors


def _cash_settled_click_order(pg_db):
    """A Click order the driver delivered and the order was converted to cash (Case A): the payment reads CASH and
    keeps the Click identifiers handle_prepare stamped. Nothing is collected yet."""
    customer = _customer_pg(pg_db, "+998901251011")
    _reach(pg_db, customer, "email")
    driver = _user(pg_db, "+998901251013", role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF)
    profile = DeliveryPerson(user_id=driver.id, full_name="Commit Driver", phone=driver.phone, is_active=True)
    pg_db.session.add(profile)
    pg_db.session.flush()
    order = _order(
        pg_db,
        customer,
        status=OrderStatus.DELIVERED,
        method=PaymentMethod.CASH,
        provider_data={"click": {"click_trans_id": "950001", "click_paydoc_id": "950001001"}},
    )
    delivered_at = datetime.now(UTC)
    pg_db.session.add(
        Delivery(
            order_id=order.id,
            delivery_person_id=profile.id,
            status=DeliveryStatus.DELIVERED,
            scheduled_date=delivered_at,
            scheduled_time_slot="09:00-12:00",
            delivered_at=delivered_at,
            actual_delivery_time=delivered_at,
        )
    )
    pg_db.session.commit()
    return order


def test_case_c_late_click_debit_describes_the_click_money(pg_world, pg_db, sent, monkeypatch):
    """The admin records the customer's cash for the delivered order (the Record Collection modal's body); then the
    customer's Click payment lands anyway. The Click rail is restored and the cash is freed (owner ruling
    2026-10-15). Both events still produce their message (an open owner question whether to collapse them), and the
    worker reading the Click one in a session of its own describes the Click debit, never the reversed cash."""
    order = _cash_settled_click_order(pg_db)
    payment_id, customer_id, order_number = order.payment.id, order.user_id, order.order_number
    notices = _record_notices(monkeypatch)
    sent.on_publish, errors = _the_worker_renders(pg_world, sent, notices)
    client = pg_world.test_client()

    collected = client.post(
        COLLECTIONS_URL,
        json={
            "customer_id": customer_id,
            "amount": 15000,
            "source": "standalone_meeting",
            "order_id": order.id,
            "notes": "Paid at the office",
        },
        headers=_admin_headers(pg_db, "+998901251012"),
    )
    assert collected.status_code == 201, collected.get_json()
    event_id = collected.get_json()["data"]["cash_collection_event"]["id"]
    complete = client.post(
        WEBHOOK_URL,
        data=make_click_webhook_form(
            action="1",
            click_trans_id="950001",
            merchant_trans_id=order_number,
            amount="15000",
            secret_key=TEST_CLICK_SHOP_SECRET_KEY,
            merchant_prepare_id=str(payment_id),
            click_paydoc_id="950001001",
        ),
        content_type="application/x-www-form-urlencoded",
    )

    assert complete.get_json()["error"] == 0, complete.get_json()
    assert errors == []
    assert sent.email_tasks == [
        ((payment_id,), {"collection_state_token": "15000.00", "cash_collection_event_id": event_id}),
        ((payment_id,), {}),
    ]
    cash_message, click_message = sent.at_publish
    [(_, _, cash_channels, cash_data)] = cash_message
    [(user_id, _, channels, data)] = click_message
    assert (user_id, channels, cash_channels) == (customer_id, [NotificationChannel.EMAIL], [NotificationChannel.EMAIL])
    assert (data["order_number"], data["payment_method"], data["payment_amount"]) == (
        order_number,
        NotificationService._payment_method_label(PaymentMethod.CLICK, "en"),
        "15,000",
    )
    assert data["payment_method"] != cash_data["payment_method"], "the Click message described the cash collection"
    pg_db.session.expire_all()
    payment = pg_db.session.get(Payment, payment_id)
    assert (payment.payment_method, payment.status) == (PaymentMethod.CLICK, PaymentStatus.COMPLETED)


# --------------------------------------------------------------------------- 3. a rollback sends nothing


@pytest.mark.parametrize("bot", ["connected", None], ids=["bot", "email"])
def test_a_rolled_back_completion_sends_nothing(
    matrix_client, db, sample_user, order_with_address, sent, monkeypatch, bot
):
    """The completing transaction fails after the confirmation was decided (here: queueing the receipt raises before
    the commit), so the webhook rolls back and Click is answered 503. Nobody is told anything, the payment is left
    PENDING and unmarked, and Click's retry of the same COMPLETE then confirms once."""
    customer = _customer(db, sample_user, bot=bot, email=REAL_EMAIL)
    order = order_with_address
    order.status = OrderStatus.CONFIRMED
    db.session.commit()
    payment = _seed_click_payment(db, order)
    sent.payment_id = payment.id

    def the_commit_never_comes(self, payment_id):
        raise RuntimeError("the database went away before the commit")

    monkeypatch.setattr(PaymentService, "queue_click_fiscalization", the_commit_never_comes)
    form = {
        "click_trans_id": "961001",
        "merchant_trans_id": order.order_number,
        "amount": str(int(order.total_amount)),
        "secret_key": TEST_CLICK_SHOP_SECRET_KEY,
    }
    prepare = matrix_client.post(
        WEBHOOK_URL, data=make_click_webhook_form(action="0", **form), content_type="application/x-www-form-urlencoded"
    )
    assert prepare.get_json()["error"] == 0, prepare.get_json()
    complete_form = {**form, "merchant_prepare_id": str(payment.id), "click_paydoc_id": "961001001"}

    failed = matrix_client.post(
        WEBHOOK_URL,
        data=make_click_webhook_form(action="1", **complete_form),
        content_type="application/x-www-form-urlencoded",
    )

    assert failed.status_code == 503, failed.get_data(as_text=True)
    assert (sent.pushes, sent.email_tasks) == ([], [])
    db.session.expire_all()
    payment = db.session.get(Payment, payment.id)
    assert (payment.status, PaymentService.payment_confirmation_dispatched(payment)) == (PaymentStatus.PENDING, False)

    monkeypatch.setattr(PaymentService, "queue_click_fiscalization", lambda self, payment_id: None)
    retried = matrix_client.post(
        WEBHOOK_URL,
        data=make_click_webhook_form(action="1", **complete_form),
        content_type="application/x-www-form-urlencoded",
    )

    assert retried.get_json()["error"] == 0, retried.get_json()
    if bot:
        assert (sent.pushes, sent.email_tasks) == ([_push(customer, order, payment)], [])
    else:
        assert (sent.pushes, sent.email_tasks) == ([], [((payment.id,), {})])


# --------------------------------------------------------------------------- 4. reconcile decides on locked rows


@pytest.fixture
def overlap(pg_world, pg_db, sent, monkeypatch, lock_point):
    """A Telegram-less customer's PENDING Click order, prepared on the real webhook and old enough for the
    reconcile; Click's COMPLETE for it ready on its own thread, with the signal its payment row lock gives
    (`payment_lock`); every `_handle_successful_payment` run counted by the thread that ran it."""
    customer = _customer_pg(pg_db, "+998901251021")
    _reach(pg_db, customer, "email")
    order = _order(pg_db, customer, status=OrderStatus.PENDING, method=PaymentMethod.CLICK)
    payment_id = order.payment.id
    form = {
        "click_trans_id": "962001",
        "merchant_trans_id": order.order_number,
        "amount": "15000",
        "secret_key": TEST_CLICK_SHOP_SECRET_KEY,
    }
    prepare = pg_world.test_client().post(
        WEBHOOK_URL, data=make_click_webhook_form(action="0", **form), content_type="application/x-www-form-urlencoded"
    )
    assert prepare.get_json()["error"] == 0, prepare.get_json()
    payment = pg_db.session.get(Payment, payment_id)
    payment.created_at = datetime.now(UTC) - timedelta(minutes=30)
    pg_db.session.commit()
    complete_form = make_click_webhook_form(
        action="1", **form, merchant_prepare_id=str(payment_id), click_paydoc_id="962001001"
    )
    answers, errors, seen, handled = {}, [], {}, []

    def post_complete():
        response = pg_world.test_client().post(
            WEBHOOK_URL, data=dict(complete_form), content_type="application/x-www-form-urlencoded"
        )
        return response.get_json()

    complete = _on_its_own_thread("click-complete", pg_world, post_complete, answers, errors)
    handle = PaymentService._handle_successful_payment

    def counted(self, payment, **kwargs):
        handled.append(threading.current_thread().name)
        return handle(self, payment, **kwargs)

    monkeypatch.setattr(PaymentService, "_handle_successful_payment", counted)
    return SimpleNamespace(
        order_id=order.id,
        payment_id=payment_id,
        complete=complete,
        payment_lock=lock_point("click-complete", "payments"),
        answers=answers,
        errors=errors,
        seen=seen,
        handled=handled,
        handle=counted,
    )


def _click_says_completed(self, payment):
    """Click's merchant status API (external I/O): the payment went through."""
    return {"status": "completed", "provider_transaction_id": "962001001", "raw": {"payment_status": 2}}


class TestAReconcileOverlappingAComplete:
    """`check_payment_status` read the payment unlocked, so a reconcile overlapping an in-flight COMPLETE ran
    `_handle_successful_payment` a second time and confirmed the payment twice. It now re-reads the payment FOR UPDATE
    before deciding (Payment, then Order, the webhook's own lock order): it waits for a COMPLETE that holds the row and
    then sees it COMPLETED, or completes it alone while the COMPLETE waits and then answers "already paid"."""

    @staticmethod
    def _one_completion_one_confirmation(pg_db, sent, world, result):
        assert world.errors == []
        assert result["errors"] == 0
        assert sent.email_tasks == [((world.payment_id,), {})]
        pg_db.session.expire_all()
        payment = pg_db.session.get(Payment, world.payment_id)
        assert (payment.status, PaymentService.payment_confirmation_dispatched(payment)) == (
            PaymentStatus.COMPLETED,
            True,
        )
        assert pg_db.session.get(Order, world.order_id).status == OrderStatus.CONFIRMED

    def test_a_complete_that_commits_while_the_reconcile_asks_click_is_not_completed_again(
        self, pg_db, sent, overlap, monkeypatch
    ):
        world = overlap

        def click_answers_while_the_complete_lands(self, payment):
            if not world.complete.ident:
                world.complete.start()
                world.seen["complete"] = _committed_or_queued(world.complete, world.payment_lock)
            return _click_says_completed(self, payment)

        monkeypatch.setattr(ClickPaymentProviderService, "check_payment_status", click_answers_while_the_complete_lands)
        try:
            result = reconcile_pending_payments.run()
        finally:
            _finish(*([world.complete] if world.complete.ident else []))

        assert world.seen == {"complete": "committed"}
        assert world.answers["click-complete"]["error"] == 0
        assert world.handled == ["click-complete"], "the reconcile completed a payment the webhook had completed"
        self._one_completion_one_confirmation(pg_db, sent, world, result)

    def test_a_complete_that_lands_while_the_reconcile_completes_waits_and_is_already_paid(
        self, pg_db, sent, overlap, monkeypatch
    ):
        world = overlap
        counted = world.handle

        def the_complete_lands_now(self, payment, **kwargs):
            if not world.complete.ident:
                world.complete.start()
                world.seen["complete"] = _committed_or_queued(world.complete, world.payment_lock)
            return counted(self, payment, **kwargs)

        monkeypatch.setattr(ClickPaymentProviderService, "check_payment_status", _click_says_completed)
        monkeypatch.setattr(PaymentService, "_handle_successful_payment", the_complete_lands_now)
        try:
            result = reconcile_pending_payments.run()
        finally:
            _finish(*([world.complete] if world.complete.ident else []))

        assert world.seen == {"complete": "queued"}, "the COMPLETE did not wait for the reconcile's lock on the payment"
        assert world.answers["click-complete"]["error"] == -4
        assert world.handled == ["MainThread"]
        self._one_completion_one_confirmation(pg_db, sent, world, result)


    def test_a_gateway_cancel_never_overwrites_a_complete_that_committed_behind_the_reconciles_read(
        self, pg_db, sent, overlap, monkeypatch
    ):
        """The reconcile's other write: Click reports an earlier attempt cancelled, on an order cancelled meanwhile,
        while the customer's COMPLETE commits. The reconcile decides on the locked row, sees the payment COMPLETED,
        and writes nothing over it."""
        world = overlap
        pg_db.session.get(Order, world.order_id).status = OrderStatus.CANCELLED
        pg_db.session.commit()

        def click_reports_a_cancel_while_the_complete_lands(self, payment):
            if not world.complete.ident:
                world.complete.start()
                world.seen["complete"] = _committed_or_queued(world.complete, world.payment_lock)
            return {"status": "cancelled", "error_note": "Cancelled by the payer", "raw": {"payment_status": -99}}

        monkeypatch.setattr(
            ClickPaymentProviderService, "check_payment_status", click_reports_a_cancel_while_the_complete_lands
        )
        try:
            result = reconcile_pending_payments.run()
        finally:
            _finish(*([world.complete] if world.complete.ident else []))

        assert world.errors == []
        assert world.seen == {"complete": "committed"}
        assert (world.answers["click-complete"]["error"], result["errors"]) == (0, 0)
        assert world.handled == ["click-complete"]
        pg_db.session.expire_all()
        payment = pg_db.session.get(Payment, world.payment_id)
        assert (payment.status, payment.failure_reason) == (PaymentStatus.COMPLETED, None)


# --------------------------------------------------------------------------- 5. the late-debit credit notice


def _cancelled_order_with_a_cancelled_click_payment(db, order):
    order.status = OrderStatus.CANCELLED
    payment = _seed_click_payment(db, order)
    payment.status = PaymentStatus.CANCELLED
    db.session.commit()
    return payment


def _late_debit(client, order, payment, trans_id):
    """A genuine Click debit (COMPLETE, error 0, both Click ids) landing on the cancelled order's payment."""
    return client.post(
        WEBHOOK_URL,
        data=make_click_webhook_form(
            action="1",
            click_trans_id=trans_id,
            merchant_trans_id=order.order_number,
            amount=str(int(order.total_amount)),
            secret_key=TEST_CLICK_SHOP_SECRET_KEY,
            merchant_prepare_id=str(payment.id),
            click_paydoc_id=f"{trans_id}001",
        ),
        content_type="application/x-www-form-urlencoded",
    )


@pytest.mark.parametrize(
    "bot, email, channels",
    [
        ("blocked", REAL_EMAIL, [NotificationChannel.EMAIL]),
        ("blocked", PLACEHOLDER_EMAIL, None),
        ("connected", REAL_EMAIL, [NotificationChannel.TELEGRAM]),
    ],
    ids=["inactive-bot-real-email", "neither", "connected-bot"],
)
def test_a_late_debit_credit_notice_uses_the_one_channel_rule(
    matrix_client,
    db,
    sample_user,
    order_with_address,
    no_fiscalization,
    late_debit_notices,
    monkeypatch,
    bot,
    email,
    channels,
):
    """A genuine Click debit lands on a cancelled order: the money becomes the customer's prepaid balance and they
    are told so, by their one channel, when the worker runs the published notice. The customer once switched SMS on
    for this notice; that preference is never read (SMS is OTP-only)."""
    customer = _customer(db, sample_user, bot=bot, email=email)
    db.session.add(
        NotificationPreference(
            user_id=customer.id,
            notification_type="payment_late_debit_credited",
            channel=NotificationChannel.SMS,
            is_enabled=True,
        )
    )
    order = order_with_address
    payment = _cancelled_order_with_a_cancelled_click_payment(db, order)

    response = _late_debit(matrix_client, order, payment, "963001")

    assert response.get_json()["error"] == -4, response.get_json()
    amount = str(ClickPaymentProviderService._normalize_amount(str(int(order.total_amount))))
    assert late_debit_notices.published == [((customer.id, order.order_number, amount), {})]
    notices = _record_notices(monkeypatch)
    late_debit_notices.run(*late_debit_notices.published[0][0])
    expected_data = {"order_number": order.order_number, "amount": amount}
    assert notices == ([] if channels is None else [(customer.id, "payment_late_debit_credited", channels, expected_data)])


def test_the_late_debit_credit_notice_is_published_only_after_the_credit_commits(
    pg_world, pg_db, late_debit_notices
):
    """At the instant the notice is published, a connection of its own already reads the credit (the prepaid
    balance the debit became): the notice is no longer sent inside the COMPLETE's transaction, under its row locks."""
    customer = _customer_pg(pg_db, "+998901251031")
    _reach(pg_db, customer, "email")
    order = _order(pg_db, customer, status=OrderStatus.CANCELLED, method=PaymentMethod.CLICK)
    payment = order.payment
    payment.status = PaymentStatus.CANCELLED
    pg_db.session.commit()

    def the_credit_as_committed():
        with Session(pg_db.engine) as fresh:
            credit = fresh.query(CashCollectionEvent).filter_by(idempotency_key="click-late-debit:965001").one_or_none()
            return credit.amount if credit is not None else None

    late_debit_notices.on_publish = the_credit_as_committed

    response = _late_debit(pg_world.test_client(), order, payment, "965001")

    assert response.get_json()["error"] == -4, response.get_json()
    assert late_debit_notices.published == [((customer.id, order.order_number, "15000.00"), {})]
    assert late_debit_notices.at_publish == [Decimal("15000.00")]


def test_a_rolled_back_late_debit_credit_notifies_nobody(pg_world, pg_db, late_debit_notices, monkeypatch):
    """The COMPLETE's transaction fails after the credit was booked and the notice parked (here: the callback audit
    raises before the commit). Click is answered 503, no credit is kept, and nobody is told they were credited.
    Postgres: on the SQLite suite the audit logger's own session shares the one connection and commits the credit."""
    customer = _customer_pg(pg_db, "+998901251032")
    _reach(pg_db, customer, "email")
    order = _order(pg_db, customer, status=OrderStatus.CANCELLED, method=PaymentMethod.CLICK)
    payment = order.payment
    payment.status = PaymentStatus.CANCELLED
    pg_db.session.commit()

    def the_commit_never_comes(self, payment, **kwargs):
        raise RuntimeError("the database went away before the commit")

    monkeypatch.setattr(ClickPaymentProviderService, "_append_callback_audit", the_commit_never_comes)

    response = _late_debit(pg_world.test_client(), order, payment, "965101")

    assert response.status_code == 503, response.get_data(as_text=True)
    assert late_debit_notices.published == []
    with Session(pg_db.engine) as fresh:
        assert fresh.query(CashCollectionEvent).filter_by(idempotency_key="click-late-debit:965101").count() == 0


# --------------------------------------------------------------------------- 6. a bot push the bot did not take


def test_a_bot_push_the_bot_did_not_take_is_resent_by_the_sweep(
    matrix_client, db, sample_user, order_with_address, no_fiscalization, monkeypatch
):
    """`trigger_bot_webhook` never raises: a bot that is down or timed out answers {"success": False}. That push did
    not leave, so the payment is unmarked, and the side-effect sweep sends it again with the same request id (the bot
    collapses a push that had really been delivered)."""
    customer = _customer(db, sample_user, bot="connected", email=PLACEHOLDER_EMAIL)
    order = order_with_address
    payment = _seed_click_payment(db, order)
    answers = [{"success": False, "message": "Bot webhook timeout"}, {"success": True, "message": "Notification sent"}]
    pushes = []

    def trigger_bot_webhook(endpoint, payload=None, *, request_id=None):
        pushes.append((endpoint, request_id, dict(payload or {})))
        return answers[len(pushes) - 1]

    monkeypatch.setattr("business_app.utils.bot_webhook.trigger_bot_webhook", trigger_bot_webhook)

    _pay_by_click(matrix_client, order, "966001")

    assert pushes == [_push(customer, order, payment)]
    db.session.expire_all()
    assert PaymentService.payment_confirmation_dispatched(db.session.get(Payment, payment.id)) is False

    # The sweep's first run once the payment is older than the floor it leaves to the completion itself.
    later = datetime.now(UTC) + payment_tasks.CONFIRMATION_REDISPATCH_FLOOR + timedelta(minutes=1)
    monkeypatch.setattr(payment_tasks, "get_utc_now", lambda: later)
    swept = reconcile_completed_payment_side_effects.run()

    assert (swept["scanned"], swept["confirmation_redispatched"]) == (1, 1)
    assert pushes == [_push(customer, order, payment)] * 2
    db.session.expire_all()
    assert PaymentService.payment_confirmation_dispatched(db.session.get(Payment, payment.id)) is True


def _bot_answers(monkeypatch, answer):
    """The HTTP hop to the customer bot answering `answer` (`trigger_bot_webhook`'s shape for it), and the Celery publish
    of the email task (bound against the task's `run`): each recorded, never sent."""
    state = SimpleNamespace(pushes=[], email_tasks=[])
    task = notification_tasks.send_payment_confirmation_task
    signature = inspect.signature(task.run)

    def trigger_bot_webhook(endpoint, payload=None, *, request_id=None):
        state.pushes.append((endpoint, request_id, dict(payload or {})))
        return dict(answer)

    def delay(*args, **kwargs):
        signature.bind(*args, **kwargs)
        state.email_tasks.append((args, kwargs))

    monkeypatch.setattr("business_app.utils.bot_webhook.trigger_bot_webhook", trigger_bot_webhook)
    monkeypatch.setitem(vars(task), "delay", delay)
    return state


def _sweep_after_the_floor(monkeypatch):
    """Two runs of the side-effect sweep, once the payment is older than the floor it leaves to the completion."""
    later = datetime.now(UTC) + payment_tasks.CONFIRMATION_REDISPATCH_FLOOR + timedelta(minutes=1)
    monkeypatch.setattr(payment_tasks, "get_utc_now", lambda: later)
    return [reconcile_completed_payment_side_effects.run() for _ in range(2)]


DISPATCH_LOGGER = "business_app.utils.payment_confirmation_dispatch"


@pytest.fixture
def dispatch_records():
    """Every record the dispatch module logs, from WARNING up. Its logger does not reach caplog's root handler once the
    app's logging is set up, and the test config logs from ERROR up, so the recorder sits on the logger itself, which is
    opened to WARNING for the test."""
    records = []
    handler = logging.Handler(level=logging.WARNING)
    handler.emit = records.append
    dispatch_logger = logging.getLogger(DISPATCH_LOGGER)
    level = dispatch_logger.level
    dispatch_logger.addHandler(handler)
    dispatch_logger.setLevel(logging.WARNING)
    try:
        yield records
    finally:
        dispatch_logger.removeHandler(handler)
        dispatch_logger.setLevel(level)


def _blocked_as_a_customer_notice_marks_it(db, customer, before):
    db.session.expire_all()
    user = db.session.get(User, customer.id)
    assert user.is_bot_active is False
    assert before <= ensure_utc(user.telegram_blocked_at) <= datetime.now(UTC)
    assert event_rows(user=user) == [("blocked", "send_410", None, None)]


@pytest.mark.parametrize("email, emailed", [(REAL_EMAIL, True), (PLACEHOLDER_EMAIL, False)], ids=["email", "none"])
def test_a_push_the_bot_refused_because_the_customer_blocked_it_goes_by_email_in_the_same_drain(
    matrix_client, db, sample_user, order_with_address, no_fiscalization, monkeypatch, email, emailed
):
    """Live drive D1: the bot answers 410 BOT_BLOCKED when Telegram refuses the push because the customer blocked the
    bot. As for a customer notice, the account is marked blocked (`send_410`) and the one channel resolved again, in
    the same drain: the email task for a real address, otherwise nothing. Either way the confirmation is settled, so
    the payment keeps its mark and no sweep pushes Telegram again or sends a second email."""
    customer = _customer(db, sample_user, bot="connected", email=email)
    order = order_with_address
    payment = _seed_click_payment(db, order)
    bot = _bot_answers(monkeypatch, BOT_BLOCKED)
    before = datetime.now(UTC)

    _pay_by_click(matrix_client, order, "966101")

    _blocked_as_a_customer_notice_marks_it(db, customer, before)
    assert (bot.pushes, bot.email_tasks) == ([_push(customer, order, payment)], [((payment.id,), {})] if emailed else [])
    assert PaymentService.payment_confirmation_dispatched(db.session.get(Payment, payment.id)) is True

    swept = _sweep_after_the_floor(monkeypatch)

    assert [run["confirmation_redispatched"] for run in swept] == [0, 0]
    assert (bot.pushes, bot.email_tasks) == ([_push(customer, order, payment)], [((payment.id,), {})] if emailed else [])


def test_a_blocked_customer_is_emailed_on_a_rail_no_sweep_re_sends(db, sample_user, order_with_address, monkeypatch):
    """A Payme confirmation is never swept (`FISCALIZED_RAILS` is Click and card), so the email has to leave in the
    drain of the commit that confirmed the payment."""
    customer = _customer(db, sample_user, bot="connected", email=REAL_EMAIL)
    order = order_with_address
    payment = _seed_click_payment(db, order)
    payment.payment_method = order.payment_method = PaymentMethod.PAYME
    payment.status = PaymentStatus.COMPLETED
    db.session.commit()
    bot = _bot_answers(monkeypatch, BOT_BLOCKED)
    before = datetime.now(UTC)

    assert PaymentService().dispatch_payment_confirmation(payment) is True
    db.session.commit()

    _blocked_as_a_customer_notice_marks_it(db, customer, before)
    assert (bot.pushes, bot.email_tasks) == ([_push(customer, order, payment)], [((payment.id,), {})])
    assert PaymentService.payment_confirmation_dispatched(db.session.get(Payment, payment.id)) is True


def test_a_push_the_bot_failed_to_send_leaves_the_customer_connected_and_the_sweep_pushes_it_again(
    matrix_client, db, sample_user, order_with_address, no_fiscalization, monkeypatch, dispatch_records
):
    """The live drive's answer: the bot's send failed ("Chat not found", a 500). That is no block: the customer stays
    connected and the sweep pushes the confirmation again under the same request id, which the bot no longer answers
    "Already processed" (tests/telegram_bot/test_webhook_payment_success.py). The sweep re-sends it on every run, so
    only the first failure is logged as an error with its traceback; each re-send the bot does not take either is a
    one-line warning."""
    customer = _customer(db, sample_user, bot="connected", email=REAL_EMAIL)
    order = order_with_address
    payment = _seed_click_payment(db, order)
    bot = _bot_answers(monkeypatch, BOT_DOWN)

    _pay_by_click(matrix_client, order, "966102")
    swept = _sweep_after_the_floor(monkeypatch)

    db.session.expire_all()
    user = db.session.get(User, customer.id)
    assert (user.is_bot_active, user.telegram_blocked_at, event_rows(user=user)) == (True, None, [])
    # The bot is still down for both sweeps, so each pushes again and leaves the payment unmarked for the next.
    assert [run["confirmation_redispatched"] for run in swept] == [1, 1]
    assert (bot.pushes, bot.email_tasks) == ([_push(customer, order, payment)] * 3, [])
    assert PaymentService.payment_confirmation_dispatched(db.session.get(Payment, payment.id)) is False
    logged = [(record.levelno, record.getMessage(), record.exc_info is not None) for record in dispatch_records]
    again = (
        f"Payment confirmation for payment {payment.id} not delivered again: the customer bot did not take it: "
        f"{BOT_DOWN['message']}"
    )
    assert logged == [
        (logging.ERROR, f"Failed to dispatch payment confirmation for payment {payment.id}", True),
        (logging.WARNING, again, False),
        (logging.WARNING, again, False),
    ]


# --------------------------------------------------------------------------- 7. a reconcile holding a stale copy


# What the customer does while the reconcile looks up their Click payment id, and what Click's status API then says.
STALE_COPY = {
    "paid_and_completed": (True, 2),
    "completed_while_click_still_says_processing": (True, 1),
    "paid_before_its_complete_arrives": (False, 2),
}


@pytest.mark.parametrize("scenario", sorted(STALE_COPY))
def test_a_reconcile_holding_a_stale_copy_never_overwrites_what_the_webhook_committed(
    pg_world, pg_db, sent, monkeypatch, scenario
):
    """The reconcile loads every PENDING payment before its loop. This customer had opened the link but not paid, so
    their copy has no Click ids; while the reconcile asks Click for the id (status_by_mti), they pay: Prepare and
    Complete commit (or only Prepare, Complete arriving after the reconcile). Writing the discovered id from the
    stale copy used to wipe the winning click_trans_id, the complete payload and the confirmation mark; with Click
    then saying "paid", the real COMPLETE read as a second charge and the customer's only payment was reversed."""
    pays_now, click_status = STALE_COPY[scenario]
    monkeypatch.setitem(pg_world.config, "CLICK_TEST_MODE", False)
    customer = _customer_pg(pg_db, "+998901251041")
    _reach(pg_db, customer, "email")
    order = _order(pg_db, customer, status=OrderStatus.PENDING, method=PaymentMethod.CLICK)
    payment_id = order.payment.id
    order.payment.created_at = datetime.now(UTC) - timedelta(minutes=30)
    pg_db.session.commit()
    form = {
        "click_trans_id": "967001",
        "merchant_trans_id": order.order_number,
        "amount": "15000",
        "secret_key": TEST_CLICK_SHOP_SECRET_KEY,
        "click_paydoc_id": "967001001",
    }
    complete_form = {**form, "merchant_prepare_id": str(payment_id)}

    def post(client, action, body):
        response = client.post(
            WEBHOOK_URL,
            data=make_click_webhook_form(action=action, **body),
            content_type="application/x-www-form-urlencoded",
        )
        return response.get_json()["error"]

    def the_customer_pays():
        client = pg_world.test_client()
        paid = {"prepare": post(client, "0", form)}
        if pays_now:
            paid["complete"] = post(client, "1", complete_form)
        return paid

    answers, errors, handled, reversals = {}, [], [], []
    webhook = _on_its_own_thread("click-webhook", pg_world, the_customer_pays, answers, errors)

    def click_merchant_api(
        self, payload=None, *, configured_url=None, fallback_path, method="POST", endpoint_label=None,
        expect_error_code=True,
    ):
        """Click's merchant API (external I/O). The customer pays while the reconcile looks their id up."""
        if endpoint_label == "payment_status_by_mti":
            if not webhook.ident:
                webhook.start()
                _finish(webhook)
            return {"error_code": 0, "payment_id": 967001001}
        if endpoint_label == "payment_status":
            return {"error_code": 0, "payment_status": click_status, "payment_id": 967001001}
        raise AssertionError(f"unexpected Click call {endpoint_label}")

    handle = PaymentService._handle_successful_payment

    def counted(self, payment, **kwargs):
        handled.append(threading.current_thread().name)
        return handle(self, payment, **kwargs)

    reverse = payment_tasks.reverse_click_payment_task
    signature = inspect.signature(reverse.run)

    def reversal(*args, **kwargs):
        signature.bind(*args, **kwargs)
        reversals.append((args, kwargs))

    monkeypatch.setattr(ClickPaymentProviderService, "merchant_request", click_merchant_api)
    monkeypatch.setattr(PaymentService, "_handle_successful_payment", counted)
    monkeypatch.setattr(payment_tasks, "reverse_click_payment_task", SimpleNamespace(delay=reversal))

    result = reconcile_pending_payments.run()
    if not pays_now:
        answers["late-complete"] = post(pg_world.test_client(), "1", complete_form)

    assert (errors, result["errors"]) == ([], 0)
    if pays_now:
        assert answers == {"click-webhook": {"prepare": 0, "complete": 0}}
    else:
        assert answers == {"click-webhook": {"prepare": 0}, "late-complete": -4}
    assert handled == (["click-webhook"] if pays_now else ["MainThread"]), "one completion, by whoever paid first"
    assert reversals == []
    assert PaymentTransaction.query.filter_by(payment_id=payment_id, transaction_type="click_duplicate_charge").count() == 0
    pg_db.session.expire_all()
    payment = pg_db.session.get(Payment, payment_id)
    click = payment.provider_data["click"]
    assert (payment.status, click["click_trans_id"], click["click_paydoc_id"]) == (
        PaymentStatus.COMPLETED,
        "967001",
        "967001001",
    )
    assert PaymentService.payment_confirmation_dispatched(payment) is True
    if pays_now:
        assert click["complete_payload"]["click_trans_id"] == "967001"
    assert sent.email_tasks == [((payment_id,), {})]


# --------------------------------------------------------------------------- 8. the rest of the money's writers


def test_a_verification_never_fails_a_payment_a_complete_committed_behind_its_read(
    pg_db, sent, overlap, monkeypatch
):
    """`process_payment_verification` decides its FAILED write on the locked row: a COMPLETE that commits while it
    asks Click, on an order cancelled meanwhile, keeps its COMPLETED payment. The verification attempt is still
    recorded."""
    world = overlap
    pg_db.session.get(Order, world.order_id).status = OrderStatus.CANCELLED
    pg_db.session.commit()

    def click_still_says_pending_while_the_complete_lands(self, payment):
        if not world.complete.ident:
            world.complete.start()
            world.seen["complete"] = _committed_or_queued(world.complete, world.payment_lock)
        return {"status": "pending", "raw": {"payment_status": 1}}

    monkeypatch.setattr(
        ClickPaymentProviderService, "check_payment_status", click_still_says_pending_while_the_complete_lands
    )
    try:
        result = process_payment_verification.run(world.payment_id)
    finally:
        _finish(*([world.complete] if world.complete.ident else []))

    assert world.errors == []
    assert world.seen == {"complete": "committed"}
    assert world.answers["click-complete"]["error"] == 0
    assert result == {"success": False, "error": "Verification failed"}
    assert world.handled == ["click-complete"]
    pg_db.session.expire_all()
    payment = pg_db.session.get(Payment, world.payment_id)
    assert (payment.status, payment.failure_reason) == (PaymentStatus.COMPLETED, None)
    assert [txn.status for txn in PaymentTransaction.query.filter_by(transaction_type="verification")] == ["failed"]


def test_moving_a_paid_business_account_order_to_click_clears_its_confirmation_mark(pg_world, pg_db, sent):
    """The business-account payment was confirmed when the order was placed. Moved to Click, the payment waits for new
    money, so its old mark goes: a Click confirmation that later fails is then re-driven, not hidden by it."""
    order_id, _customer_id = _held_agent_order_on_click(pg_world, pg_db)

    pg_db.session.expire_all()
    payment = pg_db.session.get(Order, order_id).payment
    assert (payment.payment_method, payment.status, PaymentService.payment_confirmation_dispatched(payment)) == (
        PaymentMethod.CLICK,
        PaymentStatus.PENDING,
        False,
    )
