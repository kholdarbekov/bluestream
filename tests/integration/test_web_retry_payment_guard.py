"""`POST /api/v1/orders/<id>/retry-payment` is the one customer route that opens a Click payment on an existing
order (spec 2026-10-27 §4.2, §4.5; plan 2026-10-27 Task 2a).

Its body is what the screen showed: `{"channel": "bot"|"web", "expected_amount": "30000.00"}`. The route checks
the one Pay rule first (`payment_projection.pay_refusal`, refused with 409 `PAY_REFUSED` and the refusal every
payload publishes), then that amount (409 `STATE_CHANGED` when it moved), and writes nothing when either refuses.
The Click link's return and cancel URLs lead back to the channel that tapped Pay, on every call: the bot chat's
`/start` deep link to that order or subscription, or its page on the website. A URL stored by an earlier tap never
carries over.

Why the rule lives on the write (I3 / B3): this endpoint used to check only `is_paid` and the rail. On a
CANCELLED order `PaymentService.create_payment` rewrote `payment.status = PENDING` and `payment.amount =
order.total_amount`, undoing the zeroing the dead order was left in, so it re-appeared as owing money on every
debtor list and toward the COD cap.

Driven through the endpoints the clients call, over the shared state matrix
(`tests/integration/customer_action_matrix.py`), with the real Click link builder (`apply_test_provider_secrets`)
and the real Click webhook. Only external I/O is mocked: the Asl belgisi pre-utilisation and fiscalization.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.translation import Translation
from business_app.models.user import User
from business_app.utils.password_security import hash_password
from business_app.utils.payment_projection import open_receivable_amount
from business_app.utils.timezone_utils import get_utc_now
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus, UserRole, UserType
from tests.integration.customer_action_matrix import ROW, ROWS, seed
from tests.integration.fake_gateways import (
    TEST_CLICK_SHOP_SECRET_KEY,
    apply_test_provider_secrets,
    make_click_webhook_form,
)
from tests.integration.tier_discount_factory import post_order, verify_phone

pytestmark = pytest.mark.integration

ORDERS = "/api/v1/orders"
CLICK_WEBHOOK = "/api/v1/payments/webhook/click"
CLICK_LINK = "business_app.services.payment_service.PaymentService.create_payment_link"
SEEDED_PREFIXES = ("api.common.", "api.orders.actions.", "api.payments.error.")
EN = {key: values.get("en") for key, values in BACKEND_TRANSLATIONS.items()}
# The words a refused Pay answers with, for each refusal: what the customer must read.
REFUSAL_KEY = {
    "already_paid": "api.payments.error.already_paid",
    "order_closed": "api.payments.error.lifecycle.advice_order_already_cancelled",
    "not_online": "api.orders.actions.pay.refusal.not_online",
    "partly_paid": "api.orders.actions.pay.refusal.partly_paid",
    "deadline_passed": "api.orders.actions.pay.refusal.deadline_passed",
}
SHOWN = "30000.00"
STALE = "29000.00"

WRITE_ROWS = [pytest.param(row, id=row.id) for row in ROWS]


@pytest.fixture(autouse=True)
def seeded(db):
    """The refresh sentence, the Pay words and the payment refusals, as the seeder ships them."""
    for key, values in BACKEND_TRANSLATIONS.items():
        if not key.startswith(SEEDED_PREFIXES):
            continue
        for language, value in values.items():
            if value:
                db.session.add(Translation(key=key, language=language, value=value, category=_category_for(key)))
    db.session.commit()


@pytest.fixture
def click(app):
    """The real Click link builder, configured as production configures it; the app config is put back after."""
    before = dict(app.config)
    apply_test_provider_secrets(app)
    yield app
    for key in set(app.config) - set(before):
        del app.config[key]
    app.config.update(before)


class _Customer:
    """A customer client: its bearer token, a fresh test client per call, in English."""

    def __init__(self, app, user):
        with app.app_context():
            token = create_access_token(identity=str(user.id))
        self._app = app
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def pay(self, order_id, body):
        path = f"{ORDERS}/{order_id}/retry-payment?lang=en"
        return self._app.test_client().post(path, json=body, headers=self.headers)

    def shown_pay(self, order_id):
        """The order's published Pay, as GET /orders/<id> answers it."""
        resp = self._app.test_client().get(f"{ORDERS}/{order_id}?lang=en", headers=self.headers)
        assert resp.status_code == 200, resp.get_json()
        return resp.get_json()["data"]["order"]["customer_actions"]["pay"]

    def shown_amount(self, order):
        """What the screen sends back: the published `expected_amount`; a refused Pay publishes none, so the order
        total."""
        pay = self.shown_pay(order.id)
        return pay["expected_amount"] if pay["allowed"] else str(order.total_amount)


@pytest.fixture
def customer(app, sample_user):
    return _Customer(app, sample_user)


def body(channel, amount=SHOWN):
    return {"channel": channel, "expected_amount": amount}


def query(url):
    return parse_qs(urlsplit(url).query)


def expected_return(app, order_id, subscription_id, channel):
    """Where Click sends the customer back (spec §4.5): the bot chat's deep link to the order or subscription, or
    its page on the website."""
    site = app.config["COMPANY_WEBSITE"].rstrip("/")
    bot = app.config["COMPANY_TELEGRAM_BOT_URL"]
    if channel == "bot":
        return f"{bot}?start=sub_{subscription_id}" if subscription_id else f"{bot}?start=order_{order_id}"
    if subscription_id:
        return f"{site}/my-subscriptions?sub={subscription_id}"
    return f"{site}/order-tracking?order_id={order_id}"


def _payment(db, order_id):
    db.session.expire_all()
    return db.session.get(Order, order_id).payment


def _written(db, order_id):
    """What a refused write must leave as it was."""
    payment = _payment(db, order_id)
    return (payment.status, payment.amount, payment.payment_link) if payment else None


def _in_flight(payment):
    """What a Pay tap must not touch while a Click payment is in progress: everything but the checkout link."""
    click = {k: v for k, v in (payment.provider_data or {}).get("click", {}).items() if not k.startswith("checkout_")}
    return payment.status, payment.amount, payment.idempotency_key, click


def _click_webhook(app, order_number, *, action, click_trans_id, merchant_prepare_id=None):
    form = make_click_webhook_form(
        action=action,
        click_trans_id=click_trans_id,
        merchant_trans_id=order_number,
        amount="30000",
        secret_key=TEST_CLICK_SHOP_SECRET_KEY,
        merchant_prepare_id=merchant_prepare_id,
        click_paydoc_id="5231141285",
    )
    resp = app.test_client().post(CLICK_WEBHOOK, data=form, content_type="application/x-www-form-urlencoded")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


# --------------------------------------------------------------------------- the matrix: published ⇔ write


@pytest.mark.parametrize("channel", ["bot", "web"])
@pytest.mark.parametrize("row", WRITE_ROWS)
def test_the_write_answers_what_the_payload_published(app, db, sample_user, customer, click, row, channel):
    order = seed(db, sample_user, row, now=get_utc_now())
    order_id, subscription_id = order.id, order.subscription_id
    pay = customer.shown_pay(order_id)
    before = _written(db, order_id)

    stale = customer.pay(order_id, body(channel, STALE))
    if pay["allowed"]:  # spec §13: the 409 on a stale expected_*
        assert (stale.status_code, stale.get_json()["data"]) == (409, {"reason_code": "STATE_CHANGED"})
    else:  # refusal before staleness
        assert stale.status_code == 409, stale.get_json()
        assert stale.get_json()["data"] == {"reason_code": "PAY_REFUSED", "refusal": row.pay_refusal}
    assert _written(db, order_id) == before

    resp = customer.pay(order_id, body(channel))
    if pay["allowed"]:
        assert resp.status_code == 200, resp.get_json()
        q = query(resp.get_json()["data"]["payment_url"])
        assert q["return_url"] == q["cancel_url"] == [expected_return(app, order_id, subscription_id, channel)]
        assert q["amount"] == [SHOWN]
    else:
        assert resp.status_code == 409, resp.get_json()
        assert resp.get_json()["data"] == {"reason_code": "PAY_REFUSED", "refusal": row.pay_refusal}
        assert resp.get_json()["message"] == EN[REFUSAL_KEY[row.pay_refusal]]
        assert _written(db, order_id) == before  # nothing written


def _paid_through_click_at_the_same_price(db, customer, order):
    """A delivered legacy online order with nothing collected moves onto Click at the price it was shown (spec
    §4.3, D2): both rails move, and nothing is re-priced."""
    resp = customer.pay(order.id, body("web", customer.shown_amount(order)))

    assert resp.status_code == 200, resp.get_json()
    db.session.expire_all()
    order = db.session.get(Order, order.id)
    payment = order.payment
    assert (order.payment_method, payment.payment_method) == (PaymentMethod.CLICK, PaymentMethod.CLICK)
    assert order.total_amount == payment.amount == Decimal("30000.00")
    assert payment.status is PaymentStatus.PENDING


def test_a_delivered_payme_order_pays_through_click_at_the_same_price(db, sample_user, customer, click):
    """Dev order 12: a legacy Payme order, delivered, its Payme attempt cancelled."""
    order = seed(db, sample_user, ROW["delivered_payme_cancelled"], now=get_utc_now())
    _paid_through_click_at_the_same_price(db, customer, order)


def test_a_delivered_card_order_with_a_payme_row_pays_through_click(db, sample_user, customer, click):
    """Dev order 76: a card order whose payment row is a cancelled Payme attempt."""
    order = seed(db, sample_user, ROW["delivered_card_order_payme_row"], now=get_utc_now())
    _paid_through_click_at_the_same_price(db, customer, order)


def test_a_stale_amount_is_refused_and_writes_nothing(db, sample_user, customer, click):
    order = seed(db, sample_user, ROW["click_pending"], now=get_utc_now())
    before = _written(db, order.id)

    resp = customer.pay(order.id, body("web", STALE))

    assert resp.status_code == 409 and resp.get_json()["data"] == {"reason_code": "STATE_CHANGED"}
    assert resp.get_json()["message"] == "This changed. Refresh to see the latest."
    assert _written(db, order.id) == before


def test_the_return_urls_never_carry_over_between_channels(app, db, sample_user, customer, click):
    order = seed(db, sample_user, ROW["click_pending"], now=get_utc_now())
    order_id = order.id

    for channel in ("bot", "web", "bot"):
        resp = customer.pay(order_id, body(channel))
        assert resp.status_code == 200, resp.get_json()
        q = query(resp.get_json()["data"]["payment_url"])
        assert q["return_url"] == q["cancel_url"] == [expected_return(app, order_id, None, channel)]

    assert _payment(db, order_id).callback_url == expected_return(app, order_id, None, "bot")


def test_a_pay_tap_during_a_click_payment_in_progress_leaves_it_untouched(
    app, db, sample_user, customer, click, no_fiscalization
):
    """Review Focus 3: the bot opened Click and Click PREPARE passed (it leaves the row PENDING and records
    `merchant_prepare_id`); before COMPLETE arrives the customer taps Pay on the website. The tap gets a link that
    returns to the website, the payment in progress keeps everything but its checkout link, and the first COMPLETE
    still settles the order."""
    order = seed(db, sample_user, ROW["click_pending"], now=get_utc_now())
    order_id, order_number = order.id, order.order_number
    assert customer.pay(order_id, body("bot")).status_code == 200

    prepare = _click_webhook(app, order_number, action="0", click_trans_id="940001")
    assert prepare["error"] == 0, prepare
    payment = _payment(db, order_id)
    assert payment.status == PaymentStatus.PENDING
    assert payment.provider_data["click"]["merchant_prepare_id"] == payment.id
    payment_id, snapshot = payment.id, _in_flight(payment)

    resp = customer.pay(order_id, body("web"))

    assert resp.status_code == 200, resp.get_json()
    assert query(resp.get_json()["data"]["payment_url"])["return_url"] == [
        expected_return(app, order_id, None, "web")
    ]
    assert _in_flight(_payment(db, order_id)) == snapshot

    complete = _click_webhook(
        app, order_number, action="1", click_trans_id="940001", merchant_prepare_id=str(payment_id)
    )
    assert complete["error"] == 0, complete
    db.session.expire_all()
    assert db.session.get(Order, order_id).is_paid is True
    assert db.session.get(Payment, payment_id).status == PaymentStatus.COMPLETED


@pytest.mark.parametrize(
    "sent",
    [
        {},
        {"channel": "web"},
        {"expected_amount": SHOWN},
        {"channel": "sms", "expected_amount": SHOWN},
        {"channel": "web", "expected_amount": "abc"},
        {"channel": "web", "expected_amount": "1e999999"},
    ],
    ids=["empty", "no_amount", "no_channel", "unknown_channel", "amount_not_a_number", "amount_no_total_can_hold"],
)
def test_a_malformed_body_is_a_400_and_writes_nothing(db, sample_user, customer, click, sent):
    order = seed(db, sample_user, ROW["click_pending"], now=get_utc_now())
    before = _written(db, order.id)

    resp = customer.pay(order.id, sent)

    assert resp.status_code == 400, resp.get_json()
    assert not (resp.get_json().get("data") or {}).get("payment_url")
    assert _written(db, order.id) == before


def test_another_customers_order_is_not_found(db, customer, click):
    other = User(
        email="other@example.com",
        phone="+998901112233",
        password_hash=hash_password("TestPassword123!"),
        first_name="Other",
        last_name="Customer",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(other)
    db.session.commit()
    order = seed(db, other, ROW["click_pending"], now=get_utc_now())
    before = _written(db, order.id)

    resp = customer.pay(order.id, body("web"))

    assert resp.status_code == 404, resp.get_json()
    assert _written(db, order.id) == before


@pytest.mark.parametrize("source, channel", [("telegram", "bot"), ("web", "web")])
def test_the_checkout_link_returns_to_the_channel_the_order_came_from(
    app, db, sample_user, sample_product, user_address, customer, click, source, channel
):
    verify_phone(db, sample_user)
    with patch(
        "business_app.services.payment_fiscalization_service.PaymentFiscalizationService"
        ".pre_utilise_marking_codes_for_payment",
        return_value={"utilised_at": None, "fast_path": True},
    ):
        resp = post_order(
            app,
            customer.headers,
            product_id=sample_product.id,
            address_id=user_address.id,
            payment_method="click",
            source=source,
        )

    assert resp.status_code == 201, resp.get_json()
    data = resp.get_json()["data"]
    assert query(data["payment_url"])["return_url"] == [expected_return(app, data["order"]["id"], None, channel)]


def test_the_order_page_urls_are_real_pages(app):
    adapter = app.url_map.bind("localhost")
    assert adapter.match("/order-tracking")[0] == "frontend.order_tracking"
    assert adapter.match("/my-subscriptions")[0] == "frontend.my_subscriptions"


# --------------------------------------------------------------------------- I3: a dead order stays dead


def _order_with_payment(
    db,
    user,
    *,
    order_number,
    order_status,
    payment_status=PaymentStatus.PENDING,
    is_paid=False,
    amount=Decimal("18000.00"),
    payment_amount=None,
):
    order = Order(
        user_id=user.id,
        order_number=order_number,
        status=order_status,
        subtotal=amount,
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=amount,
        payment_method=PaymentMethod.CLICK,
        is_paid=is_paid,
    )
    db.session.add(order)
    db.session.flush()
    payment = Payment(
        order_id=order.id,
        user_id=user.id,
        payment_method=PaymentMethod.CLICK,
        amount=payment_amount if payment_amount is not None else amount,
        currency="UZS",
        status=payment_status,
        payment_id=f"pay-{order_number}",
        provider_data={"click": {"click_paydoc_id": "20240101000001"}},
    )
    db.session.add(payment)
    db.session.commit()
    return order, payment


class TestWebRetryRefusesADeadOrder:
    @pytest.mark.parametrize(
        "order_status", [OrderStatus.CANCELLED, OrderStatus.RETURNED], ids=["cancelled", "returned"]
    )
    def test_it_refuses_and_hands_back_no_payment_url(self, db, sample_user, customer, order_status):
        order, _payment = _order_with_payment(
            db,
            sample_user,
            order_number=f"W3-{order_status.value}",
            order_status=order_status,
            payment_status=PaymentStatus.CANCELLED,
            # the zeroed shape a dead order is left in
            payment_amount=Decimal("0.00"),
        )

        resp = customer.pay(order.id, body("web", customer.shown_amount(order)))

        assert resp.status_code >= 400, "a dead order must not be handed a checkout URL"
        assert not (resp.get_json().get("data") or {}).get("payment_url")

    def test_it_does_not_resurrect_a_dead_order_s_receivable(self, app, db, sample_user, customer):
        """The money half. `create_payment` rewrites amount back to the order
        total; that is what makes a cancelled order owe money again."""
        order, payment = _order_with_payment(
            db,
            sample_user,
            order_number="W3-money",
            order_status=OrderStatus.CANCELLED,
            payment_status=PaymentStatus.CANCELLED,
            payment_amount=Decimal("0.00"),
        )
        payment_id = payment.id
        with app.app_context():
            assert open_receivable_amount(Payment.query.get(payment_id)) == Decimal("0.00")

        customer.pay(order.id, body("web", customer.shown_amount(order)))

        with app.app_context():
            after = Payment.query.get(payment_id)
            assert open_receivable_amount(after) == Decimal("0.00"), (
                "retrying a cancelled order re-inflated payment.amount and the "
                "order re-appeared on every debtor list"
            )
            assert after.status == PaymentStatus.CANCELLED

    def test_it_refuses_an_order_already_settled_on_its_payment(self, app, db, sample_user, customer):
        """M4's server twin: retrying would REWRITE a COMPLETED payment to
        PENDING and mint a second link."""
        order, payment = _order_with_payment(
            db,
            sample_user,
            order_number="W3-settled",
            order_status=OrderStatus.PENDING,
            payment_status=PaymentStatus.COMPLETED,
        )
        payment_id = payment.id

        resp = customer.pay(order.id, body("web", customer.shown_amount(order)))

        assert resp.status_code >= 400
        with app.app_context():
            assert Payment.query.get(payment_id).status == PaymentStatus.COMPLETED


class TestWebRetryStillServesTheLivePopulation:
    def test_a_live_unpaid_click_order_is_still_retryable(self, db, sample_user, customer):
        order, _payment = _order_with_payment(db, sample_user, order_number="W3-live", order_status=OrderStatus.PENDING)

        with patch(CLICK_LINK, lambda self, payment_id, **_: {"payment_url": "https://my.click.uz/services/pay?id=1"}):
            resp = customer.pay(order.id, body("web", customer.shown_amount(order)))

        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["data"]["payment_url"]

    def test_a_case_b_delivered_unpaid_order_is_still_retryable(self, db, sample_user, customer):
        """The whole point of B3 — do not close this door while closing the others."""
        order, _payment = _order_with_payment(
            db, sample_user, order_number="W3-caseb", order_status=OrderStatus.DELIVERED
        )

        with patch(CLICK_LINK, lambda self, payment_id, **_: {"payment_url": "https://my.click.uz/services/pay?id=2"}):
            resp = customer.pay(order.id, body("web", customer.shown_amount(order)))

        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["data"]["payment_url"]


class TestThePaymeCellThatJustifiedThePartialPredicate:
    """The rule uses `order_is_resolved`, not the full `order_is_payable_online`.

    The full predicate's payment half requires the rail to be in `FISCALIZED_RAILS`, which excludes PAYME **by
    construction** (that exclusion is the payme carve-out for the 2026-08-24 no-reversal rule). Gating on it would
    have silently refused every legacy Payme order, which is now paid through Click (spec 2026-10-27 §4.3, D2).
    `order_is_resolved` is rail-BLIND, so a dead payme order is refused and a live one is not. Both directions are
    pinned here, because a future "tidy-up" to the full predicate would break exactly one.
    """

    @staticmethod
    def _payme_order(db, user, *, order_number, order_status, payment_status):
        order = Order(
            user_id=user.id,
            order_number=order_number,
            status=order_status,
            subtotal=Decimal("30000.00"),
            delivery_fee=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            loyalty_discount=Decimal("0.00"),
            total_amount=Decimal("30000.00"),
            payment_method=PaymentMethod.PAYME,
            is_paid=False,
        )
        db.session.add(order)
        db.session.flush()
        db.session.add(
            Payment(
                order_id=order.id,
                user_id=user.id,
                payment_method=PaymentMethod.PAYME,
                amount=order.total_amount,
                currency="UZS",
                status=payment_status,
                payment_id=f"pay-{order_number}",
            )
        )
        db.session.commit()
        return order

    def test_a_payme_order_is_paid_through_click(self, db, sample_user, customer, click):
        """A live Payme order still unpaid moves onto Click at the same online price (spec §4.3, D2)."""
        order = self._payme_order(
            db,
            sample_user,
            order_number="W3-payme-live",
            order_status=OrderStatus.PENDING,
            payment_status=PaymentStatus.PENDING,
        )

        resp = customer.pay(order.id, body("web", customer.shown_amount(order)))

        assert resp.status_code == 200, resp.get_json()
        assert urlsplit(resp.get_json()["data"]["payment_url"]).netloc == "my.click.uz"
        db.session.expire_all()
        moved = db.session.get(Order, order.id)
        assert (moved.payment_method, moved.payment.payment_method) == (PaymentMethod.CLICK, PaymentMethod.CLICK)
        assert moved.payment.amount == Decimal(SHOWN)

    def test_a_cancelled_payme_order_is_refused_all_the_same(self, db, sample_user, customer):
        order = self._payme_order(
            db,
            sample_user,
            order_number="W3-payme-dead",
            order_status=OrderStatus.CANCELLED,
            payment_status=PaymentStatus.CANCELLED,
        )

        resp = customer.pay(order.id, body("web", customer.shown_amount(order)))

        assert resp.status_code >= 400
        assert not (resp.get_json().get("data") or {}).get("payment_url")
