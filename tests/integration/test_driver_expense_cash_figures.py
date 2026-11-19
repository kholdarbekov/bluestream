"""The cash engine counts a driver's expense claims (spec 2026-11-17 §6.1).

An approved claim comes off what the driver holds; a waiting one only off "Hand in now", which every session
reader publishes as `remaining_cash_to_submit`. Expected figures are the spec's worked example (the driver
collected 420,000 so'm). Claim rows are inserted as setup (`insert_expense`) until the claim and decision
routes exist; every figure is read back through the route the staff bot or the admin screens call.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event as sa_event

from business_app.services.driver_reconciliation_service import DriverReconciliationService
from tests.integration._driver_expense_helpers import insert_expense
from tests.integration._prepayment_payout_helpers import (
    BASE,
    collect_on_trip,
    driver_headers,
    hand_in,
    session_detail,
)

pytestmark = pytest.mark.integration

DRIVER_SESSION = "/api/v1/staff/reconciliation/session"
PUBLISHED = ("remaining_cash_to_submit", "expenses_waiting_total", "expenses_approved_total", "declared_variance_net")


def _driver_session(client, app, driver):
    """The staff bot's Cash screen read."""
    response = client.get(DRIVER_SESSION, headers=driver_headers(app, driver))
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def _utc(value):
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- the money rule


def test_a_waiting_claim_leaves_on_hand_and_lowers_hand_in_now(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    insert_expense(db, sid, driver.id, 150000)

    s = _driver_session(client, app, driver)

    assert s["id"] == sid
    assert (s["expected_cash_on_hand"], s["remaining_cash_to_submit"]) == (420000.0, 270000.0)
    assert (s["expenses_waiting_total"], s["expenses_approved_total"]) == (150000.0, 0.0)
    assert s["next_actions"] == ["submit_reconciliation"]


def test_an_approved_claim_lowers_on_hand(app, client, db, admin_claim_headers, sample_user, second_delivery_driver):
    driver = second_delivery_driver
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    insert_expense(db, sid, driver.id, 150000, status="approved", approved_amount=120000)

    s = _driver_session(client, app, driver)

    assert s["id"] == sid
    assert (s["expected_cash_on_hand"], s["remaining_cash_to_submit"], s["expenses_approved_total"]) == (
        300000.0,
        300000.0,
        120000.0,
    )
    # What he collected stays what it was, and an approved claim no longer waits.
    assert (s["expected_cash"], s["expenses_waiting_total"]) == (420000.0, 0.0)
    assert s["next_actions"] == ["submit_reconciliation"]


def test_after_a_net_hand_in_the_variance_shown_is_zero_and_the_bot_waits(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    insert_expense(db, sid, driver.id, 150000)

    s = hand_in(client, app, driver, 270000)

    assert (s["id"], s["next_active_session"]) == (sid, None)
    assert (s["status"], s["declared_variance"], s["declared_variance_net"]) == ("partial", -150000.0, 0.0)
    assert (s["remaining_cash_to_submit"], s["next_actions"]) == (0.0, ["await_expense_decision"])
    assert (s["expected_cash_on_hand"], s["expenses_waiting_total"]) == (420000.0, 150000.0)


def test_with_a_claim_and_no_handoff_the_net_variance_is_zero(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    insert_expense(db, sid, driver.id, 150000)

    s = _driver_session(client, app, driver)

    assert s["id"] == sid
    assert (s["declared_cash"], s["declared_variance"], s["declared_variance_net"]) == (None, 0.0, 0.0)


def test_a_waiting_claim_does_not_change_the_risk_flags_and_an_approved_one_lowers_their_basis(
    app, client, db, admin_claim_headers, sample_user, delivery_driver, second_delivery_driver, monkeypatch
):
    monkeypatch.setitem(app.config, "COD_CASH_WARNING_THRESHOLD_UZS", 400000)
    monkeypatch.setitem(app.config, "COD_CASH_ESCALATION_THRESHOLD_UZS", 10000000)
    waiting_trip = collect_on_trip(client, admin_claim_headers, sample_user.id, second_delivery_driver.id, 420000)
    approved_trip = collect_on_trip(client, admin_claim_headers, sample_user.id, delivery_driver.id, 420000)
    for trip in (waiting_trip, approved_trip):
        assert session_detail(client, admin_claim_headers, trip)["risk_flags"] == ["cash_on_hand_warning"]

    insert_expense(db, waiting_trip, second_delivery_driver.id, 150000)
    insert_expense(db, approved_trip, delivery_driver.id, 150000, status="approved")

    waiting = session_detail(client, admin_claim_headers, waiting_trip)
    approved = session_detail(client, admin_claim_headers, approved_trip)
    assert (waiting["expected_cash_on_hand"], waiting["risk_flags"]) == (420000.0, ["cash_on_hand_warning"])
    assert (approved["expected_cash_on_hand"], approved["risk_flags"]) == (270000.0, [])


# --------------------------------------------------------------------------- list and detail agree


def test_the_session_list_and_detail_publish_the_same_figures_with_one_expense_query(
    app, client, db, admin_claim_headers, sample_user, delivery_driver, second_delivery_driver, monkeypatch
):
    """Review Focus 5: the cash report's session rows batch the claim totals in ONE grouped read and publish
    the figures the session detail does. Scoped to `list_sessions`, the rows' own read."""
    # Trip A: claims, then a handoff; trip B: claims, then a later collection. Each write refreshes the
    # trip's stored figures, which the list reads.
    trip_a = collect_on_trip(client, admin_claim_headers, sample_user.id, second_delivery_driver.id, 420000)
    insert_expense(db, trip_a, second_delivery_driver.id, 150000)
    insert_expense(db, trip_a, second_delivery_driver.id, 150000, status="approved", approved_amount=120000)
    hand_in(client, app, second_delivery_driver, 100000)
    trip_b = collect_on_trip(client, admin_claim_headers, sample_user.id, delivery_driver.id, 200000)
    insert_expense(db, trip_b, delivery_driver.id, 50000)
    insert_expense(db, trip_b, delivery_driver.id, 30000, status="approved")
    assert collect_on_trip(client, admin_claim_headers, sample_user.id, delivery_driver.id, 10000) == trip_b

    expense_reads = []
    scope = []
    original = DriverReconciliationService.list_sessions

    def scoped_list_sessions(self, *args, **kwargs):
        scope.append("list_sessions")
        try:
            return original(self, *args, **kwargs)
        finally:
            scope.pop()

    def record_expense_reads(conn, cursor, statement, parameters, context, executemany):
        if scope and "driver_cash_expenses" in statement.lower():
            expense_reads.append(statement)

    monkeypatch.setattr(DriverReconciliationService, "list_sessions", scoped_list_sessions)
    sa_event.listen(db.engine, "before_cursor_execute", record_expense_reads)
    try:
        response = client.get(BASE, headers=admin_claim_headers)
    finally:
        sa_event.remove(db.engine, "before_cursor_execute", record_expense_reads)

    assert response.status_code == 200, response.get_json()
    assert len(expense_reads) == 1, expense_reads
    rows = {row["id"]: row for row in response.get_json()["data"]["sessions"]}
    expected = {
        trip_a: {
            "remaining_cash_to_submit": 50000.0,
            "expenses_waiting_total": 150000.0,
            "expenses_approved_total": 120000.0,
            "declared_variance_net": -50000.0,
        },
        trip_b: {
            "remaining_cash_to_submit": 130000.0,
            "expenses_waiting_total": 50000.0,
            "expenses_approved_total": 30000.0,
            "declared_variance_net": 0.0,
        },
    }
    for trip, figures in expected.items():
        detail = session_detail(client, admin_claim_headers, trip)
        assert {key: rows[trip][key] for key in PUBLISHED} == figures, trip
        assert {key: detail[key] for key in PUBLISHED} == figures, trip


# --------------------------------------------------------------------------- the trip's claims


def test_the_session_detail_lists_the_trips_claims(
    app, client, db, admin_claim_headers, sample_user, delivery_driver, second_delivery_driver
):
    driver = second_delivery_driver
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    other_trip = collect_on_trip(client, admin_claim_headers, sample_user.id, delivery_driver.id, 100000)
    t0 = (datetime.now(UTC) - timedelta(hours=4)).replace(microsecond=0)
    # Inserted out of time order, so "newest first" cannot be the id order.
    approved = insert_expense(
        db,
        sid,
        driver.id,
        60000,
        status="approved",
        approved_amount=50000,
        category="caps",
        submitted_at=t0 + timedelta(hours=1),
    )
    waiting = insert_expense(db, sid, driver.id, 150000, submitted_at=t0)
    withdrawn = insert_expense(
        db,
        sid,
        driver.id,
        20000,
        status="withdrawn",
        category="other",
        note="Tape for the crates",
        submitted_at=t0 + timedelta(hours=3),
    )
    rejected = insert_expense(
        db, sid, driver.id, 30000, status="rejected", category="parking_wash", submitted_at=t0 + timedelta(hours=2)
    )
    insert_expense(db, other_trip, delivery_driver.id, 10000)

    detail = session_detail(client, admin_claim_headers, sid)

    assert [{**claim, "submitted_at": _utc(claim["submitted_at"])} for claim in detail["expenses"]] == [
        {
            "id": withdrawn,
            "category": "other",
            "amount": 20000.0,
            "approved_amount": None,
            "status": "withdrawn",
            "submitted_at": t0 + timedelta(hours=3),
        },
        {
            "id": rejected,
            "category": "parking_wash",
            "amount": 30000.0,
            "approved_amount": None,
            "status": "rejected",
            "submitted_at": t0 + timedelta(hours=2),
        },
        {
            "id": approved,
            "category": "caps",
            "amount": 60000.0,
            "approved_amount": 50000.0,
            "status": "approved",
            "submitted_at": t0 + timedelta(hours=1),
        },
        {
            "id": waiting,
            "category": "fuel",
            "amount": 150000.0,
            "approved_amount": None,
            "status": "pending",
            "submitted_at": t0,
        },
    ]
