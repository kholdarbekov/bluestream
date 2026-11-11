"""The prepayment history tells the truth about returns (spec 2026-11-10 §4.4, Task 8).

Every test reads `GET .../customers/<id>/prepayment-history` the way the Prepayments page and the Users
card do (admin role claim), with credit booked through the real "Record collection" route and the return
through the real payout route. The customer-facing reads go through the real customer routes.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy import update

from business_app.models.payment import CashCollectionEvent
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.customer_link_service import CustomerLinkService
from tests.integration._prepayment_payout_helpers import (
    APPLY,
    HISTORY,
    UNDO,
    apply_payout,
    collect_on_trip,
    credit,
    from_trip,
)
from tests.unit._scope_money_helpers import delivered_cod_order, link_users, make_user

pytestmark = pytest.mark.integration

REASON = "Customer moved away"


# --------------------------------------------------------------------------- helpers


def _return(client, headers, customer_id, amount, key="h-1"):
    response = client.post(
        APPLY.format(customer_id),
        json={"paid_by": "office", "reason": REASON, "idempotency_key": key, "amount": amount},
        headers=headers,
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["payout"]["id"]


def _history(client, headers, customer_id, **query):
    qs = "&".join(f"{k}={v}" for k, v in query.items())
    response = client.get(HISTORY.format(customer_id) + (f"?{qs}" if qs else ""), headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def _event(data, event_id):
    return next(e for e in data["events"] if e["id"] == event_id)


# --------------------------------------------------------------------------- the rows


def test_history_lists_the_returned_row(client, db, admin_user, admin_claim_headers, sample_user):
    e1 = credit(client, admin_claim_headers, sample_user.id, 20000)
    e2 = credit(client, admin_claim_headers, sample_user.id, 30000)
    payout_id = _return(client, admin_claim_headers, sample_user.id, 35000)

    data = _history(client, admin_claim_headers, sample_user.id)

    assert len(data["payouts"]) == 1
    row = data["payouts"][0]
    assert row["id"] == payout_id
    assert row["customer_id"] == sample_user.id
    assert row["amount"] == 35000.0
    assert (row["paid_by"], row["driver_user_id"], row["reason"]) == ("office", None, REASON)
    assert row["debits"] == [{"event_id": e1, "amount": 20000.0}, {"event_id": e2, "amount": 15000.0}]
    assert (row["can_undo"], row["undo_block_code"], row["voided_at"]) == (True, None, None)
    assert row["actor_user_id"] == admin_user.id
    assert data["lifetime_collected"] == 50000.0
    assert data["lifetime_returned"] == 35000.0
    assert data["lifetime_applied"] == 0.0
    assert data["available_prepayment_balance"] == 15000.0
    assert _event(data, e1)["returned_amount"] == 20000.0
    assert _event(data, e2)["returned_amount"] == 15000.0


def test_lifetime_applied_excludes_returns(client, db, admin_user, admin_claim_headers, sample_user):
    credit(client, admin_claim_headers, sample_user.id, 50000)
    _order, payment = delivered_cod_order(db, sample_user, total=Decimal("10000.00"))
    CashCollectionService().apply_customer_prepaid_credit_to_payment(payment)
    db.session.commit()
    _return(client, admin_claim_headers, sample_user.id, 20000)

    data = _history(client, admin_claim_headers, sample_user.id)

    assert data["lifetime_collected"] == 50000.0
    assert data["lifetime_returned"] == 20000.0
    assert data["lifetime_applied"] == 10000.0
    assert data["available_prepayment_balance"] == 20000.0


def test_a_voided_drawn_event_leaves_lifetime_returned_and_applied_stays_non_negative(
    client, db, admin_user, admin_claim_headers, sample_user
):
    """Voiding an event a live return drew on takes the event out of lifetime_collected and its debit out of
    lifetime_returned together; the remainder (applied) is never negative."""
    voided = credit(client, admin_claim_headers, sample_user.id, 20000)
    kept = credit(client, admin_claim_headers, sample_user.id, 30000)
    _return(client, admin_claim_headers, sample_user.id, 35000)  # 20000 from the first event, 15000 from the second
    db.session.execute(
        update(CashCollectionEvent).where(CashCollectionEvent.id == voided).values(voided_at=datetime.now(UTC))
    )
    db.session.commit()

    data = _history(client, admin_claim_headers, sample_user.id)

    assert data["lifetime_collected"] == 30000.0
    assert data["lifetime_returned"] == 15000.0  # the voided event's 20000 is no longer returned money
    assert data["lifetime_applied"] == 0.0
    assert data["available_prepayment_balance"] == 15000.0
    assert _event(data, kept)["returned_amount"] == 15000.0


def test_fully_applied_toggle_shows_a_drained_event_as_returned(
    client, db, admin_user, admin_claim_headers, sample_user
):
    e1 = credit(client, admin_claim_headers, sample_user.id, 20000)
    e2 = credit(client, admin_claim_headers, sample_user.id, 30000)
    _return(client, admin_claim_headers, sample_user.id, 20000)

    hidden = _history(client, admin_claim_headers, sample_user.id, include_fully_applied=0)
    shown = _history(client, admin_claim_headers, sample_user.id, include_fully_applied=1)

    assert [e["id"] for e in hidden["events"]] == [e2]
    assert {e["id"] for e in shown["events"]} == {e1, e2}
    drained = _event(shown, e1)
    assert (drained["unapplied_amount"], drained["returned_amount"]) == (0.0, 20000.0)
    assert drained["allocations"] == []
    assert _event(shown, e2)["returned_amount"] == 0.0
    # A return is never counted as applied.
    assert (shown["lifetime_applied"], shown["lifetime_returned"]) == (0.0, 20000.0)


def test_include_voided_hides_undone_payouts(client, db, admin_user, admin_claim_headers, sample_user):
    credit(client, admin_claim_headers, sample_user.id, 50000)
    undone = _return(client, admin_claim_headers, sample_user.id, 10000, key="h-undone")
    kept = _return(client, admin_claim_headers, sample_user.id, 5000, key="h-kept")
    response = client.post(UNDO.format(undone), json={"reason": "Recorded by mistake"}, headers=admin_claim_headers)
    assert response.status_code == 200, response.get_json()

    shown = _history(client, admin_claim_headers, sample_user.id, include_voided=1)
    hidden = _history(client, admin_claim_headers, sample_user.id, include_voided=0)

    assert sorted(p["id"] for p in shown["payouts"]) == sorted([undone, kept])
    undone_row = next(p for p in shown["payouts"] if p["id"] == undone)
    assert (undone_row["can_undo"], undone_row["undo_block_code"]) == (False, "already_undone")
    assert undone_row["voided_at"] is not None
    assert [p["id"] for p in hidden["payouts"]] == [kept]
    # An undone payout leaves the lifetime figure.
    assert shown["lifetime_returned"] == hidden["lifetime_returned"] == 5000.0


def test_payouts_are_newest_paid_first(client, db, admin_user, admin_claim_headers, sample_user):
    credit(client, admin_claim_headers, sample_user.id, 50000)
    older = _return(client, admin_claim_headers, sample_user.id, 1000, key="h-old")
    newer = _return(client, admin_claim_headers, sample_user.id, 2000, key="h-new")

    data = _history(client, admin_claim_headers, sample_user.id)

    assert [p["id"] for p in data["payouts"]] == [newer, older]


def test_users_card_limit_does_not_hide_payouts(client, db, admin_user, admin_claim_headers, sample_user):
    first = credit(client, admin_claim_headers, sample_user.id, 1000)
    for _ in range(24):
        credit(client, admin_claim_headers, sample_user.id, 1000)
    payout_id = _return(client, admin_claim_headers, sample_user.id, 1000)

    data = _history(client, admin_claim_headers, sample_user.id, limit=20)

    assert len(data["events"]) == 20
    assert first not in [e["id"] for e in data["events"]]  # the oldest event is past the limit
    assert [p["debits"] for p in data["payouts"]] == [[{"event_id": first, "amount": 1000.0}]]
    assert data["payouts"][0]["id"] == payout_id
    assert data["lifetime_returned"] == 1000.0


def test_history_after_unlink_follows_the_drawn_events(client, db, admin_user, admin_claim_headers):
    a, b = make_user(db), make_user(db)
    link_users(db, [a, b])
    now = datetime.now(UTC)
    a_event = credit(client, admin_claim_headers, a.id, 20000)
    b_event = credit(client, admin_claim_headers, b.id, 30000)
    # Make A's event the oldest, so a return of 15 000 via B draws A's event only.
    from business_app.models.payment import CashCollectionEvent

    db.session.get(CashCollectionEvent, a_event).occurred_at = now - timedelta(days=2)
    db.session.get(CashCollectionEvent, b_event).occurred_at = now - timedelta(days=1)
    db.session.commit()
    payout_id = _return(client, admin_claim_headers, b.id, 15000)

    linked = _history(client, admin_claim_headers, b.id)
    assert [p["id"] for p in linked["payouts"]] == [payout_id]
    assert linked["lifetime_returned"] == 15000.0

    CustomerLinkService().unlink_account(b.id, admin_user.id, "Not the same person")
    db.session.expire_all()

    a_history = _history(client, admin_claim_headers, a.id)
    b_history = _history(client, admin_claim_headers, b.id)
    assert [p["id"] for p in a_history["payouts"]] == [payout_id]
    assert a_history["payouts"][0]["customer_id"] == b.id  # the row still names who was paid
    assert a_history["payouts"][0]["debits"] == [{"event_id": a_event, "amount": 15000.0}]
    assert (a_history["lifetime_returned"], a_history["lifetime_applied"]) == (15000.0, 0.0)
    assert _event(a_history, a_event)["returned_amount"] == 15000.0
    assert b_history["payouts"] == []
    assert b_history["lifetime_returned"] == 0.0
    assert b_history["lifetime_collected"] == 30000.0
    assert _event(b_history, b_event)["returned_amount"] == 0.0

    # Undo from A's row restores A's event.
    response = client.post(UNDO.format(a_history["payouts"][0]["id"]), json={"reason": "Wrong person"},
                           headers=admin_claim_headers)
    assert response.status_code == 200, response.get_json()
    a_after = _history(client, admin_claim_headers, a.id)
    assert a_after["available_prepayment_balance"] == 20000.0
    assert _event(a_after, a_event)["unapplied_amount"] == 20000.0
    assert (a_after["lifetime_returned"], _event(a_after, a_event)["returned_amount"]) == (0.0, 0.0)


def test_can_undo_follows_the_eligibility_function(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 108000)
    status, body = apply_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=108000))
    assert status == 201, body  # the whole trip cash returned: the session zero-closes

    data = _history(client, admin_claim_headers, sample_user.id)

    row = data["payouts"][0]
    assert (row["paid_by"], row["driver_user_id"]) == ("driver_current_session", driver.id)
    assert (row["can_undo"], row["undo_block_code"]) == (False, "session_checked")
    assert row["driver_cash_session_id"] == body["data"]["payout"]["driver_cash_session_id"]


# --------------------------------------------------------------------------- customer-facing reads


def test_customer_reads_follow_the_return(
    app, client, db, admin_user, admin_claim_headers, sample_user, sample_product
):
    credit(client, admin_claim_headers, sample_user.id, 50000)
    _return(client, admin_claim_headers, sample_user.id, 35000)
    with app.app_context():
        token = create_access_token(identity=str(sample_user.id))
    customer = {"Authorization": f"Bearer {token}"}

    added = client.post("/api/v1/cart/items", json={"product_id": sample_product.id, "quantity": 1}, headers=customer)
    assert added.status_code in (200, 201), added.get_json()

    summary = client.get("/api/v1/payments/my-cod-summary", headers=customer)
    cart = client.get("/api/v1/cart/", headers=customer)

    assert summary.status_code == 200, summary.get_json()
    assert summary.get_json()["data"]["available_prepayment_balance"] == 15000.0
    assert cart.status_code == 200, cart.get_json()
    assert cart.get_json()["data"]["cart"]["cod_prepayment"]["available_balance"] == 15000.0
