"""Return prepayment "from his current trip's cash": the driver session effect (spec 2026-11-10 §4.3, Task 4).

A driver hands a leaving customer's prepayment back out of the cash he is carrying. The admin records
it as `paid_by = driver_current_session`, and the driver's open cash session must stop asking for
that money: what he holds (`expected_cash_on_hand`) drops by the return, while what he collected
(`expected_cash`, gross) stays as it was. Every variance is measured against what he holds. A return
that leaves nothing to hand in closes the session by the same rule a handoff uses.

Every test drives the routes the admin modal and the staff bot call, with the bodies they send:
collections land through the admin "Record collection" route with the driver as collector, the driver
hands cash in through `POST /api/v1/staff/reconciliation/session/submit`, and the session is read back
through the admin session-detail route.
"""

import inspect
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from business_app.models.delivery import DeliveryPerson
from business_app.models.payment import CustomerCreditPayout, DriverCashSession
from business_app.services.driver_reconciliation_service import DriverReconciliationService
from business_app.utils.audit_logger import AuditEventType, AuditLogger, AuditSeverity, audit_logger
from shared.enums import DriverCashSessionStatus
from tests.integration._prepayment_payout_helpers import (
    SESSION,
    apply_payout,
    collect_on_trip,
    credit,
    session_detail,
    from_trip,
    hand_in,
    preview_payout,
    stored,
)

pytestmark = pytest.mark.integration

SESSION_EFFECT_KEYS = {"session_id", "status", "on_hand_before", "handed_in", "cap", "on_hand_after", "will_close"}


# --------------------------------------------------------------------------- helpers


def _figures(payload):
    return {
        key: payload[key]
        for key in (
            "status",
            "expected_cash",
            "gross_cash_collected",
            "expected_cash_on_hand",
            "declared_cash",
            "declared_variance",
            "remaining_cash_to_submit",
        )
    }


# --------------------------------------------------------------------------- what the return changes


def test_current_trip_return_lowers_on_hand_not_gross(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    session_id = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 100000)
    # More credit, paid at the office: the customer's balance and the driver's cash must not be confused.
    credit(client, admin_claim_headers, sample_user.id, 15000)

    status, body = preview_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=40000))

    assert status == 200, body
    assert body["data"]["session_effect"] == {
        "session_id": session_id,
        "status": "open",
        "on_hand_before": 100000.0,
        "handed_in": 0.0,
        "cap": 100000.0,
        "expenses_waiting": 0.0,
        "on_hand_after": 60000.0,
        "will_close": False,
    }
    assert body["data"]["blocking_reasons"] == []
    # The preview wrote nothing to the session.
    assert Decimal(str(stored(db, session_id).expected_cash_on_hand)) == Decimal("100000.00")
    assert CustomerCreditPayout.query.count() == 0

    status, body = apply_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=40000))

    assert status == 201, body
    data = body["data"]
    assert (data["payout"]["paid_by"], data["payout"]["driver_user_id"], data["payout"]["driver_cash_session_id"]) == (
        "driver_current_session",
        driver.id,
        session_id,
    )
    assert data["available_prepayment_balance"] == 75000.0
    assert data["next_session_id"] is None
    assert data["driver_cash_session"]["id"] == session_id
    assert _figures(data["driver_cash_session"]) == {
        "status": "open",
        "expected_cash": 100000.0,
        "gross_cash_collected": 100000.0,
        "expected_cash_on_hand": 60000.0,
        "declared_cash": None,
        "declared_variance": 0.0,
        "remaining_cash_to_submit": 60000.0,
    }
    row = CustomerCreditPayout.query.one()
    assert (row.paid_by, row.driver_user_id, row.driver_cash_session_id) == (
        "driver_current_session",
        driver.id,
        session_id,
    )

    detail = session_detail(client, admin_claim_headers, session_id)

    assert _figures(detail) == {
        "status": "open",
        "expected_cash": 100000.0,
        "gross_cash_collected": 100000.0,
        "expected_cash_on_hand": 60000.0,
        "declared_cash": None,
        "declared_variance": 0.0,
        "remaining_cash_to_submit": 60000.0,
    }


def test_variances_measure_on_hand(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    session_id = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 100000)
    handed = hand_in(client, app, driver, 30000)
    assert handed["status"] == "partial"

    status, body = apply_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=40000))

    assert status == 201, body
    detail = session_detail(client, admin_claim_headers, session_id)
    assert _figures(detail) == {
        "status": "partial",
        "expected_cash": 100000.0,
        "gross_cash_collected": 100000.0,
        "expected_cash_on_hand": 60000.0,
        "declared_cash": 30000.0,
        "declared_variance": -30000.0,
        "remaining_cash_to_submit": 30000.0,
    }

    response = client.post(
        SESSION.format(session_id) + "/verify",
        json={"verified_cash": 60000, "reason_code": "cash_count_matched"},
        headers=admin_claim_headers,
    )

    assert response.status_code == 200, response.get_json()
    verified = response.get_json()["data"]
    assert (verified["status"], verified["verified_cash"], verified["verified_variance"]) == ("verified", 60000.0, 0.0)
    # The next read refreshes the session again: it must keep measuring against what the driver holds.
    again = session_detail(client, admin_claim_headers, session_id)
    assert (again["status"], again["verified_variance"], again["declared_variance"]) == ("verified", 0.0, -30000.0)
    assert Decimal(str(stored(db, session_id).verified_variance)) == Decimal("0.00")


def test_return_to_zero_closes_the_session(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    session_id = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 108000)

    _, preview = preview_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=108000))
    status, body = apply_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=108000))

    assert preview["data"]["session_effect"] == {
        "session_id": session_id,
        "status": "open",
        "on_hand_before": 108000.0,
        "handed_in": 0.0,
        "cap": 108000.0,
        "expenses_waiting": 0.0,
        "on_hand_after": 0.0,
        "will_close": True,
    }
    assert status == 201, body
    session = stored(db, session_id)
    assert session.status == DriverCashSessionStatus.SUBMITTED
    assert session.submitted_by_user_id == admin_user.id
    assert session.declared_cash is None
    assert session.submitted_at is not None and session.session_ended_at is not None
    assert (Decimal(str(session.expected_cash)), Decimal(str(session.expected_cash_on_hand))) == (
        Decimal("108000.00"),
        Decimal("0.00"),
    )
    next_open = DriverCashSession.query.filter(
        DriverCashSession.driver_user_id == driver.id, DriverCashSession.status == DriverCashSessionStatus.OPEN
    ).one()
    assert next_open.id != session_id
    assert Decimal(str(next_open.expected_cash_on_hand)) == Decimal("0.00")
    data = body["data"]
    assert data["next_session_id"] == next_open.id
    assert _figures(data["driver_cash_session"]) == {
        "status": "submitted",
        "expected_cash": 108000.0,
        "gross_cash_collected": 108000.0,
        "expected_cash_on_hand": 0.0,
        "declared_cash": None,
        "declared_variance": 0.0,
        "remaining_cash_to_submit": 0.0,
    }


def test_return_meeting_handed_in_closes_under_old_rule(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    session_id = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 100000)
    hand_in(client, app, driver, 60000)

    status, body = apply_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=40000))

    assert status == 201, body
    session = stored(db, session_id)
    assert session.status == DriverCashSessionStatus.SUBMITTED
    assert session.submitted_by_user_id == admin_user.id
    assert (Decimal(str(session.declared_cash)), Decimal(str(session.declared_variance))) == (
        Decimal("60000.00"),
        Decimal("0.00"),
    )
    assert _figures(body["data"]["driver_cash_session"]) == {
        "status": "submitted",
        "expected_cash": 100000.0,
        "gross_cash_collected": 100000.0,
        "expected_cash_on_hand": 60000.0,
        "declared_cash": 60000.0,
        "declared_variance": 0.0,
        "remaining_cash_to_submit": 0.0,
    }
    next_open = DriverCashSession.query.filter(
        DriverCashSession.driver_user_id == driver.id, DriverCashSession.status == DriverCashSessionStatus.OPEN
    ).one()
    assert body["data"]["next_session_id"] == next_open.id


def test_a_return_leaving_cash_to_hand_in_keeps_a_partial_session_partial(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    session_id = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 100000)
    hand_in(client, app, driver, 30000)

    status, body = apply_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=25000))

    assert status == 201, body
    assert body["data"]["next_session_id"] is None
    assert (
        body["data"]["driver_cash_session"]["status"],
        body["data"]["driver_cash_session"]["remaining_cash_to_submit"],
    ) == (
        "partial",
        45000.0,
    )
    # The driver's next tap settles exactly what is left, and that closes the trip with no variance.
    closed = hand_in(client, app, driver, 45000)
    assert (closed["status"], closed["declared_variance"]) == ("submitted", 0.0)
    assert stored(db, session_id).status == DriverCashSessionStatus.SUBMITTED


# --------------------------------------------------------------------------- what blocks it


def test_cap_is_on_hand_minus_handed_in(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    session_id = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 100000)
    hand_in(client, app, driver, 30000)

    _, preview = preview_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=70001))
    status, body = apply_payout(
        client, admin_claim_headers, sample_user.id, key="trip-over", **from_trip(driver, amount=70001)
    )

    assert preview["data"]["session_effect"]["cap"] == 70000.0
    assert len(preview["data"]["blocking_reasons"]) == 1
    assert preview["data"]["blocking_reasons"][0].startswith("amount_exceeds_driver_cash: ")
    assert preview["data"]["is_applicable"] is False
    assert (status, body["data"]["error_code"]) == (400, "PAYOUT_BLOCKED")
    assert body["errors"][0].startswith("amount_exceeds_driver_cash: ")
    assert CustomerCreditPayout.query.count() == 0

    status, body = apply_payout(
        client, admin_claim_headers, sample_user.id, key="trip-cap", **from_trip(driver, amount=70000)
    )

    assert status == 201, body
    session = stored(db, session_id)
    assert (Decimal(str(session.expected_cash_on_hand)), session.status) == (
        Decimal("30000.00"),
        DriverCashSessionStatus.SUBMITTED,
    )


def test_no_active_session_blocks_and_creates_nothing(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    for status_value in (DriverCashSessionStatus.VERIFIED, DriverCashSessionStatus.RESOLVED):
        db.session.add(
            DriverCashSession(
                driver_user_id=driver.id,
                status=status_value,
                session_started_at=datetime(2026, 11, 1, 8, 0, tzinfo=UTC),
            )
        )
    db.session.commit()
    credit(client, admin_claim_headers, sample_user.id, 50000)
    sessions_before = DriverCashSession.query.count()

    _, preview = preview_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=20000))
    status, body = apply_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=20000))

    assert preview["data"]["session_effect"] is None
    assert len(preview["data"]["blocking_reasons"]) == 1
    assert preview["data"]["blocking_reasons"][0].startswith("no_active_session: ")
    assert (status, body["data"]["error_code"]) == (400, "PAYOUT_BLOCKED")
    assert body["errors"][0].startswith("no_active_session: ")
    assert DriverCashSession.query.count() == sessions_before
    assert CustomerCreditPayout.query.count() == 0


def test_paid_before_trip_start_blocks(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    session_id = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 100000)
    session = stored(db, session_id)
    session.session_started_at = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)
    db.session.commit()

    _, preview = preview_payout(
        client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=20000, paid_at="2026-11-09T07:59:00Z")
    )
    status, body = apply_payout(
        client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=20000, paid_at="2026-11-09T07:59:00Z")
    )

    assert len(preview["data"]["blocking_reasons"]) == 1
    assert preview["data"]["blocking_reasons"][0].startswith("paid_before_current_trip: ")
    assert (status, body["data"]["error_code"]) == (400, "PAYOUT_BLOCKED")
    assert body["errors"][0].startswith("paid_before_current_trip: ")
    assert CustomerCreditPayout.query.count() == 0

    # Paid the moment the trip started is this trip's cash.
    _, at_start = preview_payout(
        client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=20000, paid_at="2026-11-09T08:00:00Z")
    )
    assert at_start["data"]["blocking_reasons"] == []


def test_an_inactive_driver_is_blocked_before_his_session_is_read(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 100000)
    DeliveryPerson.query.filter_by(user_id=driver.id).one().is_active = False
    db.session.commit()

    _, preview = preview_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=20000))
    status, body = apply_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=20000))

    assert preview["data"]["session_effect"] is None
    assert len(preview["data"]["blocking_reasons"]) == 1
    assert preview["data"]["blocking_reasons"][0].startswith("driver_not_eligible: ")
    assert (status, body["data"]["error_code"]) == (400, "PAYOUT_BLOCKED")
    assert CustomerCreditPayout.query.count() == 0


def test_current_trip_needs_a_driver(app, client, db, admin_user, admin_claim_headers, sample_user):
    credit(client, admin_claim_headers, sample_user.id, 50000)

    status, body = apply_payout(client, admin_claim_headers, sample_user.id, paid_by="driver_current_session", amount=20000)

    assert (status, body["data"]["error_code"]) == (400, "PAYOUT_DRIVER_REQUIRED")


def test_a_closing_return_audits_the_close_after_commit(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver, monkeypatch
):
    driver = second_delivery_driver
    session_id = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 100000)
    hand_in(client, app, driver, 60000)
    signature = inspect.signature(AuditLogger.log_event)
    calls = []
    seen = {"committed_since_payout_flush": False}

    def _record(*args, **kwargs):
        bound = signature.bind(audit_logger, *args, **kwargs)  # TypeError on a call the real method refuses
        bound.apply_defaults()
        arguments = dict(bound.arguments)
        del arguments["self"]
        arguments["committed_before_this_call"] = seen["committed_since_payout_flush"]
        calls.append(arguments)
        return "recorded-audit-event"

    def _after_flush(session, flush_context):
        if any(isinstance(obj, CustomerCreditPayout) for obj in session.new):
            seen["committed_since_payout_flush"] = False

    def _after_commit(session):
        seen["committed_since_payout_flush"] = True

    monkeypatch.setattr(audit_logger, "log_event", _record)
    sa_event.listen(Session, "after_flush", _after_flush)
    sa_event.listen(Session, "after_commit", _after_commit)
    try:
        # 10 000 leaves 30 000 to hand in: the trip stays open, nothing to audit for the session.
        stays_open, _ = apply_payout(
            client, admin_claim_headers, sample_user.id, key="trip-a", **from_trip(driver, amount=10000)
        )
        # 30 000 more meets the 60 000 handed in: the trip closes.
        status, body = apply_payout(
            client, admin_claim_headers, sample_user.id, key="trip-b", **from_trip(driver, amount=30000)
        )
    finally:
        sa_event.remove(Session, "after_flush", _after_flush)
        sa_event.remove(Session, "after_commit", _after_commit)

    assert (stays_open, status) == (201, 201), body
    next_session_id = body["data"]["next_session_id"]
    assert next_session_id is not None
    close_calls = [c for c in calls if c["action"] == "driver_cash_session_closed_by_payout"]
    assert close_calls == [
        {
            "event_type": AuditEventType.PAYMENT_PROCESSED,
            "action": "driver_cash_session_closed_by_payout",
            "severity": AuditSeverity.MEDIUM,
            "resource_type": "driver_cash_session",
            "resource_id": str(session_id),
            "description": None,
            "old_values": None,
            "new_values": None,
            "success": True,
            "error_message": None,
            "duration_ms": None,
            "additional_data": {
                "driver_user_id": driver.id,
                "payout_id": body["data"]["payout"]["id"],
                "actor_user_id": admin_user.id,
                "expected_cash": "100000.00",
                "expected_cash_on_hand": "60000.00",
                "declared_cash": "60000.00",
                "declared_variance": "0.00",
                "status": "submitted",
                "next_driver_cash_session_id": next_session_id,
            },
            "committed_before_this_call": True,
        }
    ]
    assert [c["action"] for c in calls] == [
        "customer_credit_payout_recorded",
        "customer_credit_payout_recorded",
        "driver_cash_session_closed_by_payout",
    ]


# --------------------------------------------------------------------------- what does not count


def test_session_without_payouts_refreshes_as_before(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    session_id = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 100000)
    # Returns that did not come out of this trip's cash: the office paid one, the driver another, already settled.
    assert apply_payout(client, admin_claim_headers, sample_user.id, key="office", paid_by="office", amount=10000)[0] == 201
    assert (
        apply_payout(
            client,
            admin_claim_headers,
            sample_user.id,
            key="settled",
            paid_by="driver_settled",
            driver_user_id=driver.id,
            amount=20000,
        )[0]
        == 201
    )

    detail = session_detail(client, admin_claim_headers, session_id)

    assert _figures(detail) == {
        "status": "open",
        "expected_cash": 100000.0,
        "gross_cash_collected": 100000.0,
        "expected_cash_on_hand": 100000.0,
        "declared_cash": None,
        "declared_variance": 0.0,
        "remaining_cash_to_submit": 100000.0,
    }
    handed = hand_in(client, app, driver, 100000)
    assert (handed["status"], handed["declared_variance"]) == ("submitted", 0.0)


def test_an_undone_current_trip_return_stops_counting(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    session_id = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 100000)
    assert apply_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=40000))[0] == 201
    payout = CustomerCreditPayout.query.one()
    payout.voided_at = datetime.now(UTC)
    payout.voided_by_user_id = admin_user.id
    payout.void_reason = "Recorded by mistake"
    db.session.commit()

    detail = session_detail(client, admin_claim_headers, session_id)

    assert (detail["expected_cash_on_hand"], detail["remaining_cash_to_submit"]) == (100000.0, 100000.0)


# --------------------------------------------------------------------------- the session writers lock the row


def _put_in_mismatch(client, headers, session_id):
    response = client.post(
        SESSION.format(session_id) + "/verify",
        json={"verified_cash": 90000, "reason_code": "cash_count_short", "notes": "10 000 short at the count"},
        headers=headers,
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["status"] == "mismatch"


SESSION_WRITERS = {
    "verify": ({"verified_cash": 100000, "reason_code": "cash_count_matched"}, "verified"),
    "resolve": (
        {
            "reason_code": "cash_recovered_later",
            "resolution_notes": "The driver brought the rest",
            "verified_cash": 100000,
        },
        "resolved",
    ),
    "force-close": ({"reason": "Driver left the company", "verified_cash": 100000}, "force_closed"),
}


@pytest.mark.parametrize("route", sorted(SESSION_WRITERS))
def test_session_writers_take_the_session_lock_before_writing(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver, monkeypatch, route
):
    """verify, resolve and force-close can act on an ACTIVE session a current-trip return also locks, so each
    takes the same row lock (`lock_session`) once, by the session id, before it writes the row."""
    driver = second_delivery_driver
    session_id = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 100000)
    if route == "resolve":
        _put_in_mismatch(client, admin_claim_headers, session_id)
    body, final_status = SESSION_WRITERS[route]
    log = []
    original = DriverReconciliationService.lock_session

    def recording_lock(self, session_id):
        log.append(("lock", session_id))
        return original(self, session_id)

    def record_session_writes(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE DRIVER_CASH_SESSIONS"):
            log.append(("write",))

    monkeypatch.setattr(DriverReconciliationService, "lock_session", recording_lock)
    sa_event.listen(db.engine, "before_cursor_execute", record_session_writes)
    try:
        response = client.post(SESSION.format(session_id) + f"/{route}", json=body, headers=admin_claim_headers)
    finally:
        sa_event.remove(db.engine, "before_cursor_execute", record_session_writes)

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["status"] == final_status
    assert [entry for entry in log if entry[0] == "lock"] == [("lock", session_id)]
    assert log[0] == ("lock", session_id), log
    assert ("write",) in log


def test_a_handoff_and_a_return_each_ask_the_close_rule_with_one_payout_sum_per_refresh(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver, monkeypatch
):
    """`submit_session` and `apply_payout_effect` decide "does this session close now?" through one helper that
    refreshes the figures and reads the live-payout fact off them: the payout SUM runs once per refresh, never
    once more for the close rule."""
    driver = second_delivery_driver
    collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 100000)
    log = []  # (writer in progress, "sum" | "refresh")
    scope = []

    def record_payout_sums(conn, cursor, statement, parameters, context, executemany):
        if scope and "sum(customer_credit_payouts.amount)" in statement.lower():
            log.append((scope[0], "sum"))

    def watching(name):
        original = getattr(DriverReconciliationService, name)

        def wrapper(self, *args, **kwargs):
            scope.append(name)
            try:
                return original(self, *args, **kwargs)
            finally:
                scope.pop()

        monkeypatch.setattr(DriverReconciliationService, name, wrapper)

    watching("apply_payout_effect")
    watching("submit_session")
    refresh = DriverReconciliationService.refresh_expected_cash

    def counting_refresh(self, session):
        if scope:
            log.append((scope[0], "refresh"))
        return refresh(self, session)

    monkeypatch.setattr(DriverReconciliationService, "refresh_expected_cash", counting_refresh)
    sa_event.listen(db.engine, "before_cursor_execute", record_payout_sums)
    try:
        status, body = apply_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=40000))
        assert status == 201, body
        hand_in(client, app, driver, 60000)
    finally:
        sa_event.remove(db.engine, "before_cursor_execute", record_payout_sums)

    for writer in ("apply_payout_effect", "submit_session"):
        refreshes = log.count((writer, "refresh"))
        assert refreshes >= 1
        assert log.count((writer, "sum")) == refreshes, (writer, log)
