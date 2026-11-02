"""One payment confirmation per payment, by the customer's channel (spec 2026-10-27 §10, D21).

A paid order is confirmed once, through the customer's one channel: the bot when it is connected, else a real
email, else nothing (`NotificationService._resolve_telegram_else_email`). That holds for every order, wherever it
was placed: a subscription order (`order_source` "web") of a Telegram customer used to get a bot push AND a
second Telegram message from the email task, and a customer who blocked the bot was still pushed to Telegram.
The payment is marked confirmed even when no channel exists, so no sweep or reconcile task sends it later. The
emails link the order's tracking page (`order_tracking_url`), which used to be empty.

Driven the way the money arrives: Click's Prepare then Complete on the real webhook, and an admin's cash
collection on the real route. Only external I/O is replaced: the HTTP hop to the customer bot (a recorder with
`trigger_bot_webhook`'s own signature), the Celery publish of the email task (bound against the task's `run`),
Redis' dedupe key, and the provider POST (Telegram sendMessage, Brevo).
"""

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.payment import Payment
from business_app.models.subscription import Subscription
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.order_service import OrderService
from business_app.services.payment_service import PaymentService
from business_app.tasks import notification_tasks
from business_app.tasks.payment_tasks import process_payment_confirmation, reconcile_completed_payment_side_effects
from shared.enums import (
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    SubscriptionFrequency,
    SubscriptionStatus,
    UserStatus,
)
from shared.validators import PLACEHOLDER_EMAIL_DOMAIN_LOCAL
from tests.integration.fake_gateways import TEST_CLICK_SHOP_SECRET_KEY, make_click_webhook_form
from tests.integration.test_payment_matrix import _seed_click_payment
from tests.notification_send_fakes import BREVO_URL, live_send_path
from tests.unit.test_payment_confirmation_collection_scoped import _cash_order

pytestmark = pytest.mark.integration

WEBHOOK_URL = "/api/v1/payments/webhook/click"
COLLECTIONS_URL = "/api/v1/admin/staff/cash-reconciliation/collections"
PAYMENT_SUCCESS = "/internal/payment-success"
SITE = "https://aqua-element.example"
BOT_URL = "https://t.me/aqua_test_bot"
BOT_TOKEN = "customer-bot-token"
CHAT_ID = "810000117"
REAL_EMAIL = "test@example.com"
PLACEHOLDER_EMAIL = f"telegram_{CHAT_ID}@{PLACEHOLDER_EMAIL_DOMAIN_LOCAL}"
TELEGRAM_SEND = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


@pytest.fixture
def channels(monkeypatch):
    """The two ways a payment confirmation leaves the backend: the HTTP hop to the customer bot, and the Celery
    publish of the email task. Each is recorded, never sent."""
    state = SimpleNamespace(pushes=[], email_tasks=[])

    def trigger_bot_webhook(endpoint, payload=None, *, request_id=None):
        state.pushes.append((endpoint, request_id, dict(payload or {})))
        return {"success": True, "message": "Notification sent"}

    task = notification_tasks.send_payment_confirmation_task
    signature = inspect.signature(task.run)

    def delay(*args, **kwargs):
        signature.bind(*args, **kwargs)
        state.email_tasks.append((args, kwargs))

    monkeypatch.setattr("business_app.utils.bot_webhook.trigger_bot_webhook", trigger_bot_webhook)
    # The instance `__dict__`, never setattr: the undo leaves no stale publish on the task.
    monkeypatch.setitem(vars(task), "delay", delay)
    return state


@pytest.fixture
def site(app, monkeypatch):
    monkeypatch.setitem(app.config, "COMPANY_WEBSITE", f"{SITE}/")
    return SITE


def _customer(db, user, *, bot, email):
    """`bot` is "connected", "blocked" or None (never linked)."""
    user.telegram_id = CHAT_ID if bot else None
    user.is_bot_active = bot == "connected"
    user.email = email
    user.preferred_language = "en"
    db.session.commit()
    return user


def _pay_by_click(client, order, trans_id):
    """Click's Prepare then Complete for the whole order, as Click sends them."""
    amount = str(int(order.total_amount))
    prepare = client.post(
        WEBHOOK_URL,
        data=make_click_webhook_form(
            action="0",
            click_trans_id=trans_id,
            merchant_trans_id=order.order_number,
            amount=amount,
            secret_key=TEST_CLICK_SHOP_SECRET_KEY,
        ),
        content_type="application/x-www-form-urlencoded",
    )
    assert prepare.get_json()["error"] == 0, prepare.get_json()
    complete = client.post(
        WEBHOOK_URL,
        data=make_click_webhook_form(
            action="1",
            click_trans_id=trans_id,
            merchant_trans_id=order.order_number,
            amount=amount,
            secret_key=TEST_CLICK_SHOP_SECRET_KEY,
            merchant_prepare_id=str(prepare.get_json()["merchant_prepare_id"]),
        ),
        content_type="application/x-www-form-urlencoded",
    )
    assert complete.get_json()["error"] == 0, complete.get_json()


def _push(user, order, payment):
    """The one bot push a paid order gets, with the bot's own dedupe id."""
    return (
        PAYMENT_SUCCESS,
        f"payment-confirm:{payment.id}",
        {
            "user_id": user.id,
            "telegram_id": CHAT_ID,
            "order_id": order.id,
            "order_number": order.order_number,
            "amount": float(order.total_amount),
            "currency": "UZS",
        },
    )


def _marked_at(db, payment):
    db.session.expire_all()
    stamp = db.session.get(Payment, payment.id).provider_data["post_payment"]["confirmation_enqueued_at"]
    return datetime.fromisoformat(stamp)


def _subscription_for(db, user, address):
    subscription = Subscription(
        user_id=user.id,
        name="Water",
        status=SubscriptionStatus.ACTIVE,
        frequency=SubscriptionFrequency.WEEKLY,
        delivery_day_of_week=3,
        delivery_address_id=address.id,
        payment_method=PaymentMethod.CLICK,
        billing_amount=Decimal("18000.00"),
        start_date=datetime.now(UTC),
    )
    db.session.add(subscription)
    db.session.commit()
    return subscription


def _run_email_task(app, monkeypatch, call):
    """Run a recorded email-task publish for real; only Redis' dedupe key and the provider POST are faked. The bot
    token is configured, so a Telegram send would show up as its own POST."""
    args, kwargs = call
    fake_redis = MagicMock()
    fake_redis.get.return_value = None
    with patch("business_app.redis_client", fake_redis), live_send_path(
        app, monkeypatch, bot_url=BOT_URL, extra_config={"TELEGRAM_BOT_TOKEN": BOT_TOKEN}
    ) as post:
        result = notification_tasks.send_payment_confirmation_task.run(*args, **kwargs)
    return result, post


# --------------------------------------------------------------------------- the online payment (Click)


def test_a_telegram_customer_paying_a_subscription_order_gets_one_bot_message(
    matrix_client, db, sample_user, order_with_address, sample_address, no_fiscalization, channels
):
    """A subscription order is placed by the engine with `order_source` "web"; its customer is on Telegram."""
    customer = _customer(db, sample_user, bot="connected", email=REAL_EMAIL)
    order = order_with_address
    order.subscription_id = _subscription_for(db, customer, sample_address).id
    db.session.commit()
    payment = _seed_click_payment(db, order)
    before = datetime.now(UTC)

    _pay_by_click(matrix_client, order, "917001")

    assert (channels.pushes, channels.email_tasks) == ([_push(customer, order, payment)], [])
    assert before <= _marked_at(db, payment) <= datetime.now(UTC)


def test_a_telegram_customer_paying_a_bot_order_gets_the_same_one_message(
    matrix_client, db, sample_user, order_with_address, no_fiscalization, channels
):
    customer = _customer(db, sample_user, bot="connected", email=PLACEHOLDER_EMAIL)
    order = order_with_address
    order.order_source = "telegram"
    db.session.commit()
    payment = _seed_click_payment(db, order)

    _pay_by_click(matrix_client, order, "917002")

    assert (channels.pushes, channels.email_tasks) == ([_push(customer, order, payment)], [])


def test_an_email_customer_gets_one_email_linking_the_order_page(
    app, matrix_client, db, sample_user, order_with_address, no_fiscalization, channels, site, monkeypatch
):
    customer = _customer(db, sample_user, bot=None, email=REAL_EMAIL)
    order = order_with_address
    payment = _seed_click_payment(db, order)

    _pay_by_click(matrix_client, order, "917003")

    assert (channels.pushes, channels.email_tasks) == ([], [((payment.id,), {})])
    _result, post = _run_email_task(app, monkeypatch, channels.email_tasks[0])
    assert [call.args for call in post.call_args_list] == [(BREVO_URL,)]
    sent = post.call_args.kwargs["json"]
    assert sent["to"] == [{"email": REAL_EMAIL, "name": f"{customer.first_name} {customer.last_name}"}]
    assert f'href="{site}/order-tracking?order_id={order.id}"' in sent["htmlContent"]


def test_a_blocked_bot_with_a_real_email_gets_the_email(
    app, matrix_client, db, sample_user, order_with_address, no_fiscalization, channels, monkeypatch
):
    """The customer ordered in the bot and blocked it afterwards: their real address is the one channel."""
    _customer(db, sample_user, bot="blocked", email=REAL_EMAIL)
    order = order_with_address
    order.order_source = "telegram"
    db.session.commit()
    payment = _seed_click_payment(db, order)

    _pay_by_click(matrix_client, order, "917004")

    assert (channels.pushes, channels.email_tasks) == ([], [((payment.id,), {})])
    _result, post = _run_email_task(app, monkeypatch, channels.email_tasks[0])
    assert [call.args for call in post.call_args_list] == [(BREVO_URL,)]


def test_no_channel_sends_nothing_and_still_marks_the_payment(
    matrix_client, db, sample_user, order_with_address, no_fiscalization, channels
):
    """A bot sign-up (placeholder email) who blocked the bot: nobody to tell. The payment is still marked, so
    the side-effect sweep does not try again."""
    _customer(db, sample_user, bot="blocked", email=PLACEHOLDER_EMAIL)
    order = order_with_address
    order.order_source = "telegram"
    db.session.commit()
    payment = _seed_click_payment(db, order)
    before = datetime.now(UTC)

    _pay_by_click(matrix_client, order, "917005")

    assert (channels.pushes, channels.email_tasks) == ([], [])
    assert before <= _marked_at(db, payment) <= datetime.now(UTC)
    swept = reconcile_completed_payment_side_effects.run()
    assert (swept["scanned"], swept["confirmation_redispatched"]) == (1, 0)  # in the sweep's population, not resent
    assert (channels.pushes, channels.email_tasks) == ([], [])


def test_reconciling_an_already_confirmed_payment_sends_nothing_more(
    matrix_client, db, sample_user, order_with_address, no_fiscalization, channels
):
    """A Telegram customer's website order. After Click's Complete, the reconcile chain (the confirmation task
    the pending-payment reconcile publishes, and the side-effect sweep) adds no second message."""
    customer = _customer(db, sample_user, bot="connected", email=REAL_EMAIL)
    order = order_with_address
    payment = _seed_click_payment(db, order)
    _pay_by_click(matrix_client, order, "917006")

    confirmed = process_payment_confirmation.run(payment.id)
    swept = reconcile_completed_payment_side_effects.run()

    assert confirmed == {"success": True, "skipped": True, "reason": "already_confirmed"}
    assert (swept["scanned"], swept["confirmation_redispatched"]) == (1, 0)  # in the sweep's population, not resent
    assert (channels.pushes, channels.email_tasks) == ([_push(customer, order, payment)], [])


@pytest.mark.parametrize("bot", ["connected", None], ids=["telegram", "email"])
def test_the_sweep_running_between_a_click_completes_two_commits_sends_nothing(
    matrix_client, db, sample_user, order_with_address, no_fiscalization, channels, monkeypatch, bot
):
    """Click's COMPLETE on a pending order commits COMPLETED inside the order's confirmation (`update_order_status`)
    and writes the confirmation mark only in its next commit, after the award and the contract settlement. The
    side-effect sweep, run on its beat, reads the payment in between: completed, unmarked. A payment that new is its
    own completion's to confirm, so the sweep leaves it, and the customer gets one message (an email has no request id
    for the bot to collapse a second one on)."""
    customer = _customer(db, sample_user, bot=bot, email=REAL_EMAIL)
    order = order_with_address
    payment = _seed_click_payment(db, order)
    between, swept = [], []
    award = OrderService.maybe_award_purchase_points

    def the_sweep_runs_between_the_commits(self, awarded, *args, **kwargs):
        # The first thing the COMPLETE does after the commit that confirmed the order.
        if not swept:
            row = db.session.get(Payment, payment.id)
            between.append((row.status, PaymentService.payment_confirmation_dispatched(row)))
            swept.append(reconcile_completed_payment_side_effects.run())
        return award(self, awarded, *args, **kwargs)

    monkeypatch.setattr(OrderService, "maybe_award_purchase_points", the_sweep_runs_between_the_commits)

    _pay_by_click(matrix_client, order, "917007")

    assert between == [(PaymentStatus.COMPLETED, False)]  # the sweep really read it completed and unmarked
    assert (swept[0]["scanned"], swept[0]["confirmation_redispatched"], swept[0]["errors"]) == (1, 0, 0)
    expected = ([_push(customer, order, payment)], []) if bot else ([], [((payment.id,), {})])
    assert (channels.pushes, channels.email_tasks) == expected
    assert PaymentService.payment_confirmation_dispatched(db.session.get(Payment, payment.id)) is True


# --------------------------------------------------------------------------- a cash collection


@pytest.fixture
def admin_headers(app, db, admin_user):
    """`manager_or_higher_required` reads the role CLAIM."""
    admin_user.status = UserStatus.ACTIVE.value
    db.session.commit()
    token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _recipient(call):
    """Who a provider POST went to: Telegram's chat, or Brevo's addressee."""
    body = call.kwargs["json"]
    return body["chat_id"] if "chat_id" in body else body["to"][0]["email"]


@pytest.mark.parametrize(
    "bot, email, sent, answer",
    [
        ("connected", REAL_EMAIL, [(TELEGRAM_SEND, CHAT_ID)], "telegram"),
        ("blocked", REAL_EMAIL, [(BREVO_URL, REAL_EMAIL)], "email"),
        (None, PLACEHOLDER_EMAIL, [], None),
    ],
    ids=["bot-connected", "bot-blocked-real-email", "no-channel"],
)
def test_a_cash_collection_confirmation_follows_the_same_channel_rule(
    app, client, db, sample_user, admin_headers, channels, monkeypatch, bot, email, sent, answer
):
    """The admin records cash a customer handed over for a delivered order (the Record Collection modal's body).
    Its confirmation, published after the collection commits, goes by the same one channel as an online payment."""
    customer = _customer(db, sample_user, bot=bot, email=email)
    order = _cash_order(db, customer, number="TG_CASH_17B", total="30000.00", status=OrderStatus.DELIVERED)
    payment = CashCollectionService().ensure_cod_payment_for_order(order)
    db.session.commit()

    response = client.post(
        COLLECTIONS_URL,
        json={
            "customer_id": customer.id,
            "amount": 30000,
            "source": "standalone_meeting",
            "order_id": order.id,
            "notes": "Paid at the office",
        },
        headers=admin_headers,
    )

    assert response.status_code == 201, response.get_json()
    event_id = response.get_json()["data"]["cash_collection_event"]["id"]
    assert channels.email_tasks == [
        ((payment.id,), {"collection_state_token": "30000.00", "cash_collection_event_id": event_id})
    ]
    result, post = _run_email_task(app, monkeypatch, channels.email_tasks[0])
    assert [(call.args[0], _recipient(call)) for call in post.call_args_list] == sent
    if answer is None:
        assert result == {"skipped": True, "reason": "no_channel"}
    else:
        assert (list(result), result[answer]["success"]) == ([answer], True)
    assert channels.pushes == []


# --------------------------------------------------------------------------- the order confirmation email


def test_the_order_confirmation_email_links_to_the_tracking_page(
    app, db, sample_user, order_with_address, site, monkeypatch
):
    _customer(db, sample_user, bot=None, email=REAL_EMAIL)
    order = order_with_address

    with live_send_path(app, monkeypatch, bot_url=BOT_URL) as post:
        notification_tasks.send_order_notification_task.run(order.id, "order_created")

    assert [call.args for call in post.call_args_list] == [(BREVO_URL,)]
    assert f'href="{site}/order-tracking?order_id={order.id}"' in post.call_args.kwargs["json"]["htmlContent"]
