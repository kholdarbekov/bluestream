"""A collected-cash correction cannot pull the money out from under a waiting claim (spec 2026-11-17 §6.5, owner Q2).

While a claim waits on a trip, the trip's on hand may not drop below what the driver handed in plus the claims
waiting (the expense floor, so "Hand in now" never goes below 0). The order detail publishes the lowest amount the
edit accepts, the preview names the floor, and both correction routes refuse with 400 `EVENT_FUNDS_DRIVER_EXPENSE`.
A correction upwards, or one made once the claim is decided, goes through as today.

Every test drives the routes the admin screens call, with the bodies they send: the order detail, the collected-cash
preview and apply (`/admin/orders/<id>/collected-cash[/preview]`), the cash reconciliation event adjust and the
session detail. Door cash lands through `door_cash_on_trip`, the claim through the staff bot's Send, the hand-in
through the bot's hand-in tap and decisions through the admin Driver expenses page. Figures: a 100,000 COD order paid
100,000 in cash, a 30,000 claim and 70,000 handed in, so the floor is 100,000.
"""

import inspect

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from business_app.models.driver_expense import DriverCashExpense
from business_app.models.payment import CashCollectionEvent
from business_app.services.cash_collection_service import CashCollectionService
from business_app.utils.audit_logger import AuditEventType, AuditLogger, AuditSeverity, audit_logger
from tests.integration._driver_expense_helpers import (
    approve,
    claim,
    door_cash_on_trip,
    insert_expense,
    reject,
    withdraw,
)
from tests.integration._prepayment_payout_helpers import BASE, hand_in, session_detail

pytestmark = pytest.mark.integration

ADJUST = BASE + "/events/{}/adjust"
REASON = "Driver miscounted"
CODE = "EVENT_FUNDS_DRIVER_EXPENSE"


def _why(session_id, after, floor):
    """The refusal's sentence, with the figures the spec's example gives."""
    return (
        f"this correction would leave trip #{session_id} {after} on hand, below what the driver handed in plus the "
        f"expenses waiting ({floor}); decide the waiting expense first"
    )


def _order_detail(client, headers, order_id):
    """The admin order detail the "Edit collected cash" dialog reads its flags from."""
    response = client.get(f"/api/v1/admin/orders/{order_id}", headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]["order"]


def _preview(client, headers, order_id, new_amount):
    """The dialog's preview, asked whenever the amount changes."""
    response = client.post(
        f"/api/v1/admin/orders/{order_id}/collected-cash/preview", json={"new_amount": new_amount}, headers=headers
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def _apply(client, headers, order_id, new_amount):
    """The dialog's Save, with the reason typed. Returns the response without asserting."""
    return client.post(
        f"/api/v1/admin/orders/{order_id}/collected-cash",
        json={"new_amount": new_amount, "reason": REASON},
        headers=headers,
    )


def _adjust(client, headers, event_id, new_amount):
    """The cash reconciliation screen's "Adjust amount" on one collection. Returns the response without asserting."""
    return client.post(ADJUST.format(event_id), json={"new_amount": new_amount, "reason": REASON}, headers=headers)


def _correct(route, client, headers, trip, new_amount):
    """A correction through either screen: the order's collected-cash edit or the collection's adjust."""
    if route == "apply":
        return _apply(client, headers, trip["order"].id, new_amount)
    return _adjust(client, headers, trip["event_id"], new_amount)


def _error_code(response):
    return (response.get_json().get("data") or {}).get("error_code")


def _event(db, event_id):
    db.session.expire_all()
    return db.session.get(CashCollectionEvent, event_id)


def _order_events(db, order_id):
    """The order's collections, oldest first: (id, amount, voided)."""
    db.session.expire_all()
    rows = CashCollectionEvent.query.filter_by(order_id=order_id).order_by(CashCollectionEvent.id).all()
    return [(row.id, float(row.amount), row.voided_at is not None) for row in rows]


@pytest.fixture
def audits(monkeypatch):
    """Every audit entry from here on, bound like the real `log_event` call (the payout tests' recorder)."""
    signature = inspect.signature(AuditLogger.log_event)
    calls = []

    def _record(*args, **kwargs):
        bound = signature.bind(audit_logger, *args, **kwargs)  # TypeError on a call the real method refuses
        bound.apply_defaults()
        arguments = dict(bound.arguments)
        del arguments["self"]
        calls.append(arguments)
        return "recorded-audit-event"

    monkeypatch.setattr(audit_logger, "log_event", _record)
    return calls


@pytest.fixture
def trip(app, client, db, sample_order, sample_user, second_delivery_driver):
    """100,000 collected at the door on the driver's trip and 70,000 of it handed in: the trip is partial.

    Returns the order, the collection's id and the trip's id. No claim waits yet."""
    event_id = door_cash_on_trip(db, sample_order, sample_user, second_delivery_driver, total=100000, cash=100000)
    session_id = db.session.get(CashCollectionEvent, event_id).driver_cash_session_id
    return {"order": sample_order, "event_id": event_id, "session_id": session_id}


def _claim_then_hand_in(client, app, driver, trip):
    """The driver claims 30,000 for fuel, then hands in the 70,000 his Cash screen asks for. Returns the claim's id."""
    status, body = claim(client, app, driver, 30000)
    assert status == 201, body
    assert hand_in(client, app, driver, 70000)["status"] == "partial"
    return body["data"]["expense"]["id"]


@pytest.fixture
def waiting_claim(app, client, second_delivery_driver, trip):
    """`trip` with a 30,000 claim waiting and 70,000 handed in: the floor is 70,000 + 30,000 = 100,000."""
    return {**trip, "expense_id": _claim_then_hand_in(client, app, second_delivery_driver, trip)}


# --------------------------------------------------------------------------- a waiting claim sets the floor


def test_a_correction_below_the_floor_is_refused_everywhere(client, db, admin_claim_headers, waiting_claim):
    order, event_id, sid = waiting_claim["order"], waiting_claim["event_id"], waiting_claim["session_id"]

    meta = _order_detail(client, admin_claim_headers, order.id)
    preview = _preview(client, admin_claim_headers, order.id, 90000)
    applied = _apply(client, admin_claim_headers, order.id, 90000)
    adjusted = _adjust(client, admin_claim_headers, event_id, 90000)

    assert (meta["is_collected_cash_editable"], meta["collected_cash_edit_min_amount"]) == (True, 100000.0)
    assert meta["collected_cash_edit_block_code"] is None
    assert (preview["is_editable"], preview["blocking_reasons"]) == (
        False,
        ["event_funds_driver_expense: " + _why(sid, "90000.00", "100000.00")],
    )
    assert (applied.status_code, _error_code(applied), applied.get_json()["errors"]) == (
        400,
        CODE,
        ["event_funds_driver_expense: " + _why(sid, "90000.00", "100000.00")],
    )
    assert (adjusted.status_code, _error_code(adjusted), adjusted.get_json()["errors"]) == (
        400,
        CODE,
        [f"Collection {event_id}: " + _why(sid, "90000.00", "100000.00")],
    )
    assert _event(db, event_id).voided_at is None
    assert _order_events(db, order.id) == [(event_id, 100000.0, False)]
    detail = session_detail(client, admin_claim_headers, sid)
    assert (detail["expected_cash_on_hand"], detail["remaining_cash_to_submit"]) == (100000.0, 0.0)


def test_the_published_minimum_is_the_lowest_amount_the_correction_accepts(
    client, db, admin_claim_headers, waiting_claim
):
    """The order detail's minimum and the routes agree at the boundary: 99,999 is refused, 100,000 goes through."""
    order, sid = waiting_claim["order"], waiting_claim["session_id"]
    assert _order_detail(client, admin_claim_headers, order.id)["collected_cash_edit_min_amount"] == 100000.0

    below = _preview(client, admin_claim_headers, order.id, 99999)
    at = _apply(client, admin_claim_headers, order.id, 100000)

    assert below["blocking_reasons"] == ["event_funds_driver_expense: " + _why(sid, "99999.00", "100000.00")]
    assert at.status_code == 200, at.get_json()


@pytest.mark.parametrize("route", ["apply", "adjust"])
def test_a_correction_upwards_is_always_allowed(client, db, admin_claim_headers, waiting_claim, route):
    sid = waiting_claim["session_id"]

    r = _correct(route, client, admin_claim_headers, waiting_claim, 110000)

    assert r.status_code == 200, r.get_json()
    detail = session_detail(client, admin_claim_headers, sid)
    assert (detail["expected_cash_on_hand"], detail["expenses_waiting_total"], detail["remaining_cash_to_submit"]) == (
        110000.0,
        30000.0,
        10000.0,
    )


def _reject(client, app, headers, driver, expense_id):
    status, body = reject(client, headers, expense_id, "Not a business expense")
    assert status == 200, body


def _withdraw(client, app, headers, driver, expense_id):
    status, body = withdraw(client, app, driver, expense_id)
    assert status == 200, body


def _approve(client, app, headers, driver, expense_id):
    status, body = approve(client, headers, expense_id, 30000)
    assert status == 200, body


@pytest.mark.parametrize(
    "decide, on_hand_after",
    [(_reject, 90000.0), (_withdraw, 90000.0), (_approve, 60000.0)],
    ids=["rejected", "withdrawn", "approved"],
)
def test_deciding_the_claim_lifts_the_floor(
    app, client, db, admin_claim_headers, second_delivery_driver, waiting_claim, decide, on_hand_after
):
    order, sid = waiting_claim["order"], waiting_claim["session_id"]
    decide(client, app, admin_claim_headers, second_delivery_driver, waiting_claim["expense_id"])
    assert _order_detail(client, admin_claim_headers, order.id)["collected_cash_edit_min_amount"] is None

    r = _apply(client, admin_claim_headers, order.id, 90000)

    assert r.status_code == 200, r.get_json()
    assert session_detail(client, admin_claim_headers, sid)["expected_cash_on_hand"] == on_hand_after


@pytest.mark.parametrize("route", ["apply", "adjust"])
def test_without_expenses_nothing_changes(app, client, db, admin_claim_headers, second_delivery_driver, trip, route):
    assert hand_in(client, app, second_delivery_driver, 70000)["status"] == "partial"
    assert _order_detail(client, admin_claim_headers, trip["order"].id)["collected_cash_edit_min_amount"] is None

    r = _correct(route, client, admin_claim_headers, trip, 90000)

    assert r.status_code == 200, r.get_json()
    assert session_detail(client, admin_claim_headers, trip["session_id"])["expected_cash_on_hand"] == 90000.0


def test_an_order_without_door_cash_publishes_no_minimum(client, admin_claim_headers, sample_order):
    meta = _order_detail(client, admin_claim_headers, sample_order.id)

    assert (meta["is_collected_cash_editable"], meta["collected_cash_edit_min_amount"]) == (False, None)


# --------------------------------------------------------------------------- where the event adjust refuses


def test_the_event_adjust_refuses_before_it_writes_anything(client, db, admin_claim_headers, waiting_claim, audits):
    """The unlocked pre-check refuses every correction no claim races: no reversal is audited and nothing is
    flushed, so no audit row (written through the logger's own committed session) can outlive the refusal."""
    flushed = []

    def _after_flush(session, flush_context):
        flushed.extend(type(row).__name__ for row in [*session.new, *session.dirty, *session.deleted])

    audits.clear()
    sa_event.listen(Session, "after_flush", _after_flush)
    try:
        r = _adjust(client, admin_claim_headers, waiting_claim["event_id"], 90000)
    finally:
        sa_event.remove(Session, "after_flush", _after_flush)

    assert (r.status_code, _error_code(r)) == (400, CODE)
    assert (audits, flushed) == ([], [])


@pytest.fixture
def a_claim_lands_after_the_pre_check(monkeypatch, db, second_delivery_driver):
    """The race the check under the session lock exists for: the driver's 30,000 claim commits after the
    correction's unlocked pre-check passed and before its reversal takes the trip's session lock. Records the claim's
    id; the reversal itself runs for real.

    The claim's commit shares the request's session, which by then has only read and locked, so it commits nothing
    of the correction."""
    real_reverse = CashCollectionService.reverse_collection_event
    landed = []

    def _claim_then_reverse(self, event_id, **kwargs):
        if not landed:
            session_id = db.session.get(CashCollectionEvent, event_id).driver_cash_session_id
            landed.append(insert_expense(db, session_id, second_delivery_driver.id, 30000))
        return real_reverse(self, event_id, **kwargs)

    monkeypatch.setattr(CashCollectionService, "reverse_collection_event", _claim_then_reverse)
    return landed


@pytest.mark.parametrize("route", ["apply", "adjust"])
def test_a_claim_that_lands_after_the_pre_check_is_refused_under_the_session_lock(
    app, client, db, admin_claim_headers, second_delivery_driver, trip, audits, a_claim_lands_after_the_pre_check, route
):
    # `audits` records the entries instead of writing them: on the in-memory SQLite engine the audit logger's own
    # session shares the request's one connection, so a real write would commit the correction before its rollback.
    order, event_id, sid = trip["order"], trip["event_id"], trip["session_id"]
    assert hand_in(client, app, second_delivery_driver, 70000)["status"] == "partial"
    audits.clear()

    r = _correct(route, client, admin_claim_headers, trip, 90000)

    assert (r.status_code, _error_code(r), r.get_json()["errors"]) == (
        400,
        CODE,
        [f"Collection {event_id}: " + _why(sid, "90000.00", "100000.00")],
    )
    # The reversal and the replacement rolled back; the claim, committed before them, still waits.
    (expense_id,) = a_claim_lands_after_the_pre_check
    assert _order_events(db, order.id) == [(event_id, 100000.0, False)]
    assert db.session.get(DriverCashExpense, expense_id).status == DriverCashExpense.PENDING
    detail = session_detail(client, admin_claim_headers, sid)
    assert (detail["expected_cash_on_hand"], detail["expenses_waiting_total"]) == (100000.0, 30000.0)
    # The reversal's and the replacement's own-session audit rows outlive the rollback; one entry says they were
    # undone, and nothing claims the correction happened.
    assert [call["action"] for call in audits] == [
        "cash_collection_reversed",
        "cash_collection_posted",
        "cash_collection_reversal_refused",
    ]
    replacement_id = int(audits[1]["resource_id"])
    assert replacement_id != event_id
    assert audits[2] == {
        "event_type": AuditEventType.PAYMENT_REFUNDED,
        "action": "cash_collection_reversal_refused",
        "severity": AuditSeverity.HIGH,
        "resource_type": "cash_collection_event",
        "resource_id": str(event_id),
        "description": None,
        "old_values": None,
        "new_values": None,
        "success": False,
        "error_message": "EVENT_FUNDS_DRIVER_EXPENSE: the reversal would take back cash an approved expense paid out",
        "duration_ms": None,
        "additional_data": {
            "driver_cash_session_id": sid,
            "error_code": CODE,
            "replacement_event_id": replacement_id,
        },
    }
