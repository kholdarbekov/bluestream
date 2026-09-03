"""The late-Click-debit hole (Task 17A / Case C).

``ClickPaymentProviderService._restore_click_rail_after_offline_settlement``
("Case C") restores an order to the CLICK rail and queues fiscalization
without revoking ``tier_discount`` or recomputing ``total_amount``. Before
this feature that was harmless, because ``tier_discount`` was always 0.00.

Concrete failure this test pins: customer checks out on Click
(``tier_discount = 0``). Before delivery they switch to Cash via
``POST /api/v1/payments/create`` -- Task 17's grant path fires,
``tier_discount`` becomes non-zero and ``total_amount`` drops. The order is
delivered and settled in cash for the discounted total. Later a stale
in-flight Click charge from the original checkout finally posts. Case C fires,
restores the Click rail, and calls ``queue_click_fiscalization``. The fiscal
payload prices ``received_card = to_tiyin(order.total_amount)`` off a
DISCOUNTED total while ``build_click_fiscalization_payload`` fills per-item
``Discount`` from ``loyalty_discount`` ALONE, so
``Sum(Price - Discount) != received_card`` -- a tax-committee reconciliation
failure.

Revoking on the Click rail is also the arithmetically correct answer, not
merely the safe one: Click debited the original, undiscounted amount, so the
undiscounted total is what the receipt should state.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from flask_jwt_extended import create_access_token

from business_app import db as _db
from business_app.models.corporate import (
    CorporateContract,
    CorporateContractProductPrice,
    CorporateContractStatus,
    CorporatePrepaymentAccount,
    CorporatePrepaymentBalance,
)
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.user import User, UserAddress
from shared.enums import (
    CorporateContractTrackingMode,
    DeliveryStatus,
    EntitySubtype,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
    UserType,
)

from tests.integration.fake_gateways import TEST_CLICK_SHOP_SECRET_KEY, make_click_webhook_form
from tests.integration.tier_discount_factory import (
    post_order,
    seed_program,
    seed_tier,
    verify_phone,
)

WEBHOOK_URL = "/api/v1/payments/webhook/click"
TIER_RATE = Decimal("9")


def _headers(app, user_id):
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _reload(order_id):
    _db.session.expire_all()
    return Order.query.get(order_id)


@pytest.fixture
def program(db):
    return seed_program(db)


@pytest.fixture
def customer(db, sample_user):
    verify_phone(db, sample_user)
    return sample_user


@pytest.fixture
def driver_profile(db, delivery_driver):
    profile = DeliveryPerson(
        user_id=delivery_driver.id,
        full_name="Test Driver",
        phone=delivery_driver.phone,
        email=delivery_driver.email,
        is_active=True,
        is_available=True,
    )
    _db.session.add(profile)
    _db.session.commit()
    return profile


def _stamp_click_identifiers(payment, *, click_trans_id, click_paydoc_id):
    """Stand in for a real ``handle_prepare`` round trip: stamp the identifiers
    Click's PREPARE call would have written onto ``provider_data``. PREPARE's
    own protocol mechanics are a different code path and out of scope here."""
    provider_data = dict(payment.provider_data or {})
    provider_data["click"] = {"click_trans_id": click_trans_id, "click_paydoc_id": click_paydoc_id}
    payment.provider_data = provider_data
    _db.session.commit()


def _deliver_with_cash(driver, delivery, amount):
    from business_app.services.staff_service import StaffService

    with patch("business_app.tasks.notification_tasks.send_delivery_update_task.delay"), patch(
        "business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"
    ):
        StaffService.update_delivery_status(
            delivery_id=delivery.id,
            new_status="delivered",
            staff_user_id=driver.id,
            metadata={"cash_collected": str(amount)},
        )


def _post_late_complete(client, *, order, payment, amount, click_trans_id, click_paydoc_id):
    form = make_click_webhook_form(
        action="1",
        click_trans_id=click_trans_id,
        merchant_trans_id=order.order_number,
        amount=str(int(amount)),
        secret_key=TEST_CLICK_SHOP_SECRET_KEY,
        merchant_prepare_id=str(payment.id),
        error=0,
        click_paydoc_id=click_paydoc_id,
    )
    return client.post(WEBHOOK_URL, data=form, content_type="application/x-www-form-urlencoded")


@pytest.mark.integration
def test_late_click_debit_after_tier_discounted_cash_settlement_revokes_the_discount(
    matrix_app, db, customer, sample_product, user_address, program, delivery_driver, driver_profile, no_fiscalization
):
    app = matrix_app
    client = app.test_client()
    headers = _headers(app, customer.id)
    seed_tier(db, program, name="Base", rate=TIER_RATE)

    # 1. Customer checks out on Click. Fiscalized rail: tier_discount is 0.
    resp = post_order(
        app,
        headers,
        product_id=sample_product.id,
        address_id=user_address.id,
        payment_method="click",
    )
    assert resp.status_code == 201, resp.get_json()
    order = _reload(resp.get_json()["data"]["order"]["id"])
    assert Decimal(str(order.tier_discount)) == Decimal("0.00")
    payment_id = Payment.query.filter_by(order_id=order.id).one().id
    _stamp_click_identifiers(
        Payment.query.get(payment_id), click_trans_id="950101", click_paydoc_id="5231199101"
    )

    # 2. Before delivery, the customer switches to cash -- Task 17's grant path.
    resp = client.post(
        "/api/v1/payments/create",
        json={"order_id": order.id, "payment_method": "cash"},
        headers=headers,
    )
    assert resp.status_code in (200, 201), resp.get_json()
    order = _reload(order.id)
    granted_tier_discount = Decimal(str(order.tier_discount))
    assert granted_tier_discount > Decimal("0.00"), "the flip to cash must grant the tier discount"
    discounted_total = Decimal(str(order.total_amount))
    undiscounted_total = Decimal(str(order.subtotal)) + Decimal(str(order.delivery_fee or 0))
    assert discounted_total < undiscounted_total

    payment = Payment.query.get(payment_id)
    assert Decimal(str(payment.amount)) == discounted_total
    # create_payment's provider_data overwrite (a separate, pre-existing gap --
    # not this task's concern) clobbers the click sub-dict on every flip;
    # restamp so the late-complete webhook below can resolve this payment.
    _stamp_click_identifiers(payment, click_trans_id="950101", click_paydoc_id="5231199101")

    # 3. The order is delivered and settled in cash for the DISCOUNTED total.
    order.status = OrderStatus.OUT_FOR_DELIVERY
    order.delivery_address_id = user_address.id
    _db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver_profile.id,
        status=DeliveryStatus.ARRIVED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    _db.session.add(delivery)
    _db.session.commit()

    _deliver_with_cash(delivery_driver, delivery, discounted_total)
    _db.session.expire_all()
    payment = Payment.query.get(payment_id)
    assert payment.payment_method == PaymentMethod.CASH
    assert payment.status == PaymentStatus.COMPLETED

    # 4. The stale in-flight Click charge from the original checkout finally posts.
    resp = _post_late_complete(
        client,
        order=order,
        payment=payment,
        amount=discounted_total,
        click_trans_id="950101",
        click_paydoc_id="5231199101",
    )
    assert resp.status_code == 200, resp.get_json()

    _db.session.expire_all()
    order = Order.query.get(order.id)
    payment = Payment.query.get(payment_id)

    # Case C fired and restored the Click rail.
    assert payment.payment_method == PaymentMethod.CLICK, "Case C must restore the Click rail"
    assert order.payment_method == PaymentMethod.CLICK

    # THE HOLE: the tier discount is a COD-only benefit and must never reach a
    # Click fiscal receipt. Revoking is arithmetically correct too -- Click
    # debited the undiscounted amount, so the undiscounted total is what the
    # receipt must state.
    assert Decimal(str(order.tier_discount)) == Decimal("0.00")
    assert Decimal(str(order.total_amount)) == undiscounted_total
    assert Decimal(str(payment.amount)) == undiscounted_total


# --------------------------------------------------------------------------- #
# F1 -- the admin payment-method-edit rail move (cash -> business_account ->
# click) was the third caller missing apply_tier_discount_for_rail.
# --------------------------------------------------------------------------- #

BA_TIER_RATE = Decimal("2")
BA_UNIT_PRICE = Decimal("18000.00")


@pytest.fixture
def loyalty_eligible_workplace(db, sample_product):
    """A workplace entity that is both loyalty-eligible AND business-account
    eligible for ``sample_product`` -- so the same order can legally ride
    cash -> business_account -> click.

    Mirrors ``test_tier_discount_order_creation.loyalty_eligible_workplace``.
    """
    user = User(
        email=f"wp-{uuid4().hex[:8]}@example.com",
        phone=f"+99895{uuid4().int % 10000000:07d}",
        password_hash="x" * 60,
        first_name="Work",
        last_name="Place",
        user_type=UserType.ENTITY,
        entity_subtype=EntitySubtype.WORKPLACE,
        company_name="Tier Co F1",
        role=UserRole.CUSTOMER,
        is_verified=True,
        phone_verified_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.flush()
    address = UserAddress(
        user_id=user.id,
        title="Office",
        full_address="Office 1",
        street_address="Office 1",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
        is_default=True,
    )
    db.session.add(address)
    contract = CorporateContract(
        user_id=user.id,
        contract_number=f"C-{uuid4().hex[:10]}",
        name="F1 coverage",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(UTC) - timedelta(days=1),
        currency="UZS",
        is_active=True,
        is_loyalty_points_eligible=True,
        tracking_mode=CorporateContractTrackingMode.UNITS,
    )
    db.session.add(contract)
    db.session.flush()
    db.session.add(
        CorporateContractProductPrice(
            contract_id=contract.id,
            product_id=sample_product.id,
            unit_price=BA_UNIT_PRICE,
            is_prepayment_eligible=True,
            is_active=True,
        )
    )
    account = CorporatePrepaymentAccount(contract_id=contract.id, is_active=True)
    db.session.add(account)
    db.session.flush()
    db.session.add(
        CorporatePrepaymentBalance(
            account_id=account.id,
            product_id=sample_product.id,
            prepaid_units=Decimal("50.00"),
            reserved_units=Decimal("0.00"),
            consumed_units=Decimal("0.00"),
            is_active=True,
        )
    )
    db.session.commit()
    return user, address


@pytest.fixture
def admin_user(db):
    user = User(
        email=f"admin-f1-{uuid4().hex[:8]}@example.com",
        phone=f"+99890{uuid4().int % 10000000:07d}",
        password_hash="x" * 60,
        first_name="Admin",
        last_name="F1",
        user_type=UserType.STAFF,
        role=UserRole.ADMIN,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.mark.integration
def test_admin_two_hop_cash_to_business_account_to_click_never_carries_the_tier_discount(
    app, db, loyalty_eligible_workplace, admin_user, sample_product, program
):
    """The exact two-hop admin chain from the spec: a loyalty-eligible entity
    customer places a CASH order (subtotal 36 000, tier_discount 720, total
    35 280). An admin reclassifies cash -> business_account, then
    business_account -> click. Neither hop may leave a live tier_discount on
    the order once it lands on a fiscalized rail -- and business_account,
    which is not COD either, must not carry it in between.
    """
    seed_tier(db, program, name="Base", rate=BA_TIER_RATE)
    customer, address = loyalty_eligible_workplace
    customer_headers = _headers(app, customer.id)
    admin_headers = _headers(app, admin_user.id)

    # 1. Customer places a CASH order. quantity=2 * unit_price=18 000 = 36 000
    #    subtotal; a 2% tier grants 720.
    resp = post_order(
        app,
        customer_headers,
        product_id=sample_product.id,
        address_id=address.id,
        payment_method="cash",
    )
    assert resp.status_code == 201, resp.get_json()
    order = _reload(resp.get_json()["data"]["order"]["id"])
    subtotal = Decimal(str(order.subtotal))
    assert subtotal == Decimal("36000.00")
    undiscounted_total = subtotal + Decimal(str(order.delivery_fee or 0))
    assert Decimal(str(order.tier_discount)) == Decimal("720.00")
    assert Decimal(str(order.total_amount)) == Decimal("35280.00")

    # 2. Admin reclassifies cash -> business_account.
    resp = app.test_client().post(
        f"/api/v1/admin/orders/{order.id}/payment-method",
        json={"new_method": "business_account", "reason": "F1 pin: cash to business_account"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.get_json()
    order = _reload(order.id)
    payment = Payment.query.filter_by(order_id=order.id).one()
    assert order.payment_method == PaymentMethod.BUSINESS_ACCOUNT
    # business_account is not COD: the discount must not survive the hop.
    assert Decimal(str(order.tier_discount)) == Decimal("0.00")
    assert Decimal(str(order.total_amount)) == undiscounted_total
    assert Decimal(str(payment.amount)) == undiscounted_total

    # 3. Admin unwinds business_account -> click.
    resp = app.test_client().post(
        f"/api/v1/admin/orders/{order.id}/payment-method",
        json={"new_method": "click", "reason": "F1 pin: business_account to click"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.get_json()
    order = _reload(order.id)
    payment = Payment.query.filter_by(order_id=order.id).one()

    assert order.payment_method == PaymentMethod.CLICK
    assert payment.payment_method == PaymentMethod.CLICK

    # THE INVARIANT: a tier discount must never reach a fiscalized (Click)
    # receipt. Both the order and the payment row that mints the Click link
    # must reflect the undiscounted total.
    assert Decimal(str(order.tier_discount)) == Decimal("0.00")
    assert Decimal(str(order.total_amount)) == undiscounted_total
    assert Decimal(str(payment.amount)) == undiscounted_total
    assert Decimal(str(payment.outstanding_amount)) == undiscounted_total
