"""Undo of a prepayment return: one eligibility function, the same events back (spec 2026-11-10 §4.3, Task 5).

Every test drives the routes the admin screen calls: credit is booked through the admin "Record
collection" route, the return through the payout apply route, the undo through
`POST .../prepayment-payouts/<id>/undo` with the `{reason}` body the Undo button sends, and the ledger
and the driver's session are read back from the database and the admin session-detail route.
"""

import inspect
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import update
from sqlalchemy.orm import Session

from business_app.models.payment import (
    CashCollectionAllocation,
    CashCollectionEvent,
    CustomerCreditPayout,
)
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.prepayment_payout_service import payout_undo_block_reason
from business_app.tasks.customer_link_tasks import reconcile_customer_link_invariants
from business_app.utils.audit_logger import AuditEventType, AuditLogger, AuditSeverity, audit_logger
from shared.enums import DriverCashSessionStatus, OrderStatus
from tests.integration._prepayment_payout_helpers import (
    REASON,
    apply_payout,
    balance,
    collect_on_trip,
    credit,
    from_trip,
    hand_in,
    payout_row,
    return_office,
    session_detail,
    stored,
    unapplied,
    undo,
)
from tests.unit._scope_money_helpers import delivered_cod_order

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- what undo restores


def test_undo_office_return_restores_the_same_events(client, db, admin_user, admin_claim_headers, sample_user):
    e1 = credit(client, admin_claim_headers, sample_user.id, 20000)
    e2 = credit(client, admin_claim_headers, sample_user.id, 30000)
    payout_id = return_office(client, admin_claim_headers, sample_user.id, amount=35000)
    assert (unapplied(db, e1), unapplied(db, e2)) == (Decimal("0.00"), Decimal("15000.00"))

    status, body = undo(client, admin_claim_headers, payout_id)

    assert status == 200, body
    assert (unapplied(db, e1), unapplied(db, e2)) == (Decimal("20000.00"), Decimal("30000.00"))
    payout = payout_row(db, payout_id)
    assert (payout.voided_by_user_id, payout.void_reason) == (admin_user.id, REASON)
    assert payout.voided_at is not None
    data = body["data"]
    assert (data["payout"]["id"], data["payout"]["can_undo"], data["payout"]["undo_block_code"]) == (
        payout_id,
        False,
        "already_undone",
    )
    assert (data["payout"]["void_reason"], data["payout"]["voided_by_user_id"]) == (REASON, admin_user.id)
    assert data["available_prepayment_balance"] == 50000.0
    assert data["driver_cash_session"] is None
    assert balance(sample_user.id) == Decimal("50000.00")
    assert reconcile_customer_link_invariants()["event_conservation_violations"] == []


def test_undo_on_an_active_session_restores_on_hand(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    session_id = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 100000)
    status, body = apply_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=40000))
    assert status == 201, body
    payout_id = body["data"]["payout"]["id"]
    assert session_detail(client, admin_claim_headers, session_id)["expected_cash_on_hand"] == 60000.0

    status, body = undo(client, admin_claim_headers, payout_id)

    assert status == 200, body
    assert body["data"]["payout"]["can_undo"] is False
    session = body["data"]["driver_cash_session"]
    assert (session["id"], session["status"], session["expected_cash_on_hand"], session["expected_cash"]) == (
        session_id,
        "open",
        100000.0,
        100000.0,
    )
    assert session_detail(client, admin_claim_headers, session_id)["expected_cash_on_hand"] == 100000.0
    assert stored(db, session_id).status == DriverCashSessionStatus.OPEN


def test_a_live_current_trip_return_can_be_undone_in_its_apply_answer(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 100000)

    status, body = apply_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=40000))

    assert status == 201, body
    payout = body["data"]["payout"]
    assert (payout["can_undo"], payout["undo_block_code"], body["data"]["next_session_id"]) == (True, None, None)


def test_undo_on_a_partial_session_restores_on_hand_and_keeps_the_handoff(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    session_id = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 100000)
    status, body = apply_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=40000))
    assert status == 201, body
    payout_id = body["data"]["payout"]["id"]
    partial = hand_in(client, app, driver, 20000)  # of the 60000 he still holds
    assert (partial["status"], partial["declared_cash"], partial["expected_cash_on_hand"]) == (
        "partial",
        20000.0,
        60000.0,
    )

    status, body = undo(client, admin_claim_headers, payout_id)

    assert status == 200, body
    session = body["data"]["driver_cash_session"]
    assert (session["id"], session["status"], session["expected_cash_on_hand"], session["declared_cash"]) == (
        session_id,
        "partial",
        100000.0,
        20000.0,
    )
    stored_session = stored(db, session_id)
    assert stored_session.status == DriverCashSessionStatus.PARTIAL
    assert Decimal(str(stored_session.declared_variance)) == Decimal("-80000.00")  # 20000 handed in of 100000 held
    assert session_detail(client, admin_claim_headers, session_id)["remaining_cash_to_submit"] == 80000.0
    assert payout_row(db, payout_id).voided_at is not None


def test_undo_refused_once_the_session_is_submitted(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    session_id = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 108000)
    status, body = apply_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=108000))
    assert status == 201, body
    assert body["data"]["next_session_id"] is not None
    payout_id = body["data"]["payout"]["id"]
    assert body["data"]["payout"]["can_undo"] is False
    assert body["data"]["payout"]["undo_block_code"] == "session_checked"

    status, body = undo(client, admin_claim_headers, payout_id)

    assert status == 400
    assert body["data"]["error_code"] == "PAYOUT_UNDO_BLOCKED"
    assert body["errors"][0].startswith("session_checked:")
    assert payout_row(db, payout_id).voided_at is None
    assert stored(db, session_id).status == DriverCashSessionStatus.SUBMITTED


def test_undo_refused_after_the_driver_submitted_the_trip(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 100000)
    status, body = apply_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=40000))
    assert status == 201, body
    payout_id = body["data"]["payout"]["id"]
    closed = hand_in(client, app, driver, 60000)
    assert closed["status"] == "submitted"

    status, body = undo(client, admin_claim_headers, payout_id)

    assert status == 400
    assert (body["data"]["error_code"], body["errors"][0].split(":")[0]) == ("PAYOUT_UNDO_BLOCKED", "session_checked")
    assert payout_row(db, payout_id).voided_at is None


def test_driver_settled_and_office_ignore_session_state(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    credit(client, admin_claim_headers, sample_user.id, 50000)
    office_id = return_office(client, admin_claim_headers, sample_user.id, key="o-1", amount=10000)
    settled_id = return_office(
        client, admin_claim_headers, sample_user.id, key="s-1", amount=10000, paid_by="driver_settled",
        driver_user_id=driver.id,
    )

    assert payout_undo_block_reason(payout_row(db, office_id)) is None
    assert payout_undo_block_reason(payout_row(db, settled_id)) is None
    status, body = undo(client, admin_claim_headers, settled_id)
    assert status == 200, body
    assert balance(sample_user.id) == Decimal("40000.00")


def test_second_undo_is_refused(client, db, admin_user, admin_claim_headers, sample_user):
    event_id = credit(client, admin_claim_headers, sample_user.id, 50000)
    payout_id = return_office(client, admin_claim_headers, sample_user.id, amount=30000)
    assert undo(client, admin_claim_headers, payout_id)[0] == 200

    status, body = undo(client, admin_claim_headers, payout_id, reason="Trying to undo twice")

    assert status == 400
    assert (body["data"]["error_code"], body["errors"][0].split(":")[0]) == ("PAYOUT_UNDO_BLOCKED", "already_undone")
    assert payout_row(db, payout_id).void_reason == REASON
    assert unapplied(db, event_id) == Decimal("50000.00")


def test_voided_funding_event_blocks_undo(client, db, admin_user, admin_claim_headers, sample_user):
    event_id = credit(client, admin_claim_headers, sample_user.id, 50000)
    payout_id = return_office(client, admin_claim_headers, sample_user.id, amount=30000)
    db.session.execute(
        update(CashCollectionEvent).where(CashCollectionEvent.id == event_id).values(voided_at=datetime.now(UTC))
    )
    db.session.commit()
    db.session.expire_all()

    status, body = undo(client, admin_claim_headers, payout_id)

    assert status == 400
    assert (body["data"]["error_code"], body["errors"][0].split(":")[0]) == (
        "PAYOUT_UNDO_BLOCKED",
        "funding_event_voided",
    )
    assert payout_row(db, payout_id).voided_at is None
    assert unapplied(db, event_id) == Decimal("20000.00")


def test_undo_after_later_consumption_restores_exact_debits(
    client, db, admin_user, admin_claim_headers, sample_user
):
    event_id = credit(client, admin_claim_headers, sample_user.id, 50000)
    payout_id = return_office(client, admin_claim_headers, sample_user.id, amount=30000)
    assert unapplied(db, event_id) == Decimal("20000.00")
    # A later delivered COD debt spends the rest of the event.
    order, payment = delivered_cod_order(db, sample_user, total=Decimal("20000.00"))
    CashCollectionService().apply_customer_prepaid_credit_to_payment(payment)
    db.session.commit()
    assert unapplied(db, event_id) == Decimal("0.00")

    status, body = undo(client, admin_claim_headers, payout_id)

    assert status == 200, body
    assert unapplied(db, event_id) == Decimal("30000.00")
    assert balance(sample_user.id) == Decimal("30000.00")
    assert reconcile_customer_link_invariants()["event_conservation_violations"] == []


def test_undo_does_not_reserve_on_pending_orders(client, db, admin_user, admin_claim_headers, sample_user):
    event_id = credit(client, admin_claim_headers, sample_user.id, 50000)
    payout_id = return_office(client, admin_claim_headers, sample_user.id, amount=50000)
    delivered_cod_order(db, sample_user, total=Decimal("30000.00"), status=OrderStatus.CONFIRMED)
    db.session.commit()
    allocations_before = CashCollectionAllocation.query.count()

    status, body = undo(client, admin_claim_headers, payout_id)

    assert status == 200, body
    assert CashCollectionAllocation.query.count() == allocations_before
    assert CashCollectionAllocation.query.filter_by(allocation_mode="prepaid_reservation").count() == 0
    assert unapplied(db, event_id) == Decimal("50000.00")


# --------------------------------------------------------------------------- inputs, access, audit


@pytest.mark.parametrize("reason", ["abc", "    ", None, 12345, "x" * 501])
def test_undo_reason_rules(client, db, admin_user, admin_claim_headers, sample_user, reason):
    credit(client, admin_claim_headers, sample_user.id, 50000)
    payout_id = return_office(client, admin_claim_headers, sample_user.id, amount=30000)

    status, body = undo(client, admin_claim_headers, payout_id, reason=reason)

    expected = "PAYOUT_REASON_TOO_LONG" if reason == "x" * 501 else "PAYOUT_REASON_REQUIRED"
    assert (status, body["data"]["error_code"]) == (400, expected)
    assert payout_row(db, payout_id).voided_at is None


def test_undo_unknown_payout_is_404(client, db, admin_user, admin_claim_headers):
    status, _ = undo(client, admin_claim_headers, 987654)
    assert status == 404


def test_undo_is_admin_only(
    client, db, admin_user, admin_claim_headers, sample_user, manager_claim_headers, operator_auth_headers
):
    credit(client, admin_claim_headers, sample_user.id, 50000)
    payout_id = return_office(client, admin_claim_headers, sample_user.id, amount=30000)

    for headers in (manager_claim_headers, operator_auth_headers):
        assert undo(client, headers, payout_id)[0] == 403
    assert payout_row(db, payout_id).voided_at is None


def test_undo_audit_after_commit(client, db, admin_user, admin_claim_headers, sample_user, monkeypatch):
    event_id = credit(client, admin_claim_headers, sample_user.id, 50000)
    payout_id = return_office(client, admin_claim_headers, sample_user.id, amount=30000)
    signature = inspect.signature(AuditLogger.log_event)
    calls = []
    seen = {"void_flushed": False, "committed_after_void": False}

    def _record(*args, **kwargs):
        bound = signature.bind(audit_logger, *args, **kwargs)  # TypeError on a call the real method refuses
        bound.apply_defaults()
        arguments = dict(bound.arguments)
        del arguments["self"]
        arguments["committed_before_this_call"] = seen["committed_after_void"]
        calls.append(arguments)
        return "recorded-audit-event"

    def _after_flush(session, flush_context):
        # the undo's own flush: the payout header leaves the live set
        if any(isinstance(obj, CustomerCreditPayout) and obj.voided_at is not None for obj in session.dirty):
            seen["void_flushed"] = True

    def _after_commit(session):
        if seen["void_flushed"]:
            seen["committed_after_void"] = True

    monkeypatch.setattr(audit_logger, "log_event", _record)
    sa_event.listen(Session, "after_flush", _after_flush)
    sa_event.listen(Session, "after_commit", _after_commit)
    try:
        refused_first = undo(client, admin_claim_headers, payout_id, reason="abc")[0]
        assert (refused_first, calls) == (400, [])
        status, body = undo(client, admin_claim_headers, payout_id)
        assert status == 200, body
        assert undo(client, admin_claim_headers, payout_id, reason="Trying to undo twice")[0] == 400
    finally:
        sa_event.remove(Session, "after_flush", _after_flush)
        sa_event.remove(Session, "after_commit", _after_commit)

    assert calls == [
        {
            "event_type": AuditEventType.PAYMENT_REFUNDED,
            "action": "customer_credit_payout_undone",
            "severity": AuditSeverity.HIGH,
            "resource_type": "customer_credit_payout",
            "resource_id": str(payout_id),
            "description": f"Return of 30000.00 to customer {sample_user.id} undone (office)",
            "old_values": None,
            "new_values": None,
            "success": True,
            "error_message": None,
            "duration_ms": None,
            "additional_data": {
                "customer_id": sample_user.id,
                "amount": "30000.00",
                "paid_by": "office",
                "driver_user_id": None,
                "driver_cash_session_id": None,
                "actor_user_id": admin_user.id,
                "reason": REASON,
                "debits": [{"event_id": event_id, "amount": "30000.00"}],
            },
            "committed_before_this_call": True,
        }
    ]
