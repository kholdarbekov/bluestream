"""A collection a live prepayment return drew on cannot be corrected (spec 2026-11-10 §4.3a, Task 6).

Scenario: a delivered COD order of 100,000, door cash 130,000 (surplus 30,000 sits on the event as
prepaid credit) and an office return of 30,000. Voiding that event would reset `unapplied_amount` to
the full amount and re-post fresh credit, handing the paid-out 30,000 back as spendable money.

Every test drives the routes the admin screens call: collected-cash preview/apply, the cash
reconciliation event-adjust route, the order detail, the payout apply and undo routes.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.payment import CashCollectionEvent, DriverCashSession
from business_app.services.cash_collection_service import CashCollectionService
from business_app.utils.exceptions import ValidationError
from shared.enums import DeliveryStatus, DriverCashSessionStatus, OrderStatus, PaymentMethod

pytestmark = pytest.mark.integration

BASE = "/api/v1/admin/staff/cash-reconciliation"
APPLY = BASE + "/customers/{}/prepayment-payouts"
UNDO = BASE + "/prepayment-payouts/{}/undo"
COLLECTIONS = BASE + "/collections"
ADJUST = BASE + "/events/{}/adjust"


def _blocking(payout_ids):
    return (
        f"event_funds_prepayment_return: prepayment return(s) {payout_ids} drew on this collection; "
        "undo the return first"
    )


@pytest.fixture
def door_cash(db, sample_order, sample_user, delivery_driver):
    """Order 100,000 COD, door cash 130,000: the event holds 30,000 of surplus credit."""
    sample_order.status = OrderStatus.DELIVERED
    sample_order.payment_method = PaymentMethod.CASH
    sample_order.total_amount = Decimal("100000")
    sample_order.paid_at = datetime.now(timezone.utc)
    db.session.add(
        DeliveryPerson(
            user_id=delivery_driver.id,
            full_name="D",
            phone=delivery_driver.phone,
            email=delivery_driver.email,
            is_active=True,
            is_available=True,
        )
    )
    delivery = Delivery(
        order_id=sample_order.id,
        delivery_person_id=delivery_driver.id,
        status=DeliveryStatus.DELIVERED,
        scheduled_date=datetime.now(timezone.utc),
        scheduled_time_slot="09:00-12:00",
        actual_delivery_time=datetime.now(timezone.utc),
    )
    db.session.add(delivery)
    db.session.commit()
    svc = CashCollectionService()
    svc.ensure_cod_payment_for_order(sample_order)
    event = svc.post_collection(
        customer_id=sample_user.id,
        amount=Decimal("130000"),
        source="delivery_completion",
        collector_user_id=delivery_driver.id,
        recorded_by_user_id=delivery_driver.id,
        order_id=sample_order.id,
        delivery_id=delivery.id,
        notes="seed",
    )
    db.session.commit()
    return sample_order, event.id


def _return(client, headers, customer_id, amount, key="r-1"):
    response = client.post(
        APPLY.format(customer_id),
        json={"paid_by": "office", "amount": amount, "reason": "Customer moved away", "idempotency_key": key},
        headers=headers,
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["payout"]["id"]


def _preview(client, headers, order_id, new_amount=100000):
    response = client.post(
        f"/api/v1/admin/orders/{order_id}/collected-cash/preview", json={"new_amount": new_amount}, headers=headers
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def _event(db, event_id):
    db.session.expire_all()
    return db.session.get(CashCollectionEvent, event_id)


def test_collected_cash_preview_names_the_return(client, db, admin_claim_headers, sample_user, door_cash):
    order, event_id = door_cash
    assert _event(db, event_id).unapplied_amount == Decimal("30000.00")
    payout_id = _return(client, admin_claim_headers, sample_user.id, 30000)

    data = _preview(client, admin_claim_headers, order.id)

    assert data["is_editable"] is False
    assert data["blocking_reasons"] == [_blocking([payout_id])]


def test_collected_cash_apply_is_refused(client, db, admin_claim_headers, sample_user, door_cash):
    order, event_id = door_cash
    payout_id = _return(client, admin_claim_headers, sample_user.id, 30000)

    response = client.post(
        f"/api/v1/admin/orders/{order.id}/collected-cash",
        json={"new_amount": 100000, "reason": "Driver typo fix"},
        headers=admin_claim_headers,
    )

    assert response.status_code == 400, response.get_json()
    assert response.get_json()["errors"][0] == _blocking([payout_id])
    event = _event(db, event_id)
    assert (event.voided_at, event.amount, event.unapplied_amount) == (None, Decimal("130000.00"), Decimal("0.00"))
    assert CashCollectionService().get_customer_prepaid_balance(sample_user.id) == Decimal("0.00")


def _submit_the_session_of(db, event_id):
    """The driver's session is SUBMITTED: the one state the event-adjust route accepts, so the only thing that can
    still refuse an adjust is the prepayment return."""
    event = _event(db, event_id)
    assert event.driver_cash_session_id is not None
    db.session.get(DriverCashSession, event.driver_cash_session_id).status = DriverCashSessionStatus.SUBMITTED
    db.session.commit()


def _adjust(client, headers, event_id, new_amount=100000):
    return client.post(
        ADJUST.format(event_id),
        json={"new_amount": new_amount, "reason": "Driver typo fix"},
        headers=headers,
    )


def test_admin_event_adjust_is_refused(client, db, admin_claim_headers, sample_user, door_cash):
    _, event_id = door_cash
    _submit_the_session_of(db, event_id)
    payout_id = _return(client, admin_claim_headers, sample_user.id, 30000)

    response = _adjust(client, admin_claim_headers, event_id)

    assert response.status_code == 400, response.get_json()
    assert response.get_json()["errors"] == [
        f"Collection {event_id} funded prepayment return(s) [{payout_id}]; undo the return first"
    ]
    event = _event(db, event_id)
    assert (event.voided_at, event.unapplied_amount) == (None, Decimal("0.00"))

    # Control: undone, the very same adjust goes through, so the return was the only thing refusing it.
    undo = client.post(UNDO.format(payout_id), json={"reason": "Recorded by mistake"}, headers=admin_claim_headers)
    assert undo.status_code == 200, undo.get_json()
    assert _adjust(client, admin_claim_headers, event_id).status_code == 200


def test_order_detail_stops_offering_the_edit(client, db, admin_claim_headers, sample_user, door_cash):
    order, _ = door_cash
    url = f"/api/v1/admin/orders/{order.id}"
    before = client.get(url, headers=admin_claim_headers)
    assert before.status_code == 200, before.get_json()
    assert before.get_json()["data"]["order"]["is_collected_cash_editable"] is True

    _return(client, admin_claim_headers, sample_user.id, 30000)

    after = client.get(url, headers=admin_claim_headers)
    assert after.get_json()["data"]["order"]["is_collected_cash_editable"] is False


def _edit_flags(client, headers, order_id):
    response = client.get(f"/api/v1/admin/orders/{order_id}", headers=headers)
    assert response.status_code == 200, response.get_json()
    order = response.get_json()["data"]["order"]
    return order["is_collected_cash_editable"], order["collected_cash_edit_block_code"]


def test_order_detail_names_the_return_that_hides_the_edit(client, db, admin_claim_headers, sample_user, door_cash):
    """§4.3a: the detail says WHY "Edit collected cash" is gone, from the same live-payout read the correction
    guards use, and stops saying it once the return is undone."""
    order, _ = door_cash
    before = _edit_flags(client, admin_claim_headers, order.id)
    payout_id = _return(client, admin_claim_headers, sample_user.id, 30000)
    during = _edit_flags(client, admin_claim_headers, order.id)
    undo = client.post(UNDO.format(payout_id), json={"reason": "Recorded by mistake"}, headers=admin_claim_headers)
    assert undo.status_code == 200, undo.get_json()
    after = _edit_flags(client, admin_claim_headers, order.id)

    assert (before, during, after) == (
        (True, None),
        (False, "event_funds_prepayment_return"),
        (True, None),
    )


def test_an_expired_window_is_not_blamed_on_the_return(client, db, admin_claim_headers, sample_user, door_cash):
    """Past the edit window, undoing the return would not bring the edit back, so the return is not named."""
    order, _ = door_cash
    _return(client, admin_claim_headers, sample_user.id, 30000)
    order.delivery.actual_delivery_time = datetime.now(timezone.utc) - timedelta(hours=100)
    db.session.commit()

    assert _edit_flags(client, admin_claim_headers, order.id) == (False, None)


def test_undo_reopens_the_correction(client, db, admin_claim_headers, sample_user, door_cash):
    order, event_id = door_cash
    payout_id = _return(client, admin_claim_headers, sample_user.id, 30000)
    assert _preview(client, admin_claim_headers, order.id)["is_editable"] is False

    undo = client.post(UNDO.format(payout_id), json={"reason": "Recorded by mistake"}, headers=admin_claim_headers)
    assert undo.status_code == 200, undo.get_json()

    data = _preview(client, admin_claim_headers, order.id)
    assert (data["is_editable"], data["blocking_reasons"]) == (True, [])
    assert CashCollectionService().live_payout_ids_for_event(event_id) == []
    assert _edit_flags(client, admin_claim_headers, order.id) == (True, None)  # the order detail offers it again


def test_reverse_collection_event_refuses_directly(client, db, admin_user, admin_claim_headers, sample_user, door_cash):
    _, event_id = door_cash
    payout_id = _return(client, admin_claim_headers, sample_user.id, 30000)

    with pytest.raises(ValidationError) as caught:
        CashCollectionService().reverse_collection_event(event_id, reversed_by_user_id=admin_user.id, reason="typo")

    assert caught.value.error_code == "EVENT_FUNDS_PREPAYMENT_RETURN"
    assert caught.value.message == (
        f"Collection {event_id} funded prepayment return(s) [{payout_id}]; undo the return first"
    )
    event = _event(db, event_id)
    assert (event.voided_at, event.unapplied_amount) == (None, Decimal("0.00"))


def test_an_unrelated_collection_stays_correctable(client, db, admin_claim_headers, sample_user, door_cash):
    order, event_id = door_cash
    payout_id = _return(client, admin_claim_headers, sample_user.id, 30000)
    # Prepaid at the office AFTER the return: the return drew on the door cash, not on this event.
    other = client.post(
        COLLECTIONS,
        json={"customer_id": sample_user.id, "amount": 50000, "source": "admin_adjustment", "notes": "Prepaid"},
        headers=admin_claim_headers,
    )
    assert other.status_code == 201, other.get_json()
    other_id = other.get_json()["data"]["cash_collection_event"]["id"]
    svc = CashCollectionService()
    assert (svc.live_payout_ids_for_event(event_id), svc.live_payout_ids_for_event(other_id)) == ([payout_id], [])

    response = client.post(
        ADJUST.format(other_id),
        json={"new_amount": 40000, "reason": "Counted again at the office"},
        headers=admin_claim_headers,
    )

    assert response.status_code == 200, response.get_json()
    assert _event(db, other_id).voided_at is not None
    assert Decimal(str(response.get_json()["data"]["cash_collection_event"]["amount"])) == Decimal("40000")


def test_two_returns_on_one_event_stay_blocked_until_both_are_undone(
    client, db, admin_claim_headers, sample_user, door_cash
):
    order, event_id = door_cash
    first = _return(client, admin_claim_headers, sample_user.id, 10000, key="r-first")
    second = _return(client, admin_claim_headers, sample_user.id, 20000, key="r-second")
    assert CashCollectionService().live_payout_ids_for_event(event_id) == [first, second]
    assert _preview(client, admin_claim_headers, order.id)["blocking_reasons"] == [_blocking([first, second])]

    undone = client.post(UNDO.format(first), json={"reason": "Recorded by mistake"}, headers=admin_claim_headers)
    assert undone.status_code == 200, undone.get_json()

    data = _preview(client, admin_claim_headers, order.id)
    assert (data["is_editable"], data["blocking_reasons"]) == (False, [_blocking([second])])
    assert _edit_flags(client, admin_claim_headers, order.id) == (False, "event_funds_prepayment_return")
    refused = client.post(
        f"/api/v1/admin/orders/{order.id}/collected-cash",
        json={"new_amount": 100000, "reason": "Driver typo fix"},
        headers=admin_claim_headers,
    )
    assert (refused.status_code, refused.get_json()["errors"][0]) == (400, _blocking([second]))

    undone = client.post(UNDO.format(second), json={"reason": "Recorded by mistake"}, headers=admin_claim_headers)
    assert undone.status_code == 200, undone.get_json()
    assert _preview(client, admin_claim_headers, order.id)["is_editable"] is True
    assert _edit_flags(client, admin_claim_headers, order.id) == (True, None)


@pytest.fixture
def returned_from_office_credit(client, db, admin_user, admin_claim_headers, sample_user, door_cash):
    """The customer also prepaid 50,000 at the office, BEFORE the door cash: a 40,000 return draws only that older
    credit (oldest first) and never touches the door cash event, whose 30,000 surplus stays free."""
    order, door_event_id = door_cash
    office = CashCollectionService().post_collection(
        customer_id=sample_user.id,
        amount=Decimal("50000"),
        source="admin_adjustment",
        recorded_by_user_id=admin_user.id,
        notes="Prepaid in cash at the office",
        occurred_at=datetime.now(timezone.utc) - timedelta(days=5),
    )
    db.session.commit()
    payout_id = _return(client, admin_claim_headers, sample_user.id, 40000)
    svc = CashCollectionService()
    assert (svc.live_payout_ids_for_event(office.id), svc.live_payout_ids_for_event(door_event_id)) == ([payout_id], [])
    return order, door_event_id


def test_the_door_cash_of_a_customer_who_got_office_credit_back_stays_correctable(
    client, db, admin_claim_headers, sample_user, returned_from_office_credit
):
    """A different event: the return drew on the office credit, so every correction surface of the door cash stays open."""
    order, door_event_id = returned_from_office_credit

    preview = _preview(client, admin_claim_headers, order.id)
    flags = _edit_flags(client, admin_claim_headers, order.id)
    applied = client.post(
        f"/api/v1/admin/orders/{order.id}/collected-cash",
        json={"new_amount": 100000, "reason": "Driver typo fix"},
        headers=admin_claim_headers,
    )

    assert (preview["is_editable"], preview["blocking_reasons"]) == (True, [])
    assert flags == (True, None)
    assert applied.status_code == 200, applied.get_json()


def test_the_event_adjust_of_that_door_cash_goes_through(
    client, db, admin_claim_headers, sample_user, returned_from_office_credit
):
    _, door_event_id = returned_from_office_credit
    _submit_the_session_of(db, door_event_id)

    response = _adjust(client, admin_claim_headers, door_event_id)

    assert response.status_code == 200, response.get_json()
    assert _event(db, door_event_id).voided_at is not None
