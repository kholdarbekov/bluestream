"""Fixture builders the driver cash-expense tests share (spec 2026-11-17)."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.driver_expense import DriverCashExpense
from business_app.services.cash_collection_service import CashCollectionService
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod
from tests.integration._prepayment_payout_helpers import HAND_IN, driver_headers

EXPENSES = "/api/v1/staff/expenses"
EXPENSE_OPTIONS = EXPENSES + "/options"
WITHDRAW = EXPENSES + "/{}/withdraw"
ADMIN = "/api/v1/admin/driver-expenses"


# --------------------------------------------------------------------------- the driver's claim routes


def options(client, app, driver):
    """The staff bot's read before it starts a claim: the trip, its "Hand in now" (`max_amount`) and waiting total."""
    response = client.get(EXPENSE_OPTIONS, headers=driver_headers(app, driver))
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def claim(client, app, driver, amount, *, key=None, **body):
    """The staff bot's Send on a claim's confirmation screen: a fuel claim of `amount` unless `body` says otherwise
    (`note`, `receipt_file_id`, or any field to override). `key` is the draft's idempotency key, a fresh one unless
    given. Returns (status, json) without asserting."""
    payload = {"category": "fuel", "amount": amount, "idempotency_key": key or uuid4().hex, **body}
    response = client.post(EXPENSES, json=payload, headers=driver_headers(app, driver))
    return response.status_code, response.get_json()


def withdraw(client, app, driver, expense_id):
    """The staff bot's Withdraw tap on a waiting claim. Returns (status, json) without asserting."""
    response = client.post(WITHDRAW.format(expense_id), headers=driver_headers(app, driver))
    return response.status_code, response.get_json()


def my_expenses(client, app, driver):
    """The staff bot's My expenses screen. Returns (status, json) without asserting."""
    response = client.get(EXPENSES, headers=driver_headers(app, driver))
    return response.status_code, response.get_json()


def hand_in_body(client, app, driver, body):
    """The staff bot's hand-in with the body it posts: a typed amount (`declared_cash`), the Cash screen's button
    (`declared_cash`, `session_id`, `basis`) or "settle everything" (`{}`). Returns the response without asserting."""
    return client.post(HAND_IN, json=body, headers=driver_headers(app, driver))


# --------------------------------------------------------------------------- the admin Driver expenses page


def admin_list(client, headers, **params):
    """The admin Driver expenses page's list read, with the page's filters as query parameters (`status`,
    `driver_id`, `category`, `start_date`, `end_date`, `page`, `per_page`)."""
    response = client.get(ADMIN, query_string=params, headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def admin_detail(client, headers, expense_id):
    """The admin page's read of one claim, when its dialog opens: the row, the warnings and the reason minimum."""
    response = client.get(f"{ADMIN}/{expense_id}", headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def _decide(client, headers, expense_id, action, body):
    response = client.post(f"{ADMIN}/{expense_id}/{action}", json=body, headers=headers)
    return response.status_code, response.get_json()


def preview_decision(client, headers, expense_id, approved_amount):
    """The approve dialog's effect line for `approved_amount`, asked again whenever the amount changes. Returns
    (status, json) without asserting."""
    return _decide(client, headers, expense_id, "preview", {"approved_amount": approved_amount})


def approve(client, headers, expense_id, approved_amount):
    """The approve dialog's Approve, with the amount typed (the claim unless lowered). Returns (status, json)."""
    return _decide(client, headers, expense_id, "approve", {"approved_amount": approved_amount})


def reject(client, headers, expense_id, reason):
    """The Reject dialog, with the reason typed. Returns (status, json) without asserting."""
    return _decide(client, headers, expense_id, "reject", {"reason": reason})


def undo_approval(client, headers, expense_id, reason):
    """Undo approval on an approved row, with the reason typed. Returns (status, json) without asserting."""
    return _decide(client, headers, expense_id, "undo", {"reason": reason})


# --------------------------------------------------------------------------- setup rows


def door_cash_on_trip(db, order, customer, driver, *, total, cash):
    """A delivered COD order of `total` with `cash` collected at the door by `driver` on his active trip.

    Returns the collection event's id. A `DeliveryPerson` row is added only when the driver has none
    (`DeliveryPerson.user_id` is unique and `second_delivery_driver` already carries one)."""
    order.status = OrderStatus.DELIVERED
    order.payment_method = PaymentMethod.CASH
    order.total_amount = Decimal(str(total))
    order.paid_at = datetime.now(timezone.utc)
    if DeliveryPerson.query.filter_by(user_id=driver.id).first() is None:
        db.session.add(
            DeliveryPerson(
                user_id=driver.id,
                full_name="D",
                phone=driver.phone,
                email=driver.email,
                is_active=True,
                is_available=True,
            )
        )
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver.id,
        status=DeliveryStatus.DELIVERED,
        scheduled_date=datetime.now(timezone.utc),
        scheduled_time_slot="09:00-12:00",
        actual_delivery_time=datetime.now(timezone.utc),
    )
    db.session.add(delivery)
    db.session.commit()
    svc = CashCollectionService()
    svc.ensure_cod_payment_for_order(order)
    event = svc.post_collection(
        customer_id=customer.id,
        amount=Decimal(str(cash)),
        source="delivery_completion",
        collector_user_id=driver.id,
        recorded_by_user_id=driver.id,
        order_id=order.id,
        delivery_id=delivery.id,
        notes="seed",
    )
    db.session.commit()
    return event.id


def insert_expense(
    db,
    session_id,
    driver_id,
    amount,
    *,
    status="pending",
    approved_amount=None,
    decided_by=None,
    decided_at=None,
    withdrawn_at=None,
    category="fuel",
    note=None,
    submitted_at=None,
    key=None,
    decision_reason="Set up as rejected",
):
    """Setup only: one claim row as the expense routes would have left it, before those routes exist.

    Fills what its status needs so the table's CHECKs pass: approved gets `approved_amount` (the full claim
    unless given), a decider (the driver unless given) and `decided_at`; rejected gets a decider, `decided_at`
    and `decision_reason`; withdrawn gets `withdrawn_at`. Unset times are now; the key is unique unless given.
    Returns the claim's id. It never refreshes the trip's stored figures."""
    now = datetime.now(timezone.utc)
    decided = status in (DriverCashExpense.APPROVED, DriverCashExpense.REJECTED)
    row = DriverCashExpense(
        driver_user_id=driver_id,
        driver_cash_session_id=session_id,
        category=category,
        amount=Decimal(str(amount)),
        note=note,
        status=status,
        idempotency_key=key or uuid4().hex,
        approved_amount=(
            Decimal(str(approved_amount if approved_amount is not None else amount))
            if status == DriverCashExpense.APPROVED
            else None
        ),
        decided_by_user_id=(decided_by or driver_id) if decided else None,
        decided_at=(decided_at or now) if decided else None,
        decision_reason=decision_reason if status == DriverCashExpense.REJECTED else None,
        self_decided=False,
        submitted_at=submitted_at or now,
        withdrawn_at=(withdrawn_at or now) if status == DriverCashExpense.WITHDRAWN else None,
    )
    db.session.add(row)
    db.session.commit()
    return row.id
