"""A late Click payment after a Click -> cash conversion is never refused (owner ruling 2026-10-15).

The customer's Click payment lands after the order was converted to cash (cash at the door, or a
personal card transfer), whatever the cash payment's status: nothing collected, partly paid or
fully paid. Owner's rule:

* the Click money re-converts the order back to Click and settles the payment;
* the cash the conversion had taken is freed: it pays the customer's outstanding debts first, on
  their own and their linked accounts (the wallet cluster), oldest first, and whatever is left is
  the customer's prepaid balance;
* the driver really handed that cash in, so the event amount and cash session never change.

A dead (cancelled) order is not restored: the Click money becomes prepaid balance, as before.

Contract world: catalogue 15 000 (the Click price), contract cash 13 500, so a Click total and a
cash total can only be confused on purpose.
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest

from business_app import db as _db
from business_app.models.corporate import (
    CorporatePrepaymentAccount,
    CorporatePrepaymentEventType,
    CorporatePrepaymentLedger,
)
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.payment import CashCollectionAllocation, CashCollectionEvent, PaymentTransaction
from business_app.models.user import User, UserAddress
from business_app.services.cash_collection_service import CashCollectionService
from shared.enums import (
    CashCollectionSource,
    CorporateContractTrackingMode,
    DeliveryStatus,
    EntitySubtype,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
    UserStatus,
    UserType,
)
from tests.integration.contract_pricing_factory import (
    CASH,
    CATALOGUE,
    admin_headers,
    customer_headers,
    make_contract,
    make_staff,
    make_workplace,
)
from tests.integration.fake_gateways import TEST_CLICK_SHOP_SECRET_KEY, make_click_webhook_form
from tests.integration.tier_discount_factory import post_order

pytestmark = pytest.mark.integration

WEBHOOK_URL = "/api/v1/payments/webhook/click"
FORM = "application/x-www-form-urlencoded"


@pytest.fixture
def world(db, sample_product):
    user, address = make_workplace(db)
    make_contract(db, user, sample_product)
    return user, address


@pytest.fixture
def driver(db):
    user = make_staff(db, UserRole.DELIVERY_DRIVER, "+998901119915")
    db.session.add(
        DeliveryPerson(
            user_id=user.id, full_name="Late Click Driver", phone=user.phone, is_active=True, is_available=True
        )
    )
    db.session.commit()
    return user


@pytest.fixture
def admin(db):
    return make_staff(db, UserRole.ADMIN, "+998901119916")


@pytest.fixture
def audit_events(monkeypatch):
    """Every audit event written, by action. Calls through to the real logger."""
    from business_app.utils.audit_logger import audit_logger

    seen = []
    real = audit_logger.log_event

    def record(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(audit_logger, "log_event", record)
    return seen


@pytest.fixture
def fiscalization_queued(monkeypatch, audit_events):
    """Stands in for the shared `no_fiscalization` stub, and records what the restore had written by then.

    `PaymentService.queue_click_fiscalization` COMMITS (and `_handle_successful_payment` may too), so it marks
    where the late-Click transaction ends: the whole restore, freed cash re-applied and audit written included,
    must already be done when it runs. A bare no-op stub cannot see that ordering.
    """
    from business_app.services.payment_service import PaymentService

    seen = []

    def record(self, payment_id):
        seen.append(
            {
                "payment_id": payment_id,
                "live_allocations": _live_allocation_set(),
                "restore_audited": any(e.get("action") == "click_late_complete_rail_restored" for e in audit_events),
            }
        )

    monkeypatch.setattr(PaymentService, "queue_click_fiscalization", record)
    return seen


def _reload(order_id):
    _db.session.expire_all()
    return Order.query.get(order_id)


def _money(value):
    return Decimal(str(value or 0))


def _fee(order):
    return _money(order.delivery_fee)


def _place(app, user, address, product, method, quantity):
    resp = post_order(
        app, customer_headers(app, user), product_id=product.id, address_id=address.id,
        payment_method=method, quantity=quantity,
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return _reload(resp.get_json()["data"]["order"]["id"])


def _prepare(client, order, trans_id):
    """Click PREPARE, as for every real Click order: it stamps the Click transaction on the payment."""
    form = make_click_webhook_form(
        action="0",
        click_trans_id=trans_id,
        merchant_trans_id=order.order_number,
        amount=str(int(order.total_amount)),
        secret_key=TEST_CLICK_SHOP_SECRET_KEY,
        error=0,
    )
    resp = client.post(WEBHOOK_URL, data=form, content_type=FORM)
    assert resp.get_json()["error"] == 0, resp.get_json()


def _click_order(app, client, user, address, product, quantity, trans_id):
    order = _place(app, user, address, product, "click", quantity)
    _prepare(client, order, trans_id)
    order = _reload(order.id)
    assert order.payment.status == PaymentStatus.PENDING
    assert _money(order.total_amount) == CATALOGUE * quantity + _fee(order)
    return order


def _at_the_door(order_id, driver):
    order = _reload(order_id)
    order.status = OrderStatus.OUT_FOR_DELIVERY
    delivery = Delivery(
        order_id=order.id, delivery_person_id=driver.id, status=DeliveryStatus.ARRIVED,
        scheduled_date=datetime.now(timezone.utc), scheduled_time_slot="09:00-12:00",
    )
    _db.session.add(delivery)
    _db.session.commit()
    return delivery.id


def _deliver(app, driver, order_id, cash):
    """The driver's real "delivered" tap with the cash they took."""
    delivery_id = _at_the_door(order_id, driver)
    with patch("business_app.tasks.notification_tasks.send_delivery_update_task.delay"), patch(
        "business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"
    ):
        resp = app.test_client().put(
            f"/api/v1/staff/delivery/{delivery_id}/status",
            json={"status": "delivered", "metadata": {"cash_collected": str(cash)}},
            headers=customer_headers(app, driver),
        )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return _reload(order_id)


def _personal_card_transfer_of_nothing(app, admin, order):
    """The admin records a personal card transfer of 0: the order converts to cash, nothing is allocated."""
    resp = app.test_client().post(
        "/api/v1/admin/staff/cash-reconciliation/collections",
        json={
            "customer_id": order.user_id,
            "amount": "0",
            "source": CashCollectionSource.PERSONAL_CARD_TRANSFER.value,
            "order_id": order.id,
            "notes": "Customer says a card transfer is on its way",
        },
        headers=admin_headers(app, admin),
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return _reload(order.id)


def _late_complete(client, order, trans_id, amount, *, error=0, drop=()):
    form = make_click_webhook_form(
        action="1",
        click_trans_id=trans_id,
        merchant_trans_id=order.order_number,
        amount=str(int(amount)),
        secret_key=TEST_CLICK_SHOP_SECRET_KEY,
        merchant_prepare_id=str(order.payment.id),
        error=error,
        click_paydoc_id=f"5231{trans_id}",
    )
    for field in drop:  # neither field is signed
        form.pop(field)
    return client.post(WEBHOOK_URL, data=form, content_type=FORM)


def _door_event(order_id):
    return CashCollectionEvent.query.filter_by(
        order_id=order_id, source=CashCollectionSource.DELIVERY_COMPLETION
    ).one()


def _live_allocated(event_id):
    return sum(
        (
            _money(a.allocated_amount)
            for a in CashCollectionAllocation.query.filter_by(cash_collection_event_id=event_id, reversed_at=None)
        ),
        Decimal("0.00"),
    )


def _prepaid(user_id):
    return CashCollectionService().get_customer_prepaid_balance(user_id)


def _live_allocation_set():
    return {
        (a.id, a.payment_id, _money(a.allocated_amount))
        for a in CashCollectionAllocation.query.filter(CashCollectionAllocation.reversed_at.is_(None))
    }


def _assert_restored_before_fiscalization(seen, payment_id, *, paid_payment_id=None):
    """The receipt was queued once, after the freed cash was re-applied and the restore audited."""
    calls = [call for call in seen if call["payment_id"] == payment_id]
    assert len(calls) == 1, seen
    if paid_payment_id is not None:
        assert any(row[1] == paid_payment_id for row in calls[0]["live_allocations"]), (
            "the freed cash had not paid the debt yet when fiscalization was queued (and committed)"
        )
    _db.session.expire_all()
    assert calls[0]["live_allocations"] == _live_allocation_set(), (
        "freed cash was re-applied after fiscalization was queued, in a second transaction"
    )
    assert calls[0]["restore_audited"], "fiscalization was queued before the restore was audited"


def _assert_back_on_click(order, click_total):
    assert order.payment_method == PaymentMethod.CLICK
    assert order.payment.payment_method == PaymentMethod.CLICK
    assert order.payment.status == PaymentStatus.COMPLETED
    assert _money(order.total_amount) == click_total
    assert _money(order.payment.amount) == click_total
    assert order.is_paid is True


def _partly_paid_conversion(app, client, user, address, product, driver, trans_id):
    order = _click_order(app, client, user, address, product, 2, trans_id)
    click_total = _money(order.total_amount)
    order = _deliver(app, driver, order.id, Decimal("10000"))
    assert order.payment_method == PaymentMethod.CASH
    assert _money(order.total_amount) == CASH * 2 + _fee(order)
    # The real status a door shortfall produces.
    assert order.payment.status == PaymentStatus.PARTIALLY_PAID
    return order, click_total


def test_freed_cash_pays_an_older_debt_first_and_the_rest_is_prepaid_balance(
    app, matrix_app, matrix_client, db, world, driver, sample_product, fiscalization_queued, audit_events
):
    user, address = world
    older = _place(app, user, address, sample_product, "cash", 2)
    order = _click_order(app, matrix_client, user, address, sample_product, 3, "940301")
    click_total = _money(order.total_amount)

    order = _deliver(app, driver, order.id, CASH * 3 + _fee(order))
    assert (order.payment_method, order.payment.status) == (PaymentMethod.CASH, PaymentStatus.COMPLETED)
    freed = _money(order.total_amount)
    older = _deliver(app, driver, older.id, Decimal("0"))
    debt = _money(older.payment.outstanding_amount)
    assert debt == CASH * 2 + _fee(older) and debt < freed
    event = _door_event(order.id)
    event_before = (event.id, _money(event.amount), event.driver_cash_session_id)

    resp = _late_complete(matrix_client, order, "940301", click_total)

    assert resp.get_json()["error"] == 0, resp.get_json()
    order = _reload(order.id)
    _assert_back_on_click(order, click_total)
    older = _reload(older.id)
    assert _money(older.payment.outstanding_amount) == debt - min(freed, debt)
    assert older.payment.status == PaymentStatus.COMPLETED
    assert _prepaid(user.id) == freed - debt
    event = _door_event(order.id)
    assert (event.id, _money(event.amount), event.driver_cash_session_id) == event_before
    assert _live_allocated(event.id) + _money(event.unapplied_amount) == _money(event.amount)

    restored = [e for e in audit_events if e.get("action") == "click_late_complete_rail_restored"]
    assert len(restored) == 1
    data = restored[0]["additional_data"]
    assert data["reversed_cash_amount"] == str(freed)
    assert data["debts_paid"] == [{"order_number": older.order_number, "amount": str(debt)}]
    assert data["left_as_prepaid_balance"] == str(freed - debt)
    _assert_restored_before_fiscalization(fiscalization_queued, order.payment.id, paid_payment_id=older.payment.id)


def _individual(db, phone):
    """A phone-verified individual customer with one in-zone address (linking is individuals-only)."""
    user = User(
        email=f"ind-{uuid4().hex[:8]}@example.com",
        phone=phone,
        password_hash="x" * 60,
        first_name="Linked",
        last_name="Customer",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        status=UserStatus.ACTIVE,
        is_verified=True,
        phone_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(user)
    db.session.flush()
    address = UserAddress(
        user_id=user.id, title="Home", full_address="Home 1", street_address="Home 1", city="Tashkent",
        latitude=41.31, longitude=69.28, is_default=True,
    )
    db.session.add(address)
    db.session.commit()
    return user, address


def test_freed_cash_pays_a_linked_accounts_older_debt_first_and_the_rest_is_prepaid_balance(
    app, matrix_app, matrix_client, db, driver, admin, sample_product, fiscalization_queued
):
    """One person, two phone accounts: the wallet cluster's debts are the customer's debts."""
    customer, home = _individual(db, "+998901119917")
    sibling, sibling_home = _individual(db, "+998901119918")
    resp = app.test_client().post(
        f"/api/v1/admin/users/{customer.id}/link",
        json={"secondaryUserId": sibling.id, "reason": "same person, second phone"},
        headers=admin_headers(app, admin),
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    older = _place(app, sibling, sibling_home, sample_product, "cash", 2)
    order = _click_order(app, matrix_client, customer, home, sample_product, 3, "940309")
    click_total = _money(order.total_amount)

    order = _deliver(app, driver, order.id, click_total)
    assert (order.payment_method, order.payment.status) == (PaymentMethod.CASH, PaymentStatus.COMPLETED)
    freed = _money(order.total_amount)
    assert freed == click_total  # a regular customer's price does not depend on the method
    older = _deliver(app, driver, older.id, Decimal("0"))
    debt = _money(older.payment.outstanding_amount)
    assert Decimal("0") < debt < freed

    resp = _late_complete(matrix_client, order, "940309", click_total)

    assert resp.get_json()["error"] == 0, resp.get_json()
    _assert_back_on_click(_reload(order.id), click_total)
    older = _reload(older.id)
    assert _money(older.payment.outstanding_amount) == Decimal("0.00")
    assert older.payment.status == PaymentStatus.COMPLETED
    # The balance is the cluster's one wallet: the same people whose debts were paid.
    assert _prepaid(customer.id) == freed - debt
    assert _prepaid(sibling.id) == freed - debt
    _assert_restored_before_fiscalization(
        fiscalization_queued, _reload(order.id).payment.id, paid_payment_id=older.payment.id
    )


def test_a_grocery_contract_counts_each_real_payment_once_and_charges_the_order_once(
    app, matrix_app, matrix_client, db, driver, sample_product, fiscalization_queued
):
    """Grocery (AMOUNT-mode contract): every delivered order is CHARGEd once; every real payment is COLLECTed once.

    The door cash was COLLECTed when the driver took it, and the late Click payment is COLLECTed when it
    lands: two real payments, so two COLLECTs. The door-cash COLLECT must NOT be reversed: the freed cash is
    still the customer's money (it pays the older debt, the rest is prepaid balance), and spending that
    balance later posts no second COLLECT. So the contract's credit equals the cash ledger's prepaid balance.
    """
    user, address = make_workplace(db, subtype=EntitySubtype.GROCERY_STORE)
    contract, _row = make_contract(db, user, sample_product, tracking_mode=CorporateContractTrackingMode.AMOUNT)
    older = _place(app, user, address, sample_product, "cash", 2)
    order = _click_order(app, matrix_client, user, address, sample_product, 3, "940310")
    click_total = _money(order.total_amount)

    order = _deliver(app, driver, order.id, CASH * 3 + _fee(order))
    assert (order.payment_method, order.payment.status) == (PaymentMethod.CASH, PaymentStatus.COMPLETED)
    freed = _money(order.total_amount)
    older = _deliver(app, driver, older.id, Decimal("0"))
    debt = _money(older.payment.outstanding_amount)
    assert Decimal("0") < debt < freed

    resp = _late_complete(matrix_client, order, "940310", click_total)

    assert resp.get_json()["error"] == 0, resp.get_json()
    order = _reload(order.id)
    _assert_back_on_click(order, click_total)
    assert _money(_reload(older.id).payment.outstanding_amount) == Decimal("0.00")
    prepaid = _prepaid(user.id)
    assert prepaid == freed - debt

    ledger = CorporatePrepaymentLedger.query.filter_by(contract_id=contract.id).all()
    charged_for_order = sum(
        (
            _money(row.amount)
            for row in ledger
            if row.order_id == order.id
            and (
                row.event_type == CorporatePrepaymentEventType.CHARGE
                or (row.entry_metadata or {}).get("kind") == "reprice_true_up"
            )
        ),
        Decimal("0.00"),
    )
    assert charged_for_order == click_total, "the order is charged once, at its Click total"
    collects = sorted(_money(row.amount) for row in ledger if row.event_type == CorporatePrepaymentEventType.COLLECT)
    assert collects == sorted([freed, click_total]), "each real payment is collected exactly once"
    assert not [row for row in ledger if (row.entry_metadata or {}).get("kind") == "collection_reversal"]
    account = CorporatePrepaymentAccount.query.filter_by(contract_id=contract.id).one()
    assert _money(account.lifetime_charged) == click_total + debt
    assert _money(account.lifetime_collected) == freed + click_total
    assert _money(account.outstanding_amount) == -prepaid, "contract credit == the cash ledger's prepaid balance"
    _assert_restored_before_fiscalization(fiscalization_queued, order.payment.id, paid_payment_id=older.payment.id)


def test_partly_paid_conversion_is_restored_and_the_partial_cash_is_prepaid_balance(
    app, matrix_app, matrix_client, db, world, driver, sample_product, fiscalization_queued
):
    user, address = world
    order, click_total = _partly_paid_conversion(app, matrix_client, user, address, sample_product, driver, "940302")
    event = _door_event(order.id)
    event_before = (event.id, _money(event.amount), event.driver_cash_session_id)

    resp = _late_complete(matrix_client, order, "940302", click_total)

    assert resp.get_json()["error"] == 0, resp.get_json()
    order = _reload(order.id)
    _assert_back_on_click(order, click_total)
    assert _prepaid(user.id) == Decimal("10000.00")
    event = _door_event(order.id)
    assert (event.id, _money(event.amount), event.driver_cash_session_id) == event_before
    assert _live_allocated(event.id) + _money(event.unapplied_amount) == _money(event.amount)
    on_order = CashCollectionAllocation.query.filter_by(payment_id=order.payment.id).all()
    assert on_order and all(a.reversed_at is not None for a in on_order)
    _assert_restored_before_fiscalization(fiscalization_queued, order.payment.id)


def test_converted_with_nothing_collected_is_restored_and_the_balance_is_unchanged(
    app, matrix_app, matrix_client, db, world, admin, sample_product, fiscalization_queued
):
    user, address = world
    order = _click_order(app, matrix_client, user, address, sample_product, 2, "940303")
    click_total = _money(order.total_amount)
    order = _personal_card_transfer_of_nothing(app, admin, order)
    assert (order.payment_method, order.payment.status) == (PaymentMethod.CASH, PaymentStatus.PENDING)
    balance_before = _prepaid(user.id)

    resp = _late_complete(matrix_client, order, "940303", click_total)

    assert resp.get_json()["error"] == 0, resp.get_json()
    _assert_back_on_click(_reload(order.id), click_total)
    assert _prepaid(user.id) == balance_before
    _assert_restored_before_fiscalization(fiscalization_queued, _reload(order.id).payment.id)


def test_a_late_click_for_the_cash_amount_is_still_refused(
    app, matrix_app, matrix_client, db, world, driver, sample_product, fiscalization_queued
):
    user, address = world
    order, _click_total = _partly_paid_conversion(app, matrix_client, user, address, sample_product, driver, "940304")

    resp = _late_complete(matrix_client, order, "940304", order.total_amount)

    assert resp.get_json()["error"] == -2, resp.get_json()
    order = _reload(order.id)
    assert order.payment_method == PaymentMethod.CASH
    assert order.payment.status == PaymentStatus.PARTIALLY_PAID


def test_a_re_sent_callback_after_the_restore_answers_already_paid_and_writes_nothing(
    app, matrix_app, matrix_client, db, world, driver, sample_product, fiscalization_queued
):
    from business_app.utils.service_factory import get_payment_service

    user, address = world
    order, click_total = _partly_paid_conversion(app, matrix_client, user, address, sample_product, driver, "940305")
    assert _late_complete(matrix_client, order, "940305", click_total).get_json()["error"] == 0
    order = _reload(order.id)

    def snapshot():
        _db.session.expire_all()
        return (
            PaymentTransaction.query.filter_by(payment_id=order.payment.id).count(),
            CashCollectionAllocation.query.count(),
            CashCollectionEvent.query.count(),
            _prepaid(user.id),
        )

    before = snapshot()
    # Past the route's 24h response cache, the re-send reaches the service.
    redis_client = get_payment_service().redis_client
    redis_client.delete("bs:webhook:dedup:click:940305:1", "bs:webhook:dedup:click:940305:1:response")

    resp = _late_complete(matrix_client, order, "940305", click_total)

    assert resp.get_json()["error"] == -4, resp.get_json()
    assert snapshot() == before
    _assert_back_on_click(_reload(order.id), click_total)


def test_a_cancelled_converted_order_is_not_restored_and_the_click_money_is_prepaid_balance(
    app, matrix_app, matrix_client, db, world, admin, sample_product, fiscalization_queued
):
    user, address = world
    order = _click_order(app, matrix_client, user, address, sample_product, 2, "940306")
    click_total = _money(order.total_amount)
    order = _personal_card_transfer_of_nothing(app, admin, order)
    resp = app.test_client().post(
        f"/api/v1/orders/{order.id}/cancel", json={"expected_via": "cancel"}, headers=customer_headers(app, user)
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert _reload(order.id).status == OrderStatus.CANCELLED

    resp = _late_complete(matrix_client, order, "940306", click_total)

    assert resp.get_json()["error"] == -4, resp.get_json()
    order = _reload(order.id)
    assert (order.status, order.payment_method) == (OrderStatus.CANCELLED, PaymentMethod.CASH)
    assert order.payment.payment_method == PaymentMethod.CASH
    assert _prepaid(user.id) == click_total


@pytest.mark.parametrize(
    "error, drop, answer",
    [(-5017, (), -9), (0, ("click_paydoc_id",), -8)],
    ids=["click_cancelled", "success_without_paydoc_id"],
)
def test_a_non_genuine_callback_never_changes_a_converted_cash_payment(
    app, matrix_app, matrix_client, db, world, driver, sample_product, fiscalization_queued, error, drop, answer
):
    user, address = world
    order, click_total = _partly_paid_conversion(app, matrix_client, user, address, sample_product, driver, "940307")
    before = (order.payment.status, order.payment.provider_transaction_id, dict(order.payment.provider_data["click"]))

    resp = _late_complete(matrix_client, order, "940307", click_total, error=error, drop=drop)

    assert resp.get_json()["error"] == answer, resp.get_json()
    order = _reload(order.id)
    assert order.payment.payment_method == PaymentMethod.CASH
    click_data = dict(order.payment.provider_data["click"])
    click_data.pop("callbacks", None)
    before_click = dict(before[2])
    before_click.pop("callbacks", None)
    assert (order.payment.status, order.payment.provider_transaction_id, click_data) == (
        before[0],
        before[1],
        before_click,
    )


def test_a_callback_without_an_error_code_never_cancels_a_paid_converted_payment(
    app, matrix_app, matrix_client, db, world, driver, sample_product, fiscalization_queued
):
    user, address = world
    order = _click_order(app, matrix_client, user, address, sample_product, 2, "940308")
    click_total = _money(order.total_amount)
    order = _deliver(app, driver, order.id, CASH * 2 + _fee(order))
    assert (order.payment_method, order.payment.status) == (PaymentMethod.CASH, PaymentStatus.COMPLETED)

    resp = _late_complete(matrix_client, order, "940308", click_total, drop=("error",))

    assert resp.get_json()["error"] == -8, resp.get_json()
    order = _reload(order.id)
    assert (order.payment.payment_method, order.payment.status) == (PaymentMethod.CASH, PaymentStatus.COMPLETED)
    assert order.is_paid is True
