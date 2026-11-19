"""A driver withdraws a waiting claim and reads his own claims from the staff bot (spec 2026-11-17 §6.4).

Every test drives the two routes the staff bot calls: `POST /api/v1/staff/expenses/<id>/withdraw` and
`GET /api/v1/staff/expenses` (My expenses). Trip cash lands through the admin "Record collection" route and is read
back through the admin session-detail route. Figures are the spec's worked example (collected 420,000, claim 150,000).
"""

from datetime import UTC, datetime, timedelta

import pytest

from business_app.models.driver_expense import DriverCashExpense
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from tests.integration._driver_expense_helpers import claim, insert_expense, my_expenses, withdraw
from tests.integration._prepayment_payout_helpers import collect_on_trip, session_detail

pytestmark = pytest.mark.integration


def test_withdrawing_restores_hand_in_now(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver, monkeypatch
):
    driver = second_delivery_driver
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    expense_id = claim(client, app, driver, 150000)[1]["data"]["expense"]["id"]
    calls = []
    monkeypatch.setattr(audit_logger, "log_event", lambda **kwargs: calls.append(kwargs))

    status, body = withdraw(client, app, driver, expense_id)

    assert (status, body["data"]["expense"]["status"], body["data"]["hand_in_now"]) == (200, "withdrawn", 420000.0)
    assert body["data"]["expenses_waiting_total"] == 0.0
    assert session_detail(client, admin_claim_headers, sid)["remaining_cash_to_submit"] == 420000.0
    db.session.expire_all()
    row = db.session.get(DriverCashExpense, expense_id)
    assert (row.status, row.withdrawn_at is not None, row.decided_at) == ("withdrawn", True, None)
    assert calls == [
        {
            "event_type": AuditEventType.PAYMENT_PROCESSED,
            "action": "driver_expense_withdrawn",
            "severity": AuditSeverity.MEDIUM,
            "resource_type": "driver_cash_expense",
            "resource_id": str(expense_id),
            "additional_data": {
                "expense_id": expense_id,
                "driver_user_id": driver.id,
                "driver_cash_session_id": sid,
                "amount": "150000.00",
            },
        }
    ]


def test_withdrawing_a_decided_or_withdrawn_claim_is_refused(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    approved_id = insert_expense(db, sid, driver.id, 100000, status="approved")
    pending_id = claim(client, app, driver, 50000)[1]["data"]["expense"]["id"]
    assert withdraw(client, app, driver, pending_id)[0] == 200

    for expense_id in (approved_id, pending_id):
        status, body = withdraw(client, app, driver, expense_id)
        assert (status, body["error_code"]) == (409, "DRIVER_EXPENSE_NOT_WITHDRAWABLE")
    db.session.expire_all()
    assert db.session.get(DriverCashExpense, approved_id).status == "approved"


def test_another_drivers_claim_is_not_found(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver, delivery_driver
):
    driver = second_delivery_driver
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    expense_id = claim(client, app, driver, 150000)[1]["data"]["expense"]["id"]

    for target in (expense_id, expense_id + 1000):
        status, body = withdraw(client, app, delivery_driver, target)
        assert (status, body["error_code"]) == (404, "DRIVER_EXPENSE_NOT_FOUND")
    db.session.expire_all()
    assert db.session.get(DriverCashExpense, expense_id).status == "pending"
    assert session_detail(client, admin_claim_headers, sid)["remaining_cash_to_submit"] == 270000.0


def test_my_expenses_lists_waiting_and_the_last_ten_decided(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    pending_id = claim(client, app, driver, 150000)[1]["data"]["expense"]["id"]
    base = datetime.now(UTC) - timedelta(days=1)
    kinds = {5: "withdrawn", 11: "withdrawn", 2: "rejected", 8: "rejected"}
    ids = {}
    for n in range(1, 12):
        at = base + timedelta(minutes=n)
        status = kinds.get(n, "approved")
        ids[n] = insert_expense(
            db,
            sid,
            driver.id,
            1000 * n,
            status=status,
            decided_at=at if status != "withdrawn" else None,
            withdrawn_at=at if status == "withdrawn" else None,
        )

    code, body = my_expenses(client, app, driver)

    assert code == 200
    data = body["data"]
    assert [r["id"] for r in data["waiting"]] == [pending_id]
    assert [r["id"] for r in data["decided"]] == [ids[n] for n in (11, 10, 9, 8, 7, 6, 5, 4, 3, 2)]
    assert {r["status"] for r in data["decided"]} == {"approved", "rejected", "withdrawn"}
    assert (data["hand_in_now"], data["expenses_waiting_total"]) == (270000.0, 150000.0)


def test_my_expenses_without_a_trip_or_claims_is_empty(app, client, db, second_delivery_driver):
    code, body = my_expenses(client, app, second_delivery_driver)

    assert (code, body["data"]) == (
        200,
        {"waiting": [], "decided": [], "hand_in_now": 0.0, "expenses_waiting_total": 0.0},
    )
