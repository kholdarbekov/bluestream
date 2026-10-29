"""A Payme checkout opened before the deploy can never take the money twice (spec 2026-10-27 §4.3, owner call 13;
plan 2026-10-27 Task 2a, fix rounds 1 and 2).

Payme keeps finishing a checkout it opened before the deploy: PerformTransaction still captures a charge that
CreateTransaction opened (owner call 13). Meanwhile the customer's Pay moves a legacy Payme order onto Click. If both
rails could capture, the customer would pay twice and nothing would record a credit or refund. Two guards stop it:

- the Pay tap (`POST /orders/<id>/retry-payment`) does not move a Payme payment whose Payme charge is still open
  (pending and inside `PAYME_TIMEOUT_MS`): 409 `STATE_CHANGED`, and nothing is written;
- PerformTransaction never captures a payment that is no longer open on Payme (the bot's `/payments/create` moved
  it to Click, or something other than this charge settled it): it cancels the charge, so Payme releases the payer's
  hold. A payment this very charge settled is a retried Perform, and it completes (fix round 2).

Driven through the real endpoints: the Payme JSON-RPC webhook, the Click webhook, `retry-payment` and the bot's
`/payments/create`. Only external I/O is mocked: the Payme Basic-auth check (as in `test_payment_matrix.py`) and
fiscalization. The last two tests run on PostgreSQL: the tap locks the payment row before the order, the order the
webhooks lock them in, so a tap racing a webhook waits instead of deadlocking; and a duplicate Perform waiting on the
payment row answers what the first one committed.
"""

import base64
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy import create_engine, text

from business_app.models.order import Order
from business_app.models.payment import PaymentTransaction
from business_app.models.user import User
from business_app.services.payment_service import PaymentService
from business_app.services.providers.payme_provider import PaymeProvider
from business_app.utils.constants import PaymeErrors, PaymeState
from business_app.utils.helpers import to_ms
from business_app.utils.password_security import hash_password
from business_app.utils.timezone_utils import get_utc_now
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus, UserRole, UserStatus, UserType
from tests.integration.customer_action_matrix import ROW, Row, seed
from tests.integration.fake_gateways import (
    TEST_CLICK_SHOP_SECRET_KEY,
    TEST_PAYME_SECRET_KEY,
    apply_test_provider_secrets,
    make_click_webhook_form,
    make_payme_webhook_body,
)

pytestmark = pytest.mark.integration

PAYME_WEBHOOK = "/api/v1/payments/webhook/payme"
CLICK_WEBHOOK = "/api/v1/payments/webhook/click"
PAYME_AUTH = {"Authorization": "Basic " + base64.b64encode(f"Paycom:{TEST_PAYME_SECRET_KEY}".encode()).decode()}
SHOWN = "30000.00"
TIYIN = 3_000_000
# A legacy Payme order, still unpaid: the shape a Payme checkout open at the deploy leaves.
LIVE_PAYME = Row("live_payme_pending", OrderStatus.PENDING, PaymentMethod.PAYME, PaymentStatus.PENDING)


@pytest.fixture
def gateways(app):
    """Click and Payme configured as production configures them, with Payme's Basic-auth check passed; the app
    config is put back after."""
    before = dict(app.config)
    apply_test_provider_secrets(app)
    with patch.object(PaymeProvider, "verify_payme_signature", return_value=True):
        yield app
    for key in set(app.config) - set(before):
        del app.config[key]
    app.config.update(before)


def _now_ms():
    return int(datetime.now(UTC).timestamp() * 1000)


def _unique_id():
    """A gateway transaction id no earlier run used: the webhook idempotency guard caches answers by it."""
    return str(secrets.randbelow(9 * 10**11) + 10**11)


def _headers(app, user):
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _payme(app, method, params):
    body = make_payme_webhook_body(method=method, params=params, request_id=secrets.randbelow(10**6))
    resp = app.test_client().post(PAYME_WEBHOOK, json=body, headers=PAYME_AUTH)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


def _open_payme_charge(db, order_id, *, opened_ms=None):
    """The pending charge a Payme CreateTransaction opened before the deploy left on the order's payment. Payme's
    CreateTransaction now refuses new ids (spec 2026-10-27 §4.2), so the row is written the way it wrote it."""
    trans_id = f"payme-{_unique_id()}"
    payment = db.session.get(Order, order_id).payment
    opened_at = datetime.fromtimestamp((opened_ms or _now_ms()) / 1000, tz=UTC)
    db.session.add(
        PaymentTransaction(
            payment_id=payment.id,
            transaction_type="charge",
            amount=payment.amount,
            currency="UZS",
            status="pending",
            provider_transaction_id=trans_id,
            provider_response={"id": trans_id, "amount": TIYIN, "account": {"order_id": order_id}},
            created_at=opened_at,
        )
    )
    db.session.commit()
    return trans_id


def _click_webhook(app, order_number, *, action, click_trans_id, merchant_prepare_id=None):
    form = make_click_webhook_form(
        action=action,
        click_trans_id=click_trans_id,
        merchant_trans_id=order_number,
        amount="30000",
        secret_key=TEST_CLICK_SHOP_SECRET_KEY,
        merchant_prepare_id=merchant_prepare_id,
        click_paydoc_id=_unique_id(),
    )
    resp = app.test_client().post(CLICK_WEBHOOK, data=form, content_type="application/x-www-form-urlencoded")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    answer = resp.get_json()
    assert answer["error"] == 0, answer
    return answer


def _retry_payment(app, user, order_id, channel):
    return app.test_client().post(
        f"/api/v1/orders/{order_id}/retry-payment?lang=en",
        json={"channel": channel, "expected_amount": SHOWN},
        headers=_headers(app, user),
    )


def _money_state(db, order_id):
    """Everything a refused or ignored write must leave as it was: the order's rail and paid flag, and the payment
    row's rail, status, amount, provider data, idempotency key, link, return URL and provider transaction."""
    db.session.expire_all()
    order = db.session.get(Order, order_id)
    payment = order.payment
    return {
        "order_status": order.status,
        "order_rail": order.payment_method,
        "is_paid": order.is_paid,
        "rail": payment.payment_method,
        "status": payment.status,
        "amount": payment.amount,
        "provider_data": payment.provider_data,
        "idempotency_key": payment.idempotency_key,
        "payment_link": payment.payment_link,
        "callback_url": payment.callback_url,
        "provider_transaction_id": payment.provider_transaction_id,
    }


def _charge(db, trans_id):
    db.session.expire_all()
    return PaymentTransaction.query.filter_by(provider_transaction_id=trans_id).one()


# --------------------------------------------------------------------------- the Pay tap leaves it on Payme


@pytest.mark.parametrize("channel", ["bot", "web"])
def test_a_pay_tap_while_a_payme_checkout_is_open_moves_nothing(
    app, db, sample_user, gateways, no_fiscalization, channel
):
    """The customer opened the Payme checkout before the deploy, then taps Pay. The tap is told the order changed
    and moves nothing, and Payme's PerformTransaction still completes the payment on Payme (owner call 13)."""
    order_id = seed(db, sample_user, LIVE_PAYME, now=get_utc_now()).id
    trans_id = _open_payme_charge(db, order_id)
    before = _money_state(db, order_id)

    resp = _retry_payment(app, sample_user, order_id, channel)

    assert resp.status_code == 409, resp.get_json()
    assert resp.get_json()["data"] == {"reason_code": "STATE_CHANGED"}
    assert not (resp.get_json().get("data") or {}).get("payment_url")
    assert _money_state(db, order_id) == before

    performed = _payme(app, "PerformTransaction", {"id": trans_id})

    assert performed.get("result", {}).get("state") == PaymeState.COMPLETED.value, performed
    db.session.expire_all()
    paid = db.session.get(Order, order_id)
    assert paid.is_paid is True
    assert paid.payment.payment_method == PaymentMethod.PAYME
    assert paid.payment.status == PaymentStatus.COMPLETED
    assert paid.payment.provider_transaction_id == trans_id
    assert _charge(db, trans_id).status == "completed"


def test_a_payme_charge_past_payme_s_window_no_longer_holds_the_pay(app, db, sample_user, gateways, no_fiscalization):
    """Past `PAYME_TIMEOUT_MS` Payme can no longer capture the charge, so the tap moves the order onto Click, and a
    late PerformTransaction is refused by the same timeout rule."""
    order_id = seed(db, sample_user, LIVE_PAYME, now=get_utc_now()).id
    opened = _now_ms() - app.config.get("PAYME_TIMEOUT_MS", 43200000) - 60_000
    trans_id = _open_payme_charge(db, order_id, opened_ms=opened)

    resp = _retry_payment(app, sample_user, order_id, "web")

    assert resp.status_code == 200, resp.get_json()
    assert urlsplit(resp.get_json()["data"]["payment_url"]).netloc == "my.click.uz"
    moved = _money_state(db, order_id)
    assert (moved["order_rail"], moved["rail"], moved["status"]) == (
        PaymentMethod.CLICK,
        PaymentMethod.CLICK,
        PaymentStatus.PENDING,
    )

    performed = _payme(app, "PerformTransaction", {"id": trans_id})

    assert "result" not in performed and performed["error"]["code"] == PaymeErrors.OPERATION_NOT_ALLOWED
    assert _charge(db, trans_id).status == "cancelled"
    assert _money_state(db, order_id) == moved


# --------------------------------------------------------------------------- Payme never captures a moved payment


@pytest.mark.parametrize("click_settled", [False, True], ids=["click_pending", "click_completed"])
def test_payme_never_captures_a_payment_that_moved_to_click(
    app, db, sample_user, gateways, no_fiscalization, click_settled
):
    """The bot's Pay today (`POST /payments/create`, telegram_bot/handlers/payments.py) moves the order onto Click
    while its Payme checkout is open; Click may settle it before Payme's PerformTransaction arrives. Perform cancels
    the Payme charge instead of capturing a second payment, and Payme's later CancelTransaction refunds nothing and
    cancels nothing."""
    order = seed(db, sample_user, LIVE_PAYME, now=get_utc_now())
    order_id, order_number = order.id, order.order_number
    trans_id = _open_payme_charge(db, order_id)
    moved = app.test_client().post(
        "/api/v1/payments/create",
        json={"order_id": order_id, "payment_method": "click"},
        headers=_headers(app, sample_user),
    )
    assert moved.status_code == 201, moved.get_json()
    db.session.expire_all()
    payment = db.session.get(Order, order_id).payment
    assert payment.payment_method == PaymentMethod.CLICK
    if click_settled:
        click_trans_id = _unique_id()
        _click_webhook(app, order_number, action="0", click_trans_id=click_trans_id)
        _click_webhook(
            app, order_number, action="1", click_trans_id=click_trans_id, merchant_prepare_id=str(payment.id)
        )
    before = _money_state(db, order_id)
    assert before["is_paid"] is click_settled
    assert before["status"] == (PaymentStatus.COMPLETED if click_settled else PaymentStatus.PENDING)

    performed = _payme(app, "PerformTransaction", {"id": trans_id})

    assert "result" not in performed and performed["error"]["code"] == PaymeErrors.OPERATION_NOT_ALLOWED
    assert _charge(db, trans_id).status == "cancelled"
    assert _money_state(db, order_id) == before
    checked = _payme(app, "CheckTransaction", {"id": trans_id})["result"]
    assert (checked["state"], checked["perform_time"]) == (PaymeState.CANCELLED.value, 0)

    cancelled = _payme(app, "CancelTransaction", {"id": trans_id, "reason": 3})

    assert cancelled.get("result", {}).get("state") == PaymeState.CANCELLED.value, cancelled
    assert _money_state(db, order_id) == before


def test_payme_never_captures_a_payme_payment_already_settled(app, db, sample_user, gateways, no_fiscalization):
    """The other half of the guard: the payment is still on Payme but was settled another way while the charge
    waited (here written directly, as a staff settlement would leave it). Perform cancels the charge."""
    order_id = seed(db, sample_user, LIVE_PAYME, now=get_utc_now()).id
    trans_id = _open_payme_charge(db, order_id)
    settled = db.session.get(Order, order_id).payment
    settled.status = PaymentStatus.COMPLETED
    settled.provider_transaction_id = "settled-elsewhere"
    db.session.commit()
    before = _money_state(db, order_id)

    performed = _payme(app, "PerformTransaction", {"id": trans_id})

    assert "result" not in performed and performed["error"]["code"] == PaymeErrors.OPERATION_NOT_ALLOWED
    assert _charge(db, trans_id).status == "cancelled"
    assert _money_state(db, order_id) == before


def test_a_retried_perform_of_the_charge_that_settled_the_payment_completes_it(
    app, db, sample_user, gateways, no_fiscalization
):
    """PerformTransaction settles the payment and confirms the order inside `update_order_status`'s commit, and only
    then marks the charge completed. If a later step of `_handle_successful_payment` raises (the purchase award, the
    grocery-contract settlement), Payme gets -32400 and that commit stays: the payment is COMPLETED by this very
    charge, which is still pending. Written directly here, as that commit leaves it. Payme's retry of the same Perform
    (it reaches the handler when the webhook dedup guard is bypassed) completes the charge and answers success, so
    Payme captures the money the order already counts as paid. Cancelling it would release the payer's hold and leave
    a paid, confirmed order with no money behind it."""
    order_id = seed(db, sample_user, LIVE_PAYME, now=get_utc_now()).id
    trans_id = _open_payme_charge(db, order_id)
    order = db.session.get(Order, order_id)
    settled_at = get_utc_now()
    order.payment.status = PaymentStatus.COMPLETED
    order.payment.paid_at = settled_at
    order.payment.provider_transaction_id = trans_id
    order.status = OrderStatus.CONFIRMED
    order.is_paid = True
    order.paid_at = settled_at
    db.session.commit()
    assert _charge(db, trans_id).status == "pending"
    before = _money_state(db, order_id)

    performed = _payme(app, "PerformTransaction", {"id": trans_id})

    charge = _charge(db, trans_id)
    assert charge.status == "completed"
    assert performed.get("result") == {
        "transaction": str(charge.id),
        "perform_time": to_ms(charge.processed_at),
        "state": PaymeState.COMPLETED.value,
    }, performed
    assert performed["result"]["perform_time"] > 0
    assert _money_state(db, order_id) == before
    checked = _payme(app, "CheckTransaction", {"id": trans_id})["result"]
    assert (checked["state"], checked["perform_time"]) == (
        PaymeState.COMPLETED.value,
        performed["result"]["perform_time"],
    )


# --------------------------------------------------------------------------- PostgreSQL: the lock order


def _pg_customer(pg_db):
    user = User(
        phone=f"+99890{secrets.randbelow(9_000_000) + 1_000_000}",
        email=f"lock-order.{secrets.token_hex(4)}@example.com",
        first_name="Lock",
        last_name="Order",
        password_hash=hash_password("TestPassword123!"),
        role=UserRole.CUSTOMER,
        user_type=UserType.INDIVIDUAL,
        status=UserStatus.ACTIVE,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    pg_db.session.add(user)
    pg_db.session.commit()
    return user


def _wait_until_a_backend_waits_on_a_lock(engine, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as watch:  # a fresh transaction each time: pg_stat_activity is a per-transaction snapshot
            waiting = watch.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ).scalar()
        if waiting:
            return
        time.sleep(0.05)
    raise AssertionError("the Pay tap never waited for the payment row the webhook holds")


def test_a_pay_tap_locks_the_payment_row_before_the_order(pg_app, pg_db, ephemeral_pg_database):
    """Click PREPARE and COMPLETE lock the payment row, then touch the order. A tap that locked the order first and
    then waited for the payment would deadlock with them, and PostgreSQL would abort one side (the webhook answered
    5xx, or the customer a 500). While a webhook holds the payment row, the tap must wait holding nothing on the
    order, so the webhook's next step goes through; once the webhook commits, the tap completes."""
    apply_test_provider_secrets(pg_app)
    user = _pg_customer(pg_db)
    order = seed(pg_db, user, ROW["click_pending"], now=get_utc_now())
    order_id, payment_id, user_id = order.id, order.payment.id, user.id
    pg_db.session.close()

    outcome = {}

    def tap():
        with pg_app.app_context():
            try:
                outcome["result"] = PaymentService().pay_order_as_customer(
                    order_id, user_id, channel="web", expected_amount=Decimal(SHOWN)
                )["payment_url"]
            except Exception as exc:  # reported below, on the test's thread
                outcome["error"] = exc

    engine = create_engine(ephemeral_pg_database)
    tapper = threading.Thread(target=tap, daemon=True)
    try:
        with engine.connect() as webhook:  # leaving the block rolls back, so a failure below still frees the tap
            webhook.execute(text("SELECT id FROM payments WHERE id = :id FOR UPDATE"), {"id": payment_id})
            tapper.start()
            _wait_until_a_backend_waits_on_a_lock(engine)

            # The webhook's next step: the order row. NOWAIT fails at once if the waiting tap holds it.
            webhook.execute(text("SELECT id FROM orders WHERE id = :id FOR UPDATE NOWAIT"), {"id": order_id})
            webhook.commit()
    finally:
        if tapper.is_alive():
            tapper.join(timeout=30)
        engine.dispose()

    assert not tapper.is_alive(), "the Pay tap never finished"
    assert "error" not in outcome, outcome.get("error")
    assert urlsplit(outcome["result"]).netloc == "my.click.uz"


def test_a_duplicate_perform_waiting_on_the_payment_lock_answers_what_the_first_one_answered(
    pg_app, pg_db, ephemeral_pg_database
):
    """Two deliveries of the same PerformTransaction race once the webhook dedup guard is bypassed. Both read the
    charge pending. The first takes the payment row's lock, captures and commits. The second waited on that lock: it
    must re-read the charge under it and answer the first one's completion, with the same perform_time, instead of
    writing over it from its stale read (as "cancelled", or with a perform_time of its own)."""
    user = _pg_customer(pg_db)
    order_id = seed(pg_db, user, LIVE_PAYME, now=get_utc_now()).id
    trans_id = _open_payme_charge(pg_db, order_id)
    charge = PaymentTransaction.query.filter_by(provider_transaction_id=trans_id).one()
    charge_id, payment_id = charge.id, charge.payment_id
    pg_db.session.close()
    first_performed_at = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=30)

    outcome = {}

    def duplicate_perform():
        with pg_app.app_context():
            body = make_payme_webhook_body(method="PerformTransaction", params={"id": trans_id}, request_id=2)
            outcome["answer"] = PaymentService().handle_payme_webhook(body)

    engine = create_engine(ephemeral_pg_database)
    performer = threading.Thread(target=duplicate_perform, daemon=True)
    try:
        with engine.connect() as first:  # the first delivery, holding the payment row while it captures
            first.execute(text("SELECT id FROM payments WHERE id = :id FOR UPDATE"), {"id": payment_id})
            performer.start()
            _wait_until_a_backend_waits_on_a_lock(engine)

            first.execute(
                text(
                    "UPDATE payments SET status = 'completed', paid_at = :at, provider_transaction_id = :trans "
                    "WHERE id = :id"
                ),
                {"at": first_performed_at, "trans": trans_id, "id": payment_id},
            )
            first.execute(
                text("UPDATE payment_transactions SET status = 'completed', processed_at = :at WHERE id = :id"),
                {"at": first_performed_at, "id": charge_id},
            )
            first.commit()
    finally:
        if performer.is_alive():
            performer.join(timeout=30)
        engine.dispose()

    assert not performer.is_alive(), "the duplicate Perform never finished"
    assert outcome["answer"].get("result") == {
        "transaction": str(charge_id),
        "perform_time": to_ms(first_performed_at),
        "state": PaymeState.COMPLETED.value,
    }, outcome["answer"]
    charge = pg_db.session.get(PaymentTransaction, charge_id)
    assert (charge.status, to_ms(charge.processed_at)) == ("completed", to_ms(first_performed_at))
